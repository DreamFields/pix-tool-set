"""Find the event where a depth buffer actually holds rendered geometry.

This exists because of a trap measured in this capture. pixtool samples a resource
as it stood *before* the requested event, so asking at the pass that writes depth
returns the pre-write state. Of the 16 events that bind rid 1985 as depth-stencil,
exactly one (draw 2352) comes back with geometry; the other 15 return the same
analytic gradient. Guessing the event therefore fails 15 times out of 16.

The scan is honest about cost: each probe is a full GPU replay, so the number of
probes is capped and reported.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..context import ToolContext
from ..engine import png as png_mod
from ..errors import PixToolError, not_found
from ..results import ToolResult
from ._common import tool, with_session

_NOTE = (
    "Each probe is a separate GPU replay through pixtool, so this is slow by nature; "
    "raise --max-probes only when needed. Depth is exported as 16-bit greyscale, which "
    "pixtool documents as a visual representation: levels are a quantisation of the "
    "original 32-bit float, not the float itself. There is no float depth export - "
    "pixtool refuses DDS for depth buffers (PIXTOOL13)."
)


def _depth_events(capture, resource_id: int) -> list:
    return [
        draw
        for draw in capture.draw_calls
        if draw.depth_stencil_resource_id == resource_id and draw.global_id is not None
    ]


@tool(
    name="find-depth-content",
    summary=(
        "Scan the events that bind a depth buffer and report which ones actually contain "
        "rendered geometry, so the right event can be exported."
    ),
    category="textures",
    parameters=with_session(
        resource_id={"type": "integer", "description": "Depth-stencil resource id."},
        global_id={
            "type": "integer",
            "description": (
                "PIX Global ID of the event to take the depth target from. Unique across "
                "every queue, so use this for an id copied out of the PIX GUI."
            ),
        },
        queue_id={
            "type": "integer",
            "description": (
                "Exported event list 'Queue ID' of the event to take the depth target "
                "from. Every row of that export has one, but the export covers a single "
                "command queue, so use global_id or draw_index for actions outside it."
            ),
        },
        draw_index={"type": "integer", "description": "Take the depth target from this draw."},
        max_probes={
            "type": "integer",
            "description": "How many events to replay at most. Default 8.",
        },
        stop_on_first={
            "type": "boolean",
            "description": "Stop as soon as an event with geometry is found. Default true.",
        },
        output={
            "type": "string",
            "description": "Directory to keep the probe PNGs in.",
        },
    ),
    returns="Per-event content classification and the best event to export from.",
    examples=[
        "pix-tool-set find-depth-content --queue-id 17765",
        "pix-tool-set find-depth-content --global-id 5417",
        "pix-tool-set find-depth-content --resource-id 1985 --max-probes 16 --stop-on-first false",
    ],
    notes=_NOTE,
)
def find_depth_content(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    record = context.session(args)
    if not record.capture_path:
        raise PixToolError(
            code="capture_required",
            message="Probing depth contents needs the original .wpix file.",
            stage="export",
            suggestion="Re-open the session with --capture pointing at the .wpix file.",
        )

    resource_id = args.get("resource_id")
    if resource_id is None:
        draw = capture.resolve_draw(
            draw_index=args.get("draw_index"),
            global_id=args.get("global_id"),
            queue_id=args.get("queue_id"),
        )
        if draw is None:
            raise not_found(
                "depth resource",
                "<no selector>",
                "Pass --resource-id, or a pass selector such as --global-id.",
            )
        resource_id = draw.depth_stencil_resource_id
        if resource_id is None:
            raise not_found(
                "depth target", f"draw {draw.index}", "This draw binds no depth-stencil."
            )
    resource_id = int(resource_id)

    events = _depth_events(capture, resource_id)
    if not events:
        raise not_found(
            "depth-stencil events",
            str(resource_id),
            "No event in this capture binds that resource as depth-stencil.",
        )

    limit = int(args.get("max_probes") or 8)
    stop_flag = args.get("stop_on_first")
    stop_on_first = True if stop_flag is None else bool(stop_flag)

    directory = Path(str(args["output"])) if args.get("output") else None
    if directory:
        directory.mkdir(parents=True, exist_ok=True)

    pixtool = context.require_pixtool(args)
    probes: list[dict[str, Any]] = []
    rendered: list[dict[str, Any]] = []

    for draw in events[:limit]:
        path = (
            directory / f"depth_draw{draw.index}.png"
            if directory
            else context.resolve_output(None, f"depth_probe_{draw.index}.png")
        )
        entry: dict[str, Any] = {
            "draw_index": draw.index,
            "global_id": draw.global_id,
            "pass_name": draw.pass_name,
        }
        try:
            pixtool.save_resource(
                Path(record.capture_path), path, global_id=draw.global_id, depth=True
            )
        except PixToolError as exc:
            entry["exported"] = False
            entry["detail"] = exc.message
            probes.append(entry)
            continue

        entry["exported"] = True
        entry["path"] = str(path)
        try:
            image = png_mod.parse(path)
        except ValueError as exc:
            entry["decoded"] = False
            entry["detail"] = str(exc)
            probes.append(entry)
            continue

        entry["decoded"] = True
        entry["image"] = image.to_dict()
        entry.update(png_mod.content_character(image))
        probes.append(entry)
        if entry.get("content_character") == "rendered":
            rendered.append(entry)
            if stop_on_first:
                break

    data: dict[str, Any] = {
        "resource_id": resource_id,
        "depth_events_total": len(events),
        "events_probed": len(probes),
        "probes": probes,
        "events_with_geometry": [entry["draw_index"] for entry in rendered],
    }
    if rendered:
        best = rendered[0]
        data["best_event"] = {
            "draw_index": best["draw_index"],
            "global_id": best["global_id"],
            "pass_name": best["pass_name"],
            "distinct_levels": best.get("distinct_levels"),
            "discontinuities": best.get("discontinuities_sampled"),
            "path": best.get("path"),
        }
        data["how_to_export"] = (
            f"pix-tool-set save-render-target --draw-index {best['draw_index']} "
            "--depth -o depth.png"
        )
        return ToolResult.success(data)

    result = ToolResult.partial(data)
    result.degrade(
        f"None of the {len(probes)} probed event(s) contained rendered geometry.",
        reason=(
            "pixtool samples a resource before the event executes, so events at or "
            "before the first depth write all return the pre-render state."
        ),
        alternative=(
            "Raise --max-probes to cover more of the "
            f"{len(events)} events that bind this resource."
        ),
    )
    return result
