"""Check that PDB directories stored on a session are actually used.

`session-set-pdb-dirs` records the engine's ShaderSymbols directories on the session so
later calls need not repeat the path. That only works if the lookup resolves the *active*
session when no --session is given: `SessionStore.get(None)` matches no name and returns
None, so the stored directories were silently ignored and every call had to pass
--pdb-dirs by hand.

Runs each tool through its registered handler rather than the CLI, because PowerShell
pipelines mangle the JSON on stdout.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set.context import ToolContext
from pix_tool_set.errors import PixToolError
from pix_tool_set.registry import get_registry
from pix_tool_set.tools import load_builtin_tools

EXPECTED_PDB = "f3dddac6e04484977a815ca5bd84f78a.pdb"
QUEUE_ID = 18704


def call(name: str, args: dict) -> dict:
    """Invoke a tool the way `cli.py` does, including its error envelope.

    The CLI turns a PixToolError into the error payload rather than letting it escape, so
    a test that calls the handler directly has to do the same or it cannot observe how a
    bad request is reported.
    """
    definition = get_registry().get(name)
    context = ToolContext.from_cwd(None)
    try:
        result = definition.handler(definition.validate_args(args), context)
    except PixToolError as exc:
        return {"status": "error", "data": {}, "error": {"code": exc.code, "message": str(exc)}}
    return result.to_dict()


def main() -> int:
    load_builtin_tools()
    failures: list[str] = []
    out_dir = Path(__file__).resolve().parent / "_verify_pdbfix_tmp"

    # The session must already carry the directories for this test to mean anything.
    sessions = call("session-list", {})
    active = next(
        (s for s in sessions["data"]["sessions"] if s.get("active")), None
    )
    if active is None:
        print("SKIP: no active session; run session-open first")
        return 0
    print(f"active session : {active['session']}")
    print(f"stored pdb dirs: {active['shader_pdb_dirs']}")
    if not active["shader_pdb_dirs"]:
        print("SKIP: active session has no stored pdb dirs to exercise")
        return 0

    # 1. pass-shader-source must reach the PDB tier without being told the path.
    src = call("pass-shader-source", {"queue_id": QUEUE_ID, "stage": "CS"})
    if src["status"] == "error":
        failures.append(f"pass-shader-source errored: {src['error']['message']}")
    else:
        stages = src["data"].get("stages") or []
        tier = stages[0].get("source_tier") if stages else None
        used = src["data"].get("pdb_dirs_used") or []
        print(f"pass-shader-source tier={tier} pdb_dirs_used={used}")
        if tier != "pdb-hlsl":
            failures.append(f"pass-shader-source tier={tier!r}, expected 'pdb-hlsl'")
        if not used:
            failures.append("pass-shader-source reported no pdb_dirs_used")

    # 2. shader-edit-begin used to refuse outright with invalid_argument.
    begin = call(
        "shader-edit-begin",
        {"queue_id": QUEUE_ID, "stage": "CS", "output": str(out_dir)},
    )
    if begin["status"] == "error":
        failures.append(f"shader-edit-begin errored: {begin['error']['message']}")
    else:
        pdb_path = begin["data"].get("pdb_path") or ""
        print(f"shader-edit-begin pdb_path={pdb_path}")
        if EXPECTED_PDB not in pdb_path:
            failures.append(f"shader-edit-begin pdb_path={pdb_path!r}")

    # 3. A bad session name must still degrade into a structured error, not a traceback.
    bad = call("pass-shader-source", {"session": "no-such-session", "queue_id": QUEUE_ID})
    code = bad.get("error", {}).get("code") if bad["status"] == "error" else None
    print(f"bad session -> status={bad['status']} code={code}")
    if bad["status"] != "error":
        failures.append("a missing session should be reported as an error")
    elif code == "unhandled_error":
        failures.append("a missing session leaked an unhandled error")

    if out_dir.exists():
        for child in out_dir.iterdir():
            child.unlink()
        out_dir.rmdir()

    if failures:
        print("\nFAIL")
        for item in failures:
            print("  -", item)
        return 1
    print("\nPASS: stored PDB directories are honoured without --pdb-dirs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
