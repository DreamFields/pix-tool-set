"""Read replayed render target values, decoded from pixtool's DDS output.

Why this exists: save-render-target writes an image file, which answers "what did
it look like" but not "what was the value at this pixel". A PNG is 8-bit and
already contrast-mapped, so numbers cannot be recovered from it. A DDS keeps the
source DXGI format, so this tool replays to DDS and decodes real values.

Two limits are inherent to pixtool and are reported rather than worked around:

  * Depth buffers cannot be written as DDS at all. pixtool refuses with PIXTOOL13
    ("Cannot save Depth Buffer as DDS ... ends with .png"), so replayed depth is
    8-bit only and this tool declines it with that explanation.
  * The resource is sampled as it stood *before* the requested event executes.
    Verified in this capture: every render target of Queue ID 17765 comes back
    entirely zero at its own event, while targets written by earlier draws come
    back over 99% populated. To see what a draw produced, ask at a later event.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..context import ToolContext
from ..engine import dds
from ..errors import PixToolError, invalid_argument, not_found
from ..results import ToolResult
from ._common import tool, with_session

_NOTE = (
    "Replays the frame through pixtool to a DDS, which preserves the original DXGI "
    "format, then decodes it. Values are the contents the resource had *before* the "
    "requested event ran, which is how pixtool samples it; pass a later event to see "
    "what a draw produced. Depth buffers are not supported because pixtool cannot write "
    "them as DDS: use read-resource-texture for recorded depth bytes, or "
    "save-render-target for an 8-bit depth image."
)


def _read_depth_levels(
    args: dict[str, Any], context: ToolContext, capture, record, draw
) -> ToolResult:
    """Read a replayed depth buffer as 16-bit levels.

    Deliberately not presented as float depth. pixtool documents --depth as a
    "visual representation" and only writes PNG, so what comes back is a
    quantisation of the original R32 depth, not the value the shader wrote.
    """
    from ..engine import png as png_mod

    resource_id = draw.depth_stencil_resource_id
    if resource_id is None:
        raise not_found(
            "depth target", f"draw {draw.index}", "This draw binds no depth-stencil."
        )

    keep = args.get("keep")
    path = (
        Path(str(keep))
        if keep
        else context.resolve_output(None, f"draw{draw.index}_depth.png")
    )
    if path.suffix.lower() != ".png":
        path = path.with_suffix(".png")

    pixtool = context.require_pixtool(args)
    pixtool.save_resource(
        Path(record.capture_path), path, global_id=draw.global_id, depth=True
    )

    try:
        image = png_mod.parse(path)
    except ValueError as exc:
        result = ToolResult.partial(
            {
                "draw_index": draw.index,
                "global_id": draw.global_id,
                "pass_name": draw.pass_name,
                "resource_id": resource_id,
                "png_path": str(path),
            }
        )
        result.degrade("The depth PNG could not be decoded.", reason=str(exc))
        return result

    resource = capture.resource(resource_id)
    data: dict[str, Any] = {
        "draw_index": draw.index,
        "global_id": draw.global_id,
        "pass_name": draw.pass_name,
        "resource_id": resource_id,
        "resource": resource.to_dict() if resource else None,
        "image": image.to_dict(),
        "png_path": str(path),
        "values_are": (
            f"{image.bit_depth}-bit greyscale levels (0..{image.max_level}), a "
            "quantisation of the original 32-bit float depth; pixtool offers no float "
            "depth export"
        ),
    }
    data.update(png_mod.content_character(image))

    want = int(args.get("pixels") or 0)
    if want:
        data["pixels"] = image.channel(0)[:want]

    at_x, at_y = args.get("at_x"), args.get("at_y")
    if at_x is not None and at_y is not None:
        try:
            level = image.pixel(int(at_x), int(at_y))
            data["pixel"] = {
                "x": int(at_x),
                "y": int(at_y),
                "level": level,
                "normalised": (
                    round(level / image.max_level, 8)
                    if isinstance(level, int)
                    else None
                ),
            }
        except IndexError as exc:
            data["pixel"] = {"x": int(at_x), "y": int(at_y), "error": str(exc)}

    if data.get("content_character") == "analytic_gradient":
        result = ToolResult.partial(data)
        result.degrade(
            "This depth image has no geometry discontinuities, so it still holds the "
            "pre-render state at this event.",
            reason=(
                "pixtool samples a resource before the requested event executes."
            ),
            alternative=(
                "Run find-depth-content to locate an event whose depth contains "
                "rendered geometry."
            ),
        )
        return result
    return ToolResult.success(data, output_paths=[str(path)])


@tool(
    name="read-replay-target",
    summary=(
        "Replay a draw's render target on the GPU and read back real pixel values, "
        "decoded from a lossless DDS rather than an 8-bit image."
    ),
    category="textures",
    parameters=with_session(
        draw_index={"type": "integer", "description": "Draw index to replay."},
        queue_id={
            "type": "integer",
            "description": (
                "Exported event list 'Queue ID' of the event. Present on every row of that "
                "export, but the export covers a single command queue, so draw_index is "
                "the selector that always resolves; Global ID is reported in the results "
                "only."
            ),
        },
        pass_name={"type": "string", "description": "Pass name (substring match)."},
        rtv={"type": "integer", "description": "Render target slot. Default 0."},
        depth={
            "type": "boolean",
            "description": (
                "Read the depth buffer instead, as 16-bit greyscale levels. No float "
                "export exists for depth."
            ),
        },
        at_x={"type": "integer", "description": "Column of a single pixel to read."},
        at_y={"type": "integer", "description": "Row of a single pixel to read."},
        pixels={
            "type": "integer",
            "description": "Return this many leading pixels in row-major order. Default 0.",
        },
        keep={
            "type": "string",
            "description": "Keep the intermediate .dds at this path instead of a temp file.",
        },
    ),
    returns="Format, dimensions, value statistics, optional pixel samples and the DDS path.",
    examples=[
        "pix-tool-set read-replay-target --queue-id 17765 --rtv 1 --at-x 766 --at-y 382",
        "pix-tool-set read-replay-target --draw-index 2352 --depth --at-x 766 --at-y 382",
        "pix-tool-set read-replay-target --draw-index 231 --pixels 4",
    ],
    notes=_NOTE,
)
def read_replay_target(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    record = context.session(args)
    if not record.capture_path:
        raise PixToolError(
            code="capture_required",
            message="Replaying a render target needs the original .wpix file.",
            stage="export",
            suggestion="Re-open the session with --capture pointing at the .wpix file.",
        )

    draw = capture.resolve_draw(
        draw_index=args.get("draw_index"),
        queue_id=args.get("queue_id"),
    )
    if draw is None and args.get("pass_name"):
        entry = capture.find_pass(str(args["pass_name"]))
        if entry is not None:
            draw = capture.draw_call(entry["first_draw_index"])
    if draw is None:
        raise invalid_argument(
            "draw_index/queue_id/pass_name", "provide one way to select the event"
        )

    slot = int(args.get("rtv") or 0)
    want_depth = bool(args.get("depth"))
    targets = draw.render_target_resource_ids or []

    if want_depth:
        return _read_depth_levels(args, context, capture, record, draw)

    if slot >= len(targets):
        raise not_found(
            "render target",
            f"rtv{slot}",
            f"draw {draw.index} binds {len(targets)} render target(s).",
        )
    resource_id = targets[slot]
    resource = capture.resource(resource_id)
    if resource is not None and resource.is_depth_stencil:
        raise invalid_argument(
            "rtv",
            "pixtool cannot write a depth buffer as DDS (PIXTOOL13). Pass --depth to "
            "read it as 16-bit levels instead.",
        )

    keep = args.get("keep")
    path = (
        Path(str(keep))
        if keep
        else context.resolve_output(None, f"draw{draw.index}_rtv{slot}.dds")
    )
    if path.suffix.lower() != ".dds":
        path = path.with_suffix(".dds")

    pixtool = context.require_pixtool(args)
    pixtool.save_resource(
        Path(record.capture_path), path, global_id=draw.global_id, rtv=slot
    )

    try:
        image = dds.parse(path)
    except ValueError as exc:
        result = ToolResult.partial(
            {
                "draw_index": draw.index,
                "global_id": draw.global_id,
                "pass_name": draw.pass_name,
                "rtv": slot,
                "resource_id": resource_id,
                "dds_path": str(path),
            }
        )
        result.degrade(
            "The replay produced a DDS this decoder does not understand.",
            reason=str(exc),
            alternative="save-render-target writes a viewable PNG instead.",
        )
        return result

    data: dict[str, Any] = {
        "draw_index": draw.index,
        "global_id": draw.global_id,
        "pass_name": draw.pass_name,
        "rtv": slot,
        "resource_id": resource_id,
        "resource": resource.to_dict() if resource else None,
        "image": image.to_dict(),
        "dds_path": str(path),
        "values_are": (
            "contents before this event executed, which is how pixtool samples a "
            "resource; ask at a later event to see what this draw produced"
        ),
    }

    expected = image.width * image.height * image.bytes_per_pixel
    actual = len(image.data) - image.pixel_offset
    data["payload_matches_dimensions"] = expected == actual
    if expected != actual:
        data["payload_delta"] = actual - expected

    # Whole-surface emptiness check on raw bytes: cheap and decisive.
    payload = image.data[image.pixel_offset :]
    nonzero_bytes = sum(1 for byte in payload if byte)
    data["nonzero_bytes"] = nonzero_bytes
    data["nonzero_byte_ratio"] = round(nonzero_bytes / max(len(payload), 1), 6)
    data["surface_is_empty"] = nonzero_bytes == 0

    want = int(args.get("pixels") or 0)
    if want:
        data["pixels"] = list(image.iter_pixels(limit=want))

    at_x, at_y = args.get("at_x"), args.get("at_y")
    if at_x is not None and at_y is not None:
        try:
            data["pixel"] = {
                "x": int(at_x),
                "y": int(at_y),
                "value": image.pixel(int(at_x), int(at_y)),
            }
        except IndexError as exc:
            data["pixel"] = {"x": int(at_x), "y": int(at_y), "error": str(exc)}

    if not keep and path.exists():
        data["dds_path"] = str(path)

    if data["surface_is_empty"]:
        result = ToolResult.partial(data)
        result.degrade(
            "The replayed surface is entirely zero.",
            reason=(
                "pixtool samples a resource before the requested event executes, so a "
                "target that this draw is the first to write has nothing in it yet."
            ),
            alternative=(
                "Select a later event that reads or overwrites the same resource to see "
                "the contents this draw produced."
            ),
        )
        return result
    return ToolResult.success(data, output_paths=[str(path)])
