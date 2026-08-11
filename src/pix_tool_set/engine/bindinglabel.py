"""Reproduce the PIX UI's "Binding" column for a resource on a given event.

The PIX resource-history view answers, for every event that touches a resource,
*how* the resource was reached: ``OM RTV 1``, ``CS SRV 2``, ``PS SRV 8``,
``API Parameters [1]``. Without that column a history says "this dispatch read
GBufferA" but not which ``t#`` register the shader saw it on, which is exactly
what is needed to go and read the HLSL.

Every part of the label is derived from the export; nothing is inferred from the
PIX UI itself:

* **OM RTV n** -- ``n`` is the index of the resource in the draw's render-target
  array, i.e. the RTV slot ``OMSetRenderTargets`` bound it to.
* **CS/PS/VS SRV n** -- ``n`` is the shader register (``t#``). A descriptor table
  binding gives a heap span; the root signature declares, per range, the
  ``BaseShaderRegister`` and how many descriptors follow it, so the register is
  ``base_shader_register + offset_within_range``. The stage prefix comes from the
  root parameter's ``ShaderVisibility``, falling back to the pipeline type
  (compute actions can only be ``CS``).
* **API Parameters [n]** -- used for events that reach the resource as a plain
  API argument rather than through a binding, e.g. a barrier. ``n`` is the
  resource's index in that call's argument array.

A label is only emitted when the underlying facts are present. When the register
cannot be pinned down the stage and view kind are still reported and the register
is left out, because a made-up ``t0`` would send the reader to the wrong
declaration -- a confident wrong answer is worse here than a partial one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from .model import RootParameterKind, ViewKind

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .capture import Capture
    from .model import DrawCall

# D3D12_SHADER_VISIBILITY_* -> the prefix PIX uses in the binding column.
_VISIBILITY_PREFIX: dict[str, str] = {
    "VERTEX": "VS",
    "HULL": "HS",
    "DOMAIN": "DS",
    "GEOMETRY": "GS",
    "PIXEL": "PS",
    "AMPLIFICATION": "AS",
    "MESH": "MS",
}

_VIEW_LABEL: dict[ViewKind, str] = {
    ViewKind.SRV: "SRV",
    ViewKind.UAV: "UAV",
    ViewKind.CBV: "CBV",
    ViewKind.SAMPLER: "SAMPLER",
}


@dataclass(slots=True)
class BindingLabel:
    """One way a single event reaches a single resource."""

    text: str
    category: str  # "output_merger" | "descriptor_table" | "root_descriptor" | "api_parameter"
    stage: Optional[str] = None
    view_kind: Optional[str] = None
    shader_register: Optional[int] = None
    register_space: Optional[int] = None
    slot: Optional[int] = None
    root_index: Optional[int] = None
    heap_id: Optional[int] = None
    heap_index: Optional[int] = None
    confidence: str = "exact"
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"binding": self.text, "category": self.category}
        for key, value in (
            ("stage", self.stage),
            ("view_kind", self.view_kind),
            ("shader_register", self.shader_register),
            ("register_space", self.register_space),
            ("slot", self.slot),
            ("root_index", self.root_index),
            ("heap_id", self.heap_id),
            ("heap_index", self.heap_index),
        ):
            if value is not None:
                payload[key] = value
        if self.confidence != "exact":
            payload["confidence"] = self.confidence
        if self.reason:
            payload["reason"] = self.reason
        return payload


def _stage_prefix(draw: "DrawCall", visibility: str) -> str:
    """Pick the stage prefix PIX would show.

    ``ShaderVisibility`` is the authoritative answer when the root parameter names
    one stage. ``ALL`` means the table is visible everywhere, and there PIX shows
    the stage that actually consumed it, so fall back to the pipeline type: a
    compute action has only CS, and a draw is attributed to PS because that is the
    stage that samples textures in this engine's passes. That fallback is marked
    as such by the caller through ``confidence``.
    """
    prefix = _VISIBILITY_PREFIX.get(visibility.upper())
    if prefix is not None:
        return prefix
    return "CS" if draw.launches_compute else "PS"


def _register_for_offset(parameter, offset: int) -> tuple[Optional[int], Optional[int]]:
    """Map a descriptor's offset inside a table to its shader register.

    A root descriptor table is a concatenation of ranges. Offsets are counted from
    the table base, so walking the ranges in declaration order and subtracting
    each one's descriptor count locates the range the offset falls in; the
    register is then that range's base plus the remainder.

    Unbounded ranges (``NumDescriptors == UINT_MAX``) absorb the rest of the table,
    which is correct for the bindless arrays UE5 declares.
    """
    if parameter is None or parameter.kind is not RootParameterKind.DESCRIPTOR_TABLE:
        return None, None
    remaining = offset
    for entry in parameter.ranges:
        count = entry.get("count", 0)
        base = entry.get("base_shader_register")
        space = entry.get("register_space")
        if base is None:
            return None, None
        if count in (0xFFFFFFFF, -1):
            return base + remaining, space
        if remaining < count:
            return base + remaining, space
        remaining -= count
    return None, None


def labels_for(
    capture: "Capture", draw: "DrawCall", resource_id: int
) -> list[BindingLabel]:
    """Every binding through which ``draw`` reaches ``resource_id``.

    A single event can reach one resource more than once (bound as an SRV on two
    registers, or as both a render target and an input). All of them are returned,
    most specific first, so a caller can show the primary one and still report the
    rest instead of silently dropping a real binding.
    """
    out: list[BindingLabel] = []

    # -- output merger: render targets and depth ------------------------
    for slot, rid in enumerate(draw.render_target_resource_ids):
        if rid == resource_id:
            out.append(
                BindingLabel(
                    text=f"OM RTV {slot}",
                    category="output_merger",
                    slot=slot,
                    view_kind="RTV",
                )
            )
    if draw.depth_stencil_resource_id == resource_id:
        out.append(
            BindingLabel(text="OM DSV", category="output_merger", view_kind="DSV")
        )

    # -- descriptor tables and root descriptors -------------------------
    signature = (
        capture.root_signatures.get(draw.root_signature_id)
        if draw.root_signature_id is not None
        else None
    )
    for binding in draw.bindings:
        parameter = (
            signature.parameter(binding.root_index) if signature is not None else None
        )
        visibility = parameter.visibility if parameter is not None else ""

        if binding.kind is RootParameterKind.DESCRIPTOR_TABLE or binding.resolved_views:
            for offset, view in enumerate(binding.resolved_views):
                if view.resource_id != resource_id:
                    continue
                register, space = _register_for_offset(parameter, offset)
                stage = _stage_prefix(draw, visibility)
                kind_label = _VIEW_LABEL.get(view.kind, view.kind.value)
                if register is None:
                    # No root signature range covers this offset, so the t#/u#
                    # number is genuinely unknown. Report the stage and kind and
                    # say why the register is missing rather than guessing one.
                    out.append(
                        BindingLabel(
                            text=f"{stage} {kind_label}",
                            category="descriptor_table",
                            stage=stage,
                            view_kind=kind_label,
                            root_index=binding.root_index,
                            heap_id=view.heap_id,
                            heap_index=view.heap_index,
                            confidence="partial",
                            reason=(
                                "The root signature for this action does not declare a "
                                "descriptor range covering this table offset, so the "
                                "shader register cannot be derived."
                            ),
                        )
                    )
                    continue
                out.append(
                    BindingLabel(
                        text=f"{stage} {kind_label} {register}",
                        category="descriptor_table",
                        stage=stage,
                        view_kind=kind_label,
                        shader_register=register,
                        register_space=space,
                        root_index=binding.root_index,
                        heap_id=view.heap_id,
                        heap_index=view.heap_index,
                        confidence=(
                            "exact"
                            if _VISIBILITY_PREFIX.get(visibility.upper())
                            else "stage_inferred"
                        ),
                        reason=(
                            ""
                            if _VISIBILITY_PREFIX.get(visibility.upper())
                            else (
                                "The root parameter is visible to all stages; the stage "
                                "shown is the one this action's pipeline type implies."
                            )
                        ),
                    )
                )
            continue

        # Root descriptors (CBV/SRV/UAV bound straight by GPU virtual address)
        # name their register in the root signature itself.
        if binding.resource_id == resource_id and parameter is not None:
            kind_label = {
                RootParameterKind.CBV: "CBV",
                RootParameterKind.SRV: "SRV",
                RootParameterKind.UAV: "UAV",
            }.get(binding.kind)
            if kind_label is not None:
                stage = _stage_prefix(draw, visibility)
                out.append(
                    BindingLabel(
                        text=f"{stage} {kind_label} {parameter.shader_register}",
                        category="root_descriptor",
                        stage=stage,
                        view_kind=kind_label,
                        shader_register=parameter.shader_register,
                        register_space=parameter.register_space,
                        root_index=binding.root_index,
                    )
                )

    return out


def api_parameter_label(index: int | None, total: int | None = None) -> BindingLabel:
    """The label PIX shows for a resource reached as a raw API argument.

    Barriers, discards and copies take resources positionally, and PIX identifies
    them by that position -- ``API Parameters [1]`` is the second entry of the
    barrier array. ``index`` of None means the position is unknown, which PIX
    itself renders as ``API Parameters [...]``.
    """
    if index is None:
        return BindingLabel(text="API Parameters [...]", category="api_parameter")
    label = BindingLabel(
        text=f"API Parameters [{index}]", category="api_parameter", slot=index
    )
    if total is not None:
        label.reason = f"argument {index} of {total}"
    return label
