from __future__ import annotations

from typing import Any

from pix_tool_set.context import ToolContext
from pix_tool_set.registry import tool
from pix_tool_set.results import ToolResult


@tool(
    name="check-cpp-export",
    description="Validate that a PIX C++ export directory exists and contains required files.",
    parameters={
        "type": "object",
        "properties": {
            "capture_path": {"type": "string", "description": "Optional PIX .wpix capture path."},
            "export_dir": {"type": "string", "description": "Existing or target PIX C++ export directory."},
            "auto_export": {"type": "boolean", "description": "Export C++ project with pixtool when missing."},
            "pixtool_path": {"type": "string", "description": "Optional path to pixtool.exe."},
        },
        "additionalProperties": False,
    },
    requires_cpp_export=True,
)
def check_cpp_export(args: dict[str, Any], context: ToolContext) -> ToolResult:
    return ToolResult.success({"export_dir": args["export_dir"], "capture_path": args.get("capture_path")})


@tool(
    name="build-index",
    description="Build or refresh the capture database from a PIX save-event-list CSV export.",
    parameters={
        "type": "object",
        "properties": {
            "capture_path": {"type": "string", "description": "PIX .wpix capture path used by pixtool save-event-list."},
            "export_dir": {"type": "string", "description": "Output root directory for event-list CSV, index.json, and capture.sqlite. Defaults from capture_path."},
            "refresh": {"type": "boolean", "description": "Regenerate the event-list CSV and database even if cache is valid."},
            "pixtool_path": {"type": "string", "description": "Optional path to pixtool.exe. Defaults to Microsoft PIX 2603.25."},
            "counters": {"type": "string", "description": "Optional save-event-list counters pattern passed as --counters=<pattern>."},
        },
        "required": ["capture_path"],
        "additionalProperties": False,
    },
    requires_cpp_export=False,
)
def build_export_index(args: dict[str, Any], context: ToolContext) -> ToolResult:
    from pathlib import Path

    from pix_tool_set.indexer import build_index_from_capture

    index = build_index_from_capture(
        capture_path=args["capture_path"],
        export_dir=args.get("export_dir"),
        refresh=bool(args.get("refresh", False)),
        pixtool_path=args.get("pixtool_path"),
        counters=args.get("counters"),
        workspace=context.workspace,
    )
    index_path = Path(index["export_dir"]) / ".cache" / "pix-tool-set" / "index.json"
    output_paths = [str(index_path)]
    if index.get("event_list_csv_path"):
        output_paths.append(str(index["event_list_csv_path"]))
    if index.get("database_path"):
        output_paths.append(str(index["database_path"]))
    return ToolResult.success(
        {
            "capture_path": index.get("capture_path"),
            "export_dir": index["export_dir"],
            "cache_hit": index.get("cache_hit", False),
            "event_list_csv_path": index.get("event_list_csv_path"),
            "event_list_cache_hit": index.get("event_list_cache_hit", False),
            "event_list_refreshed": index.get("event_list_refreshed", False),
            "diagnostics": index["diagnostics"],
            "index_path": str(index_path),
            "database_path": index.get("database_path"),
            "database_cache_hit": index.get("database_cache_hit", False),
            "database_schema_version": index.get("database_schema_version"),
            "database_table_counts": index.get("database_table_counts", {}),
        },
        output_paths=output_paths,
    )
