"""Requirement section 9: data export (buffers, meshes, render targets)."""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

from ..context import ToolContext
from ..engine.model import ShaderStage
from ..errors import PixToolError, invalid_argument, not_found
from ..results import ToolResult
from ._common import DRAW_SELECTOR, tool, with_session

_FORMAT_DECODERS: dict[str, tuple[str, int, int]] = {
    # name -> (struct code per component, component count, bytes)
    "R32G32B32A32_FLOAT": ("f", 4, 16),
    "R32G32B32_FLOAT": ("f", 3, 12),
    "R32G32_FLOAT": ("f", 2, 8),
    "R32_FLOAT": ("f", 1, 4),
    "R32G32B32A32_UINT": ("I", 4, 16),
    "R32G32B32_UINT": ("I", 3, 12),
    "R32G32_UINT": ("I", 2, 8),
    "R32_UINT": ("I", 1, 4),
    "R32G32B32A32_SINT": ("i", 4, 16),
    "R32_SINT": ("i", 1, 4),
    "R16G16B16A16_FLOAT": ("e", 4, 8),
    "R16G16_FLOAT": ("e", 2, 4),
    "R16_FLOAT": ("e", 1, 2),
    "R16G16B16A16_UINT": ("H", 4, 8),
    "R16G16_UINT": ("H", 2, 4),
    "R16_UINT": ("H", 1, 2),
    "R8G8B8A8_UNORM": ("B", 4, 4),
    "B8G8R8A8_UNORM": ("B", 4, 4),
    "R8G8_UNORM": ("B", 2, 2),
    "R8_UNORM": ("B", 1, 1),
}

_BUFFER_NOTE = (
    "Buffer bytes come from resources.bin, which holds the uploads PIX captured. Buffers "
    "that the GPU generated at replay time (UAV output, indirect args written by a compute "
    "pass) have no captured bytes; the tool reports that explicitly instead of guessing."
)


def _decode_format(fmt: str, raw: bytes, offset: int) -> list[float] | None:
    name = fmt.replace("DXGI_FORMAT_", "").upper()
    spec = _FORMAT_DECODERS.get(name)
    if spec is None:
        return None
    code, count, size = spec
    if offset + size > len(raw):
        return None
    values = list(struct.unpack_from("<" + code * count, raw, offset))
    if name.endswith("_UNORM"):
        values = [value / 255.0 for value in values]
    return [round(float(value), 6) for value in values]


def _resource_blob(capture, resource_id: int) -> bytes | None:
    """Fetch a resource's captured contents, CPU page writes included."""
    try:
        return capture.read_resource_bytes(resource_id)
    except PixToolError:
        return None


@tool(
    name="read-buffer",
    summary=(
        "Read raw bytes from a buffer resource and optionally decode them as a typed array "
        "(float/uint/index data)."
    ),
    category="export",
    parameters=with_session(
        resource_id={"type": "integer", "description": "Buffer resource id."},
        offset_bytes={"type": "integer", "description": "Start offset. Default 0."},
        length_bytes={"type": "integer", "description": "How many bytes to read. Default 256."},
        format={
            "type": "string",
            "description": "Decode as this DXGI format, e.g. R32G32B32_FLOAT.",
        },
        stride={"type": "integer", "description": "Element stride when decoding. Defaults to format size."},
        output={"type": "string", "description": "Write the raw bytes to this file."},
        required=["resource_id"],
    ),
    returns="Hex bytes, optional decoded elements, and the resource descriptor.",
    examples=["pix-tool-set read-buffer --resource-id 33 --length-bytes 128 --format R32G32B32_FLOAT"],
    notes=_BUFFER_NOTE,
)
def read_buffer(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    resource_id = int(args["resource_id"])
    resource = capture.resource(resource_id)
    if resource is None:
        raise not_found("resource", resource_id, "Run list-buffers to find valid ids.")

    offset = int(args.get("offset_bytes") or 0)
    length = int(args.get("length_bytes") or 256)
    blob = _resource_blob(capture, resource_id)

    data: dict[str, Any] = {
        "resource": resource.to_dict(),
        "requested": {"offset_bytes": offset, "length_bytes": length},
        "bytes_available": blob is not None,
    }

    if blob is None:
        result = ToolResult.partial(data)
        result.degrade(
            "No captured bytes for this buffer in resources.bin.",
            reason=(
                "PIX stores an upload blob only for resources it initialised at capture time. "
                "GPU-generated buffer contents are not part of a C++ export."
            ),
            alternative="Use save-render-target for image data, or inspect the buffer in the PIX UI.",
        )
        return result

    window = blob[offset : offset + length]
    data["hex"] = window.hex(" ")
    data["length_returned"] = len(window)

    fmt = args.get("format")
    if fmt:
        stride = int(args.get("stride") or 0)
        name = fmt.replace("DXGI_FORMAT_", "").upper()
        spec = _FORMAT_DECODERS.get(name)
        if spec is None:
            raise invalid_argument(
                "format", f"{fmt} is not a supported decode format ({sorted(_FORMAT_DECODERS)})"
            )
        step = stride or spec[2]
        elements = []
        cursor = 0
        while cursor + spec[2] <= len(window):
            decoded = _decode_format(fmt, window, cursor)
            if decoded is None:
                break
            elements.append(decoded)
            cursor += step
        data["format"] = fmt
        data["stride"] = step
        data["elements"] = elements

    output_paths: list[str] = []
    if args.get("output"):
        path = context.resolve_output(args.get("output"), f"buffer_{resource_id}.bin")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(window)
        output_paths.append(str(path))

    return ToolResult.success(data, output_paths=output_paths)


@tool(
    name="export-mesh",
    summary=(
        "Export the geometry of a draw call as OBJ or JSON. Uses the bound vertex and index "
        "buffers plus the PSO input layout to describe the mesh."
    ),
    category="export",
    parameters=with_session(
        DRAW_SELECTOR,
        output={"type": "string", "description": "Output file path (.obj or .json)."},
        format={
            "type": "string",
            "enum": ["obj", "json"],
            "description": "Output format. Default json.",
        },
        required=[],
    ),
    returns="Path of the written file plus the mesh description that was exported.",
    examples=["pix-tool-set export-mesh --draw-index 2461 -o mesh.json"],
    notes=(
        "Vertex positions can only be written when the vertex buffer bytes were captured. "
        "When they are absent the export still contains the complete mesh description "
        "(layout, strides, counts, buffer ids) as JSON so the caller can locate the data."
    ),
)
def export_mesh(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    draw = capture.resolve_draw(
        draw_index=args.get("draw_index"),
        global_id=args.get("global_id"),
        queue_id=args.get("queue_id"),
    )
    if draw is None:
        raise not_found("draw call", args.get("draw_index") or args.get("global_id"))

    pso = draw.pipeline_state
    layout = pso.input_layout if pso else []
    shader = draw.shader(ShaderStage.VS)

    description: dict[str, Any] = {
        "draw_index": draw.index,
        "global_id": draw.global_id,
        "pass_name": draw.pass_name,
        "primitive_topology": draw.primitive_topology,
        "counts": {
            "vertex_or_index_count": draw.vertex_or_index_count,
            "instance_count": draw.instance_count,
            "triangle_count": draw.triangle_count,
            "start_index": draw.start_index,
            "base_vertex": draw.base_vertex,
        },
        "input_layout": layout,
        "vertex_buffers": [
            {
                **vertex.to_dict(),
                "resource": (
                    capture.resource(vertex.resource_id).to_dict()
                    if vertex.resource_id is not None
                    and capture.resource(vertex.resource_id) is not None
                    else None
                ),
            }
            for vertex in draw.vertex_buffers
        ],
        "index_buffer": draw.index_buffer.to_dict() if draw.index_buffer else None,
        "vertex_shader_input_signature": (
            [element.to_dict() for element in shader.input_signature] if shader else []
        ),
        "positions_available": False,
    }

    fmt = (args.get("format") or "json").lower()
    default_name = f"draw_{draw.index}_mesh.{'obj' if fmt == 'obj' else 'json'}"
    path = context.resolve_output(args.get("output"), default_name)
    path.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "obj":
        lines = [
            f"# pix-tool-set mesh export for draw {draw.index}",
            f"# pass: {draw.pass_name}",
            f"# triangles: {draw.triangle_count}  instances: {draw.instance_count}",
            "# vertex positions were not captured in this export; see the JSON sidecar",
            f"o draw_{draw.index}",
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        sidecar = path.with_suffix(".json")
        sidecar.write_text(
            json.dumps(description, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        outputs = [str(path), str(sidecar)]
    else:
        path.write_text(
            json.dumps(description, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        outputs = [str(path)]

    result = ToolResult.partial(
        {"mesh": description, "format": fmt, "path": str(path)}, output_paths=outputs
    )
    result.degrade(
        "Mesh description exported without vertex positions.",
        reason="Vertex buffer bytes are not present in this C++ export.",
        alternative="Open the draw in the PIX UI mesh viewer for transformed vertex values.",
    )
    return result


@tool(
    name="save-render-target",
    summary=(
        "Save a render target or depth buffer of a draw call to an image file through "
        "pixtool. This is the reliable way to get pixel data out of a capture."
    ),
    category="export",
    parameters=with_session(
        DRAW_SELECTOR,
        output={"type": "string", "description": "Output image path. Extension picks the format."},
        rtv={"type": "integer", "description": "Render target slot index. Default 0."},
        depth={"type": "boolean", "description": "Save the depth buffer instead."},
        marker={"type": "string", "description": "Use the last draw under this PIX marker."},
    ),
    returns="Written file path and which resource it corresponds to.",
    examples=[
        "pix-tool-set save-render-target --draw-index 2461 -o rt0.png",
        "pix-tool-set save-render-target --global-id 3644 --depth -o depth.png",
    ],
)
def save_render_target(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    record = context.session(args)
    if not record.capture_path:
        raise PixToolError(
            code="capture_required",
            message="Saving a render target needs the original .wpix file.",
            stage="export",
            suggestion="Re-open the session with --capture pointing at the .wpix file.",
        )

    marker = args.get("marker")
    draw = capture.resolve_draw(
        draw_index=args.get("draw_index"),
        global_id=args.get("global_id"),
        queue_id=args.get("queue_id"),
    )
    if draw is None and marker is None:
        raise invalid_argument(
            "draw_index/global_id/marker", "provide one way to select the event"
        )

    rtv = int(args.get("rtv") or 0)
    depth = bool(args.get("depth"))
    stem = (
        f"draw{draw.index}_{'depth' if depth else f'rtv{rtv}'}"
        if draw is not None
        else f"marker_{'depth' if depth else f'rtv{rtv}'}"
    )
    path = context.resolve_output(args.get("output"), f"{stem}.png")
    pixtool = context.require_pixtool(args)
    pixtool.save_resource(
        Path(record.capture_path),
        path,
        global_id=draw.global_id if draw is not None else None,
        marker=marker,
        rtv=None if depth else rtv,
        depth=depth,
    )

    data: dict[str, Any] = {
        "path": str(path),
        "bytes": path.stat().st_size,
        "depth": depth,
        "rtv": None if depth else rtv,
    }
    if draw is not None:
        data["draw_index"] = draw.index
        data["global_id"] = draw.global_id
        data["pass_name"] = draw.pass_name
        resource_id = (
            draw.depth_stencil_resource_id
            if depth
            else (
                draw.render_target_resource_ids[rtv]
                if rtv < len(draw.render_target_resource_ids)
                else None
            )
        )
        if resource_id is not None:
            resource = capture.resource(resource_id)
            data["resource"] = resource.to_dict() if resource else {"resource_id": resource_id}
    return ToolResult.success(data, output_paths=[str(path)])


@tool(
    name="export-report",
    summary=(
        "Write a full JSON report of the capture: statistics, passes, draw calls, resources "
        "and shaders. Useful for handing the whole frame to another tool at once."
    ),
    category="export",
    parameters=with_session(
        output={"type": "string", "description": "Output .json path."},
        max_draws={"type": "integer", "description": "Cap on included draw calls. Default 500."},
        include_bindings={"type": "boolean", "description": "Include per-draw bindings (large)."},
    ),
    returns="Path and size of the written report.",
    examples=["pix-tool-set export-report -o frame.json --max-draws 200"],
)
def export_report(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    max_draws = int(args.get("max_draws") or 500)
    detail = bool(args.get("include_bindings"))

    document = {
        "statistics": capture.frame_statistics(),
        "passes": capture.passes,
        "draw_calls": [
            draw.to_dict(detail=detail, max_views=6)
            for draw in capture.draw_calls[:max_draws]
        ],
        "resources": [resource.to_dict() for resource in capture.resources.values()],
        "pipeline_states": [
            pso.to_dict() for pso in capture.pipeline_states.values()
        ],
        "shaders": [shader.to_dict() for shader in capture.shaders],
    }
    path = context.resolve_output(args.get("output"), "capture-report.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")

    result = ToolResult.success(
        {
            "path": str(path),
            "bytes": path.stat().st_size,
            "draw_calls_included": len(document["draw_calls"]),
            "draw_calls_total": len(capture.draw_calls),
        },
        output_paths=[str(path)],
    )
    if len(capture.draw_calls) > max_draws:
        result.add_diagnostic(
            "info",
            f"Only the first {max_draws} draw calls were included; raise --max-draws for more.",
        )
    return result
