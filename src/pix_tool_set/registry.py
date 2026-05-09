"""Tool registry: the single source of truth for what this toolkit can do.

Each tool declares a JSON-Schema parameter block, so the CLI can auto-generate
flags, ``list-tools`` can hand an AI client a machine-readable catalogue, and
``describe`` can explain one tool in detail.  Nothing about a tool is hard-coded
anywhere else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Literal

from .errors import PixToolError
from .results import ToolResult

ToolHandler = Callable[[dict[str, Any], "ToolContext"], ToolResult]  # noqa: F821

Category = Literal[
    "session",
    "events",
    "frame",
    "textures",
    "shaders",
    "geometry",
    "pipeline",
    "resources",
    "export",
    "advanced",
    "performance",
    "diagnostics",
    "meta",
]

CATEGORY_TITLES: dict[str, str] = {
    "session": "Session management",
    "events": "Event and action navigation",
    "frame": "Frame statistics",
    "textures": "Texture analysis",
    "shaders": "Shader analysis",
    "geometry": "Geometry and draw calls",
    "pipeline": "Pipeline state",
    "resources": "Resource management",
    "export": "Data export",
    "advanced": "Advanced analysis",
    "performance": "Performance analysis",
    "diagnostics": "Diagnostics",
    "meta": "Toolkit meta",
    "pixels": "Pixel-level debugging",
}


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    summary: str
    category: str
    parameters: dict[str, Any]
    handler: ToolHandler
    returns: str = ""
    examples: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    requires_session: bool = True
    notes: str = ""

    @property
    def description(self) -> str:
        return self.summary

    def to_metadata(self, *, verbose: bool = True) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "name": self.name,
            "category": self.category,
            "summary": self.summary,
            "requires_session": self.requires_session,
        }
        if self.aliases:
            meta["aliases"] = list(self.aliases)
        if verbose:
            meta["parameters"] = self.parameters
            if self.returns:
                meta["returns"] = self.returns
            if self.examples:
                meta["examples"] = list(self.examples)
            if self.notes:
                meta["notes"] = self.notes
        return meta

    # -- validation ----------------------------------------------------
    def validate_args(self, args: dict[str, Any]) -> dict[str, Any]:
        """Check required keys and coerce declared types."""
        props: dict[str, Any] = self.parameters.get("properties", {})
        required: list[str] = self.parameters.get("required", [])
        unknown = sorted(set(args) - set(props))
        if unknown:
            raise PixToolError(
                code="unknown_parameter",
                message=f"Tool {self.name} received unknown parameter(s): {', '.join(unknown)}",
                stage="validation",
                suggestion=f"Run `describe {self.name}` to see accepted parameters.",
                details={"accepted": sorted(props)},
            )
        missing = [key for key in required if args.get(key) is None]
        if missing:
            raise PixToolError(
                code="missing_parameter",
                message=f"Tool {self.name} requires: {', '.join(missing)}",
                stage="validation",
                suggestion=f"Run `describe {self.name}` to see accepted parameters.",
                details={"missing": missing},
            )
        cleaned: dict[str, Any] = {}
        for key, value in args.items():
            if value is None:
                continue
            cleaned[key] = _coerce(key, value, props.get(key, {}))
        return cleaned


def _coerce(key: str, value: Any, schema: dict[str, Any]) -> Any:
    expected = schema.get("type")
    if expected is None or value is None:
        return value
    try:
        if expected == "integer" and not isinstance(value, bool):
            return int(value)
        if expected == "number" and not isinstance(value, bool):
            return float(value)
        if expected == "boolean":
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in {"1", "true", "yes", "on"}
        if expected == "array" and isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
    except (TypeError, ValueError) as exc:
        raise PixToolError(
            code="invalid_argument",
            message=f"Parameter {key} could not be read as {expected}: {value!r}",
            stage="validation",
            suggestion="Check the value type against the parameter schema.",
        ) from exc
    enum = schema.get("enum")
    if enum and value not in enum:
        raise PixToolError(
            code="invalid_argument",
            message=f"Parameter {key} must be one of {enum}, got {value!r}",
            stage="validation",
        )
    return value


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._aliases: dict[str, str] = {}

    def register(self, tool: ToolDefinition) -> ToolDefinition:
        if tool.name in self._tools or tool.name in self._aliases:
            raise PixToolError(
                code="tool_name_conflict",
                message=f"Tool name already registered: {tool.name}",
                stage="registration",
            )
        if tool.category not in CATEGORY_TITLES:
            raise PixToolError(
                code="tool_category_invalid",
                message=f"Unknown category {tool.category!r} for tool {tool.name}",
                stage="registration",
                details={"known": sorted(CATEGORY_TITLES)},
            )
        self._tools[tool.name] = tool
        for alias in tool.aliases:
            self._aliases[alias] = tool.name
        return tool

    def tool(
        self,
        *,
        name: str,
        summary: str,
        category: str,
        parameters: dict[str, Any] | None = None,
        returns: str = "",
        examples: Iterable[str] = (),
        aliases: Iterable[str] = (),
        requires_session: bool = True,
        notes: str = "",
    ) -> Callable[[ToolHandler], ToolHandler]:
        def _decorate(handler: ToolHandler) -> ToolHandler:
            self.register(
                ToolDefinition(
                    name=name,
                    summary=summary,
                    category=category,
                    parameters=parameters or {"type": "object", "properties": {}},
                    handler=handler,
                    returns=returns,
                    examples=tuple(examples),
                    aliases=tuple(aliases),
                    requires_session=requires_session,
                    notes=notes,
                )
            )
            return handler

        return _decorate

    def get(self, name: str) -> ToolDefinition:
        canonical = self._aliases.get(name, name)
        tool = self._tools.get(canonical)
        if tool is None:
            raise PixToolError(
                code="tool_not_found",
                message=f"Tool is not registered: {name}",
                stage="dispatch",
                suggestion="Run `list-tools` to see every available tool.",
                details={"closest": _closest(name, self._tools)},
            )
        return tool

    def has(self, name: str) -> bool:
        return name in self._tools or name in self._aliases

    def list_tools(self, category: str | None = None) -> list[ToolDefinition]:
        tools = sorted(self._tools.values(), key=lambda t: (t.category, t.name))
        if category is None:
            return tools
        return [t for t in tools if t.category == category]

    def categories(self) -> dict[str, list[ToolDefinition]]:
        grouped: dict[str, list[ToolDefinition]] = {}
        for tool in self.list_tools():
            grouped.setdefault(tool.category, []).append(tool)
        return grouped

    def metadata(self, category: str | None = None, *, verbose: bool = True) -> list[dict[str, Any]]:
        return [t.to_metadata(verbose=verbose) for t in self.list_tools(category)]


def _closest(name: str, pool: dict[str, ToolDefinition]) -> list[str]:
    import difflib

    return difflib.get_close_matches(name, list(pool), n=3, cutoff=0.5)


_REGISTRY = ToolRegistry()


def get_registry() -> ToolRegistry:
    return _REGISTRY
