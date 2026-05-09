from __future__ import annotations

from typing import Any

from .context import ToolContext
from .cpp_export import ensure_cpp_export
from .registry import ToolDefinition, get_registry
from .results import ToolResult


def execute_tool(definition_or_name: ToolDefinition | str, args: dict[str, Any], context: ToolContext) -> ToolResult:
    definition = get_registry().get(definition_or_name) if isinstance(definition_or_name, str) else definition_or_name
    working_args = dict(args)
    diagnostics: list[dict[str, Any]] = []

    if definition.requires_cpp_export:
        export_info = ensure_cpp_export(working_args, context.workspace)
        diagnostics.append({"stage": "cpp_export_check", **export_info.to_dict()})

    result = definition.handler(working_args, context)
    if diagnostics:
        result.diagnostics[:0] = diagnostics
    return result
