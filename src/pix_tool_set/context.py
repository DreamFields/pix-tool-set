"""Execution context handed to every tool handler."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .engine.capture import Capture
from .errors import PixToolError
from .pixtool import PixTool
from .session import SessionRecord, SessionStore

_CAPTURE_CACHE: dict[str, Capture] = {}


@dataclass(slots=True)
class ToolContext:
    workspace: Path
    store: SessionStore = field(default_factory=SessionStore)
    pixtool_path: str | None = None
    _record: Optional[SessionRecord] = field(default=None, repr=False)

    @classmethod
    def from_cwd(cls, pixtool_path: str | None = None) -> "ToolContext":
        return cls(workspace=Path.cwd(), pixtool_path=pixtool_path)

    # ------------------------------------------------------------------
    def session(self, args: dict[str, Any]) -> SessionRecord:
        """Resolve which session this invocation targets."""
        if self._record is not None:
            return self._record
        record = self.store.resolve(
            session=args.get("session"),
            capture_path=args.get("capture"),
            export_dir=args.get("export_dir"),
        )
        self._record = record
        return record

    def capture(self, args: dict[str, Any]) -> Capture:
        """Attach to the parsed capture for this session (cached per process)."""
        record = self.session(args)
        key = str(Path(record.export_dir).resolve())
        cached = _CAPTURE_CACHE.get(key)
        if cached is not None:
            self.store.touch(record.name)
            return cached

        export_dir = Path(record.export_dir)
        if not export_dir.exists():
            raise PixToolError(
                code="export_missing",
                message=f"Export directory does not exist: {export_dir}",
                stage="session",
                paths=[str(export_dir)],
                suggestion="Run `session-open --capture <file.wpix>` to create the export.",
            )
        event_csv = Path(record.event_csv) if record.event_csv else None
        if event_csv is not None and not event_csv.exists():
            event_csv = None

        pixtool: PixTool | None = None
        candidate = self.pixtool_path or record.pixtool_path
        try:
            pixtool = PixTool.locate(candidate)
        except PixToolError:
            pixtool = None

        capture = Capture(
            capture_path=Path(record.capture_path) if record.capture_path else None,
            export_dir=export_dir,
            event_csv=event_csv,
            pixtool=pixtool,
        )
        _CAPTURE_CACHE[key] = capture
        self.store.touch(record.name)
        return capture

    def require_pixtool(self, args: dict[str, Any] | None = None) -> PixTool:
        candidate = self.pixtool_path
        if args:
            candidate = args.get("pixtool") or candidate
        if candidate is None and self._record is not None:
            candidate = self._record.pixtool_path
        return PixTool.locate(candidate)

    # ------------------------------------------------------------------
    def resolve_output(self, raw: str | None, default_name: str) -> Path:
        """Turn a user-supplied output path into an absolute path."""
        if raw:
            path = Path(raw).expanduser()
            return path if path.is_absolute() else (self.workspace / path).resolve()
        record = self._record
        base = (
            Path(record.export_dir).parent / "outputs"
            if record is not None and record.export_dir
            else self.workspace / "outputs"
        )
        return (base / default_name).resolve()


def clear_capture_cache() -> None:
    _CAPTURE_CACHE.clear()
