from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from .errors import PixToolError

HEADER_ALIASES = {
    "global_id": {"globalid", "global id", "eventid", "event id", "id", "queue id", "queueid"},
    "name": {"name", "event name", "event", "eventname", "label"},
    "depth": {"depth", "level", "hierarchy", "indent", "event depth"},
    "parent_global_id": {"parentglobalid", "parent global id", "parent id", "parentid", "parent"},
    "start_time": {"start", "start time", "starttime", "timestamp", "time", "cpu start", "gpu start"},
    "duration": {"duration", "duration ms", "duration (ms)", "elapsed", "elapsed ms", "gpu duration", "cpu duration"},
}
HEADER_PRIORITY = {
    "global_id": ["queue id", "queueid", "global id", "globalid", "event id", "eventid", "id"],
}
REQUIRED_FIELDS = ("global_id", "name")
OPTIONAL_FIELDS = ("depth", "parent_global_id", "start_time", "duration")


def _normalize_header(value: str | None) -> str:
    return " ".join(str(value or "").strip().lower().replace("_", " ").replace("-", " ").split())


def _canonical_header(value: str | None) -> str | None:
    normalized = _normalize_header(value)
    compact = normalized.replace(" ", "")
    for canonical, aliases in HEADER_ALIASES.items():
        if normalized in aliases or compact in aliases:
            return canonical
    return None


def _header_map(fieldnames: list[str] | None, csv_path: Path) -> dict[str, str]:
    if not fieldnames:
        raise PixToolError(
            code="event_list_csv_missing_header",
            message=f"Event list CSV has no header row: {csv_path}",
            stage="event_list_csv",
            paths=[str(csv_path)],
        )
    mapping: dict[str, str] = {}
    priorities: dict[str, int] = {}
    for header in fieldnames:
        canonical = _canonical_header(header)
        if not canonical:
            continue
        normalized = _normalize_header(header)
        compact = normalized.replace(" ", "")
        priority_values = HEADER_PRIORITY.get(canonical, [])
        priority = len(priority_values)
        for candidate in (normalized, compact):
            if candidate in priority_values:
                priority = min(priority, priority_values.index(candidate))
        if canonical not in mapping or priority < priorities.get(canonical, len(priority_values)):
            mapping[canonical] = header
            priorities[canonical] = priority
    missing = [field for field in REQUIRED_FIELDS if field not in mapping]
    if missing:
        raise PixToolError(
            code="event_list_csv_missing_required_field",
            message=f"Event list CSV is missing required field(s): {', '.join(missing)}",
            stage="event_list_csv",
            paths=[str(csv_path)],
            details={"missing_fields": missing, "csv_path": str(csv_path), "available_fields": fieldnames},
        )
    return mapping


def _value(row: dict[str, str], mapping: dict[str, str], field: str) -> str | None:
    source = mapping.get(field)
    if not source:
        return None
    value = row.get(source)
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _int_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def _is_shader_event_name(name: str | None) -> bool:
    return str(name or "") in {
        "Dispatch",
        "DispatchIndirect",
        "DispatchMesh",
        "DispatchRays",
        "DrawInstanced",
        "DrawIndexedInstanced",
        "Draw",
        "DrawIndexed",
        "ExecuteIndirect",
    }


def _stage_group(name: str | None) -> str | None:
    if name in {"Dispatch", "DispatchIndirect"}:
        return "compute"
    if name == "DispatchRays":
        return "raytracing"
    if _is_shader_event_name(name):
        return "graphics_or_indirect"
    return None


def _counter_values(row: dict[str, str], mapped_headers: set[str]) -> dict[str, str]:
    counters: dict[str, str] = {}
    for key, value in row.items():
        if key in mapped_headers:
            continue
        text = str(value or "").strip()
        if text:
            counters[str(key)] = text
    return counters


def _raw_row(row: dict[str, Any]) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    for key, value in row.items():
        raw_key = str(key) if key is not None else "extra_columns"
        raw[raw_key] = value
    return raw


def parse_event_list_csv(csv_path: str | Path) -> dict[str, Any]:
    path = Path(csv_path).resolve()
    events: list[dict[str, Any]] = []
    events_by_global_id: dict[str, dict[str, Any]] = {}
    stack_by_depth: dict[int, str] = {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            mapping = _header_map(reader.fieldnames, path)
            mapped_headers = set(mapping.values())
            for row_number, row in enumerate(reader, start=2):
                global_id = _value(row, mapping, "global_id")
                name = _value(row, mapping, "name")
                if not global_id or not name:
                    raise PixToolError(
                        code="event_list_csv_missing_required_value",
                        message=f"Event list CSV row {row_number} is missing global_id or name.",
                        stage="event_list_csv",
                        paths=[str(path)],
                        details={"row_number": row_number, "csv_path": str(path), "missing_fields": [field for field in REQUIRED_FIELDS if not _value(row, mapping, field)]},
                    )
                depth = _int_or_none(_value(row, mapping, "depth"))
                explicit_parent = _value(row, mapping, "parent_global_id")
                parent_global_id = explicit_parent
                if parent_global_id is None and depth is not None and depth > 0:
                    parent_global_id = stack_by_depth.get(depth - 1)
                if depth is not None:
                    stack_by_depth[depth] = global_id
                    for existing_depth in list(stack_by_depth):
                        if existing_depth > depth:
                            del stack_by_depth[existing_depth]
                counters = _counter_values(row, mapped_headers)
                event = {
                    "global_id": global_id,
                    "name": name,
                    "event_type": name,
                    "is_shader_event": _is_shader_event_name(name),
                    "shader_stage_group": _stage_group(name),
                    "file": str(path),
                    "line": row_number,
                    "parent_global_id": parent_global_id,
                    "marker_path": [],
                    "pso_id": None,
                    "root_signature_id": None,
                    "root_descriptor_tables": {},
                    "root_constant_buffer_views": {},
                    "input_assembler": {"vertex_buffers": [], "index_buffer": None},
                    "output_merger": {"render_targets": [], "depth_stencil": None},
                    "resource_refs": [],
                    "calls": [],
                    "event_list": {
                        "depth": depth,
                        "start_time": _value(row, mapping, "start_time"),
                        "duration": _value(row, mapping, "duration"),
                        "counters": counters,
                        "raw": _raw_row(row),
                    },
                }
                events.append(event)
                events_by_global_id[global_id] = event
    except PixToolError:
        raise
    except OSError as exc:
        raise PixToolError(
            code="event_list_csv_read_failed",
            message=f"Failed to read event list CSV: {path}",
            stage="event_list_csv",
            paths=[str(path)],
            details={"error": str(exc)},
        ) from exc
    except csv.Error as exc:
        raise PixToolError(
            code="event_list_csv_parse_failed",
            message=f"Failed to parse event list CSV: {path}",
            stage="event_list_csv",
            paths=[str(path)],
            details={"error": str(exc)},
        ) from exc

    return {
        "events": events,
        "events_by_global_id": events_by_global_id,
        "shader_event_global_ids": [event["global_id"] for event in events if event.get("is_shader_event")],
        "diagnostics": {
            "event_list_csv_path": str(path),
            "event_count": len(events),
            "shader_event_count": sum(1 for event in events if event.get("is_shader_event")),
        },
    }
