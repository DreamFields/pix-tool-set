from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pix_tool_set.context import ToolContext
from pix_tool_set.registry import tool
from pix_tool_set.results import ToolResult


@tool(
    name="get-event-shader-source",
    description="Locate shader blobs and optionally resolve HLSL source for a shader event global id.",
    parameters={
        "type": "object",
        "properties": {
            "capture_path": {"type": "string", "description": "Optional PIX .wpix capture path."},
            "export_dir": {"type": "string", "description": "PIX C++ export directory."},
            "global_id": {"type": "integer", "description": "Event Global ID."},
            "pdb_search_paths": {"type": "array", "description": "Directories to search for shader PDBs."},
            "resolver_path": {"type": "string", "description": "Optional shader PDB resolver executable."},
            "output_path": {"type": "string", "description": "Optional JSON output path for full result."},
            "auto_export": {"type": "boolean", "description": "Export C++ project with pixtool when missing."},
            "refresh": {"type": "boolean", "description": "Rebuild the index even if cache is valid."},
        },
        "required": ["global_id"],
        "additionalProperties": False,
    },
    requires_cpp_export=True,
)
def get_event_shader_source_tool(args: dict[str, Any], context: ToolContext) -> ToolResult:
    from pix_tool_set.shader_source import get_event_shader_source

    result = get_event_shader_source(
        args["export_dir"],
        args["global_id"],
        pdb_search_paths=args.get("pdb_search_paths"),
        resolver_path=args.get("resolver_path"),
        refresh=bool(args.get("refresh", False)),
    )
    output_paths: list[str] = []
    if args.get("output_path"):
        out = Path(args["output_path"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        output_paths.append(str(out.resolve()))
    return ToolResult.success(
        {
            "global_id": result["event"]["global_id"],
            "pso_id": result.get("pso_id"),
            "stage_count": len(result["stages"]),
            "stages": result["stages"],
            "diagnostics": result["diagnostics"],
        },
        output_paths=output_paths,
    )
