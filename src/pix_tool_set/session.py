"""Named sessions so an AI client can open a capture once and reuse it.

A CLI process is short-lived, so "open capture" cannot mean "hold it in RAM".
Instead ``session-open`` performs the expensive work once (pixtool export plus
event list) and records where the artifacts live in a small JSON registry under
the user profile.  Every later command resolves a session by name (or falls back
to the most recently used one) and re-attaches to those artifacts instantly.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from .errors import PixToolError, capture_not_found, session_missing

SESSION_FILE_VERSION = 1


def sessions_root() -> Path:
    override = os.environ.get("PIX_TOOL_SET_HOME")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        return Path(base) / "pix-tool-set"
    return Path.home() / ".pix-tool-set"


def sessions_file() -> Path:
    return sessions_root() / "sessions.json"


@dataclass(slots=True)
class SessionRecord:
    name: str
    capture_path: str
    export_dir: str
    event_csv: str | None = None
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    pixtool_path: str | None = None
    notes: str = ""
    shader_pdb_dirs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> dict[str, Any]:
        capture = Path(self.capture_path)
        return {
            "session": self.name,
            "capture_path": self.capture_path,
            "capture_exists": capture.exists(),
            "capture_size_bytes": capture.stat().st_size if capture.exists() else 0,
            "export_dir": self.export_dir,
            "export_exists": Path(self.export_dir).exists(),
            "event_csv": self.event_csv,
            "shader_pdb_dirs": list(self.shader_pdb_dirs),
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
        }


class SessionStore:
    """JSON-backed registry of named sessions."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or sessions_file()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": SESSION_FILE_VERSION, "active": None, "sessions": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"version": SESSION_FILE_VERSION, "active": None, "sessions": {}}
        payload.setdefault("sessions", {})
        payload.setdefault("active", None)
        return payload

    def _save(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # ------------------------------------------------------------------
    def list(self) -> list[SessionRecord]:
        payload = self._load()
        records = [SessionRecord(**value) for value in payload["sessions"].values()]
        records.sort(key=lambda r: -r.last_used_at)
        return records

    def active_name(self) -> str | None:
        return self._load().get("active")

    def get(self, name: str) -> Optional[SessionRecord]:
        payload = self._load()
        raw = payload["sessions"].get(name)
        if raw is None:
            resolved = self._match_name(payload, name)
            raw = payload["sessions"].get(resolved) if resolved else None
        return SessionRecord(**raw) if raw else None

    @staticmethod
    def _match_name(payload: dict[str, Any], name: str) -> str | None:
        """Resolve a session name case-insensitively.

        Session names default to the capture's file stem, and Windows paths are not
        case-sensitive, so `Tiled.wpix` is just as legitimately referred to as
        ``tiled``. Requiring the exact casing turns a correct request into
        "No session named 'tiled'", which reads as "the session is gone" rather than
        "type it differently".

        An exact match always wins; this is only consulted when that fails. An
        ambiguous fold (two sessions differing only by case) is left unresolved so
        the caller gets the explicit not-found error listing both, rather than a
        silent guess between them.
        """
        folded = name.casefold()
        matches = [key for key in payload["sessions"] if key.casefold() == folded]
        return matches[0] if len(matches) == 1 else None

    def put(self, record: SessionRecord, *, make_active: bool = True) -> SessionRecord:
        payload = self._load()
        # Reuse the stored spelling of an equivalent name. Letting both "Tiled" and
        # "tiled" exist would make every case-insensitive lookup ambiguous, so the
        # first spelling registered wins and re-opening updates it in place.
        if record.name not in payload["sessions"]:
            existing = self._match_name(payload, record.name)
            if existing is not None:
                record.name = existing
        payload["sessions"][record.name] = record.to_dict()
        if make_active:
            payload["active"] = record.name
        self._save(payload)
        return record

    def touch(self, name: str) -> None:
        payload = self._load()
        entry = payload["sessions"].get(name)
        if entry is None:
            # Mirror `get`: a name that resolved for reading must also resolve here,
            # or the session would load fine yet never become the active one.
            name = self._match_name(payload, name) or name
            entry = payload["sessions"].get(name)
        if entry is None:
            return
        entry["last_used_at"] = time.time()
        payload["active"] = name
        self._save(payload)

    def remove(self, name: str) -> bool:
        payload = self._load()
        if name not in payload["sessions"]:
            name = self._match_name(payload, name) or name
        existed = payload["sessions"].pop(name, None) is not None
        if payload.get("active") == name:
            remaining = sorted(
                payload["sessions"].values(), key=lambda r: -r.get("last_used_at", 0)
            )
            payload["active"] = remaining[0]["name"] if remaining else None
        self._save(payload)
        return existed

    def clear(self) -> int:
        payload = self._load()
        count = len(payload["sessions"])
        self._save({"version": SESSION_FILE_VERSION, "active": None, "sessions": {}})
        return count

    # ------------------------------------------------------------------
    def resolve(
        self,
        *,
        session: str | None = None,
        capture_path: str | None = None,
        export_dir: str | None = None,
    ) -> SessionRecord:
        """Find the session a command should operate on."""
        if session:
            record = self.get(session)
            if record is None:
                raise PixToolError(
                    code="session_not_found",
                    message=f"No session named {session!r}.",
                    stage="session",
                    suggestion="Run `session-list` to see open sessions.",
                    details={"known": [r.name for r in self.list()]},
                )
            return record

        if capture_path:
            capture = Path(capture_path).expanduser().resolve()
            if not capture.exists():
                raise capture_not_found(str(capture))
            for record in self.list():
                if Path(record.capture_path) == capture:
                    return record
            derived_export = (
                Path(export_dir).expanduser().resolve()
                if export_dir
                else default_export_dir(capture)
            )
            return SessionRecord(
                name=default_session_name(capture),
                capture_path=str(capture),
                export_dir=str(derived_export),
                event_csv=str(derived_export.parent / f"{capture.stem}.events.csv"),
            )

        if export_dir:
            resolved = Path(export_dir).expanduser().resolve()
            return SessionRecord(
                name=resolved.name,
                capture_path="",
                export_dir=str(resolved),
                event_csv=None,
            )

        active = self.active_name()
        if active:
            record = self.get(active)
            if record is not None:
                return record
        raise session_missing()


def default_session_name(capture: Path) -> str:
    return capture.stem


def default_export_dir(capture: Path) -> Path:
    """Cache directory beside the capture: ``<name>.pixcache/cpp``."""
    return capture.parent / f"{capture.stem}.pixcache" / "cpp"
