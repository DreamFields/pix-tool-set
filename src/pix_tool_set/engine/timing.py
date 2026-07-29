"""Optional GPU counter enrichment for the exported event list.

`pixtool save-event-list` accepts `--counters=<glob>`, which adds one CSV column
per matching counter. Two hard constraints found by testing PIX 2603.25:

  * A counter name containing spaces cannot be passed literally; pixtool rejects
    it as an unknown option. A glob such as `*Duration*` works and is the only
    practical way to select timing counters.
  * `--counters=*` requests every counter and fails on this capture with
    E_PIX_PERFORMANCE_ANALYSIS_FAILED after ~39s. Narrow globs succeed.

Timing export costs a full replay (~105s on a 2.5 GB capture), so it is opt-in
and cached next to the base event list.
"""

from __future__ import annotations

import csv
import io
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

# Counters worth having per event. Globs only - see the note above.
TIMING_GLOB = "*Duration*"

# Preferred timing column, best first. PIX emits several near-identical ones.
_TIMING_COLUMNS = (
    "TOP to EOP Duration (ns)",
    "EOP to EOP Duration (ns)",
    "gpu__time_duration.sum",
)

_BASE_COLUMNS = {"Queue ID", "Parent", "Name", "Global ID"}


@dataclass(slots=True)
class EventTiming:
    """Measured GPU duration for one event, keyed by both PIX identifiers."""

    queue_id: int
    global_id: Optional[int]
    duration_ns: int
    column: str

    @property
    def duration_ms(self) -> float:
        return self.duration_ns / 1_000_000.0


class TimingTable:
    """Parsed counter-enriched event list."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.by_queue_id: dict[int, EventTiming] = {}
        self.by_global_id: dict[int, EventTiming] = {}
        self.counter_columns: list[str] = []
        self.timing_column: str = ""
        self._load()

    def _load(self) -> None:
        with io.open(self.path, encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, skipinitialspace=True)
            fields = [f for f in (reader.fieldnames or []) if f]
            self.counter_columns = [f for f in fields if f not in _BASE_COLUMNS]
            self.timing_column = next(
                (name for name in _TIMING_COLUMNS if name in fields),
                self.counter_columns[0] if self.counter_columns else "",
            )
            if not self.timing_column:
                return
            for row in reader:
                raw = (row.get(self.timing_column) or "").strip()
                if not raw:
                    continue
                try:
                    duration = int(float(raw))
                except ValueError:
                    continue
                try:
                    queue_id = int((row.get("Queue ID") or "").strip())
                except ValueError:
                    continue
                gid_raw = (row.get("Global ID") or "").strip()
                global_id = int(gid_raw) if gid_raw.isdigit() else None
                entry = EventTiming(queue_id, global_id, duration, self.timing_column)
                self.by_queue_id[queue_id] = entry
                if global_id is not None:
                    self.by_global_id[global_id] = entry

    @property
    def measured_count(self) -> int:
        return len(self.by_queue_id)

    def lookup(
        self, *, global_id: int | None = None, queue_id: int | None = None
    ) -> Optional[EventTiming]:
        if global_id is not None:
            found = self.by_global_id.get(global_id)
            if found is not None:
                return found
        if queue_id is not None:
            return self.by_queue_id.get(queue_id)
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "timing_column": self.timing_column,
            "counter_columns": self.counter_columns,
            "measured_events": self.measured_count,
        }


def timing_csv_path(cache_dir: Path, capture_stem: str) -> Path:
    return Path(cache_dir) / f"{capture_stem}.events.timing.csv"


def export_timing_csv(
    pixtool_exe: Path,
    capture: Path,
    destination: Path,
    *,
    counters: str = TIMING_GLOB,
    timeout: int = 1800,
) -> dict[str, Any]:
    """Replay the capture to produce an event list with counter columns.

    Returns a report dict; raises nothing so the caller can degrade gracefully.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(pixtool_exe),
        "open-capture",
        str(capture),
        "save-event-list",
        str(destination),
        f"--counters={counters}",
    ]
    started = time.time()
    try:
        proc = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "elapsed_seconds": round(time.time() - started, 1),
            "error": f"pixtool timed out after {timeout}s",
            "counters": counters,
        }
    elapsed = round(time.time() - started, 1)
    if proc.returncode != 0 or not destination.exists():
        tail = (proc.stdout or proc.stderr or "").strip().splitlines()[-3:]
        return {
            "ok": False,
            "elapsed_seconds": elapsed,
            "exit_code": proc.returncode,
            "error": " | ".join(tail) or "pixtool failed",
            "counters": counters,
        }
    return {
        "ok": True,
        "elapsed_seconds": elapsed,
        "path": str(destination),
        "size_bytes": destination.stat().st_size,
        "counters": counters,
    }
