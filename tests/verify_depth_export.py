"""Read the pass's depth buffer from the capture and export it to disk."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set import call_tool  # noqa: E402
from pix_tool_set.context import clear_capture_cache  # noqa: E402

QUEUE_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 17765
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("G:/pix-tool-set/depth-out")


def main() -> int:
    clear_capture_cache()
    payload = call_tool(
        "read-resource-texture",
        {
            "session": "tiled",
            "queue_id": QUEUE_ID,
            "target": "depth",
            "pixels": 6,
            "at_x": 766,
            "at_y": 382,
            "output": str(OUT),
        },
    )
    print("=" * 80)
    print(f"status : {payload['status']}")
    if payload["status"] == "error":
        print(payload["error"])
        return 1
    data = payload["data"]
    resource = data["resource"]
    print(f"resource : {data['resource_id']}  {resource.get('format')}  "
          f"{resource.get('width')}x{resource.get('height')}")
    print(f"blob     : {data['blob_bytes']:,} bytes")
    print(f"footprints: {data['footprint_count']}  "
          f"declared={data.get('footprint_total_bytes'):,}  "
          f"delta={data.get('footprint_vs_blob_delta')}")
    print(f"contents : {data['contents_are']}")
    print("=" * 80)

    for plane in data["planes"]:
        print(f"\nplane {plane['subresource_index']}  {plane['format']}  "
              f"{plane['width']}x{plane['height']}  pitch={plane['row_pitch']}")
        print(f"   decoded={plane['decoded']} rows={plane.get('rows_recovered')} "
              f"pixels={plane.get('pixels'):,} bpp={plane.get('bytes_per_pixel')}")
        if plane.get("min") is not None:
            print(f"   range   : min={plane['min']:.8g} max={plane['max']:.8g}")
            print(f"   zeros   : {plane.get('zero_count'):,} / "
                  f"{plane.get('sampled_pixels'):,} sampled")
            print(f"   in [0,1]: {plane.get('in_unit_range'):,}")
            if plane.get("nonzero_min") is not None:
                print(f"   nonzero : {plane['nonzero_min']:.8g} .. "
                      f"{plane['nonzero_max']:.8g}")
        if plane.get("content_character"):
            print(f"   character: {plane['content_character']}  "
                  f"(steps={plane.get('neighbour_step_distinct_values')}, "
                  f"edges={plane.get('discontinuities_sampled')})")
            print(f"   -> {plane.get('content_note')}")
        if plane.get("distinct_values") is not None:
            print(f"   distinct: {plane['distinct_values']}  "
                  f"top={plane.get('top_values')}")
        if plane.get("values") is not None:
            preview = plane["values"][:6]
            shown = [f"{v:.8g}" if isinstance(v, float) else str(v) for v in preview]
            print(f"   first 6 : {shown}")
        if plane.get("pixel"):
            print(f"   pixel   : {plane['pixel']}")
        if plane.get("output"):
            path = Path(plane["output"])
            print(f"   file    : {path.name}  "
                  f"({path.stat().st_size:,} bytes on disk)")

    for entry in data.get("files") or []:
        print(f"\nwritten: {entry['path']}")
        print(f"   {entry['bytes']:,} bytes, {entry['layout']}")

    for entry in payload.get("diagnostics", [])[:3]:
        print(f"\n[{entry['level']}] {entry['message'][:140]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
