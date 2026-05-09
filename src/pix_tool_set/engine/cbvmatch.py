"""Match root CBV parameters to the cbuffer layouts they actually supply.

Shared by pass-values and constant-buffer so the rule lives in one place.

Why it matters: a graphics draw commonly binds cb0/cb1/cb2 at once. Decoding
every layout against every root CBV yields one right answer and N-1 wrong ones,
all looking equally plausible. The root signature declares a shader register per
parameter and the shader reflection declares a cbN register per cbuffer, so the
two are joined on that number.

Why visibility is part of the key: a root signature may legally declare the same
register twice, once per stage. Observed in this capture on the Slate draws, where
root[2] is b0 visible to PIXEL and root[3] is b0 visible to VERTEX.
"""

from __future__ import annotations

from typing import Any, Optional

from .model import RootParameterKind

# D3D12_SHADER_VISIBILITY -> shader stage abbreviation used by the model.
VISIBILITY_STAGE = {
    "PIXEL": "PS",
    "VERTEX": "VS",
    "GEOMETRY": "GS",
    "HULL": "HS",
    "DOMAIN": "DS",
    "AMPLIFICATION": "AS",
    "MESH": "MS",
}


def collect_cbuffer_layouts(
    draw,
    *,
    stage: str | None = None,
    name: str | None = None,
) -> list[dict[str, Any]]:
    """Every decodable cbuffer layout on a draw, tagged with its cbN register."""
    stage_filter = (stage or "").strip().upper() or None
    name_filter = (name or "").strip().lower() or None

    layouts: list[dict[str, Any]] = []
    for shader in draw.shaders:
        if stage_filter and shader.stage.value.upper() != stage_filter:
            continue
        registers: dict[str, int] = {}
        for record in shader.resource_bindings:
            if record.get("type") != "cbuffer":
                continue
            bind = str(record.get("hlsl_bind") or "")
            cb_name = record.get("name")
            if cb_name and bind.startswith("cb") and bind[2:].isdigit():
                registers[cb_name] = int(bind[2:])
        for cbuffer in shader.constant_buffers:
            if not (cbuffer.get("fields") or []):
                continue
            if name_filter and str(cbuffer.get("name") or "").lower() != name_filter:
                continue
            layouts.append(
                {
                    "stage": shader.stage.value,
                    "shader_register": registers.get(cbuffer.get("name")),
                    **cbuffer,
                }
            )
    return layouts


def root_cbv_registers(capture, draw) -> dict[int, tuple[int, Optional[str]]]:
    """root parameter index -> (shader register, stage the parameter is visible to)."""
    signature = capture.root_signatures.get(draw.root_signature_id)
    if signature is None:
        return {}
    out: dict[int, tuple[int, Optional[str]]] = {}
    for parameter in signature.parameters:
        if parameter.kind is not RootParameterKind.CBV:
            continue
        visibility = str(getattr(parameter, "visibility", "") or "").upper()
        # ALL is deliberately mapped to None: it sees every stage and so cannot
        # disambiguate between two layouts sharing a register.
        out[parameter.index] = (
            parameter.shader_register,
            VISIBILITY_STAGE.get(visibility),
        )
    return out


def layouts_for_root(
    layouts: list[dict[str, Any]],
    registers: dict[int, tuple[int, Optional[str]]],
    root_index: int | None,
) -> tuple[list[dict[str, Any]], bool]:
    """Return (matching layouts, whether the register was known).

    When the register is unknown every layout is returned so nothing is hidden,
    but the flag lets the caller say that only one of them can be right.
    """
    info = registers.get(root_index)
    if info is None:
        return layouts, False
    register, stage = info
    matched = [
        layout for layout in layouts if layout.get("shader_register") == register
    ]
    if stage and len(matched) > 1:
        narrowed = [layout for layout in matched if layout.get("stage") == stage]
        if narrowed:
            return narrowed, True
    return matched, True
