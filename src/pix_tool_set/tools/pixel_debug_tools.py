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
        # trace-downstream answers "who is affected"; the contract of each edge
        # (barriers, subresources, formats) is trace-resource-lineage's job, so
        # point there rather than re-deriving it here.
        "next_action": {
            "tool": "trace-resource-lineage",
            "resource_id": sorted(output_resources)[0] if output_resources else None,
            "reason": (
                "This tool lists the impact surface. For one output resource, "
                "trace-resource-lineage asserts the production-consumption contract "
                "(missing transitions/UAV barriers, subresource and format mismatches) "
                "and hands back ready-to-run follow-up commands."
            ),
        },
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
# pixel-history-replay (P6): the PIX Pixel History panel, reproduced
# ======================================================================
#
# A separate tool rather than an option on pixel-value-history, because the two
# answer different questions with different costs and different trust levels.
# pixel-value-history reads the capture's metadata and says *which* events touch a
# texel -- instant, and true of any capture. This one records GPU copies into a
# rebuilt replay to say *what value* each event left behind -- minutes to build, and
# only meaningful for events the export actually contains. Folding them together
# would make a cheap metadata query silently able to trigger a half-hour compile.

_HISTORY_NOTE = (
    "Reproduces the PIX Debug panel's Pixel History for one texel of one resource: "
    "for every candidate event, both the Previous Value (immediately before it) and "
    "the New Value (immediately after it). It works by injecting 1x1 readback copies "
    "into the exported replay project around each event and rebuilding it, which is "
    "the only way to observe a mid-frame value: exporting the resource at event N "
    "yields the state *before* N ran, so a single sample can never attribute a write "
    "to the event that made it. Adjacent events are cross-checked -- one event's New "
    "Value must equal the next one's Previous Value -- and any mismatch is reported "
    "rather than smoothed over, because it means something outside the candidate set "
    "wrote the texel. Values a probe could not obtain are reported as null with the "
    "reason; they are never synthesised. The frame's initial contents (what PIX calls "
    "'Recreation #1') are read statically from the capture's recorded upload and need "
    "no replay, so --no-replay still answers that row plus every clear whose colour "
    "the export records. Building the replay is slow the first time; --skip-build "
    "reuses an executable only when it was built for this exact pixel and event set."
)


def _initial_contents_value(capture, resource_id: int, x: int, y: int) -> dict[str, Any]:
    """The texel's value at frame start, from the recorded upload -- no replay.

    This is PIX's "Recreation #1" row. It is a static read on purpose: the bytes are
    in ``resources.bin`` and the footprint states the row pitch, so replaying to
    obtain them would be a needless half hour. ``row_pitch`` must be read from the
    footprint rather than computed as ``width * stride`` -- for the reference target
    those are 6144 and 6128, and using the latter skews every row after the first.
    """
    from ..engine import footprint as footprints
    from ..engine import pixelprobe as probe
    from ..errors import PixToolError

    out: dict[str, Any] = {"source": "recorded initial upload (resources.bin)"}
    try:
        blob = capture.read_resource_bytes(resource_id)
    except PixToolError as exc:
        out["value"] = None
        out["reason"] = f"no recorded bytes for this resource: {exc.message}"
        return out

    entries = capture.resource_footprints(resource_id)
    if not entries:
        out["value"] = None
        out["reason"] = "the export declares no subresource footprint for this resource"
        return out

    plane = entries[0]
    stride = footprints.format_stride(plane.format)
    if stride is None:
        out["value"] = None
        out["reason"] = f"format {plane.format} has no known pixel stride"
        return out
    if x >= plane.width or y >= plane.height:
        out["value"] = None
        out["reason"] = (
            f"({x}, {y}) is outside the uploaded {plane.width}x{plane.height} plane"
        )
        return out

    offset = plane.slice_offset(0) + y * plane.row_pitch + x * stride
    if offset + stride > len(blob):
        out["value"] = None
        out["reason"] = (
            f"the recorded bytes stop at {len(blob)}, before this texel at {offset}"
        )
        return out

    dxgi = _dxgi_from_name(plane.format)
    if dxgi is None:
        out["value"] = None
        out["reason"] = f"{plane.format} has no DXGI number in this toolkit's table"
        return out

    decoded = probe.decode_pixel(blob[offset : offset + stride], dxgi)
    out["value"] = decoded.to_dict() if decoded else None
    out["byte_offset"] = offset
    out["row_pitch"] = plane.row_pitch
    if decoded is None:
        out["reason"] = f"DXGI format {dxgi} has no decoder; no value is guessed"
    return out


def _dxgi_from_name(name: str) -> int | None:
    """``DXGI_FORMAT_R10G10B10A2_UNORM`` -> 24, using engine/dds.py's own table."""
    from ..engine import dds

    wanted = (name or "").upper().removeprefix("DXGI_FORMAT_")
    for number, spec in dds.DXGI_FORMATS.items():
        if spec[0].upper() == wanted:
            return number
    return None


def _depth_evidence(draw) -> dict[str, Any]:
    """The pipeline facts that a depth/stencil verdict may rest on.

    Collected as evidence rather than folded into a boolean so the payload can show
    *why* a conclusion was or was not drawn. ``depth_write`` is reported but is not
    part of the test: a depth-tested draw with writes disabled still gets rejected by
    the test, which is exactly the reference case (GREATER_EQUAL, write off).
    """
    pso = draw.pipeline_state if draw is not None else None
    return {
        "depth_stencil_bound": bool(
            draw is not None and draw.depth_stencil_resource_id is not None
        ),
        "depth_stencil_resource_id": (
            draw.depth_stencil_resource_id if draw is not None else None
        ),
        "depth_test_enabled": bool(pso.depth_enabled) if pso else None,
        "depth_write_enabled": bool(pso.depth_write) if pso else None,
        "depth_func": pso.depth_func if pso else None,
        "pso_id": pso.api_id if pso else None,
    }


def _gui_global_id(capture, draw, global_id: int) -> dict[str, Any]:
    """The id the PIX GUI shows for this event, which is not always ours.

    An ExecuteIndirect is one event in the C++ export but PIX numbers the sub-action
    it expands into, at parent+1. A caller comparing our output against a GUI
    screenshot needs the id they can actually see, so both are reported with the
    offset made explicit rather than left to be discovered.
    """
    entry: dict[str, Any] = {"global_id": global_id, "gui_global_id": global_id, "gui_id_offset": 0}
    if draw is not None and getattr(draw, "api", "") == "ExecuteIndirect":
        entry["gui_global_id"] = global_id + 1
        entry["gui_id_offset"] = 1
        entry["gui_id_note"] = (
            "PIX numbers the sub-action this ExecuteIndirect expands into, so the GUI "
            "shows parent+1 while the export records the ExecuteIndirect itself."
        )
    return entry


def _candidate_events(
    capture, resource_id: int, x: int, y: int, *, include_resource_events: bool
) -> tuple[list[dict[str, Any]], list[str]]:
    """Every event that could change this texel, in frame order.

    Draws come from the capture's resource-usage graph. Clears and discards come from
    the resource-event parse, and are included because PIX's history lists them and a
    history that omits the clear cannot explain how a written texel became zero. They
    are opt-out rather than opt-in here (unlike the resource-history tool, where they
    are opt-in) because for a *pixel* history they are not optional detail -- they are
    the events that overwrite it wholesale.
    """
    notes: list[str] = []
    candidates: dict[int, dict[str, Any]] = {}

    usage = capture.resource_usage.get(resource_id, {})
    for draw_index in sorted(set(usage.get("write_draws", []))):
        draw = capture.draw_call(draw_index)
        if draw is None or draw.global_id is None:
            continue
        candidates[draw.global_id] = {
            "global_id": draw.global_id,
            "draw_index": draw.index,
            "api": draw.api,
            "event_type": "draw",
            "pass_name": draw.pass_name,
            "rtv_slot": (
                draw.render_target_resource_ids.index(resource_id)
                if resource_id in draw.render_target_resource_ids
                else None
            ),
            "_draw": draw,
        }

    if include_resource_events:
        try:
            from ..engine import resourceevents

            parsed = resourceevents.parse_resource_events(capture.export_dir)
            for event in resourceevents.events_for_resource(parsed, resource_id):
                if event.event_type not in ("clear", "discard"):
                    continue
                if event.global_id is None:
                    continue
                candidates.setdefault(
                    event.global_id,
                    {
                        "global_id": event.global_id,
                        "draw_index": None,
                        "api": event.api,
                        "event_type": event.event_type,
                        "pass_name": (
                            event.marker_path[-1] if event.marker_path else None
                        ),
                        "clear_value": event.clear_value,
                        "_draw": None,
                    },
                )
        except Exception as exc:  # noqa: BLE001
            notes.append(
                f"clears and discards could not be enumerated "
                f"({type(exc).__name__}: {exc}), so the history covers draws only"
            )

    ordered = [candidates[gid] for gid in sorted(candidates)]
    return ordered, notes


@tool(
    name="pixel-history-replay",
    summary=(
        "The PIX Pixel History panel, reproduced: Previous and New value of one texel "
        "at every event that writes it, measured on the GPU during a replay."
    ),
    category="pixels",
    parameters=with_session(
        x={"type": "integer", "description": "Pixel X coordinate."},
        y={"type": "integer", "description": "Pixel Y coordinate."},
        resource_id={
            "type": "integer",
            "description": (
                "Resource whose texel is traced. Required: a pixel belongs to a "
                "surface, and different render targets hold different values at the "
                "same coordinate."
            ),
        },
        subresource={
            "type": "integer",
            "description": "Subresource (mip/slice) index. Default 0.",
        },
        no_replay={
            "type": "boolean",
            "description": (
                "Skip the build and the GPU run. Returns the candidate event list, the "
                "statically known initial contents and recorded clear colours, with "
                "every measured value null. Useful to see what would be sampled."
            ),
        },
        include_resource_events={
            "type": "boolean",
            "description": (
                "Include clears and discards alongside draws. Default true: a pixel "
                "history without the clear cannot explain how a written texel became "
                "zero."
            ),
        },
        max_events={
            "type": "integer",
            "description": (
                "Cap the candidate events. Each costs two GPU copies. Default 64."
            ),
        },
        settle_seconds={
            "type": "integer",
            "description": "Seconds to let the replay run before giving up. Default 600.",
        },
        build_timeout={
            "type": "integer",
            "description": "Seconds allowed for configure and for build. Default 3600.",
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
            "description": (
                "Reuse the existing executable. Honoured only when it was built for "
                "this exact pixel and event set; otherwise it is refused, because a "
                "binary built for another pixel returns confident wrong values."
            ),
        },
        no_vendored_winpixruntime={
            "type": "boolean",
            "description": "Download WinPixEventRuntime from nuget instead of the vendored copy.",
        },
        keep_probe={
            "type": "boolean",
            "description": "Leave the probe installed in the export. Default false.",
        },
        output={
            "type": "string",
            "description": "Directory for the trace file. Defaults to activity_renders.",
        },
    ),
    returns=(
        "Ordered history rows with previous_value/new_value (normalised and raw hex), "
        "a verdict per event, gui_global_id, and the adjacent-pair consistency report."
    ),
    examples=[
        "pix-tool-set pixel-history-replay --resource-id 756 --x 810 --y 284",
        "pix-tool-set pixel-history-replay --resource-id 756 --x 810 --y 284 --no-replay",
        "pix-tool-set pixel-history-replay --resource-id 756 --x 810 --y 284 --skip-build",
    ],
    notes=_HISTORY_NOTE,
)
def pixel_history_replay(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)

    x = int(args.get("x", -1))
    y = int(args.get("y", -1))
    if x < 0 or y < 0:
        raise invalid_argument("x/y", "pixel coordinates must be non-negative")
    if args.get("resource_id") is None:
        raise invalid_argument(
            "resource_id",
            "name the resource to trace; the same coordinate holds different values "
            "in different render targets",
        )
    resource_id = int(args["resource_id"])
    resource = capture.resource(resource_id)
    if resource is None:
        raise not_found("resource", resource_id, "Run list-resources to see valid ids.")
    if resource.width and (x >= resource.width or y >= resource.height):
        raise invalid_argument(
            "x/y",
            f"({x}, {y}) is outside resource {resource_id}, which is "
            f"{resource.width}x{resource.height}",
        )

    subresource = int(args.get("subresource") or 0)
    max_events = int(args.get("max_events") or 64)
    include_resource_events = args.get("include_resource_events")
    if include_resource_events is None:
        include_resource_events = True

    diagnostics: list[tuple[str, str]] = []
    candidates, notes = _candidate_events(
        capture, resource_id, x, y, include_resource_events=bool(include_resource_events)
    )
    for note in notes:
        diagnostics.append(("warning", note))

    capped = len(candidates) > max_events
    if capped:
        candidates = candidates[:max_events]

    data: dict[str, Any] = {
        "pixel": {"x": x, "y": y},
        "resource": {
            "resource_id": resource_id,
            "format": resource.format,
            "dimensions": f"{resource.width}x{resource.height}",
            "subresource": subresource,
        },
        "candidate_event_count": len(candidates),
        "candidates_capped": capped,
        "initial_contents": _initial_contents_value(capture, resource_id, x, y),
    }

    # --- rows are built first without measurements, so --no-replay and a failed
    # --- replay produce the same shape as a successful one. A caller should never
    # --- have to branch on whether the GPU ran to find the events.
    rows: list[dict[str, Any]] = []
    for entry in candidates:
        draw = entry.get("_draw")
        row: dict[str, Any] = {
            "event_type": entry["event_type"],
            "api": entry["api"],
            "pass_name": entry.get("pass_name"),
            "draw_index": entry.get("draw_index"),
            "previous_value": None,
            "new_value": None,
            "verdict": pixelprobe.VERDICT_UNKNOWN,
            "verdict_is_inferred": False,
            "reason": "not measured: the replay was not run",
        }
        row.update(_gui_global_id(capture, draw, entry["global_id"]))
        if entry.get("rtv_slot") is not None:
            row["rtv_slot"] = entry["rtv_slot"]
        if entry.get("clear_value") is not None:
            row["recorded_clear_value"] = entry["clear_value"]
        if entry["event_type"] == "draw":
            row["depth_evidence"] = _depth_evidence(draw)
        rows.append(row)

    # The PIX row for frame-start contents is not an API call, so it is presented as
    # a synthetic row flagged as such rather than mixed in with real events.
    initial_row = {
        "global_id": 0,
        "gui_global_id": 0,
        "gui_id_offset": 0,
        "event_type": "initial_contents",
        "api": "Recreation",
        "is_synthetic_event": True,
        "previous_value": None,
        "new_value": data["initial_contents"].get("value"),
        "verdict": (
            pixelprobe.VERDICT_WROTE
            if data["initial_contents"].get("value")
            else pixelprobe.VERDICT_UNKNOWN
        ),
        "verdict_is_inferred": False,
        "reason": data["initial_contents"].get("reason", ""),
        "note": (
            "PIX shows this as 'Recreation #N': the resource's contents at frame "
            "start, not an API call. Read statically from the recorded upload, so it "
            "needs no replay. Its Previous Value is undefined by construction -- "
            "nothing precedes frame start -- which is why PIX prints zeros there."
        ),
    }
    data["history"] = [initial_row] + rows

    if args.get("no_replay"):
        data["replay"] = {"ran": False, "reason": "--no-replay was given"}
        result = ToolResult.partial(data)
        for level, message in diagnostics:
            result.add_diagnostic(level, message)
        result.add_diagnostic(
            "info",
            f"{len(candidates)} event(s) write resource {resource_id}; with --no-replay "
            "only the statically known values are filled in. Drop --no-replay to "
            "measure Previous/New on the GPU.",
        )
        return result

    # --- build the plan and inject ------------------------------------
    root = _export_root(context, args)
    return_states = {
        entry["global_id"]: pixelprobe.STATE_RENDER_TARGET for entry in candidates
    }
    plan = pixelprobe.build_plan(
        resource_id,
        x,
        y,
        [entry["global_id"] for entry in candidates],
        return_states=return_states,
        subresource=subresource,
    )
    data["plan"] = {"slot_count": plan.slot_count, "slots_per_event": 2}

    skip_build = bool(args.get("skip_build"))
    reuse_ok = skip_build and pixelprobe.plan_matches(root, plan)
    if skip_build and not reuse_ok:
        skip_build = False
        diagnostics.append((
            "warning",
            "--skip-build was refused: no probe matching this exact pixel and event "
            "set is installed, and reusing a binary built for another pixel would "
            "return values measured somewhere else. Rebuilding instead. Note that the "
            "probe is removed after every run unless --keep-probe is given, so "
            "--skip-build can only reuse a build from a previous --keep-probe run.",
        ))


    injection = pixelprobe.install(root, plan)
    data["probe_injection"] = {
        key: value
        for key, value in injection.items()
        if key != "injection_detail"
    }
    if injection.get("events_not_injected"):
        diagnostics.append((
            "warning",
            "Some events could not be spliced into the export, so their values will be "
            f"null rather than wrong: {injection['events_not_injected']}",
        ))
    if injection.get("events_not_found_in_export"):
        diagnostics.append((
            "warning",
            "Some candidate events have no GlobalId marker in the exported command "
            "lists, which happens for events on a queue the export does not cover: "
            f"{injection['events_not_found_in_export']}",
        ))

    trace_path: Path | None = None
    try:
        if skip_build and not injection.get("rebuild_needed"):
            executables = sorted(
                (root / "build" / "Release").glob("*.exe"),
                key=lambda p: -p.stat().st_size,
            )
            if not executables:
                raise not_found("built executable", str(root / "build" / "Release"))
            exe = executables[0]
            data["build"] = {"skipped": True, "executable": str(exe)}
        else:
            steps = _configure_and_build(
                root,
                str(args.get("generator") or "Visual Studio 18 2026"),
                int(args.get("build_timeout") or 3600),
                bool(args.get("force_reconfigure")),
                args,
            )
            data["build"] = steps
            exe = Path(steps["executable"])

        output_dir = (
            Path(str(args["output"])) if args.get("output") else Path("activity_renders")
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        # Absolute, always. The replay is launched with its cwd set to the export
        # directory, so a relative path here would make the probe write into the
        # export instead -- or fail to open the file at all, which reads as "the
        # replay produced no values" and looks exactly like a probe that never ran.
        output_dir = output_dir.resolve()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        trace_path = output_dir / f"pixelhistory_{stamp}_{resource_id}_{x}_{y}.json"


        environment = dict(os.environ)
        environment[pixelprobe.ENV_OUT] = str(trace_path)
        environment[pixelprobe.ENV_ENABLE] = "1"

        settle = int(args.get("settle_seconds") or 600)
        sentinel = Path(str(trace_path) + ".done")
        process = subprocess.Popen([str(exe)], cwd=str(root), env=environment)
        run: dict[str, Any] = {"pid": process.pid, "working_directory": str(root)}
        started = time.time()
        try:
            deadline = started + settle
            # Waiting on the sentinel, not the trace: the sentinel is written last, so
            # its presence means the JSON is complete rather than mid-write.
            while time.time() < deadline:
                if sentinel.exists():
                    break
                if process.poll() is not None:
                    run["exited_early"] = True
                    break
                time.sleep(2.0)
            run["seconds"] = round(time.time() - started, 1)
            run["completed"] = sentinel.exists()
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
            run["stopped"] = True
        data["replay"] = {"ran": True, **run}

        # --- read, decode, classify ----------------------------------
        trace = pixelprobe.read_trace(trace_path, plan)
        data["trace"] = {
            key: value for key, value in trace.items() if key != "samples"
        }
        if not trace.get("ok"):
            diagnostics.append(("warning", str(trace.get("reason"))))
        else:
            paired = pixelprobe.pair_samples(trace["samples"])
            for row in rows:
                phases = paired.get(row["global_id"]) or {}
                before = phases.get(pixelprobe.PHASE_BEFORE)
                after = phases.get(pixelprobe.PHASE_AFTER)
                verdict = pixelprobe.classify_event(
                    before,
                    after,
                    depth_evidence=row.get("depth_evidence"),
                )
                row["previous_value"] = (
                    before.value.to_dict() if before and before.value else None
                )
                row["new_value"] = after.value.to_dict() if after and after.value else None
                row.pop("reason", None)
                row.update(verdict)
                if before is not None and before.value is None and before.reason:
                    row["previous_value_reason"] = before.reason
                if after is not None and after.value is None and after.reason:
                    row["new_value_reason"] = after.reason

            ordered = [row["global_id"] for row in rows]
            data["consistency"] = pixelprobe.check_consistency(ordered, paired)
            measured = sum(1 for row in rows if row["new_value"] is not None)
            data["measured_event_count"] = measured
            if data["consistency"]["mismatches"]:
                diagnostics.append((
                    "warning",
                    "Adjacent samples disagree: an event's New Value differs from the "
                    "next event's Previous Value, so something outside the candidate "
                    "set wrote this texel. The per-event values are still measurements, "
                    "but the history is not a complete account of the frame.",
                ))
            if measured == 0:
                diagnostics.append((
                    "warning",
                    "No event produced a value. Every row reports why individually; "
                    "an empty result here means the probe did not run, which is a "
                    "different finding from the texel never being written.",
                ))
    finally:
        if bool(args.get("keep_probe")):
            data["probe_cleanup"] = {"action": "left installed (--keep-probe)"}
        else:
            data["probe_cleanup"] = pixelprobe.restore(root)

    degraded = any(level == "warning" for level, _ in diagnostics)
    paths = [str(trace_path)] if trace_path and trace_path.exists() else []
    result = (
        ToolResult.partial(data, output_paths=paths)
        if degraded
        else ToolResult.success(data, output_paths=paths)
    )
    for level, message in diagnostics:
        result.add_diagnostic(level, message)
    result.add_diagnostic(
        "info",
        "Previous/New are separate GPU reads taken immediately before and after each "
        "event. A single read at an event yields the value it inherited, not the one "
        "it wrote, which is why both are recorded.",
    )
    if capped:
        result.add_diagnostic(
            "warning",
            f"Candidates were capped at {max_events}; raise --max-events to sample more.",
        )
    return result

