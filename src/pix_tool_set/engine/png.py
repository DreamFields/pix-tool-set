"""Decode the PNG files pixtool writes, including 16-bit greyscale depth.

Written because pixtool's depth export is greyscale bit_depth=16, not 8 as first
assumed. 16 bits carries 65536 levels, so the image is a usable quantitative
artefact rather than a thumbnail -- but it is still a quantisation of a 32-bit
float, and pixtool calls it a "visual representation", so values recovered from it
are normalised levels, not the original depth.

Pure standard library: PNG row filters are undone here rather than pulling in an
image dependency for one format.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


@dataclass(frozen=True, slots=True)
class PngImage:
    width: int
    height: int
    bit_depth: int
    colour_type: int
    channels: int
    samples: list[int]

    @property
    def max_level(self) -> int:
        return (1 << self.bit_depth) - 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "bit_depth": self.bit_depth,
            "colour_type": self.colour_type,
            "channels": self.channels,
            "max_level": self.max_level,
            "sample_count": len(self.samples),
        }

    def pixel(self, x: int, y: int) -> Any:
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise IndexError(f"({x}, {y}) is outside {self.width}x{self.height}")
        base = (y * self.width + x) * self.channels
        values = self.samples[base : base + self.channels]
        return values[0] if len(values) == 1 else list(values)

    def channel(self, index: int = 0) -> list[int]:
        """One channel as a flat list, row-major."""
        if self.channels == 1:
            return list(self.samples)
        return self.samples[index :: self.channels]


def parse(path: str | Path) -> PngImage:
    """Decode a PNG to raw sample values. Raises ValueError when unsupported."""
    blob = Path(path).read_bytes()
    if blob[:8] != _SIGNATURE:
        raise ValueError("not a PNG file")

    width = height = 0
    bit_depth = colour_type = 0
    interlace = 0
    idat = bytearray()
    pos = 8
    while pos + 8 <= len(blob):
        length = struct.unpack_from(">I", blob, pos)[0]
        tag = blob[pos + 4 : pos + 8]
        payload = blob[pos + 8 : pos + 8 + length]
        if tag == b"IHDR":
            width, height = struct.unpack_from(">II", payload, 0)
            bit_depth, colour_type = payload[8], payload[9]
            interlace = payload[12]
        elif tag == b"IDAT":
            idat.extend(payload)
        elif tag == b"IEND":
            break
        pos += 12 + length

    if not idat:
        raise ValueError("PNG has no image data")
    if interlace:
        raise ValueError("interlaced PNG is not supported")
    if bit_depth not in (8, 16):
        raise ValueError(f"bit depth {bit_depth} is not supported")
    channels = _CHANNELS.get(colour_type)
    if channels is None:
        raise ValueError(f"colour type {colour_type} is not supported")

    raw = zlib.decompress(bytes(idat))
    unit = channels * (bit_depth // 8)
    stride = width * unit
    previous = bytearray(stride)
    samples: list[int] = []
    pos = 0
    for _ in range(height):
        if pos >= len(raw):
            break
        filter_type = raw[pos]
        pos += 1
        line = bytearray(raw[pos : pos + stride])
        pos += stride
        if len(line) < stride:
            break
        _unfilter(line, previous, filter_type, unit)
        if bit_depth == 16:
            samples.extend(struct.unpack(f">{width * channels}H", bytes(line)))
        else:
            samples.extend(line)
        previous = line

    return PngImage(
        width=width,
        height=height,
        bit_depth=bit_depth,
        colour_type=colour_type,
        channels=channels,
        samples=samples,
    )


def _unfilter(line: bytearray, previous: bytearray, filter_type: int, unit: int) -> None:
    """Reverse one PNG scanline filter in place."""
    if filter_type == 0:
        return
    for x in range(len(line)):
        a = line[x - unit] if x >= unit else 0
        b = previous[x]
        if filter_type == 1:
            line[x] = (line[x] + a) & 0xFF
        elif filter_type == 2:
            line[x] = (line[x] + b) & 0xFF
        elif filter_type == 3:
            line[x] = (line[x] + ((a + b) >> 1)) & 0xFF
        elif filter_type == 4:
            c = previous[x - unit] if x >= unit else 0
            p = a + b - c
            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
            pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
            line[x] = (line[x] + pred) & 0xFF
        else:
            raise ValueError(f"unknown PNG filter {filter_type}")


def content_character(image: PngImage, *, rows_sampled: int = 20) -> dict[str, Any]:
    """Does the image contain rendered geometry, or only a smooth gradient?

    Same test used for recorded depth: rendered surfaces have discontinuities where
    geometry occludes, an analytic gradient does not. This is what distinguishes a
    depth buffer that has been written from one that still holds its initial state.
    """
    values = image.channel(0)
    width, height = image.width, image.height
    if width < 4 or height < 4 or not values:
        return {}

    middle = values[(height // 2) * width : (height // 2 + 1) * width]
    diffs = [abs(middle[x + 1] - middle[x]) for x in range(len(middle) - 1)]
    if not diffs:
        return {}
    typical = sorted(diffs)[len(diffs) // 2] or 1

    edges = 0
    step = max(height // rows_sampled, 1)
    for y in range(0, height, step):
        row = values[y * width : (y + 1) * width]
        for x in range(len(row) - 1):
            if abs(row[x + 1] - row[x]) > typical * 50:
                edges += 1

    distinct = len(set(values))
    character = "rendered" if edges else "analytic_gradient"
    return {
        "content_character": character,
        "distinct_levels": distinct,
        "min_level": min(values),
        "max_level": max(values),
        "typical_step": typical,
        "discontinuities_sampled": edges,
        "content_note": (
            "discontinuities present, so this holds rendered geometry"
            if edges
            else "no discontinuities: the surface still holds its pre-render state"
        ),
    }
