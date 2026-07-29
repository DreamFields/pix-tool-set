"""Uniform tool result envelope.

Every tool returns the same shape so an AI client can consume results without
per-tool special casing::

    {
      "status": "success" | "partial" | "error",
      "tool": "<tool name>",
      "data": { ... },
      "output_paths": ["..."],
      "diagnostics": [{"level": "...", "message": "..."}],
      "error": {...}          # only when status == "error"
    }

``partial`` matters for an agent: it means the answer is usable but something
was degraded (a capability was missing, a limit truncated the list, ...).  The
reason is always in ``diagnostics``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .errors import PixToolError

ToolStatus = Literal["success", "error", "partial"]


def diagnostic(level: str, message: str, **details: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"level": level, "message": message}
    if details:
        entry.update(details)
    return entry


@dataclass(slots=True)
class ToolResult:
    status: ToolStatus
    data: dict[str, Any] = field(default_factory=dict)
    output_paths: list[str] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    error: PixToolError | None = None
    tool: str = ""

    @classmethod
    def success(
        cls,
        data: dict[str, Any] | None = None,
        output_paths: list[str] | None = None,
        diagnostics: list[dict[str, Any]] | None = None,
    ) -> "ToolResult":
        return cls(
            status="success",
            data=data or {},
            output_paths=output_paths or [],
            diagnostics=diagnostics or [],
        )

    @classmethod
    def partial(
        cls,
        data: dict[str, Any] | None = None,
        output_paths: list[str] | None = None,
        diagnostics: list[dict[str, Any]] | None = None,
    ) -> "ToolResult":
        return cls(
            status="partial",
            data=data or {},
            output_paths=output_paths or [],
            diagnostics=diagnostics or [],
        )

    @classmethod
    def failure(cls, error: PixToolError) -> "ToolResult":
        return cls(status="error", error=error)

    def add_diagnostic(self, level: str, message: str, **details: Any) -> "ToolResult":
        self.diagnostics.append(diagnostic(level, message, **details))
        return self

    def degrade(self, message: str, **details: Any) -> "ToolResult":
        """Mark a successful result as partial with an explanation."""
        if self.status == "success":
            self.status = "partial"
        return self.add_diagnostic("warning", message, **details)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "tool": self.tool,
            "data": self.data,
            "output_paths": self.output_paths,
            "diagnostics": self.diagnostics,
        }
        if self.error is not None:
            payload["error"] = self.error.to_dict()
        return payload
