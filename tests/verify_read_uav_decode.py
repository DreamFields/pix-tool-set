"""Check the read-uav decode path against a dump taken from a real replay.

Reuses the .bin plus layout sidecar that `read-uav` already wrote, so the whole
90-second replay does not have to run again. What it guards:

  * bit-packed formats decode to all their channels. R10G10B10A2_UNORM stores four
    channels inside one 32-bit integer, so `component_count` is 1 by design; using
    that as the channel count labelled a colour PNG "single channel as grey".
  * row pitch padding is dropped (6144-byte pitch carrying 6128 bytes of pixels).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set.engine import uavprobe

HERE = Path(__file__).resolve().parent


def newest_dump() -> tuple[Path, Path] | None:
    candidates: list[tuple[float, Path, Path]] = []
    for sidecar in HERE.glob("_uav_check*/*.bin.txt"):
        blob = sidecar.with_suffix("")
        if blob.exists():
            candidates.append((sidecar.stat().st_mtime, blob, sidecar))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1], candidates[0][2]


def main() -> int:
    found = newest_dump()
    if found is None:
        print("SKIP: no read-uav dump under tests/_uav_check*; run read-uav first")
        return 0
    blob_path, sidecar_path = found
    print(f"dump    : {blob_path.name}")

    dump = uavprobe.read_sidecar(blob_path)
    print(f"layout  : {dump.width}x{dump.height} fmt={dump.dxgi_format} "
          f"pitch={dump.row_pitch} row_size={dump.row_size_bytes}")

    failures: list[str] = []

    blob = blob_path.read_bytes()
    packed = uavprobe.depad(blob, dump)
    expected_packed = dump.row_size_bytes * dump.height
    print(f"depad   : {len(blob)} -> {len(packed)} bytes (expected {expected_packed})")
    if len(packed) != expected_packed:
        failures.append(f"depad produced {len(packed)}, expected {expected_packed}")
    if dump.row_pitch == dump.row_size_bytes:
        print("note    : this dump has no pitch padding, so that part is untested")

    image = uavprobe.as_image(packed, dump)
    stats = uavprobe.statistics(image)
    channels = stats.get("channels") or []
    print(f"decoded : {image.format_name} storage_units={image.component_count} "
          f"channels={len(channels)}")
    for entry in channels:
        print(f"          {entry['channel']}: mean_8bit={entry['mean_8bit']}")

    if dump.dxgi_format == 24:  # R10G10B10A2_UNORM
        if image.component_count != 1:
            failures.append(
                f"expected component_count 1 for a packed format, got {image.component_count}"
            )
        if len(channels) != 4:
            failures.append(f"expected 4 decoded channels, got {len(channels)}")

    # The PNG label must follow the decoded channels, not the storage units.
    _, mapping = uavprobe.to_rgb_png(image)
    print(f"png     : decoded_channels={mapping.get('decoded_channels')} "
          f"channels_shown={mapping.get('channels_shown')!r}")
    if len(channels) >= 3 and mapping.get("channels_shown") != "RGB":
        failures.append(
            f"a {len(channels)}-channel surface was labelled {mapping.get('channels_shown')!r}"
        )

    if failures:
        print("\nFAIL")
        for item in failures:
            print("  -", item)
        return 1
    print("\nPASS: packed channels decode and the PNG label matches them")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
