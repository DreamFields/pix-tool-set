from __future__ import annotations

import argparse
import json
from typing import Any

from .context import ToolContext
from .errors import PixToolError
from .execution import execute_tool
from .registry import ToolDefinition, get_registry
from .results import ToolResult
from .tools import load_builtin_tools


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _result_from_exception(exc: Exception) -> ToolResult:
    if isinstance(exc, PixToolError):
        return ToolResult.failure(exc)
    return ToolResult.failure(PixToolError(code="unhandled_error", message=str(exc), stage="runtime"))


def _coerce_value(value: str, schema: dict[str, Any]) -> Any:
    value_type = schema.get("type", "string")
    if value_type == "integer":
        return int(value)
    if value_type == "number":
        return float(value)
    if value_type == "boolean":
        return value.lower() in {"1", "true", "yes", "on"}
    if value_type in {"array", "object"}:
        return json.loads(value)
    return value


def _add_tool_arguments(parser: argparse.ArgumentParser, definition: ToolDefinition) -> None:
    properties = definition.parameters.get("properties", {})
    required = set(definition.parameters.get("required", []))
    for name, schema in properties.items():
        option = "--" + name.replace("_", "-")
        help_text = schema.get("description", "")
        if schema.get("type") == "boolean":
            parser.add_argument(option, dest=name, action="store_true", required=False, help=help_text)
        else:
            parser.add_argument(option, dest=name, required=name in required, help=help_text)
    parser.set_defaults(_tool_name=definition.name)


def build_parser() -> argparse.ArgumentParser:
    load_builtin_tools()
    parser = argparse.ArgumentParser(prog="pix-tool-set", description="Unified CLI for PIX C++ export based analysis tools.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list-tools", help="List registered tools.")
    run_parser = subparsers.add_parser("run", help="Run a registered tool by name.")
    run_parser.add_argument("tool_name", help="Canonical tool name or alias.")
    run_parser.add_argument("--json-args", default="{}", help="Tool arguments encoded as JSON object.")
    for definition in get_registry().list_tools("cli"):
        tool_parser = subparsers.add_parser(definition.cli_name(), help=definition.description)
        _add_tool_arguments(tool_parser, definition)
        for alias in definition.aliases:
            alias_parser = subparsers.add_parser(alias, help=f"Alias for {definition.name}")
            _add_tool_arguments(alias_parser, definition)
    return parser


def list_tools() -> int:
    _print_json({"status": "success", "tools": get_registry().metadata("cli")})
    return 0


def _args_from_namespace(definition: ToolDefinition, namespace: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for name, schema in definition.parameters.get("properties", {}).items():
        raw_value = getattr(namespace, name, None)
        if raw_value is None:
            continue
        payload[name] = bool(raw_value) if schema.get("type") == "boolean" else _coerce_value(raw_value, schema)
    return payload


def _execute(tool_name: str, args: dict[str, Any]) -> ToolResult:
    return execute_tool(tool_name, args, ToolContext.from_cwd())


def run_tool(tool_name: str, json_args: str) -> int:
    try:
        args = json.loads(json_args)
        if not isinstance(args, dict):
            raise ValueError("--json-args must decode to a JSON object")
        result = _execute(tool_name, args)
    except Exception as exc:
        result = _result_from_exception(exc)
    _print_json(result.to_dict())
    return 0 if result.status != "error" else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "list-tools":
        return list_tools()
    if args.command == "run":
        return run_tool(args.tool_name, args.json_args)
    if hasattr(args, "_tool_name"):
        try:
            definition = get_registry().get(args._tool_name)
            result = _execute(definition.name, _args_from_namespace(definition, args))
        except Exception as exc:
            result = _result_from_exception(exc)
        _print_json(result.to_dict())
        return 0 if result.status != "error" else 1
    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
