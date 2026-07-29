"""Requirement section 8: resource management."""

from __future__ import annotations

from typing import Any

from ..context import ToolContext
from ..errors import not_found
from ..results import ToolResult
from ._common import (
    PAGE_PARAMS,
    page_args,
    page_envelope,
    percent,
    tool,
    with_session,
)


@tool(
    name="list-resources",
    summary=(
        "List every resource (buffers and textures) with descriptor facts, estimated size "
        "and whether the frame touches it."
    ),
    category="resources",
    parameters=with_session(
        PAGE_PARAMS,
        kind={
            "type": "string",
            "enum": ["buffer", "texture1d", "texture2d", "texture3d", "unknown"],
            "description": "Restrict to one resource kind.",
        },
        format={"type": "string", "description": "Substring match on the DXGI format."},
        min_size_bytes={"type": "integer", "description": "Minimum estimated size in bytes."},
        used_only={"type": "boolean", "description": "Only resources referenced by a draw."},
        unused_only={"type": "boolean", "description": "Only resources never referenced."},
        sort_by={
            "type": "string",
            "enum": ["size", "pixels", "id", "width"],
            "description": "Ordering. Default 'size'.",
        },
    ),
    returns="Paged resource list with usage counts.",
    examples=[
        "pix-tool-set list-resources --limit 40",
        "pix-tool-set list-resources --unused-only",
    ],
)
def list_resources(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    offset, limit = page_args(args)
    usage = capture.resource_usage

    unused_only = bool(args.get("unused_only"))
    predicate = (lambda r: r.api_id not in usage) if unused_only else None

    window, total = capture.find_resources(
        kind=args.get("kind"),
        min_size_bytes=int(args.get("min_size_bytes") or 0),
        format_filter=args.get("format"),
        used_only=bool(args.get("used_only")),
        predicate=predicate,
        offset=offset,
        limit=limit,
        sort_by=args.get("sort_by") or "size",
    )

    rows = []
    for resource in window:
        entry = resource.to_dict()
        use = usage.get(resource.api_id)
        entry["usage"] = {
            "referenced": use is not None,
            "read_draws": len(use["read_draws"]) if use else 0,
            "write_draws": len(use["write_draws"]) if use else 0,
            "passes": use["passes"][:8] if use else [],
        }
        rows.append(entry)

    return ToolResult.success(
        {"resources": rows, **page_envelope(total, offset, limit, len(window))}
    )


@tool(
    name="list-buffers",
    summary=(
        "List buffer resources with size and role classification (vertex, index, constant, "
        "structured/UAV) inferred from how the frame binds them."
    ),
    category="resources",
    parameters=with_session(
        PAGE_PARAMS,
        role={
            "type": "string",
            "enum": ["vertex", "index", "constant", "uav", "srv", "unused"],
            "description": "Restrict to buffers used in this role.",
        },
        min_size_bytes={"type": "integer", "description": "Minimum size in bytes."},
        sort_by={
            "type": "string",
            "enum": ["size", "id"],
            "description": "Ordering. Default 'size'.",
        },
    ),
    returns="Paged buffer list with inferred roles.",
    examples=[
        "pix-tool-set list-buffers --limit 30",
        "pix-tool-set list-buffers --role index",
    ],
)
def list_buffers(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    offset, limit = page_args(args)
    usage = capture.resource_usage

    def roles_of(resource_id: int) -> list[str]:
        entry = usage.get(resource_id)
        if entry is None:
            return ["unused"]
        found: list[str] = []
        if entry["vertex_draws"]:
            found.append("vertex")
        if entry["index_draws"]:
            found.append("index")
        if entry["constant_draws"]:
            found.append("constant")
        if entry["write_draws"]:
            found.append("uav")
        if entry["read_draws"] and "vertex" not in found and "index" not in found:
            found.append("srv")
        return found or ["referenced"]

    wanted_role = args.get("role")
    buffers = [r for r in capture.resources.values() if r.is_buffer]
    if args.get("min_size_bytes"):
        threshold = int(args["min_size_bytes"])
        buffers = [r for r in buffers if r.size_bytes >= threshold]

    rows = []
    for resource in buffers:
        found = roles_of(resource.api_id)
        if wanted_role and wanted_role not in found:
            continue
        entry = resource.to_dict()
        entry["roles"] = found
        use = usage.get(resource.api_id)
        entry["usage"] = {
            "vertex_draws": len(use["vertex_draws"]) if use else 0,
            "index_draws": len(use["index_draws"]) if use else 0,
            "constant_draws": len(use["constant_draws"]) if use else 0,
            "read_draws": len(use["read_draws"]) if use else 0,
            "write_draws": len(use["write_draws"]) if use else 0,
        }
        rows.append(entry)

    sort_by = args.get("sort_by") or "size"
    rows.sort(key=lambda entry: (-entry["size_bytes"] if sort_by == "size" else entry["resource_id"]))

    total = len(rows)
    window = rows[offset : offset + limit] if limit else rows[offset:]
    total_bytes = sum(entry["size_bytes"] for entry in rows)
    return ToolResult.success(
        {
            "buffers": window,
            "totals": {
                "count": total,
                "estimated_bytes": total_bytes,
                "estimated_mib": round(total_bytes / 1048576.0, 2),
            },
            **page_envelope(total, offset, limit, len(window)),
        }
    )


@tool(
    name="resource-usage",
    summary=(
        "Full usage history of one resource: every draw that reads or writes it, the passes "
        "involved, the descriptors that point at it, and a read/write timeline."
    ),
    category="resources",
    parameters=with_session(
        resource_id={"type": "integer", "description": "Resource id to trace."},
        max_events={"type": "integer", "description": "Cap on timeline entries. Default 60."},
        include_views={"type": "boolean", "description": "Include descriptor/view list. Default true."},
        required=["resource_id"],
    ),
    returns="Read/write timeline, pass list, descriptors and hazard hints.",
    examples=["pix-tool-set resource-usage --resource-id 641"],
    aliases=["resource-history"],
)
def resource_usage(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    resource_id = int(args["resource_id"])
    resource = capture.resource(resource_id)
    if resource is None:
        raise not_found("resource", resource_id, "Run list-resources to find valid ids.")

    entry = capture.resource_usage.get(resource_id)
    max_events = int(args.get("max_events") or 60)

    timeline: list[dict[str, Any]] = []
    if entry:
        marks: dict[int, set[str]] = {}
        for index in entry["read_draws"]:
            marks.setdefault(index, set()).add("read")
        for index in entry["write_draws"]:
            marks.setdefault(index, set()).add("write")
        for index in entry["render_target_draws"]:
            marks.setdefault(index, set()).add("render_target")
        for index in entry["depth_draws"]:
            marks.setdefault(index, set()).add("depth")
        for index in entry["vertex_draws"]:
            marks.setdefault(index, set()).add("vertex")
        for index in entry["index_draws"]:
            marks.setdefault(index, set()).add("index")
        for index in entry["constant_draws"]:
            marks.setdefault(index, set()).add("constant")

        for index in sorted(marks)[:max_events]:
            draw = capture.draw_calls[index]
            timeline.append(
                {
                    "draw_index": draw.index,
                    "global_id": draw.global_id,
                    "api": draw.api,
                    "pass_name": draw.pass_name,
                    "access": sorted(marks[index]),
                }
            )

    views: list[dict[str, Any]] = []
    include_views = args.get("include_views")
    if include_views is None or bool(include_views):
        views = [
            {**view.to_dict(), "heap": key[0], "index": key[1]}
            for key, view in capture.views.items()
            if view.resource_id == resource_id
        ]

    write_indices = entry["write_draws"] if entry else []
    read_indices = entry["read_draws"] if entry else []
    hazards: list[dict[str, Any]] = []
    for write_index in write_indices:
        following_reads = [index for index in read_indices if index > write_index]
        if following_reads:
            hazards.append(
                {
                    "type": "write_then_read",
                    "write_draw": write_index,
                    "next_read_draw": following_reads[0],
                }
            )
    interleaved = [
        index for index in read_indices if index in set(write_indices)
    ]

    data = {
        "resource": resource.to_dict(),
        "referenced": entry is not None,
        "summary": {
            "read_draw_count": len(read_indices),
            "write_draw_count": len(write_indices),
            "render_target_draw_count": len(entry["render_target_draws"]) if entry else 0,
            "depth_draw_count": len(entry["depth_draws"]) if entry else 0,
            "pass_count": len(entry["passes"]) if entry else 0,
            "descriptor_count": len(views),
            "read_write_same_draw": sorted(set(interleaved))[:20],
        },
        "passes": entry["passes"] if entry else [],
        "timeline": timeline,
        "timeline_truncated": bool(entry) and len(timeline) < (
            len(set(read_indices) | set(write_indices))
        ),
        "views": views[:40],
        "hazards": hazards[:20],
    }
    result = ToolResult.success(data)
    if entry is None:
        result.degrade("This resource is never referenced by a draw in the captured frame.")
    return result
