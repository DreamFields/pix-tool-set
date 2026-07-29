"""Requirement section 3: frame statistics and render passes."""

from __future__ import annotations

from typing import Any

from ..context import ToolContext
from ..engine.model import EventKind
from ..errors import not_found
from ..results import ToolResult
from ._common import PAGE_PARAMS, page_args, page_envelope, percent, tool, with_session

_TIMING_NOTE = (
    "PIX GPU capture files do not carry per-pass wall-clock timings unless the capture "
    "was taken with GPU counters. This tool therefore reports a workload-based cost "
    "model (triangles, threads, pixel coverage, state changes) plus any counters that "
    "were exported. Treat the ranking as relative cost, not milliseconds."
)


@tool(
    name="frame-stats",
    summary=(
        "Whole-frame statistics: event and draw counts, triangle and thread totals, "
        "resource and descriptor inventory, shader stage breakdown."
    ),
    category="frame",
    parameters=with_session(),
    returns="Nested statistics object covering every parsed layer.",
    examples=["pix-tool-set frame-stats"],
    aliases=["stats"],
)
def frame_stats(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    stats = capture.frame_statistics()
    result = ToolResult.success(stats)
    if not capture.events:
        result.degrade("Event counts are zero because this session has no event list.")
    return result


@tool(
    name="list-passes",
    summary=(
        "List render passes reconstructed from PIX marker hierarchy, with draw counts, "
        "geometry load and the render targets each pass writes."
    ),
    category="frame",
    parameters=with_session(
        PAGE_PARAMS,
        name={"type": "string", "description": "Substring filter on the pass name."},
        min_draws={"type": "integer", "description": "Only passes with at least this many events."},
        sort_by={
            "type": "string",
            "enum": ["order", "draws", "triangles", "threads"],
            "description": "Ordering. Default 'order' (submission order).",
        },
    ),
    returns="Paged list of passes with per-pass workload figures.",
    examples=[
        "pix-tool-set list-passes --limit 30",
        "pix-tool-set list-passes --sort-by triangles --limit 10",
    ],
)
def list_passes(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    offset, limit = page_args(args)
    entries = list(capture.passes)

    needle = (args.get("name") or "").lower()
    if needle:
        entries = [entry for entry in entries if needle in entry["name"].lower()]
    min_draws = args.get("min_draws")
    if min_draws is not None:
        entries = [entry for entry in entries if entry["event_count"] >= int(min_draws)]

    sort_by = args.get("sort_by") or "order"
    sorters = {
        "order": lambda e: e["pass_index"],
        "draws": lambda e: -e["event_count"],
        "triangles": lambda e: -e["triangle_count"],
        "threads": lambda e: -e["thread_count"],
    }
    entries.sort(key=sorters.get(sort_by, sorters["order"]))

    window = entries[offset : offset + limit] if limit else entries[offset:]
    return ToolResult.success(
        {
            "passes": window,
            "sort_by": sort_by,
            **page_envelope(len(entries), offset, limit, len(window)),
        }
    )


@tool(
    name="pass-info",
    summary=(
        "Detail for one render pass: bound render targets and depth buffers, pipeline "
        "states used, shaders involved, and the draw calls it contains."
    ),
    category="frame",
    parameters=with_session(
        pass_name={"type": "string", "description": "Pass name (substring match)."},
        pass_index={"type": "integer", "description": "Pass index from list-passes."},
        include_draws={
            "type": "boolean",
            "description": "Include the pass's draw call list. Default true.",
        },
        max_draws={"type": "integer", "description": "Cap on listed draws. Default 25."},
    ),
    returns="Pass detail with resources, PSOs, shaders and member draws.",
    examples=[
        "pix-tool-set pass-info --pass-index 12",
        'pix-tool-set pass-info --pass-name "ShadowDepths"',
    ],
)
def pass_info(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    key = args.get("pass_index")
    if key is None:
        key = args.get("pass_name")
    if key is None:
        raise not_found("pass", None, "Pass --pass-index or --pass-name.")
    entry = capture.find_pass(key)
    if entry is None:
        raise not_found("pass", key, "Run list-passes to see valid names and indices.")

    marker_path = tuple(entry["marker_path"])
    draws = [d for d in capture.draw_calls if d.marker_path == marker_path]

    render_targets = [
        capture.resources[rid].to_dict()
        for rid in entry["render_target_ids"]
        if rid in capture.resources
    ]
    depth_targets = [
        capture.resources[rid].to_dict()
        for rid in entry["depth_stencil_ids"]
        if rid in capture.resources
    ]
    shader_keys: dict[str, dict[str, Any]] = {}
    for draw in draws:
        for shader in draw.shaders:
            shader_keys.setdefault(shader.key, shader.to_dict())

    max_draws = int(args.get("max_draws") or 25)
    include_draws = args.get("include_draws")
    include = True if include_draws is None else bool(include_draws)

    data: dict[str, Any] = {
        "pass": entry,
        "render_targets": render_targets,
        "depth_stencil_targets": depth_targets,
        "pipeline_states": [
            capture.pipeline_states[pid].to_dict()
            for pid in entry["pso_ids"]
            if pid in capture.pipeline_states
        ],
        "shaders": list(shader_keys.values()),
        "resource_summary": {
            "distinct_resources": len(
                {r.api_id for draw in draws for r in draw.resources()}
            ),
            "srv_bindings": sum(len(draw.srvs) for draw in draws),
            "uav_bindings": sum(len(draw.uavs) for draw in draws),
            "cbv_bindings": sum(len(draw.cbvs) for draw in draws),
        },
    }
    if include:
        data["draw_calls"] = [draw.to_dict() for draw in draws[:max_draws]]
        data["draw_calls_truncated"] = len(draws) > max_draws
    return ToolResult.success(data)


@tool(
    name="pass-cost",
    summary=(
        "Relative cost estimate per render pass. Reports the workload model (triangles, "
        "compute threads, pixel coverage, state churn) and any GPU counters present in "
        "the event list."
    ),
    category="frame",
    parameters=with_session(
        PAGE_PARAMS,
        pass_name={"type": "string", "description": "Restrict to passes matching this name."},
        sort_by={
            "type": "string",
            "enum": ["cost", "triangles", "threads", "pixels", "order"],
            "description": "Ordering. Default 'cost'.",
        },
    ),
    returns="Per-pass cost breakdown with shares of the frame total.",
    examples=["pix-tool-set pass-cost --limit 15"],
    notes=_TIMING_NOTE,
)
def pass_cost(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    offset, limit = page_args(args)

    by_path: dict[tuple[str, ...], list] = {}
    for draw in capture.draw_calls:
        by_path.setdefault(draw.marker_path, []).append(draw)

    counter_names: set[str] = set()
    for event in capture.events:
        counter_names.update(event.counters)

    rows: list[dict[str, Any]] = []
    for entry in capture.passes:
        draws = by_path.get(tuple(entry["marker_path"]), [])
        pixels = 0
        for draw in draws:
            for target in draw.render_targets:
                pixels += target.pixel_count
        state_changes = len({draw.pso_id for draw in draws if draw.pso_id is not None})
        triangles = entry["triangle_count"]
        threads = entry["thread_count"]
        cost = triangles * 1.0 + threads * 0.05 + pixels * 0.001 + state_changes * 500.0

        counters: dict[str, float] = {}
        if counter_names:
            for draw in draws:
                event = draw.event
                if event is None:
                    continue
                for name, raw in event.counters.items():
                    try:
                        counters[name] = counters.get(name, 0.0) + float(raw)
                    except ValueError:
                        continue

        row = {
            "pass_index": entry["pass_index"],
            "name": entry["name"],
            "event_count": entry["event_count"],
            "triangle_count": triangles,
            "thread_count": threads,
            "render_target_pixels": pixels,
            "pipeline_state_changes": state_changes,
            "cost_score": round(cost, 2),
        }
        if counters:
            row["counters"] = {k: round(v, 3) for k, v in counters.items()}
        rows.append(row)

    needle = (args.get("pass_name") or "").lower()
    if needle:
        rows = [row for row in rows if needle in row["name"].lower()]

    total_cost = sum(row["cost_score"] for row in rows)
    for row in rows:
        row["cost_share_percent"] = percent(row["cost_score"], total_cost)

    sort_by = args.get("sort_by") or "cost"
    sorters = {
        "cost": lambda r: -r["cost_score"],
        "triangles": lambda r: -r["triangle_count"],
        "threads": lambda r: -r["thread_count"],
        "pixels": lambda r: -r["render_target_pixels"],
        "order": lambda r: r["pass_index"],
    }
    rows.sort(key=sorters.get(sort_by, sorters["cost"]))
    window = rows[offset : offset + limit] if limit else rows[offset:]

    result = ToolResult.success(
        {
            "model": "workload-estimate",
            "formula": "triangles + 0.05*threads + 0.001*rt_pixels + 500*pso_changes",
            "counters_available": sorted(counter_names),
            "total_cost_score": round(total_cost, 2),
            "passes": window,
            "sort_by": sort_by,
            **page_envelope(len(rows), offset, limit, len(window)),
        }
    )
    if not counter_names:
        result.degrade(
            "No GPU counters in this capture, so timings are modelled rather than measured.",
            hint="Re-open with `session-open --counters 'D3D*' --force` if the capture has counters.",
        )
    return result
