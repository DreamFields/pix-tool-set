"""Requirement section 6: geometry, models and draw calls."""

from __future__ import annotations

from collections import Counter
from typing import Any

from ..context import ToolContext
from ..engine.model import EventKind
from ..errors import not_found
from ..results import ToolResult
from ._common import (
    DRAW_SELECTOR,
    PAGE_PARAMS,
    page_args,
    page_envelope,
    pass_identity,
    percent,
    tool,
    with_session,
)


@tool(
    name="model-stats",
    summary=(
        "Geometry inventory across the frame: distinct meshes inferred from vertex/index "
        "buffer pairs, triangle totals, instancing usage and vertex format variety."
    ),
    category="geometry",
    parameters=with_session(
        top={"type": "integer", "description": "How many heaviest meshes to list. Default 10."},
    ),
    returns="Mesh inventory with per-mesh triangle counts and draw references.",
    examples=["pix-tool-set model-stats"],
    notes=(
        "A 'mesh' here is a distinct (index buffer, vertex buffer, stride) tuple observed at "
        "draw time. PIX captures do not carry asset names, so meshes are identified by the "
        "buffers they draw from."
    ),
)
def model_stats(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    draws = [d for d in capture.draw_calls if d.kind is EventKind.DRAW]

    meshes: dict[tuple, dict[str, Any]] = {}
    for draw in draws:
        index_resource = draw.index_buffer.resource_id if draw.index_buffer else None
        vertex_resource = draw.vertex_buffers[0].resource_id if draw.vertex_buffers else None
        stride = draw.vertex_buffers[0].stride if draw.vertex_buffers else 0
        key = (index_resource, vertex_resource, stride)
        entry = meshes.setdefault(
            key,
            {
                "index_buffer_id": index_resource,
                "vertex_buffer_id": vertex_resource,
                "vertex_stride": stride,
                "draw_count": 0,
                "instance_total": 0,
                "triangle_total": 0,
                "index_total": 0,
                "draw_indices": [],
                "passes": [],
            },
        )
        entry["draw_count"] += 1
        entry["instance_total"] += max(draw.instance_count, 1)
        entry["triangle_total"] += draw.triangle_count
        entry["index_total"] += draw.vertex_or_index_count
        if len(entry["draw_indices"]) < 20:
            entry["draw_indices"].append(draw.index)
        if draw.pass_name and draw.pass_name not in entry["passes"]:
            entry["passes"].append(draw.pass_name)

    stride_counter = Counter(
        draw.vertex_buffers[0].stride for draw in draws if draw.vertex_buffers
    )
    topology_counter = Counter(draw.primitive_topology or "(inherited)" for draw in draws)

    rows = sorted(meshes.values(), key=lambda entry: -entry["triangle_total"])
    top_count = int(args.get("top") or 10)
    total_triangles = sum(entry["triangle_total"] for entry in rows)
    for entry in rows:
        entry["triangle_share_percent"] = percent(entry["triangle_total"], total_triangles)

    return ToolResult.success(
        {
            "totals": {
                "graphics_draws": len(draws),
                "distinct_meshes": len(meshes),
                "total_triangles": total_triangles,
                "total_instances": sum(max(d.instance_count, 1) for d in draws),
                "indexed_draws": sum(1 for d in draws if d.index_buffer is not None),
                "non_indexed_draws": sum(1 for d in draws if d.index_buffer is None),
                "instanced_draws": sum(1 for d in draws if d.instance_count > 1),
            },
            "vertex_strides": dict(sorted(stride_counter.items(), key=lambda kv: -kv[1])),
            "topologies": dict(sorted(topology_counter.items(), key=lambda kv: -kv[1])),
            "heaviest_meshes": rows[:top_count],
        }
    )


@tool(
    name="draw-call-stats",
    summary=(
        "Draw call statistics: counts by kind and by pass, triangle and thread distribution, "
        "instancing patterns, and the heaviest individual calls."
    ),
    category="geometry",
    parameters=with_session(
        top={"type": "integer", "description": "How many heaviest draws to list. Default 10."},
        by_pass={"type": "boolean", "description": "Include a per-pass breakdown."},
    ),
    returns="Aggregate draw statistics with distributions and outliers.",
    examples=["pix-tool-set draw-call-stats --by-pass"],
)
def draw_call_stats(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    draws = capture.draw_calls
    graphics = [d for d in draws if d.kind is EventKind.DRAW]
    dispatches = [d for d in draws if d.kind is EventKind.DISPATCH]

    triangles = sorted((d.triangle_count for d in graphics), reverse=True)
    threads = sorted((d.thread_count for d in dispatches), reverse=True)

    def distribution(values: list[int]) -> dict[str, Any]:
        if not values:
            return {"count": 0}
        total = sum(values)
        return {
            "count": len(values),
            "total": total,
            "mean": round(total / len(values), 2),
            "max": values[0],
            "median": values[len(values) // 2],
            "p90": values[int(len(values) * 0.1)] if len(values) > 10 else values[0],
        }

    top_count = int(args.get("top") or 10)
    heaviest = sorted(graphics, key=lambda d: -d.triangle_count)[:top_count]
    heaviest_dispatch = sorted(dispatches, key=lambda d: -d.thread_count)[:top_count]

    data: dict[str, Any] = {
        "totals": {
            "all": len(draws),
            "by_kind": dict(Counter(d.kind.value for d in draws)),
            "distinct_pso": len({d.pso_id for d in draws if d.pso_id is not None}),
            "distinct_root_signatures": len(
                {d.root_signature_id for d in draws if d.root_signature_id is not None}
            ),
        },
        "triangle_distribution": distribution(triangles),
        "thread_distribution": distribution(threads),
        "instancing": {
            "instanced_draws": sum(1 for d in graphics if d.instance_count > 1),
            "max_instances": max((d.instance_count for d in graphics), default=0),
            "total_instances": sum(max(d.instance_count, 1) for d in graphics),
        },
        "binding_load": {
            "avg_srv_per_draw": round(
                sum(len(d.srvs) for d in draws) / len(draws), 2
            )
            if draws
            else 0,
            "avg_uav_per_draw": round(
                sum(len(d.uavs) for d in draws) / len(draws), 2
            )
            if draws
            else 0,
            "avg_cbv_per_draw": round(
                sum(len(d.cbvs) for d in draws) / len(draws), 2
            )
            if draws
            else 0,
        },
        "heaviest_draws": [d.to_dict() for d in heaviest],
        "heaviest_dispatches": [d.to_dict() for d in heaviest_dispatch],
    }

    if bool(args.get("by_pass")):
        rows = []
        for entry in capture.passes:
            rows.append(
                {
                    "pass_index": entry["pass_index"],
                    "name": entry["name"],
                    **pass_identity(entry),
                    "draw_count": entry["draw_count"],
                    "dispatch_count": entry["dispatch_count"],
                    "triangle_count": entry["triangle_count"],
                    "thread_count": entry["thread_count"],
                }
            )
        rows.sort(key=lambda row: -(row["draw_count"] + row["dispatch_count"]))
        data["by_pass"] = rows

    return ToolResult.success(data)


@tool(
    name="list-draw-calls",
    summary=(
        "List draw calls in submission order with their pass, PSO, geometry counts and "
        "bound resource counts."
    ),
    category="geometry",
    parameters=with_session(
        PAGE_PARAMS,
        kind={
            "type": "string",
            "enum": ["draw", "dispatch", "dispatch_rays", "execute_indirect"],
            "description": "Restrict to one draw kind.",
        },
        pass_name={"type": "string", "description": "Substring match on the innermost marker."},
        detail={"type": "boolean", "description": "Include the full bound state per draw."},
        sort_by={
            "type": "string",
            "enum": ["order", "triangles", "threads", "instances"],
            "description": "Ordering. Default 'order'.",
        },
    ),
    returns="Paged draw call list.",
    examples=[
        "pix-tool-set list-draw-calls --limit 25",
        "pix-tool-set list-draw-calls --sort-by triangles --limit 10",
    ],
)
def list_draw_calls(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    offset, limit = page_args(args)

    matched, _total = capture.find_draw_calls(
        kind=args.get("kind"),
        pass_name=args.get("pass_name"),
    )
    sort_by = args.get("sort_by") or "order"
    sorters = {
        "order": lambda d: d.index,
        "triangles": lambda d: -d.triangle_count,
        "threads": lambda d: -d.thread_count,
        "instances": lambda d: -d.instance_count,
    }
    matched.sort(key=sorters.get(sort_by, sorters["order"]))

    total = len(matched)
    window = matched[offset : offset + limit] if limit else matched[offset:]
    detail = bool(args.get("detail"))
    return ToolResult.success(
        {
            "draw_calls": [draw.to_dict(detail=detail) for draw in window],
            "sort_by": sort_by,
            **page_envelope(total, offset, limit, len(window)),
        }
    )


@tool(
    name="diff-draw-calls",
    summary=(
        "Compare two draw calls field by field: pipeline state, shaders, render targets, "
        "geometry inputs and every root binding. Reports only what differs plus a summary."
    ),
    category="geometry",
    parameters=with_session(
        left_draw={"type": "integer", "description": "First draw index."},
        right_draw={"type": "integer", "description": "Second draw index."},
        left_global_id={"type": "integer", "description": "First draw by Global ID."},
        right_global_id={"type": "integer", "description": "Second draw by Global ID."},
        include_same={"type": "boolean", "description": "Also list fields that match."},
    ),
    returns="Field-level differences with a same/different verdict per group.",
    examples=[
        "pix-tool-set diff-draw-calls --left-draw 2461 --right-draw 2462",
        "pix-tool-set diff-draw-calls --left-global-id 3644 --right-global-id 3650",
    ],
    aliases=["compare-draw-calls"],
)
def diff_draw_calls(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    left = capture.resolve_draw(
        draw_index=args.get("left_draw"), global_id=args.get("left_global_id")
    )
    right = capture.resolve_draw(
        draw_index=args.get("right_draw"), global_id=args.get("right_global_id")
    )
    if left is None:
        raise not_found("draw call", args.get("left_draw") or args.get("left_global_id"))
    if right is None:
        raise not_found("draw call", args.get("right_draw") or args.get("right_global_id"))

    include_same = bool(args.get("include_same"))

    def shader_map(draw) -> dict[str, str]:
        return {shader.stage.value: shader.shader_hash or shader.debug_name for shader in draw.shaders}

    def binding_map(draw) -> dict[int, str]:
        out: dict[int, str] = {}
        for binding in draw.bindings:
            targets = [
                str(view.resource_id)
                for view in binding.resolved_views
                if view.resource_id is not None
            ]
            out[binding.root_index] = (
                f"{binding.kind.value}:"
                + (",".join(targets) if targets else str(binding.resource_id))
            )
        return out

    comparisons: list[dict[str, Any]] = []

    def compare(field: str, left_value: Any, right_value: Any) -> None:
        same = left_value == right_value
        if same and not include_same:
            return
        comparisons.append(
            {"field": field, "same": same, "left": left_value, "right": right_value}
        )

    compare("api", left.api, right.api)
    compare("kind", left.kind.value, right.kind.value)
    compare("pass_name", left.pass_name, right.pass_name)
    compare("marker_path", list(left.marker_path), list(right.marker_path))
    compare("pso_id", left.pso_id, right.pso_id)
    compare("root_signature_id", left.root_signature_id, right.root_signature_id)
    compare("primitive_topology", left.primitive_topology, right.primitive_topology)
    compare("shaders", shader_map(left), shader_map(right))
    compare(
        "render_targets",
        left.render_target_resource_ids,
        right.render_target_resource_ids,
    )
    compare("depth_stencil", left.depth_stencil_resource_id, right.depth_stencil_resource_id)
    compare(
        "index_buffer",
        left.index_buffer.to_dict() if left.index_buffer else None,
        right.index_buffer.to_dict() if right.index_buffer else None,
    )
    compare(
        "vertex_buffers",
        [vertex.to_dict() for vertex in left.vertex_buffers],
        [vertex.to_dict() for vertex in right.vertex_buffers],
    )
    compare("vertex_or_index_count", left.vertex_or_index_count, right.vertex_or_index_count)
    compare("instance_count", left.instance_count, right.instance_count)
    compare("triangle_count", left.triangle_count, right.triangle_count)
    compare(
        "thread_groups",
        [left.thread_group_x, left.thread_group_y, left.thread_group_z],
        [right.thread_group_x, right.thread_group_y, right.thread_group_z],
    )
    compare("viewports", left.viewports, right.viewports)
    compare("scissor_rects", left.scissor_rects, right.scissor_rects)

    left_bindings = binding_map(left)
    right_bindings = binding_map(right)
    binding_diff: list[dict[str, Any]] = []
    for root_index in sorted(set(left_bindings) | set(right_bindings)):
        lhs = left_bindings.get(root_index)
        rhs = right_bindings.get(root_index)
        if lhs != rhs or include_same:
            binding_diff.append(
                {"root_index": root_index, "same": lhs == rhs, "left": lhs, "right": rhs}
            )

    differing = [entry["field"] for entry in comparisons if not entry["same"]]
    return ToolResult.success(
        {
            "left": left.to_dict(),
            "right": right.to_dict(),
            "identical": not differing and not [b for b in binding_diff if not b["same"]],
            "differing_fields": differing,
            "field_comparison": comparisons,
            "binding_comparison": binding_diff,
            "summary": {
                "fields_compared": len(comparisons),
                "fields_differing": len(differing),
                "bindings_differing": len([b for b in binding_diff if not b["same"]]),
            },
        }
    )
