"""Pixel-level debugging tools: value history and downstream impact tracing.

These tools answer the two questions that make shader debugging tractable:

  * **pixel-value-history** (P0): "What happened to this pixel?" Given an (x, y)
    coordinate, it walks every resource the frame touches that covers that pixel,
    and returns the draw-call-ordered history of which pass wrote what value.
    This is the PIX Debug-panel "pixel history" view, made scriptable.

  * **trace-downstream** (capability B): "If I change this pass, what else breaks?"
    Given a pass or draw, it finds the output resources and walks the resource-usage
    graph forward to find every downstream draw and pass that reads them —
    transitively, so a chain like LightingCS → RWLighting → DeferredShadingPS →
    FinalRT is one call, not three.

Both tools are pure analysis: they read from the PIX capture's metadata and do
not need a replay. Pixel values at each draw require a frame-replay-dump, but
the dependency graph and draw ordering are available from the capture alone.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from ..context import ToolContext
from ..engine import pixelprobe
from ..errors import invalid_argument, not_found
from ..results import ToolResult
from ._common import (
    DRAW_SELECTOR,
    PASS_SELECTOR,
    resolve_draw,
    resolve_pass,
    tool,
    with_session,
)
from .replay_render_tools import _export_root, _configure_and_build


# ======================================================================
# pixel-value-history (P0)
# ======================================================================

_PIXEL_HISTORY_NOTE = (
    "Given a pixel coordinate (x, y), walks every render target, UAV, and depth "
    "buffer the frame touches, and returns the draw-call-ordered history of which "
    "pass wrote to or read from that pixel. This is the PIX Debug-panel pixel "
    "history view, made scriptable. The history is built from the capture's "
    "resource-usage graph, so no replay is needed — but to see actual pixel "
    "values (not just which draw touched the pixel), pass --dump-dir pointing at "
    "a frame-replay-dump output directory."
)


@tool(
    name="pixel-value-history",
    summary=(
        "Trace a single pixel (x, y) through every resource and draw in the frame, "
        "returning the ordered history of what wrote to it."
    ),
    category="pixels",
    parameters=with_session(
        x={"type": "integer", "description": "Pixel X coordinate."},
        y={"type": "integer", "description": "Pixel Y coordinate."},
        dump_dir={
            "type": "string",
            "description": (
                "Directory of a frame-replay-dump output. When provided, actual pixel "
                "values are read from the dump files. Without it, the history shows "
                "which draws touched the pixel but not the values."
            ),
        },
        max_entries={
            "type": "integer",
            "description": "Cap the number of history entries. Default 100.",
        },
        resource_types={
            "type": "array",
            "items": {"type": "string", "enum": ["uav", "rt", "depth", "all"]},
            "description": "Which resource types to include. Default ['all'].",
        },
    ),
    returns="Ordered list of history entries: resource, draw, pass, read/write, and pixel value (if dump available).",
    examples=[
        "pix-tool-set pixel-value-history --x 640 --y 360",
        "pix-tool-set pixel-value-history --x 100 --y 200 --dump-dir G:\\dumps",
    ],
    notes=_PIXEL_HISTORY_NOTE,
)
def pixel_value_history(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)

    x = int(args.get("x", 0))
    y = int(args.get("y", 0))
    max_entries = int(args.get("max_entries") or 100)
    resource_types = args.get("resource_types") or ["all"]
    if "all" in resource_types:
        resource_types = ["uav", "rt", "depth"]

    if x < 0 or y < 0:
        raise invalid_argument("x/y", "pixel coordinates must be non-negative")

    # Build the resource usage map: resource_id -> {read_draws, write_draws, ...}
    usage = capture.resource_usage

    # Collect history entries: each entry is a (draw_index, resource_id, action, ...)
    history: list[dict[str, Any]] = []

    for rid, info in usage.items():
        resource = capture.resource(rid)
        if resource is None:
            continue

        # Check if this resource covers the pixel coordinate.
        if resource.width == 0 or resource.height == 0:
            continue
        if x >= resource.width or y >= resource.height:
            continue

        # Determine resource type.
        is_rt = bool(info.get("render_target_draws"))
        is_depth = bool(info.get("depth_draws"))
        is_uav = bool(info.get("write_draws") and not is_rt and not is_depth)

        if is_rt:
            rtype = "rt"
        elif is_depth:
            rtype = "depth"
        else:
            rtype = "uav"

        if rtype not in resource_types:
            continue

        # Collect draw indices that touch this resource.
        read_draws = set(info.get("read_draws", []))
        write_draws = set(info.get("write_draws", []))
        all_draws = sorted(read_draws | write_draws)

        for draw_idx in all_draws:
            draw = capture.draw_call(draw_idx)
            if draw is None:
                continue

            action = "write" if draw_idx in write_draws else "read"

            entry: dict[str, Any] = {
                "draw_index": draw_idx,
                "global_id": draw.global_id,
                "api": draw.api,
                "pass_name": draw.pass_name,
                "resource_id": rid,
                "resource_type": rtype,
                "resource_format": resource.format,
                "resource_dimensions": f"{resource.width}x{resource.height}",
                "action": action,
                "pixel": {"x": x, "y": y},
            }
            history.append(entry)

    # Sort by draw index to get temporal order.
    history.sort(key=lambda e: e["draw_index"])

    # Cap the number of entries.
    capped = len(history) > max_entries
    history = history[:max_entries]

    # If a dump directory is provided, read actual pixel values.
    dump_dir = args.get("dump_dir")
    if dump_dir:
        from pathlib import Path
        from ..engine import uavprobe

        dump_path = Path(dump_dir)
        for entry in history:
            rid = entry["resource_id"]
            dump_bin = dump_path / f"framedump_*_{rid}.bin"
            matches = sorted(dump_path.glob(f"framedump_*_{rid}.bin"))
            if matches:
                try:
                    dump = uavprobe.read_sidecar(matches[0])
                    blob = matches[0].read_bytes()
                    packed = uavprobe.depad(blob, dump)
                    image = uavprobe.as_image(packed, dump)
                    if 0 <= x < image.width and 0 <= y < image.height:
                        entry["pixel"]["value"] = image.pixel(x, y)
                    else:
                        entry["pixel"]["value"] = None
                        entry["pixel"]["note"] = "outside dump dimensions"
                except Exception as exc:
                    entry["pixel"]["value"] = None
                    entry["pixel"]["decode_error"] = f"{type(exc).__name__}: {exc}"
            else:
                entry["pixel"]["value"] = None
                entry["pixel"]["note"] = "no dump file found for this resource"

    data: dict[str, Any] = {
        "pixel": {"x": x, "y": y},
        "history": history,
        "entry_count": len(history),
        "capped": capped,
        "dump_dir": dump_dir,
        "resource_types": resource_types,
    }

    result = ToolResult.success(data)
    if not history:
        result.degrade(
            "No resources touch pixel ({}, {}) in this frame. Check that the "
            "coordinates are within the frame's render target dimensions.".format(x, y),
            reason="no matching resources",
        )
    elif not dump_dir:
        result.add_diagnostic(
            "info",
            "History shows which draws touched the pixel, but not the actual values. "
            "Pass --dump-dir (from a frame-replay-dump run) to see pixel values.",
        )
    if capped:
        result.add_diagnostic(
            "warning",
            f"History was capped at {max_entries} entries. Pass --max-entries to see more.",
        )
    return result


# ======================================================================
# trace-downstream (capability B)
# ======================================================================

_TRACE_NOTE = (
    "Given a pass or draw, finds every resource it writes to, then walks forward "
    "through the frame's resource-usage graph to find every downstream draw and pass "
    "that reads those resources — transitively, so the full impact chain is one call. "
    "This is the 'what breaks if I change this?' view: it tells you which passes to "
    "check after modifying a shader, so you don't have to guess or re-run the whole "
    "frame to discover a dependency. The graph is built from the capture's descriptor "
    "and binding metadata, so no replay is needed."
)


@tool(
    name="trace-downstream",
    summary=(
        "Given a pass or draw, find every downstream pass that transitively reads "
        "its output resources. The full impact chain of a shader edit in one call."
    ),
    category="pixels",
    parameters=with_session(
        PASS_SELECTOR,
        DRAW_SELECTOR,
        max_depth={
            "type": "integer",
            "description": (
                "Maximum graph traversal depth. Default 0 (unlimited). Use a small "
                "value to limit the blast radius."
            ),
        },
        include_resources={
            "type": "boolean",
            "description": "Include the resource IDs in the output. Default true.",
        },
    ),
    returns="List of downstream passes and draws that transitively depend on the selected pass's output.",
    examples=[
        "pix-tool-set trace-downstream --queue-id 18461",
        "pix-tool-set trace-downstream --draw-index 2461",
        "pix-tool-set trace-downstream --pass-name RayTracingBuildLightGrid --max-depth 3",
    ],
    notes=_TRACE_NOTE,
)
def trace_downstream(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)

    # Resolve the source pass/draw.
    draw = None
    try:
        draw = resolve_draw(capture, args, what="dispatch")
    except Exception:
        pass

    if draw is None:
        entry = resolve_pass(capture, args)
        draw = capture.draw_call(entry["first_draw_index"])
        if draw is None:
            raise not_found("draw", args.get("draw_index") or args.get("queue_id") or args.get("pass_name"))

    source_pass = draw.pass_name or f"draw_{draw.index}"
    max_depth = int(args.get("max_depth") or 0)
    include_resources = args.get("include_resources")
    if include_resources is None:
        include_resources = True

    # Build the resource-usage map.
    usage = capture.resource_usage

    # Find the output resources of the source draw: render targets, depth, UAVs.
    output_resources: set[int] = set()
    for rid in draw.render_target_resource_ids:
        output_resources.add(rid)
    if draw.depth_stencil_resource_id is not None:
        output_resources.add(draw.depth_stencil_resource_id)
    for view in draw.views():
        if view.kind and view.kind.value == "UAV" and view.resource_id is not None:
            output_resources.add(view.resource_id)

    # Walk forward: for each output resource, find draws that read it.
    # Then for those draws' output resources, find their readers, etc.
    visited_passes: set[str] = set()
    visited_draws: set[int] = set()
    visited_resources: set[int] = set()
    impact_chain: list[dict[str, Any]] = []

    # BFS: level 0 = source, level 1 = direct readers, etc.
    current_resources = set(output_resources)
    visited_resources.update(current_resources)
    depth = 0

    while current_resources:
        if max_depth > 0 and depth >= max_depth:
            break

        next_resources: set[int] = set()

        for rid in current_resources:
            info = usage.get(rid, {})
            read_draws = info.get("read_draws", [])

            for draw_idx in read_draws:
                if draw_idx in visited_draws:
                    continue
                visited_draws.add(draw_idx)

                reader = capture.draw_call(draw_idx)
                if reader is None:
                    continue

                reader_pass = reader.pass_name or f"draw_{draw_idx}"
                is_new_pass = reader_pass not in visited_passes
                visited_passes.add(reader_pass)

                # Find what this reader writes to (for transitive traversal).
                reader_outputs: set[int] = set()
                for out_rid in reader.render_target_resource_ids:
                    reader_outputs.add(out_rid)
                if reader.depth_stencil_resource_id is not None:
                    reader_outputs.add(reader.depth_stencil_resource_id)
                for view in reader.views():
                    if view.kind and view.kind.value == "UAV" and view.resource_id is not None:
                        reader_outputs.add(view.resource_id)

                # New resources to explore.
                for out_rid in reader_outputs:
                    if out_rid not in visited_resources:
                        visited_resources.add(out_rid)
                        next_resources.add(out_rid)

                entry: dict[str, Any] = {
                    "depth": depth + 1,
                    "draw_index": draw_idx,
                    "global_id": reader.global_id,
                    "api": reader.api,
                    "pass_name": reader_pass,
                    "pass_label": reader_pass if is_new_pass else None,
                    "reads_resource": rid,
                    "writes_resources": sorted(reader_outputs) if reader_outputs else [],
                }
                impact_chain.append(entry)

        current_resources = next_resources
        depth += 1

    # Build the pass-level summary.
    pass_order: list[str] = []
    seen_passes: set[str] = set()
    for entry in impact_chain:
        pname = entry["pass_name"]
        if pname not in seen_passes:
            seen_passes.add(pname)
            pass_order.append(pname)

    data: dict[str, Any] = {
        "source": {
            "draw_index": draw.index,
            "global_id": draw.global_id,
            "pass_name": source_pass,
            "output_resources": sorted(output_resources) if include_resources else [],
        },
        "downstream_draws": impact_chain,
        "downstream_draw_count": len(impact_chain),
        "downstream_passes": pass_order,
        "downstream_pass_count": len(pass_order),
        "max_depth_reached": depth,
        "truncated": max_depth > 0 and depth >= max_depth,
        "all_affected_resources": sorted(visited_resources) if include_resources else [],
    }

    result = ToolResult.success(data)
    if not impact_chain:
        result.add_diagnostic(
            "info",
            "No downstream draws read the output resources of this pass. The pass "
            "may be a terminal output (e.g., final backbuffer) or its outputs may not "
            "be consumed in this frame.",
        )
    if data.get("truncated"):
        result.add_diagnostic(
            "warning",
            f"Traversal was capped at depth {max_depth}. Pass --max-depth 0 for the "
            "full transitive closure.",
        )
    return result


# ======================================================================
# pixel-trace (P1 + P2 --auto)
# ======================================================================

_PIXEL_TRACE_NOTE = (
    "Injects a pixel-level probe into the exported C++ replay project that reads the "
    "pixel at (x, y) from the currently bound render target after each recorded draw, "
    "and writes the values to a JSON trace file. This is the PIX Debug-panel pixel "
    "history view, made scriptable: instead of seeing only the final value, you see "
    "how the pixel evolved through every draw that touched it. Requires CMake and a "
    "Visual Studio toolchain; the probe is removed afterwards unless --keep-probe is "
    "given. Use --auto to trace only the draws that touch resources at (x, y), "
    "discovered from the capture's resource-usage graph — this skips draws that "
    "cannot affect the pixel and makes the trace faster and easier to read."
)


@tool(
    name="pixel-trace",
    summary=(
        "Replay the frame with a pixel-level probe and return the per-draw value "
        "history of a single pixel (x, y). The PIX Debug-panel pixel history, scripted."
    ),
    category="pixels",
    parameters=with_session(
        x={"type": "integer", "description": "Pixel X coordinate to trace."},
        y={"type": "integer", "description": "Pixel Y coordinate to trace."},
        output={
            "type": "string",
            "description": "Directory for the trace file. Defaults to the activity log directory.",
        },
        auto={
            "type": "boolean",
            "description": (
                "Auto-detect which draws to trace based on resource usage at (x, y). "
                "Only draws that touch resources covering the pixel are recorded, "
                "skipping draws that cannot affect it. Default false."
            ),
        },
        settle_seconds={
            "type": "integer",
            "description": "Seconds to let the replay run. Default 300.",
        },
        build_timeout={
            "type": "integer",
            "description": "Seconds allowed for configure and for build. Default 1800.",
        },
        generator={
            "type": "string",
            "description": "CMake generator. Default 'Visual Studio 18 2026'.",
        },
        force_reconfigure={
            "type": "boolean",
            "description": "Wipe the build directory first and reconfigure from scratch.",
        },
        skip_build={
            "type": "boolean",
            "description": "Run the existing executable without rebuilding.",
        },
        no_vendored_winpixruntime={
            "type": "boolean",
            "description": "Download WinPixEventRuntime from nuget instead of using the vendored copy.",
        },
        keep_probe={
            "type": "boolean",
            "description": "Leave the pixel probe in the export. Default false.",
        },
        max_draws={
            "type": "integer",
            "description": "Cap the number of draws to trace. Default 10000.",
        },
    ),
    returns="Ordered per-draw pixel values: draw index, resource ID, RGBA. Plus the trace file path.",
    examples=[
        "pix-tool-set pixel-trace --x 640 --y 360",
        "pix-tool-set pixel-trace --x 100 --y 200 --auto",
        "pix-tool-set pixel-trace --x 320 --y 240 --skip-build --keep-probe",
    ],
    notes=_PIXEL_TRACE_NOTE,
)
def pixel_trace(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    root = _export_root(context, args)

    x = int(args.get("x", 0))
    y = int(args.get("y", 0))
    auto = bool(args.get("auto"))
    max_draws = int(args.get("max_draws") or 10000)

    if x < 0 or y < 0:
        raise invalid_argument("x/y", "pixel coordinates must be non-negative")

    # --- --auto: find which draws touch resources at (x, y)
    auto_draws: list[int] | None = None
    if auto:
        usage = capture.resource_usage
        relevant_draws: set[int] = set()
        for rid, info in usage.items():
            resource = capture.resource(rid)
            if resource is None:
                continue
            if resource.width == 0 or resource.height == 0:
                continue
            if x >= resource.width or y >= resource.height:
                continue
            # This resource covers (x, y); add all draws that write to it.
            for draw_idx in info.get("write_draws", []):
                relevant_draws.add(draw_idx)
        auto_draws = sorted(relevant_draws)

    data: dict[str, Any] = {
        "export_dir": str(root),
        "pixel": {"x": x, "y": y},
        "auto": auto,
        "auto_draws": auto_draws if auto else None,
    }
    diagnostics: list[tuple[str, str]] = []

    # --- Install the pixel probe.
    injection = pixelprobe.install(root)
    data["probe_injection"] = injection
    try:
        skip_build = bool(args.get("skip_build"))
        downgraded = skip_build and bool(injection.get("rebuild_needed"))
        if downgraded:
            skip_build = False
            diagnostics.append((
                "warning",
                "--skip-build was ignored: the pixel probe had to be injected just now.",
            ))
        if skip_build:
            executables = sorted(
                (root / "build" / "Release").glob("*.exe"), key=lambda p: -p.stat().st_size
            )
            if not executables:
                raise not_found("built executable", str(root / "build" / "Release"))
            data["build"] = {"skipped": True, "executable": str(executables[0])}
            exe = executables[0]
        else:
            timeout = int(args.get("build_timeout") or 1800)
            generator = str(args.get("generator") or "Visual Studio 18 2026")
            steps = _configure_and_build(
                root, generator, timeout, bool(args.get("force_reconfigure")), args
            )
            data["build"] = steps
            exe = Path(steps["executable"])

        # --- Run the replay with the pixel probe.
        settle = int(args.get("settle_seconds") or 300)
        output_dir = Path(str(args["output"])) if args.get("output") else Path("activity_renders")
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        trace_path = output_dir / f"pixeltrace_{stamp}_{x}_{y}.json"

        environment = dict(os.environ)
        environment[pixelprobe.ENV_X] = str(x)
        environment[pixelprobe.ENV_Y] = str(y)
        environment[pixelprobe.ENV_OUT] = str(trace_path)
        environment[pixelprobe.ENV_MAX_DRAWS] = str(max_draws)

        process = subprocess.Popen([str(exe)], cwd=str(root), env=environment)
        run_info: dict[str, Any] = {
            "pid": process.pid,
            "working_directory": str(root),
        }
        started = time.time()
        try:
            # Wait for the trace file to appear and stabilize.
            deadline = started + settle
            while time.time() < deadline:
                if trace_path.exists():
                    # Wait a bit more for the file to be fully written.
                    time.sleep(3.0)
                    break
                time.sleep(2.0)

            run_info["seconds"] = round(time.time() - started, 1)
            run_info["trace_file"] = str(trace_path) if trace_path.exists() else None
        finally:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
            run_info["stopped"] = True

        data["run"] = run_info

        # --- Read the trace.
        if trace_path.exists():
            trace_data = pixelprobe.read_trace(trace_path)
            data["trace"] = trace_data.get("trace", [])
            data["entry_count"] = trace_data.get("entry_count", 0)

            # Enrich with pass names from the capture.
            for entry in data["trace"]:
                draw_idx = entry.get("draw")
                if draw_idx is not None:
                    draw = capture.draw_call(draw_idx)
                    if draw is not None:
                        entry["pass_name"] = draw.pass_name
                        entry["global_id"] = draw.global_id
                        entry["api"] = draw.api

            # If --auto, filter to only the relevant draws.
            if auto and auto_draws is not None:
                auto_set = set(auto_draws)
                data["trace"] = [
                    e for e in data["trace"] if e.get("draw") in auto_set
                ]
                data["entry_count"] = len(data["trace"])
                data["auto_filtered"] = True
        else:
            data["trace"] = []
            data["entry_count"] = 0
            diagnostics.append((
                "warning",
                "The pixel trace file was not produced within the settle window. "
                "The probe may not have run, or the replay crashed before the flush. "
                "Raise --settle-seconds and try again.",
            ))

    finally:
        if bool(args.get("keep_probe")):
            data["probe_cleanup"] = {"action": "left installed (--keep-probe)"}
        else:
            data["probe_cleanup"] = pixelprobe.restore(root)

    result = ToolResult.success(data)
    for level, message in diagnostics:
        result.add_diagnostic(level, message)

    if data.get("entry_count", 0) == 0:
        result.degrade(
            "No pixel values were recorded. The probe may not have found a render "
            "target at ({}, {}), or the replay did not complete.".format(x, y),
            reason="trace file is empty or missing",
        )
    else:
        result.add_diagnostic(
            "info",
            f"Recorded {data['entry_count']} pixel values at ({x}, {y}) across the "
            "frame. Each entry shows the draw index, resource ID, and RGBA value.",
        )

    return result
