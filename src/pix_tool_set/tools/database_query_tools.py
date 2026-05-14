from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pix_tool_set.capture_db import (
    connect_database,
    database_path,
    load_event,
    load_event_bound_resources,
    load_resource_references,
    load_resource_shader_accesses,
    load_same_named_resource_ids,
    load_shader_source_cache,
)
from pix_tool_set.context import ToolContext
from pix_tool_set.event_analysis import analyze_shader_event_tree_payload, write_event_analysis
from pix_tool_set.indexer import build_index
from pix_tool_set.io_utils import default_output_path, write_json_file
from pix_tool_set.registry import tool
from pix_tool_set.results import ToolResult


DEFAULT_TOP_LIMIT = 20
DEFAULT_SAMPLE_LIMIT = 20


def _optional_int(value: Any, default: int) -> int:
    if value is None:
        return default
    return int(value)


def _ensure_database(export_dir: str | Path, refresh: bool = False) -> tuple[Path, dict[str, Any]]:
    index = build_index(export_dir, refresh=refresh)
    db_path = Path(index.get("database_path") or database_path(export_dir))
    return db_path, index


def _load_all_events(db_path: str | Path, *, shader_only: bool = False) -> list[dict[str, Any]]:
    where = "WHERE is_shader_event = 1" if shader_only else ""
    with connect_database(db_path) as connection:
        rows = connection.execute(
            f"""
            SELECT event_json
            FROM events
            {where}
            ORDER BY event_order
            """
        ).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        try:
            event = json.loads(row["event_json"])
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _node_payload(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "global_id": event.get("global_id"),
        "parent_global_id": event.get("parent_global_id"),
        "name": event.get("name"),
        "event_type": event.get("event_type"),
        "is_shader_event": bool(event.get("is_shader_event")),
        "shader_stage_group": event.get("shader_stage_group"),
        "pso_id": event.get("pso_id"),
        "file": event.get("file"),
        "line": event.get("line"),
        "marker_path": event.get("marker_path", []),
        "children": [],
    }


def _build_shader_event_tree_from_database(db_path: str | Path, index: dict[str, Any]) -> dict[str, Any]:
    all_events = _load_all_events(db_path)
    events_by_gid = {str(event.get("global_id")): event for event in all_events}
    retained: set[str] = set()

    for event in all_events:
        if not event.get("is_shader_event"):
            continue
        current = event
        visited: set[str] = set()
        while current is not None and str(current.get("global_id")) not in visited:
            gid = str(current.get("global_id"))
            retained.add(gid)
            visited.add(gid)
            parent = current.get("parent_global_id")
            current = events_by_gid.get(str(parent)) if parent else None

    nodes = {gid: _node_payload(events_by_gid[gid]) for gid in retained if gid in events_by_gid}
    roots: list[dict[str, Any]] = []
    for gid, node in nodes.items():
        parent = node.get("parent_global_id")
        if parent and str(parent) in nodes:
            nodes[str(parent)]["children"].append(node)
        else:
            roots.append(node)

    def sort_tree(items: list[dict[str, Any]]) -> None:
        items.sort(key=lambda item: int(item.get("global_id") or 0))
        for item in items:
            sort_tree(item["children"])

    sort_tree(roots)
    shader_event_count = sum(1 for event in all_events if event.get("is_shader_event"))
    return {
        "tree": roots,
        "metadata": {
            "export_dir": index["export_dir"],
            "total_events": len(all_events),
            "shader_event_count": shader_event_count,
            "retained_tree_node_count": len(nodes),
            "cache_hit": index.get("cache_hit", False),
            "database_path": str(db_path),
            "query_mode": "sqlite",
        },
    }


def _select_resource(resources: list[dict[str, Any]], selector: str | int) -> dict[str, Any]:
    needle = str(selector).lower()
    matches: list[dict[str, Any]] = []
    for resource in resources:
        candidates = [
            resource.get("resource_id"),
            resource.get("resource_name"),
            resource.get("display_name"),
            resource.get("shader_binding_name"),
            resource.get("binding_name"),
        ]
        if any(str(candidate).lower() == needle for candidate in candidates if candidate is not None):
            matches.append(resource)
            continue
        if any(needle in str(candidate).lower() for candidate in candidates if candidate is not None):
            matches.append(resource)
    if not matches:
        from pix_tool_set.errors import PixToolError

        raise PixToolError(
            code="resource_not_bound",
            message=f"Resource is not bound to the event in the capture database: {selector}",
            stage="database_resource_access_history",
            suggestion="Use db-get-event-resource for the event and choose one of the returned resource names or ids.",
        )
    return matches[0]


def _event_sort_key(event: dict[str, Any]) -> tuple[int, int]:
    try:
        order = int(event.get("event_order"))
    except (TypeError, ValueError):
        try:
            order = int(event.get("global_id"))
        except (TypeError, ValueError):
            order = 0
    try:
        line = int(event.get("line") or 0)
    except (TypeError, ValueError):
        line = 0
    return order, line


def _shader_binding(resource: dict[str, Any]) -> str:
    stage = str(resource.get("stage") or resource.get("shader_stage") or "Shader")
    view_type = str(resource.get("view_type") or "Resource")
    slot = resource.get("shader_binding_slot")
    if slot is None:
        slot = resource.get("binding_slot")
    if slot is None:
        return f"{stage} {view_type}"
    return f"{stage} {view_type} {slot}"


def _read_write_for_view(view_type: str | None) -> str:
    if view_type == "UAV":
        return "Read/Write"
    if view_type == "SRV":
        return "Read"
    return "Unknown"


def _build_access_history_from_database(db_path: str | Path, event: dict[str, Any], target: dict[str, Any]) -> list[dict[str, Any]]:
    resource_ids = load_same_named_resource_ids(db_path, target.get("resource_name"), target.get("resource_id"))
    rows: list[dict[str, Any]] = []

    for ref in load_resource_references(db_path, resource_ids):
        ref_event = ref.get("event") or {}
        rows.append(
            {
                "global_id": ref.get("global_id"),
                "event_name": ref_event.get("name"),
                "event_type": ref_event.get("event_type"),
                "marker_path": ref_event.get("marker_path", []),
                "binding": f"API Parameters [{ref.get('resource_id')}]",
                "read_write": "Unknown",
                "resource_id": ref.get("resource_id"),
                "resource_name": target.get("resource_name"),
                "view_type": None,
                "shader_stage": None,
                "file": ref.get("file"),
                "line": ref.get("line"),
                "text": ref.get("text"),
                "source": "resource_references",
                "event_order": ref.get("event_order"),
            }
        )

    for access in load_resource_shader_accesses(db_path, resource_ids):
        access_event = access.get("event") or {}
        resource = access.get("resource") or {}
        view_type = resource.get("view_type")
        rows.append(
            {
                "global_id": access_event.get("global_id"),
                "event_name": access_event.get("name"),
                "event_type": access_event.get("event_type"),
                "marker_path": access_event.get("marker_path", []),
                "binding": _shader_binding(resource),
                "read_write": _read_write_for_view(view_type),
                "resource_id": resource.get("resource_id"),
                "resource_name": resource.get("resource_name"),
                "view_type": view_type,
                "shader_stage": resource.get("stage"),
                "file": access_event.get("file"),
                "line": access_event.get("line"),
                "text": None,
                "source": "event_bound_resources",
                "event_order": access_event.get("event_order"),
            }
        )

    rows.sort(key=lambda row: (int(row.get("event_order") or 0), int(row.get("line") or 0)))
    unique: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for row in rows:
        key = (row.get("global_id"), row.get("binding"), row.get("resource_id"), row.get("view_type"), row.get("line"), row.get("source"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


@tool(
    name="db-extract-shader-events-tree",
    description="Extract shader-executing events from the capture SQLite database and save a pruned event tree JSON.",
    parameters={
        "type": "object",
        "properties": {
            "capture_path": {"type": "string", "description": "Optional PIX .wpix capture path."},
            "export_dir": {"type": "string", "description": "PIX C++ export directory."},
            "output_path": {"type": "string", "description": "Output JSON path. Defaults to <export_dir>/shader_events_tree.db.json."},
            "auto_export": {"type": "boolean", "description": "Export C++ project with pixtool when missing."},
            "refresh": {"type": "boolean", "description": "Rebuild the index and database even if cache is valid."},
        },
        "additionalProperties": False,
    },
    requires_cpp_export=True,
)
def db_extract_shader_events_tree(args: dict[str, Any], context: ToolContext) -> ToolResult:
    db_path, index = _ensure_database(args["export_dir"], refresh=bool(args.get("refresh", False)))
    payload = _build_shader_event_tree_from_database(db_path, index)
    output_path = args.get("output_path") or str(Path(args["export_dir"]) / "shader_events_tree.db.json")
    written_path = write_json_file(output_path, {"tree": payload["tree"], "metadata": payload["metadata"]})
    return ToolResult.success(
        payload["metadata"],
        output_paths=[written_path],
        diagnostics=[{"stage": "database_shader_event_tree", "database_hit": True, "database_path": str(db_path), "query_mode": "sqlite"}],
    )


@tool(
    name="db-analyze-events",
    description="Analyze shader event statistics directly from the capture SQLite database.",
    parameters={
        "type": "object",
        "properties": {
            "capture_path": {"type": "string", "description": "Optional PIX .wpix capture path."},
            "export_dir": {"type": "string", "description": "PIX C++ export directory."},
            "output_path": {"type": "string", "description": "Optional output JSON path for the event analysis."},
            "auto_export": {"type": "boolean", "description": "Export C++ project with pixtool when missing."},
            "refresh": {"type": "boolean", "description": "Rebuild the index and database even if cache is valid."},
            "top_limit": {"type": "integer", "description": "Maximum number of count rows to return for distributions. Defaults to 20."},
            "sample_limit": {"type": "integer", "description": "Maximum number of PSO and marker path examples to return. Defaults to 20."},
        },
        "additionalProperties": False,
    },
    requires_cpp_export=True,
)
def db_analyze_events(args: dict[str, Any], context: ToolContext) -> ToolResult:
    db_path, index = _ensure_database(args["export_dir"], refresh=bool(args.get("refresh", False)))
    payload = _build_shader_event_tree_from_database(db_path, index)
    analysis = analyze_shader_event_tree_payload(
        payload["tree"],
        metadata=payload["metadata"],
        top_limit=_optional_int(args.get("top_limit"), DEFAULT_TOP_LIMIT),
        sample_limit=_optional_int(args.get("sample_limit"), DEFAULT_SAMPLE_LIMIT),
    )
    output_paths: list[str] = []
    if args.get("output_path"):
        output_paths.append(write_event_analysis(analysis, args["output_path"]))
    return ToolResult.success(
        analysis,
        output_paths=output_paths,
        diagnostics=[{"stage": "database_event_analysis", "database_hit": True, "database_path": str(db_path), "query_mode": "sqlite"}],
    )


@tool(
    name="db-get-event-resource",
    description="Resolve currently bound resources for an event global id directly from the capture SQLite database.",
    parameters={
        "type": "object",
        "properties": {
            "capture_path": {"type": "string", "description": "Optional PIX .wpix capture path."},
            "export_dir": {"type": "string", "description": "PIX C++ export directory."},
            "global_id": {"type": "integer", "description": "Event Global ID."},
            "output_path": {"type": "string", "description": "Optional JSON output path."},
            "auto_export": {"type": "boolean", "description": "Export C++ project with pixtool when missing."},
            "refresh": {"type": "boolean", "description": "Rebuild the index and database even if cache is valid."},
        },
        "required": ["global_id"],
        "additionalProperties": False,
    },
    requires_cpp_export=True,
)
def db_get_event_resource(args: dict[str, Any], context: ToolContext) -> ToolResult:
    db_path, _ = _ensure_database(args["export_dir"], refresh=bool(args.get("refresh", False)))
    event = load_event(db_path, args["global_id"])
    resources = load_event_bound_resources(db_path, args["global_id"])
    payload = {
        "global_id": str(args["global_id"]),
        "status": "success" if event and resources else "partial",
        "event": event,
        "resource_count": len(resources),
        "resources": resources,
        "diagnostics": {
            "database_hit": True,
            "database_path": str(db_path),
            "query_mode": "sqlite",
            "reason": None if event and resources else "No event or bound resources were found in the capture database.",
        },
    }
    filename = "db_resource_" + str(args["global_id"]) + ".json"
    output_path = args.get("output_path") or default_output_path(args["export_dir"], filename)
    written_path = write_json_file(output_path, payload)
    if payload["status"] == "partial":
        return ToolResult.partial(payload, output_paths=[written_path])
    return ToolResult.success(payload, output_paths=[written_path])


@tool(
    name="db-get-resource-access-history",
    description="Export resource access history for a bound event resource directly from the capture SQLite database.",
    parameters={
        "type": "object",
        "properties": {
            "capture_path": {"type": "string", "description": "Optional PIX .wpix capture path."},
            "export_dir": {"type": "string", "description": "PIX C++ export directory."},
            "global_id": {"type": "integer", "description": "Event Global ID used to resolve the bound resource."},
            "resource": {"type": "string", "description": "Resource selector: resource id, resource name, shader binding name, or display name."},
            "output_path": {"type": "string", "description": "Optional JSON output path."},
            "auto_export": {"type": "boolean", "description": "Export C++ project with pixtool when missing."},
            "refresh": {"type": "boolean", "description": "Rebuild the index and database even if cache is valid."},
        },
        "required": ["global_id", "resource"],
        "additionalProperties": False,
    },
    requires_cpp_export=True,
)
def db_get_resource_access_history(args: dict[str, Any], context: ToolContext) -> ToolResult:
    db_path, _ = _ensure_database(args["export_dir"], refresh=bool(args.get("refresh", False)))
    event = load_event(db_path, args["global_id"])
    event_resources = load_event_bound_resources(db_path, args["global_id"])
    target = _select_resource(event_resources, args["resource"])
    rows = _build_access_history_from_database(db_path, event or {}, target)
    payload = {
        "global_id": str(args["global_id"]),
        "status": "success" if rows else "partial",
        "event": event,
        "resource": target,
        "access_count": len(rows),
        "access_history": rows,
        "diagnostics": {
            "database_hit": True,
            "database_path": str(db_path),
            "query_mode": "sqlite",
            "reason": None if rows else "No access history rows were found in the capture database for the selected resource.",
        },
    }
    filename = "db_access_history_" + str(args["global_id"]) + ".json"
    output_path = args.get("output_path") or default_output_path(args["export_dir"], filename)
    written_path = write_json_file(output_path, payload)
    if payload["status"] == "partial":
        return ToolResult.partial(payload, output_paths=[written_path])
    return ToolResult.success(payload, output_paths=[written_path])


@tool(
    name="db-get-event-shader-source",
    description="Read cached shader metadata and resolved HLSL source for an event global id directly from the capture SQLite database.",
    parameters={
        "type": "object",
        "properties": {
            "capture_path": {"type": "string", "description": "Optional PIX .wpix capture path."},
            "export_dir": {"type": "string", "description": "PIX C++ export directory."},
            "global_id": {"type": "integer", "description": "Event Global ID."},
            "output_path": {"type": "string", "description": "Optional JSON output path for full result."},
            "auto_export": {"type": "boolean", "description": "Export C++ project with pixtool when missing."},
            "refresh": {"type": "boolean", "description": "Rebuild the index and database even if cache is valid."},
        },
        "required": ["global_id"],
        "additionalProperties": False,
    },
    requires_cpp_export=True,
)
def db_get_event_shader_source(args: dict[str, Any], context: ToolContext) -> ToolResult:
    db_path, _ = _ensure_database(args["export_dir"], refresh=bool(args.get("refresh", False)))
    event = load_event(db_path, args["global_id"])
    pso_id = event.get("pso_id") if event else None
    stages = load_shader_source_cache(db_path, pso_id)
    payload = {
        "global_id": str(args["global_id"]),
        "event": event,
        "pso_id": pso_id,
        "stage_count": len(stages),
        "stages": stages,
        "diagnostics": {
            "database_hit": True,
            "database_path": str(db_path),
            "query_mode": "sqlite",
            "reason": None if stages else "No resolved shader source cache was found in the capture database for this event PSO.",
        },
    }
    output_paths: list[str] = []
    if args.get("output_path"):
        output_paths.append(write_json_file(args["output_path"], payload))
    return ToolResult.success(payload, output_paths=output_paths)
