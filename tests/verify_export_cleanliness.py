"""replay-reset must not call an export clean while a probe is still injected.

The regression this guards: three separate mechanisms inject into a pixtool C++
export -- shader-edit-apply, the read-uav readback probe, and the
pixel-history-replay sampler -- but replay-reset only knew about the first. After a
``pixel-history-replay --keep-probe`` run it reported ``clean: true`` with 16
injected sample calls still sitting in CommandLists_*.cpp, which would then be
compiled into the next replay.

The test works on a synthetic export copy, not the user's real one, so it can
inject freely without touching a 2.4 GB capture's cache.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pix_tool_set.engine import exportstate, pixelprobe, uavprobe  # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'ok' if ok else 'FAIL'}]   {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label + (f" -- {detail}" if detail else ""))


def make_fake_export(root: Path) -> None:
    """A minimal export: enough files for the markers to have somewhere to live."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "RenderFrame.cpp").write_text(
        "#include \"pch.h\"\nvoid RenderFrame() {\n    g_perFrameBuffers.clear();\n}\n",
        encoding="utf-8",
    )
    (root / "CommandLists_000.cpp").write_text(
        "void PopulateCommandList_3007_0() {\n    // GlobalId        = 1\n    {\n    }\n}\n",
        encoding="utf-8",
    )
    (root / "CreatePSOs.cpp").write_text(
        "void CreatePipelineState_100() {\n}\n", encoding="utf-8"
    )
    (root / "CMakeLists.txt").write_text("project(Replay)\n", encoding="utf-8")


def main() -> int:
    print("=" * 74)
    print("replay-reset cleanliness reporting across all three injectors")
    print("=" * 74)

    with tempfile.TemporaryDirectory(prefix="pixts-exportstate-") as tmp:
        root = Path(tmp) / "cpp"
        make_fake_export(root)

        print()
        print("[1] a pristine export is clean")
        state = exportstate.inspect(root)
        check("clean is True", state["clean"] is True)
        check("no injectors listed", state["injectors_present"] == [],
              str(state["injectors_present"]))

        print()
        print("[2] a shader-edit patch is detected")
        (root / "edited_CreatePipelineState_100_PS.dxil").write_bytes(b"DXBC")
        state = exportstate.inspect(root)
        check("clean is False", state["clean"] is False)
        check("shader-edit-apply named as the injector",
              "shader-edit-apply" in state["injectors_present"],
              str(state["injectors_present"]))
        check("the bytecode file is listed",
              "edited_CreatePipelineState_100_PS.dxil"
              in state["shader_edit"]["bytecode_files"])
        (root / "edited_CreatePipelineState_100_PS.dxil").unlink()

        print()
        print("[3] a read-uav probe marker is detected")
        render = root / "RenderFrame.cpp"
        render.write_text(
            render.read_text(encoding="utf-8") + f"\n{uavprobe.MARKER}\n",
            encoding="utf-8",
        )
        state = exportstate.inspect(root)
        check("clean is False", state["clean"] is False)
        check("read-uav named as the injector",
              "read-uav" in state["injectors_present"],
              str(state["injectors_present"]))
        check("shader edits still reported clean",
              state["shader_edit"]["injected"] is False)
        make_fake_export(root)

        print()
        print("[4] a pixel-history probe marker is detected -- the reported regression")
        cmd = root / "CommandLists_000.cpp"
        cmd.write_text(
            cmd.read_text(encoding="utf-8") + f"\n{pixelprobe.MARKER} (slot 0)\n",
            encoding="utf-8",
        )
        (root / "PixToolSetPixelProbe.h").write_text("// probe\n", encoding="utf-8")
        state = exportstate.inspect(root)
        check("clean is False", state["clean"] is False)
        check("pixel-history-replay named as the injector",
              "pixel-history-replay" in state["injectors_present"],
              str(state["injectors_present"]))
        check("the generated probe file is listed",
              "PixToolSetPixelProbe.h" in state["pixel_probe"]["probe_files"])
        check("a restore path is offered",
              bool(state["pixel_probe"]["restore_with"]))

        print()
        print("[5] two injectors at once are both reported")
        (root / "edited_CreatePipelineState_100_CS.dxil").write_bytes(b"DXBC")
        state = exportstate.inspect(root)
        check("both injectors listed",
              {"shader-edit-apply", "pixel-history-replay"}
              <= set(state["injectors_present"]),
              str(state["injectors_present"]))

        print()
        print("[6] a leftover .orig alone does not make an export unclean")
        # Rebuild from scratch: the previous steps deliberately left injections
        # behind, and reusing them here would test the wrong thing.
        shutil.rmtree(root)
        make_fake_export(root)
        (root / "Helpers.h").write_text("// live\n", encoding="utf-8")
        (root / "Helpers.h.orig").write_text("// backup\n", encoding="utf-8")
        state = exportstate.inspect(root)
        check("clean is True despite the backup", state["clean"] is True,
              str(state["injectors_present"]))
        check("but the backup is still reported for visibility",
              "Helpers.h.orig" in state["backups_present"])

    print()
    print("=" * 74)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)})")
        for line in FAILURES:
            print("  - " + line)
        return 1
    print("PASSED: cleanliness is reported per injector, not as one misleading flag.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
