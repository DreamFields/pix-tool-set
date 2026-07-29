"""Command line interface.

Designed so an AI client can drive it without reading documentation:

  * ``list-tools``            machine-readable catalogue of every tool
  * ``describe <tool>``       the JSON Schema and examples for one tool
  * ``run <tool> --json-args`` uniform invocation with a JSON payload
  * ``<tool> --flag value``   ergonomic direct invocation, flags generated
                              automatically from the tool's schema

Every command prints one JSON object on stdout and exits 0 on success or 1 on a
tool-level error, so callers never have to parse prose.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .context import ToolContext
from .errors import PixToolError
from .registry import CATEGORY_TITLES, ToolDefinition, get_registry
from .results import ToolResult
from .tools import load_builtin_tools

__all__ = ["main", "build_parser"]

_GLOBAL_FLAGS = ("--pixtool",)


def _emit(payload: dict[str, Any], compact: bool = False) -> None:
    if compact:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    else:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text)


def _failure(exc: Exception, tool_name: str = "") -> ToolResult:
    if isinstance(exc, PixToolError):
        result = ToolResult.failure(exc)
    else:
        result = ToolResult.failure(
            PixToolError(
                code="unhandled_error",
                message=f"{type(exc).__name__}: {exc}",
                stage="runtime",
                suggestion="Re-run with --traceback to see the Python stack.",
            )
        )
    result.tool = tool_name
    return result


def _flag_name(parameter: str) -> str:
    return "--" + parameter.replace("_", "-")


def _add_tool_flags(parser: argparse.ArgumentParser, definition: ToolDefinition) -> None:
    properties: dict[str, Any] = definition.parameters.get("properties", {})
    required = set(definition.parameters.get("required", []))
    for name, schema in properties.items():
        flags = [_flag_name(name)]
        if name == "output":
            flags.append("-o")
        help_text = schema.get("description", "")
        if schema.get("enum"):
            help_text = f"{help_text} Choices: {', '.join(map(str, schema['enum']))}".strip()
        if schema.get("type") == "boolean":
            parser.add_argument(*flags, dest=name, action="store_true", help=help_text)
        else:
            parser.add_argument(
                *flags,
                dest=name,
                required=name in required,
                metavar=schema.get("type", "value").upper(),
                help=help_text,
            )
    parser.set_defaults(_tool=definition.name)


def _namespace_to_args(definition: ToolDefinition, namespace: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for name, schema in definition.parameters.get("properties", {}).items():
        value = getattr(namespace, name, None)
        if value is None:
            continue
        if schema.get("type") == "boolean" and value is False:
            continue
        payload[name] = value
    return payload


def build_parser() -> argparse.ArgumentParser:
    load_builtin_tools()
    registry = get_registry()

    parser = argparse.ArgumentParser(
        prog="pix-tool-set",
        description=(
            "Scriptable analysis of PIX (.wpix) GPU captures. Start with `session-open`, "
            "then query. Run `list-tools` for the machine-readable catalogue."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Typical flow:\n"
            "  pix-tool-set session-open --capture C:/caps/frame.wpix\n"
            "  pix-tool-set frame-stats\n"
            "  pix-tool-set list-passes --limit 20\n"
            "  pix-tool-set draw-state --draw-index 100\n"
        ),
    )
    parser.add_argument("--pixtool", help="Path to pixtool.exe when auto-detection fails.")
    parser.add_argument("--compact", action="store_true", help="Emit single-line JSON.")
    parser.add_argument("--traceback", action="store_true", help="Print Python tracebacks.")

    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    catalogue = sub.add_parser(
        "list-tools", help="List every tool with its JSON Schema (machine-readable)."
    )
    catalogue.add_argument("--category", choices=sorted(CATEGORY_TITLES), help="Filter by category.")
    catalogue.add_argument(
        "--brief", action="store_true", help="Omit schemas; names and summaries only."
    )

    describe = sub.add_parser("describe", help="Show the full schema for one tool.")
    describe.add_argument("tool_name", help="Tool name or alias.")

    runner = sub.add_parser("run", help="Run a tool with a JSON argument object.")
    runner.add_argument("tool_name", help="Tool name or alias.")
    runner.add_argument(
        "--json-args", default="{}", help="Arguments as a JSON object, e.g. '{\"limit\": 10}'."
    )

    for definition in registry.list_tools():
        tool_parser = sub.add_parser(
            definition.name,
            help=definition.summary,
            description=definition.summary
            + (f"\n\nNote: {definition.notes}" if definition.notes else ""),
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="Examples:\n  " + "\n  ".join(definition.examples)
            if definition.examples
            else None,
        )
        _add_tool_flags(tool_parser, definition)
        for alias in definition.aliases:
            alias_parser = sub.add_parser(alias, help=f"Alias for {definition.name}.")
            _add_tool_flags(alias_parser, definition)

    return parser


def _cmd_list_tools(args: argparse.Namespace) -> int:
    registry = get_registry()
    verbose = not args.brief
    grouped: dict[str, list[dict[str, Any]]] = {}
    for definition in registry.list_tools(args.category):
        grouped.setdefault(definition.category, []).append(
            definition.to_metadata(verbose=verbose)
        )
    payload = {
        "status": "success",
        "tool_count": sum(len(items) for items in grouped.values()),
        "categories": [
            {
                "category": category,
                "title": CATEGORY_TITLES.get(category, category),
                "tools": items,
            }
            for category, items in sorted(grouped.items())
        ],
        "usage": {
            "invoke": "pix-tool-set run <tool> --json-args '{...}'",
            "direct": "pix-tool-set <tool> --flag value",
            "describe": "pix-tool-set describe <tool>",
        },
    }
    _emit(payload, args.compact)
    return 0


def _cmd_describe(args: argparse.Namespace) -> int:
    registry = get_registry()
    definition = registry.get(args.tool_name)
    payload = {
        "status": "success",
        "tool": definition.to_metadata(verbose=True),
        "cli": {
            "direct": f"pix-tool-set {definition.name} "
            + " ".join(
                _flag_name(name) + " <" + schema.get("type", "value") + ">"
                for name, schema in definition.parameters.get("properties", {}).items()
                if name in set(definition.parameters.get("required", []))
            ),
            "json": f"pix-tool-set run {definition.name} --json-args '{{...}}'",
        },
    }
    _emit(payload, args.compact)
    return 0


def _execute(tool_name: str, tool_args: dict[str, Any], namespace: argparse.Namespace) -> ToolResult:
    registry = get_registry()
    definition = registry.get(tool_name)
    context = ToolContext.from_cwd(getattr(namespace, "pixtool", None))
    cleaned = definition.validate_args(tool_args)
    result = definition.handler(cleaned, context)
    result.tool = definition.name
    return result


def _cmd_run(args: argparse.Namespace) -> int:
    try:
        payload = json.loads(args.json_args)
        if not isinstance(payload, dict):
            raise ValueError("--json-args must decode to a JSON object")
        result = _execute(args.tool_name, payload, args)
    except Exception as exc:  # noqa: BLE001
        if getattr(args, "traceback", False):
            import traceback

            traceback.print_exc()
        result = _failure(exc, args.tool_name)
    _emit(result.to_dict(), args.compact)
    return 0 if result.status != "error" else 1


def _cmd_direct(args: argparse.Namespace) -> int:
    registry = get_registry()
    tool_name = getattr(args, "_tool")
    try:
        definition = registry.get(tool_name)
        result = _execute(definition.name, _namespace_to_args(definition, args), args)
    except Exception as exc:  # noqa: BLE001
        if getattr(args, "traceback", False):
            import traceback

            traceback.print_exc()
        result = _failure(exc, tool_name)
    _emit(result.to_dict(), args.compact)
    return 0 if result.status != "error" else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "list-tools":
        return _cmd_list_tools(args)
    if args.command == "describe":
        try:
            return _cmd_describe(args)
        except PixToolError as exc:
            _emit(_failure(exc, args.tool_name).to_dict(), args.compact)
            return 1
    if args.command == "run":
        return _cmd_run(args)
    if hasattr(args, "_tool"):
        return _cmd_direct(args)

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
