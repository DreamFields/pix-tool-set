"""Read real pixel values out of a GPU replay, and show the two hard limits."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set import call_tool  # noqa: E402
from pix_tool_set.context import clear_capture_cache  # noqa: E402

OUT = Path("G:/pix-tool-set/depth-out/replay")


def report(label: str, payload: dict) -> None:
    print("\n" + "=" * 78)
    print(f"{label}   status={payload['status']}")
    print("=" * 78)
    if payload["status"] == "error":
        error = payload["error"]
        print(f"   {error['code']}: {error['message'][:150]}")
        return
    data = payload["data"]
    image = data.get("image") or {}
    print(f"   draw #{data['draw_index']}  {data['pass_name'][:44]}")
    print(f"   rtv{data['rtv']} rid={data['resource_id']}")
    if image:
        print(f"   {image['format']}  {image['width']}x{image['height']}  "
              f"bpp={image['bytes_per_pixel']}  dxgi={image['dxgi_format']}")
        print(f"   payload matches dimensions: {data['payload_matches_dimensions']}")
    print(f"   nonzero bytes: {data['nonzero_bytes']:,} "
          f"({100.0 * data['nonzero_byte_ratio']:.2f}%)   empty={data['surface_is_empty']}")
    if data.get("pixel"):
        print(f"   pixel: {data['pixel']}")
    if data.get("pixels"):
        print(f"   first pixels: {data['pixels'][:4]}")
    print(f"   note: {data['values_are'][:110]}")
    for entry in payload.get("diagnostics", [])[:2]:
        print(f"   [{entry['level']}] {entry['message'][:120]}")


def main() -> int:
    clear_capture_cache()
    OUT.mkdir(parents=True, exist_ok=True)

    # 1. The pass from the question: its own targets are not yet written.
    report(
        "Queue ID 17765 rtv1 (the pass's own target)",
        call_tool(
            "read-replay-target",
            {
                "session": "tiled",
                "queue_id": 17765,
                "rtv": 1,
                "at_x": 766,
                "at_y": 382,
                "keep": str(OUT / "q17765_rtv1.dds"),
            },
        ),
    )

    # 2. A target that earlier draws have populated: real values come back.
    report(
        "draw 2328 rtv0 (written by earlier draws)",
        call_tool(
            "read-replay-target",
            {
                "session": "tiled",
                "draw_index": 2328,
                "rtv": 0,
                "at_x": 900,
                "at_y": 500,
                "pixels": 3,
                "keep": str(OUT / "draw2328_rtv0.dds"),
            },
        ),
    )

    # 3. Depth is refused, with the reason.
    report(
        "Queue ID 17765 depth (expected to be refused)",
        call_tool(
            "read-replay-target",
            {"session": "tiled", "queue_id": 17765, "rtv": 0, "keep": str(OUT / "d.dds")},
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
