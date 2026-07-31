"""pix-tool-set: scriptable analysis of PIX (.wpix) GPU captures.

Python API
----------
    from pix_tool_set import open_capture

    capture = open_capture(r"C:\\caps\\frame.wpix")
    capture.frame_statistics()
    capture.find_draw_calls(pass_name="Lumen", limit=10)

Tool API (what the CLI and any AI client use)
---------------------------------------------
    from pix_tool_set import call_tool

    result = call_tool("list-passes", {"limit": 10})
    result["status"], result["data"]
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .context import ToolContext
from .engine.capture import Capture
from .errors import PixToolError
from .registry import ToolDefinition, get_registry
from .results import ToolResult
from .session import SessionStore
from .tools import load_builtin_tools

__version__ = "2.0.0"

__all__ = [
    "Capture",
    "PixToolError",
    "SessionStore",
    "ToolContext",
    "ToolDefinition",
    "ToolResult",
    "call_tool",
    "list_tools",
    "open_capture",
    "__version__",
]


def call_tool(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Invoke a registered tool and return its result envelope as a dict."""
    load_builtin_tools()
    registry = get_registry()
    from .engine import activity

    timer = activity.Timer(name, args or {}, "python:call_tool", argv=[])
    try:
        definition = registry.get(name)
    except PixToolError as exc:
        envelope = ToolResult.failure(exc).to_dict()
        timer.finish(envelope)
        return envelope
    context = ToolContext.from_cwd()
    try:
        cleaned = definition.validate_args(args or {})
        result = definition.handler(cleaned, context)
    except PixToolError as exc:
        result = ToolResult.failure(exc)
    result.tool = definition.name
    envelope = result.to_dict()
    timer.finish(envelope, session=str((args or {}).get("session") or "") or None)
    return envelope


def list_tools(category: str | None = None, *, verbose: bool = False) -> list[dict[str, Any]]:
    """Machine-readable catalogue of every tool."""
    load_builtin_tools()
    return get_registry().metadata(category, verbose=verbose)


def open_capture(
    capture_path: str | Path,
    *,
    session: str | None = None,
    force: bool = False,
    skip_events: bool = False,
) -> Capture:
    """Open a capture (exporting if needed) and return the parsed engine object."""
    payload: dict[str, Any] = {"capture": str(capture_path)}
    if session:
        payload["session"] = session
    if force:
        payload["force"] = True
    if skip_events:
        payload["skip_events"] = True
    envelope = call_tool("session-open", payload)
    if envelope["status"] == "error":
        error = envelope["error"]
        raise PixToolError(
            code=error["code"],
            message=error["message"],
            stage=error["stage"],
            paths=error.get("paths", []),
            suggestion=error.get("suggestion"),
            details=error.get("details", {}),
        )
    context = ToolContext.from_cwd()
    return context.capture({"session": envelope["data"]["session"]})
