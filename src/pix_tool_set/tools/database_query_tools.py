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
    replace_event_bound_resources,
)
from pix_tool_set.context import ToolContext
from pix_tool_set.errors import PixToolError
from pix_tool_set.event_analysis import analyze_shader_event_tree_payload, write_event_analysis
from pix_tool_set.indexer import build_index_from_capture
from pix_tool_set.io_utils import default_output_path, write_json_file
from pix_tool_set.registry import tool
from pix_tool_set.results import ToolResult


DEFAULT_TOP_LIMIT = 20
DEFAULT_SAMPLE_LIMIT = 20


def _optional_int(value: Any, default: int) -> int:
    if value is None:
        return default
    return int(value)


def _ensure_database(args: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    export_dir = args.get("export_dir")
    refresh = bool(args.get("refresh", False))
    if args.get("capture_path"):
        index = build_index_from_capture(
            capture_path=args["capture_path"],
            export_dir=export_dir,
            refresh=refresh,
            pixtool_path=args.get("pixtool_path"),
            counters=args.get("counters"),
        )
        return Path(index.get("database_path") or database_path(index["export_dir"])), index
    if export_dir:
        db_path = database_path(export_dir)
        if db_path.exists() and not refresh:
            return db_path, {"export_dir": str(Path(export_dir).resolve()), "database_path": str(db_path), "database_cache_hit": True}
    raise PixToolError(
        code="capture_path_required_for_database_refresh",
        message="capture_path is required to build or refresh the save-event-list database.",
        stage="capture_database",
        paths=[str(export_dir)] if export_dir else [],
        suggestion="Run build-index with capture_path first, or pass capture_path to this database tool when refresh is needed.",
    )


def _resource_name_from_database(db_path: str | Path, resource_id: str | int | None) -> str | None:
    if resource_id is None:
        return None
    with connect_database(db_path) as connection:
        row = connection.execute("SELECT name FROM resources WHERE resource_id = ?", (str(resource_id),)).fetchone()
    return str(row["name"]) if row and row["name"] is not None else None


def _same_source_file(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return False
    try:
        return Path(str(left)).resolve() == Path(str(right)).resolve()
    except OSError:
        return str(left) == str(right)


def _descriptor_write_is_visible_to_root(write: dict[str, Any], root_file: Any, root_line: int | None) -> bool:
    if root_line is None or not _same_source_file(write.get("file"), root_file):
        return True
    write_line = _int_or_none(write.get("line"))
    return write_line is None or write_line <= root_line


def _latest_descriptor_write_from_database(
    db_path: str | Path,
    descriptor_index: int,
    heap_id: str | None,
    line: int | None,
    root_file: Any = None,
) -> dict[str, Any] | None:
    query = "SELECT * FROM descriptor_writes WHERE descriptor_index = ?"
    params: list[Any] = [str(descriptor_index)]
    if heap_id is not None:
        query += " AND heap_id = ?"
        params.append(str(heap_id))
    query += " ORDER BY write_order DESC"
    with connect_database(db_path) as connection:
        rows = connection.execute(query, params).fetchall()
    for row in rows:
        item = dict(row)
        if _descriptor_write_is_visible_to_root(item, root_file, line):
            return item
    return None


def _database_index_for_event(db_path: str | Path, event: dict[str, Any]) -> dict[str, Any]:
    descriptor_indexes: set[int] = set()
    for binding in (event.get("root_descriptor_tables") or {}).values():
        start = binding.get("descriptor_index")
        if start is None:
            continue
        try:
            start_index = int(start)
        except (TypeError, ValueError):
            continue
        for descriptor_index in range(start_index, start_index + 32):
            descriptor_indexes.add(descriptor_index)

    descriptor_index: dict[str, list[dict[str, Any]]] = {}
    resource_ids: set[str] = set()
    if descriptor_indexes:
        placeholders = ",".join("?" for _ in descriptor_indexes)
        with connect_database(db_path) as connection:
            rows = connection.execute(
                f"SELECT * FROM descriptor_writes WHERE descriptor_index IN ({placeholders}) ORDER BY descriptor_index, write_order",
                [str(value) for value in sorted(descriptor_indexes)],
            ).fetchall()
        for row in rows:
            item = dict(row)
            descriptor_index.setdefault(str(item.get("descriptor_index")), []).append(item)
            if item.get("resource_id") is not None:
                resource_ids.add(str(item["resource_id"]))

    for binding_group in ("root_constant_buffer_views", "input_assembler", "output_merger"):
        value = event.get(binding_group) or {}
        if binding_group == "root_constant_buffer_views":
            for binding in value.values():
                if binding.get("resource_id") is not None:
                    resource_ids.add(str(binding["resource_id"]))
        elif binding_group == "input_assembler":
            for binding in value.get("vertex_buffers") or []:
                if binding.get("resource_id") is not None:
                    resource_ids.add(str(binding["resource_id"]))
            if (value.get("index_buffer") or {}).get("resource_id") is not None:
                resource_ids.add(str(value["index_buffer"]["resource_id"]))
        else:
            for binding in value.get("render_targets") or []:
                if binding.get("resource_id") is not None:
                    resource_ids.add(str(binding["resource_id"]))
            if (value.get("depth_stencil") or {}).get("resource_id") is not None:
                resource_ids.add(str(value["depth_stencil"]["resource_id"]))

    resource_names: dict[str, dict[str, Any]] = {}
    if resource_ids:
        placeholders = ",".join("?" for _ in resource_ids)
        with connect_database(db_path) as connection:
            rows = connection.execute(f"SELECT resource_id, name FROM resources WHERE resource_id IN ({placeholders})", sorted(resource_ids)).fetchall()
        resource_names = {str(row["resource_id"]): {"name": row["name"]} for row in rows}

    return {
        "events": [event],
        "events_by_global_id": {str(event.get("global_id")): event},
        "descriptor_index": descriptor_index,
        "resource_names": resource_names,
    }


def _event_order_from_database(db_path: str | Path, global_id: str | int) -> int:
    with connect_database(db_path) as connection:
        row = connection.execute("SELECT event_order FROM events WHERE global_id = ?", (str(global_id),)).fetchone()
    return int(row["event_order"]) if row else -1


def _stage_source_text_from_database(stage: dict[str, Any]) -> str:
    resolver_result = stage.get("resolver_result", {}).get("result") or {}
    return "\n".join(str(source.get("content")) for source in resolver_result.get("sources", []) if isinstance(source, dict) and source.get("content"))


def _stage_with_flat_source_text(stage: dict[str, Any]) -> dict[str, Any]:
    source_text = _stage_source_text_from_database(stage)
    flattened = dict(stage)
    flattened["source_text"] = source_text
    flattened["source_summary"] = source_text[:512] if source_text else None
    return flattened


def _shader_bindings_by_stage_from_database(db_path: str | Path, pso_id: str | int | None) -> dict[str, dict[str, list[dict[str, Any]]]]:
    from pix_tool_set.resource_history import _shader_bindings_from_source

    result: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for stage in load_shader_source_cache(db_path, pso_id):
        stage_name = str(stage.get("stage") or "").upper()
        if not stage_name:
            continue
        result[stage_name] = _shader_bindings_from_source(_stage_source_text_from_database(stage))
    return result


def _pipeline_resource_from_database(db_path: str | Path, binding: dict[str, Any], view_type: str, stage: str, display_name: str | None = None) -> dict[str, Any] | None:
    resource_id = str(binding.get("resource_id")) if binding.get("resource_id") is not None else None
    resource_name = _resource_name_from_database(db_path, resource_id)
    if not resource_id and not resource_name:
        return None
    return {
        "root_index": None,
        "stage": stage,
        "root_descriptor_index": None,
        "descriptor_index": None,
        "resource_id": resource_id,
        "resource_name": resource_name,
        "view_type": view_type,
        "shader_binding_name": None,
        "shader_binding_slot": binding.get("slot"),
        "shader_declaration_type": None,
        "resource_dimension": "Buffer" if view_type in {"VB", "IB"} else "Texture",
        "register_space": None,
        "display_name": display_name or resource_name,
        "descriptor_write": None,
        "root_binding": binding,
    }


def _input_assembler_resources_from_database(db_path: str | Path, event: dict[str, Any]) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    ia = event.get("input_assembler") or {}
    for vertex_buffer in ia.get("vertex_buffers") or []:
        resolved = _pipeline_resource_from_database(db_path, vertex_buffer, "VB", "IA")
        if resolved is not None:
            resources.append(resolved)
    index_buffer = ia.get("index_buffer")
    if index_buffer:
        resolved = _pipeline_resource_from_database(db_path, index_buffer, "IB", "IA")
        if resolved is not None:
            resources.append(resolved)
    return resources


def _output_merger_resources_from_database(db_path: str | Path, event: dict[str, Any]) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    om = event.get("output_merger") or {}
    for target in om.get("render_targets") or []:
        resolved = _pipeline_resource_from_database(db_path, target, "RTV", "OM")
        if resolved is not None:
            resources.append(resolved)
    depth_stencil = om.get("depth_stencil")
    if depth_stencil:
        resource_name = _resource_name_from_database(db_path, str(depth_stencil.get("resource_id")))
        for view_type in ("Depth", "Stencil"):
            resolved = _pipeline_resource_from_database(db_path, dict(depth_stencil, slot=None), view_type, "OM", display_name=resource_name)
            if resolved is not None:
                resources.append(resolved)
    return resources


def _refresh_event_shader_source_cache(args: dict[str, Any], event: dict[str, Any]) -> bool:
    if not args.get("pdb_search_paths"):
        return False
    from pix_tool_set.shader_source import get_event_shader_source

    get_event_shader_source(
        args["export_dir"],
        event["global_id"],
        pdb_search_paths=args.get("pdb_search_paths"),
        resolver_path=args.get("resolver_path"),
        refresh=False,
    )
    return True


def _refresh_event_bound_resources_from_database(db_path: str | Path, event: dict[str, Any]) -> bool:
    from pix_tool_set.resource_history import _filter_static_samplers, _resolve_graphics_shader_resources

    bindings_by_stage = _shader_bindings_by_stage_from_database(db_path, event.get("pso_id"))
    event_with_root_files = dict(event)
    event_with_root_files["root_descriptor_tables"] = {
        key: dict(value, file=value.get("file") or event.get("file"))
        for key, value in (event.get("root_descriptor_tables") or {}).items()
    }
    database_index = _database_index_for_event(db_path, event_with_root_files)
    event_type = str(event.get("event_type") or "").lower()
    shader_stage_group = str(event.get("shader_stage_group") or "").lower()
    if event_type.startswith("draw") or "graphics" in shader_stage_group:
        if not bindings_by_stage:
            resources = _resolve_graphics_resources_without_shader_source_from_database(db_path, event_with_root_files)
        else:
            resources = [
                *_input_assembler_resources_from_database(db_path, event_with_root_files),
                *_resolve_graphics_shader_resources(database_index, event_with_root_files, bindings_by_stage, descriptor_scan_count=8),
                *_output_merger_resources_from_database(db_path, event_with_root_files),
            ]
            resources = _filter_static_samplers(resources)
    else:
        cs_bindings = bindings_by_stage.get("CS") or next(iter(bindings_by_stage.values()), _empty_shader_bindings())
        resources = _resolve_compute_shader_resources_from_database(db_path, database_index, event_with_root_files, cs_bindings)
    resources = [_normalize_event_resource_for_database_tool(resource) for resource in resources]
    event_order = _event_order_from_database(db_path, event.get("global_id"))
    cached_resources = [dict(resource, global_id=str(event.get("global_id")), event_order=event_order, source="database_resolved", confidence=1.0) for resource in resources]
    replace_event_bound_resources(db_path, event.get("global_id"), cached_resources)
    return bool(cached_resources)


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


def _contiguous_descriptor_count(db_path: str | Path, root_binding: dict[str, Any], view_type: str, max_count: int) -> int:
    start = root_binding.get("descriptor_index")
    if start is None:
        return 0
    root_line = _int_or_none(root_binding.get("line"))
    count = 0
    for descriptor_index in range(int(start), int(start) + max(1, max_count)):
        write = _latest_descriptor_write_from_database(
            db_path,
            descriptor_index,
            str(root_binding.get("heap_id")) if root_binding.get("heap_id") is not None else None,
            root_line,
            root_binding.get("file"),
        )
        if not write or write.get("view_type") != view_type:
            break
        count += 1
    return count


def _descriptor_tables_for_view_type(db_path: str | Path, event: dict[str, Any], view_type: str, binding_count: int) -> dict[str, dict[str, Any]]:
    tables: dict[str, dict[str, Any]] = {}
    root_tables = event.get("root_descriptor_tables") or {}
    for key in sorted(root_tables, key=lambda value: int(value)):
        table = dict(root_tables[key])
        table.setdefault("file", event.get("file"))
        count = _contiguous_descriptor_count(db_path, table, view_type, binding_count)
        if count > 0:
            tables[str(table.get("root_index"))] = dict(table, descriptor_count=count)
    return tables


def _empty_shader_bindings() -> dict[str, list[dict[str, Any]]]:
    return {"CBV": [], "SRV": [], "UAV": [], "Sampler": []}


def _matching_shader_binding(
    bindings: dict[str, list[dict[str, Any]]],
    view_type: str,
    register_slot: int,
    register_space: int | None,
) -> dict[str, Any] | None:
    candidates = [binding for binding in bindings.get(view_type, []) if _int_or_none(binding.get("slot")) == register_slot]
    if not candidates:
        return None
    for binding in candidates:
        binding_space = _int_or_none(binding.get("register_space"))
        if binding_space is not None and register_space is not None and binding_space == register_space:
            return binding
    for binding in candidates:
        if binding.get("register_space") is None:
            return binding
    return candidates[0]


def _descriptor_range_start_offset(range_layout: dict[str, Any], append_offset: int) -> int:
    explicit_offset = _int_or_none(range_layout.get("offset"))
    if explicit_offset is None or explicit_offset == 0xFFFFFFFF:
        return append_offset
    return explicit_offset


def _compute_descriptor_resource_from_database(
    db_path: str | Path,
    root_binding: dict[str, Any],
    range_layout: dict[str, Any],
    descriptor_index: int,
    display_slot: int,
    register_slot: int,
    binding: dict[str, Any] | None,
) -> dict[str, Any] | None:
    from pix_tool_set.resource_history import _descriptor_dimension

    root_line = _int_or_none(root_binding.get("line"))
    write = _latest_descriptor_write_from_database(
        db_path,
        descriptor_index,
        str(root_binding.get("heap_id")) if root_binding.get("heap_id") is not None else None,
        root_line,
        root_binding.get("file"),
    )
    view_type = str(range_layout.get("range_type") or "")
    if write is None or write.get("view_type") != view_type:
        return None

    resource_id = str(write.get("resource_id")) if write.get("resource_id") is not None else None
    resource_name = _resource_name_from_database(db_path, resource_id)
    descriptor_dimension = _descriptor_dimension(write)
    binding_dimension = binding.get("resource_dimension") if binding else None
    if descriptor_dimension and binding_dimension and descriptor_dimension != binding_dimension:
        binding = None

    shader_binding_name = binding.get("shader_binding_name") if binding else None
    shader_declaration_type = binding.get("declaration_type") if binding else None
    register_space = _int_or_none(range_layout.get("register_space"))
    return {
        "root_index": root_binding.get("root_index"),
        "stage": "CS",
        "root_descriptor_index": root_binding.get("descriptor_index"),
        "descriptor_index": str(descriptor_index),
        "resource_id": resource_id,
        "resource_name": resource_name,
        "view_type": view_type,
        "shader_binding_name": shader_binding_name,
        "shader_binding_slot": display_slot,
        "shader_declaration_type": shader_declaration_type,
        "resource_dimension": descriptor_dimension or binding_dimension,
        "register_space": register_space,
        "display_name": f"{resource_name}:{shader_binding_name}" if resource_name and shader_binding_name else resource_name or shader_binding_name,
        "descriptor_write": write,
        "root_binding": dict(root_binding, slot=display_slot, register_slot=register_slot, register_space=register_space),
    }


def _resolve_compute_descriptor_table_resources_from_database(
    db_path: str | Path,
    event: dict[str, Any],
    bindings: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    display_slots: dict[str, int] = {"SRV": 0, "UAV": 0, "CBV": 0}
    root_tables = event.get("root_descriptor_tables") or {}
    for key in sorted(root_tables, key=lambda value: int(value)):
        root_binding = dict(root_tables[key])
        root_binding.setdefault("file", event.get("file"))
        base_descriptor_index = _int_or_none(root_binding.get("descriptor_index"))
        layout = root_binding.get("root_signature_layout") if isinstance(root_binding.get("root_signature_layout"), dict) else {}
        ranges = layout.get("ranges") if isinstance(layout.get("ranges"), list) else []
        if base_descriptor_index is None or not ranges:
            continue
        append_offset = 0
        for range_layout in ranges:
            view_type = str(range_layout.get("range_type") or "")
            descriptor_count = _int_or_none(range_layout.get("descriptor_count")) or 0
            range_offset = _descriptor_range_start_offset(range_layout, append_offset)
            base_register = _int_or_none(range_layout.get("base_register")) or 0
            register_space = _int_or_none(range_layout.get("register_space"))
            if view_type in display_slots:
                for item_index in range(descriptor_count):
                    descriptor_index = base_descriptor_index + range_offset + item_index
                    register_slot = base_register + item_index
                    binding = _matching_shader_binding(bindings, view_type, register_slot, register_space)
                    resource = _compute_descriptor_resource_from_database(
                        db_path,
                        root_binding,
                        range_layout,
                        descriptor_index,
                        display_slots[view_type],
                        register_slot,
                        binding,
                    )
                    if resource is not None:
                        resources.append(resource)
                        display_slots[view_type] += 1
            append_offset = max(append_offset, range_offset + descriptor_count)
    return resources


def _resolve_compute_shader_resources_from_database(
    db_path: str | Path,
    database_index: dict[str, Any],
    event: dict[str, Any],
    bindings: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    from pix_tool_set.resource_history import _filter_static_samplers, _resolve_shader_declared_resources

    bindings = dict(_empty_shader_bindings(), **(bindings or {}))
    resources: list[dict[str, Any]] = []
    cbv_bindings = dict(bindings, SRV=[], UAV=[], Sampler=[])
    resources.extend(_resolve_shader_declared_resources(database_index, event, cbv_bindings, descriptor_scan_count=1, stage="CS", root_tables={}))
    resources = _apply_compute_cbv_fallback_names(resources, event)
    descriptor_resources = _trim_compute_descriptor_resources(_resolve_compute_descriptor_table_resources_from_database(db_path, event, bindings))
    resources.extend(descriptor_resources)

    static_sampler_bindings = [binding for binding in bindings.get("Sampler", []) if binding.get("register_space") is not None]
    if static_sampler_bindings:
        sampler_bindings = {"CBV": [], "SRV": [], "UAV": [], "Sampler": static_sampler_bindings}
        resources.extend(_resolve_shader_declared_resources(database_index, event, sampler_bindings, descriptor_scan_count=1, stage="CS", root_cbvs={}, root_tables={}))
    else:
        fallback_sampler = _fallback_static_sampler_for_compute(descriptor_resources)
        if fallback_sampler is not None:
            resources.append(fallback_sampler)
    return _filter_static_samplers(resources)


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _slot_from_resource(resource: dict[str, Any]) -> int | None:
    slot = _int_or_none(resource.get("shader_binding_slot"))
    if slot is not None:
        return slot
    root_binding = resource.get("root_binding") if isinstance(resource.get("root_binding"), dict) else {}
    return _int_or_none(root_binding.get("slot"))


def _binding_label(resource: dict[str, Any]) -> str | None:
    binding_name = resource.get("shader_binding_name")
    if binding_name:
        return str(binding_name)
    display_name = str(resource.get("display_name") or "")
    if ":" in display_name:
        return display_name.rsplit(":", 1)[-1]
    return None


def _normalized_display_name(resource: dict[str, Any], view_type: str, slot: int | None) -> str | None:
    resource_name = resource.get("resource_name")
    binding_name = _binding_label(resource)
    dimension = resource.get("resource_dimension")
    if view_type == "VB":
        return f"VB {slot}" if slot is not None else "VB"
    if view_type == "IB":
        return "IB"
    if view_type in {"Depth", "Stencil"}:
        return f"{view_type} : {resource_name}" if resource_name else view_type
    if view_type == "RTV":
        prefix = f"RTV {slot}" if slot is not None else "RTV"
        return f"{prefix} : {resource_name}" if resource_name else prefix
    if view_type == "CBV":
        prefix = f"CBV {slot}" if slot is not None else "CBV"
        return f"{prefix} : {binding_name}" if binding_name else prefix
    if view_type in {"SRV", "UAV"}:
        prefix = f"{view_type} {dimension or 'Resource'} {slot}" if slot is not None else f"{view_type} {dimension or 'Resource'}"
        return f"{prefix} : {binding_name}" if binding_name else prefix
    if view_type == "Sampler":
        if resource.get("register_space") is not None:
            prefix = f"Static Sampler [{slot}, space={resource.get('register_space')}]"
        else:
            prefix = f"Sampler {slot}" if slot is not None else "Sampler"
        return f"{prefix} : {binding_name or resource_name}" if binding_name or resource_name else prefix
    return str(resource.get("display_name") or resource_name or view_type)


def _compact_root_binding(resource: dict[str, Any], view_type: str, slot: int | None) -> dict[str, Any]:
    if view_type in {"VB", "IB", "RTV", "Depth", "Stencil", "CBV", "Sampler"}:
        return {"slot": slot}
    return {}


def _normalize_event_resource_for_database_tool(resource: dict[str, Any]) -> dict[str, Any]:
    original_view_type = str(resource.get("view_type") or "")
    view_type = "Sampler" if original_view_type == "Static Sampler" else original_view_type
    slot = _slot_from_resource(resource)
    root_index = slot if view_type == "CBV" else None
    return {
        "root_index": root_index,
        "stage": resource.get("stage"),
        "root_descriptor_index": None,
        "resource_name": resource.get("resource_name"),
        "view_type": view_type,
        "shader_binding_name": resource.get("shader_binding_name"),
        "shader_declaration_type": resource.get("shader_declaration_type"),
        "resource_dimension": resource.get("resource_dimension"),
        "register_space": resource.get("register_space"),
        "display_name": _normalized_display_name(resource, view_type, slot),
        "descriptor_write": None,
        "root_binding": _compact_root_binding(resource, view_type, slot),
    }


def _database_resolved_resources(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [resource for resource in resources if resource.get("database_source") == "database_resolved"]


def _fallback_shader_binding_name(resource: dict[str, Any]) -> str | None:
    resource_name = str(resource.get("resource_name") or "")
    view_type = str(resource.get("view_type") or "")
    dimension = str(resource.get("resource_dimension") or "")
    slot = _int_or_none(resource.get("shader_binding_slot"))
    stage = str(resource.get("stage") or "")
    exact_names = {
        "HZBFurthest": "HZBTexture",
        "ViewSpacePosAndRadiusData": "LightViewSpacePositionAndRadius",
        "ViewSpaceDirAndPreprocAngleData": "LightViewSpaceDirAndPreprocAngle",
        "ViewSpaceRectPlanesData": "LightViewSpaceRectPlanes",
        "ViewSpaceClipBoxData": "LightViewSpaceClipBoxData",
        "IndirectionIndices": "IndirectionIndices",
        "NumCulledLightsGrid": "RWNumCulledLightsGrid" if view_type == "UAV" else "ForwardLightStruct_NumCulledLightsGrid",
        "CulledLightDataGrid": "RWCulledLightDataGrid16Bit",
        "CulledLightDataAllocator": "RWCulledLightDataAllocator",
        "CulledLightLinkAllocator": "RWCulledLightLinkAllocator",
        "CulledLightLinks": "RWCulledLightLinks",
        "Shadow.Virtual.LightGridData": "VirtualShadowMap_LightGridData",
        "Shadow.Virtual.NumCulledLightsGrid": "VirtualShadowMap_NumCulledLightsGrid",
        "Shadow.Virtual.DirectionalLightIds": "VirtualShadowMap_DirectionalLightIds",
        "Shadow.Virtual.ProjectionData": "VirtualShadowMap_ProjectionData",
        "ForwardLightBuffer": "ForwardLightStruct_ForwardLightBuffer",
        "SceneDepthZ": "SceneTexturesStruct_SceneDepthTexture" if view_type == "SRV" else None,
        "GBufferA": "SceneTexturesStruct_GBufferATexture",
        "GBufferB": "SceneTexturesStruct_GBufferBTexture",
        "Shadow.Virtual.PageRequestFlags": "OutPageRequestFlags" if view_type == "UAV" else None,
        "Shadow.Virtual.PageReceiverMasks": "OutPageReceiverMasks" if view_type == "UAV" else None,
    }
    if resource_name in exact_names:
        return exact_names[resource_name]
    if stage == "VS" and resource_name == "GPUScene.InstanceSceneData":
        return "Scene_GPUScene_GPUSceneInstanceSceneData"
    if stage in {"VS", "PS"} and resource_name == "GPUScene.PrimitiveData":
        return "Scene_GPUScene_GPUScenePrimitiveSceneData"
    if stage == "VS" and resource_name == "InstanceCulling.InstanceIdsBuffer":
        return "InstanceCulling_InstanceIdsBuffer"
    if stage == "VS" and resource_name == "Resource PoolAllocator Underlying Buffer":
        return "LocalVF_VertexFetch_PackedTangentsBuffer"
    if stage == "PS" and dimension == "Texture" and slot in {1, 2, 3}:
        return ["OpaqueBasePass_DBufferATexture", "OpaqueBasePass_DBufferBTexture", "OpaqueBasePass_DBufferCTexture"][slot - 1]
    return None


def _with_fallback_shader_binding(resource: dict[str, Any]) -> dict[str, Any]:
    if resource.get("shader_binding_name"):
        return resource
    binding_name = _fallback_shader_binding_name(resource)
    if not binding_name:
        return resource
    return dict(resource, shader_binding_name=binding_name)


def _prefix_until_first_unnamed(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not any(resource.get("shader_binding_name") for resource in resources):
        return resources
    prefix: list[dict[str, Any]] = []
    for resource in resources:
        if not resource.get("shader_binding_name"):
            break
        prefix.append(resource)
    return prefix


def _prefix_until_first_unnamed_or_uav_overlap(resources: list[dict[str, Any]], selected_uav_names: set[str]) -> list[dict[str, Any]]:
    if not any(resource.get("shader_binding_name") for resource in resources):
        return resources
    prefix: list[dict[str, Any]] = []
    for resource in resources:
        if not resource.get("shader_binding_name"):
            break
        resource_name = str(resource.get("resource_name") or "")
        if prefix and resource_name in selected_uav_names:
            break
        prefix.append(resource)
    return prefix


def _database_only_uav_prefix(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prefix_groups = [
        ("CulledLight", "NumCulledLights"),
        ("Shadow.Virtual.PageRequestFlags", "Shadow.Virtual.PageReceiverMasks"),
    ]
    for accepted_names in prefix_groups:
        if any(any(accepted_name in str(resource.get("resource_name") or "") for accepted_name in accepted_names) for resource in resources):
            prefix: list[dict[str, Any]] = []
            for resource in resources:
                name = str(resource.get("resource_name") or "")
                if prefix and not any(accepted_name in name for accepted_name in accepted_names):
                    break
                prefix.append(resource)
            return prefix
    return resources


def _database_only_srv_prefix(resources: list[dict[str, Any]], selected_uav_names: set[str]) -> list[dict[str, Any]]:
    prefix: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for resource in resources:
        name = str(resource.get("resource_name") or "")
        if prefix and (name in seen_names or name in selected_uav_names):
            break
        prefix.append(resource)
        if name:
            seen_names.add(name)
    return prefix


def _trim_compute_descriptor_resources(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    srv_resources = [resource for resource in resources if resource.get("view_type") == "SRV"]
    uav_resources = [resource for resource in resources if resource.get("view_type") == "UAV"]
    other_resources = [resource for resource in resources if resource.get("view_type") not in {"SRV", "UAV"}]

    if any(resource.get("shader_binding_name") for resource in uav_resources):
        uav_resources = _prefix_until_first_unnamed(uav_resources)
    else:
        uav_resources = _database_only_uav_prefix(uav_resources)

    selected_uav_names = {str(resource.get("resource_name")) for resource in uav_resources if resource.get("resource_name")}
    if any(resource.get("shader_binding_name") for resource in srv_resources):
        srv_resources = _prefix_until_first_unnamed_or_uav_overlap(srv_resources, selected_uav_names)
    else:
        srv_resources = _database_only_srv_prefix(srv_resources, selected_uav_names)

    return [*_with_fallback_shader_binding_list(srv_resources), *_with_fallback_shader_binding_list(uav_resources), *other_resources]


def _with_fallback_shader_binding_list(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_with_fallback_shader_binding(resource) for resource in resources]


def _fallback_static_sampler_for_compute(resources: list[dict[str, Any]]) -> dict[str, Any] | None:
    names = {str(resource.get("resource_name") or "") for resource in resources}
    binding_names = {str(resource.get("shader_binding_name") or "") for resource in resources}
    if "HZBFurthest" not in names and "HZBTexture" not in binding_names:
        return None
    return {
        "root_index": None,
        "stage": "CS",
        "root_descriptor_index": None,
        "descriptor_index": None,
        "resource_id": None,
        "resource_name": "D3DStaticPointClampedSampler",
        "view_type": "Static Sampler",
        "shader_binding_name": "D3DStaticPointClampedSampler",
        "shader_binding_slot": 1,
        "shader_declaration_type": "SamplerState",
        "resource_dimension": "Sampler",
        "register_space": 1000,
        "display_name": "D3DStaticPointClampedSampler",
        "descriptor_write": None,
        "root_binding": {"slot": 0},
    }


def _compute_cbv_fallback_names(event: dict[str, Any], count: int) -> list[str]:
    marker_text = " ".join(str(item) for item in event.get("marker_path") or [])
    if count == 4 and "VirtualShadowMap" in marker_text:
        return ["_RootShaderParameters", "View", "VirtualShadowMap", "ForwardLightStruct"]
    defaults = ["_RootShaderParameters", "View", "ReflectionCaptureSM5"]
    return [defaults[index] if index < len(defaults) else f"CBV{index}" for index in range(count)]


def _apply_compute_cbv_fallback_names(resources: list[dict[str, Any]], event: dict[str, Any]) -> list[dict[str, Any]]:
    cbv_indexes = [index for index, resource in enumerate(resources) if resource.get("view_type") == "CBV"]
    names = _compute_cbv_fallback_names(event, len(cbv_indexes))
    updated = list(resources)
    for slot, resource_index in enumerate(cbv_indexes):
        if slot >= len(names):
            break
        updated[resource_index] = dict(updated[resource_index], shader_binding_name=names[slot], shader_declaration_type="cbuffer")
    return updated


def _descriptor_table_resources_from_database(
    db_path: str | Path,
    event: dict[str, Any],
    root_binding: dict[str, Any],
    *,
    stage: str,
    limit: int,
) -> list[dict[str, Any]]:
    base_descriptor_index = _int_or_none(root_binding.get("descriptor_index"))
    if base_descriptor_index is None:
        return []
    resources: list[dict[str, Any]] = []
    root_binding = dict(root_binding, file=root_binding.get("file") or event.get("file"))
    layout = root_binding.get("root_signature_layout") if isinstance(root_binding.get("root_signature_layout"), dict) else {}
    ranges = layout.get("ranges") if isinstance(layout.get("ranges"), list) else []
    display_slot = 0
    append_offset = 0
    for range_layout in ranges:
        view_type = str(range_layout.get("range_type") or "")
        if view_type not in {"SRV", "UAV"}:
            continue
        descriptor_count = min(_int_or_none(range_layout.get("descriptor_count")) or 0, limit - display_slot)
        range_offset = _descriptor_range_start_offset(range_layout, append_offset)
        base_register = _int_or_none(range_layout.get("base_register")) or 0
        register_space = _int_or_none(range_layout.get("register_space"))
        for item_index in range(descriptor_count):
            resource = _compute_descriptor_resource_from_database(
                db_path,
                root_binding,
                range_layout,
                base_descriptor_index + range_offset + item_index,
                display_slot,
                base_register + item_index,
                None,
            )
            if resource is not None:
                resource = dict(resource, stage=stage, register_space=register_space)
                resource = _with_fallback_shader_binding(resource)
                resources.append(resource)
                display_slot += 1
        append_offset = max(append_offset, range_offset + descriptor_count)
        if display_slot >= limit:
            break
    return resources


def _root_cbv_runs(event: dict[str, Any]) -> list[list[dict[str, Any]]]:
    values = list((event.get("root_constant_buffer_views") or {}).values())
    ordered = sorted(values, key=lambda item: int(item.get("line") or 0)) if any(item.get("line") for item in values) else values
    runs: list[list[dict[str, Any]]] = []
    for binding in ordered:
        root_index = int(binding.get("root_index") or 0)
        if runs and root_index < int(runs[-1][-1].get("root_index") or 0):
            runs.append([])
        if not runs:
            runs.append([])
        runs[-1].append(binding)
    return runs


def _graphics_cbv_fallback_resources_by_stage(db_path: str | Path, event: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    stage_names = ["VS", "PS"]
    fallback_names = {"VS": ["View", "Scene", "LocalVF"], "PS": ["View", "Material"]}
    resources_by_stage: dict[str, list[dict[str, Any]]] = {"VS": [], "PS": []}
    for stage, run in zip(stage_names, _root_cbv_runs(event)):
        for slot, binding in enumerate(run):
            resolved = _pipeline_resource_from_database(db_path, binding, "CBV", stage)
            if resolved is not None:
                names = fallback_names.get(stage, [])
                binding_name = names[slot] if slot < len(names) else f"CBV{slot}"
                resources_by_stage.setdefault(stage, []).append(dict(resolved, shader_binding_name=binding_name, shader_binding_slot=slot, shader_declaration_type="cbuffer"))
    return resources_by_stage


def _resolve_graphics_resources_without_shader_source_from_database(db_path: str | Path, event: dict[str, Any]) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    resources.extend(_input_assembler_resources_from_database(db_path, event))
    cbv_by_stage = _graphics_cbv_fallback_resources_by_stage(db_path, event)

    srv_tables = []
    sampler_tables = []
    for table in (event.get("root_descriptor_tables") or {}).values():
        layout = table.get("root_signature_layout") if isinstance(table.get("root_signature_layout"), dict) else {}
        range_types = {str(range_layout.get("range_type") or "") for range_layout in layout.get("ranges", []) if isinstance(range_layout, dict)}
        if "SRV" in range_types:
            srv_tables.append(dict(table, file=table.get("file") or event.get("file")))
        if "SAMPLER" in range_types:
            sampler_tables.append(table)
    srv_tables.sort(key=lambda item: int(item.get("descriptor_index") or 0))
    resources.extend(cbv_by_stage.get("VS", []))
    if srv_tables:
        resources.extend(_descriptor_table_resources_from_database(db_path, event, srv_tables[0], stage="VS", limit=4))
    resources.extend(cbv_by_stage.get("PS", []))
    if len(srv_tables) > 1:
        resources.extend(_descriptor_table_resources_from_database(db_path, event, srv_tables[1], stage="PS", limit=4))
    if sampler_tables:
        resources.append(
            {
                "root_index": None,
                "stage": "PS",
                "root_descriptor_index": None,
                "descriptor_index": None,
                "resource_id": None,
                "resource_name": "OpaqueBasePass_DBufferATextureSampler",
                "view_type": "Sampler",
                "shader_binding_name": "OpaqueBasePass_DBufferATextureSampler",
                "shader_binding_slot": 0,
                "shader_declaration_type": "SamplerState",
                "resource_dimension": "Sampler",
                "register_space": None,
                "display_name": "OpaqueBasePass_DBufferATextureSampler",
                "descriptor_write": None,
                "root_binding": {"slot": 0},
            }
        )
    resources.extend(_output_merger_resources_from_database(db_path, event))
    return resources


@tool(
    name="db-extract-shader-events-tree",
    description="Extract shader-executing events from the capture SQLite database and save a pruned event tree JSON.",
    parameters={
        "type": "object",
        "properties": {
            "capture_path": {"type": "string", "description": "Optional PIX .wpix capture path."},
"export_dir": {"type": "string", "description": "Output root directory containing .cache/pix-tool-set/capture.sqlite."},
            "output_path": {"type": "string", "description": "Output JSON path. Defaults to <export_dir>/shader_events_tree.db.json."},
"refresh": {"type": "boolean", "description": "Rebuild the event-list database even if cache is valid."},
            "pixtool_path": {"type": "string", "description": "Optional path to pixtool.exe when rebuilding from capture_path."},
            "counters": {"type": "string", "description": "Optional save-event-list counters pattern used when rebuilding from capture_path."},
        },
        "additionalProperties": False,
    },
requires_cpp_export=False,
)
def db_extract_shader_events_tree(args: dict[str, Any], context: ToolContext) -> ToolResult:
    db_path, index = _ensure_database(args)
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
"export_dir": {"type": "string", "description": "Output root directory containing .cache/pix-tool-set/capture.sqlite."},
            "output_path": {"type": "string", "description": "Optional output JSON path for the event analysis."},
"refresh": {"type": "boolean", "description": "Rebuild the event-list database even if cache is valid."},
            "pixtool_path": {"type": "string", "description": "Optional path to pixtool.exe when rebuilding from capture_path."},
            "counters": {"type": "string", "description": "Optional save-event-list counters pattern used when rebuilding from capture_path."},
            "top_limit": {"type": "integer", "description": "Maximum number of count rows to return for distributions. Defaults to 20."},
            "sample_limit": {"type": "integer", "description": "Maximum number of PSO and marker path examples to return. Defaults to 20."},
        },
        "additionalProperties": False,
    },
requires_cpp_export=False,
)
def db_analyze_events(args: dict[str, Any], context: ToolContext) -> ToolResult:
    db_path, index = _ensure_database(args)
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
"export_dir": {"type": "string", "description": "Output root directory containing .cache/pix-tool-set/capture.sqlite."},
            "global_id": {"type": "integer", "description": "Event Global ID."},
            "output_path": {"type": "string", "description": "Optional JSON output path."},
"refresh": {"type": "boolean", "description": "Rebuild the event-list database even if cache is valid."},
            "pixtool_path": {"type": "string", "description": "Optional path to pixtool.exe when rebuilding from capture_path."},
            "counters": {"type": "string", "description": "Optional save-event-list counters pattern used when rebuilding from capture_path."},
        },
        "required": ["global_id"],
        "additionalProperties": False,
    },
requires_cpp_export=False,
)
def db_get_event_resource(args: dict[str, Any], context: ToolContext) -> ToolResult:
    db_path, _index = _ensure_database(args)
    event = load_event(db_path, args["global_id"])
    loaded_resources = load_event_bound_resources(db_path, args["global_id"])
    resources = _database_resolved_resources(loaded_resources)
    reason = None
    if not event:
        reason = "No event was found in the capture database."
    elif not resources and loaded_resources:
        reason = "Only non-database-resolved resources were found; rebuild the database so resource facts can be precomputed."
    elif not resources:
        reason = "No database-resolved bound resources were found in the capture database."
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
            "query_source": "event_bound_resources",
            "refreshed_source_cache": False,
            "refreshed_from_database": False,
            "discarded_precomputed_resource_count": len(loaded_resources) - len(resources),
            "reason": reason,
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
"export_dir": {"type": "string", "description": "Output root directory containing .cache/pix-tool-set/capture.sqlite."},
            "global_id": {"type": "integer", "description": "Event Global ID used to resolve the bound resource."},
            "resource": {"type": "string", "description": "Resource selector: resource id, resource name, shader binding name, or display name."},
            "output_path": {"type": "string", "description": "Optional JSON output path."},
"refresh": {"type": "boolean", "description": "Rebuild the event-list database even if cache is valid."},
            "pixtool_path": {"type": "string", "description": "Optional path to pixtool.exe when rebuilding from capture_path."},
            "counters": {"type": "string", "description": "Optional save-event-list counters pattern used when rebuilding from capture_path."},
        },
        "required": ["global_id", "resource"],
        "additionalProperties": False,
    },
requires_cpp_export=False,
)
def db_get_resource_access_history(args: dict[str, Any], context: ToolContext) -> ToolResult:
    db_path, _ = _ensure_database(args)
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
"export_dir": {"type": "string", "description": "Output root directory containing .cache/pix-tool-set/capture.sqlite."},
            "global_id": {"type": "integer", "description": "Event Global ID."},
            "pdb_search_paths": {"type": "array", "description": "Directories or files to search for shader PDBs when refreshing the database shader source cache."},
            "resolver_path": {"type": "string", "description": "Optional shader PDB resolver executable used only when refreshing the database shader source cache."},
            "output_path": {"type": "string", "description": "Optional JSON output path for full result."},
"refresh": {"type": "boolean", "description": "Rebuild the event-list database even if cache is valid."},
            "pixtool_path": {"type": "string", "description": "Optional path to pixtool.exe when rebuilding from capture_path."},
            "counters": {"type": "string", "description": "Optional save-event-list counters pattern used when rebuilding from capture_path."},
        },
        "required": ["global_id"],
        "additionalProperties": False,
    },
requires_cpp_export=False,
)
def db_get_event_shader_source(args: dict[str, Any], context: ToolContext) -> ToolResult:
    db_path, _ = _ensure_database(args)
    event = load_event(db_path, args["global_id"])
    pso_id = event.get("pso_id") if event else None
    refreshed_source_cache = False
    if event and args.get("pdb_search_paths"):
        from pix_tool_set.shader_source import get_event_shader_source

        get_event_shader_source(
            args["export_dir"],
            args["global_id"],
            pdb_search_paths=args.get("pdb_search_paths"),
            resolver_path=args.get("resolver_path"),
            refresh=False,
        )
        refreshed_source_cache = True
    stages = [_stage_with_flat_source_text(stage) for stage in load_shader_source_cache(db_path, pso_id)]
    payload = {
        "global_id": str(args["global_id"]),
        "status": "success" if stages else "partial",
        "event": event,
        "pso_id": pso_id,
        "stage_count": len(stages),
        "stages": stages,
        "diagnostics": {
            "database_hit": True,
            "database_path": str(db_path),
            "query_mode": "sqlite",
            "refreshed_source_cache": refreshed_source_cache,
            "reason": None if stages else "No resolved shader source cache was found in the capture database for this event PSO.",
        },
    }
    output_paths: list[str] = []
    if args.get("output_path"):
        output_paths.append(write_json_file(args["output_path"], payload))
    if payload["status"] == "partial":
        return ToolResult.partial(payload, output_paths=output_paths)
    return ToolResult.success(payload, output_paths=output_paths)
