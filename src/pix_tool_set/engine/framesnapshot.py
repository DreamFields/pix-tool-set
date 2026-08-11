"""Per-edit frame snapshots: one directory per shader edit, beside the export.

The problem: a shader edit changes the whole frame, and comparing "before" with
"after" needs both full-frame dumps to still exist. But every dump run writes into
one shared output directory with a timestamped prefix, so telling which dump
belongs to which edit means reading timestamps and remembering what was patched
when. After three or four edits that bookkeeping is guesswork, and guessing wrong
means comparing the wrong pair and drawing a confident wrong conclusion.

A snapshot fixes the association at capture time. Each one is a numbered directory
next to the export::

    <capture>.pixcache/
        cpp/                        the export itself
        snapshots/
            0000-baseline/          before any edit
            0001-edit-PS-a1b2c3/    after the first edit
            0002-edit-CS-d4e5f6/    after the second
            index.json

and each carries a ``manifest.json`` recording *what the export looked like when
the dump was taken*: the edit ledger contents, the patched PSOs and stages, the
shader hashes, the source file used, and the injector state. The dump files
themselves live inside, so a snapshot is self-contained and can be deleted, moved
or archived as one unit.

Numbering is sequential and never reused, so ``0002`` is always the state after
the second edit even if ``0001`` was deleted. The number comes from the index, not
from counting directories, precisely so that deleting one does not silently
renumber the others and invalidate every note the user wrote down.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any, Optional

from . import exportstate
from .editledger import EditLedger

#: Directory name holding all snapshots, as a sibling of the export directory.
SNAPSHOTS_DIRNAME = "snapshots"
_INDEX_FILE = "index.json"
_MANIFEST_FILE = "manifest.json"


def snapshots_root(export_dir: Path | str) -> Path:
    """Where snapshots live for a given export.

    Deliberately a sibling of the export rather than a child: the export directory
    is what gets patched, rebuilt and restored from ``.orig`` backups, and a
    snapshot must not be at risk from any of that. It is also what
    ``session-open`` would overwrite on a re-export.
    """
    return Path(export_dir).parent / SNAPSHOTS_DIRNAME


def _read_index(root: Path) -> dict[str, Any]:
    path = root / _INDEX_FILE
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"version": 1, "next_sequence": 0, "snapshots": []}


def _write_index(root: Path, data: dict[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    data["version"] = 1
    data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    (root / _INDEX_FILE).write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _slugify(text: str, limit: int = 40) -> str:
    keep = [ch if (ch.isalnum() or ch in "-_") else "-" for ch in text]
    slug = "".join(keep).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug[:limit] or "snapshot"


def describe_edit_state(export_dir: Path | str) -> dict[str, Any]:
    """What edits are currently applied to the export.

    This is the identity of a snapshot: two dumps taken with the same edit state
    should agree, and one taken with a different edit state is what a comparison is
    *for*. Recorded from the ledger plus a filesystem scan, so a patch applied
    outside the ledger still shows up rather than making two different states look
    identical.
    """
    root = Path(export_dir)
    ledger = EditLedger(root)
    entries = ledger.list_entries()
    groups = ledger.groups()

    edits: list[dict[str, Any]] = []
    for group_id, members in groups.items():
        first = members[0]
        edits.append(
            {
                "group_id": group_id,
                "stage": first.get("stage"),
                "shader_hash": first.get("shader_hash"),
                "scope": first.get("scope"),
                "source_file": first.get("source_file"),
                "timestamp": first.get("timestamp"),
                "pso_ids": sorted(str(m.get("pso_id")) for m in members),
            }
        )
    edits.sort(key=lambda entry: entry.get("timestamp") or "")

    state = exportstate.inspect(root)
    return {
        "edit_count": len(edits),
        "patched_pso_count": len(entries),
        "edits": edits,
        "injectors_present": state.get("injectors_present", []),
        "export_clean": state.get("clean", False),
    }


def suggest_label(edit_state: dict[str, Any]) -> str:
    """A directory label describing the edit state, for human recognition.

    ``baseline`` when nothing is patched; otherwise the most recent edit's stage and
    a short shader hash, which is what a caller actually recognises later ("that was
    the PS one").
    """
    edits = edit_state.get("edits") or []
    if not edits:
        return "baseline"
    last = edits[-1]
    stage = (last.get("stage") or "shader").upper()
    digest = (last.get("shader_hash") or "")[:6].lower()
    suffix = f"-{digest}" if digest else ""
    more = f"-plus{len(edits) - 1}" if len(edits) > 1 else ""
    return _slugify(f"edit-{stage}{suffix}{more}")


def create(
    export_dir: Path | str,
    *,
    label: Optional[str] = None,
    note: str = "",
) -> dict[str, Any]:
    """Allocate the next snapshot directory and write its manifest.

    Returns the record, including ``path`` -- the directory a dump should write
    into. The manifest is written *before* the dump runs so that a crashed or
    interrupted run still leaves evidence of what was being attempted, rather than
    an unexplained directory of binary files.
    """
    export = Path(export_dir)
    root = snapshots_root(export)
    index = _read_index(root)

    sequence = int(index.get("next_sequence", 0))
    edit_state = describe_edit_state(export)
    chosen = _slugify(label) if label else suggest_label(edit_state)
    dirname = f"{sequence:04d}-{chosen}"
    path = root / dirname
    path.mkdir(parents=True, exist_ok=True)

    record = {
        "sequence": sequence,
        "label": chosen,
        "directory": dirname,
        "path": str(path),
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "note": note,
        "edit_state": edit_state,
        "is_baseline": edit_state["edit_count"] == 0,
        "complete": False,
    }

    (path / _MANIFEST_FILE).write_text(
        json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    index["next_sequence"] = sequence + 1
    index.setdefault("snapshots", []).append(
        {key: record[key] for key in ("sequence", "label", "directory", "created", "is_baseline")}
    )
    _write_index(root, index)
    return record


def finalise(
    export_dir: Path | str,
    record: dict[str, Any],
    *,
    dump_summary: dict[str, Any] | None = None,
    reliable: bool = True,
) -> dict[str, Any]:
    """Mark a snapshot complete and record what the dump produced.

    ``reliable`` carries through the dump's own ``frame_end_unreliable`` verdict. A
    snapshot whose dump did not finish cleanly is kept, not deleted -- deleting it
    would lose the evidence of the failure -- but it is flagged so a later
    comparison can refuse to trust it instead of quietly diffing partial data.
    """
    path = Path(record["path"])
    files = sorted(
        p.name for p in path.iterdir() if p.is_file() and p.name != _MANIFEST_FILE
    )
    total_bytes = sum((path / name).stat().st_size for name in files)

    record = dict(record)
    record.update(
        {
            "complete": True,
            "finalised": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "reliable": reliable,
            "file_count": len(files),
            "files": files[:200],
            "total_bytes": total_bytes,
            "dump_summary": dump_summary or {},
        }
    )
    (path / _MANIFEST_FILE).write_text(
        json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    root = snapshots_root(export_dir)
    index = _read_index(root)
    for entry in index.get("snapshots", []):
        if entry.get("sequence") == record["sequence"]:
            entry.update(
                {
                    "complete": True,
                    "reliable": reliable,
                    "file_count": len(files),
                    "total_bytes": total_bytes,
                }
            )
            break
    _write_index(root, index)
    return record


def read_manifest(path: Path | str) -> dict[str, Any]:
    manifest = Path(path) / _MANIFEST_FILE
    if not manifest.exists():
        return {}
    try:
        return json.loads(manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def listing(export_dir: Path | str) -> list[dict[str, Any]]:
    """Every snapshot on disk, newest sequence last.

    Read from the directories themselves rather than only the index, so a snapshot
    whose index entry was lost is still reported. An index entry with no directory
    is reported as ``missing`` instead of being dropped, because a comparison that
    silently skips a deleted snapshot would answer the wrong question.
    """
    root = snapshots_root(export_dir)
    if not root.exists():
        return []

    found: dict[int, dict[str, Any]] = {}
    for path in sorted(root.iterdir()):
        if not path.is_dir():
            continue
        manifest = read_manifest(path)
        sequence = manifest.get("sequence")
        if sequence is None:
            head = path.name.split("-", 1)[0]
            sequence = int(head) if head.isdigit() else -1
        entry = {
            "sequence": sequence,
            "directory": path.name,
            "path": str(path),
            "exists": True,
            "manifest_present": bool(manifest),
        }
        entry.update(
            {
                key: manifest.get(key)
                for key in (
                    "label",
                    "created",
                    "note",
                    "is_baseline",
                    "complete",
                    "reliable",
                    "file_count",
                    "total_bytes",
                )
                if key in manifest
            }
        )
        if manifest.get("edit_state"):
            entry["edit_count"] = manifest["edit_state"].get("edit_count")
            entry["edits"] = manifest["edit_state"].get("edits")
        found[sequence] = entry

    index = _read_index(root)
    for entry in index.get("snapshots", []):
        sequence = entry.get("sequence")
        if sequence is not None and sequence not in found:
            found[sequence] = {
                "sequence": sequence,
                "directory": entry.get("directory"),
                "path": str(root / str(entry.get("directory"))),
                "exists": False,
                "missing": True,
                "label": entry.get("label"),
                "created": entry.get("created"),
            }

    return [found[key] for key in sorted(found)]


def resolve(export_dir: Path | str, selector: int | str) -> Optional[dict[str, Any]]:
    """Find a snapshot by sequence number, directory name or label."""
    entries = listing(export_dir)
    text = str(selector)
    if text.isdigit():
        wanted = int(text)
        for entry in entries:
            if entry.get("sequence") == wanted:
                return entry
    for entry in entries:
        if entry.get("directory") == text or entry.get("label") == text:
            return entry
    return None


def remove(export_dir: Path | str, selector: int | str) -> dict[str, Any]:
    """Delete one snapshot directory, keeping its index entry as a tombstone.

    The index entry is kept on purpose: sequence numbers must never be reused, or a
    note saying "compare 0002 with 0004" would come to mean something different
    after a deletion.
    """
    entry = resolve(export_dir, selector)
    if entry is None:
        return {"removed": False, "reason": f"no snapshot matches {selector!r}"}
    path = Path(entry["path"])
    if path.exists():
        shutil.rmtree(path)
    root = snapshots_root(export_dir)
    index = _read_index(root)
    for record in index.get("snapshots", []):
        if record.get("sequence") == entry.get("sequence"):
            record["deleted"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            break
    _write_index(root, index)
    return {
        "removed": True,
        "sequence": entry.get("sequence"),
        "directory": entry.get("directory"),
        "note": "the sequence number is retired, not recycled",
    }
