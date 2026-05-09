from __future__ import annotations

from typing import Any

from pix_tool_set.context import ToolContext
from pix_tool_set.registry import tool
from pix_tool_set.results import ToolResult


@tool(
    name="extract-shader-events-tree",
    description="Extract shader-executing events from a PIX C++ export and save a pruned event tree JSON.",
    parameters={
        "type": "object",
        "properties": {
            "capture_path": {"type": "string", "description": "Optional PIX .wpix capture path."},
            "export_dir": {"type": "string", "description": "PIX C++ export directory."},
            "output_path": {"type": "string", "description": "Output JSON path. Defaults to <export_dir>/shader_events_tree.json."},
            "auto_export": {"type": "boolean", "description": "Export C++ project with pixtool when missing."},
            "refresh": {"type": "boolean", "description": "Rebuild the index even if cache is valid."},
        },
        "additionalProperties": False,
    },
    requires_cpp_export=True,
)
def extract_shader_events_tree(args: dict[str, Any], context: ToolContext) -> ToolResult:
    from pix_tool_set.shader_events import write_shader_event_tree

    payload = write_shader_event_tree(
        args["export_dir"],
        output_path=args.get("output_path"),
        refresh=bool(args.get("refresh", False)),
    )
    return ToolResult.success(
        payload["metadata"],
        output_paths=[payload["output_path"]],
        diagnostics=[{"stage": "shader_event_tree", "root_count": len(payload["tree"])}],
    )
