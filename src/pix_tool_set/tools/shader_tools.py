"""Requirement section 5: shader analysis."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from ..context import ToolContext
from ..engine.model import ShaderStage
from ..errors import PixToolError, invalid_argument, not_found
from ..results import ToolResult
from ._common import (
    DRAW_SELECTOR,
    PAGE_PARAMS,
    page_args,
    page_envelope,
    resolve_draw,
    tool,
    with_session,
)
from ._raytracing_bindings import export_binding_view, raytracing_binding_payload


_STAGES = [stage.value for stage in ShaderStage]

_SOURCE_NOTE = (
    "PIX captures store compiled bytecode. Original HLSL text survives only when the "
    "shader was built with embedded debug info (/Zi /Qembed_debug). When it is absent, "
    "these tools return the DXIL disassembly, which carries the full signature, resource "
    "binding table, entry point and IR. The `has_embedded_source` flag tells you which "
    "case you are in."
)

_SHADER_SELECTOR: dict[str, Any] = {
    "pso_id": {"type": "integer", "description": "Pipeline state that owns the shader."},
    "stage": {"type": "string", "enum": _STAGES, "description": "Shader stage to select."},
    "shader_hash": {"type": "string", "description": "Shader hash or PDB debug name."},
    **DRAW_SELECTOR,
}


def _resolve_shader(capture, args: dict[str, Any]):
    # A pso_id / stage / shader_hash lookup does not go through a draw at all, so
    # the queue qualifiers only constrain the draw-based path; find_shader resolves
    # that draw itself and is handed them there. _SHADER_SELECTOR splices in the
    # whole DRAW_SELECTOR, so dropping them here would advertise a restriction the
    # tool silently ignores.
    shader = capture.find_shader(
        pso_id=args.get("pso_id"),
        stage=args.get("stage"),
        shader_hash=args.get("shader_hash"),
        draw_index=args.get("draw_index"),
        queue_id=args.get("queue_id"),
        queue_name=args.get("queue_name"),
        queue_object_id=args.get("queue_object_id"),
    )
    if shader is None:
        raise not_found(
            "shader",
            args.get("shader_hash") or args.get("pso_id") or args.get("draw_index"),
            "Use list-shaders to find a pso_id + stage pair, or pass --draw-index.",
        )
    return shader


@tool(
    name="shader-stats",
    summary=(
        "Shader inventory: counts per stage, unique shaders after de-duplication by hash, "
        "size distribution, and how many draws each stage serves."
    ),
    category="shaders",
    parameters=with_session(
        used_only={"type": "boolean", "description": "Only count shaders bound by a draw."},
        top={"type": "integer", "description": "How many largest shaders to list. Default 10."},
    ),
    returns="Per-stage counts, byte totals and the largest shaders.",
    examples=["pix-tool-set shader-stats", "pix-tool-set shader-stats --used-only"],
    notes=_SOURCE_NOTE,
)
def shader_stats(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    used_only = bool(args.get("used_only"))
    shaders, _total = capture.find_shaders(used_only=used_only)

    by_stage: dict[str, dict[str, Any]] = {}
    for shader in shaders:
        entry = by_stage.setdefault(
            shader.stage.value,
            {"stage": shader.stage.value, "count": 0, "unique": set(), "bytes": 0},
        )
        entry["count"] += 1
        entry["unique"].add(shader.key)
        entry["bytes"] += shader.byte_size
    stage_rows = []
    for entry in by_stage.values():
        stage_rows.append(
            {
                "stage": entry["stage"],
                "count": entry["count"],
                "unique_count": len(entry["unique"]),
                "total_bytes": entry["bytes"],
            }
        )
    stage_rows.sort(key=lambda row: -row["count"])

    draw_stage_counter: Counter[str] = Counter()
    for draw in capture.draw_calls:
        for shader in draw.shaders:
            draw_stage_counter[shader.stage.value] += 1

    top_count = int(args.get("top") or 10)
    largest = sorted(shaders, key=lambda s: -s.byte_size)[:top_count]

    result = ToolResult.success(
        {
            "totals": {
                "shaders": len(shaders),
                "unique_shaders": len({s.key for s in shaders}),
                "total_bytes": sum(s.byte_size for s in shaders),
                "pipeline_states": len(capture.pipeline_states),
            },
            "by_stage": stage_rows,
            "stage_bindings_per_draw": dict(draw_stage_counter),
            "largest": [s.to_dict() for s in largest],
            "capabilities": {"disassembly_available": capture.disassembly_available},
        }
    )
    if not capture.disassembly_available:
        result.degrade(
            "dxcompiler.dll is unavailable, so disassembly-derived fields stay empty.",
            reason=capture.disassembly_unavailable_reason,
        )
    return result


@tool(
    name="list-shaders",
    summary="List shaders with stage, owning PSO, size, hash and PDB debug name.",
    category="shaders",
    parameters=with_session(
        PAGE_PARAMS,
        stage={"type": "string", "enum": _STAGES, "description": "Restrict to one stage."},
        name={"type": "string", "description": "Substring match on hash or debug name."},
        used_only={"type": "boolean", "description": "Only shaders bound by a draw."},
        unique={"type": "boolean", "description": "Collapse duplicates by stage+hash."},
    ),
    returns="Paged shader list.",
    examples=[
        "pix-tool-set list-shaders --stage CS --unique --limit 30",
    ],
)
def list_shaders(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    offset, limit = page_args(args)
    window, total = capture.find_shaders(
        stage=args.get("stage"),
        name=args.get("name"),
        used_only=bool(args.get("used_only")),
        unique=bool(args.get("unique")),
        offset=offset,
        limit=limit,
    )
    return ToolResult.success(
        {
            "shaders": [shader.to_dict() for shader in window],
            **page_envelope(total, offset, limit, len(window)),
        }
    )


@tool(
    name="shader-info",
    summary=(
        "Detail for one shader: stage, size, hash, DXBC chunk inventory, entry point, "
        "thread group size and which draws use it."
    ),
    category="shaders",
    parameters=with_session(
        _SHADER_SELECTOR,
        max_draws={"type": "integer", "description": "Cap on listed consumer draws. Default 10."},
    ),
    returns="Shader metadata plus consumer draw list.",
    examples=[
        "pix-tool-set shader-info --pso-id 3184 --stage PS",
        "pix-tool-set shader-info --draw-index 2461 --stage VS",
    ],
    notes=_SOURCE_NOTE,
)
def shader_info(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    shader = _resolve_shader(capture, args)
    max_draws = int(args.get("max_draws") or 10)

    consumers = [
        {
            "draw_index": draw.index,
            "global_id": draw.global_id,
            "api": draw.api,
            "pass_name": draw.pass_name,
        }
        for draw in capture.draw_calls
        if draw.pso_id == shader.pso_id
    ]

    # D1: sibling PSOs — every PSO that references the same (stage, shader_hash).
    # This is the list the caller needs to decide whether --scope shader is needed
    # before patching. When it's non-empty and >1, the shader is shared and patching
    # only the selected PSO would be a partial change.
    stage_val = shader.stage.value
    shader_hash = shader.shader_hash or shader.hash_md5
    sibling_psos = capture.sibling_psos(stage_val, shader_hash)
    has_siblings = len(sibling_psos) > 1

    data = {
        "shader": shader.to_dict(detail=True),
        "pipeline_state": (
            capture.pipeline_states[shader.pso_id].to_dict()
            if shader.pso_id in capture.pipeline_states
            else None
        ),
        "consumers": consumers[:max_draws],
        "consumer_count": len(consumers),
        "has_embedded_source": shader.has_embedded_source,
        "sibling_psos": sibling_psos,
        "patch_scope_warning": (
            f"Shader {stage_val}:{shader_hash} is used by {len(sibling_psos)} PSOs. "
            f"Patching only pso {shader.pso_id} would leave the other "
            f"{len(sibling_psos) - 1} unchanged. Pass --scope shader to "
            f"shader-edit-apply to patch all {len(sibling_psos)} PSOs, or --scope pso "
            f"to explicitly patch only this one."
            if has_siblings
            else None
        ),
    }
    result = ToolResult.success(data)
    if not capture.disassembly_available:
        result.degrade("Disassembly unavailable; entry point and thread size are unknown.")
    if has_siblings:
        result.add_diagnostic(
            "warning",
            f"Shader {stage_val}:{shader_hash} is shared by {len(sibling_psos)} PSOs. "
            "The default 'auto' scope in shader-edit-apply will refuse to patch until you "
            "choose --scope shader or --scope pso explicitly.",
        )
    return result


@tool(
    name="disassemble-shader",
    summary=(
        "Disassemble a shader to DXIL/DXBC text. Optionally write it to a file and/or "
        "dump the raw bytecode."
    ),
    category="shaders",
    parameters=with_session(
        _SHADER_SELECTOR,
        output={"type": "string", "description": "Write the disassembly text here."},
        bytecode_output={"type": "string", "description": "Write the raw .dxbc blob here."},
        max_lines={
            "type": "integer",
            "description": "Trim the inline text to this many lines. Default 400; 0 means no limit.",
        },
        prefer_source={
            "type": "boolean",
            "description": "Return embedded HLSL when the shader carries it.",
        },
    ),
    returns="Disassembly text (possibly trimmed) and any written file paths.",
    examples=[
        "pix-tool-set disassemble-shader --pso-id 2972 --stage CS",
        "pix-tool-set disassemble-shader --draw-index 2461 --stage PS -o ps.txt --max-lines 0",
    ],
    notes=_SOURCE_NOTE,
    aliases=["shader-source"],
)
def disassemble_shader(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    shader = _resolve_shader(capture, args)

    prefer_source = bool(args.get("prefer_source"))
    embedded = shader.embedded_source if prefer_source else ""
    text = embedded or shader.disassembly
    kind = "hlsl" if embedded else "dxil-disassembly"

    output_paths: list[str] = []
    if args.get("output") and text:
        path = context.resolve_output(args.get("output"), f"{shader.key.replace(':', '_')}.txt")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", errors="replace")
        output_paths.append(str(path))
    if args.get("bytecode_output"):
        blob = shader.bytecode
        if blob:
            path = context.resolve_output(
                args.get("bytecode_output"), f"{shader.key.replace(':', '_')}.dxbc"
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(blob)
            output_paths.append(str(path))

    max_lines = args.get("max_lines")
    max_lines = 400 if max_lines is None else int(max_lines)
    lines = text.splitlines()
    truncated = bool(max_lines) and len(lines) > max_lines
    inline = "\n".join(lines[:max_lines]) if truncated else text

    data = {
        "shader": shader.to_dict(),
        "content_kind": kind,
        "line_count": len(lines),
        "truncated": truncated,
        "text": inline,
    }
    if not text:
        result = ToolResult.partial(data, output_paths=output_paths)
        result.degrade(
            "No disassembly could be produced for this shader.",
            reason=capture.disassembly_unavailable_reason or "empty bytecode",
        )
        return result
    result = ToolResult.success(data, output_paths=output_paths)
    if truncated:
        result.add_diagnostic(
            "info",
            f"Inline text trimmed to {max_lines} of {len(lines)} lines; pass --max-lines 0 or -o to get everything.",
        )
    if prefer_source and not embedded:
        result.degrade(
            "This shader has no embedded HLSL, so the DXIL disassembly is returned instead."
        )
    return result


@tool(
    name="shader-reflection",
    summary=(
        "Reflection data for a shader: input and output signature elements, declared "
        "resource bindings, thread group size and pipeline validation info."
    ),
    category="shaders",
    parameters=with_session(_SHADER_SELECTOR),
    returns="Signatures, resource declarations and metadata.",
    examples=["pix-tool-set shader-reflection --pso-id 3184 --stage VS"],
    notes=_SOURCE_NOTE,
)
def shader_reflection(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    shader = _resolve_shader(capture, args)

    inputs = [element.to_dict() for element in shader.input_signature]
    outputs = [element.to_dict() for element in shader.output_signature]
    bindings = shader.resource_bindings

    data = {
        "shader": shader.to_dict(detail=True),
        "input_signature": inputs,
        "output_signature": outputs,
        "resource_bindings": bindings,
        "constant_buffers": shader.constant_buffers,
        "metadata": shader.metadata,
        "counts": {
            "inputs": len(inputs),
            "outputs": len(outputs),
            "declared_resources": len(bindings),
        },
    }
    result = ToolResult.success(data)
    if not bindings and not inputs:
        result.degrade(
            "Reflection came back empty; the shader may be a stub or disassembly is unavailable.",
            reason=capture.disassembly_unavailable_reason,
        )
    return result


def _export_binding_view(capture, export) -> dict[str, Any]:
    """Kept as a thin alias: Doc/dxr-raytracing-test-cases.md cites this name as the
    step that reproduces the PIX RayGen record panel ordering. The implementation now
    lives in ``_raytracing_bindings`` so ``pass-bindings`` shares it verbatim.
    """
    return export_binding_view(capture, export)



def _raytracing_bindings(capture, draw, args: dict[str, Any]) -> ToolResult:
    """What a ray dispatch has bound, at whatever precision the export supports.

    The payload itself is built in ``_raytracing_bindings`` (the module), because
    ``pass-bindings`` needs the identical view and used to lack it entirely -- it ran the
    rasterisation path on ray dispatches and reported "State objects are not yet
    modelled" while this function was already resolving them. Sharing the builder is what
    keeps the two from drifting apart again.
    """
    payload, degradations = raytracing_binding_payload(
        capture, draw, max_views=int(args.get("max_views") or 128)
    )

    data: dict[str, Any] = {
        "draw_index": draw.index,
        "global_id": draw.global_id,
        "queue_id": draw.queue_id,
        "pass_name": draw.pass_name,
        "pso_id": None,
        "effective_kind": draw.effective_kind.value,
        "descriptor_heap_ids": draw.descriptor_heap_ids,
    }
    data.update(payload)

    result = ToolResult.success(data)
    for message, reason, extra in degradations:
        result.degrade(message, reason=reason, **extra)
    return result



@tool(
    name="shader-bindings",

    summary=(
        "What a shader actually has bound at a given draw: the declared HLSL registers "
        "matched against the concrete resources the root signature supplies."
    ),
    category="shaders",
    parameters=with_session(
        DRAW_SELECTOR,
        stage={"type": "string", "enum": _STAGES, "description": "Shader stage to inspect."},
        max_views={
            "type": "integer",
            "description": (
                "Cap on views listed per descriptor table. Default 128, which covers UE5's "
                "64-entry SRV tables; lower it only to shrink the response."
            ),
        },
    ),
    returns="Declared registers, root parameter mapping and resolved resources.",
    examples=["pix-tool-set shader-bindings --draw-index 2461 --stage PS"],
)
def shader_bindings(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    draw = resolve_draw(capture, args)

    stage = args.get("stage")
    shaders = [draw.shader(stage)] if stage else draw.shaders
    shaders = [shader for shader in shaders if shader is not None]
    if not shaders:
        # A raytracing state object is not a PSO, so this is the path a ray
        # dispatch takes. Answering "no shader" would hide the one fact the caller
        # needs. How much can be said depends on what resolved, and the three
        # levels are kept apart because they call for different next steps.
        if draw.state_object_id is not None:
            return _raytracing_bindings(capture, draw, args)
        raise not_found("shader", stage or "any", "This draw has no shader for that stage.")


    max_views = int(args.get("max_views") or 128)
    signature = capture.root_signatures.get(draw.root_signature_id or -1)

    stage_rows: list[dict[str, Any]] = []
    for shader in shaders:
        declared = shader.resource_bindings
        stage_rows.append(
            {
                "stage": shader.stage.value,
                "shader": shader.to_dict(),
                "declared_registers": declared,
                "declared_count": len(declared),
            }
        )

    binding_rows: list[dict[str, Any]] = []
    for binding in draw.bindings:
        row = binding.to_dict(max_views=max_views)
        parameter = signature.parameter(binding.root_index) if signature else None
        if parameter is not None:
            row["root_parameter"] = parameter.to_dict()
        resolved = []
        for view in binding.resolved_views[:max_views]:
            entry = view.to_dict()
            resource = (
                capture.resource(view.resource_id) if view.resource_id is not None else None
            )
            if resource is not None:
                entry["resource"] = resource.to_dict()
            resolved.append(entry)
        row["resolved"] = resolved
        binding_rows.append(row)

    return ToolResult.success(
        {
            "draw_index": draw.index,
            "global_id": draw.global_id,
            "pass_name": draw.pass_name,
            "pso_id": draw.pso_id,
            "root_signature": signature.to_dict() if signature else None,
            "stages": stage_rows,
            "root_bindings": binding_rows,
            "descriptor_heap_ids": draw.descriptor_heap_ids,
        }
    )


@tool(
    name="constant-buffer",
    summary=(
        "Constant buffer layout and, when the backing bytes are recoverable, the values "
        "bound at a draw."
    ),
    category="shaders",
    parameters=with_session(
        DRAW_SELECTOR,
        stage={"type": "string", "enum": _STAGES, "description": "Shader stage to inspect."},
        slot={"type": "integer", "description": "cbuffer register index (b#) to focus on."},
        max_bytes={"type": "integer", "description": "Cap on dumped bytes per buffer. Default 512."},
        output={
            "type": "string",
            "description": (
                "Directory to write the cbuffer contents to: a .bin of the raw bytes at "
                "the bound offset plus a .json of the decoded fields. Without it the "
                "values are printed only, which is impractical to diff or archive."
            ),
        },
    ),
    returns=(
        "cbuffer layouts, the root parameter that supplies them, decoded values when the "
        "bytes were captured, and the written file paths when --output is given."
    ),
    examples=[
        "pix-tool-set constant-buffer --draw-index 2461 --stage PS",
        "pix-tool-set constant-buffer --global-id 3163 --stage CS --output G:\\out",
    ],
    notes=(
        "Values come from the captured contents of the buffer the root CBV points at, "
        "read out of resources.bin at the recorded byte offset and decoded against the "
        "field offsets in the shader reflection. PIX only stores data it saw uploaded, so "
        "a buffer produced entirely on the GPU has a layout but no values; that case is "
        "reported as values_available=false rather than guessed."
    ),
)
def constant_buffer(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    draw = resolve_draw(capture, args)

    stage = args.get("stage")
    shaders = [draw.shader(stage)] if stage else draw.shaders
    shaders = [shader for shader in shaders if shader is not None]

    layouts: list[dict[str, Any]] = []
    for shader in shaders:
        for buffer in shader.constant_buffers:
            layouts.append({"stage": shader.stage.value, **buffer})

    wanted_slot = args.get("slot")
    declared: list[dict[str, Any]] = []
    for shader in shaders:
        for entry in shader.resource_bindings:
            if entry.get("type") != "cbuffer":
                continue
            if wanted_slot is not None:
                bind = entry.get("hlsl_bind", "")
                if not bind.startswith(f"cb{wanted_slot}"):
                    continue
            declared.append({"stage": shader.stage.value, **entry})

    from ..engine import cbvmatch
    from ..engine import values as values_mod
    from ..engine.model import RootParameterKind

    max_bytes = int(args.get("max_bytes") or 512)
    tagged = cbvmatch.collect_cbuffer_layouts(draw, stage=args.get("stage"))
    root_info = cbvmatch.root_cbv_registers(capture, draw)

    suppliers: list[dict[str, Any]] = []
    decoded_any = False
    for binding in draw.bindings:
        if binding.kind is not RootParameterKind.CBV:
            continue
        entry: dict[str, Any] = {
            "root_index": binding.root_index,
            "resource_id": binding.resource_id,
            "byte_offset": binding.va_offset,
        }
        info = root_info.get(binding.root_index)
        entry["shader_register"] = info[0] if info else None
        entry["visibility_stage"] = info[1] if info else None
        matched, known = cbvmatch.layouts_for_root(
            tagged, root_info, binding.root_index
        )
        entry["register_matched"] = known
        resource = (
            capture.resource(binding.resource_id) if binding.resource_id is not None else None
        )
        if resource is not None:
            entry["resource"] = resource.to_dict()

        # Read the bytes the root CBV points at and decode them against the layout.
        if binding.resource_id is not None:
            sources = capture.resource_data_sources(binding.resource_id)
            page = binding.va_offset // 4096
            status = capture.resource_page_status(binding.resource_id, page)
            entry["data_sources"] = sources
            entry["page"] = page
            entry["page_rewritten_during_frame"] = status["rewritten"]
            entry["page_patches_applied"] = status.get("patches_applied", 0)
            stale = status["rewritten"] and not status["patched"]
            try:
                blob = capture.read_resource_bytes(
                    binding.resource_id,
                    offset=binding.va_offset,
                    length=max(max_bytes, 4096),
                )
            except PixToolError as exc:
                entry["values_available"] = False
                entry["values_detail"] = exc.message
            else:
                entry["bytes_read"] = len(blob)
                entry["hexdump"] = values_mod.hexdump(blob, limit=min(max_bytes, 256))
                # Kept out of the JSON payload; only used to write the .bin below.
                entry["_blob"] = blob
                decoded_blocks = []
                for layout in matched:
                    fields = layout.get("fields") or []
                    if not fields:
                        continue
                    decoded_blocks.append(
                        {
                            "cbuffer": layout.get("name"),
                            "stage": layout.get("stage"),
                            "shader_register": layout.get("shader_register"),
                            "declared_size": layout.get("size"),
                            "fields": values_mod.decode_layout(blob, fields),
                        }
                    )
                if decoded_blocks:
                    entry["decoded"] = decoded_blocks
                if stale:
                    # The frame rewrote this page but the patch blob could not be
                    # decoded, so the bytes on hand predate the frame.
                    entry["values_available"] = False
                    entry["values_are_stale"] = True
                    entry["values_detail"] = (
                        f"page {page} of resource {binding.resource_id} is rewritten from "
                        "the CPU during the frame and that patch could not be decoded; "
                        "the values below predate the frame"
                    )
                else:
                    entry["values_available"] = True
                    if status["rewritten"]:
                        entry["values_note"] = (
                            f"page {page} was rewritten from the CPU during the frame; "
                            f"{status['patches_applied']} patch write(s) applied, so these "
                            "are the values the shader read"
                        )
                    decoded_any = decoded_any or bool(decoded_blocks)
        suppliers.append(entry)

    # Write the contents out when asked. Done after the loop so one directory holds
    # every supplier of this draw, named by root index so two CBVs cannot collide.
    written: list[dict[str, Any]] = []
    output = args.get("output")
    if output:
        from pathlib import Path
        import json as _json

        directory = Path(str(output))
        directory.mkdir(parents=True, exist_ok=True)
        for entry in suppliers:
            blob = entry.get("_blob")
            if not blob:
                continue
            # Trim to the size the shader declares rather than dumping the whole
            # 4096-byte page that was read. A 64-byte cbuffer padded out to a page
            # buries the real values in unrelated bytes that belong to other
            # allocations in the same suballocated buffer.
            declared_size = 0
            for block in entry.get("decoded") or []:
                if block.get("declared_size"):
                    declared_size = max(declared_size, int(block["declared_size"]))
            payload_bytes = blob[:declared_size] if declared_size else blob
            stem = (
                f"cbv_gid{draw.global_id}_root{entry['root_index']}"
                f"_res{entry.get('resource_id')}"
            )
            bin_path = directory / f"{stem}.bin"
            bin_path.write_bytes(payload_bytes)
            files: dict[str, Any] = {
                "root_index": entry["root_index"],
                "resource_id": entry.get("resource_id"),
                "bin_path": str(bin_path),
                "bytes": len(payload_bytes),
                "byte_offset": entry.get("byte_offset"),
                "trimmed_to_declared_size": bool(declared_size),
            }
            if entry.get("decoded"):
                json_path = directory / f"{stem}.json"
                json_path.write_text(
                    _json.dumps(
                        {
                            "global_id": draw.global_id,
                            "draw_index": draw.index,
                            "root_index": entry["root_index"],
                            "resource_id": entry.get("resource_id"),
                            "byte_offset": entry.get("byte_offset"),
                            "values_available": entry.get("values_available"),
                            "cbuffers": entry["decoded"],
                        },
                        indent=2,
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                files["json_path"] = str(json_path)
            written.append(files)

    # The raw bytes are not JSON-serialisable and would bloat the payload anyway.
    for entry in suppliers:
        entry.pop("_blob", None)

    data: dict[str, Any] = {
        "draw_index": draw.index,
        "global_id": draw.global_id,
        "stages": [shader.stage.value for shader in shaders],
        "declared_cbuffers": declared,
        "layouts": layouts,
        "root_cbv_suppliers": suppliers,
        "values_available": decoded_any,
    }
    if output:
        data["files"] = written

    result = ToolResult.success(
        data,
        output_paths=[item["bin_path"] for item in written],
    )
    stale = [s for s in suppliers if s.get("values_are_stale")]
    if not layouts:
        result.degrade(
            "No cbuffer layout could be recovered; disassembly may be unavailable.",
            reason=capture.disassembly_unavailable_reason,
        )
    elif stale:
        result.degrade(
            "Layout and bytes were recovered, but this buffer's page is rewritten from the "
            "CPU during the frame, so the decoded values are the pre-frame upload rather "
            "than what the shader read.",
            resource_ids=[s["resource_id"] for s in stale],
            reason=(
                "PIX records those writes as separate patch blobs in resources.bin whose "
                "stream position is not yet resolved."
            ),
        )
    elif not decoded_any:
        result.degrade(
            "Layout is known but the backing bytes were not captured for this buffer.",
            hint=(
                "PIX stores contents it observed being uploaded; buffers written only on "
                "the GPU have no recorded bytes."
            ),
        )
    return result