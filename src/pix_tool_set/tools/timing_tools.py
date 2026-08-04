"""Export and query measured GPU timing from the PIX event list."""

from __future__ import annotations

from typing import Any

from ..context import ToolContext
from ..engine import timing as timing_mod
from ..errors import not_found
from ..pixtool import find_pixtool
from ..results import ToolResult
from ._common import (
    PAGE_PARAMS,
    page_args,
    page_envelope,
    pass_identity,
    tool,
    with_session,
)

_NOTE = (
    "Timing comes from a real GPU replay driven by `pixtool save-event-list --counters`, "
    "so it is measured rather than modelled. The replay costs roughly 100s on a 2.5 GB "
    "capture and is cached next to the event list, so later calls are instant. Counter "
    "names containing spaces cannot be requested individually (pixtool rejects them); a "
    "glob such as `*Duration*` is the supported way to select them, and `--counters=*` "
    "fails outright on large captures."
)


@tool(
    name="export-timing",
    summary=(
        "Replay the capture to export an event list enriched with GPU duration counters, "
        "upgrading pass-cost and event-timing from estimates to measurements."
    ),
    category="performance",
    parameters=with_session(
        counters={
            "type": "string",
            "description": (
                "Counter glob passed to pixtool. Default '*Duration*'. Must be a glob when "
                "the counter name contains spaces."
            ),
        },
        force={
            "type": "boolean",
            "description": "Re-export even if a cached timing CSV already exists.",
        },
        timeout={"type": "integer", "description": "Seconds to allow for the replay. Default 1800."},
    ),
    returns="Export report plus the counter columns and how many events carry a measurement.",
    examples=[
        "pix-tool-set export-timing",
        "pix-tool-set export-timing --counters '*Duration*' --force",
    ],
    notes=_NOTE,
)
def export_timing(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    if capture.event_csv is None:
        raise not_found("event list", "events.csv", "Re-open the session to export it.")

    table, report = timing_mod.ensure_timing(
        capture,
        pixtool_exe=find_pixtool(),
        counters=str(args.get("counters") or timing_mod.TIMING_GLOB),
        timeout=int(args.get("timeout") or 1800),
        force=bool(args.get("force")),
    )
    if table is None:
        result = ToolResult.partial({"reused_cache": False, "measurement": report})
        result.degrade(
            "pixtool could not export the counter-enriched event list; timing stays "
            f"estimated: {report.get('reason')}",
            counters=report.get("counters"),
        )
        return result

    reused = report.get("source") == "cache"
    result = ToolResult.success(
        {
            "reused_cache": reused,
            "export": report.get("export"),
            "timing": table.to_dict(),
            **({"hint": "Pass --force to re-export."} if reused else {}),
        }
    )
    result.add_diagnostic(
        "info",
        f"{table.measured_count:,} events carry a measured duration from "
        f"column '{table.timing_column}'."
        + ("" if reused else " pass-cost and event-timing now report real GPU time."),
    )
    return result


@tool(
    name="event-timing",
    summary=(
        "Measured GPU duration per event or per pass, sorted by cost. Replays the capture "
        "once to measure it if no cached measurement exists."
    ),
    category="performance",
    parameters=with_session(
        PAGE_PARAMS,
        group_by={
            "type": "string",
            "enum": ["event", "pass"],
            "description": "Report individual events or aggregate per pass. Default pass.",
        },
        global_id={"type": "integer", "description": "PIX GUI 'Global ID' to look up."},
        queue_id={"type": "integer", "description": "PIX GUI 'Queue ID' to look up."},
        pass_name={"type": "string", "description": "Restrict to passes matching this substring."},
        no_measure={
            "type": "boolean",
            "description": (
                "Never replay the capture; report only a cached measurement if one "
                "exists. By default the capture is measured once when nothing is cached."
            ),
        },
        force_measure={
            "type": "boolean",
            "description": "Re-run the timing replay even if a cached measurement exists.",
        },
        counters={
            "type": "string",
            "description": "Counter glob for the timing replay. Default '*Duration*'.",
        },
        timeout={
            "type": "integer",
            "description": "Seconds to allow for the timing replay. Default 1800.",
        },
    ),
    returns="Measured durations in nanoseconds and milliseconds.",
    examples=[
        "pix-tool-set event-timing --limit 15",
        "pix-tool-set event-timing --global-id 3893",
        "pix-tool-set event-timing --group-by event --limit 20",
    ],
    notes=_NOTE,
)
def event_timing(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    table, timing_report = timing_mod.ensure_timing(
        capture,
        counters=str(args.get("counters") or timing_mod.TIMING_GLOB),
        timeout=int(args.get("timeout") or 1800),
        force=bool(args.get("force_measure")),
        allow_export=not args.get("no_measure"),
    )
    if table is None:
        result = ToolResult.partial({"available": False, "measurement": timing_report})
        result.degrade(
            "No measured GPU timing is available: "
            f"{timing_report.get('reason') or 'the capture carries no duration counters'}.",
            remedy="pix-tool-set pass-cost --force-measure",
        )
        return result

    global_id = args.get("global_id")
    queue_id = args.get("queue_id")
    if global_id is not None or queue_id is not None:
        entry = table.lookup(global_id=global_id, queue_id=queue_id)
        if entry is None:
            label = (
                f"global_id={global_id}" if global_id is not None else f"queue_id={queue_id}"
            )
            raise not_found(
                "timing", label, "That event carries no counter sample in this capture."
            )
        event = capture.resolve_event(global_id=entry.global_id, queue_id=entry.queue_id)
        pass_entry = capture.find_pass_by_event(
            global_id=entry.global_id, queue_id=entry.queue_id
        )
        return ToolResult.success(
            {
                "available": True,
                "timing_column": table.timing_column,
                "event": {
                    "queue_id": entry.queue_id,
                    "global_id": entry.global_id,
                    "name": event.name if event else None,
                    "duration_ns": entry.duration_ns,
                    "duration_ms": round(entry.duration_ms, 4),
                },
                "pass": (
                    {
                        "pass_index": pass_entry["pass_index"],
                        "name": pass_entry["name"],
                        **pass_identity(pass_entry),
                    }
                    if pass_entry
                    else None
                ),
            }
        )

    offset, limit = page_args(args, default_limit=20)
    group_by = str(args.get("group_by") or "pass")
    needle = str(args.get("pass_name") or "").lower()

    if group_by == "event":
        rows = []
        for entry in table.by_queue_id.values():
            event = capture.resolve_event(
                global_id=entry.global_id, queue_id=entry.queue_id
            )
            name = event.name if event else ""
            leaf = event.marker_path[-1] if event and event.marker_path else ""
            if needle and needle not in leaf.lower():
                continue
            rows.append(
                {
                    "queue_id": entry.queue_id,
                    "global_id": entry.global_id,
                    "name": name,
                    "pass_name": leaf,
                    "duration_ns": entry.duration_ns,
                    "duration_ms": round(entry.duration_ms, 4),
                }
            )
    else:
        buckets: dict[int, dict[str, Any]] = {}
        for entry in table.by_queue_id.values():
            pass_entry = capture.find_pass_by_event(
                global_id=entry.global_id, queue_id=entry.queue_id
            )
            if pass_entry is None:
                continue
            if needle and needle not in pass_entry["name"].lower():
                continue
            bucket = buckets.setdefault(
                pass_entry["pass_index"],
                {
                    "pass_index": pass_entry["pass_index"],
                    "name": pass_entry["name"],
                    **pass_identity(pass_entry),
                    "marker_path": pass_entry["marker_path"],
                    "measured_events": 0,
                    "duration_ns": 0,
                },
            )
            bucket["measured_events"] += 1
            bucket["duration_ns"] += entry.duration_ns
        rows = list(buckets.values())
        for row in rows:
            row["duration_ms"] = round(row["duration_ns"] / 1_000_000.0, 4)

    rows.sort(key=lambda row: -row["duration_ns"])
    total = len(rows)
    window = rows[offset : offset + limit] if limit else rows[offset:]

    total_ns = sum(row["duration_ns"] for row in rows)
    result = ToolResult.success(
        {
            "available": True,
            "group_by": group_by,
            "timing_column": table.timing_column,
            "measured_events": table.measured_count,
            "total_duration_ns": total_ns,
            "total_duration_ms": round(total_ns / 1_000_000.0, 3),
            "rows": window,
            **page_envelope(total, offset, limit, len(window)),
        }
    )
    result.add_diagnostic(
        "info",
        "Durations are per-event GPU samples; overlapping async work means the sum can "
        "exceed wall-clock frame time.",
    )
    return result
