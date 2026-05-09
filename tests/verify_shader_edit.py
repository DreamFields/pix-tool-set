"""Regression: the scriptable equivalent of PIX's Debug-panel "Apply".

Covers the whole chain on a known pass (Queue ID 18461, RayTracingBuildLightGridCS):

  1. shader-edit-begin recovers real HLSL plus the PDB's compile arguments
  2. an unmodified round-trip reproduces the captured shader's binding signature
  3. a real source edit still compiles and stays slot-compatible
  4. a syntax error surfaces DXC's own diagnostics rather than a generic failure
  5. a binding change is refused instead of silently patched
  6. --patch rewrites the exported project and leaves a restorable backup

The binding signature is the load-bearing check: the recorded command lists bind
resources by slot, so a replacement that moves a register reads the wrong resource.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

QUEUE_ID = 18461
SESSION = "Tiled"
PDB_DIR = r"F:\JL_TMR\UnrealEngine\Games\JyGame\Saved\ShaderSymbols\PCD3D_SM6"
WORK = Path(__file__).resolve().parent.parent / "build" / "shader-edit-regression"

PASSED: list[str] = []
FAILED: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    if condition:
        PASSED.append(label)
        print(f"  PASS  {label}")
    else:
        FAILED.append(f"{label}: {detail}")
        print(f"  FAIL  {label} :: {detail}")
    return condition


def run(*args: str) -> dict:
    proc = subprocess.run(
        ["pix-tool-set", *args, "--session", SESSION],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=True,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {
            "status": "error",
            "error": {"message": (proc.stdout or proc.stderr)[-800:]},
        }


def signature(payload: dict) -> list[tuple]:
    return [
        (row["id"], row["name"], row["type"], row["bind"])
        for row in payload
    ]


# ----------------------------------------------------------------------
def stage_begin() -> dict:
    print("[1] shader-edit-begin recovers source and arguments")
    result = run(
        "shader-edit-begin",
        "--queue-id",
        str(QUEUE_ID),
        "--output",
        str(WORK),
        "--pdb-dirs",
        PDB_DIR,
    )
    check("begin succeeds", result.get("status") == "success", str(result.get("error")))
    data = result.get("data", {})
    check("entry point recovered", data.get("entry_point") == "RayTracingBuildLightGridCS",
          str(data.get("entry_point")))
    check("compile args recovered", "-T" in (data.get("compile_args") or []),
          str(data.get("compile_args")))
    check("target profile is cs_6_6", "cs_6_6" in (data.get("compile_args") or []))
    files = data.get("files", {})
    hlsl = Path(files.get("editable_hlsl", ""))
    check("editable hlsl written", hlsl.exists(), str(hlsl))
    check("args sidecar written", Path(files.get("compile_args", "")).exists())
    check("pristine copy written", Path(files.get("pristine_copy", "")).exists())
    check("5 original bindings", len(data.get("original_bindings") or []) == 5,
          str(len(data.get("original_bindings") or [])))
    return data


def stage_roundtrip(data: dict) -> None:
    print("[2] unmodified round-trip preserves the binding signature")
    hlsl = data["files"]["editable_hlsl"]
    result = run(
        "shader-edit-apply", "--queue-id", str(QUEUE_ID), "--source", hlsl,
        "--output", str(WORK),
    )
    check("round-trip compiles", result.get("status") == "success", str(result.get("error")))
    payload = result.get("data", {})
    checkinfo = payload.get("binding_check", {})
    check("bindings identical", checkinfo.get("identical") is True)
    check("entry and threads match", checkinfo.get("entry_and_threads_match") is True)
    check("container is signed", (payload.get("new_container") or {}).get("signed") is True)
    check(
        "compiled through dxcompiler or dxc",
        "dxc" in ((payload.get("compile") or {}).get("method") or "").lower(),
        str((payload.get("compile") or {}).get("method")),
    )
    check("nothing patched without --patch", "patch" not in payload)


def stage_real_edit(data: dict) -> Path:
    print("[3] a real source edit compiles and stays slot-compatible")
    pristine = Path(data["files"]["pristine_copy"]).read_text(encoding="utf-8")
    edited = WORK / "edited_behaviour.hlsl"
    marker = "RWLightGrid[DispatchThreadId] = 0;"
    if marker not in pristine:
        check("edit anchor present", False, "the axis-reject write was not found")
        return edited
    edited.write_text(
        pristine.replace(marker, "RWLightGrid[DispatchThreadId] = 7;"), encoding="utf-8"
    )
    shutil.copy2(Path(data["files"]["compile_args"]), WORK / "edited_behaviour.args.txt")

    result = run(
        "shader-edit-apply", "--queue-id", str(QUEUE_ID), "--source", str(edited),
        "--output", str(WORK),
    )
    check("edited shader compiles", result.get("status") == "success", str(result.get("error")))
    payload = result.get("data", {})
    check("edit stays slot-compatible",
          (payload.get("binding_check") or {}).get("identical") is True)
    check("args read from sidecar",
          "args.txt" in (payload.get("compile_args_origin") or ""),
          str(payload.get("compile_args_origin")))
    new_hash = (payload.get("new_container") or {}).get("shader_hash")
    check("edit changed the shader hash",
          bool(new_hash) and new_hash != "3e92071c09a522dfa4e259e557334efc",
          str(new_hash))
    return edited


def stage_syntax_error(data: dict) -> None:
    print("[4] a syntax error surfaces DXC's own diagnostics")
    pristine = Path(data["files"]["pristine_copy"]).read_text(encoding="utf-8")
    broken = WORK / "syntax_error.hlsl"
    anchor = "uint3 VoxelId = 0, VoxelRes = 1;"
    if anchor not in pristine:
        check("syntax anchor present", False, anchor)
        return
    broken.write_text(pristine.replace(anchor, "uint3 VoxelId = 0, VoxelRes = ;"),
                      encoding="utf-8")
    shutil.copy2(Path(data["files"]["compile_args"]), WORK / "syntax_error.args.txt")

    result = run(
        "shader-edit-apply", "--queue-id", str(QUEUE_ID), "--source", str(broken),
        "--output", str(WORK),
    )
    check("compile failure is reported as an error", result.get("status") == "error",
          str(result.get("status")))
    error = result.get("error") or {}
    check("error code is specific", error.get("code") == "shader_compile_failed",
          str(error.get("code")))
    output = ((error.get("details") or {}).get("compiler_output") or "")
    check("dxc text is passed through", "error:" in output, output[:200])
    check("dxc reports a line number", bool(re.search(r":\d+:\d+:", output)), output[:200])
    check("no NUL leaks into the report", "\x00" not in output)


def stage_binding_guard(data: dict) -> None:
    print("[5] a binding change is refused, not patched")
    pristine = Path(data["files"]["pristine_copy"]).read_text(encoding="utf-8")
    variant = WORK / "binding_change.hlsl"
    anchor = "RWTexture2DArray<uint> RWLightGrid;"
    if anchor not in pristine:
        check("uav declaration found", False, anchor)
        return
    # Declaring an extra UAV ahead of RWLightGrid shifts every auto-assigned slot.
    shifted = pristine.replace(
        anchor, "RWBuffer<uint> PixToolSetProbe;\n" + anchor
    ).replace(
        "RWLightGrid[DispatchThreadId] = 0;",
        "RWLightGrid[DispatchThreadId] = 0; PixToolSetProbe[0] = 1;",
    )
    variant.write_text(shifted, encoding="utf-8")
    shutil.copy2(Path(data["files"]["compile_args"]), WORK / "binding_change.args.txt")

    result = run(
        "shader-edit-apply", "--queue-id", str(QUEUE_ID), "--source", str(variant),
        "--output", str(WORK),
    )
    check("status is partial, not success", result.get("status") == "partial",
          str(result.get("status")))
    payload = result.get("data", {})
    info = payload.get("binding_check") or {}
    check("mismatch detected", info.get("identical") is False)
    check("not patched", "patch" not in payload)
    replacement = signature(info.get("replacement") or [])
    moved = [row for row in replacement if row[1] == "RWLightGrid"]
    check("RWLightGrid was displaced from u0",
          bool(moved) and moved[0][3] != "u0",
          str(moved))
    reasons = " ".join(d.get("reason", "") for d in result.get("diagnostics") or [])
    check("reason explains the refusal", "binding" in reasons.lower(), reasons)


def stage_patch(edited: Path) -> None:
    print("[6] --patch rewrites the export and leaves a restorable backup")
    result = run(
        "shader-edit-apply", "--queue-id", str(QUEUE_ID), "--source", str(edited),
        "--output", str(WORK), "--patch",
    )
    check("patch succeeds", result.get("status") == "success", str(result.get("error")))
    patch = (result.get("data") or {}).get("patch") or {}
    check("patched the right PSO function",
          patch.get("function") == "CreatePipelineState_3241", str(patch.get("function")))
    check("replaced the CS assignment",
          "pssDesc.CS" in (patch.get("replaced") or ""), str(patch.get("replaced")))

    cpp = Path(patch.get("bytecode_file", "")).parent
    source_file = cpp / "CreatePSOs.cpp"
    backup = Path(patch.get("backup", ""))
    text = source_file.read_text(encoding="utf-8", errors="replace")
    check("bytecode payload written", Path(patch.get("bytecode_file", "")).exists())
    check("backup created", backup.exists(), str(backup))
    check("marker present in source", "pix-tool-set: CS replaced" in text)
    check("helper is available", "ReadFileBytes" in (cpp / "Helpers.h").read_text(
        encoding="utf-8", errors="replace"))

    # The sequential blob read must survive: resources.bin has no index, so skipping a
    # read would misalign every later blob. The recorded assignment must also survive,
    # because the toolkit parses this file to learn each PSO's shader sizes.
    body_start = text.find("void CreatePipelineState_3241()")
    body = text[body_start : text.find("\n}\n", body_start)]
    check("sequential blob read preserved", "g_resourceReader->Read(data, 12491)" in body)
    check("recorded assignment preserved",
          "reinterpret_cast<BYTE*>(&data[offset]), 16436" in body)
    check("override follows the assignment",
          body.find("editedBytes_CS") > body.find("&data[offset]), 16436"))

    # Applying twice must be refused rather than nesting patches.
    again = run(
        "shader-edit-apply", "--queue-id", str(QUEUE_ID), "--source", str(edited),
        "--output", str(WORK), "--patch",
    )
    check("double patch refused", again.get("status") == "error",
          str(again.get("status")))
    check("double patch names the cause",
          (again.get("error") or {}).get("code") == "already_patched",
          str((again.get("error") or {}).get("code")))

    restore(cpp)
    check("export restored", "pix-tool-set: CS replaced" not in
          source_file.read_text(encoding="utf-8", errors="replace"))


def restore(cpp: Path) -> None:
    """Put the exported project back, so the regression leaves no trace."""
    for name in ("CreatePSOs.cpp", "Helpers.h"):
        backup = cpp / f"{name}.orig" if name.endswith(".cpp") else cpp / "Helpers.h.orig"
        backup = cpp / (name + ".orig")
        if not backup.exists():
            # shader-edit-apply names the cpp backup CreatePSOs.cpp.orig
            backup = cpp / (Path(name).stem + Path(name).suffix + ".orig")
        if backup.exists():
            shutil.copy2(backup, cpp / name)
            backup.unlink()
    for payload in cpp.glob("edited_CreatePipelineState_*.dxil"):
        payload.unlink()


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    print(f"work dir: {WORK}\n")
    data = stage_begin()
    if FAILED:
        print("\nbegin stage failed; later stages depend on it.")
        return 1
    print()
    stage_roundtrip(data)
    print()
    edited = stage_real_edit(data)
    print()
    stage_syntax_error(data)
    print()
    stage_binding_guard(data)
    print()
    stage_patch(edited)

    print()
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    for entry in FAILED:
        print("  -", entry)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
