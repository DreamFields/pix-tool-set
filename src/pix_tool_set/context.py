from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ToolContext:
    workspace: Path
    config: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_cwd(cls) -> "ToolContext":
        return cls(workspace=Path.cwd())
