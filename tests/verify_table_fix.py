"""Verify the descriptor-table expansion fix against report section 2.3.

Before the fix, root[0] of the TileClassification dispatches expanded to 64
views that were all rid=896 (PIX filler). After the fix, expansion stops at the
next bound table base and rejects slots whose view kind contradicts the declared
range, so what remains should be a small, plausible set.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set.context import ToolContext, clear_capture_cache  # noqa: E402

SESSION = sys.argv[1] if len(sys.argv) > 1 else "tiled"
DRAWS = [int(x) for x in sys.argv[2:]] or [2476, 2606, 2688]


def main() -> int:
    clear_capture_cache()
    capture = ToolContext.from_cwd().capture({"session": SESSION})

    heap_bases = 0
    parser_bases = getattr(capture, "_table_bases", None)
    print("=" * 78)
    print("descriptor-table expansion after fix")
    print("=" * 78)

    for index in DRAWS:
        draw = capture.draw_call(index)
        if draw is None:
            print(f"\ndraw #{index}: not found")
            continue
        print(f"\ndraw #{draw.index}  {draw.pass_name}")
        print(f"  pso={draw.pso_id} rootsig={draw.root_signature_id}")
        shader = draw.shader("CS")
        if shader is not None:
            declared = shader.resource_bindings
            srv = [d for d in declared if d["id"].startswith("T")]
            uav = [d for d in declared if d["id"].startswith("U")]
            print(f"  shader declares: {len(srv)} SRV, {len(uav)} UAV")
        for binding in draw.bindings:
            rids = sorted(
                {v.resource_id for v in binding.resolved_views if v.resource_id is not None}
            )
            kinds = sorted({v.kind.value for v in binding.resolved_views})
            print(
                f"  root[{binding.root_index}] {binding.kind.value:16s} "
                f"base={binding.heap_index} views={len(binding.resolved_views)} "
                f"conf={binding.table_confidence or '-'}"
            )
            if binding.resolved_views:
                print(f"      kinds={kinds} rids={rids[:8]}")
            elif binding.resource_id is not None:
                print(f"      root descriptor -> rid={binding.resource_id}")

    # frame-wide effect
    print("\n" + "=" * 78)
    print("frame-wide table expansion confidence")
    print("=" * 78)
    counts: dict[str, int] = {}
    total_views = 0
    for draw in capture.draw_calls:
        for binding in draw.bindings:
            if binding.heap_index is None:
                continue
            key = binding.table_confidence or "-"
            counts[key] = counts.get(key, 0) + 1
            total_views += len(binding.resolved_views)
    print(f"  descriptor tables bound : {sum(counts.values()):,}")
    print(f"  total views expanded    : {total_views:,}")
    for key, value in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"    {key:8s} {value:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
