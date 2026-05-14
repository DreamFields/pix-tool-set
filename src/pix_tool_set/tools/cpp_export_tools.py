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
    description="Build or refresh the reusable index for a PIX C++ export directory.",
    parameters={
        "type": "object",
        "properties": {
            "capture_path": {"type": "string", "description": "Optional PIX .wpix capture path."},
            "export_dir": {"type": "string", "description": "PIX C++ export directory."},
            "auto_export": {"type": "boolean", "description": "Export C++ project with pixtool when missing."},
            "refresh": {"type": "boolean", "description": "Rebuild the index even if cache is valid."},
        },
        "additionalProperties": False,
    },
    requires_cpp_export=True,
)
def build_export_index(args: dict[str, Any], context: ToolContext) -> ToolResult:
    from pathlib import Path

    from pix_tool_set.indexer import build_index

    index = build_index(args["export_dir"], refresh=bool(args.get("refresh", False)))
    index_path = Path(args["export_dir"]) / ".cache" / "pix-tool-set" / "index.json"
    output_paths = [str(index_path)]
    if index.get("database_path"):
        output_paths.append(str(index["database_path"]))
    return ToolResult.success(
        {
            "export_dir": index["export_dir"],
            "cache_hit": index.get("cache_hit", False),
            "diagnostics": index["diagnostics"],
            "index_path": str(index_path),
            "database_path": index.get("database_path"),
            "database_cache_hit": index.get("database_cache_hit", False),
            "database_schema_version": index.get("database_schema_version"),
            "database_table_counts": index.get("database_table_counts", {}),
        },
        output_paths=output_paths,
    )
