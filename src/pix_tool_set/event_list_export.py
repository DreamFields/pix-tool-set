from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .cpp_export import default_export_dir, find_pixtool
from .errors import PixToolError

DEFAULT_PIXTOOL_PATH = Path("C:/Program Files/Microsoft PIX/2603.25/pixtool.exe")
EVENT_LIST_FILENAME = "event-list.csv"
CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class EventListPaths:
    capture_path: Path
    export_dir: Path
    cache_dir: Path
    csv_path: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "capture_path": str(self.capture_path),
            "export_dir": str(self.export_dir),
            "cache_dir": str(self.cache_dir),
            "csv_path": str(self.csv_path),
        }


@dataclass(frozen=True, slots=True)
class EventListExportResult:
    paths: EventListPaths
    pixtool_path: Path
    command: list[str]
    refreshed: bool
    cache_hit: bool
    stdout: str | None = None
    stderr: str | None = None

    def diagnostics(self) -> dict[str, Any]:
        return {
            "stage": "save_event_list",
            "capture_path": str(self.paths.capture_path),
            "csv_path": str(self.paths.csv_path),
            "pixtool_path": str(self.pixtool_path),
            "command": self.command,
            "refreshed": self.refreshed,
            "cache_hit": self.cache_hit,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


def resolve_event_list_paths(capture_path: str | Path, export_dir: str | Path | None = None, workspace: str | Path | None = None) -> EventListPaths:
    capture = Path(capture_path).expanduser().resolve()
    if not capture.exists():
        raise PixToolError(
            code="capture_not_found",
            message=f"Capture file does not exist: {capture}",
            stage="save_event_list",
            paths=[str(capture)],
            suggestion="Pass a valid .wpix capture_path.",
        )
    root = Path(export_dir).expanduser().resolve() if export_dir else default_export_dir(capture, Path(workspace or Path.cwd()).resolve()).resolve()
    cache_dir = root / ".cache" / "pix-tool-set"
    return EventListPaths(capture_path=capture, export_dir=root, cache_dir=cache_dir, csv_path=cache_dir / EVENT_LIST_FILENAME)


def resolve_pixtool_path(explicit_path: str | Path | None = None) -> Path:
    if explicit_path:
        candidate = Path(explicit_path).expanduser().resolve()
        if candidate.exists() and candidate.is_file():
            return candidate
        raise PixToolError(
            code="pixtool_not_found",
            message=f"pixtool.exe is not available: {candidate}",
            stage="save_event_list",
            paths=[str(candidate)],
            suggestion="Pass a valid pixtool_path or install Microsoft PIX 2603.25.",
        )
    if DEFAULT_PIXTOOL_PATH.exists() and DEFAULT_PIXTOOL_PATH.is_file():
        return DEFAULT_PIXTOOL_PATH
    discovered = find_pixtool(None)
    if discovered is not None:
        return discovered
    raise PixToolError(
        code="pixtool_not_found",
        message=f"pixtool.exe was not found. Default path is unavailable: {DEFAULT_PIXTOOL_PATH}",
        stage="save_event_list",
        paths=[str(DEFAULT_PIXTOOL_PATH)],
        suggestion="Install Microsoft PIX 2603.25, set PIXTOOL_PATH, or pass pixtool_path.",
    )


def event_list_is_current(paths: EventListPaths) -> bool:
    if not paths.csv_path.exists():
        return False
    return paths.csv_path.stat().st_mtime_ns >= paths.capture_path.stat().st_mtime_ns


def build_save_event_list_command(pixtool_path: str | Path, capture_path: str | Path, csv_path: str | Path, counters: str | None = None) -> list[str]:
    command = [str(pixtool_path), "open-capture", str(capture_path), "save-event-list", str(csv_path)]
    if counters:
        command.append(f"--counters={counters}")
    return command


def _default_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)


def export_event_list_csv(
    *,
    capture_path: str | Path,
    export_dir: str | Path | None = None,
    workspace: str | Path | None = None,
    refresh: bool = False,
    pixtool_path: str | Path | None = None,
    counters: str | None = None,
    runner: CommandRunner | None = None,
) -> EventListExportResult:
    paths = resolve_event_list_paths(capture_path, export_dir, workspace)
    paths.cache_dir.mkdir(parents=True, exist_ok=True)
    pixtool = resolve_pixtool_path(pixtool_path)
    command = build_save_event_list_command(pixtool, paths.capture_path, paths.csv_path, counters)
    if not refresh and event_list_is_current(paths):
        return EventListExportResult(paths=paths, pixtool_path=pixtool, command=command, refreshed=False, cache_hit=True)

    run = runner or _default_runner
    try:
        completed = run(command)
    except subprocess.TimeoutExpired as exc:
        raise PixToolError(
            code="save_event_list_timeout",
            message="PIX save-event-list timed out after 30 minutes.",
            stage="save_event_list",
            paths=[str(paths.capture_path), str(paths.csv_path)],
            details={"command": command, "stdout": exc.stdout, "stderr": exc.stderr, "intermediate_file": str(paths.csv_path)},
            suggestion="The capture file may be very large. Try exporting the event list manually or use a smaller capture file.",
        ) from exc
    if completed.returncode != 0:
        raise PixToolError(
            code="save_event_list_failed",
            message="PIX save-event-list failed.",
            stage="save_event_list",
            paths=[str(paths.capture_path), str(paths.csv_path)],
            details={"command": command, "returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr, "intermediate_file": str(paths.csv_path)},
        )
    if not paths.csv_path.exists():
        raise PixToolError(
            code="save_event_list_missing_output",
            message="PIX save-event-list completed but did not create the CSV file.",
            stage="save_event_list",
            paths=[str(paths.csv_path)],
            details={"command": command, "stdout": completed.stdout, "stderr": completed.stderr},
        )
    return EventListExportResult(
        paths=paths,
        pixtool_path=pixtool,
        command=command,
        refreshed=True,
        cache_hit=False,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
