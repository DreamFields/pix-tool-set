"""Requirement section 11: performance analysis."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from ..context import ToolContext
from ..engine.model import EventKind, ViewKind, format_bits_per_pixel
from ..results import ToolResult
from ._common import PAGE_PARAMS, page_args, page_envelope, percent, tool, with_session

_ESTIMATE_NOTE = (
    "These figures are derived statically from the captured command stream (bound targets, "
    "draw arguments, resource descriptors). They are estimates for ranking and comparison, "
    "not hardware measurements. Use a PIX timing capture for measured numbers."
)


@tool(
    name="analyze-overdraw",
    summary=(
        "Estimate overdraw per render target: how many draws write the same target, their "
        "combined covered area versus the target's own area, and which passes contribute."
    ),
    category="performance",
    parameters=with_session(
        PAGE_PARAMS,
        min_draws={"type": "integer", "description": "Only targets written by at least N draws. Default 2."},
        sort_by={
            "type": "string",
            "enum": ["overdraw", "draws", "pixels"],
            "description": "Ordering. Default 'overdraw'.",
        },
    ),
    returns="Per-target overdraw ratio with contributing passes and blend usage.",
    examples=["pix-tool-set analyze-overdraw --limit 15"],
    notes=_ESTIMATE_NOTE,
)
def analyze_overdraw(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    offset, limit = page_args(args)

    per_target: dict[int, dict[str, Any]] = {}
    for draw in capture.draw_calls:
        if draw.kind not in (EventKind.DRAW, EventKind.EXECUTE_INDIRECT):
            continue
        for resource_id in draw.render_target_resource_ids:
            resource = capture.resource(resource_id)
            if resource is None or not resource.is_texture:
                continue
            entry = per_target.setdefault(
                resource_id,
                {
                    "resource_id": resource_id,
                    "description": resource.describe(),
                    "target_pixels": resource.pixel_count,
                    "draw_count": 0,
                    "covered_pixels": 0,
                    "blended_draws": 0,
                    "depth_tested_draws": 0,
                    "passes": [],
                    "triangles": 0,
                    "bounded_draws": 0,
                    "unbounded_draws": 0,
                },
            )
            entry["draw_count"] += 1
            entry["triangles"] += draw.triangle_count

            # Establish the tightest area bound this draw could touch.
            # Scissor is authoritative when present; viewport narrows it further.
            # Without either, a draw may cover the whole target, but a draw with
            # only a handful of triangles almost certainly does not, so cap the
            # estimate by a per-triangle budget instead of assuming full screen.
            bounded = False
            area = resource.pixel_count
            if draw.viewports:
                viewport = draw.viewports[0]
                viewport_area = int(
                    max(viewport.get("width", 0), 0) * max(viewport.get("height", 0), 0)
                )
                if viewport_area:
                    area = min(area, viewport_area)
                    bounded = True
            if draw.scissor_rects:
                scissor = draw.scissor_rects[0]
                clipped = max(scissor.get("right", 0) - scissor.get("left", 0), 0) * max(
                    scissor.get("bottom", 0) - scissor.get("top", 0), 0
                )
                if clipped:
                    area = min(area, clipped)
                    bounded = True
            if not bounded:
                entry["unbounded_draws"] += 1
            else:
                entry["bounded_draws"] += 1

            triangles = max(draw.triangle_count, 1)
            if triangles < 64:
                # Small-geometry draws (UI quads, decals, debug shapes) cannot
                # plausibly shade the whole target. Bound them generously but
                # finitely so a batch of 2000 quads does not read as 2000x overdraw.
                area = min(area, triangles * 8192)
            entry["covered_pixels"] += area

            pso = draw.pipeline_state
            if pso is not None:
                if pso.blend_enabled:
                    entry["blended_draws"] += 1
                if pso.depth_enabled:
                    entry["depth_tested_draws"] += 1
            if draw.pass_name and draw.pass_name not in entry["passes"]:
                entry["passes"].append(draw.pass_name)
                # Pair each contributing pass with an addressable id, so a caller can
                # jump straight to it in PIX instead of searching by name.
                entry.setdefault("pass_queue_ids", []).append(draw.queue_id)

    min_draws = int(args.get("min_draws") or 2)
    rows = []
    for entry in per_target.values():
        if entry["draw_count"] < min_draws:
            continue
        target_pixels = max(entry["target_pixels"], 1)
        entry["overdraw_ratio"] = round(entry["covered_pixels"] / target_pixels, 3)
        entry["blend_share_percent"] = percent(entry["blended_draws"], entry["draw_count"])
        entry["depth_test_share_percent"] = percent(
            entry["depth_tested_draws"], entry["draw_count"]
        )
        entry["confidence"] = (
            "high"
            if entry["unbounded_draws"] == 0
            else ("low" if entry["bounded_draws"] == 0 else "medium")
        )
        rows.append(entry)

    sort_by = args.get("sort_by") or "overdraw"
    sorters = {
        "overdraw": lambda entry: -entry["overdraw_ratio"],
        "draws": lambda entry: -entry["draw_count"],
        "pixels": lambda entry: -entry["covered_pixels"],
    }
    rows.sort(key=sorters.get(sort_by, sorters["overdraw"]))
    total = len(rows)
    window = rows[offset : offset + limit] if limit else rows[offset:]

    observations = []
    for entry in window[:5]:
        if entry["overdraw_ratio"] > 3.0:
            observations.append(
                {
                    "severity": "warning" if entry["confidence"] != "low" else "info",
                    "resource_id": entry["resource_id"],
                    "confidence": entry["confidence"],
                    "message": (
                        f"Estimated overdraw {entry['overdraw_ratio']}x across "
                        f"{entry['draw_count']} draws; consider a depth pre-pass or "
                        "front-to-back sorting."
                    ),
                }
            )
    result = ToolResult.success(
        {
            "targets": window,
            "observations": observations,
            "sort_by": sort_by,
            "method": "area-bound-estimate",
            "method_detail": (
                "Per draw the covered area is bounded by scissor and viewport when present; "
                "draws with fewer than 64 triangles are additionally capped at 8192 pixels "
                "per triangle so batches of small quads are not reported as full-screen "
                "coverage. 'confidence' reports whether the bounds were available."
            ),
            **page_envelope(total, offset, limit, len(window)),
        }
    )
    unbounded_total = sum(entry["unbounded_draws"] for entry in rows)
    if unbounded_total:
        result.add_diagnostic(
            "info",
            f"{unbounded_total} draw(s) had no viewport or scissor recorded; their coverage is bounded heuristically.",
        )
    return result


@tool(
    name="analyze-bandwidth",
    summary=(
        "Estimate memory bandwidth per pass and per resource: bytes written to render "
        "targets, bytes read through sampled textures, and the biggest contributors."
    ),
    category="performance",
    parameters=with_session(
        PAGE_PARAMS,
        group_by={
            "type": "string",
            "enum": ["pass", "resource"],
            "description": "Aggregate by render pass or by resource. Default 'pass'.",
        },
    ),
    returns="Estimated read/write bytes with shares of the frame total.",
    examples=[
        "pix-tool-set analyze-bandwidth --limit 15",
        "pix-tool-set analyze-bandwidth --group-by resource",
    ],
    notes=_ESTIMATE_NOTE,
)
def analyze_bandwidth(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    offset, limit = page_args(args)
    group_by = args.get("group_by") or "pass"

    def write_bytes(draw) -> int:
        total = 0
        for resource_id in draw.render_target_resource_ids:
            resource = capture.resource(resource_id)
            if resource is None:
                continue
            area = resource.pixel_count
            if draw.viewports:
                viewport = draw.viewports[0]
                area = int(
                    max(viewport.get("width", 0), 0) * max(viewport.get("height", 0), 0)
                ) or area
            total += area * format_bits_per_pixel(resource.format) // 8
        if draw.depth_stencil_resource_id is not None:
            resource = capture.resource(draw.depth_stencil_resource_id)
            if resource is not None:
                total += resource.pixel_count * format_bits_per_pixel(resource.format) // 8
        return total

    def read_bytes(draw) -> int:
        total = 0
        seen: set[int] = set()
        for view in draw.srvs:
            rid = view.resource_id
            if rid is None or rid in seen:
                continue
            seen.add(rid)
            resource = capture.resource(rid)
            if resource is not None:
                total += resource.size_bytes
        for vertex in draw.vertex_buffers:
            total += vertex.size_bytes
        if draw.index_buffer is not None:
            total += draw.index_buffer.size_bytes
        return total

    buckets: dict[Any, dict[str, Any]] = {}
    for draw in capture.draw_calls:
        written = write_bytes(draw)
        read = read_bytes(draw)
        if group_by == "resource":
            targets = list(draw.render_target_resource_ids)
            if draw.depth_stencil_resource_id is not None:
                targets.append(draw.depth_stencil_resource_id)
            for resource_id in targets:
                resource = capture.resource(resource_id)
                entry = buckets.setdefault(
                    resource_id,
                    {
                        "resource_id": resource_id,
                        "description": resource.describe() if resource else None,
                        "write_bytes": 0,
                        "read_bytes": 0,
                        "draw_count": 0,
                    },
                )
                entry["write_bytes"] += written // max(len(targets), 1)
                entry["draw_count"] += 1
            for view in draw.srvs:
                if view.resource_id is None:
                    continue
                resource = capture.resource(view.resource_id)
                entry = buckets.setdefault(
                    view.resource_id,
                    {
                        "resource_id": view.resource_id,
                        "description": resource.describe() if resource else None,
                        "write_bytes": 0,
                        "read_bytes": 0,
                        "draw_count": 0,
                    },
                )
                entry["read_bytes"] += resource.size_bytes if resource else 0
        else:
            key = draw.pass_name or "(root)"
            entry = buckets.setdefault(
                key,
                {
                    "pass_name": key,
                    # Grouping is by name, so several distinct passes can share a row.
                    # Quote the first contributing action's Queue ID so the row is still
                    # addressable in the PIX UI rather than being a bare label.
                    "queue_id": draw.queue_id,
                    "first_draw_index": draw.index,
                    "write_bytes": 0,
                    "read_bytes": 0,
                    "draw_count": 0,
                    "triangles": 0,
                },
            )
            entry["write_bytes"] += written
            entry["read_bytes"] += read
            entry["draw_count"] += 1
            entry["triangles"] += draw.triangle_count

    rows = list(buckets.values())
    for entry in rows:
        entry["total_bytes"] = entry["write_bytes"] + entry["read_bytes"]
        entry["total_mib"] = round(entry["total_bytes"] / 1048576.0, 3)
    grand_total = sum(entry["total_bytes"] for entry in rows)
    for entry in rows:
        entry["share_percent"] = percent(entry["total_bytes"], grand_total)
    rows.sort(key=lambda entry: -entry["total_bytes"])

    total = len(rows)
    window = rows[offset : offset + limit] if limit else rows[offset:]
    return ToolResult.success(
        {
            "group_by": group_by,
            "totals": {
                "estimated_bytes": grand_total,
                "estimated_mib": round(grand_total / 1048576.0, 2),
                "write_bytes": sum(entry["write_bytes"] for entry in rows),
                "read_bytes": sum(entry["read_bytes"] for entry in rows),
            },
            "entries": window,
            **page_envelope(total, offset, limit, len(window)),
        }
    )


@tool(
    name="analyze-state-changes",
    summary=(
        "Find state-change churn: pipeline state and root signature switches, descriptor "
        "heap rebinds, redundant re-binds, and the passes where they concentrate."
    ),
    category="performance",
    parameters=with_session(
        PAGE_PARAMS,
        min_switches={"type": "integer", "description": "Only report passes with at least N switches."},
    ),
    returns="Frame-wide switch counts, per-pass churn and redundancy findings.",
    examples=["pix-tool-set analyze-state-changes --limit 15"],
    notes=_ESTIMATE_NOTE,
)
def analyze_state_changes(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    offset, limit = page_args(args)
    draws = capture.draw_calls

    pso_switches = 0
    rootsig_switches = 0
    heap_switches = 0
    redundant_pso = 0
    previous_pso = previous_rootsig = None
    previous_heaps: list[int] | None = None
    for draw in draws:
        if draw.pso_id != previous_pso:
            pso_switches += 1
            previous_pso = draw.pso_id
        elif draw.pso_id is not None:
            redundant_pso += 1
        if draw.root_signature_id != previous_rootsig:
            rootsig_switches += 1
            previous_rootsig = draw.root_signature_id
        if draw.descriptor_heap_ids != previous_heaps:
            heap_switches += 1
            previous_heaps = list(draw.descriptor_heap_ids)

    per_pass: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list] = defaultdict(list)
    for draw in draws:
        grouped[draw.pass_name or "(root)"].append(draw)
    for name, members in grouped.items():
        switches = 0
        previous = None
        for draw in members:
            if draw.pso_id != previous:
                switches += 1
                previous = draw.pso_id
        per_pass[name] = {
            "pass_name": name,
            # See analyze-bandwidth: rows are keyed by name, so the first member's
            # Queue ID is what makes the row addressable in PIX.
            "queue_id": members[0].queue_id if members else None,
            "first_draw_index": members[0].index if members else None,
            "event_count": len(members),
            "pipeline_state_switches": switches,
            "distinct_pipeline_states": len(
                {draw.pso_id for draw in members if draw.pso_id is not None}
            ),
            "switches_per_event": round(switches / max(len(members), 1), 3),
        }

    rows = list(per_pass.values())
    min_switches = args.get("min_switches")
    if min_switches is not None:
        rows = [row for row in rows if row["pipeline_state_switches"] >= int(min_switches)]
    rows.sort(key=lambda row: -row["pipeline_state_switches"])

    findings: list[dict[str, Any]] = []
    if draws and pso_switches > len(draws) * 0.8:
        findings.append(
            {
                "severity": "warning",
                "topic": "pso_churn",
                "message": (
                    f"{pso_switches} PSO switches for {len(draws)} events "
                    f"({percent(pso_switches, len(draws))}%); sorting draws by material would help."
                ),
            }
        )
    if redundant_pso > 0:
        findings.append(
            {
                "severity": "info",
                "topic": "redundant_binds",
                "message": f"{redundant_pso} consecutive draws reuse the already-bound PSO (no cost, informational).",
            }
        )
    worst = rows[:3]
    for row in worst:
        if row["switches_per_event"] > 0.9 and row["event_count"] > 10:
            findings.append(
                {
                    "severity": "warning",
                    "topic": "pass_state_churn",
                    "message": (
                        f"Pass '{row['pass_name']}' switches pipeline state on nearly every "
                        f"one of its {row['event_count']} events."
                    ),
                }
            )

    total = len(rows)
    window = rows[offset : offset + limit] if limit else rows[offset:]
    return ToolResult.success(
        {
            "frame_totals": {
                "events": len(draws),
                "pipeline_state_switches": pso_switches,
                "root_signature_switches": rootsig_switches,
                "descriptor_heap_switches": heap_switches,
                "redundant_pso_binds": redundant_pso,
                "distinct_pipeline_states": len(
                    {draw.pso_id for draw in draws if draw.pso_id is not None}
                ),
            },
            "by_pass": window,
            "findings": findings,
            **page_envelope(total, offset, limit, len(window)),
        }
    )
