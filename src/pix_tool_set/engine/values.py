"""Decode constant buffer field values from captured bytes.

The shader reflection gives each cbuffer field a name, offset and type; the
resource stream gives the bytes the root CBV points at. Combining the two turns
"cb0 is 4 MB at offset 3529728" into named values, which is what a user actually
wants when asking what a pass was configured with.
"""

from __future__ import annotations

import struct
from typing import Any

# HLSL scalar kinds we can decode straight from bytes.
_SCALARS = {
    "float": ("f", 4),
    "int": ("i", 4),
    "uint": ("I", 4),
    "dword": ("I", 4),
    "bool": ("I", 4),
    "half": ("e", 2),
    "double": ("d", 8),
}


def _base_and_count(type_name: str) -> tuple[str, int, int]:
    """Split an HLSL type into (scalar, components, rows).

    float4          -> ("float", 4, 1)
    float4x4        -> ("float", 4, 4)
    row_major float4x4 -> ("float", 4, 4)   (storage modifiers are ignored)
    uint            -> ("uint", 1, 1)
    """
    name = (type_name or "").strip().lower()
    for modifier in ("row_major", "column_major", "const", "static", "unorm", "snorm"):
        if name.startswith(modifier + " "):
            name = name[len(modifier) + 1 :].strip()
    for scalar in _SCALARS:
        if not name.startswith(scalar):
            continue
        suffix = name[len(scalar) :]
        if not suffix:
            return scalar, 1, 1
        if "x" in suffix:
            left, _, right = suffix.partition("x")
            if left.isdigit() and right.isdigit():
                return scalar, int(left), int(right)
        if suffix.isdigit():
            return scalar, int(suffix), 1
    return "", 0, 0


def decode_field(blob: bytes, offset: int, type_name: str) -> Any:
    """Decode one field, or None when the type is not a plain numeric."""
    scalar, components, rows = _base_and_count(type_name)
    if not scalar or components == 0:
        return None
    code, width = _SCALARS[scalar]
    total = components * rows
    end = offset + total * width
    if offset < 0 or end > len(blob):
        return None
    values = list(struct.unpack_from(f"<{total}{code}", blob, offset))
    if scalar == "bool":
        values = [bool(v) for v in values]
    if rows > 1:
        return [values[i * components : (i + 1) * components] for i in range(rows)]
    return values[0] if total == 1 else values


def decode_layout(
    blob: bytes, fields: list[dict[str, Any]], *, base_offset: int = 0
) -> list[dict[str, Any]]:
    """Decode every field of a cbuffer layout against captured bytes."""
    out: list[dict[str, Any]] = []
    for field in fields:
        offset = field.get("offset")
        type_name = field.get("type") or ""
        row: dict[str, Any] = {
            "name": field.get("name"),
            "type": type_name,
            "offset": offset,
            "size": field.get("size"),
        }
        if field.get("is_padding"):
            # PIX lists trailing padding with no value; mirror that instead of
            # inventing a number for bytes the shader never reads.
            row["value"] = None
            row["is_padding"] = True
            row["note"] = "trailing padding"
        elif offset is None:
            row["value"] = None
            row["note"] = "reflection did not report an offset"
        else:
            value = decode_field(blob, base_offset + int(offset), type_name)
            row["value"] = value
            if value is None:
                row["note"] = "type is not a plain scalar/vector/matrix"
        out.append(row)
    return out


def hexdump(blob: bytes, *, start: int = 0, limit: int = 256, width: int = 16) -> list[str]:
    """Compact hex+ASCII dump, useful when a layout is unavailable."""
    lines: list[str] = []
    view = blob[:limit]
    for position in range(0, len(view), width):
        chunk = view[position : position + width]
        hexpart = " ".join(f"{byte:02x}" for byte in chunk)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{start + position:08x}  {hexpart:<{width * 3}}  {text}")
    return lines


def decode_typed_array(
    blob: bytes, element_type: str, *, max_elements: int = 32
) -> list[Any]:
    """Decode a buffer as a flat array of one scalar type."""
    scalar, components, rows = _base_and_count(element_type)
    if not scalar:
        return []
    code, width = _SCALARS[scalar]
    stride = components * rows * width
    if stride == 0:
        return []
    count = min(len(blob) // stride, max_elements)
    out: list[Any] = []
    for index in range(count):
        value = decode_field(blob, index * stride, element_type)
        out.append(value)
    return out
