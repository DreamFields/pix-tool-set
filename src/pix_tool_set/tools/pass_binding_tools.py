"""One-shot pass -> shader bindings, plus binding trust classification.

Report section 3 documented a three-step manual recipe (list-passes -> read
first_draw_index -> shader-bindings) with five separate pitfalls. Both tools here
collapse that into a single call and make the trust boundary explicit instead of
leaving it to the caller to discover.
"""

from __future__ import annotations

from typing import Any

from ..context import ToolContext
from ..engine.model import RootParameterKind, ShaderStage
from ..errors import invalid_argument, not_found
from ..results import ToolResult
from ._common import (
    PAGE_PARAMS,
    page_args,
    page_envelope,
    resolve_pass,
    tool,
    with_session,
)

_TRUST_NOTE = (
    "Declared registers come from the shader bytecode reflection and are authoritative. "
    "Runtime descriptor-table contents are reconstructed from the C++ export; when PIX did "
    "not record the real descriptor writes for a draw the table reads as filler, so every "
    "table reports a `trust` level rather than pretending the mapping is exact."
)

_STAGES = [stage.value for stage in ShaderStage]


def _classify_table(binding, declared_counts: dict[str, int]) -> dict[str, Any]:
    """Decide how much to trust one descriptor table's reconstructed contents."""
    views = binding.resolved_views
    kinds = {view.kind.value for view in views}
    rids = {view.resource_id for view in views if view.resource_id is not None}

    expected = 0
    for kind in kinds:
        expected = max(expected, declared_counts.get(kind, 0))

    if not views:
        trust = "unavailable"
        reason = (
            "PIX recorded no descriptor writes for this table window; the shader's declared "
            "registers are the only reliable answer."
        )
    elif len(rids) == 1 and expected > 1:
        trust = "filler"
        reason = (
            f"All {len(views)} slot(s) point at one resource while the shader declares "
            f"{expected}; this window holds PIX initialisation filler, not the real binding."
        )
    elif binding.table_confidence == "exact" and expected and len(views) >= expected:
        trust = "reliable"
        reason = "Table is fully bounded and slot count matches the shader declaration."
    else:
        trust = "partial"
        reason = (
            f"Table expanded to {len(views)} slot(s) "
            f"(confidence={binding.table_confidence or 'unknown'}); treat register->resource "
            "mapping as unconfirmed."
        )
    return {"trust": trust, "reason": reason, "distinct_resource_ids": sorted(rids)}


def _collect(capture, draw, stage_filter: str | None, max_views: int) -> dict[str, Any]:
    shaders = [draw.shader(stage_filter)] if stage_filter else draw.shaders
    shaders = [shader for shader in shaders if shader is not None]

    declared_counts = {"SRV": 0, "UAV": 0, "CBV": 0, "SAMPLER": 0}
    stage_rows: list[dict[str, Any]] = []
    for shader in shaders:
        declared = shader.resource_bindings
        for entry in declared:
            ident = entry.get("id", "")
            if ident.startswith("T"):
                declared_counts["SRV"] += 1
            elif ident.startswith("U"):
                declared_counts["UAV"] += 1
            elif ident.startswith("CB"):
                declared_counts["CBV"] += 1
            elif ident.startswith("S"):
                declared_counts["SAMPLER"] += 1
        stage_rows.append(
            {
                "stage": shader.stage.value,
                "shader": shader.to_dict(),
                "declared_count": len(declared),
                "declared_registers": declared,
                "num_threads": shader.num_threads,
            }
        )

    signature = capture.root_signatures.get(draw.root_signature_id or -1)
    tables: list[dict[str, Any]] = []
    root_descriptors: list[dict[str, Any]] = []

    for binding in draw.bindings:
        if binding.kind is RootParameterKind.DESCRIPTOR_TABLE:
            row = binding.to_dict(max_views=max_views)
            row.update(_classify_table(binding, declared_counts))
            parameter = signature.parameter(binding.root_index) if signature else None
            if parameter is not None:
                row["declared_ranges"] = parameter.ranges
                row["declared_descriptor_count"] = parameter.num_descriptors
            resolved = []
            for view in binding.resolved_views[:max_views]:
                item = view.to_dict()
                resource = (
                    capture.resource(view.resource_id)
                    if view.resource_id is not None
                    else None
                )
                if resource is not None:
                    item["resource"] = resource.to_dict()
                resolved.append(item)
            row["resolved"] = resolved
            tables.append(row)
        else:
            entry: dict[str, Any] = {
                "root_index": binding.root_index,
                "binding_kind": binding.kind.value,
                "resource_id": binding.resource_id,
                "byte_offset": binding.va_offset,
                "trust": "reliable" if binding.resource_id is not None else "unavailable",
            }
            resource = (
                capture.resource(binding.resource_id)
                if binding.resource_id is not None
                else None
            )
            if resource is not None:
                entry["resource"] = resource.to_dict()
            root_descriptors.append(entry)

    return {
        "draw_index": draw.index,
        "global_id": draw.global_id,
        "api": draw.api,
        "pso_id": draw.pso_id,
        "root_signature_id": draw.root_signature_id,
        "stages": stage_rows,
        "declared_totals": declared_counts,
        "root_descriptors": root_descriptors,
        "descriptor_tables": tables,
        "descriptor_heap_ids": draw.descriptor_heap_ids,
    }


@tool(
    name="pass-bindings",
    summary=(
        "Shader bindings for a whole pass in one call: resolves the pass by name or index, "
        "picks its representative draws, and returns each shader's declared registers "
        "together with the runtime bindings and how much to trust them."
    ),
    category="shaders",
    parameters=with_session(
        pass_name={"type": "string", "description": "Pass name (substring match)."},
        pass_index={"type": "integer", "description": "Pass index from list-passes."},
        global_id={
            "type": "integer",
            "description": "PIX GUI 'Global ID' of a draw/dispatch inside the pass.",
        },
        queue_id={
            "type": "integer",
            "description": (
                "PIX GUI 'Queue ID' of any event in the pass, including the pass marker "
                "itself. Present on every event, unlike Global ID."
            ),
        },
        stage={
            "type": "string",
            "enum": _STAGES,
            "description": "Restrict to one shader stage, e.g. CS.",
        },
        max_views={
            "type": "integer",
            "description": "Views to list per descriptor table. Default 128 (large enough for UE5 tables).",
        },
        per_pso={
            "type": "boolean",
            "description": "Report one representative draw per distinct PSO. Default true.",
        },
        max_draws={"type": "integer", "description": "Cap on reported draws. Default 8."},
        all_matches={
            "type": "boolean",
            "description": "When the name matches several passes, report all of them.",
        },
    ),
    returns=(
        "Pass identity, per-stage declared registers (authoritative), runtime root "
        "descriptors and descriptor tables each tagged with a trust level."
    ),
    examples=[
        'pix-tool-set pass-bindings --pass-name TileClassificationBuildLists --stage CS',
        "pix-tool-set pass-bindings --pass-index 270",
        "pix-tool-set pass-bindings --global-id 3893",
        "pix-tool-set pass-bindings --queue-id 18704",
        'pix-tool-set pass-bindings --pass-name TileClassification --all-matches',
    ],
    notes=_TRUST_NOTE,
)
def pass_bindings(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    max_views = int(args.get("max_views") or 128)
    max_draws = int(args.get("max_draws") or 8)
    per_pso = args.get("per_pso")
    per_pso = True if per_pso is None else bool(per_pso)
    stage_filter = args.get("stage")

    if bool(args.get("all_matches")) and args.get("pass_name"):
        needle = str(args["pass_name"]).lower()
        entries = [p for p in capture.passes if needle in p["name"].lower()]
        if not entries:
            raise not_found("pass", args["pass_name"], "Run list-passes to see valid names.")
    else:
        entries = [resolve_pass(capture, args)]

    pass_payloads: list[dict[str, Any]] = []
    trust_tally: dict[str, int] = {}

    for entry in entries:
        marker_path = tuple(entry["marker_path"])
        draws = [d for d in capture.draw_calls if d.marker_path == marker_path]
        if per_pso:
            chosen: list[Any] = []
            seen: set[int | None] = set()
            for draw in draws:
                if draw.pso_id in seen:
                    continue
                seen.add(draw.pso_id)
                chosen.append(draw)
        else:
            chosen = draws

        draw_rows = []
        for draw in chosen[:max_draws]:
            row = _collect(capture, draw, stage_filter, max_views)
            for table in row["descriptor_tables"]:
                trust_tally[table["trust"]] = trust_tally.get(table["trust"], 0) + 1
            for descriptor in row["root_descriptors"]:
                trust_tally[descriptor["trust"]] = trust_tally.get(descriptor["trust"], 0) + 1
            draw_rows.append(row)

        pass_payloads.append(
            {
                "pass_index": entry["pass_index"],
                "name": entry["name"],
                "marker_path": entry["marker_path"],
                "event_count": entry["event_count"],
                "draw_count": entry["draw_count"],
                "dispatch_count": entry["dispatch_count"],
                "distinct_pso_ids": entry["pso_ids"],
                "first_global_id": entry.get("first_global_id"),
                "first_queue_id": entry.get("first_queue_id"),
                "marker_queue_id": entry.get("marker_queue_id"),
                "total_draws_in_pass": len(draws),
                "draws_reported": len(draw_rows),
                "draws": draw_rows,
            }
        )

    result = ToolResult.success(
        {
            "passes": pass_payloads,
            "pass_count": len(pass_payloads),
            "trust_summary": trust_tally,
            "trust_levels": {
                "reliable": "Value is taken straight from the recorded call or matches the shader declaration.",
                "partial": "Reconstructed but unconfirmed; do not rely on register->resource mapping.",
                "filler": "Window holds PIX initialisation filler; the real descriptors were not recorded.",
                "unavailable": "No descriptor data recorded for this table.",
            },
        }
    )
    if trust_tally.get("filler") or trust_tally.get("unavailable"):
        result.degrade(
            "Some runtime descriptor tables could not be recovered from the export; "
            "use the declared_registers of each stage as the authoritative answer.",
            filler_tables=trust_tally.get("filler", 0),
            unavailable_tables=trust_tally.get("unavailable", 0),
        )
    if len(entries) == 1 and pass_payloads and pass_payloads[0]["total_draws_in_pass"] > max_draws:
        result.add_diagnostic(
            "info",
            f"Pass has {pass_payloads[0]['total_draws_in_pass']} draws; reported "
            f"{pass_payloads[0]['draws_reported']}. Raise --max-draws or set --per-pso false for more.",
        )
    return result


@tool(
    name="find-pass",
    summary=(
        "Resolve a pass name to the identifiers other tools need: pass index, draw index, "
        "global id and PSO ids. Lists every match so ambiguous names are obvious."
    ),
    category="events",
    parameters=with_session(
        PAGE_PARAMS,
        name={"type": "string", "description": "Pass name or substring to look up."},
        global_id={
            "type": "integer",
            "description": "PIX GUI 'Global ID'; returns the pass containing that action.",
        },
        queue_id={
            "type": "integer",
            "description": "PIX GUI 'Queue ID'; works for markers as well as actions.",
        },
    ),
    returns="Every matching pass with the ids needed by draw-state / shader-bindings.",
    examples=[
        'pix-tool-set find-pass --name TileClassificationBuildLists',
        "pix-tool-set find-pass --global-id 3893",
        "pix-tool-set find-pass --queue-id 18704",
    ],
)
def find_pass(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    offset, limit = page_args(args, default_limit=25)

    def row(entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "pass_index": entry["pass_index"],
            "name": entry["name"],
            "marker_path": entry["marker_path"],
            "subsystem": entry["marker_path"][-2] if len(entry["marker_path"]) > 1 else "",
            "draw_index": entry["first_draw_index"],
            "last_draw_index": entry["last_draw_index"],
            "global_id": entry["first_global_id"],
            "queue_id": entry.get("first_queue_id"),
            "marker_queue_id": entry.get("marker_queue_id"),
            "event_count": entry["event_count"],
            "draw_count": entry["draw_count"],
            "dispatch_count": entry["dispatch_count"],
            "pso_ids": entry["pso_ids"],
        }

    global_id = args.get("global_id")
    queue_id = args.get("queue_id")
    if global_id is not None or queue_id is not None:
        entry = capture.find_pass_by_event(global_id=global_id, queue_id=queue_id)
        label = f"global_id={global_id}" if global_id is not None else f"queue_id={queue_id}"
        if entry is None:
            raise not_found(
                "pass",
                label,
                "Use locate-event to confirm the id exists in this capture.",
            )
        return ToolResult.success(
            {
                "query": label,
                "matches": [row(entry)],
                "next_step": (
                    "Feed draw_index into `pass-bindings`, `draw-state` or `shader-bindings`, "
                    "or pass the same --global-id/--queue-id straight to `pass-bindings`."
                ),
                **page_envelope(1, 0, limit, 1),
            }
        )

    if not args.get("name"):
        raise invalid_argument("name/global_id/queue_id", "provide one of them")
    needle = str(args["name"]).lower()

    matches = [row(e) for e in capture.passes if needle in e["name"].lower()]

    total = len(matches)
    window = matches[offset : offset + limit] if limit else matches[offset:]
    if not matches:
        raise not_found("pass", args["name"], "Run list-passes to browse available names.")

    result = ToolResult.success(
        {
            "query": args["name"],
            "matches": window,
            "next_step": (
                "Feed draw_index into `pass-bindings`, `draw-state` or `shader-bindings`."
            ),
            **page_envelope(total, offset, limit, len(window)),
        }
    )
    if total > 1:
        result.add_diagnostic(
            "info",
            f"{total} passes share this name; use marker_path or subsystem to pick the right one.",
        )
    return result
