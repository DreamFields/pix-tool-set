"""Requirement section 1: session management (open / close / info)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ..context import ToolContext, clear_capture_cache
from ..engine.capture import Capture
from ..errors import PixToolError, capture_not_found
from ..pixtool import PixTool, validate_export
from ..results import ToolResult
from ..session import SessionRecord, default_export_dir, default_session_name
from ._common import SESSION_PARAMS, object_schema, tool, with_session


@tool(
    name="session-open",
    summary=(
        "Open a .wpix capture: run the pixtool C++ export plus event list once, cache "
        "the artifacts, and register a named session that later commands reuse."
    ),
    category="session",
    parameters=object_schema(
        capture={
            "type": "string",
            "description": "Absolute path to the .wpix capture file.",
        },
        session={
            "type": "string",
            "description": "Name for this session. Defaults to the capture file stem.",
        },
        export_dir={
            "type": "string",
            "description": "Where to place the C++ export. Defaults to <capture>.pixcache/cpp.",
        },
        pixtool={
            "type": "string",
            "description": "Path to pixtool.exe when it is not auto-detected.",
        },
        force={
            "type": "boolean",
            "description": "Re-export even when a usable cache already exists.",
        },
        skip_events={
            "type": "boolean",
            "description": "Skip the event list export (faster, but event tools go dark).",
        },
        counters={
            "type": "array",
            "description": "GPU counter name patterns to include in the event list, e.g. 'D3D*'.",
        },
        with_timing={
            "type": "boolean",
            "description": (
                "Also export measured GPU durations per event (a second replay, roughly "
                "100s on a 2.5 GB capture). Upgrades pass-cost from estimate to measurement."
            ),
        },
        timeout={
            "type": "integer",
            "description": "Seconds to allow for the export. Default 10800.",
        },
        required=["capture"],
    ),
    returns="Session summary plus which artifacts were produced or reused.",
    examples=[
        "pix-tool-set session-open --capture D:/caps/frame.wpix",
        "pix-tool-set session-open --capture D:/caps/frame.wpix --session frame_a --force",
    ],
    requires_session=False,
)
def session_open(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture_path = Path(args["capture"]).expanduser().resolve()
    if not capture_path.exists():
        raise capture_not_found(str(capture_path))

    name = args.get("session") or default_session_name(capture_path)
    export_dir = (
        Path(args["export_dir"]).expanduser().resolve()
        if args.get("export_dir")
        else default_export_dir(capture_path)
    )
    event_csv = export_dir.parent / f"{capture_path.stem}.events.csv"
    force = bool(args.get("force"))
    timeout = args.get("timeout") or 10800

    diagnostics: list[dict[str, Any]] = []
    export_missing = validate_export(export_dir) if export_dir.exists() else ["<all>"]
    need_export = force or bool(export_missing)
    need_events = not bool(args.get("skip_events")) and (force or not event_csv.exists())

    pixtool: PixTool | None = None
    if need_export or need_events:
        pixtool = PixTool.locate(args.get("pixtool") or context.pixtool_path)

    started = time.time()
    if need_export:
        assert pixtool is not None
        export_dir.mkdir(parents=True, exist_ok=True)
        pixtool.export_to_cpp(
            capture_path,
            export_dir,
            force=True,
            timeout=timeout,
            log_path=export_dir.parent / "export-cpp.log",
        )
        diagnostics.append(
            {
                "level": "info",
                "message": "C++ export generated.",
                "seconds": round(time.time() - started, 1),
            }
        )
    else:
        diagnostics.append(
            {"level": "info", "message": "Reused the existing C++ export cache."}
        )

    events_ok = event_csv.exists()
    if need_events:
        assert pixtool is not None
        try:
            pixtool.save_event_list(
                capture_path,
                event_csv,
                counters=args.get("counters") or (),
                timeout=min(timeout, 3600),
                log_path=export_dir.parent / "event-list.log",
            )
            events_ok = True
        except PixToolError as exc:
            diagnostics.append(
                {
                    "level": "warning",
                    "message": f"Event list export failed: {exc.message}",
                    "code": exc.code,
                }
            )

    timing_report: dict[str, Any] | None = None
    if bool(args.get("with_timing")) and events_ok:
        from ..engine import timing as timing_mod

        timing_csv = timing_mod.timing_csv_path(event_csv.parent, capture_path.stem)
        if timing_csv.exists() and not force:
            timing_report = {"ok": True, "reused_cache": True, "path": str(timing_csv)}
            diagnostics.append(
                {"level": "info", "message": "Reused the cached GPU timing event list."}
            )
        else:
            exe = pixtool.exe if pixtool is not None else PixTool.locate(
                args.get("pixtool") or context.pixtool_path
            ).exe
            timing_report = timing_mod.export_timing_csv(
                exe, capture_path, timing_csv, timeout=min(timeout, 3600)
            )
            diagnostics.append(
                {
                    "level": "info" if timing_report.get("ok") else "warning",
                    "message": (
                        "Measured GPU timing exported."
                        if timing_report.get("ok")
                        else f"Timing export failed: {timing_report.get('error')}"
                    ),
                    "seconds": timing_report.get("elapsed_seconds"),
                }
            )

    record = SessionRecord(
        name=name,
        capture_path=str(capture_path),
        export_dir=str(export_dir),
        event_csv=str(event_csv) if events_ok else None,
        pixtool_path=str(pixtool.exe) if pixtool is not None else None,
    )
    context.store.put(record)
    clear_capture_cache()

    capture = Capture(
        capture_path=capture_path,
        export_dir=export_dir,
        event_csv=event_csv if events_ok else None,
        pixtool=pixtool,
    )
    data = {
        **record.summary(),
        "timing_export": timing_report,
        "counts": {
            "events": len(capture.events),
            "draw_calls": len(capture.draw_calls),
            "resources": len(capture.resources),
            "pipeline_states": len(capture.pipeline_states),
            "shaders": len(capture.shaders),
        },
        "capabilities": {
            "event_list": events_ok,
            "shader_disassembly": capture.disassembly_available,
        },
        "elapsed_seconds": round(time.time() - started, 1),
    }
    result = ToolResult.success(data, output_paths=[str(export_dir)], diagnostics=diagnostics)
    if not events_ok:
        result.degrade(
            "Event list is unavailable, so event-centric tools will return empty results.",
            affected=["list-actions", "search-actions", "locate-event"],
        )
    return result


@tool(
    name="session-close",
    summary="Close a session. Registry entry is removed; cached export files are kept unless --purge.",
    category="session",
    parameters=object_schema(
        session={"type": "string", "description": "Session to close. Defaults to the active one."},
        purge={
            "type": "boolean",
            "description": "Also delete the cached export directory from disk.",
        },
        all={"type": "boolean", "description": "Close every registered session."},
    ),
    returns="Which sessions were closed and whether files were deleted.",
    examples=["pix-tool-set session-close", "pix-tool-set session-close --session frame_a --purge"],
    requires_session=False,
)
def session_close(args: dict[str, Any], context: ToolContext) -> ToolResult:
    import shutil

    purge = bool(args.get("purge"))
    closed: list[str] = []
    removed_paths: list[str] = []

    targets: list[SessionRecord]
    if args.get("all"):
        targets = context.store.list()
    else:
        record = context.store.resolve(session=args.get("session"))
        targets = [record]

    for record in targets:
        if purge and record.export_dir:
            cache_root = Path(record.export_dir).parent
            if cache_root.exists() and cache_root.name.endswith(".pixcache"):
                shutil.rmtree(cache_root, ignore_errors=True)
                removed_paths.append(str(cache_root))
        context.store.remove(record.name)
        closed.append(record.name)

    clear_capture_cache()
    return ToolResult.success(
        {
            "closed": closed,
            "purged": purge,
            "removed_paths": removed_paths,
            "remaining_sessions": [r.name for r in context.store.list()],
        }
    )


@tool(
    name="session-list",
    summary="List every registered session and mark which one is active.",
    category="session",
    parameters=object_schema(),
    returns="Array of session summaries, newest first.",
    examples=["pix-tool-set session-list"],
    requires_session=False,
)
def session_list(args: dict[str, Any], context: ToolContext) -> ToolResult:
    active = context.store.active_name()
    sessions = [record.summary() for record in context.store.list()]
    for entry in sessions:
        entry["active"] = entry["session"] == active
    return ToolResult.success({"active": active, "sessions": sessions, "count": len(sessions)})


@tool(
    name="capture-info",
    summary=(
        "Basic facts about the open capture: file size, export location, artifact "
        "availability, and top-level object counts."
    ),
    category="session",
    parameters=with_session(),
    returns="Capture metadata, counts per layer, and capability flags.",
    examples=["pix-tool-set capture-info", "pix-tool-set capture-info --session frame_a"],
)
def capture_info(args: dict[str, Any], context: ToolContext) -> ToolResult:
    record = context.session(args)
    capture = context.capture(args)
    export_dir = Path(record.export_dir)

    resources_bin = export_dir / "resources.bin"
    cpp_files = sorted(export_dir.glob("*.cpp"))
    data = {
        **record.summary(),
        "export": {
            "cpp_file_count": len(cpp_files),
            "resources_bin_bytes": resources_bin.stat().st_size
            if resources_bin.exists()
            else 0,
            "command_list_files": len(list(export_dir.glob("CommandLists*.cpp"))),
            # Counted separately because `Descriptors*.cpp` does not match
            # `ModifyDescriptors_*.cpp`, and those hold the descriptor writes that are
            # actually live at a draw. Reporting only the first number made an export
            # look fully accounted for while half the descriptor data went unmentioned.
            "descriptor_files": len(list(export_dir.glob("Descriptors*.cpp"))),
            "modify_descriptor_files": len(list(export_dir.glob("ModifyDescriptors*.cpp"))),
        },
        "counts": {
            "events": len(capture.events),
            "draw_calls": len(capture.draw_calls),
            "passes": len(capture.passes),
            "resources": len(capture.resources),
            "descriptors": len(capture.views),
            "pipeline_states": len(capture.pipeline_states),
            "root_signatures": len(capture.root_signatures),
            "shaders": len(capture.shaders),
        },
        # Says whether resource_usage can be read as fact. An empty read/write list means
        # "not recorded" rather than "untouched" when coverage is short of 100%.
        "descriptor_coverage": capture.descriptor_coverage,
        "capabilities": {
            "event_list": bool(capture.events),
            "shader_disassembly": capture.disassembly_available,
            "disassembly_note": capture.disassembly_unavailable_reason,
            "pixtool_available": record.pixtool_path is not None,
        },
    }
    result = ToolResult.success(data)
    if not capture.events:
        result.degrade("No event list in this session; run `session-open --force` to add one.")
    coverage = data["descriptor_coverage"]
    if coverage["descriptor_tables_bound"] and not coverage["usage_is_complete"]:
        result.degrade(
            f"{coverage['tables_empty']} of {coverage['descriptor_tables_bound']} descriptor "
            "tables resolved to no views, so resource_usage is incomplete for the resources "
            "they address: an empty read/write list there means unrecorded, not untouched."
        )
    return result
