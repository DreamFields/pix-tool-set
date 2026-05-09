"""Locate depth that contains geometry, then read levels from it."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set import call_tool  # noqa: E402
from pix_tool_set.context import clear_capture_cache  # noqa: E402

OUT = Path("G:/pix-tool-set/depth-out/find")


def main() -> int:
    clear_capture_cache()
    OUT.mkdir(parents=True, exist_ok=True)

    print("=" * 84)
    print("STEP 1  find an event whose depth holds rendered geometry")
    print("=" * 84)
    payload = call_tool(
        "find-depth-content",
        {
            "session": "Tiled",
            "queue_id": 17765,
            "max_probes": 4,
            "output": str(OUT),
        },
    )
    print(f"status: {payload['status']}")
    data = payload["data"]
    print(f"resource {data['resource_id']}  "
          f"{data['depth_events_total']} depth events, {data['events_probed']} probed")
    print(f"\n{'draw':>6s} {'gid':>6s}  {'character':<19s} {'levels':>7s} {'edges':>6s}  pass")
    print("-" * 84)
    for probe in data["probes"]:
        if not probe.get("decoded"):
            print(f"{probe['draw_index']:>6d} {probe.get('global_id') or 0:>6d}  "
                  f"{'not decoded':<19s} {'-':>7s} {'-':>6s}  "
                  f"{probe['pass_name'][:28]}")
            continue
        print(f"{probe['draw_index']:>6d} {probe.get('global_id') or 0:>6d}  "
              f"{probe.get('content_character', '?'):<19s} "
              f"{probe.get('distinct_levels', 0):>7d} "
              f"{probe.get('discontinuities_sampled', 0):>6d}  "
              f"{probe['pass_name'][:28]}")

    best = data.get("best_event")
    if best:
        print(f"\nbest event: draw #{best['draw_index']} ({best['pass_name'][:40]})")
        print(f"   levels={best['distinct_levels']} edges={best['discontinuities']}")
        print(f"   {data['how_to_export']}")
    for entry in payload.get("diagnostics", [])[:2]:
        print(f"   [{entry['level']}] {entry['message'][:120]}")

    if not best:
        return 1

    print("\n" + "=" * 84)
    print("STEP 2  read depth levels at that event")
    print("=" * 84)
    read = call_tool(
        "read-replay-target",
        {
            "session": "Tiled",
            "draw_index": best["draw_index"],
            "depth": True,
            "at_x": 766,
            "at_y": 382,
            "pixels": 6,
            "keep": str(OUT / "best_depth.png"),
        },
    )
    print(f"status: {read['status']}")
    info = read["data"]
    image = info["image"]
    print(f"   {image['width']}x{image['height']}  bit_depth={image['bit_depth']}  "
          f"max_level={image['max_level']}")
    print(f"   character={info.get('content_character')} "
          f"levels={info.get('distinct_levels')} "
          f"range={info.get('min_level')}..{info.get('max_level')} "
          f"edges={info.get('discontinuities_sampled')}")
    print(f"   pixel: {info.get('pixel')}")
    print(f"   first levels: {info.get('pixels')}")
    print(f"   note: {info['values_are'][:110]}")

    print("\n" + "=" * 84)
    print("STEP 3  the same read at the pass from the question, for contrast")
    print("=" * 84)
    contrast = call_tool(
        "read-replay-target",
        {
            "session": "Tiled",
            "queue_id": 17765,
            "depth": True,
            "at_x": 766,
            "at_y": 382,
            "keep": str(OUT / "q17765_depth.png"),
        },
    )
    info = contrast["data"]
    print(f"status: {contrast['status']}")
    print(f"   character={info.get('content_character')} "
          f"levels={info.get('distinct_levels')} "
          f"edges={info.get('discontinuities_sampled')}")
    print(f"   pixel: {info.get('pixel')}")
    for entry in contrast.get("diagnostics", [])[:1]:
        print(f"   [{entry['level']}] {entry['message'][:120]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
