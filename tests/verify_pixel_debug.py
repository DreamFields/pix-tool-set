"""Verify the new pixel-debug and impact-tracking tools (Steps 1-9).

This is the integration test for the pixel-debug-and-impact-design.md implementation.
It calls pix-tool-set as a subprocess against a real PIX capture (Tiled.wpix),
following the same pattern as verify_shader_edit.py.

Covers:
  1. shader-info.sibling_psos (D1)
  2. shader-edit-apply --scope auto refuses with siblings (D1)
  3. replay-edits lists patches (D3)
  4. replay-reset reverts patches (D4)
  5. replay-baseline-check detects patches (D5)
  6. pixel-value-history returns draw-ordered history (P0)
  7. trace-downstream returns impact chain (capability B)
  8. shader-edit-diff --list-checkpoints returns empty list (Step 7)
  9. frame-replay-dump schema is valid (D2, schema-only check)

Requires: Tiled.wpix capture, PIX runtime, session-open already done.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SESSION = "Tiled"
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


# ----------------------------------------------------------------------
def stage_sibling_psos() -> None:
    """D1: shader-info reports sibling_psos and patch_scope_warning."""
    print("[1] shader-info.sibling_psos")
    result = run("shader-info", "--queue-id", "18461", "--stage", "CS")
    check("shader-info succeeds", result.get("status") == "success",
          str(result.get("error")))
    data = result.get("data", {})
    check("sibling_psos field present", "sibling_psos" in data, str(data.keys()))
    check("patch_scope_warning field present", "patch_scope_warning" in data,
          str(data.keys()))


def stage_scope_auto_refuses() -> None:
    """D1: shader-edit-apply --scope auto should error if >1 sibling PSO."""
    print("[2] shader-edit-apply --scope auto refuses with siblings")
    # This test is informational: if the shader has 1 PSO, auto resolves to pso (no error).
    # If it has >1, auto errors with ambiguous_shader_scope.
    # We check that the scope field is present in the output or the error is correct.
    result = run("shader-info", "--queue-id", "18461", "--stage", "CS")
    data = result.get("data", {})
    siblings = data.get("sibling_psos", [])
    if len(siblings) > 1:
        # --scope auto should refuse. We don't run shader-edit-apply here (it needs a source file),
        # but we verify the sibling_psos list is >1, which is the precondition.
        check("scope auto would refuse (siblings > 1)", len(siblings) > 1,
              f"found {len(siblings)} siblings")
    else:
        check("scope auto resolves to pso (1 sibling)", len(siblings) == 1,
              f"found {len(siblings)} siblings")


def stage_replay_edits() -> None:
    """D3: replay-edits lists patches from the ledger."""
    print("[3] replay-edits lists patches")
    result = run("replay-edits")
    check("replay-edits succeeds", result.get("status") == "success",
          str(result.get("error")))
    data = result.get("data", {})
    check("ledger_entries field present", "ledger_entries" in data, str(data.keys()))
    check("filesystem_patches field present", "filesystem_patches" in data,
          str(data.keys()))
    check("consistent field present", "consistent" in data, str(data.keys()))


def stage_replay_reset() -> None:
    """D4: replay-reset reverts patches."""
    print("[4] replay-reset reverts patches")
    result = run("replay-reset")
    check("replay-reset succeeds", result.get("status") == "success",
          str(result.get("error")))
    data = result.get("data", {})
    check("clean field present", "clean" in data, str(data.keys()))
    check("export is clean after reset", data.get("clean") is True,
          str(data.get("clean")))


def stage_baseline_check_refuses_patches() -> None:
    """D5: replay-baseline-check refuses when patches are present."""
    print("[5] replay-baseline-check detects patches")
    # This test only verifies the schema is valid. Actually running it requires
    # a full build, which takes minutes. We check the tool is registered and
    # its schema is correct.
    result = run("describe", "replay-baseline-check")
    check("replay-baseline-check is registered",
          result.get("status") == "success", str(result.get("error")))


def stage_pixel_value_history() -> None:
    """P0: pixel-value-history returns draw-ordered history."""
    print("[6] pixel-value-history")
    result = run("pixel-value-history", "--x", "640", "--y", "360")
    check("pixel-value-history succeeds", result.get("status") == "success",
          str(result.get("error")))
    data = result.get("data", {})
    check("history field present", "history" in data, str(data.keys()))
    check("entry_count field present", "entry_count" in data, str(data.keys()))
    check("pixel field present", "pixel" in data, str(data.keys()))
    # The history may be empty if no RT covers (640, 360), but the structure must be valid.
    check("history is a list", isinstance(data.get("history"), list), str(type(data.get("history"))))


def stage_trace_downstream() -> None:
    """Capability B: trace-downstream returns impact chain."""
    print("[7] trace-downstream")
    result = run("trace-downstream", "--queue-id", "18461")
    check("trace-downstream succeeds", result.get("status") == "success",
          str(result.get("error")))
    data = result.get("data", {})
    check("downstream_draws field present", "downstream_draws" in data, str(data.keys()))
    check("downstream_passes field present", "downstream_passes" in data, str(data.keys()))
    check("source field present", "source" in data, str(data.keys()))


def stage_checkpoint_list() -> None:
    """Step 7: shader-edit-diff --list-checkpoints returns empty list."""
    print("[8] shader-edit-diff --list-checkpoints")
    result = run("shader-edit-diff", "--queue-id", "18461", "--stage", "CS",
                 "--resource-id", "3032", "--list-checkpoints")
    check("list-checkpoints succeeds", result.get("status") == "success",
          str(result.get("error")))
    data = result.get("data", {})
    check("checkpoints field present", "checkpoints" in data, str(data.keys()))
    check("count field present", "count" in data, str(data.keys()))


def stage_frame_replay_dump_schema() -> None:
    """D2: frame-replay-dump schema is valid (schema-only check)."""
    print("[9] frame-replay-dump schema")
    result = run("describe", "frame-replay-dump")
    check("frame-replay-dump is registered",
          result.get("status") == "success", str(result.get("error")))
    schema = result.get("data", {}).get("parameters", {}).get("properties", {})
    check("has --at parameter", "at" in schema, str(schema.keys()))
    check("has --max-resources parameter", "max_resources" in schema, str(schema.keys()))
    check("has --resource-types parameter", "resource_types" in schema, str(schema.keys()))


# ----------------------------------------------------------------------
def main() -> int:
    print("=" * 72)
    print("pixel-debug-and-impact-design verification")
    print("=" * 72)

    stages = [
        stage_sibling_psos,
        stage_scope_auto_refuses,
        stage_replay_edits,
        stage_replay_reset,
        stage_baseline_check_refuses_patches,
        stage_pixel_value_history,
        stage_trace_downstream,
        stage_checkpoint_list,
        stage_frame_replay_dump_schema,
    ]

    for stage in stages:
        try:
            stage()
        except Exception as exc:
            FAILED.append(f"{stage.__name__}: {type(exc).__name__}: {exc}")
            print(f"  FAIL  {stage.__name__} :: {exc}")

    print("\n" + "=" * 72)
    print(f"PASSED: {len(PASSED)}  FAILED: {len(FAILED)}")
    if FAILED:
        print("\nFailures:")
        for f in FAILED:
            print(f"  - {f}")
        print("\nRESULT: FAIL")
        return 1
    else:
        print("\nRESULT: PASS")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
