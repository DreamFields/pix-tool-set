"""Subresource footprints for texture uploads.

A texture is not a flat pixel array in resources.bin. The export uploads it with
CopyTextureRegion and a placed footprint, so the blob is a sequence of
subresources, each with its own format, dimensions and *row pitch* which is padded
to a hardware alignment. The footprints are emitted as::

    static ResourceInitInfo g_resourceInitInfo_1985_0[] =
    {
        { 0,       { DXGI_FORMAT_R32_TYPELESS, 1532, 764, 1, 6144 }, 0 },
        { 4694016, { DXGI_FORMAT_R8_TYPELESS,  1532, 764, 1, 1536 }, 1 }
    };

For a depth-stencil surface those two entries are the depth plane and the stencil
plane. Ignoring them and dividing total bytes by pixel count yields nonsense
(5.013 bytes per pixel for the case above), so the footprint has to be read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_RE_ARRAY = re.compile(
    r"static\s+ResourceInitInfo\s+g_resourceInitInfo_(\d+)_(\d+)\s*\[\]\s*=\s*\{",
)
_RE_ENTRY = re.compile(
    r"\{\s*(\d+)\s*,\s*\{\s*(DXGI_FORMAT_\w+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,"
    r"\s*(\d+)\s*\}\s*,\s*(\d+)\s*\}"
)


@dataclass(frozen=True, slots=True)
class SubresourceFootprint:
    """One subresource inside a texture upload blob."""

    offset: int
    format: str
    width: int
    height: int
    depth: int
    row_pitch: int
    subresource_index: int

    @property
    def slice_bytes(self) -> int:
        """Bytes of one z slice, padding included.

        A Texture3D packs every z slice into the *same* subresource, one after another,
        each occupying ``row_pitch * height`` bytes. A Tex2DArray instead gets one
        subresource per layer. This property is what lets a caller address a z slice
        without conflating the two layouts.
        """
        return self.row_pitch * self.height

    @property
    def size_bytes(self) -> int:
        return self.row_pitch * self.height * max(self.depth, 1)

    @property
    def is_volume(self) -> bool:
        return self.depth > 1

    def slice_offset(self, z: int) -> int:
        return self.offset + z * self.slice_bytes

    def to_dict(self) -> dict:
        out = {
            "subresource_index": self.subresource_index,
            "offset": self.offset,
            "format": self.format,
            "width": self.width,
            "height": self.height,
            "depth": self.depth,
            "row_pitch": self.row_pitch,
            "size_bytes": self.size_bytes,
        }
        if self.is_volume:
            out["slice_bytes"] = self.slice_bytes
            out["z_slices"] = self.depth
            out["layout"] = (
                "volume: all z slices live in this one subresource, "
                f"{self.slice_bytes} bytes apart"
            )
        return out


def parse_footprints(root: Path) -> dict[int, list[SubresourceFootprint]]:
    """resource id -> its subresource footprints, ordered as uploaded."""
    header = root / "CapturedAssets.h"
    if not header.exists():
        return {}
    text = header.read_text(encoding="utf-8", errors="replace")

    out: dict[int, list[SubresourceFootprint]] = {}
    for match in _RE_ARRAY.finditer(text):
        resource_id = int(match.group(1))
        end = text.find("};", match.end())
        if end < 0:
            continue
        body = text[match.end() : end]
        entries = []
        for item in _RE_ENTRY.finditer(body):
            entries.append(
                SubresourceFootprint(
                    offset=int(item.group(1)),
                    format=item.group(2),
                    width=int(item.group(3)),
                    height=int(item.group(4)),
                    depth=int(item.group(5)),
                    row_pitch=int(item.group(6)),
                    subresource_index=int(item.group(7)),
                )
            )
        if entries:
            # Several arrays can exist per resource (one per upload batch); keep
            # them appended in declaration order.
            out.setdefault(resource_id, []).extend(entries)
    return out


# Bytes per pixel for the formats that appear in depth/colour footprints.
_FORMAT_STRIDE = {
    "DXGI_FORMAT_R32_TYPELESS": 4,
    "DXGI_FORMAT_R32_FLOAT": 4,
    "DXGI_FORMAT_R32_UINT": 4,
    "DXGI_FORMAT_R8_TYPELESS": 1,
    "DXGI_FORMAT_R8_UINT": 1,
    "DXGI_FORMAT_R8_UNORM": 1,
    "DXGI_FORMAT_R16_TYPELESS": 2,
    "DXGI_FORMAT_R16_FLOAT": 2,
    "DXGI_FORMAT_R16_UNORM": 2,
    "DXGI_FORMAT_R16_UINT": 2,
    "DXGI_FORMAT_R8G8B8A8_UNORM": 4,
    "DXGI_FORMAT_R8G8B8A8_TYPELESS": 4,
    "DXGI_FORMAT_B8G8R8A8_UNORM": 4,
    "DXGI_FORMAT_R16G16B16A16_UNORM": 8,
    "DXGI_FORMAT_R16G16B16A16_FLOAT": 8,
    "DXGI_FORMAT_R32G32B32A32_FLOAT": 16,
    "DXGI_FORMAT_R11G11B10_FLOAT": 4,
    "DXGI_FORMAT_R10G10B10A2_UNORM": 4,
    "DXGI_FORMAT_R16G16_FLOAT": 4,
    "DXGI_FORMAT_R16G16_UNORM": 4,
    "DXGI_FORMAT_R32G32_FLOAT": 8,
}


def format_stride(name: str) -> int | None:
    """Bytes per pixel, or None when the format is block-compressed/unknown."""
    return _FORMAT_STRIDE.get((name or "").upper())


def extract_rows(
    blob: bytes, footprint: SubresourceFootprint, z: int = 0
) -> list[bytes] | None:
    """Split one z slice of a subresource into tightly packed rows.

    ``z`` defaults to 0, which is the only slice a 2D texture has. For a Texture3D it
    selects which depth slice to read, because all of them share one subresource.

    Returns None when the format cannot be laid out. A short read (the capture holds
    fewer bytes than the footprint declares) yields the rows that *are* present rather
    than nothing, so the caller can see how far the data goes and report it.
    """
    stride = format_stride(footprint.format)
    if stride is None:
        return None
    if z < 0 or z >= max(footprint.depth, 1):
        return None
    row_bytes = footprint.width * stride
    base = footprint.slice_offset(z)
    rows: list[bytes] = []
    for y in range(footprint.height):
        start = base + y * footprint.row_pitch
        if start + row_bytes > len(blob):
            break
        rows.append(blob[start : start + row_bytes])
    return rows


def slice_availability(
    blob: bytes, footprint: SubresourceFootprint
) -> dict[str, int | bool]:
    """How many z slices the recorded bytes actually cover.

    Captures can be a few bytes short of what the footprint declares, so the last
    slice may be partial. Saying so is better than silently returning a truncated
    image that looks complete.
    """
    slice_bytes = footprint.slice_bytes
    declared = max(footprint.depth, 1)
    if slice_bytes <= 0:
        return {"declared": declared, "complete": 0, "partial": False}
    available = max(len(blob) - footprint.offset, 0)
    whole = available // slice_bytes
    return {
        "declared": declared,
        "complete": min(whole, declared),
        "partial": whole < declared and available % slice_bytes > 0,
        "bytes_recorded": available,
        "bytes_declared": slice_bytes * declared,
    }
