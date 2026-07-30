"""End-to-end verification against a real capture export.

Registers a session that points at an existing pixtool export, then exercises
every registered tool and reports status per tool. Use this after changing the
engine or the tool layer.

    python tests/verify_live.py                       # default export
    python tests/verify_live.py <export_dir> <capture.wpix>
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set.context import ToolContext, clear_capture_cache  # noqa: E402
from pix_tool_set.registry import get_registry  # noqa: E402
from pix_tool_set.session import SessionRecord  # noqa: E402
from pix_tool_set.tools import load_builtin_tools  # noqa: E402

DEFAULT_EXPORT = Path(r"C:\Users\vinmeng\With\20260729\tdv7\export\cpp")
DEFAULT_CSV = Path(r"C:\Users\vinmeng\With\20260729\tdv7\export\event_list.csv")
DEFAULT_CAPTURE = Path(r"C:\Users\vinmeng\Desktop\ManyLights\debug\NoTiled.wpix")

SESSION = "verify"

# tools that need pixtool to replay the capture (slow / GPU dependent)
REPLAY_TOOLS = {
    "export-texture",
    "export-draw-textures",
    "read-texture-pixels",
    "texture-pixel-stats",
    "pick-pixel",
    "sample-pixel-region",
    "save-render-target",
}

SKIP = {"session-open", "session-close"}


def build_args(name: str, capture) -> dict:
    """Reasonable arguments for each tool so the call is meaningful."""
    base = {"session": SESSION}
    draw = next((d for d in capture.draw_calls if d.render_target_resource_ids), None)
    dispatch = next((d for d in capture.draw_calls if d.kind.value == "dispatch"), None)
    texture = next(
        (r for r in capture.resources.values() if r.is_texture and r.is_render_target), None
    )
    buffer_res = next((r for r in capture.resources.values() if r.is_buffer), None)
    shader = next((s for s in capture.shaders if s.stage.value == "CS"), None)
    pso_id = draw.pso_id if draw else None
    chosen_pass_name = capture.passes[0]["name"] if capture.passes else ""

    specific: dict[str, dict] = {
        "action-info": {"global_id": draw.global_id} if draw and draw.global_id else {},
        "locate-event": {"draw_index": draw.index} if draw else {},
        "pass-info": {"pass_index": 0},
        "analyze-pass": {"pass_index": 0},
        "texture-info": {"resource_id": texture.api_id} if texture else {},
        "resource-usage": {"resource_id": texture.api_id} if texture else {},
        "read-buffer": {"resource_id": buffer_res.api_id} if buffer_res else {},
        "export-mesh": {"draw_index": draw.index} if draw else {},
        "draw-state": {"draw_index": draw.index} if draw else {},
        "vertex-input": {"draw_index": draw.index} if draw else {},
        "post-vs-data": {"draw_index": draw.index} if draw else {},
        "pipeline-state": {"pso_id": pso_id} if pso_id is not None else {},
        "shader-info": {"pso_id": shader.pso_id, "stage": "CS"} if shader else {},
        "shader-reflection": {"pso_id": shader.pso_id, "stage": "CS"} if shader else {},
        "disassemble-shader": (
            {"pso_id": shader.pso_id, "stage": "CS", "max_lines": 5} if shader else {}
        ),
        "shader-bindings": {"draw_index": draw.index} if draw else {},
        "constant-buffer": {"draw_index": draw.index} if draw else {},
        "diff-draw-calls": {"left_draw": draw.index, "right_draw": draw.index + 1}
        if draw and draw.index + 1 < len(capture.draw_calls)
        else {},
        "search-actions": {"query": "Draw"},
        "find-pass": {"name": chosen_pass_name or "Pass"},
        "pass-bindings": {"pass_index": 0, "max_draws": 2},
        "pass-shader-source": {"pass_index": 0, "max_lines": 20},
        "session-set-pdb-dirs": {"clear": True},
        "pass-values": {"pass_index": 0, "max_bytes": 64, "max_views": 4},
        "pixel-history": (
            {"resource_id": texture.api_id, "x": 10, "y": 10} if texture else {"x": 10, "y": 10}
        ),
        "debug-pixel-shader": {"draw_index": draw.index} if draw else {},
        "export-report": {"max_draws": 5, "output": "verify-out/report.json"},
        "export-texture": (
            {"resource_id": texture.api_id, "output": "verify-out/tex.png"} if texture else {}
        ),
        "export-draw-textures": (
            {"draw_index": draw.index, "output_dir": "verify-out/draw", "max_files": 2}
            if draw
            else {}
        ),
        "read-texture-pixels": (
            {
                "resource_id": texture.api_id,
                "x": 0,
                "y": 0,
                "width": 2,
                "height": 2,
                "output": "verify-out/pixels.png",
            }
            if texture
            else {}
        ),
        "texture-pixel-stats": (
            {
                "resource_id": texture.api_id,
                "x": 0,
                "y": 0,
                "width": 16,
                "height": 16,
                "output": "verify-out/stats.png",
            }
            if texture
            else {}
        ),
        "pick-pixel": (
            {"resource_id": texture.api_id, "x": 4, "y": 4, "output": "verify-out/pick.png"}
            if texture
            else {"x": 4, "y": 4}
        ),
        "sample-pixel-region": (
            {
                "resource_id": texture.api_id,
                "x": 0,
                "y": 0,
                "width": 16,
                "height": 16,
                "output": "verify-out/region.png",
            }
            if texture
            else {"x": 0, "y": 0}
        ),
        "save-render-target": (
            {"draw_index": draw.index, "output": "verify-out/rt.png"} if draw else {}
        ),
        "list-actions": {"limit": 3},
        "list-draw-calls": {"limit": 3},
        "list-passes": {"limit": 3},
        "list-textures": {"limit": 3},
        "list-resources": {"limit": 3},
        "list-buffers": {"limit": 3},
        "list-shaders": {"limit": 3},
        "list-pipeline-states": {"limit": 3},
        "find-draw-calls": {"limit": 3},
        "pass-cost": {"limit": 3},
        "analyze-overdraw": {"limit": 3},
        "analyze-bandwidth": {"limit": 3},
        "analyze-state-changes": {"limit": 3},
        "diagnose-negative-values": {"limit": 5},
        "diagnose-precision": {"limit": 5},
        "diagnose-reflection-mismatch": {"limit": 5, "max_draws": 20},
        "diagnose-mobile-risks": {"limit": 5},
        "session-list": {},
    }
    extra = specific.get(name, {})
    if name == "session-list":
        return {}
    return {**base, **extra}


def main() -> int:
    flags = [item for item in sys.argv[1:] if item.startswith("--")]
    positional = [item for item in sys.argv[1:] if not item.startswith("--")]
    include_replay = "--with-replay" in flags

    export_dir = Path(positional[0]) if positional else DEFAULT_EXPORT
    capture_path = Path(positional[1]) if len(positional) > 1 else DEFAULT_CAPTURE
    csv_path = DEFAULT_CSV if not positional else export_dir.parent / "event_list.csv"

    if not export_dir.exists():
        print(f"export dir not found: {export_dir}")
        return 2

    load_builtin_tools()
    registry = get_registry()
    context = ToolContext.from_cwd()
    context.store.put(
        SessionRecord(
            name=SESSION,
            capture_path=str(capture_path) if capture_path.exists() else "",
            export_dir=str(export_dir),
            event_csv=str(csv_path) if csv_path.exists() else None,
        )
    )
    clear_capture_cache()

    print("=" * 78)
    print(f"export : {export_dir}")
    print(f"capture: {capture_path} (exists={capture_path.exists()})")
    print(f"events : {csv_path} (exists={csv_path.exists()})")
    print("=" * 78)

    capture = ToolContext.from_cwd().capture({"session": SESSION})
    print(
        f"parsed: draws={len(capture.draw_calls)} events={len(capture.events)} "
        f"resources={len(capture.resources)} passes={len(capture.passes)} "
        f"shaders={len(capture.shaders)}"
    )
    print()

    rows: list[tuple[str, str, float, str]] = []

    for definition in registry.list_tools():
        name = definition.name
        if name in SKIP:
            rows.append((name, "skipped", 0.0, "mutates sessions"))
            continue
        if name in REPLAY_TOOLS and not include_replay:
            rows.append((name, "skipped", 0.0, "needs GPU replay (--with-replay)"))
            continue

        args = build_args(name, capture)
        started = time.time()
        try:
            local_context = ToolContext.from_cwd()
            cleaned = definition.validate_args(args)
            result = definition.handler(cleaned, local_context)
            elapsed = time.time() - started
            note = ""
            if result.diagnostics:
                note = result.diagnostics[0].get("message", "")[:60]
            rows.append((name, result.status, elapsed, note))
            json.dumps(result.to_dict())  # ensure serialisable
        except Exception as exc:  # noqa: BLE001
            elapsed = time.time() - started
            rows.append((name, "EXCEPTION", elapsed, f"{type(exc).__name__}: {exc}"))
            traceback.print_exc(limit=4)

    print(f"{'tool':32s} {'status':11s} {'sec':>6s}  note")
    print("-" * 78)
    for name, status, elapsed, note in rows:
        print(f"{name:32s} {status:11s} {elapsed:6.2f}  {note}")

    failures = [row for row in rows if row[1] == "EXCEPTION"]
    partial = [row for row in rows if row[1] == "partial"]
    ok = [row for row in rows if row[1] == "success"]
    skipped = [row for row in rows if row[1] == "skipped"]

    print("-" * 78)
    print(
        f"success={len(ok)} partial={len(partial)} skipped={len(skipped)} "
        f"exceptions={len(failures)}"
    )
    if failures:
        print("\nEXCEPTIONS:")
        for name, _status, _elapsed, note in failures:
            print(f"  {name}: {note}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
