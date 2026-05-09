"""Decode DDS files that pixtool writes for replayed render targets.

Only needed because pixtool's lossless output is DDS. A PNG has already been
reduced to 8 bits and contrast-mapped, so it cannot carry the original values; a
DDS keeps the source DXGI format, which is what makes real numbers recoverable
from a GPU replay.

Depth buffers are excluded by pixtool itself (PIXTOOL13: "Cannot save Depth Buffer
as DDS ... Make sure file name for Depth Buffer ends with .png"), so replayed depth
is 8-bit only. That limitation belongs to the tool chain, not to this parser.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_MAGIC = b"DDS "
_HEADER_SIZE = 124
_DDPF_FOURCC = 0x4

# DXGI_FORMAT values that appear in pixtool output, with how to read one element.
# (name, bytes per pixel, component count, struct code per component)
DXGI_FORMATS: dict[int, tuple[str, int, int, str]] = {
    2: ("R32G32B32A32_FLOAT", 16, 4, "f"),
    3: ("R32G32B32A32_UINT", 16, 4, "I"),
    6: ("R32G32B32_FLOAT", 12, 3, "f"),
    10: ("R16G16B16A16_FLOAT", 8, 4, "e"),
    11: ("R16G16B16A16_UNORM", 8, 4, "H"),
    12: ("R16G16B16A16_UINT", 8, 4, "H"),
    16: ("R32G32_FLOAT", 8, 2, "f"),
    17: ("R32G32_UINT", 8, 2, "I"),
    24: ("R10G10B10A2_UNORM", 4, 1, "I"),
    26: ("R11G11B10_FLOAT", 4, 1, "I"),
    28: ("R8G8B8A8_UNORM", 4, 4, "B"),
    29: ("R8G8B8A8_UNORM_SRGB", 4, 4, "B"),
    34: ("R16G16_FLOAT", 4, 2, "e"),
    35: ("R16G16_UNORM", 4, 2, "H"),
    41: ("R32_FLOAT", 4, 1, "f"),
    42: ("R32_UINT", 4, 1, "I"),
    43: ("R32_SINT", 4, 1, "i"),
    54: ("R16_FLOAT", 2, 1, "e"),
    56: ("R16_UNORM", 2, 1, "H"),
    57: ("R16_UINT", 2, 1, "H"),
    61: ("R8_UNORM", 1, 1, "B"),
    62: ("R8_UINT", 1, 1, "B"),
    87: ("B8G8R8A8_UNORM", 4, 4, "B"),
}

# Formats whose integer storage represents a normalised 0..1 value.
_UNORM_MAX = {"H": 65535.0, "B": 255.0}


# Legacy D3DFORMAT codes that can appear in the fourcc field, mapped to DXGI.
_D3DFORMAT_TO_DXGI: dict[int, int] = {
    36: 11,   # D3DFMT_A16B16G16R16   -> R16G16B16A16_UNORM
    110: 11,  # D3DFMT_Q16W16V16U16   -> R16G16B16A16_UNORM (same layout)
    113: 10,  # D3DFMT_A16B16G16R16F  -> R16G16B16A16_FLOAT
    114: 54,  # D3DFMT_R16F           -> R16_FLOAT
    115: 34,  # D3DFMT_G16R16F        -> R16G16_FLOAT
    116: 2,   # D3DFMT_A32B32G32R32F  -> R32G32B32A32_FLOAT
    111: 41,  # D3DFMT_R32F           -> R32_FLOAT
    112: 16,  # D3DFMT_G32R32F        -> R32G32_FLOAT
}


@dataclass(frozen=True, slots=True)
class DdsImage:
    width: int
    height: int
    dxgi_format: int
    format_name: str
    bytes_per_pixel: int
    component_count: int
    component_code: str
    pixel_offset: int
    data: bytes

    @property
    def row_pitch(self) -> int:
        return self.width * self.bytes_per_pixel

    def to_dict(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "dxgi_format": self.dxgi_format,
            "format": self.format_name,
            "bytes_per_pixel": self.bytes_per_pixel,
            "components": self.component_count,
            "pixel_data_offset": self.pixel_offset,
            "pixel_bytes": len(self.data) - self.pixel_offset,
        }

    def pixel(self, x: int, y: int, *, normalise: bool = True) -> Any:
        """Value at (x, y): a scalar for 1-component formats, else a list."""
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise IndexError(f"({x}, {y}) is outside {self.width}x{self.height}")
        base = self.pixel_offset + y * self.row_pitch + x * self.bytes_per_pixel
        if base + self.bytes_per_pixel > len(self.data):
            raise IndexError(f"pixel ({x}, {y}) is past the end of the DDS payload")

        # Bit-packed formats store all channels inside one integer, so returning
        # that integer would be useless to a caller asking for pixel values.
        unpacker = _PACKED_UNPACKERS.get(self.format_name)
        if unpacker is not None:
            packed = struct.unpack_from("<I", self.data, base)[0]
            return unpacker(packed, normalise)

        values = list(
            struct.unpack_from(
                f"<{self.component_count}{self.component_code}", self.data, base
            )
        )
        if normalise and self.format_name.endswith(("UNORM", "UNORM_SRGB")):
            scale = _UNORM_MAX.get(self.component_code)
            if scale:
                values = [value / scale for value in values]
        return values[0] if len(values) == 1 else values

    def iter_pixels(self, limit: int | None = None, *, normalise: bool = True):
        """Yield pixels in row-major order."""
        count = self.width * self.height
        if limit is not None:
            count = min(count, limit)
        for index in range(count):
            yield self.pixel(index % self.width, index // self.width, normalise=normalise)


def _unpack_r10g10b10a2(packed: int, normalise: bool) -> list[float]:
    """R10G10B10A2_UNORM: low bits are red, top two bits are alpha."""
    red = packed & 0x3FF
    green = (packed >> 10) & 0x3FF
    blue = (packed >> 20) & 0x3FF
    alpha = (packed >> 30) & 0x3
    if not normalise:
        return [red, green, blue, alpha]
    return [red / 1023.0, green / 1023.0, blue / 1023.0, alpha / 3.0]


def _float_from_bits(bits: int, mantissa_bits: int, exponent_bits: int) -> float:
    """Reconstruct a small unsigned float (no sign bit), as used by R11G11B10."""
    mantissa = bits & ((1 << mantissa_bits) - 1)
    exponent = (bits >> mantissa_bits) & ((1 << exponent_bits) - 1)
    bias = (1 << (exponent_bits - 1)) - 1
    if exponent == 0:
        if mantissa == 0:
            return 0.0
        return mantissa / (1 << mantissa_bits) * (2.0 ** (1 - bias))
    if exponent == (1 << exponent_bits) - 1:
        return float("inf") if mantissa == 0 else float("nan")
    return (1.0 + mantissa / (1 << mantissa_bits)) * (2.0 ** (exponent - bias))


def _unpack_r11g11b10(packed: int, normalise: bool) -> list[float]:
    """R11G11B10_FLOAT: two 11-bit floats then one 10-bit float, all unsigned."""
    red = _float_from_bits(packed & 0x7FF, 6, 5)
    green = _float_from_bits((packed >> 11) & 0x7FF, 6, 5)
    blue = _float_from_bits((packed >> 22) & 0x3FF, 5, 5)
    return [red, green, blue]


# Formats whose channels are bit fields inside a single integer.
_PACKED_UNPACKERS = {
    "R10G10B10A2_UNORM": _unpack_r10g10b10a2,
    "R11G11B10_FLOAT": _unpack_r11g11b10,
}


def parse(path: str | Path) -> DdsImage:
    """Read a DDS written by pixtool. Raises ValueError when unsupported."""
    blob = Path(path).read_bytes()
    if len(blob) < 128 or blob[:4] != _MAGIC:
        raise ValueError("not a DDS file")
    size = struct.unpack_from("<I", blob, 4)[0]
    if size != _HEADER_SIZE:
        raise ValueError(f"unexpected DDS header size {size}")

    height, width = struct.unpack_from("<II", blob, 12)
    pf_flags = struct.unpack_from("<I", blob, 80)[0]
    fourcc = blob[84:88]

    offset = 128
    dxgi = None
    if pf_flags & _DDPF_FOURCC and fourcc == b"DX10":
        dxgi = struct.unpack_from("<I", blob, 128)[0]
        offset = 148
    elif pf_flags & _DDPF_FOURCC:
        # pixtool also emits a legacy header whose fourcc field holds a numeric
        # D3DFORMAT rather than four characters (observed: 36 = A16B16G16R16 for an
        # R16G16B16A16_UNORM target).
        code = struct.unpack_from("<I", blob, 84)[0]
        dxgi = _D3DFORMAT_TO_DXGI.get(code)
        if dxgi is None:
            raise ValueError(
                f"legacy DDS fourcc {code} ({fourcc!r}) is not a known D3DFORMAT"
            )
    else:
        # Uncompressed legacy header: derive the format from the bit layout.
        bit_count = struct.unpack_from("<I", blob, 88)[0]
        masks = struct.unpack_from("<4I", blob, 92)
        dxgi = _legacy_format(bit_count, masks)
        if dxgi is None:
            raise ValueError(
                f"legacy DDS pixel format not recognised "
                f"(bits={bit_count}, masks={[hex(m) for m in masks]})"
            )

    spec = DXGI_FORMATS.get(dxgi)
    if spec is None:
        raise ValueError(f"DXGI format {dxgi} is not supported for decoding")
    name, bpp, components, code = spec
    return DdsImage(
        width=width,
        height=height,
        dxgi_format=dxgi,
        format_name=name,
        bytes_per_pixel=bpp,
        component_count=components,
        component_code=code,
        pixel_offset=offset,
        data=blob,
    )


def _legacy_format(bit_count: int, masks: tuple[int, ...]) -> int | None:
    """Map a legacy DDPIXELFORMAT to a DXGI format number."""
    red, green, blue, alpha = masks
    if bit_count == 64 and red == 0xFFFF and green == 0xFFFF0000:
        return 11  # R16G16B16A16_UNORM
    if bit_count == 32:
        if red == 0x00FF0000 and blue == 0x000000FF:
            return 87  # B8G8R8A8_UNORM
        if red == 0x000000FF and blue == 0x00FF0000:
            return 28  # R8G8B8A8_UNORM
        if red == 0xFFFFFFFF:
            return 42  # R32_UINT
    if bit_count == 16 and red == 0xFFFF:
        return 56  # R16_UNORM
    if bit_count == 8 and red == 0xFF:
        return 61  # R8_UNORM
    return None
