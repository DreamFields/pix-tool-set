"""Read and export texture contents straight from the capture's own bytes.

Separate from the existing texture tools, which drive `pixtool save-resource` and
therefore need a GPU replay. Those fail outright on this depth surface (exit 12),
yet the export does contain its pixels: PIX uploads the initial contents of many
textures through CopyTextureRegion, and the subresource footprints are recorded.

Scope note, stated up front because it is the whole caveat: these are the contents
the resource was *initialised* with at capture time, not the result of the pass.
For a depth buffer that a draw writes to, the recorded plane is what the depth
buffer held going in. Reading the post-draw result requires a GPU replay, which is
what save-render-target is for.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

from ..context import ToolContext
from ..engine import footprint as fp
from ..errors import PixToolError, invalid_argument, not_found
from ..results import ToolResult
from ._common import tool, with_session

_NOTE = (
    "Reads bytes recorded in resources.bin, honouring the subresource footprints the "
    "export declares (format, dimensions and row pitch per plane). No GPU replay is "
    "involved, so it works on resources pixtool refuses, but it returns the contents the "
    "texture was initialised with rather than the output of any particular draw. A "
    "depth-stencil surface appears as two planes: R32 depth then R8 stencil."
)


def _resolve_resource(capture, args: dict[str, Any]):
    if args.get("resource_id") is not None:
        rid = int(args["resource_id"])
        resource = capture.resource(rid)
        if resource is None:
            raise not_found("resource", rid, "Run list-textures for valid ids.")
        return rid, resource

    # Locate the depth or a render target of a pass instead.
    selector = {
        key: args.get(key)
        for key in ("draw_index", "global_id", "queue_id", "pass_index", "pass_name")
        if args.get(key) is not None
    }
    if not selector:
        raise invalid_argument(
            "resource_id",
            "Pass --resource-id, or identify a pass with --queue-id/--global-id/"
            "--pass-name/--draw-index plus --target depth|rt0|rt1...",
        )
    if args.get("draw_index") is not None:
        draw = capture.draw_call(int(args["draw_index"]))
    else:
        entry = None
        if args.get("queue_id") is not None or args.get("global_id") is not None:
            entry = capture.find_pass_by_event(
                global_id=args.get("global_id"), queue_id=args.get("queue_id")
            )
        elif args.get("pass_index") is not None:
            entry = capture.find_pass(int(args["pass_index"]))
        elif args.get("pass_name"):
            entry = capture.find_pass(str(args["pass_name"]))
        if entry is None:
            raise not_found("pass", str(selector), "Run find-pass to check the selector.")
        draw = capture.draw_call(entry["first_draw_index"])
    if draw is None:
        raise not_found("draw", str(selector))

    target = str(args.get("target") or "depth").lower()
    if target in ("depth", "depthstencil", "dsv"):
        rid = draw.depth_stencil_resource_id
        if rid is None:
            raise not_found(
                "depth target",
                f"draw {draw.index}",
                "This draw binds no depth-stencil; try --target rt0.",
            )
    elif target.startswith("rt"):
        suffix = target[2:] or "0"
        if not suffix.isdigit():
            raise invalid_argument("target", f"{target!r} is not depth or rtN")
        index = int(suffix)
        ids = draw.render_target_resource_ids or []
        if index >= len(ids):
            raise not_found(
                "render target",
                target,
                f"draw {draw.index} binds {len(ids)} render target(s).",
            )
        rid = ids[index]
    else:
        raise invalid_argument("target", f"{target!r} is not depth or rtN")
    return rid, capture.resource(rid)


def _plane_summary(blob: bytes, entry, *, sample_limit: int = 400000) -> dict[str, Any]:
    """Describe one plane's values without loading the whole thing into JSON."""
    stride = fp.format_stride(entry.format)
    out: dict[str, Any] = {**entry.to_dict(), "bytes_per_pixel": stride}
    rows = fp.extract_rows(blob, entry)
    if rows is None or stride is None:
        out["decoded"] = False
        out["detail"] = f"{entry.format} is not a plain uncompressed format"
        return out
    packed = b"".join(rows)
    out["decoded"] = True
    out["rows_recovered"] = len(rows)
    out["packed_bytes"] = len(packed)
    out["pixels"] = len(packed) // stride if stride else 0

    count = min(out["pixels"], sample_limit)
    if count == 0:
        return out
    if stride == 4 and "R32" in entry.format.upper():
        values = struct.unpack_from(f"<{count}f", packed, 0)
        finite = [v for v in values if v == v and abs(v) != float("inf")]
        nonzero = [v for v in finite if v != 0.0]
        out["sampled_pixels"] = count
        out["min"] = min(finite) if finite else None
        out["max"] = max(finite) if finite else None
        out["zero_count"] = sum(1 for v in finite if v == 0.0)
        out["in_unit_range"] = sum(1 for v in finite if 0.0 <= v <= 1.0)
        if nonzero:
            out["nonzero_min"] = min(nonzero)
            out["nonzero_max"] = max(nonzero)
        out.update(_content_character(rows, entry))
    elif stride == 1:
        out["sampled_pixels"] = count
        histogram: dict[int, int] = {}
        for byte in packed[:count]:
            histogram[byte] = histogram.get(byte, 0) + 1
        out["distinct_values"] = len(histogram)
        out["top_values"] = sorted(
            histogram.items(), key=lambda item: -item[1]
        )[:6]
    return out


def _content_character(rows: list[bytes], entry) -> dict[str, Any]:
    """Tell an analytic gradient apart from a rendered depth image.

    Rendered depth has discontinuities wherever geometry occludes something behind
    it. A cleared or analytically initialised surface varies by a constant step and
    has none. Reporting this stops a caller from mistaking initialisation content
    for the output of the pass they asked about.
    """
    width, height = entry.width, entry.height
    if width < 4 or len(rows) < 4:
        return {}

    def row_values(y: int) -> tuple[float, ...]:
        return struct.unpack_from(f"<{width}f", rows[y], 0)

    middle = row_values(len(rows) // 2)
    diffs = [middle[x + 1] - middle[x] for x in range(min(64, width - 1))]
    if not diffs:
        return {}
    distinct = len({round(d, 12) for d in diffs})
    typical = sorted(abs(d) for d in diffs)[len(diffs) // 2] or 1e-12

    edges = 0
    step = max(len(rows) // 24, 1)
    for y in range(0, len(rows), step):
        values = row_values(y)
        for x in range(width - 1):
            if abs(values[x + 1] - values[x]) > typical * 50:
                edges += 1
                if edges > 64:
                    break
        if edges > 64:
            break

    linear = distinct <= 2
    if linear and edges == 0:
        character = "analytic_gradient"
        note = (
            "values change by a constant step and no geometry discontinuity was "
            "found, so this is initialisation content rather than depth written by "
            "a draw"
        )
    elif edges > 0:
        character = "rendered"
        note = "depth discontinuities present, consistent with rendered geometry"
    else:
        character = "uniform_or_unclear"
        note = "smooth but not linear; cannot tell initialisation from rendered output"
    return {
        "content_character": character,
        "content_note": note,
        "neighbour_step_distinct_values": distinct,
        "discontinuities_sampled": edges,
    }


def _to_greyscale(
    rows: list[bytes], entry, stride: int
) -> tuple[bytearray, float, float] | None:
    """Contrast-stretch a plane to 8-bit grey, returning (pixels, low, high)."""
    width, height = entry.width, len(rows)
    if stride == 4 and "R32" in entry.format.upper():
        values: list[float] = []
        for row in rows:
            values.extend(struct.unpack_from(f"<{width}f", row, 0))
        finite = [v for v in values if v == v and abs(v) != float("inf")]
        if not finite:
            return None
        low, high = min(finite), max(finite)
    elif stride == 1:
        values = [float(byte) for row in rows for byte in row[:width]]
        # Stretch over the values actually present, not the format's full range.
        # A 0/1 occupancy mask would otherwise render as solid black.
        present = [value for value in values]
        low, high = (min(present), max(present)) if present else (0.0, 255.0)
        if high <= low:
            low, high = 0.0, max(high, 1.0)
    else:
        return None

    span = high - low
    pixels = bytearray(width * height)
    for index, value in enumerate(values[: width * height]):
        if value != value:
            pixels[index] = 0
            continue
        if span <= 0:
            pixels[index] = 0
        else:
            scaled = int((value - low) / span * 255.0)
            pixels[index] = 0 if scaled < 0 else (255 if scaled > 255 else scaled)
    return pixels, low, high


def _encode_png(pixels: bytearray, width: int, height: int) -> bytes:
    """Minimal 8-bit greyscale PNG. Hand-rolled to avoid a new dependency."""
    import zlib

    raw = bytearray()
    for y in range(height):
        raw.append(0)  # no filter
        raw.extend(pixels[y * width : (y + 1) * width])

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return (
            struct.pack(">I", len(payload))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    header = struct.pack(">2I5B", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


@tool(
    name="read-resource-texture",
    summary=(
        "Read a texture or depth buffer from the capture's recorded bytes, without a GPU "
        "replay. Handles subresource footprints and row pitch, and can dump planes to disk."
    ),
    category="textures",
    parameters=with_session(
        resource_id={"type": "integer", "description": "Texture resource id."},
        queue_id={"type": "integer", "description": "PIX GUI Queue ID to take the target from."},
        global_id={"type": "integer", "description": "PIX GUI Global ID to take the target from."},
        pass_name={"type": "string", "description": "Pass name to take the target from."},
        pass_index={"type": "integer", "description": "Pass index to take the target from."},
        draw_index={"type": "integer", "description": "Draw index to take the target from."},
        target={
            "type": "string",
            "description": "Which attachment of the pass: depth (default) or rt0, rt1, ...",
        },
        pixels={
            "type": "integer",
            "description": "How many leading pixels of each plane to return. Default 0.",
        },
        at_x={"type": "integer", "description": "Read a single pixel at this column."},
        at_y={"type": "integer", "description": "Read a single pixel at this row."},
        output={
            "type": "string",
            "description": (
                "Directory to write each plane to as a raw, tightly packed .bin "
                "(pitch padding removed)."
            ),
        },
        png={
            "type": "string",
            "description": (
                "Also write a viewable 8-bit greyscale PNG per plane to this directory, "
                "contrast-stretched over the plane's actual value range. Reverse-Z depth "
                "is otherwise indistinguishable from black."
            ),
        },
    ),
    returns="Footprint of every plane, value statistics, optional pixel samples and file paths.",
    examples=[
        "pix-tool-set read-resource-texture --queue-id 17765 --target depth",
        "pix-tool-set read-resource-texture --queue-id 17765 --target depth --at-x 766 --at-y 382",
        "pix-tool-set read-resource-texture --resource-id 1985 --output G:\\out --png G:\\out",
    ],
    notes=_NOTE,
)
def read_resource_texture(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    resource_id, resource = _resolve_resource(capture, args)

    try:
        blob = capture.read_resource_bytes(resource_id)
    except PixToolError as exc:
        result = ToolResult.partial(
            {
                "resource_id": resource_id,
                "resource": resource.to_dict() if resource else None,
                "bytes_available": False,
            }
        )
        result.degrade(
            "No recorded bytes for this texture.",
            reason=exc.message,
            alternative=(
                "Use save-render-target, which replays the frame on the GPU and can "
                "capture contents the export never stored."
            ),
        )
        return result

    footprints = capture.resource_footprints(resource_id)
    data: dict[str, Any] = {
        "resource_id": resource_id,
        "resource": resource.to_dict(),
        "bytes_available": True,
        "blob_bytes": len(blob),
        "footprint_count": len(footprints),
        "contents_are": "initial upload recorded at capture time, not a draw's output",
    }

    if not footprints:
        data["planes"] = []
        result = ToolResult.partial(data)
        result.degrade(
            "Bytes were recovered but no subresource footprint was recorded, so the "
            "row pitch is unknown and the pixels cannot be laid out reliably.",
            hint="read-buffer can still dump the raw bytes.",
        )
        return result

    planes = [_plane_summary(blob, entry) for entry in footprints]
    data["planes"] = planes
    declared = sum(entry.size_bytes for entry in footprints)
    data["footprint_total_bytes"] = declared
    data["footprint_vs_blob_delta"] = len(blob) - declared

    want = int(args.get("pixels") or 0)
    at_x, at_y = args.get("at_x"), args.get("at_y")
    for entry, plane in zip(footprints, planes):
        if not plane.get("decoded"):
            continue
        stride = fp.format_stride(entry.format)
        rows = fp.extract_rows(blob, entry)
        if rows is None or not stride:
            continue

        if want:
            packed = b"".join(rows)
            count = min(want, len(packed) // stride)
            if stride == 4 and "R32" in entry.format.upper():
                plane["values"] = list(struct.unpack_from(f"<{count}f", packed, 0))
            elif stride == 1:
                plane["values"] = list(packed[:count])
            else:
                plane["values_hex"] = packed[: count * stride].hex(" ")

        if at_x is not None and at_y is not None:
            x, y = int(at_x), int(at_y)
            if 0 <= y < len(rows) and 0 <= x < entry.width:
                chunk = rows[y][x * stride : (x + 1) * stride]
                pixel: dict[str, Any] = {"x": x, "y": y, "hex": chunk.hex()}
                if stride == 4 and "R32" in entry.format.upper():
                    pixel["value"] = struct.unpack("<f", chunk)[0]
                elif stride == 1:
                    pixel["value"] = chunk[0]
                plane["pixel"] = pixel
            else:
                plane["pixel"] = {
                    "x": x,
                    "y": y,
                    "error": "outside this plane's dimensions",
                }

    output = args.get("output")
    if output:
        directory = Path(str(output))
        directory.mkdir(parents=True, exist_ok=True)
        written = []
        for entry, plane in zip(footprints, planes):
            rows = fp.extract_rows(blob, entry)
            if rows is None:
                continue
            name = (
                f"resource{resource_id}_sub{entry.subresource_index}_"
                f"{entry.width}x{entry.height}_"
                f"{entry.format.replace('DXGI_FORMAT_', '')}.bin"
            )
            path = directory / name
            payload = b"".join(rows)
            path.write_bytes(payload)
            written.append(
                {
                    "subresource_index": entry.subresource_index,
                    "path": str(path),
                    "bytes": len(payload),
                    "layout": "tightly packed rows, pitch padding removed",
                }
            )
            plane["output"] = str(path)
        data["files"] = written

    png_dir = args.get("png")
    if png_dir:
        directory = Path(str(png_dir))
        directory.mkdir(parents=True, exist_ok=True)
        images = []
        for entry, plane in zip(footprints, planes):
            rows = fp.extract_rows(blob, entry)
            stride = fp.format_stride(entry.format)
            if rows is None or not stride:
                continue
            grey = _to_greyscale(rows, entry, stride)
            if grey is None:
                continue
            pixels, low, high = grey
            name = (
                f"resource{resource_id}_sub{entry.subresource_index}_"
                f"{entry.width}x{entry.height}.png"
            )
            path = directory / name
            path.write_bytes(_encode_png(pixels, entry.width, entry.height))
            images.append(
                {
                    "subresource_index": entry.subresource_index,
                    "path": str(path),
                    "stretched_from": low,
                    "stretched_to": high,
                    "note": "8-bit greyscale, contrast stretched over the range above",
                }
            )
            plane["png"] = str(path)
        data["images"] = images

    undecoded = [plane for plane in planes if not plane.get("decoded")]
    initialisation = [
        plane for plane in planes if plane.get("content_character") == "analytic_gradient"
    ]
    if undecoded:
        result = ToolResult.partial(data)
        result.degrade(
            f"{len(undecoded)} of {len(planes)} plane(s) use a format this tool cannot "
            "lay out (block compressed or unrecognised).",
            formats=[plane.get("format") for plane in undecoded],
        )
        return result
    if initialisation:
        result = ToolResult.partial(data)
        result.degrade(
            "The recovered pixels look like initialisation content, not the output of "
            "this pass: values change by a constant step with no geometry edges.",
            reason=(
                "resources.bin stores what PIX saw uploaded. A depth buffer the GPU "
                "renders into is never uploaded, so its post-draw contents are absent."
            ),
            alternative=(
                "Use save-render-target to replay the frame on the GPU and capture the "
                "depth buffer as the pass left it."
            ),
            planes=[plane["subresource_index"] for plane in initialisation],
        )
        return result
    return ToolResult.success(data)
