"""Structured error type shared by every tool.

Every failure surfaces as a machine-readable object so an AI client can react
without parsing prose: ``code`` selects the recovery path, ``stage`` says where
it broke, and ``suggestion`` tells the caller what to try next.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class PixToolError(Exception):
    code: str
    message: str
    stage: str
    paths: list[str] = field(default_factory=list)
    suggestion: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "stage": self.stage,
            "paths": self.paths,
            "suggestion": self.suggestion,
            "details": self.details,
        }


def capture_not_found(path: str) -> PixToolError:
    return PixToolError(
        code="capture_not_found",
        message=f"Capture file does not exist: {path}",
        stage="session",
        paths=[path],
        suggestion="Check the path, or run `session-open` with an existing .wpix file.",
    )


def session_missing() -> PixToolError:
    return PixToolError(
        code="session_missing",
        message="No capture session is open and no --capture/--session was provided.",
        stage="session",
        suggestion="Run `session-open --capture <file.wpix>` first, or pass --capture explicitly.",
    )


def export_incomplete(export_dir: str, missing: list[str]) -> PixToolError:
    return PixToolError(
        code="export_incomplete",
        message=f"C++ export at {export_dir} is missing required files.",
        stage="export",
        paths=[export_dir],
        suggestion="Re-run `session-open --force` to regenerate the export.",
        details={"missing": missing},
    )


def pixtool_missing() -> PixToolError:
    return PixToolError(
        code="pixtool_missing",
        message="pixtool.exe was not found on this machine.",
        stage="export",
        suggestion=(
            "Install Microsoft PIX, or set the PIXTOOL_PATH environment variable, "
            "or pass --pixtool <path to pixtool.exe>."
        ),
    )


def not_found(kind: str, key: Any, hint: str | None = None) -> PixToolError:
    return PixToolError(
        code=f"{kind}_not_found",
        message=f"No {kind} matches {key!r}.",
        stage="query",
        suggestion=hint or f"List available {kind}s first to find a valid identifier.",
    )


def unsupported(feature: str, reason: str, suggestion: str | None = None) -> PixToolError:
    return PixToolError(
        code="unsupported",
        message=f"{feature} is not available: {reason}",
        stage="capability",
        suggestion=suggestion,
    )


def invalid_argument(name: str, reason: str) -> PixToolError:
    return PixToolError(
        code="invalid_argument",
        message=f"Invalid value for {name}: {reason}",
        stage="validation",
        suggestion="Run `describe <tool>` to see the accepted parameter schema.",
    )
