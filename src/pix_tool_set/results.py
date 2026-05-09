from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .errors import PixToolError

ToolStatus = Literal["success", "error", "partial"]


@dataclass(slots=True)
class ToolResult:
    status: ToolStatus
    data: dict[str, Any] = field(default_factory=dict)
    output_paths: list[str] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    error: PixToolError | None = None

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

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "data": self.data,
            "output_paths": self.output_paths,
            "diagnostics": self.diagnostics,
        }
        if self.error is not None:
            payload["error"] = self.error.to_dict()
        return payload
