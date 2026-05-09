"""Compare the two ways of getting depth out, and make both viewable.

Path A: read-resource-texture, straight from resources.bin, no replay.
Path B: save-render-target, which replays the frame on the GPU.

They answer different questions. A gives the depth buffer's initial contents,
B gives what the pass actually left in it. Showing both side by side is the point:
the numbers differ, and a caller has to know which one they got.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set import call_tool  # noqa: E402
from pix_tool_set.context import clear_capture_cache  # noqa: E402

QUEUE_ID = 17765
OUT = Path("G:/pix-tool-set/depth-out")


def main() -> int:
    clear_capture_cache()
    OUT.mkdir(parents=True, exist_ok=True)

    print("=" * 80)
    print("PATH A: capture bytes, no GPU replay")
    print("=" * 80)
    payload = call_tool(
        "read-resource-texture",
        {
            "session": "Tiled",
            "queue_id": QUEUE_ID,
            "target": "depth",
            "output": str(OUT),
            "png": str(OUT),
        },
    )
    print(f"status: {payload['status']}")
    data = payload["data"]
    for plane in data["planes"]:
        print(f"\nplane {plane['subresource_index']} {plane['format']}")
        print(f"   {plane['width']}x{plane['height']} pitch={plane['row_pitch']} "
              f"pixels={plane.get('pixels'):,}")
        if plane.get("min") is not None:
            print(f"   range={plane['min']:.8g}..{plane['max']:.8g}")
        if plane.get("content_character"):
            print(f"   character={plane['content_character']} "
                  f"edges={plane.get('discontinuities_sampled')}")
        if plane.get("output"):
            print(f"   raw: {Path(plane['output']).name}")
        if plane.get("png"):
            path = Path(plane["png"])
            print(f"   png: {path.name} ({path.stat().st_size:,} B)")
    for image in data.get("images") or []:
        print(f"\n   stretched {image['stretched_from']:.8g} .. "
              f"{image['stretched_to']:.8g} -> {Path(image['path']).name}")
    for entry in payload.get("diagnostics", [])[:2]:
        print(f"\n   [{entry['level']}] {entry['message'][:130]}")

    print("\n" + "=" * 80)
    print("PATH B: GPU replay via pixtool")
    print("=" * 80)
    replay = call_tool(
        "save-render-target",
        {
            "session": "Tiled",
            "queue_id": QUEUE_ID,
            "depth": True,
            "output": str(OUT / "replay_depth.png"),
        },
    )
    print(f"status: {replay['status']}")
    if replay["status"] != "error":
        info = replay["data"]
        path = Path(info["path"])
        print(f"   file: {path.name} ({info['bytes']:,} B)")
        print(f"   resource {info['resource']['resource_id']} "
              f"{info['resource']['format']}")
        print(f"   pass: {info['pass_name']}")

    # Quantify the difference: how many pixels differ between the two?
    raw = OUT / f"resource1985_sub0_1532x764_R32_TYPELESS.bin"
    if raw.exists():
        blob = raw.read_bytes()
        count = min(len(blob) // 4, 300000)
        values = struct.unpack_from(f"<{count}f", blob, 0)
        distinct = len({round(v, 9) for v in values})
        print(f"\npath A depth: {count:,} sampled, {distinct:,} distinct values")
        print(f"   -> {distinct / count:.4f} distinct ratio "
              f"({'smooth ramp' if distinct / count > 0.9 else 'quantised'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
