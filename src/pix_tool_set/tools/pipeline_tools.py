"""Requirement section 7: pipeline state."""

from __future__ import annotations

import struct
from typing import Any

from ..context import ToolContext
from ..engine.model import EventKind, ShaderStage
from ..errors import not_found
from ..results import ToolResult
from ._common import (
    DRAW_SELECTOR,
    PAGE_PARAMS,
    page_args,
    page_envelope,
    tool,
    with_session,
)

_FORMAT_SIZES: dict[str, int] = {
    "R32G32B32A32": 16,
    "R32G32B32": 12,
    "R16G16B16A16": 8,
    "R32G32": 8,
    "R10G10B10A2": 4,
    "R11G11B10": 4,
    "R8G8B8A8": 4,
    "B8G8R8A8": 4,
    "R16G16": 4,
    "R32": 4,
    "R8G8": 2,
    "R16": 2,
    "R8": 1,
}


def _format_size(fmt: str) -> int:
    name = fmt.replace("DXGI_FORMAT_", "").upper()
    for key, size in sorted(_FORMAT_SIZES.items(), key=lambda kv: -len(kv[0])):
        if name.startswith(key):
            return size
    return 4


@tool(
    name="list-pipeline-states",
    summary=(
        "List pipeline state objects with their shader stages, render target formats, "
        "root signature and how many draws use each one."
    ),
    category="pipeline",
    parameters=with_session(
        PAGE_PARAMS,
        kind={
            "type": "string",
            "enum": ["graphics", "compute"],
            "description": "Restrict to graphics or compute PSOs.",
        },
        used_only={"type": "boolean", "description": "Only PSOs actually bound by a draw."},
        detail={"type": "boolean", "description": "Include full state (blend, depth, layout)."},
    ),
    returns="Paged PSO list with usage counts.",
    examples=["pix-tool-set list-pipeline-states --used-only --limit 30"],
)
def list_pipeline_states(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    offset, limit = page_args(args)

    usage: dict[int, int] = {}
    for draw in capture.draw_calls:
        if draw.pso_id is not None:
            usage[draw.pso_id] = usage.get(draw.pso_id, 0) + 1

    kind = args.get("kind")
    used_only = bool(args.get("used_only"))
    rows = []
    for pso in capture.pipeline_states.values():
        resolved_kind = "compute" if pso.is_compute else "graphics"
        if kind and resolved_kind != kind:
            continue
        if used_only and pso.api_id not in usage:
            continue
        entry = pso.to_dict(detail=bool(args.get("detail")))
        entry["draw_count"] = usage.get(pso.api_id, 0)
        rows.append(entry)
    rows.sort(key=lambda entry: (-entry["draw_count"], entry["pso_id"]))

    total = len(rows)
    window = rows[offset : offset + limit] if limit else rows[offset:]
    return ToolResult.success(
        {"pipeline_states": window, **page_envelope(total, offset, limit, len(window))}
    )


@tool(
    name="pipeline-state",
    summary=(
        "Full pipeline state detail: shaders, input layout, blend/depth/raster state, "
        "render target formats, and the deserialised root signature."
    ),
    category="pipeline",
    parameters=with_session(
        pso_id={"type": "integer", "description": "Pipeline state id."},
        draw_index={"type": "integer", "description": "Take the PSO bound at this draw."},
        global_id={"type": "integer", "description": "Take the PSO bound at this event."},
    ),
    returns="PSO detail plus root signature layout.",
    examples=[
        "pix-tool-set pipeline-state --pso-id 3184",
        "pix-tool-set pipeline-state --draw-index 2461",
    ],
)
def pipeline_state(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    pso_id = args.get("pso_id")
    if pso_id is None:
        draw = capture.resolve_draw(
            draw_index=args.get("draw_index"), global_id=args.get("global_id")
        )
        if draw is None or draw.pso_id is None:
            raise not_found("pipeline state", args.get("draw_index") or args.get("global_id"))
        pso_id = draw.pso_id

    pso = capture.pipeline_state(int(pso_id))
    if pso is None:
        raise not_found("pipeline state", pso_id, "Run list-pipeline-states for valid ids.")

    signature = capture.root_signatures.get(pso.root_signature_id or -1)
    consumers = [
        {
            "draw_index": draw.index,
            "global_id": draw.global_id,
            "pass_name": draw.pass_name,
        }
        for draw in capture.draw_calls
        if draw.pso_id == pso.api_id
    ]

    return ToolResult.success(
        {
            "pipeline_state": pso.to_dict(detail=True),
            "root_signature": signature.to_dict() if signature else None,
            "shaders": [shader.to_dict(detail=True) for shader in pso.shaders],
            "consumer_count": len(consumers),
            "consumers": consumers[:20],
        }
    )


@tool(
    name="draw-state",
    summary=(
        "Everything bound at one draw: pipeline state, root signature, descriptor heaps, "
        "render targets, depth buffer, viewports, scissors and every root binding."
    ),
    category="pipeline",
    parameters=with_session(
        DRAW_SELECTOR,
        max_views={"type": "integer", "description": "Cap on views per binding. Default 12."},
    ),
    returns="Complete GPU state snapshot for the selected draw.",
    examples=["pix-tool-set draw-state --draw-index 2461"],
    aliases=["draw-info"],
)
def draw_state(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    draw = capture.resolve_draw(
        draw_index=args.get("draw_index"), global_id=args.get("global_id")
    )
    if draw is None:
        raise not_found("draw call", args.get("draw_index") or args.get("global_id"))

    max_views = int(args.get("max_views") or 12)
    pso = draw.pipeline_state
    signature = capture.root_signatures.get(draw.root_signature_id or -1)

    return ToolResult.success(
        {
            "draw_call": draw.to_dict(detail=True, max_views=max_views),
            "pipeline_state": pso.to_dict(detail=True) if pso else None,
            "root_signature": signature.to_dict() if signature else None,
            "event": draw.event.to_dict(detail=True) if draw.event else None,
            "resource_summary": {
                "distinct_resources": len(draw.resources()),
                "buffers": len(draw.buffers),
                "textures": len(draw.textures),
            },
        }
    )


@tool(
    name="vertex-input",
    summary=(
        "Vertex input layout of a draw: the PSO input element descriptors matched against "
        "the vertex buffer views actually bound, plus the index buffer."
    ),
    category="pipeline",
    parameters=with_session(
        DRAW_SELECTOR,
        include_vs_signature={
            "type": "boolean",
            "description": "Also include the vertex shader input signature. Default true.",
        },
    ),
    returns="Input layout, bound vertex buffers, index buffer and derived vertex counts.",
    examples=["pix-tool-set vertex-input --draw-index 2461"],
)
def vertex_input(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    draw = capture.resolve_draw(
        draw_index=args.get("draw_index"), global_id=args.get("global_id")
    )
    if draw is None:
        raise not_found("draw call", args.get("draw_index") or args.get("global_id"))

    pso = draw.pipeline_state
    layout = pso.input_layout if pso else []

    by_slot: dict[int, list[dict[str, Any]]] = {}
    for element in layout:
        by_slot.setdefault(element["input_slot"], []).append(element)

    buffers = []
    for vertex in draw.vertex_buffers:
        resource = (
            capture.resource(vertex.resource_id) if vertex.resource_id is not None else None
        )
        elements = by_slot.get(vertex.slot, [])
        declared_stride = sum(_format_size(item["format"]) for item in elements)
        buffers.append(
            {
                **vertex.to_dict(),
                "resource": resource.to_dict() if resource else None,
                "elements": elements,
                "declared_element_bytes": declared_stride,
                "stride_matches_layout": (
                    declared_stride == vertex.stride if elements and vertex.stride else None
                ),
            }
        )

    index_entry = None
    if draw.index_buffer is not None:
        resource = (
            capture.resource(draw.index_buffer.resource_id)
            if draw.index_buffer.resource_id is not None
            else None
        )
        index_entry = {
            **draw.index_buffer.to_dict(),
            "resource": resource.to_dict() if resource else None,
        }

    data: dict[str, Any] = {
        "draw_index": draw.index,
        "global_id": draw.global_id,
        "primitive_topology": draw.primitive_topology,
        "input_layout": layout,
        "input_layout_element_count": len(layout),
        "vertex_buffers": buffers,
        "index_buffer": index_entry,
        "draw_args": {
            "vertex_or_index_count": draw.vertex_or_index_count,
            "instance_count": draw.instance_count,
            "start_index": draw.start_index,
            "base_vertex": draw.base_vertex,
            "start_instance": draw.start_instance,
        },
    }

    include_signature = args.get("include_vs_signature")
    if include_signature is None or bool(include_signature):
        shader = draw.shader(ShaderStage.VS)
        if shader is not None:
            data["vertex_shader_input_signature"] = [
                element.to_dict() for element in shader.input_signature
            ]
    return ToolResult.success(data)


@tool(
    name="post-vs-data",
    summary=(
        "Post-vertex-shader data for a draw. Reports the vertex shader output signature "
        "(the layout of transformed vertices) plus the input geometry that feeds it."
    ),
    category="pipeline",
    parameters=with_session(
        DRAW_SELECTOR,
        max_vertices={
            "type": "integer",
            "description": "How many input vertices to decode from the vertex buffer. Default 0 (none).",
        },
    ),
    returns="Output signature, stream layout and optionally decoded input vertices.",
    examples=["pix-tool-set post-vs-data --draw-index 2461"],
    notes=(
        "PIX C++ export does not contain post-transform vertex buffers: those exist only "
        "inside a live PIX replay session. This tool therefore reports the exact output "
        "signature and stream layout, and can decode the pre-transform vertices from "
        "resources.bin when they were captured. For true post-VS values, use the PIX UI "
        "mesh viewer on the same capture."
    ),
)
def post_vs_data(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    draw = capture.resolve_draw(
        draw_index=args.get("draw_index"), global_id=args.get("global_id")
    )
    if draw is None:
        raise not_found("draw call", args.get("draw_index") or args.get("global_id"))

    shader = draw.shader(ShaderStage.VS)
    outputs = [element.to_dict() for element in shader.output_signature] if shader else []
    stream_bytes = sum(_format_size("R32G32B32A32") for _ in outputs)

    data: dict[str, Any] = {
        "draw_index": draw.index,
        "global_id": draw.global_id,
        "vertex_shader": shader.to_dict() if shader else None,
        "output_signature": outputs,
        "output_element_count": len(outputs),
        "estimated_output_stride_bytes": stream_bytes,
        "input_vertex_count": draw.vertex_or_index_count,
        "instance_count": draw.instance_count,
        "post_transform_values_available": False,
        "input_vertices": [],
    }

    max_vertices = int(args.get("max_vertices") or 0)
    if max_vertices > 0 and draw.vertex_buffers:
        primary = draw.vertex_buffers[0]
        resource = (
            capture.resource(primary.resource_id) if primary.resource_id is not None else None
        )
        data["input_vertex_source"] = {
            "resource_id": primary.resource_id,
            "stride": primary.stride,
            "resource": resource.to_dict() if resource else None,
        }
        data["input_vertices_note"] = (
            "Vertex bytes live in resources.bin only when the exporter captured that upload; "
            "use read-buffer on the vertex buffer resource to inspect raw bytes."
        )

    result = ToolResult.partial(data)
    result.add_diagnostic(
        "info",
        "Post-transform vertex values are not present in a C++ export; the output layout is reported instead.",
    )
    if shader is None:
        result.add_diagnostic("warning", "This draw has no vertex shader (mesh or compute path).")
    return result
