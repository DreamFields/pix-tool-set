from __future__ import annotations

from typing import Any

from pix_tool_set.context import ToolContext
from pix_tool_set.event_analysis import analyze_shader_event_tree_payload, write_event_analysis
from pix_tool_set.registry import tool
from pix_tool_set.results import ToolResult
from pix_tool_set.shader_events import build_shader_event_tree


DEFAULT_TOP_LIMIT = 20
DEFAULT_SAMPLE_LIMIT = 20


def _optional_int(value: Any, default: int) -> int:
    if value is None:
        return default
    return int(value)


@tool(
    name="analyze-events",
    description="Analyze a PIX shader event tree and return event type, shader stage, PSO, and marker path statistics.",
    parameters={
        "type": "object",
        "properties": {
            "capture_path": {"type": "string", "description": "Optional PIX .wpix capture path."},
            "export_dir": {"type": "string", "description": "PIX C++ export directory."},
            "output_path": {"type": "string", "description": "Optional output JSON path for the event analysis."},
            "auto_export": {"type": "boolean", "description": "Export C++ project with pixtool when missing."},
            "refresh": {"type": "boolean", "description": "Rebuild the index even if cache is valid."},
            "top_limit": {"type": "integer", "description": "Maximum number of count rows to return for distributions. Defaults to 20."},
            "sample_limit": {"type": "integer", "description": "Maximum number of PSO and marker path examples to return. Defaults to 20."},
        },
        "additionalProperties": False,
    },
    requires_cpp_export=True,
)
def analyze_events(args: dict[str, Any], context: ToolContext) -> ToolResult:
    payload = build_shader_event_tree(
        args["export_dir"],
        refresh=bool(args.get("refresh", False)),
    )
    top_limit = _optional_int(args.get("top_limit"), DEFAULT_TOP_LIMIT)
    sample_limit = _optional_int(args.get("sample_limit"), DEFAULT_SAMPLE_LIMIT)
    analysis = analyze_shader_event_tree_payload(
        payload["tree"],
        metadata=payload["metadata"],
        top_limit=top_limit,
        sample_limit=sample_limit,
    )

    output_paths: list[str] = []
    if args.get("output_path"):
        output_paths.append(write_event_analysis(analysis, args["output_path"]))

    return ToolResult.success(
        analysis,
        output_paths=output_paths,
        diagnostics=[{"stage": "event_analysis", "root_count": len(payload["tree"])}],
    )
