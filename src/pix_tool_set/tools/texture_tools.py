"""Requirement section 4: texture analysis.

Pixel-level work (read pixels, statistics, pick, sample region) needs the actual
image, which PIX only materialises through ``pixtool save-resource``.  These
tools therefore export a PNG/DDS first and then decode it locally.  PNG decoding
is implemented in-process with zlib so the toolkit keeps a zero-dependency
install; when a request needs a format the local decoder cannot read, the result
comes back as ``partial`` with the exported file path so the caller still has the
data.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Any, Iterable

from ..context import ToolContext
from ..engine.model import EventKind, ViewKind
from ..errors import PixToolError, invalid_argument, not_found
from ..results import ToolResult
from ._common import (
    DRAW_SELECTOR,
    PAGE_PARAMS,
    page_args,
    page_envelope,
    percent,
    tool,
    with_session,
)

_PIXEL_NOTE = (
    "Pixel data is obtained by asking pixtool to save the resource, then decoding the "
    "image locally. The capture must replay on this machine for that to succeed."
)


# ==========================================================================
# minimal PNG reader (stdlib only)
# ==========================================================================
class PngImage:
    def __init__(self, width: int, height: int, channels: int, depth: int, rows: list[bytes]):
        self.width = width
        self.height = height
        self.channels = channels
        self.depth = depth
        self.rows = rows

    @property
    def bytes_per_sample(self) -> int:
        return 2 if self.depth == 16 else 1

    def pixel(self, x: int, y: int) -> list[float]:
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise invalid_argument("x/y", f"({x},{y}) is outside {self.width}x{self.height}")
        stride = self.channels * self.bytes_per_sample
        row = self.rows[y]
        start = x * stride
        out: list[float] = []
        maximum = 65535.0 if self.depth == 16 else 255.0
        for channel in range(self.channels):
            offset = start + channel * self.bytes_per_sample
            if self.depth == 16:
                value = struct.unpack_from(">H", row, offset)[0]
            else:
                value = row[offset]
            out.append(value / maximum)
        return out

    def iter_pixels(self, box: tuple[int, int, int, int] | None = None) -> Iterable[list[float]]:
        x0, y0, x1, y1 = box or (0, 0, self.width, self.height)
        for y in range(max(y0, 0), min(y1, self.height)):
            for x in range(max(x0, 0), min(x1, self.width)):
                yield self.pixel(x, y)


_PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def read_png(path: Path) -> PngImage:
    raw = path.read_bytes()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise PixToolError(
            code="image_decode_unsupported",
            message=f"{path.name} is not a PNG file.",
            stage="texture",
            paths=[str(path)],
            suggestion="Export with a .png extension so the built-in decoder can read it.",
        )
    position = 8
    width = height = depth = color_type = 0
    interlace = 0
    palette = b""
    idat = bytearray()
    while position + 8 <= len(raw):
        length, tag = struct.unpack_from(">I4s", raw, position)
        position += 8
        payload = raw[position : position + length]
        position += length + 4
        if tag == b"IHDR":
            width, height, depth, color_type, _comp, _filt, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
        elif tag == b"PLTE":
            palette = payload
        elif tag == b"IDAT":
            idat += payload
        elif tag == b"IEND":
            break

    if interlace:
        raise PixToolError(
            code="image_decode_unsupported",
            message="Interlaced PNG is not supported by the built-in decoder.",
            stage="texture",
            paths=[str(path)],
        )
    channels = _PNG_CHANNELS.get(color_type)
    if channels is None:
        raise PixToolError(
            code="image_decode_unsupported",
            message=f"Unsupported PNG color type {color_type}.",
            stage="texture",
            paths=[str(path)],
        )

    data = zlib.decompress(bytes(idat))
    sample_bytes = 2 if depth == 16 else 1
    if depth < 8:
        raise PixToolError(
            code="image_decode_unsupported",
            message=f"PNG bit depth {depth} is not supported by the built-in decoder.",
            stage="texture",
            paths=[str(path)],
        )
    stride = width * channels * sample_bytes
    bpp = channels * sample_bytes

    rows: list[bytes] = []
    previous = bytearray(stride)
    offset = 0
    for _ in range(height):
        filter_type = data[offset]
        offset += 1
        line = bytearray(data[offset : offset + stride])
        offset += stride
        if filter_type == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif filter_type == 2:
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 0xFF
        elif filter_type == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif filter_type == 4:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                up = previous[i]
                up_left = previous[i - bpp] if i >= bpp else 0
                p = left + up - up_left
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - up_left)
                predictor = left if (pa <= pb and pa <= pc) else (up if pb <= pc else up_left)
                line[i] = (line[i] + predictor) & 0xFF
        rows.append(bytes(line))
        previous = line

    if color_type == 3 and palette:
        expanded: list[bytes] = []
        for row in rows:
            out = bytearray()
            for index in row[:width]:
                base = index * 3
                out += palette[base : base + 3]
            expanded.append(bytes(out))
        return PngImage(width, height, 3, 8, expanded)
    return PngImage(width, height, channels, depth, rows)


# ==========================================================================
# helpers
# ==========================================================================
def _texture_export(
    context: ToolContext,
    args: dict[str, Any],
    capture,
    *,
    resource_id: int | None,
    global_id: int | None,
    rtv: int,
    depth: bool,
    stem: str,
) -> tuple[Path, list[dict[str, Any]]]:
    """Ask pixtool to materialise a texture as PNG. Returns (path, diagnostics)."""
    record = context.session(args)
    if not record.capture_path:
        raise PixToolError(
            code="capture_required",
            message="This operation needs the original .wpix file, but the session only has an export directory.",
            stage="texture",
            suggestion="Re-open with `session-open --capture <file.wpix>`.",
        )
    if global_id is None and resource_id is not None:
        usage = capture.resource_usage.get(resource_id)
        if usage:
            candidates = usage["render_target_draws"] or usage["depth_draws"]
            if candidates:
                draw = capture.draw_calls[candidates[-1]]
                global_id = draw.global_id
                if resource_id in draw.render_target_resource_ids:
                    rtv = draw.render_target_resource_ids.index(resource_id)
                elif draw.depth_stencil_resource_id == resource_id:
                    depth = True

    out_path = context.resolve_output(args.get("output"), f"{stem}.png")
    pixtool = context.require_pixtool(args)
    pixtool.save_resource(
        Path(record.capture_path),
        out_path,
        global_id=global_id,
        rtv=None if depth else rtv,
        depth=depth,
    )
    diagnostics = [
        {
            "level": "info",
            "message": "Texture exported through pixtool save-resource.",
            "global_id": global_id,
            "rtv": None if depth else rtv,
            "depth": depth,
        }
    ]
    return out_path, diagnostics


def _channel_statistics(image: PngImage, box: tuple[int, int, int, int] | None = None) -> dict[str, Any]:
    counts = image.channels
    minimum = [1e30] * counts
    maximum = [-1e30] * counts
    total = [0.0] * counts
    total_squared = [0.0] * counts
    samples = 0
    negatives = 0
    non_finite = 0
    saturated = 0

    for pixel in image.iter_pixels(box):
        samples += 1
        for index, value in enumerate(pixel):
            minimum[index] = min(minimum[index], value)
            maximum[index] = max(maximum[index], value)
            total[index] += value
            total_squared[index] += value * value
            if value < 0.0:
                negatives += 1
            if value >= 1.0:
                saturated += 1

    if samples == 0:
        return {"samples": 0, "channels": []}

    channels = []
    names = ["r", "g", "b", "a"][:counts] if counts <= 4 else [f"c{i}" for i in range(counts)]
    for index in range(counts):
        mean = total[index] / samples
        variance = max(total_squared[index] / samples - mean * mean, 0.0)
        channels.append(
            {
                "channel": names[index],
                "min": round(minimum[index], 6),
                "max": round(maximum[index], 6),
                "mean": round(mean, 6),
                "stddev": round(variance**0.5, 6),
            }
        )
    return {
        "samples": samples,
        "channels": channels,
        "negative_samples": negatives,
        "saturated_samples": saturated,
        "non_finite_samples": non_finite,
    }


# ==========================================================================
# 4.1 list textures
# ==========================================================================
@tool(
    name="list-textures",
    summary=(
        "List texture resources with dimensions, format, mip count, estimated memory and "
        "how many draws touch each one."
    ),
    category="textures",
    parameters=with_session(
        PAGE_PARAMS,
        kind={
            "type": "string",
            "enum": ["texture1d", "texture2d", "texture3d"],
            "description": "Restrict to one texture dimensionality.",
        },
        format={"type": "string", "description": "Substring match on the DXGI format name."},
        min_width={"type": "integer", "description": "Minimum width in pixels."},
        min_size_bytes={"type": "integer", "description": "Minimum estimated size in bytes."},
        render_target={"type": "boolean", "description": "Only render-target textures."},
        depth_stencil={"type": "boolean", "description": "Only depth-stencil textures."},
        uav={"type": "boolean", "description": "Only UAV-capable textures."},
        used_only={"type": "boolean", "description": "Only textures referenced by a draw."},
        sort_by={
            "type": "string",
            "enum": ["size", "pixels", "id", "width"],
            "description": "Ordering. Default 'size'.",
        },
    ),
    returns="Paged texture list with usage counts.",
    examples=[
        "pix-tool-set list-textures --limit 20",
        "pix-tool-set list-textures --render-target --min-width 1024",
    ],
)
def list_textures(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    offset, limit = page_args(args)
    kind = args.get("kind")

    window, total = capture.find_resources(
        kind=kind,
        min_width=int(args.get("min_width") or 0),
        min_size_bytes=int(args.get("min_size_bytes") or 0),
        format_filter=args.get("format"),
        render_target=args.get("render_target"),
        depth_stencil=args.get("depth_stencil"),
        uav=args.get("uav"),
        used_only=bool(args.get("used_only")),
        predicate=(lambda r: r.is_texture) if kind is None else None,
        offset=offset,
        limit=limit,
        sort_by=args.get("sort_by") or "size",
    )
    usage = capture.resource_usage
    rows = []
    for resource in window:
        entry = resource.to_dict()
        use = usage.get(resource.api_id)
        entry["usage"] = {
            "read_draws": len(use["read_draws"]) if use else 0,
            "write_draws": len(use["write_draws"]) if use else 0,
            "passes": use["passes"] if use else [],
        }
        rows.append(entry)
    return ToolResult.success(
        {"textures": rows, **page_envelope(total, offset, limit, len(window))}
    )


# ==========================================================================
# 4.2 texture inventory statistics
# ==========================================================================
@tool(
    name="texture-stats",
    summary=(
        "Aggregate texture inventory: totals by dimensionality and format, estimated "
        "memory footprint, render-target vs sampled split, and the largest consumers."
    ),
    category="textures",
    parameters=with_session(
        top={"type": "integer", "description": "How many largest textures to list. Default 10."},
    ),
    returns="Inventory totals, per-format breakdown and the biggest textures.",
    examples=["pix-tool-set texture-stats"],
)
def texture_stats(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    textures = [r for r in capture.resources.values() if r.is_texture]
    total_bytes = sum(r.size_bytes for r in textures)

    by_format: dict[str, dict[str, Any]] = {}
    for resource in textures:
        entry = by_format.setdefault(
            resource.format, {"format": resource.format, "count": 0, "bytes": 0}
        )
        entry["count"] += 1
        entry["bytes"] += resource.size_bytes
    for entry in by_format.values():
        entry["share_percent"] = percent(entry["bytes"], total_bytes)

    by_kind: dict[str, int] = {}
    for resource in textures:
        by_kind[resource.kind.value] = by_kind.get(resource.kind.value, 0) + 1

    usage = capture.resource_usage
    unused = [r for r in textures if r.api_id not in usage]
    top_count = int(args.get("top") or 10)
    largest = sorted(textures, key=lambda r: -r.size_bytes)[:top_count]

    return ToolResult.success(
        {
            "totals": {
                "textures": len(textures),
                "estimated_bytes": total_bytes,
                "estimated_mib": round(total_bytes / 1048576.0, 2),
                "render_targets": sum(1 for r in textures if r.is_render_target),
                "depth_stencils": sum(1 for r in textures if r.is_depth_stencil),
                "uav_capable": sum(1 for r in textures if r.is_uav),
                "multisampled": sum(1 for r in textures if r.sample_count > 1),
                "mipmapped": sum(1 for r in textures if r.mip_levels > 1),
                "unused_in_frame": len(unused),
            },
            "by_kind": by_kind,
            "by_format": sorted(by_format.values(), key=lambda e: -e["bytes"]),
            "largest": [r.to_dict() for r in largest],
            "note": "Sizes are computed from the resource descriptor, not from residency data.",
        }
    )


# ==========================================================================
# 4.3 texture detail
# ==========================================================================
@tool(
    name="texture-info",
    summary=(
        "Full detail for one texture: descriptor fields, every descriptor/view that points "
        "at it, and which draws read or write it."
    ),
    category="textures",
    parameters=with_session(
        resource_id={"type": "integer", "description": "Resource id from list-textures."},
        max_views={"type": "integer", "description": "Cap on listed views. Default 20."},
        max_draws={"type": "integer", "description": "Cap on listed draws. Default 20."},
        required=["resource_id"],
    ),
    returns="Descriptor detail, views, and reader/writer draw lists.",
    examples=["pix-tool-set texture-info --resource-id 641"],
)
def texture_info(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    resource_id = int(args["resource_id"])
    resource = capture.resource(resource_id)
    if resource is None:
        raise not_found("texture", resource_id, "Run list-textures to find valid ids.")

    max_views = int(args.get("max_views") or 20)
    max_draws = int(args.get("max_draws") or 20)

    views = [
        {**view.to_dict(), "heap": key[0], "index": key[1]}
        for key, view in capture.views.items()
        if view.resource_id == resource_id
    ]
    usage = capture.resource_usage.get(resource_id, {})

    def draw_rows(indices: list[int]) -> list[dict[str, Any]]:
        rows = []
        for index in indices[:max_draws]:
            draw = capture.draw_calls[index]
            rows.append(
                {
                    "draw_index": draw.index,
                    "global_id": draw.global_id,
                    "api": draw.api,
                    "pass_name": draw.pass_name,
                }
            )
        return rows

    return ToolResult.success(
        {
            "texture": resource.to_dict(),
            "views": views[:max_views],
            "view_count": len(views),
            "usage": {
                "read_draw_count": len(usage.get("read_draws", [])),
                "write_draw_count": len(usage.get("write_draws", [])),
                "passes": usage.get("passes", []),
                "readers": draw_rows(usage.get("read_draws", [])),
                "writers": draw_rows(usage.get("write_draws", [])),
                "render_target_draws": draw_rows(usage.get("render_target_draws", [])),
            },
        }
    )


# ==========================================================================
# 4.4 export texture
# ==========================================================================
@tool(
    name="export-texture",
    summary=(
        "Export a texture to an image file via pixtool. Choose the event with --global-id "
        "or let the tool pick the last draw that had the resource bound."
    ),
    category="textures",
    parameters=with_session(
        resource_id={"type": "integer", "description": "Resource id to export."},
        global_id={"type": "integer", "description": "Event whose contents to capture."},
        rtv={"type": "integer", "description": "Render target slot index. Default 0."},
        depth={"type": "boolean", "description": "Export the depth buffer instead of an RTV."},
        output={"type": "string", "description": "Output file path. Extension picks the format."},
    ),
    returns="Path of the written image plus the event that supplied it.",
    examples=[
        "pix-tool-set export-texture --resource-id 641 -o rt.png",
        "pix-tool-set export-texture --global-id 3644 --depth -o depth.png",
    ],
    notes=_PIXEL_NOTE,
)
def export_texture(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    resource_id = args.get("resource_id")
    global_id = args.get("global_id")
    if resource_id is None and global_id is None:
        raise invalid_argument("resource_id/global_id", "provide at least one")

    stem = f"texture_{resource_id if resource_id is not None else 'gid' + str(global_id)}"
    path, diagnostics = _texture_export(
        context,
        args,
        capture,
        resource_id=int(resource_id) if resource_id is not None else None,
        global_id=int(global_id) if global_id is not None else None,
        rtv=int(args.get("rtv") or 0),
        depth=bool(args.get("depth")),
        stem=stem,
    )
    data: dict[str, Any] = {
        "path": str(path),
        "bytes": path.stat().st_size,
        "resource_id": resource_id,
    }
    if resource_id is not None:
        resource = capture.resource(int(resource_id))
        if resource is not None:
            data["texture"] = resource.to_dict()
    return ToolResult.success(data, output_paths=[str(path)], diagnostics=diagnostics)


# ==========================================================================
# 4.5 export every texture of a draw call
# ==========================================================================
@tool(
    name="export-draw-textures",
    summary=(
        "Export the textures involved in one draw call: its render targets, depth buffer, "
        "and optionally the textures it samples."
    ),
    category="textures",
    parameters=with_session(
        DRAW_SELECTOR,
        output_dir={"type": "string", "description": "Directory for the exported images."},
        include_depth={"type": "boolean", "description": "Also export the depth buffer. Default true."},
        include_inputs={
            "type": "boolean",
            "description": "Also try to export sampled (SRV) textures. Slower.",
        },
        max_files={"type": "integer", "description": "Safety cap on exported files. Default 12."},
    ),
    returns="List of written files with the resource each one came from.",
    examples=["pix-tool-set export-draw-textures --draw-index 2461 --output-dir out/draw2461"],
    notes=_PIXEL_NOTE,
)
def export_draw_textures(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    record = context.session(args)
    draw = capture.resolve_draw(
        draw_index=args.get("draw_index"),
        global_id=args.get("global_id"),
        queue_id=args.get("queue_id"),
    )
    if draw is None:
        raise not_found("draw call", args.get("draw_index") or args.get("global_id"))
    if not record.capture_path:
        raise PixToolError(
            code="capture_required",
            message="Exporting textures needs the original .wpix file.",
            stage="texture",
            suggestion="Re-open the session with --capture pointing at the .wpix file.",
        )

    base = context.resolve_output(args.get("output_dir"), f"draw_{draw.index}")
    base.mkdir(parents=True, exist_ok=True)
    pixtool = context.require_pixtool(args)
    cap_path = Path(record.capture_path)

    include_depth = args.get("include_depth")
    include_depth = True if include_depth is None else bool(include_depth)
    max_files = int(args.get("max_files") or 12)

    written: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for slot, resource_id in enumerate(draw.render_target_resource_ids):
        if len(written) >= max_files:
            break
        target = base / f"rt{slot}_res{resource_id}.png"
        try:
            pixtool.save_resource(cap_path, target, global_id=draw.global_id, rtv=slot)
            written.append(
                {"path": str(target), "resource_id": resource_id, "role": f"rtv{slot}"}
            )
        except PixToolError as exc:
            failures.append({"resource_id": resource_id, "role": f"rtv{slot}", "reason": exc.message})

    if include_depth and draw.depth_stencil_resource_id is not None and len(written) < max_files:
        target = base / f"depth_res{draw.depth_stencil_resource_id}.png"
        try:
            pixtool.save_resource(cap_path, target, global_id=draw.global_id, depth=True)
            written.append(
                {
                    "path": str(target),
                    "resource_id": draw.depth_stencil_resource_id,
                    "role": "depth",
                }
            )
        except PixToolError as exc:
            failures.append(
                {
                    "resource_id": draw.depth_stencil_resource_id,
                    "role": "depth",
                    "reason": exc.message,
                }
            )

    if bool(args.get("include_inputs")):
        seen: set[int] = set()
        for view in draw.srvs:
            if len(written) >= max_files:
                break
            rid = view.resource_id
            if rid is None or rid in seen:
                continue
            resource = capture.resource(rid)
            if resource is None or not resource.is_texture:
                continue
            seen.add(rid)
            target = base / f"srv_res{rid}.png"
            try:
                pixtool.save_resource(cap_path, target, global_id=draw.global_id, rtv=0)
                written.append({"path": str(target), "resource_id": rid, "role": "srv"})
            except PixToolError as exc:
                failures.append({"resource_id": rid, "role": "srv", "reason": exc.message})

    data = {
        "draw_index": draw.index,
        "global_id": draw.global_id,
        "output_dir": str(base),
        "files": written,
        "failures": failures,
    }
    result = ToolResult.success(
        data, output_paths=[entry["path"] for entry in written]
    )
    if failures:
        result.degrade(
            f"{len(failures)} texture(s) could not be exported.",
            hint="PIX can only save resources that are bound as an RTV/DSV at that event.",
        )
    return result


# ==========================================================================
# 4.6 read pixels
# ==========================================================================
@tool(
    name="read-texture-pixels",
    summary=(
        "Read a rectangle of pixel values from a texture. Exports the image through pixtool "
        "and decodes it locally, returning normalised float channel values."
    ),
    category="textures",
    parameters=with_session(
        resource_id={"type": "integer", "description": "Resource id to read."},
        global_id={"type": "integer", "description": "Event whose contents to read."},
        x={"type": "integer", "description": "Left edge. Default 0."},
        y={"type": "integer", "description": "Top edge. Default 0."},
        width={"type": "integer", "description": "Rectangle width. Default 8."},
        height={"type": "integer", "description": "Rectangle height. Default 8."},
        depth={"type": "boolean", "description": "Read the depth buffer."},
        rtv={"type": "integer", "description": "Render target slot. Default 0."},
        output={"type": "string", "description": "Where to keep the intermediate PNG."},
        max_pixels={"type": "integer", "description": "Safety cap on returned pixels. Default 4096."},
    ),
    returns="Row-major array of pixels, each an array of channel values in 0..1.",
    examples=[
        "pix-tool-set read-texture-pixels --resource-id 641 --x 100 --y 100 --width 4 --height 4"
    ],
    notes=_PIXEL_NOTE,
)
def read_texture_pixels(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    resource_id = args.get("resource_id")
    global_id = args.get("global_id")
    if resource_id is None and global_id is None:
        raise invalid_argument("resource_id/global_id", "provide at least one")

    path, diagnostics = _texture_export(
        context,
        args,
        capture,
        resource_id=int(resource_id) if resource_id is not None else None,
        global_id=int(global_id) if global_id is not None else None,
        rtv=int(args.get("rtv") or 0),
        depth=bool(args.get("depth")),
        stem=f"pixels_{resource_id if resource_id is not None else global_id}",
    )

    x = int(args.get("x") or 0)
    y = int(args.get("y") or 0)
    width = int(args.get("width") or 8)
    height = int(args.get("height") or 8)
    cap = int(args.get("max_pixels") or 4096)
    if width * height > cap:
        raise invalid_argument(
            "width*height", f"{width * height} pixels exceeds max_pixels={cap}"
        )

    try:
        image = read_png(path)
    except PixToolError as exc:
        return ToolResult.partial(
            {"image_path": str(path), "decode_error": exc.to_dict()},
            output_paths=[str(path)],
            diagnostics=diagnostics,
        ).add_diagnostic(
            "warning",
            "Image was exported but could not be decoded in-process; open the file directly.",
        )

    rows: list[list[list[float]]] = []
    for row_y in range(y, min(y + height, image.height)):
        row: list[list[float]] = []
        for row_x in range(x, min(x + width, image.width)):
            row.append([round(value, 6) for value in image.pixel(row_x, row_y)])
        rows.append(row)

    return ToolResult.success(
        {
            "image_path": str(path),
            "image_size": {"width": image.width, "height": image.height},
            "channels": image.channels,
            "bit_depth": image.depth,
            "region": {"x": x, "y": y, "width": len(rows[0]) if rows else 0, "height": len(rows)},
            "pixels": rows,
        },
        output_paths=[str(path)],
        diagnostics=diagnostics,
    )


# ==========================================================================
# 4.7 pixel statistics over a texture
# ==========================================================================
@tool(
    name="texture-pixel-stats",
    summary=(
        "Per-channel min/max/mean/stddev over a texture or a sub-rectangle, plus counts of "
        "negative and saturated samples."
    ),
    category="textures",
    parameters=with_session(
        resource_id={"type": "integer", "description": "Resource id to analyse."},
        global_id={"type": "integer", "description": "Event whose contents to analyse."},
        x={"type": "integer", "description": "Region left edge."},
        y={"type": "integer", "description": "Region top edge."},
        width={"type": "integer", "description": "Region width. Default whole image."},
        height={"type": "integer", "description": "Region height. Default whole image."},
        depth={"type": "boolean", "description": "Analyse the depth buffer."},
        rtv={"type": "integer", "description": "Render target slot. Default 0."},
        output={"type": "string", "description": "Where to keep the intermediate PNG."},
    ),
    returns="Per-channel statistics with sample counts.",
    examples=["pix-tool-set texture-pixel-stats --resource-id 641"],
    notes=_PIXEL_NOTE,
)
def texture_pixel_stats(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    resource_id = args.get("resource_id")
    global_id = args.get("global_id")
    if resource_id is None and global_id is None:
        raise invalid_argument("resource_id/global_id", "provide at least one")

    path, diagnostics = _texture_export(
        context,
        args,
        capture,
        resource_id=int(resource_id) if resource_id is not None else None,
        global_id=int(global_id) if global_id is not None else None,
        rtv=int(args.get("rtv") or 0),
        depth=bool(args.get("depth")),
        stem=f"stats_{resource_id if resource_id is not None else global_id}",
    )

    try:
        image = read_png(path)
    except PixToolError as exc:
        return ToolResult.partial(
            {"image_path": str(path), "decode_error": exc.to_dict()},
            output_paths=[str(path)],
            diagnostics=diagnostics,
        ).add_diagnostic("warning", "Image exported but not decodable in-process.")

    box = None
    if any(args.get(key) is not None for key in ("x", "y", "width", "height")):
        x = int(args.get("x") or 0)
        y = int(args.get("y") or 0)
        width = int(args.get("width") or image.width)
        height = int(args.get("height") or image.height)
        box = (x, y, x + width, y + height)

    stats = _channel_statistics(image, box)
    return ToolResult.success(
        {
            "image_path": str(path),
            "image_size": {"width": image.width, "height": image.height},
            "region": {"x": box[0], "y": box[1], "width": box[2] - box[0], "height": box[3] - box[1]}
            if box
            else None,
            **stats,
        },
        output_paths=[str(path)],
        diagnostics=diagnostics,
    )


# ==========================================================================
# 4.8 pick a single pixel
# ==========================================================================
@tool(
    name="pick-pixel",
    summary="Read one pixel and report its channel values, hex colour and luminance.",
    category="textures",
    parameters=with_session(
        x={"type": "integer", "description": "Pixel X coordinate."},
        y={"type": "integer", "description": "Pixel Y coordinate."},
        resource_id={"type": "integer", "description": "Resource id to sample."},
        global_id={"type": "integer", "description": "Event whose contents to sample."},
        depth={"type": "boolean", "description": "Sample the depth buffer."},
        rtv={"type": "integer", "description": "Render target slot. Default 0."},
        output={"type": "string", "description": "Where to keep the intermediate PNG."},
        required=["x", "y"],
    ),
    returns="Channel values, 8-bit hex colour and relative luminance.",
    examples=["pix-tool-set pick-pixel --resource-id 641 --x 960 --y 540"],
    notes=_PIXEL_NOTE,
)
def pick_pixel(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    resource_id = args.get("resource_id")
    global_id = args.get("global_id")
    if resource_id is None and global_id is None:
        raise invalid_argument("resource_id/global_id", "provide at least one")

    path, diagnostics = _texture_export(
        context,
        args,
        capture,
        resource_id=int(resource_id) if resource_id is not None else None,
        global_id=int(global_id) if global_id is not None else None,
        rtv=int(args.get("rtv") or 0),
        depth=bool(args.get("depth")),
        stem=f"pick_{resource_id if resource_id is not None else global_id}",
    )
    try:
        image = read_png(path)
    except PixToolError as exc:
        return ToolResult.partial(
            {"image_path": str(path), "decode_error": exc.to_dict()},
            output_paths=[str(path)],
            diagnostics=diagnostics,
        ).add_diagnostic("warning", "Image exported but not decodable in-process.")

    x = int(args["x"])
    y = int(args["y"])
    values = image.pixel(x, y)
    eight_bit = [max(0, min(255, int(round(value * 255)))) for value in values]
    hex_colour = "#" + "".join(f"{component:02x}" for component in eight_bit[:4])
    luminance = None
    if len(values) >= 3:
        luminance = round(
            0.2126 * values[0] + 0.7152 * values[1] + 0.0722 * values[2], 6
        )

    return ToolResult.success(
        {
            "image_path": str(path),
            "coordinate": {"x": x, "y": y},
            "image_size": {"width": image.width, "height": image.height},
            "channels": [round(value, 6) for value in values],
            "channels_8bit": eight_bit,
            "hex": hex_colour,
            "luminance": luminance,
        },
        output_paths=[str(path)],
        diagnostics=diagnostics,
    )
