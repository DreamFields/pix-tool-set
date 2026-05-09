"""Export one slice of a Tex2DArray UAV, e.g. RWLightGrid.

Built for a case the existing texture tools could not reach. The dispatch's
recorded UAV table base is stale (root[1] points at heap 32 index 134140, which
holds buffer views, while RWLightGrid's descriptor is at 134034), so resolving the
resource through the table fails. This tool therefore accepts a resource id
directly, and can also match a UAV by the name the shader declares.

Slice bounds are enforced rather than clamped: asking for a slice a resource does
not have is a mistake worth reporting, not something to silently substitute.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

from ..context import ToolContext
from ..engine import footprint as fp
from ..engine.model import ViewKind
from ..errors import PixToolError, invalid_argument, not_found
from ..results import ToolResult
from ._common import tool, with_session

_NOTE = (
    "Reads bytes recorded in resources.bin and slices them with the subresource "
    "footprints the export declares. A UAV that the GPU writes during the frame is not "
    "re-uploaded, so what comes back is the resource's initial content unless a CPU "
    "write covered it; the report says which. Slice indices are validated against the "
    "resource's array size."
)


def _find_by_name(capture, draw, name: str):
    """Resolve a declared UAV name to the resource behind it.

    Uses the shader's declaration for the expected dimension, then looks for the one
    resource in the frame whose UAV views match it. Necessary because the dispatch's
    descriptor table base is not reliable here.
    """
    wanted = name.strip().lower()
    declared = None
    for shader in draw.shaders:
        for record in shader.resource_bindings:
            if (record.get("name") or "").lower() != wanted:
                continue
            declared = record
            break
        if declared:
            break
    if declared is None:
        return None, None

    dimension = (declared.get("dimension") or "").lower()
    expect_array = "2darray" in dimension
    candidates: dict[int, Any] = {}
    for (_, _), view in capture.views.items():
        if view.kind is not ViewKind.UAV or view.resource_id is None:
            continue
        view_dim = (view.dimension or "").upper()
        if expect_array and "TEXTURE2DARRAY" not in view_dim:
            continue
        if not expect_array and "TEXTURE2DARRAY" in view_dim:
            continue
        resource = capture.resource(view.resource_id)
        if resource is None:
            continue
        candidates[view.resource_id] = resource

    if expect_array:
        # A light-grid style array has more than one slice; plain render targets
        # exported as 2darray views have exactly one.
        multi = {
            rid: resource
            for rid, resource in candidates.items()
            if resource.depth_or_array_size > 1
        }
        if multi:
            candidates = multi

    # Narrow by the resolution the pass's own parameters imply. A grid shader
    # carries its extent in a cbuffer field, and matching against it is far more
    # reliable than picking from every array UAV in the frame.
    hint = _resolution_hint(capture, draw)
    if hint and len(candidates) > 1:
        sized = {
            rid: resource
            for rid, resource in candidates.items()
            if resource.width == hint and resource.height == hint
        }
        if sized:
            candidates = sized

    if len(candidates) == 1:
        rid, resource = next(iter(candidates.items()))
        return rid, resource
    return None, sorted(candidates)


def _resolution_hint(capture, draw) -> int | None:
    """A square extent declared in the pass's cbuffer, when one is present.

    Looks for a field whose name ends in Resolution, which is how UE5 states a grid's
    size. Returns None rather than guessing when no such field exists.
    """
    for shader in draw.shaders:
        for cbuffer in shader.constant_buffers:
            for field in cbuffer.get("fields") or []:
                name = (field.get("name") or "").lower()
                if name.endswith("resolution") and field.get("offset") is not None:
                    value = _read_uint_field(capture, draw, cbuffer, field)
                    if value:
                        return value
    return None


def _read_uint_field(capture, draw, cbuffer: dict, field: dict) -> int | None:
    """Decode one uint field of a cbuffer bound at this draw."""
    from ..engine import cbvmatch
    from ..engine.model import RootParameterKind

    registers = cbvmatch.root_cbv_registers(capture, draw)
    layouts = cbvmatch.collect_cbuffer_layouts(draw)
    target = next(
        (layout for layout in layouts if layout.get("name") == cbuffer.get("name")),
        None,
    )
    if target is None:
        return None
    wanted = target.get("shader_register")
    for binding in draw.bindings:
        if binding.kind is not RootParameterKind.CBV or binding.resource_id is None:
            continue
        info = registers.get(binding.root_index)
        if info is not None and wanted is not None and info[0] != wanted:
            continue
        try:
            blob = capture.read_resource_bytes(
                binding.resource_id, offset=binding.va_offset or 0, length=512
            )
        except PixToolError:
            continue
        offset = int(field["offset"])
        if offset + 4 > len(blob):
            continue
        return struct.unpack_from("<I", blob, offset)[0]
    return None


@tool(
    name="export-uav-slice",
    summary=(
        "Export one array slice of a texture UAV from the capture's recorded bytes, "
        "with slice bounds validated against the resource."
    ),
    category="textures",
    parameters=with_session(
        resource_id={"type": "integer", "description": "Texture resource id."},
        name={
            "type": "string",
            "description": (
                "Declared UAV name to resolve instead, e.g. RWLightGrid. Needs a pass "
                "selector so the shader's declaration can be read."
            ),
        },
        global_id={
            "type": "integer",
            "description": (
                "PIX Global ID of the pass's event. Unique across every queue, so this is "
                "the selector for an id copied out of the PIX GUI."
            ),
        },
        queue_id={
            "type": "integer",
            "description": (
                "Exported event list 'Queue ID' of the pass. Present on every row of that "
                "export, which covers a single command queue; use global_id or draw_index "
                "for a pass outside it."
            ),
        },
        draw_index={"type": "integer", "description": "Draw index of the pass."},
        slice={
            "type": "integer",
            "description": "Array slice to export. Default 0.",
        },
        mip={
            "type": "integer",
            "description": (
                "Mip level to export. Default 0. A texture's mips are separate "
                "subresources, so a mip-chain pass needs this to reach anything but "
                "the top level."
            ),
        },
        output={"type": "string", "description": "Directory for the .bin and .png."},
        png={
            "type": "boolean",
            "description": "Also write a contrast-stretched greyscale PNG. Default true.",
        },
        pixels={
            "type": "integer",
            "description": "Return this many leading pixel values. Default 0.",
        },
    ),
    returns="Slice footprint, value distribution, and written file paths.",
    examples=[
        "pix-tool-set export-uav-slice --queue-id 18461 --name RWLightGrid --slice 2",
        "pix-tool-set export-uav-slice --global-id 5312 --name RWLightGrid --slice 2",
        "pix-tool-set export-uav-slice --resource-id 824 --slice 0 --output G:\\out",
    ],
    notes=_NOTE,
)
def export_uav_slice(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)

    resource_id = args.get("resource_id")
    resource = None
    resolved_by = "resource_id"

    if resource_id is None and args.get("name"):
        draw = capture.resolve_draw(
            draw_index=args.get("draw_index"),
            global_id=args.get("global_id"),
            queue_id=args.get("queue_id"),
        )
        if draw is None:
            raise invalid_argument(
                "global_id/queue_id/draw_index",
                "resolving a UAV by name needs a pass selector too",
            )
        found, detail = _find_by_name(capture, draw, str(args["name"]))
        if found is None:
            raise not_found(
                "UAV",
                str(args["name"]),
                (
                    f"Could not narrow it to one resource; candidates: {detail}. "
                    "Pass --resource-id."
                    if detail
                    else "The shader declares no UAV with that name."
                ),
            )
        resource_id = found
        resource = detail
        resolved_by = "name"

    if resource_id is None:
        raise invalid_argument(
            "resource_id/name", "identify the UAV by id, or by name plus a pass selector"
        )
    resource_id = int(resource_id)
    if resource is None:
        resource = capture.resource(resource_id)
    if resource is None:
        raise not_found("resource", resource_id)

    slices = max(resource.depth_or_array_size, 1)
    index = int(args.get("slice") or 0)
    if index < 0 or index >= slices:
        raise invalid_argument(
            "slice",
            f"resource {resource_id} has {slices} slice(s), so valid indices are "
            f"0..{slices - 1}; {index} is out of range.",
        )

    mip_levels = max(resource.mip_levels, 1)
    mip = int(args.get("mip") or 0)
    if mip < 0 or mip >= mip_levels:
        raise invalid_argument(
            "mip",
            f"resource {resource_id} has {mip_levels} mip level(s), so valid values are "
            f"0..{mip_levels - 1}; {mip} is out of range.",
        )

    # D3D12 subresource ordering: mip varies fastest, then array slice. Using the
    # slice index alone (as this tool used to) reaches mip 0 of slice N only, so a
    # mip-chain texture had 9 of its 10 levels unreachable.
    subresource_index = mip + index * mip_levels

    try:
        blob = capture.read_resource_bytes(resource_id)
    except PixToolError as exc:
        result = ToolResult.partial(
            {
                "resource_id": resource_id,
                "resource": resource.to_dict(),
                "resolved_by": resolved_by,
                "slice": index,
                "slice_count": slices,
                "bytes_available": False,
            }
        )
        result.degrade(
            "No recorded bytes for this resource.",
            reason=exc.message,
            alternative="A GPU-written UAV is never uploaded; there may be nothing stored.",
        )
        return result

    footprints = capture.resource_footprints(resource_id)
    data: dict[str, Any] = {
        "resource_id": resource_id,
        "resource": resource.to_dict(),
        "resolved_by": resolved_by,
        "slice": index,
        "slice_count": slices,
        "mip": mip,
        "mip_levels": mip_levels,
        "subresource_index": subresource_index,
        "blob_bytes": len(blob),
        "footprint_count": len(footprints),
        "contents_are": (
            "initial upload recorded at capture time; a UAV written by the GPU during "
            "the frame is not re-uploaded"
        ),
    }

    entry = next(
        (f for f in footprints if f.subresource_index == subresource_index),
        footprints[subresource_index] if subresource_index < len(footprints) else None,
    )
    if entry is None:
        result = ToolResult.partial(data)
        result.degrade(
            f"No subresource footprint was recorded for mip {mip} slice {index} "
            f"(subresource {subresource_index}).",
            reason=(
                f"{len(footprints)} footprint(s) exist for this resource, so the row "
                "pitch for that subresource is unknown."
            ),
        )
        return result

    data["footprint"] = entry.to_dict()
    rows = fp.extract_rows(blob, entry)
    stride = fp.format_stride(entry.format)
    if rows is None or stride is None:
        result = ToolResult.partial(data)
        result.degrade(f"{entry.format} cannot be laid out by this tool.")
        return result

    packed = b"".join(rows)
    data["rows_recovered"] = len(rows)
    data["packed_bytes"] = len(packed)
    data["pixels"] = len(packed) // stride

    if stride == 1:
        histogram: dict[int, int] = {}
        for byte in packed:
            histogram[byte] = histogram.get(byte, 0) + 1
        data["distinct_values"] = len(histogram)
        data["value_histogram"] = sorted(
            histogram.items(), key=lambda item: -item[1]
        )[:8]
        data["min"] = min(packed) if packed else None
        data["max"] = max(packed) if packed else None
        data["nonzero"] = sum(count for value, count in histogram.items() if value)
    elif stride == 4:
        count = min(len(packed) // 4, 400000)
        values = struct.unpack_from(f"<{count}I", packed, 0)
        data["min"] = min(values) if values else None
        data["max"] = max(values) if values else None
        data["nonzero"] = sum(1 for value in values if value)
        data["distinct_values"] = len(set(values))

    want = int(args.get("pixels") or 0)
    if want:
        if stride == 1:
            data["values"] = list(packed[:want])
        elif stride == 4:
            data["values"] = list(
                struct.unpack_from(f"<{min(want, len(packed)//4)}I", packed, 0)
            )

    # State plainly where the bytes came from. A UAV the GPU fills is not re-uploaded,
    # so matching content does not prove it is the dispatch's output.
    sources = capture.resource_data_sources(resource_id)
    data["data_sources"] = sources
    data["cpu_written"] = bool(sources.get("cpu_page_writes"))
    data["provenance"] = (
        "recorded CPU write during the frame"
        if sources.get("cpu_page_writes")
        else "initial upload at capture time; the dispatch's own writes are not stored"
    )

    output = args.get("output")
    if output:
        directory = Path(str(output))
        directory.mkdir(parents=True, exist_ok=True)
        stem = (
            f"resource{resource_id}_slice{index}_mip{mip}_"
            f"{entry.width}x{entry.height}_"
            f"{entry.format.replace('DXGI_FORMAT_', '')}"
        )
        raw_path = directory / f"{stem}.bin"
        raw_path.write_bytes(packed)
        written = [
            {
                "path": str(raw_path),
                "bytes": len(packed),
                "layout": "tightly packed rows, pitch padding removed",
            }
        ]

        want_png = args.get("png")
        if want_png is None or bool(want_png):
            from .resource_texture_tools import _encode_png, _to_greyscale

            grey = _to_greyscale(rows, entry, stride)
            if grey is not None:
                pixels, low, high = grey
                png_path = directory / f"{stem}.png"
                png_path.write_bytes(_encode_png(pixels, entry.width, len(rows)))
                written.append(
                    {
                        "path": str(png_path),
                        "bytes": png_path.stat().st_size,
                        "layout": "8-bit greyscale, contrast stretched",
                        "stretched_from": low,
                        "stretched_to": high,
                    }
                )
        data["files"] = written

    if data.get("nonzero") == 0:
        result = ToolResult.partial(data)
        result.degrade(
            f"Slice {index} is entirely zero in the recorded bytes.",
            reason=(
                "This UAV is filled by the dispatch on the GPU, and resources.bin only "
                "holds uploads and CPU writes, so the written values are not present."
            ),
            alternative=(
                "The values can be reasoned about from the shader and its parameters, or "
                "inspected in the PIX GUI which reads the post-dispatch state."
            ),
        )
        return result
    return ToolResult.success(
        data, output_paths=[entry["path"] for entry in data.get("files", [])]
    )
