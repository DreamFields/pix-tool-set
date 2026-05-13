from __future__ import annotations

from typing import Any

from pix_tool_set.context import ToolContext
from pix_tool_set.io_utils import default_output_path, write_json_file
from pix_tool_set.registry import tool
from pix_tool_set.results import ToolResult


DEFAULT_DESCRIPTOR_SCAN_COUNT = 8


def _optional_int(value: Any, default: int) -> int:
    if value is None:
        return default
    return int(value)


@tool(
    name="get-event-resource",
    description="Resolve currently bound descriptor resources for an event global id.",
    parameters={
        "type": "object",
        "properties": {
            "capture_path": {"type": "string", "description": "Optional PIX .wpix capture path."},
            "export_dir": {"type": "string", "description": "PIX C++ export directory."},
            "global_id": {"type": "integer", "description": "Event Global ID."},
            "descriptor_scan_count": {"type": "integer", "description": "Number of descriptors to inspect from each root descriptor table start."},
            "pdb_search_paths": {"type": "array", "description": "Directories or files to search for shader PDBs when resolving shader binding names."},
            "output_path": {"type": "string", "description": "Optional JSON output path."},
            "auto_export": {"type": "boolean", "description": "Export C++ project with pixtool when missing."},
            "refresh": {"type": "boolean", "description": "Rebuild the index even if cache is valid."},
        },
        "required": ["global_id"],
        "additionalProperties": False,
    },
    requires_cpp_export=True,
)
def get_event_resource_tool(args: dict[str, Any], context: ToolContext) -> ToolResult:
    from pix_tool_set.resource_history import get_event_resource

    result = get_event_resource(
        args["export_dir"],
        args["global_id"],
        descriptor_scan_count=_optional_int(args.get("descriptor_scan_count"), DEFAULT_DESCRIPTOR_SCAN_COUNT),
        pdb_search_paths=args.get("pdb_search_paths"),
        refresh=bool(args.get("refresh", False)),
    )
    filename = "resource_" + str(args["global_id"]) + ".json"
    output_path = args.get("output_path") or default_output_path(args["export_dir"], filename)
    written_path = write_json_file(output_path, result)
    payload = {
        "global_id": result["event"]["global_id"],
        "status": result["status"],
        "resource_count": len(result["resources"]),
        "resources": [item.get("display_name") for item in result["resources"]],
        "diagnostics": result["diagnostics"],
    }
    if result["status"] == "partial":
        return ToolResult.partial(payload, output_paths=[written_path])
    return ToolResult.success(payload, output_paths=[written_path])






@tool(
    name="get-resource-access-history",
    description="Export PIX-like access history for a bound resource selected from an event global id.",
    parameters={
        "type": "object",
        "properties": {
            "capture_path": {"type": "string", "description": "Optional PIX .wpix capture path."},
            "export_dir": {"type": "string", "description": "PIX C++ export directory."},
            "global_id": {"type": "integer", "description": "Event Global ID used to resolve the bound resource."},
            "resource": {"type": "string", "description": "Resource selector: resource id, resource name, shader binding name, or display name such as RayTracing.LightGrid:RWLightGrid."},
            "descriptor_scan_count": {"type": "integer", "description": "Number of descriptors to inspect from each root descriptor table start."},
            "pdb_search_paths": {"type": "array", "description": "Directories or files to search for shader PDBs when resolving shader binding names."},
            "output_path": {"type": "string", "description": "Optional JSON output path."},
            "auto_export": {"type": "boolean", "description": "Export C++ project with pixtool when missing."},
            "refresh": {"type": "boolean", "description": "Rebuild the index even if cache is valid."},
        },
        "required": ["global_id", "resource"],
        "additionalProperties": False,
    },
    requires_cpp_export=True,
)
def get_resource_access_history_tool(args: dict[str, Any], context: ToolContext) -> ToolResult:
    from pix_tool_set.resource_history import get_resource_access_history

    result = get_resource_access_history(
        args["export_dir"],
        args["global_id"],
        args["resource"],
        descriptor_scan_count=_optional_int(args.get("descriptor_scan_count"), DEFAULT_DESCRIPTOR_SCAN_COUNT),
        pdb_search_paths=args.get("pdb_search_paths"),
        refresh=bool(args.get("refresh", False)),
    )
    filename = "access_history_" + str(args["global_id"]) + ".json"
    output_path = args.get("output_path") or default_output_path(args["export_dir"], filename)
    written_path = write_json_file(output_path, result)
    payload = {
        "global_id": result["event"]["global_id"],
        "status": result["status"],
        "resource": result["resource"].get("display_name") or result["resource"].get("resource_name"),
        "resource_id": result["resource"].get("resource_id"),
        "access_count": len(result["access_history"]),
        "access_history": result["access_history"],
        "diagnostics": result["diagnostics"],
    }
    if result["status"] == "partial":
        return ToolResult.partial(payload, output_paths=[written_path])
    return ToolResult.success(payload, output_paths=[written_path])
