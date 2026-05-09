from __future__ import annotations

from typing import Any

from pix_tool_set.context import ToolContext
from pix_tool_set.registry import tool
from pix_tool_set.results import ToolResult


@tool(
    name="diagnose-environment",
    aliases=("env",),
    description="Return basic runtime information for verifying CLI and MCP wiring.",
    parameters={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "workspace": {"type": "string"},
        },
    },
)
def diagnose_environment(args: dict[str, Any], context: ToolContext) -> ToolResult:
    return ToolResult.success(
        {
            "workspace": str(context.workspace),
            "config_keys": sorted(context.config.keys()),
        }
    )
