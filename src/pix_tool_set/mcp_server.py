from __future__ import annotations

import inspect
from typing import Any

from pix_tool_set.context import ToolContext
from pix_tool_set.errors import PixToolError
from pix_tool_set.execution import execute_tool
from pix_tool_set.registry import get_registry
from pix_tool_set.tools import load_builtin_tools


def _json_schema_type_to_python_type(schema_type: str) -> str:
    """Map a JSON schema type string to a Python type annotation string."""
    mapping = {
        "string": "str",
        "number": "float",
        "integer": "int",
        "boolean": "bool",
        "array": "list",
        "object": "dict",
        "null": "None",
    }
    return mapping.get(schema_type, "Any")


def _create_handler_with_schema(
    tool_name: str,
    parameters: dict[str, Any],
    description: str,
) -> Any:
    """Dynamically create a handler function whose signature matches *parameters*.

    FastMCP derives the JSON *inputSchema* from the function signature that
    is passed to ``@server.tool()``.  By building a function whose
    keyword-only arguments exactly mirror the tool's JSON-schema properties
    we guarantee that the schema advertised to MCP clients is correct.

    The function body simply forwards all received arguments to
    :func:`execute_tool` and returns the result as a dict.
    """
    properties = parameters.get("properties", {})
    required_params = set(parameters.get("required", []))
    additional = parameters.get("additionalProperties", True)

    # ------------------------------------------------------------------
    # Build the function signature string
    # Python requires that parameters without defaults precede those with
    # defaults.  We collect required (no default) and optional (with
    # default) parameter strings separately, then concatenate them.
    # Everything is keyword-only (prefixed with `*`) so callers must
    # use `name=value` syntax, which matches JSON-RPC calls.
    # ------------------------------------------------------------------
    required_parts: list[str] = []
    optional_parts: list[str] = []
    for name in properties:
        py_type_str = _json_schema_type_to_python_type(
            properties[name].get("type", "string")
        )
        if name in required_params:
            required_parts.append(f"{name}: {py_type_str}")
        else:
            optional_parts.append(f"{name}: {py_type_str} = None")

    sig_parts: list[str] = ["*"]  # force all subsequent params to be keyword-only
    sig_parts.extend(required_parts)
    sig_parts.extend(optional_parts)
    if additional:
        # FastMCP rejects names starting with '_', so use a plain 'kwargs'.
        sig_parts.append("**kwargs: Any")

    sig_str = ", ".join(sig_parts) if len(sig_parts) > 1 else "**kwargs: Any"

    # ------------------------------------------------------------------
    # Build the function body
    # The body collects every argument the function received and forwards
    # it to ``execute_tool`` as a plain dict.
    # ------------------------------------------------------------------
    prop_names = list(properties.keys())
    if additional:
        # With **kwargs present, all declared params + kwargs dict
        body = (
            f"    all_args = {{}}\n"
            f"    all_args.update({{ {', '.join(f'{name!r}: {name}' for name in prop_names)} }})\n"
            f"    if kwargs:\n"
            f"        all_args.update(kwargs)\n"
            f"    result = _execute_tool({tool_name!r}, all_args, _context)\n"
            f"    return result.to_dict()\n"
        )
    else:
        body = (
            f"    all_args = {{ {', '.join(f'{name!r}: {name}' for name in prop_names)} }}\n"
            f"    result = _execute_tool({tool_name!r}, all_args, _context)\n"
            f"    return result.to_dict()\n"
        )

    func_src = f'def _handler({sig_str}) -> dict[str, Any]:\n{body}'

    # ------------------------------------------------------------------
    # Compile and execute the function source
    # ------------------------------------------------------------------
    namespace: dict[str, Any] = {
        "_execute_tool": execute_tool,
        "_context": ToolContext.from_cwd(),
        "Any": Any,
        "dict": dict,
        "list": list,
        "str": str,
        "int": int,
        "float": float,
        "bool": bool,
        "set": set,
    }

    exec(compile(func_src, f"<dynamic:{tool_name}>", "exec"), namespace)
    handler = namespace["_handler"]
    handler.__name__ = f"handler_{tool_name}"
    handler.__doc__ = description or ""
    return handler


def create_server() -> Any:
    """Create and return a configured FastMCP server."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise PixToolError(
            code="mcp_dependency_missing",
            message="The mcp package is required to run the MCP server.",
            stage="startup",
            suggestion="Install the project with MCP dependencies: python -m pip install -e .",
        ) from exc

    load_builtin_tools()
    server = FastMCP("pix-tool-set")

    for definition in get_registry().list_tools("mcp"):
        handler = _create_handler_with_schema(
            tool_name=definition.name,
            parameters=definition.parameters,
            description=definition.description,
        )
        server.tool(
            name=definition.mcp_name(),
            description=definition.description,
        )(handler)

    return server


def main() -> None:
    server = create_server()
    server.run()


if __name__ == "__main__":
    main()
