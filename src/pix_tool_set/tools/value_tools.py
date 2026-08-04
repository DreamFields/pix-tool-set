"""Read the values of every resource a pass's shader binds, in one call.

This is the "what was this pass actually working with" tool: it walks the draw's
root parameters and descriptor tables, then for each bound resource reports the
captured bytes, decoded where a sensible interpretation exists.

Honesty rules baked in:
  * cbuffers are decoded against the shader's reflected field offsets
  * a resource whose page the frame rewrote is only reported as current when the
    patch blob actually decoded
  * a buffer the GPU produced has no recorded bytes, and says so
"""

from __future__ import annotations

from typing import Any

from ..context import ToolContext
from ..engine import cbvmatch
from ..engine import values as values_mod
from ..engine.model import RootParameterKind
from ..errors import PixToolError, not_found
from ..results import ToolResult
from ._common import PASS_SELECTOR, resolve_pass, tool, with_session

_NOTE = (
    "Values come from resources.bin, which stores what PIX observed being uploaded plus "
    "the per-frame CPU page writes the export records as Map+memcpy. Buffers whose "
    "contents are produced entirely on the GPU have no recorded bytes and are reported "
    "with values_available=false rather than guessed. UAV outputs are therefore usually "
    "empty: they hold whatever preceded the dispatch, not its results."
)

_MAX_ELEMENTS = 16


def _describe_resource(capture, resource_id: int) -> dict[str, Any]:
    resource = capture.resource(resource_id)
    if resource is None:
        return {"resource_id": resource_id, "known": False}
    return {
        "resource_id": resource_id,
        "known": True,
        "description": resource.describe(),
        "kind": str(resource.kind),
        "size_bytes": resource.size_bytes,
        "format": resource.format,
        "is_texture": resource.is_texture,
        "is_uav": resource.is_uav,
    }


def _read_values(
    capture,
    resource_id: int,
    *,
    offset: int,
    max_bytes: int,
    element_type: str | None,
) -> dict[str, Any]:
    """Read and optionally decode one bound resource."""
    out: dict[str, Any] = {"byte_offset": offset}
    page = offset // 4096
    status = capture.resource_page_status(resource_id, page)
    out["page"] = page
    out["page_rewritten_during_frame"] = status["rewritten"]
    if status["rewritten"]:
        out["page_patches_applied"] = status.get("patches_applied", 0)

    try:
        blob = capture.read_resource_bytes(
            resource_id, offset=offset, length=max_bytes
        )
    except PixToolError as exc:
        out["values_available"] = False
        out["detail"] = exc.message
        return out

    if status["rewritten"] and not status["patched"]:
        out["values_available"] = False
        out["values_are_stale"] = True
        out["detail"] = (
            f"page {page} was rewritten from the CPU during the frame but that patch "
            "could not be decoded; bytes shown predate the frame"
        )
    else:
        out["values_available"] = True

    out["bytes_read"] = len(blob)
    out["hexdump"] = values_mod.hexdump(blob, start=offset, limit=min(max_bytes, 128))
    if element_type:
        decoded = values_mod.decode_typed_array(
            blob, element_type, max_elements=_MAX_ELEMENTS
        )
        if decoded:
            out["element_type"] = element_type
            out["elements"] = decoded
    return out


@tool(
    name="pass-values",
    summary=(
        "Read the actual values of the resources a pass binds: cbuffer fields decoded by "
        "name, plus bytes and typed elements for the SRVs and UAVs."
    ),
    category="shaders",
    parameters=with_session(
        PASS_SELECTOR,
        draw_index={"type": "integer", "description": "Address the draw directly."},
        max_bytes={
            "type": "integer",
            "description": "Bytes to read per resource. Default 256.",
        },
        stage={
            "type": "string",
            "description": "Restrict cbuffer decoding to one stage, e.g. PS, VS, CS.",
        },
        cbuffer={
            "type": "string",
            "description": (
                "Only decode the cbuffer with this name, e.g. Scene or View. "
                "Matched case-insensitively."
            ),
        },
        element_type={
            "type": "string",
            "description": (
                "Decode SRV/UAV buffers as this HLSL type, e.g. float4, uint, float. "
                "Omit for hex only."
            ),
        },
        include_views={
            "type": "boolean",
            "description": "Also read resources reached through descriptor tables. Default true.",
        },
        max_views={
            "type": "integer",
            "description": "Cap on descriptor-table resources to read. Default 24.",
        },
    ),
    returns="Per-binding values with an explicit availability flag for each.",
    examples=[
        "pix-tool-set pass-values --queue-id 18385",
        "pix-tool-set pass-values --queue-id 17765 --stage PS --cbuffer Scene",
        "pix-tool-set pass-values --queue-id 18385 --element-type float4",
    ],
    notes=_NOTE,
)
def pass_values(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)

    if args.get("draw_index") is not None:
        draw = capture.draw_call(int(args["draw_index"]))
        entry = None
        if draw is None:
            raise not_found("draw", args["draw_index"])
    else:
        entry = resolve_pass(capture, args)
        draw = capture.draw_call(entry["first_draw_index"])
        if draw is None:
            raise not_found("draw", entry["first_draw_index"])

    max_bytes = int(args.get("max_bytes") or 256)
    element_type = args.get("element_type")
    include_views = args.get("include_views")
    include_views = True if include_views is None else bool(include_views)
    max_views = int(args.get("max_views") or 24)

    # cbuffer field values, keyed by the shader's own names.
    # A graphics draw binds several cbuffers at once, so each layout must be
    # matched to the root parameter that actually supplies it. The root signature
    # declares a shader_register per parameter, and the shader's reflection gives
    # each cbuffer its cbN register, so the two are joined on that number rather
    # than on the order they appear.
    layouts = cbvmatch.collect_cbuffer_layouts(
        draw, stage=args.get("stage"), name=args.get("cbuffer")
    )
    root_info = cbvmatch.root_cbv_registers(capture, draw)

    root_entries: list[dict[str, Any]] = []
    view_entries: list[dict[str, Any]] = []
    available = unavailable = stale = 0

    for binding in draw.bindings:
        kind = binding.kind.value
        if binding.resource_id is not None:
            record = {
                "root_index": binding.root_index,
                "binding_kind": kind,
                **_describe_resource(capture, binding.resource_id),
            }
            values = _read_values(
                capture,
                binding.resource_id,
                offset=binding.va_offset or 0,
                max_bytes=max(max_bytes, 4096 if kind == "root_cbv" else max_bytes),
                element_type=None if kind == "root_cbv" else element_type,
            )
            record["values"] = values
            if kind == "root_cbv" and values.get("bytes_read"):
                matched, known = cbvmatch.layouts_for_root(
                    layouts, root_info, binding.root_index
                )
                info = root_info.get(binding.root_index)
                register = info[0] if info else None
                record["shader_register"] = register
                record["visibility_stage"] = info[1] if info else None
                record["register_matched"] = known
                blob = capture.read_resource_bytes(
                    binding.resource_id,
                    offset=binding.va_offset or 0,
                    length=max(max_bytes, 4096),
                )
                record["cbuffer_fields"] = [
                    {
                        "cbuffer": layout.get("name"),
                        "stage": layout.get("stage"),
                        "shader_register": layout.get("shader_register"),
                        "declared_size": layout.get("size"),
                        "fields": values_mod.decode_layout(blob, layout["fields"]),
                    }
                    for layout in matched
                ]
                if register is None:
                    record["register_note"] = (
                        "root signature did not report a shader register, so every "
                        "cbuffer layout is shown and only one of them is correct"
                    )
            if values.get("values_are_stale"):
                stale += 1
            elif values.get("values_available"):
                available += 1
            else:
                unavailable += 1
            root_entries.append(record)

        if not include_views:
            continue
        for view in binding.resolved_views:
            if view.resource_id is None or len(view_entries) >= max_views:
                continue
            record = {
                "root_index": binding.root_index,
                "binding_kind": view.kind.value if hasattr(view, "kind") else "view",
                **_describe_resource(capture, view.resource_id),
            }
            resource = capture.resource(view.resource_id)
            if resource is not None and resource.is_texture:
                record["values"] = {
                    "values_available": False,
                    "detail": "texture data; use read-texture-pixels or save-render-target",
                }
                unavailable += 1
            else:
                values = _read_values(
                    capture,
                    view.resource_id,
                    offset=0,
                    max_bytes=max_bytes,
                    element_type=element_type,
                )
                record["values"] = values
                if values.get("values_are_stale"):
                    stale += 1
                elif values.get("values_available"):
                    available += 1
                else:
                    unavailable += 1
            view_entries.append(record)

    data = {
        "draw_index": draw.index,
        "global_id": draw.global_id,
        "pass_name": draw.pass_name,
        "pass_index": entry["pass_index"] if entry else None,
        "queue_id": entry.get("first_queue_id") if entry else None,
        "pso_id": draw.pso_id,
        "stages": [shader.stage.value for shader in draw.shaders],
        "root_bindings": root_entries,
        "descriptor_table_resources": view_entries,
        "summary": {
            "values_available": available,
            "values_stale": stale,
            "values_unavailable": unavailable,
        },
    }

    if stale or unavailable:
        result = ToolResult.partial(data)
        result.degrade(
            f"{available} binding(s) have trustworthy values; "
            f"{stale} are stale and {unavailable} have no captured bytes.",
            reason=(
                "PIX records uploads and CPU writes, not GPU-produced contents, so UAV "
                "outputs and GPU-filled buffers have nothing to read."
            ),
        )
        return result
    return ToolResult.success(data)
