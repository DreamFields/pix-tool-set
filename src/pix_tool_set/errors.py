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

    def __str__(self) -> str:
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
