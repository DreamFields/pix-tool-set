"""Snapshot tools: browse and compare the per-edit frame dumps.

``frame-replay-dump --snapshot`` writes each full-frame dump into its own numbered
directory beside the export, together with a manifest of the shader edits that were
applied at the time. These tools are the read side of that:

  * **snapshot-list** -- what snapshots exist, what was patched in each, whether the
    replay that produced them finished cleanly.
  * **snapshot-compare** -- diff two snapshots resource by resource, so "what did
    this edit actually change across the whole frame?" is one call.
  * **snapshot-remove** -- delete one, retiring its sequence number rather than
    recycling it.

Why comparison lives here rather than in ``shader-edit-diff``: that tool compares
one resource before and after a single edit within one session. This compares two
*complete frames* captured at different times, which is the only way to answer
"edit A changed 3 resources, edit B changed 40" -- and the only way to notice that
an edit changed something nobody was looking at.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..context import ToolContext
from ..engine import framesnapshot, uavprobe
from ..errors import invalid_argument, not_found
from ..results import ToolResult
from ._common import tool, with_session
from .replay_render_tools import _export_root


@tool(
    name="snapshot-list",
    summary=(
        "List every per-edit frame snapshot: which shader edits were applied, how "
        "many resources were dumped, and whether the replay finished cleanly."
    ),
    category="meta",
    parameters=with_session(
        detail={
            "type": "boolean",
            "description": "Include the full edit list and file names for each snapshot.",
        },
    ),
    returns="Ordered snapshot list with edit state, file counts and reliability flags.",
    examples=[
        "pix-tool-set snapshot-list",
        "pix-tool-set snapshot-list --detail",
    ],
    notes=(
        "Snapshots live in <capture>.pixcache/snapshots/, a sibling of the export "
        "directory so that patching, rebuilding or restoring the export cannot "
        "disturb them. Sequence numbers are never reused: a deleted snapshot leaves a "
        "tombstone in the index and is reported with missing: true, because silently "
        "renumbering would change what a note like 'compare 2 with 4' refers to."
    ),
)
def snapshot_list(args: dict[str, Any], context: ToolContext) -> ToolResult:
    root = _export_root(context, args)
    entries = framesnapshot.listing(root)
    detail = bool(args.get("detail"))

    rows: list[dict[str, Any]] = []
    for entry in entries:
        row = dict(entry)
        if not detail:
            row.pop("edits", None)
            row.pop("files", None)
        rows.append(row)

    present = [entry for entry in entries if entry.get("exists")]
    unreliable = [
        entry for entry in present if entry.get("complete") and entry.get("reliable") is False
    ]
    incomplete = [entry for entry in present if entry.get("complete") is False]

    data = {
        "snapshots_root": str(framesnapshot.snapshots_root(root)),
        "snapshots": rows,
        "count": len(rows),
        "present_count": len(present),
        "total_bytes": sum(int(entry.get("total_bytes") or 0) for entry in present),
        "current_edit_state": framesnapshot.describe_edit_state(root),
    }
    result = ToolResult.success(data)
    if not rows:
        result.add_diagnostic(
            "info",
            "No snapshots yet. Run frame-replay-dump --snapshot to capture the current "
            "state of the frame; do it once before any edit to get a baseline.",
        )
    if incomplete:
        result.add_diagnostic(
            "warning",
            f"{len(incomplete)} snapshot(s) have no finalised manifest — the dump was "
            "interrupted. Their directories are kept so the failure is visible, but do "
            "not compare against them.",
        )
    if unreliable:
        result.add_diagnostic(
            "warning",
            f"{len(unreliable)} snapshot(s) were captured from a replay that did not "
            "finish cleanly (reliable: false). A comparison using them may attribute "
            "missing data to the edit.",
        )
    return result


@tool(
    name="snapshot-compare",
    summary=(
        "Diff two per-edit frame snapshots resource by resource: what the edit "
        "changed across the whole frame, not just the resource you were watching."
    ),
    category="meta",
    parameters=with_session(
        a={
            "type": "string",
            "description": "First snapshot: sequence number, directory name or label.",
        },
        b={
            "type": "string",
            "description": "Second snapshot. Defaults to the newest one.",
        },
        max_resources={
            "type": "integer",
            "description": "Cap the resources compared. Default 64.",
        },
        changed_only={
            "type": "boolean",
            "description": "Only report resources whose bytes differ. Default false.",
        },
    ),
    returns=(
        "Per-resource comparison with byte-level equality, per-channel statistics "
        "deltas, and the set of resources present in only one snapshot."
    ),
    examples=[
        "pix-tool-set snapshot-compare --a 0 --b 1",
        "pix-tool-set snapshot-compare --a baseline --changed-only",
    ],
    notes=(
        "Comparison is by raw dump bytes first: identical bytes mean the edit provably "
        "did not touch that resource, which is a stronger statement than equal "
        "statistics. Resources present in only one snapshot are reported separately "
        "rather than counted as changed, because appearing or disappearing usually "
        "means the dump was capped or filtered differently, not that the edit created "
        "a resource. A snapshot flagged reliable: false is refused unless the caller "
        "insists, since partial dumps look exactly like large changes."
    ),
)
def snapshot_compare(args: dict[str, Any], context: ToolContext) -> ToolResult:
    root = _export_root(context, args)
    entries = [entry for entry in framesnapshot.listing(root) if entry.get("exists")]
    if len(entries) < 2:
        raise invalid_argument(
            "a/b",
            f"need at least two snapshots to compare, found {len(entries)}. "
            "Run frame-replay-dump --snapshot before and after an edit.",
        )

    if args.get("a") is None:
        raise invalid_argument("a", "name the first snapshot (sequence, directory or label)")
    first = framesnapshot.resolve(root, str(args["a"]))
    if first is None or not first.get("exists"):
        raise not_found("snapshot", str(args["a"]), "Run snapshot-list to see valid ids.")

    if args.get("b") is not None:
        second = framesnapshot.resolve(root, str(args["b"]))
        if second is None or not second.get("exists"):
            raise not_found("snapshot", str(args["b"]), "Run snapshot-list to see valid ids.")
    else:
        second = entries[-1]
        if second.get("sequence") == first.get("sequence"):
            raise invalid_argument(
                "b",
                "the newest snapshot is the same as --a; name a second one explicitly",
            )

    max_resources = int(args.get("max_resources") or 64)
    changed_only = bool(args.get("changed_only"))

    def index_dumps(entry: dict[str, Any]) -> dict[int, Path]:
        """resource_id -> dump path, for one snapshot directory."""
        out: dict[int, Path] = {}
        for path in sorted(Path(entry["path"]).glob("framedump_*_*.bin")):
            stem = path.stem  # framedump_<stamp>_<rid>
            tail = stem.rsplit("_", 1)[-1]
            if tail.isdigit():
                out[int(tail)] = path
        return out

    dumps_a = index_dumps(first)
    dumps_b = index_dumps(second)
    shared = sorted(set(dumps_a) & set(dumps_b))
    only_a = sorted(set(dumps_a) - set(dumps_b))
    only_b = sorted(set(dumps_b) - set(dumps_a))

    # Resource names make the report readable but are not what it is for. If the
    # export can no longer be parsed (re-exported, moved, deleted) the comparison of
    # already-captured bytes is still perfectly valid, so a parse failure must not
    # block it -- the ids are enough to answer the question.
    capture = None
    capture_error: str | None = None
    try:
        capture = context.capture(args)
    except Exception as exc:
        capture_error = f"{type(exc).__name__}: {exc}"

    comparisons: list[dict[str, Any]] = []
    changed = identical = undecidable = 0

    for rid in shared[:max_resources]:
        resource = capture.resource(rid) if capture is not None else None
        row: dict[str, Any] = {
            "resource_id": rid,
            "name": resource.name if resource is not None else None,
            "description": resource.describe() if resource is not None else None,
        }
        try:
            blob_a = dumps_a[rid].read_bytes()
            blob_b = dumps_b[rid].read_bytes()
        except OSError as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
            undecidable += 1
            comparisons.append(row)
            continue

        if len(blob_a) != len(blob_b):
            # Different sizes cannot be diffed byte-wise, and a size change is not
            # something a shader edit does; report it instead of forcing a verdict.
            row.update(
                {
                    "verdict": "size_differs",
                    "bytes_a": len(blob_a),
                    "bytes_b": len(blob_b),
                    "reason": (
                        "the two dumps are different sizes, so they were not captured "
                        "with the same footprint; this is a capture difference, not an "
                        "effect of the edit"
                    ),
                }
            )
            undecidable += 1
            comparisons.append(row)
            continue

        same = blob_a == blob_b
        row["verdict"] = "identical" if same else "changed"
        row["bytes"] = len(blob_a)
        if same:
            identical += 1
            if changed_only:
                continue
        else:
            changed += 1
            differing = sum(1 for x, y in zip(blob_a, blob_b) if x != y)
            row["differing_bytes"] = differing
            row["differing_share_percent"] = round(100.0 * differing / max(len(blob_a), 1), 3)
            # Statistics come second: they say *how* it changed, once byte equality
            # has already settled *whether* it changed.
            try:
                meta = uavprobe.read_sidecar(dumps_a[rid])
                image_a = uavprobe.as_image(uavprobe.depad(blob_a, meta), meta)
                image_b = uavprobe.as_image(uavprobe.depad(blob_b, meta), meta)
                row["statistics_a"] = uavprobe.statistics(image_a)
                row["statistics_b"] = uavprobe.statistics(image_b)
            except Exception as exc:
                row["statistics_error"] = f"{type(exc).__name__}: {exc}"
        comparisons.append(row)

    def describe(entry: dict[str, Any]) -> dict[str, Any]:
        return {
            key: entry.get(key)
            for key in (
                "sequence",
                "label",
                "directory",
                "created",
                "note",
                "edit_count",
                "is_baseline",
                "reliable",
                "file_count",
            )
        }

    data = {
        "a": describe(first),
        "b": describe(second),
        "a_edits": first.get("edits") or [],
        "b_edits": second.get("edits") or [],
        "shared_resource_count": len(shared),
        "compared_count": len(comparisons),
        "changed_count": changed,
        "identical_count": identical,
        "undecidable_count": undecidable,
        "only_in_a": only_a,
        "only_in_b": only_b,
        "comparisons": comparisons,
        "capped": len(shared) > max_resources,
    }
    if capture_error is not None:
        data["resource_names_unavailable"] = capture_error
    result = ToolResult.success(data)

    if capture_error is not None:
        result.add_diagnostic(
            "info",
            "Resource names are unavailable because the export could not be parsed "
            f"({capture_error}). The byte comparison itself is unaffected — it works "
            "from the captured dumps, not from the export.",
        )

    for entry, label in ((first, "a"), (second, "b")):
        if entry.get("reliable") is False:
            result.degrade(
                f"Snapshot {label} ({entry.get('directory')}) was captured from a replay "
                "that did not finish cleanly. Differences may be missing data rather "
                "than an effect of the edit.",
                reason="snapshot manifest reports reliable: false",
            )

    if only_a or only_b:
        result.add_diagnostic(
            "info",
            f"{len(only_a)} resource(s) appear only in A and {len(only_b)} only in B. "
            "That is normally a different --max-resources or --resource-types, not an "
            "effect of the edit; they are excluded from the changed count.",
        )
    if data["capped"]:
        result.add_diagnostic(
            "warning",
            f"Compared {max_resources} of {len(shared)} shared resources; raise "
            "--max-resources for the full picture.",
        )
    result.add_diagnostic(
        "info",
        f"{changed} resource(s) changed, {identical} identical byte for byte. "
        "Identical bytes prove the edit did not touch that resource.",
    )
    return result


@tool(
    name="snapshot-remove",
    summary="Delete one frame snapshot directory, retiring its sequence number.",
    category="meta",
    parameters=with_session(
        snapshot={
            "type": "string",
            "description": "Sequence number, directory name or label to delete.",
        },
        required=["snapshot"],
    ),
    returns="Whether the snapshot was removed.",
    examples=["pix-tool-set snapshot-remove --snapshot 3"],
    notes=(
        "The sequence number is retired rather than recycled, so an earlier note "
        "referring to snapshot 3 never comes to mean a different capture. The index "
        "keeps a tombstone and snapshot-list reports the entry with missing: true."
    ),
)
def snapshot_remove(args: dict[str, Any], context: ToolContext) -> ToolResult:
    root = _export_root(context, args)
    outcome = framesnapshot.remove(root, str(args["snapshot"]))
    if not outcome.get("removed"):
        raise not_found(
            "snapshot",
            str(args["snapshot"]),
            "Run snapshot-list to see the available snapshots.",
        )
    return ToolResult.success(outcome)
