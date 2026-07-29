"""Requirement section 10: advanced analysis."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..context import ToolContext
from ..engine.model import EventKind, ShaderStage, ViewKind
from ..errors import PixToolError, invalid_argument, not_found
from ..results import ToolResult
from ._common import DRAW_SELECTOR, tool, with_session
from .texture_tools import read_png

_REPLAY_NOTE = (
    "True per-pixel replay (which fragments were shaded, what each one returned) requires a "
    "live PIX replay session. This toolkit works from a C++ export, so it answers the same "
    "questions statically: it finds every draw whose viewport/scissor covers the pixel and "
    "whose render target matches, in submission order. That is the candidate set PIX would "
    "then execute."
)


def _covers_pixel(draw, x: int, y: int) -> bool:
    if not draw.viewports and not draw.scissor_rects:
        return True
    for viewport in draw.viewports:
        left = viewport.get("top_left_x", 0.0)
        top = viewport.get("top_left_y", 0.0)
        width = viewport.get("width", 0.0)
        height = viewport.get("height", 0.0)
        if left <= x < left + width and top <= y < top + height:
            break
    else:
        if draw.viewports:
            return False
    for scissor in draw.scissor_rects:
        if (
            scissor.get("left", 0) <= x < scissor.get("right", 0)
            and scissor.get("top", 0) <= y < scissor.get("bottom", 0)
        ):
            return True
    return not draw.scissor_rects


@tool(
    name="pixel-history",
    summary=(
        "Which draws could have written a given pixel of a render target, in submission "
        "order, with the shader and blend state each one used."
    ),
    category="advanced",
    parameters=with_session(
        x={"type": "integer", "description": "Pixel X coordinate."},
        y={"type": "integer", "description": "Pixel Y coordinate."},
        resource_id={"type": "integer", "description": "Render target resource id."},
        max_events={"type": "integer", "description": "Cap on returned candidates. Default 50."},
        include_final_value={
            "type": "boolean",
            "description": "Also export the target and read the pixel's final value.",
        },
        required=["x", "y"],
    ),
    returns="Ordered candidate writer list, blend state, and optionally the final pixel value.",
    examples=["pix-tool-set pixel-history --resource-id 641 --x 960 --y 540"],
    notes=_REPLAY_NOTE,
)
def pixel_history(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    x = int(args["x"])
    y = int(args["y"])
    resource_id = args.get("resource_id")

    candidates = []
    for draw in capture.draw_calls:
        if draw.kind not in (EventKind.DRAW, EventKind.EXECUTE_INDIRECT):
            continue
        targets = draw.render_target_resource_ids
        if resource_id is not None and int(resource_id) not in targets:
            continue
        if not targets:
            continue
        if not _covers_pixel(draw, x, y):
            continue
        pso = draw.pipeline_state
        candidates.append(
            {
                "draw_index": draw.index,
                "global_id": draw.global_id,
                "api": draw.api,
                "pass_name": draw.pass_name,
                "render_target_ids": targets,
                "slot_of_target": (
                    targets.index(int(resource_id)) if resource_id is not None else 0
                ),
                "pixel_shader": (
                    draw.shader(ShaderStage.PS).to_dict()
                    if draw.shader(ShaderStage.PS)
                    else None
                ),
                "blend_enabled": pso.blend_enabled if pso else None,
                "depth_enabled": pso.depth_enabled if pso else None,
                "depth_write": pso.depth_write if pso else None,
                "depth_func": pso.depth_func if pso else None,
                "cull_mode": pso.cull_mode if pso else None,
                "viewports": draw.viewports,
                "scissor_rects": draw.scissor_rects,
            }
        )

    max_events = int(args.get("max_events") or 50)
    truncated = len(candidates) > max_events

    data: dict[str, Any] = {
        "coordinate": {"x": x, "y": y},
        "resource_id": resource_id,
        "candidate_count": len(candidates),
        "candidates": candidates[:max_events],
        "truncated": truncated,
        "method": "static-coverage-analysis",
    }

    if resource_id is not None:
        resource = capture.resource(int(resource_id))
        if resource is not None:
            data["render_target"] = resource.to_dict()

    output_paths: list[str] = []
    if bool(args.get("include_final_value")) and resource_id is not None:
        record = context.session(args)
        if record.capture_path and candidates:
            try:
                pixtool = context.require_pixtool(args)
                path = context.resolve_output(None, f"pixelhistory_{resource_id}.png")
                last = candidates[-1]
                pixtool.save_resource(
                    Path(record.capture_path),
                    path,
                    global_id=last["global_id"],
                    rtv=last["slot_of_target"],
                )
                image = read_png(path)
                data["final_value"] = {
                    "channels": [round(value, 6) for value in image.pixel(x, y)],
                    "from_draw_index": last["draw_index"],
                    "image_path": str(path),
                }
                output_paths.append(str(path))
            except PixToolError as exc:
                data["final_value_error"] = exc.to_dict()

    result = ToolResult.success(data, output_paths=output_paths)
    result.add_diagnostic(
        "info",
        "Candidates are derived from viewport/scissor coverage and render-target binding, not from replay.",
    )
    if truncated:
        result.add_diagnostic(
            "info", f"Showing {max_events} of {len(candidates)} candidates; raise --max-events."
        )
    return result


@tool(
    name="analyze-pass",
    summary=(
        "Deep analysis of one render pass: workload, resource flow in and out, shader mix, "
        "state changes, and observations about likely inefficiencies."
    ),
    category="advanced",
    parameters=with_session(
        pass_name={"type": "string", "description": "Pass name (substring match)."},
        pass_index={"type": "integer", "description": "Pass index from list-passes."},
    ),
    returns="Pass workload, inputs/outputs, shader mix and observations.",
    examples=['pix-tool-set analyze-pass --pass-name "ShadowDepths"'],
)
def analyze_pass(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    key = args.get("pass_index")
    if key is None:
        key = args.get("pass_name")
    if key is None:
        raise invalid_argument("pass_index/pass_name", "provide one of them")
    entry = capture.find_pass(key)
    if entry is None:
        raise not_found("pass", key, "Run list-passes to see valid names and indices.")

    marker_path = tuple(entry["marker_path"])
    draws = [d for d in capture.draw_calls if d.marker_path == marker_path]

    inputs: dict[int, int] = {}
    outputs: dict[int, int] = {}
    for draw in draws:
        for view in draw.srvs:
            if view.resource_id is not None:
                inputs[view.resource_id] = inputs.get(view.resource_id, 0) + 1
        for view in draw.uavs:
            if view.resource_id is not None:
                outputs[view.resource_id] = outputs.get(view.resource_id, 0) + 1
        for rid in draw.render_target_resource_ids:
            outputs[rid] = outputs.get(rid, 0) + 1

    def describe(ids: dict[int, int], limit: int = 15) -> list[dict[str, Any]]:
        rows = []
        for rid, count in sorted(ids.items(), key=lambda kv: -kv[1])[:limit]:
            resource = capture.resource(rid)
            rows.append(
                {
                    "resource_id": rid,
                    "bind_count": count,
                    "description": resource.describe() if resource else None,
                    "size_bytes": resource.size_bytes if resource else 0,
                }
            )
        return rows

    pso_sequence = [draw.pso_id for draw in draws if draw.pso_id is not None]
    pso_switches = sum(
        1 for i in range(1, len(pso_sequence)) if pso_sequence[i] != pso_sequence[i - 1]
    )
    shader_mix: dict[str, int] = {}
    for draw in draws:
        for shader in draw.shaders:
            shader_mix[shader.stage.value] = shader_mix.get(shader.stage.value, 0) + 1

    observations: list[dict[str, Any]] = []
    if draws and pso_switches > len(draws) * 0.7:
        observations.append(
            {
                "severity": "warning",
                "topic": "state_churn",
                "message": (
                    f"{pso_switches} pipeline state switches across {len(draws)} events; "
                    "batching by material could cut overhead."
                ),
            }
        )
    small_draws = [d for d in draws if d.kind is EventKind.DRAW and 0 < d.triangle_count < 50]
    if len(small_draws) > 20:
        observations.append(
            {
                "severity": "warning",
                "topic": "small_draws",
                "message": (
                    f"{len(small_draws)} draws submit fewer than 50 triangles each; "
                    "instancing or merging would reduce per-draw cost."
                ),
            }
        )
    shared = set(inputs) & set(outputs)
    if shared:
        observations.append(
            {
                "severity": "info",
                "topic": "read_write_same_resource",
                "message": f"{len(shared)} resource(s) are both read and written in this pass.",
                "resource_ids": sorted(shared)[:10],
            }
        )
    heavy = [d for d in draws if d.thread_count > 1_000_000]
    if heavy:
        observations.append(
            {
                "severity": "info",
                "topic": "heavy_dispatch",
                "message": f"{len(heavy)} dispatch(es) launch more than 1M threads.",
                "draw_indices": [d.index for d in heavy][:10],
            }
        )

    return ToolResult.success(
        {
            "pass": entry,
            "workload": {
                "events": len(draws),
                "triangles": entry["triangle_count"],
                "compute_threads": entry["thread_count"],
                "pipeline_state_switches": pso_switches,
                "distinct_pipeline_states": len(set(pso_sequence)),
                "avg_triangles_per_draw": round(
                    entry["triangle_count"] / max(entry["draw_count"], 1), 2
                ),
            },
            "inputs": describe(inputs),
            "outputs": describe(outputs),
            "shader_mix": shader_mix,
            "observations": observations,
        }
    )


@tool(
    name="sample-pixel-region",
    summary=(
        "Sample a rectangular region of a render target and report per-channel statistics "
        "plus a coarse histogram, useful for spotting flat, blown-out or noisy areas."
    ),
    category="advanced",
    parameters=with_session(
        x={"type": "integer", "description": "Region left edge."},
        y={"type": "integer", "description": "Region top edge."},
        width={"type": "integer", "description": "Region width. Default 32."},
        height={"type": "integer", "description": "Region height. Default 32."},
        resource_id={"type": "integer", "description": "Render target resource id."},
        global_id={"type": "integer", "description": "Event whose contents to sample."},
        depth={"type": "boolean", "description": "Sample the depth buffer."},
        rtv={"type": "integer", "description": "Render target slot. Default 0."},
        bins={"type": "integer", "description": "Histogram bin count. Default 8."},
        output={"type": "string", "description": "Where to keep the intermediate PNG."},
        required=["x", "y"],
    ),
    returns="Per-channel statistics and histogram for the sampled region.",
    examples=[
        "pix-tool-set sample-pixel-region --resource-id 641 --x 400 --y 300 --width 64 --height 64"
    ],
)
def sample_pixel_region(args: dict[str, Any], context: ToolContext) -> ToolResult:
    from .texture_tools import _channel_statistics, _texture_export

    capture = context.capture(args)
    resource_id = args.get("resource_id")
    global_id = args.get("global_id")
    if resource_id is None and global_id is None:
        raise invalid_argument("resource_id/global_id", "provide at least one")

    path, diagnostics = _texture_export(
        context,
        args,
        capture,
        resource_id=int(resource_id) if resource_id is not None else None,
        global_id=int(global_id) if global_id is not None else None,
        rtv=int(args.get("rtv") or 0),
        depth=bool(args.get("depth")),
        stem=f"region_{resource_id if resource_id is not None else global_id}",
    )
    try:
        image = read_png(path)
    except PixToolError as exc:
        return ToolResult.partial(
            {"image_path": str(path), "decode_error": exc.to_dict()},
            output_paths=[str(path)],
            diagnostics=diagnostics,
        ).add_diagnostic("warning", "Image exported but not decodable in-process.")

    x = int(args["x"])
    y = int(args["y"])
    width = int(args.get("width") or 32)
    height = int(args.get("height") or 32)
    box = (x, y, x + width, y + height)
    stats = _channel_statistics(image, box)

    bin_count = max(int(args.get("bins") or 8), 1)
    histograms: list[dict[str, Any]] = []
    if stats.get("samples"):
        per_channel = [[0] * bin_count for _ in range(image.channels)]
        for pixel in image.iter_pixels(box):
            for index, value in enumerate(pixel):
                slot = min(int(value * bin_count), bin_count - 1)
                per_channel[index][max(slot, 0)] += 1
        names = ["r", "g", "b", "a"][: image.channels]
        for index, counts in enumerate(per_channel):
            histograms.append(
                {
                    "channel": names[index] if index < len(names) else f"c{index}",
                    "bins": counts,
                    "bin_width": round(1.0 / bin_count, 4),
                }
            )

    flat_channels = [
        entry["channel"]
        for entry in stats.get("channels", [])
        if entry["stddev"] < 1e-6
    ]
    result = ToolResult.success(
        {
            "image_path": str(path),
            "region": {"x": x, "y": y, "width": width, "height": height},
            "image_size": {"width": image.width, "height": image.height},
            **stats,
            "histograms": histograms,
            "flat_channels": flat_channels,
        },
        output_paths=[str(path)],
        diagnostics=diagnostics,
    )
    if flat_channels:
        result.add_diagnostic(
            "info", f"Channels {flat_channels} are constant across the region."
        )
    return result


@tool(
    name="debug-pixel-shader",
    summary=(
        "Assemble everything needed to reason about the shader that produced a pixel: the "
        "candidate draw, its pixel shader disassembly, declared resources and bound inputs."
    ),
    category="advanced",
    parameters=with_session(
        x={"type": "integer", "description": "Pixel X coordinate."},
        y={"type": "integer", "description": "Pixel Y coordinate."},
        resource_id={"type": "integer", "description": "Render target resource id."},
        draw_index={"type": "integer", "description": "Skip coverage search and use this draw."},
        global_id={"type": "integer", "description": "Skip coverage search and use this event."},
        max_lines={"type": "integer", "description": "Disassembly lines to inline. Default 200."},
        required=[],
    ),
    returns="Selected draw, pixel shader disassembly, declared registers and bound resources.",
    examples=[
        "pix-tool-set debug-pixel-shader --resource-id 641 --x 960 --y 540",
        "pix-tool-set debug-pixel-shader --draw-index 2461",
    ],
    notes=(
        "Step-through debugging with live register values is only possible inside a PIX replay "
        "session. This tool gives the static equivalent: the exact shader that ran, its inputs "
        "and its code, so the caller can reason about the result."
    ),
)
def debug_pixel_shader(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    draw = capture.resolve_draw(
        draw_index=args.get("draw_index"), global_id=args.get("global_id")
    )

    coverage_note = None
    if draw is None:
        x, y = args.get("x"), args.get("y")
        if x is None or y is None:
            raise invalid_argument("x/y", "provide coordinates or a draw selector")
        resource_id = args.get("resource_id")
        matches = [
            candidate
            for candidate in capture.draw_calls
            if candidate.kind is EventKind.DRAW
            and candidate.render_target_resource_ids
            and (resource_id is None or int(resource_id) in candidate.render_target_resource_ids)
            and _covers_pixel(candidate, int(x), int(y))
        ]
        if not matches:
            raise not_found(
                "draw call",
                f"pixel ({x},{y})",
                "No draw covers that pixel; check the coordinates and resource id.",
            )
        draw = matches[-1]
        coverage_note = (
            f"Selected the last of {len(matches)} draws whose viewport covers ({x},{y})."
        )

    shader = draw.shader(ShaderStage.PS)
    max_lines = int(args.get("max_lines") or 200)

    disassembly = shader.disassembly if shader else ""
    lines = disassembly.splitlines()
    truncated = len(lines) > max_lines > 0
    inline = "\n".join(lines[:max_lines]) if truncated else disassembly

    bound_inputs: list[dict[str, Any]] = []
    for view in draw.srvs[:24]:
        resource = capture.resource(view.resource_id) if view.resource_id is not None else None
        bound_inputs.append(
            {
                **view.to_dict(),
                "resource": resource.to_dict() if resource else None,
            }
        )

    data: dict[str, Any] = {
        "draw": draw.to_dict(detail=True, max_views=8),
        "pixel_shader": shader.to_dict(detail=True) if shader else None,
        "declared_registers": shader.resource_bindings if shader else [],
        "input_signature": (
            [element.to_dict() for element in shader.input_signature] if shader else []
        ),
        "constant_buffers": shader.constant_buffers if shader else [],
        "bound_srvs": bound_inputs,
        "render_targets": [target.to_dict() for target in draw.render_targets],
        "depth_stencil": draw.depth_stencil.to_dict() if draw.depth_stencil else None,
        "disassembly": inline,
        "disassembly_truncated": truncated,
        "disassembly_line_count": len(lines),
    }
    result = ToolResult.success(data)
    if coverage_note:
        result.add_diagnostic("info", coverage_note)
    if shader is None:
        result.degrade("This draw has no pixel shader bound.")
    elif not disassembly:
        result.degrade(
            "Pixel shader disassembly is unavailable.",
            reason=capture.disassembly_unavailable_reason,
        )
    return result
