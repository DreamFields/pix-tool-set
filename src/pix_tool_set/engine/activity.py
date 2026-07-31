"""Append-only record of every tool invocation, for the live viewer and for replay.

Why a log at all: the CLI is stateless and short-lived, so "what did I just run and
what came back" is otherwise lost the moment the terminal scrolls. Recording it makes
a session reviewable and replayable after the fact.

Two files per invocation, deliberately:

  * one small line appended to ``activity.jsonl`` - the index. Kept tiny so a single
    ``write()`` stays effectively atomic when several CLI processes run at once, and so
    the viewer can tail the file by byte offset instead of re-reading it.
  * the full result envelope in ``payloads/<id>.json``. Result data is unbounded (a
    disassembly is hundreds of KB), and inlining it would bloat the index and make the
    viewer slow. Each process writes its own uniquely named payload, so there is no
    write race to arbitrate.

The byte offset of the index file is the viewer's cursor. That is what makes tailing
correct under concurrent writers: a reader never has to guess how many entries exist,
it just asks for whatever was appended past the offset it already has.

Recording never breaks a call. Every failure path here is swallowed, because losing a
log line is trivial and losing the user's actual result is not.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from ..session import sessions_root

INDEX_NAME = "activity.jsonl"
PAYLOAD_DIR = "payloads"

# Keep the newest N payloads. The index itself is cheap to keep whole.
PAYLOAD_KEEP = 3000
# Inline preview caps, so the index line stays small.
_STR_PREVIEW = 120
_DIGEST_KEYS = 14


def disabled() -> bool:
    """Recording is opt-out through the environment, for scripted bulk runs."""
    return os.environ.get("PIX_TOOL_SET_NO_LOG", "").strip().lower() in {"1", "true", "yes", "on"}


def activity_root() -> Path:
    override = os.environ.get("PIX_TOOL_SET_ACTIVITY_DIR")
    if override:
        return Path(override)
    return sessions_root() / "activity"


def index_path() -> Path:
    return activity_root() / INDEX_NAME


def payload_dir() -> Path:
    return activity_root() / PAYLOAD_DIR


# ----------------------------------------------------------------------
def _digest(data: Any) -> Any:
    """A bounded, human-readable gist of a result payload.

    Deliberately generic rather than per-tool: shapes vary, and a viewer row only needs
    enough to recognise the call. Full detail is one click away in the payload file.

    Collapsed containers are marked ``<list: n>`` / ``<dict: n keys>`` rather than
    ``[n items]``, so a reader can never mistake a placeholder for a real string value
    that happened to look like one.
    """
    if data is None:
        return None
    if isinstance(data, (int, float, bool)):
        return data
    if isinstance(data, str):
        return data if len(data) <= _STR_PREVIEW else data[: _STR_PREVIEW - 3] + "..."
    if isinstance(data, list):
        return f"<list: {len(data)}>"
    if not isinstance(data, dict):
        return f"<{type(data).__name__}>"

    out: dict[str, Any] = {}
    for key, value in list(data.items())[:_DIGEST_KEYS]:
        if value is None or isinstance(value, (int, float, bool)):
            out[key] = value
        elif isinstance(value, str):
            out[key] = value if len(value) <= _STR_PREVIEW else value[: _STR_PREVIEW - 3] + "..."
        elif isinstance(value, list):
            out[key] = f"<list: {len(value)}>"
        elif isinstance(value, dict):
            out[key] = f"<dict: {len(value)} keys>"
        else:
            out[key] = f"<{type(value).__name__}>"
    if len(data) > _DIGEST_KEYS:
        out["<truncated>"] = f"{len(data) - _DIGEST_KEYS} more key(s)"
    return out


def _display_command(argv: list[str] | None, entry: str, tool: str, args: dict[str, Any]) -> str:
    """Reconstruct what the user typed, or a faithful stand-in for API calls."""
    if argv:
        parts = ["pix-tool-set", *argv[1:]] if argv else []
        return " ".join(parts)
    rendered = " ".join(
        f"--{key.replace('_', '-')} {value!r}" if not isinstance(value, bool) else
        f"--{key.replace('_', '-')}"
        for key, value in (args or {}).items()
    )
    prefix = "call_tool" if entry.startswith("python") else "pix-tool-set"
    return f"{prefix} {tool} {rendered}".strip()


def _prune_payloads(directory: Path) -> None:
    """Drop the oldest payloads once the directory grows past the cap."""
    try:
        entries = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    excess = len(entries) - PAYLOAD_KEEP
    for stale in entries[:excess] if excess > 0 else []:
        try:
            stale.unlink()
        except OSError:
            pass


# ----------------------------------------------------------------------
def record(
    *,
    tool: str,
    args: dict[str, Any] | None,
    envelope: dict[str, Any],
    started_at: float,
    finished_at: float,
    entry: str,
    session: str | None = None,
    argv: list[str] | None = None,
) -> str | None:
    """Append one invocation to the log. Returns its id, or None when not recorded."""
    if disabled():
        return None
    try:
        root = activity_root()
        payloads = payload_dir()
        payloads.mkdir(parents=True, exist_ok=True)

        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(started_at))
        record_id = f"{stamp}-{uuid.uuid4().hex[:10]}"

        data = envelope.get("data")
        full = json.dumps(envelope, ensure_ascii=False, default=str)
        (payloads / f"{record_id}.json").write_text(full, encoding="utf-8")

        line = {
            "id": record_id,
            "tool": tool,
            "status": envelope.get("status", "unknown"),
            "entry": entry,
            "session": session,
            "started_at": round(started_at, 3),
            "finished_at": round(finished_at, 3),
            "duration_ms": round((finished_at - started_at) * 1000.0, 2),
            "command": _display_command(argv, entry, tool, args or {}),
            "args": _digest(args or {}),
            "summary": _digest(data),
            "output_paths": list(envelope.get("output_paths") or [])[:8],
            "diagnostics": [
                {"level": d.get("level"), "message": d.get("message")}
                for d in (envelope.get("diagnostics") or [])[:4]
            ],
            "payload_bytes": len(full),
            "cwd": os.getcwd(),
            "pid": os.getpid(),
        }
        error = envelope.get("error")
        if error:
            line["error"] = {
                "code": error.get("code"),
                "message": error.get("message"),
                "stage": error.get("stage"),
            }

        target = root / INDEX_NAME
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")

        # Cheap amortised prune: only every so often, keyed off the id so concurrent
        # processes do not all scan at once.
        if record_id.endswith(("0", "7")):
            _prune_payloads(payloads)
        return record_id
    except Exception:  # noqa: BLE001 - logging must never break a real call
        return None


# ----------------------------------------------------------------------
def read_since(offset: int = 0) -> tuple[list[dict[str, Any]], int, int]:
    """Entries appended past ``offset``, plus the new offset and the file size.

    Byte offsets rather than entry counts: a tailing reader stays correct even while
    another process appends, and never re-parses what it already has.
    """
    target = index_path()
    if not target.exists():
        return [], 0, 0
    try:
        size = target.stat().st_size
    except OSError:
        return [], offset, offset
    if offset > size:
        # The log was cleared or replaced; start over rather than reading garbage.
        offset = 0
    entries: list[dict[str, Any]] = []
    with open(target, "rb") as handle:
        handle.seek(offset)
        blob = handle.read()
        consumed = offset + len(blob)
        # A partial trailing line means a writer is mid-append; leave it for next poll.
        if blob and not blob.endswith(b"\n"):
            cut = blob.rfind(b"\n")
            if cut == -1:
                return [], offset, size
            consumed = offset + cut + 1
            blob = blob[: cut + 1]
    for raw in blob.decode("utf-8", "replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            entries.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return entries, consumed, size


def read_all() -> list[dict[str, Any]]:
    entries, _, _ = read_since(0)
    return entries


def read_payload(record_id: str) -> dict[str, Any] | None:
    """Full envelope for one invocation. Ids are validated, not trusted."""
    if not record_id or not all(ch.isalnum() or ch == "-" for ch in record_id):
        return None
    target = payload_dir() / f"{record_id}.json"
    if not target.exists():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def stats(entries: Iterable[dict[str, Any]] | None = None) -> dict[str, Any]:
    rows = list(entries) if entries is not None else read_all()
    by_status: dict[str, int] = {}
    by_tool: dict[str, int] = {}
    total_ms = 0.0
    slowest: dict[str, Any] | None = None
    for row in rows:
        by_status[row.get("status", "unknown")] = by_status.get(row.get("status", "unknown"), 0) + 1
        by_tool[row.get("tool", "?")] = by_tool.get(row.get("tool", "?"), 0) + 1
        duration = float(row.get("duration_ms") or 0.0)
        total_ms += duration
        if slowest is None or duration > float(slowest.get("duration_ms") or 0.0):
            slowest = row
    index = index_path()
    return {
        "total_calls": len(rows),
        "by_status": dict(sorted(by_status.items(), key=lambda kv: -kv[1])),
        "by_tool": dict(sorted(by_tool.items(), key=lambda kv: -kv[1])[:20]),
        "distinct_tools": len(by_tool),
        "total_duration_ms": round(total_ms, 2),
        "first_at": rows[0].get("started_at") if rows else None,
        "last_at": rows[-1].get("started_at") if rows else None,
        "slowest": (
            {"tool": slowest.get("tool"), "duration_ms": slowest.get("duration_ms")}
            if slowest
            else None
        ),
        "log_path": str(index),
        "log_bytes": index.stat().st_size if index.exists() else 0,
        "recording_enabled": not disabled(),
    }


def clear() -> dict[str, Any]:
    """Remove the index and every payload. Reports what went."""
    removed_payloads = 0
    directory = payload_dir()
    if directory.exists():
        for entry in directory.glob("*.json"):
            try:
                entry.unlink()
                removed_payloads += 1
            except OSError:
                pass
    target = index_path()
    had_index = target.exists()
    if had_index:
        try:
            target.unlink()
        except OSError:
            had_index = False
    return {"index_removed": had_index, "payloads_removed": removed_payloads}


# ----------------------------------------------------------------------
class Timer:
    """Times one invocation and records it, whatever the outcome.

    Used by both entry points so a call is logged identically whether it arrived from
    the CLI or from ``call_tool``.
    """

    __slots__ = ("tool", "args", "entry", "argv", "started_at", "_id")

    def __init__(
        self,
        tool: str,
        args: dict[str, Any] | None,
        entry: str,
        argv: list[str] | None = None,
    ) -> None:
        self.tool = tool
        self.args = args or {}
        self.entry = entry
        self.argv = argv if argv is not None else list(sys.argv)
        self.started_at = time.time()
        self._id: str | None = None

    @property
    def record_id(self) -> str | None:
        return self._id

    def finish(self, envelope: dict[str, Any], session: str | None = None) -> str | None:
        self._id = record(
            tool=self.tool or envelope.get("tool") or "?",
            args=self.args,
            envelope=envelope,
            started_at=self.started_at,
            finished_at=time.time(),
            entry=self.entry,
            session=session,
            argv=self.argv,
        )
        return self._id
