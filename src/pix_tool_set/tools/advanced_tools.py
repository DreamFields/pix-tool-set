"""Requirement section 10: advanced analysis."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..context import ToolContext
from ..engine import bindinglabel, resourceevents
from ..engine.model import EventKind, RootParameterKind, ShaderStage, ViewKind
from ..errors import PixToolError, invalid_argument, not_found
from ..results import ToolResult
from ._common import (
    DRAW_SELECTOR,
    PASS_SELECTOR,
    resolve_draw,
    resolve_pass,
    tool,
    with_session,
)
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
        include_resource_events={
            "type": "boolean",
            "description": (
                "Also list the clears and discards that affect the pixel. The PIX pixel "
                "history shows these alongside the draws -- a clear is usually the row "
                "that explains the value a draw started from. Requires --resource-id."
            ),
        },
        include_final_value={
            "type": "boolean",
            "description": "Also export the target and read the pixel's final value.",
        },
        required=["x", "y"],
    ),
    returns="Ordered candidate writer list, blend state, and optionally the final pixel value.",
    examples=[
        "pix-tool-set pixel-history --resource-id 641 --x 960 --y 540",
        "pix-tool-set pixel-history --resource-id 756 --x 810 --y 284 --include-resource-events",
    ],
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
        entry = {
            "draw_index": draw.index,
            "global_id": draw.global_id,
            "api": draw.api,
            "event_type": "draw",
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
        # PIX shows an ExecuteIndirect's expanded child id, one higher than the
        # ExecuteIndirect's own, so quote both rather than making the caller
        # discover the offset when comparing against the PIX window.
        if draw.global_id is not None:
            entry["gui_global_id"] = (
                draw.global_id + 1 if draw.api == "ExecuteIndirect" else draw.global_id
            )
        if resource_id is not None:
            labels = bindinglabel.labels_for(capture, draw, int(resource_id))
            if labels:
                entry["binding"] = labels[0].text
        # A draw that binds a depth buffer and tests against it may be rejected at
        # this pixel. Whether it actually was cannot be decided statically -- that
        # is a replay question -- so the state is reported and the verdict is not.
        if pso is not None and pso.depth_enabled:
            entry["may_fail_depth_stencil"] = True
        candidates.append(entry)

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

    # -- clears and discards that hit this pixel ------------------------
    if bool(args.get("include_resource_events")):
        if resource_id is None:
            raise invalid_argument(
                "resource_id",
                "required with --include-resource-events: a clear is only relevant to "
                "the pixel of a specific resource",
            )
        events = resourceevents.events_for_resource(
            capture.resource_events, int(resource_id)
        )
        rows: list[dict[str, Any]] = []
        for event in events:
            if event.event_type not in ("clear", "discard"):
                continue
            touch = event.touch_for(int(resource_id))
            if touch is None:
                continue
            row: dict[str, Any] = {
                "global_id": event.global_id,
                "gui_global_id": event.global_id,
                "api": event.api,
                "event_type": event.event_type,
                # A full-resource clear or discard covers every pixel, so it is
                # unconditionally part of this pixel's history.
                "covers_pixel": True,
                "binding": "OM [None]" if event.event_type == "clear" else None,
                "source": f"{event.source_file}:{event.source_line}",
            }
            if event.clear_value is not None:
                row["clear_value"] = event.clear_value
            rows.append(row)
        data["resource_events"] = rows

        merged = [dict(row) for row in candidates] + rows
        merged.sort(
            key=lambda row: (
                row.get("gui_global_id")
                if row.get("gui_global_id") is not None
                else (row.get("global_id") if row.get("global_id") is not None else 1 << 62)
            )
        )
        data["combined_history"] = merged[: max_events + len(rows)]
        data["combined_history_count"] = len(merged)

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
    parameters=with_session(PASS_SELECTOR),
    returns="Pass workload, inputs/outputs, shader mix and observations.",
    examples=[
        'pix-tool-set analyze-pass --pass-name "ShadowDepths"',
        "pix-tool-set analyze-pass --queue-id 18704",
    ],
)
def analyze_pass(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    entry = resolve_pass(capture, args)

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
        # Root-level bindings, which `srvs` / `uavs` cannot see: those walk the
        # resolved views of descriptor tables, and a ray dispatch binds almost
        # everything as a root descriptor instead. Without this the resource flow of
        # a raytracing pass came back completely empty -- reported as success, so it
        # read as "this pass touches nothing" rather than "this tool looked in the
        # wrong place".
        for binding in draw.bindings:
            if binding.resource_id is None:
                continue
            if binding.kind is RootParameterKind.UAV:
                outputs[binding.resource_id] = outputs.get(binding.resource_id, 0) + 1
            elif binding.kind in (RootParameterKind.SRV, RootParameterKind.CBV):
                inputs[binding.resource_id] = inputs.get(binding.resource_id, 0) + 1

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

    # Raytracing work is invisible to every counter above: a ray dispatch has no PSO,
    # no shaders on draw.shaders, no triangles and no thread_count. Summarising it as
    # zeroes across the board is a confident wrong answer, so its own shape is counted
    # here and the payload says the pass is a raytracing one.
    ray_draws = [draw for draw in draws if draw.is_raytracing]
    raytracing: dict[str, Any] = {}
    if ray_draws:
        state_object_ids = sorted(
            {d.state_object_id for d in ray_draws if d.state_object_id is not None}
        )
        export_stages: dict[str, int] = {}
        total_exports = 0
        for state_object_id in state_object_ids:
            state_object = capture.state_objects.get(state_object_id)
            if state_object is None:
                continue
            for export in state_object.resolved_exports:
                total_exports += 1
                if export.stage is not None:
                    export_stages[export.stage.value] = (
                        export_stages.get(export.stage.value, 0) + 1
                    )
        rays = 0
        dimensions: list[dict[str, Any]] = []
        for draw in ray_draws:
            sbt = draw.shader_binding_table
            if sbt is None:
                continue
            rays += sbt.ray_count or 0
            dimensions.append(
                {
                    "draw_index": draw.index,
                    "global_id": draw.global_id,
                    "dispatch_dimensions": [sbt.width, sbt.height, sbt.depth],
                    "ray_count": sbt.ray_count,
                    "shader_binding_table_key": sbt.indirect_buffer_key,
                }
            )
        raytracing = {
            "ray_dispatches": len(ray_draws),
            "state_object_ids": state_object_ids,
            "shader_exports": total_exports,
            "export_stage_mix": export_stages,
            "rays": rays,
            "dispatches": dimensions,
            "note": (
                "A ray dispatch has no PSO, no triangles and no thread_count, so the "
                "`workload` block above reads as zero for it by construction. This block "
                "is the workload of this pass. Run describe-state-object for the pipeline "
                "and pass-bindings for the bindings."
            ),
        }
        # shader_mix keys off PSO stages, so it is empty here; fill it from the exports
        # rather than leaving a blank that reads as "no shaders run".
        if not shader_mix and export_stages:
            shader_mix = dict(export_stages)

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
    if ray_draws:
        observations.append(
            {
                "severity": "info",
                "topic": "raytracing_pass",
                "message": (
                    f"{len(ray_draws)} ray dispatch(es) in this pass, tracing "
                    f"{raytracing.get('rays', 0)} rays through "
                    f"{raytracing.get('shader_exports', 0)} shader export(s). The "
                    f"triangle and thread counters do not apply to them; see the "
                    f"`raytracing` block."
                ),
                "draw_indices": [d.index for d in ray_draws][:10],
            }
        )

    data: dict[str, Any] = {
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
    if raytracing:
        data["raytracing"] = raytracing
        data["pass_kind"] = "raytracing"
    else:
        data["pass_kind"] = "rasterisation"

    return ToolResult.success(data)


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
        global_id={
            "type": "integer",
            "description": (
                "PIX Global ID of the event whose contents to sample. Unique across every "
                "queue, so use this for an id copied out of the PIX GUI."
            ),
        },
        queue_id={
            "type": "integer",
            "description": (
                "PIX GUI 'Queue ID' of the event whose contents to sample. This id is "
                "present only for events on the queue the event list export covers; use "
                "global_id for the rest."
            ),
        },
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
    queue_id = args.get("queue_id")
    global_id = args.get("global_id")
    if resource_id is None and queue_id is None and global_id is None:
        raise invalid_argument("resource_id/global_id/queue_id", "provide at least one")

    path, diagnostics = _texture_export(
        context,
        args,
        capture,
        resource_id=int(resource_id) if resource_id is not None else None,
        queue_id=int(queue_id) if queue_id is not None else None,
        global_id=int(global_id) if global_id is not None else None,
        rtv=int(args.get("rtv") or 0),
        depth=bool(args.get("depth")),
        stem=f"region_{resource_id if resource_id is not None else queue_id}",
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
        global_id={
            "type": "integer",
            "description": (
                "PIX Global ID of the event to use instead of the coverage search. Unique "
                "across every queue, so use this for an id copied out of the PIX GUI."
            ),
        },
        queue_id={
            "type": "integer",
            "description": (
                "PIX GUI 'Queue ID' of the event to use instead of the coverage search. "
                "Present only for events on the exported queue; global_id and draw_index "
                "always work."
            ),
        },
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
        draw_index=args.get("draw_index"),
        global_id=args.get("global_id"),
        queue_id=args.get("queue_id"),
    )

    coverage_note = None
    if draw is None:
        # An explicit selector that resolved to nothing must fail, not fall through to
        # the coverage search: the search would answer with whichever draw happens to
        # cover the pixel, which is a different event than the one asked for, and the
        # payload would look like a successful lookup of the requested id.
        if any(
            args.get(key) is not None
            for key in ("draw_index", "global_id", "queue_id")
        ):
            resolve_draw(capture, args)
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
