from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from .context import ToolContext
from .errors import PixToolError
from .results import ToolResult

Exposure = Literal["cli", "mcp", "both"]
ToolHandler = Callable[[dict[str, Any], ToolContext], ToolResult]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    output_schema: dict[str, Any] = field(default_factory=dict)
    aliases: tuple[str, ...] = ()
    exposure: Exposure = "both"
    requires_capture: bool = False
    requires_cpp_export: bool = False

    def cli_name(self) -> str:
        return self.name

    def mcp_name(self) -> str:
        return self.name


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._aliases: dict[str, str] = {}

    def register(self, tool: ToolDefinition) -> ToolDefinition:
        self._validate_definition(tool)
        self._validate_name_available(tool.name, "name")
        for alias in tool.aliases:
            self._validate_name_available(alias, "alias")

        self._tools[tool.name] = tool
        for alias in tool.aliases:
            self._aliases[alias] = tool.name
        return tool

    def decorator(
        self,
        *,
        name: str,
        description: str,
        parameters: dict[str, Any],
        output_schema: dict[str, Any] | None = None,
        aliases: tuple[str, ...] = (),
        exposure: Exposure = "both",
        requires_capture: bool = False,
        requires_cpp_export: bool = False,
    ) -> Callable[[ToolHandler], ToolHandler]:
        def _decorate(handler: ToolHandler) -> ToolHandler:
            self.register(
                ToolDefinition(
                    name=name,
                    description=description,
                    parameters=parameters,
                    handler=handler,
                    output_schema=output_schema or {},
                    aliases=aliases,
                    exposure=exposure,
                    requires_capture=requires_capture,
                    requires_cpp_export=requires_cpp_export,
                )
            )
            return handler

        return _decorate

    def get(self, name: str) -> ToolDefinition:
        canonical_name = self._aliases.get(name, name)
        try:
            return self._tools[canonical_name]
        except KeyError as exc:
            raise PixToolError(
                code="tool_not_found",
                message=f"Tool is not registered: {name}",
                stage="dispatch",
                suggestion="Run list-tools to see available tools.",
            ) from exc

    def list_tools(self, exposure: Exposure | None = None) -> list[ToolDefinition]:
        tools = list(self._tools.values())
        if exposure is None:
            return tools
        return [tool for tool in tools if tool.exposure in (exposure, "both")]

    def metadata(self, exposure: Exposure | None = None) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "cli_name": tool.cli_name(),
                "mcp_name": tool.mcp_name(),
                "description": tool.description,
                "aliases": list(tool.aliases),
                "exposure": tool.exposure,
                "parameters": tool.parameters,
                "output_schema": tool.output_schema,
                "requires_capture": tool.requires_capture,
                "requires_cpp_export": tool.requires_cpp_export,
            }
            for tool in self.list_tools(exposure)
        ]

    def _validate_definition(self, tool: ToolDefinition) -> None:
        if tool.exposure not in ("cli", "mcp", "both"):
            raise PixToolError(
                code="tool_exposure_invalid",
                message=f"Invalid exposure for tool {tool.name}: {tool.exposure}",
                stage="registration",
            )
        if not tool.name.strip():
            raise PixToolError(
                code="tool_name_missing",
                message="Tool name is required.",
                stage="registration",
            )
        if not tool.description.strip():
            raise PixToolError(
                code="tool_description_missing",
                message=f"Tool description is required: {tool.name}",
                stage="registration",
            )
        if not isinstance(tool.parameters, dict) or tool.parameters.get("type") != "object":
            raise PixToolError(
                code="tool_parameters_invalid",
                message=f"Tool parameters must be a JSON schema object: {tool.name}",
                stage="registration",
            )
        if not callable(tool.handler):
            raise PixToolError(
                code="tool_handler_invalid",
                message=f"Tool handler must be callable: {tool.name}",
                stage="registration",
            )

    def _validate_name_available(self, name: str, kind: str) -> None:
        if not name.strip():
            raise PixToolError(
                code="tool_alias_invalid",
                message="Tool aliases cannot be empty.",
                stage="registration",
            )
        if name in self._tools or name in self._aliases:
            raise PixToolError(
                code="tool_name_conflict",
                message=f"Tool {kind} is already registered: {name}",
                stage="registration",
            )


registry = ToolRegistry()


def register_tool(tool: ToolDefinition) -> ToolDefinition:
    return registry.register(tool)


def tool(
    *,
    name: str,
    description: str,
    parameters: dict[str, Any],
    output_schema: dict[str, Any] | None = None,
    aliases: tuple[str, ...] = (),
    exposure: Exposure = "both",
    requires_capture: bool = False,
    requires_cpp_export: bool = False,
) -> Callable[[ToolHandler], ToolHandler]:
    return registry.decorator(
        name=name,
        description=description,
        parameters=parameters,
        output_schema=output_schema,
        aliases=aliases,
        exposure=exposure,
        requires_capture=requires_capture,
        requires_cpp_export=requires_cpp_export,
    )


def get_registry() -> ToolRegistry:
    return registry
