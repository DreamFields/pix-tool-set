"""Per-edit frame snapshots must stay distinguishable and never renumber.

What this guards: a shader edit changes the whole frame, so comparing before with
after needs both full-frame dumps to survive and to be unambiguously attributable
to an edit state. The failure mode is subtle -- if two dumps can be confused, the
comparison answers the wrong question and looks perfectly plausible doing it.

Runs entirely on a synthetic export directory; no capture, build or replay needed.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pix_tool_set.engine import framesnapshot  # noqa: E402
from pix_tool_set.engine.editledger import EditLedger  # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'ok' if ok else 'FAIL'}]   {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label + (f" -- {detail}" if detail else ""))


def make_export(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "CreatePSOs.cpp").write_text("void CreatePipelineState_1() {}\n", encoding="utf-8")
    (root / "RenderFrame.cpp").write_text("void RenderFrame() {}\n", encoding="utf-8")


def fake_dump(snapshot_path: Path, resource_id: int, payload: bytes) -> None:
    """Imitate what the uav probe writes: a .bin plus its sidecar."""
    stem = f"framedump_20260811-120000_{resource_id}"
    (snapshot_path / f"{stem}.bin").write_bytes(payload)
    (snapshot_path / f"{stem}.bin.txt").write_text(
        "format=DXGI_FORMAT_R8G8B8A8_UNORM\nwidth=2\nheight=1\nrowPitch=8\n",
        encoding="utf-8",
    )


def main() -> int:
    print("=" * 74)
    print("per-edit frame snapshots: numbering, attribution, retirement")
    print("=" * 74)

    with tempfile.TemporaryDirectory(prefix="pixts-snapshot-") as tmp:
        export = Path(tmp) / "cpp"
        make_export(export)

        print()
        print("[1] the first snapshot with no edits is a baseline")
        first = framesnapshot.create(export)
        check("sequence starts at 0", first["sequence"] == 0, str(first["sequence"]))
        check("labelled baseline", first["label"] == "baseline", first["label"])
        check("is_baseline is True", first["is_baseline"] is True)
        check("directory is zero-padded", first["directory"] == "0000-baseline",
              first["directory"])
        check("lives beside the export, not inside it",
              Path(first["path"]).parent.parent == export.parent,
              first["path"])
        check("a manifest exists before any dump is written",
              (Path(first["path"]) / "manifest.json").exists())
        fake_dump(Path(first["path"]), 756, b"\x00" * 8)
        first = framesnapshot.finalise(export, first, reliable=True)
        check("finalised snapshot counts its files", first["file_count"] == 2,
              str(first["file_count"]))

        print()
        print("[2] after an edit, the snapshot records what was patched")
        ledger = EditLedger(export)
        ledger.add_group(
            stage="PS",
            shader_hash="a1b2c3d4e5f6",
            scope="pso",
            target_psos=[3245],
            source_file="BasePassPixelShader.usf",
        )
        second = framesnapshot.create(export)
        check("sequence increments", second["sequence"] == 1, str(second["sequence"]))
        check("label names the stage and shader",
              second["label"].startswith("edit-PS-a1b2c3"), second["label"])
        check("not a baseline", second["is_baseline"] is False)
        check("the edit is recorded in the manifest",
              second["edit_state"]["edit_count"] == 1,
              str(second["edit_state"]["edit_count"]))
        check("the patched PSO is named",
              "3245" in second["edit_state"]["edits"][0]["pso_ids"],
              str(second["edit_state"]["edits"][0]["pso_ids"]))
        fake_dump(Path(second["path"]), 756, b"\xff" * 8)
        second = framesnapshot.finalise(export, second, reliable=True)

        print()
        print("[3] a second edit produces a third, distinct directory")
        ledger.add_group(
            stage="CS",
            shader_hash="99887766",
            scope="shader",
            target_psos=[100, 101],
            source_file="LightingCS.usf",
        )
        third = framesnapshot.create(export)
        check("sequence increments again", third["sequence"] == 2, str(third["sequence"]))
        check("two edits now recorded", third["edit_state"]["edit_count"] == 2,
              str(third["edit_state"]["edit_count"]))
        check("label reflects the newest edit plus a count",
              "CS" in third["label"] and "plus1" in third["label"], third["label"])
        dirs = {first["directory"], second["directory"], third["directory"]}
        check("all three directories are distinct", len(dirs) == 3, str(sorted(dirs)))
        fake_dump(Path(third["path"]), 756, b"\xff" * 8)
        third = framesnapshot.finalise(export, third, reliable=True)

        print()
        print("[4] listing reports them in order with their edit counts")
        entries = framesnapshot.listing(export)
        check("three snapshots listed", len(entries) == 3, str(len(entries)))
        check("ordered by sequence",
              [e["sequence"] for e in entries] == [0, 1, 2],
              str([e["sequence"] for e in entries]))
        check("edit counts carried through",
              [e.get("edit_count") for e in entries] == [0, 1, 2],
              str([e.get("edit_count") for e in entries]))

        print()
        print("[5] an unreliable dump is kept but flagged")
        fourth = framesnapshot.create(export, note="settle window too short")
        fake_dump(Path(fourth["path"]), 756, b"\x01" * 8)
        fourth = framesnapshot.finalise(export, fourth, reliable=False)
        check("the directory still exists", Path(fourth["path"]).exists())
        check("reliable is False", fourth["reliable"] is False)
        check("the note survives", fourth["note"] == "settle window too short",
              fourth["note"])

        print()
        print("[6] deleting a snapshot retires its number, it is never reused")
        outcome = framesnapshot.remove(export, 1)
        check("removal reported", outcome["removed"] is True)
        check("the directory is gone", not Path(second["path"]).exists())
        after = framesnapshot.listing(export)
        missing = [e for e in after if e.get("missing")]
        check("the deleted snapshot is reported as missing, not dropped",
              len(missing) == 1 and missing[0]["sequence"] == 1,
              str([(e["sequence"], e.get("missing")) for e in after]))
        fifth = framesnapshot.create(export)
        check("the next sequence is 4, not the freed 1",
              fifth["sequence"] == 4, str(fifth["sequence"]))

        print()
        print("[7] resolve accepts a number, a directory name and a label")
        by_number = framesnapshot.resolve(export, 0)
        by_dir = framesnapshot.resolve(export, "0000-baseline")
        by_label = framesnapshot.resolve(export, "baseline")
        check("by sequence number", by_number is not None and by_number["sequence"] == 0)
        check("by directory name", by_dir is not None and by_dir["sequence"] == 0)
        check("by label", by_label is not None and by_label["sequence"] == 0)
        check("an unknown selector resolves to None",
              framesnapshot.resolve(export, "nope") is None)

        print()
        print("[8] the index on disk is valid JSON and records the retirement")
        index_path = framesnapshot.snapshots_root(export) / "index.json"
        payload = json.loads(index_path.read_text(encoding="utf-8"))
        check("next_sequence advanced past every allocation",
              payload["next_sequence"] == 5, str(payload["next_sequence"]))
        tombstone = [s for s in payload["snapshots"] if s.get("sequence") == 1]
        check("the deleted entry carries a deletion timestamp",
              bool(tombstone) and "deleted" in tombstone[0],
              str(tombstone))

    print()
    print("=" * 74)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)})")
        for line in FAILURES:
            print("  - " + line)
        return 1
    print("PASSED: every edit gets its own snapshot, and numbers are never recycled.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
