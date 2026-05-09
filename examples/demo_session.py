"""Demo: a realistic analysis session driven purely through the tool API.

Mirrors what an AI client would do end to end, and prints the findings.

    python examples/demo_session.py                 # uses the 'verify' session
    python examples/demo_session.py <capture.wpix>  # opens a capture first
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set import call_tool  # noqa: E402

SESSION = "verify"


def run(tool: str, **args):
    payload = call_tool(tool, args)
    if payload["status"] == "error":
        error = payload["error"]
        raise SystemExit(f"{tool} failed: {error['code']}: {error['message']}")
    for entry in payload.get("diagnostics", []):
        if entry.get("level") == "warning":
            print(f"    [degraded] {entry['message']}")
    return payload["data"]


def heading(text: str) -> None:
    print("\n" + "=" * 74)
    print(text)
    print("=" * 74)


def main() -> int:
    if len(sys.argv) > 1:
        heading("0. open the capture")
        info = run("session-open", capture=sys.argv[1], session=SESSION)
        print(f"  session   : {info['session']}")
        print(f"  export    : {info['export_dir']}")
        print(f"  counts    : {info['counts']}")

    heading("1. frame overview")
    stats = run("frame-stats", session=SESSION)
    draws = stats["draw_calls"]
    print(f"  events        : {stats['events']['total']}")
    print(
        f"  draw calls    : {draws['total']} "
        f"(draw={draws['draw']} dispatch={draws['dispatch']} "
        f"indirect={draws['execute_indirect']})"
    )
    print(f"  triangles     : {stats['geometry']['total_triangles']:,}")
    print(f"  compute thread: {stats['compute']['total_threads']:,}")
    print(f"  passes        : {stats['passes']['total']}")
    print(
        f"  textures      : {stats['resources']['textures']} "
        f"({stats['resources']['estimated_texture_bytes'] / 1048576:.0f} MiB est.)"
    )
    print(f"  shaders       : {stats['shaders']['by_stage']}")

    heading("2. most expensive passes (relative cost model)")
    for entry in run("pass-cost", session=SESSION, limit=6)["passes"]:
        print(
            f"  {entry['cost_share_percent']:5.1f}%  {entry['name'][:46]:46s} "
            f"tris={entry['triangle_count']:>8,} threads={entry['thread_count']:>10,}"
        )

    heading("3. heaviest dispatches")
    data = run("draw-call-stats", session=SESSION, top=5)
    for entry in data["heaviest_dispatches"]:
        print(
            f"  #{entry['draw_index']:<6d} threads={entry.get('thread_count', 0):>11,}  "
            f"{entry['pass_name'][:48]}"
        )

    heading("4. biggest render targets and who writes them")
    for texture in run("list-textures", session=SESSION, render_target=True, limit=5)["textures"]:
        print(
            f"  res#{texture['resource_id']:<6d} {texture['description'][:52]:52s} "
            f"writes={texture['usage']['write_draws']}"
        )

    heading("5. overdraw suspects")
    overdraw = run("analyze-overdraw", session=SESSION, limit=4)
    for entry in overdraw["targets"]:
        print(
            f"  res#{entry['resource_id']:<6d} overdraw={entry['overdraw_ratio']:>7.2f}x "
            f"draws={entry['draw_count']:<5d} conf={entry['confidence']:<6s} "
            f"{entry['description'][:36]}"
        )
    for observation in overdraw.get("observations", []):
        print(f"    ! [{observation['confidence']}] {observation['message'][:90]}")

    heading("6. bandwidth by pass")
    for entry in run("analyze-bandwidth", session=SESSION, limit=5)["entries"]:
        print(
            f"  {entry['share_percent']:5.1f}%  {entry['pass_name'][:44]:44s} "
            f"{entry['total_mib']:>9.1f} MiB"
        )

    heading("7. state change churn")
    churn = run("analyze-state-changes", session=SESSION, limit=4)
    totals = churn["frame_totals"]
    print(
        f"  PSO switches={totals['pipeline_state_switches']} of {totals['events']} events, "
        f"root sig switches={totals['root_signature_switches']}"
    )
    for finding in churn["findings"]:
        print(f"    [{finding['severity']}] {finding['message']}")

    heading("8. inspect one draw in full")
    draws = run("find-draw-calls", session=SESSION, kind="draw", limit=1, min_triangles=1000)
    if draws["draw_calls"]:
        index = draws["draw_calls"][0]["draw_index"]
        state = run("draw-state", session=SESSION, draw_index=index)
        draw = state["draw_call"]
        print(f"  draw #{draw['draw_index']} ({draw['api']}) in '{draw['pass_name'][:40]}'")
        print(f"    triangles : {draw.get('triangle_count')}")
        print(f"    PSO / RS  : {draw['pso_id']} / {draw['root_signature_id']}")
        for shader in draw.get("shaders", []):
            print(f"    {shader['stage']:3s} {shader['byte_size']:>7d}B {shader['debug_name']}")
        for target in draw.get("render_targets", []):
            print(f"    RT        : {target['description']}")
        if draw.get("depth_stencil"):
            print(f"    DEPTH     : {draw['depth_stencil']['description']}")
        print(f"    bindings  : {draw['counts']}")

    heading("9. mobile readiness")
    risks = run("diagnose-mobile-risks", session=SESSION, limit=6)
    print(f"  severity: {risks['severity_counts']}")
    for finding in risks["findings"]:
        print(f"    [{finding['severity']:7s}] {finding['topic']}: {finding['message'][:78]}")
        print(f"              -> {finding['recommendation'][:78]}")

    heading("10. precision diagnostics")
    precision = run("diagnose-precision", session=SESSION, limit=4)
    print(f"  severity: {precision['severity_counts']}")
    for finding in precision["findings"]:
        print(f"    [{finding['severity']:7s}] {finding['message'][:80]}")

    print("\ndone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
