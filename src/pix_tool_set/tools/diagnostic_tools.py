"""Requirement section 12: diagnostics.

Each diagnostic returns a list of findings with a severity, the exact object that
triggered it, and an actionable recommendation, so an AI client can act without
further interpretation.
"""

from __future__ import annotations

import re
from typing import Any

from ..context import ToolContext
from ..engine.model import EventKind, ShaderStage, format_bits_per_pixel
from ..results import ToolResult
from ._common import PAGE_PARAMS, page_args, page_envelope, tool, with_session

_SIGNED_FORMATS = re.compile(r"_(SNORM|SINT|FLOAT)$")
_UNSIGNED_FORMATS = re.compile(r"_(UNORM|UINT)$")
_LOW_PRECISION = re.compile(r"(R8|R16|B5G6R5|B5G5R5|R11G11B10|R10G10B10)")


def _finding(
    severity: str, topic: str, message: str, recommendation: str, **details: Any
) -> dict[str, Any]:
    entry = {
        "severity": severity,
        "topic": topic,
        "message": message,
        "recommendation": recommendation,
    }
    entry.update(details)
    return entry


def _envelope(findings: list[dict[str, Any]], args: dict[str, Any], **extra: Any) -> ToolResult:
    offset, limit = page_args(args)
    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda item: (order.get(item["severity"], 3), item["topic"]))
    window = findings[offset : offset + limit] if limit else findings[offset:]
    counts = {"error": 0, "warning": 0, "info": 0}
    for item in findings:
        counts[item["severity"]] = counts.get(item["severity"], 0) + 1
    payload = {
        "findings": window,
        "severity_counts": counts,
        "clean": not findings,
        **extra,
        **page_envelope(len(findings), offset, limit, len(window)),
    }
    result = ToolResult.success(payload)
    if counts.get("error"):
        result.add_diagnostic("warning", f"{counts['error']} error-level finding(s) reported.")
    return result


@tool(
    name="diagnose-negative-values",
    summary=(
        "Find places where negative or out-of-range values can appear: unsigned render "
        "targets fed by signed shader outputs, subtractive blending into UNORM targets, and "
        "depth ranges that fall outside 0..1."
    ),
    category="diagnostics",
    parameters=with_session(PAGE_PARAMS),
    returns="Findings with the draw or resource that triggers each risk.",
    examples=["pix-tool-set diagnose-negative-values"],
)
def diagnose_negative_values(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    findings: list[dict[str, Any]] = []

    for draw in capture.draw_calls:
        if draw.kind is not EventKind.DRAW:
            continue
        pso = draw.pipeline_state
        if pso is None:
            continue
        shader = draw.shader(ShaderStage.PS)
        signed_output = False
        if shader is not None:
            for element in shader.output_signature:
                if element.component_type in ("float", "int", "int16", "float16"):
                    signed_output = True
                    break
        for slot, resource_id in enumerate(draw.render_target_resource_ids):
            resource = capture.resource(resource_id)
            if resource is None:
                continue
            if _UNSIGNED_FORMATS.search(resource.format.upper()) and signed_output:
                findings.append(
                    _finding(
                        "warning",
                        "unsigned_target_signed_output",
                        (
                            f"Draw {draw.index} writes signed shader output into unsigned "
                            f"target {resource_id} ({resource.format})."
                        ),
                        "Clamp the shader output with max(value, 0) or move to a signed/float format.",
                        draw_index=draw.index,
                        global_id=draw.global_id,
                        resource_id=resource_id,
                        rtv_slot=slot,
                        format=resource.format,
                        pass_name=draw.pass_name,
                    )
                )
        if pso.blend_enabled:
            for resource_id in draw.render_target_resource_ids:
                resource = capture.resource(resource_id)
                if resource is not None and "UNORM" in resource.format.upper():
                    findings.append(
                        _finding(
                            "info",
                            "blend_into_unorm",
                            (
                                f"Draw {draw.index} blends into UNORM target {resource_id}; "
                                "subtractive or negative factors clamp silently."
                            ),
                            "Verify the blend equation, or use a float target if negatives are intended.",
                            draw_index=draw.index,
                            resource_id=resource_id,
                            format=resource.format,
                        )
                    )
                    break
        for viewport in draw.viewports:
            min_depth = viewport.get("min_depth", 0.0)
            max_depth = viewport.get("max_depth", 1.0)
            if min_depth < 0.0 or max_depth > 1.0:
                findings.append(
                    _finding(
                        "error",
                        "depth_range_out_of_bounds",
                        f"Draw {draw.index} uses depth range [{min_depth}, {max_depth}].",
                        "D3D12 requires 0 <= MinDepth <= MaxDepth <= 1; fix the viewport setup.",
                        draw_index=draw.index,
                        viewport=viewport,
                    )
                )

    return _envelope(findings, args, checks=["unsigned target vs signed output", "blend into UNORM", "viewport depth range"])


@tool(
    name="diagnose-precision",
    summary=(
        "Flag precision risks: low-bit-depth render targets used for HDR or accumulation, "
        "half-float usage in long dependency chains, and depth formats that may band."
    ),
    category="diagnostics",
    parameters=with_session(PAGE_PARAMS),
    returns="Findings ranked by severity with the affected resource and draws.",
    examples=["pix-tool-set diagnose-precision"],
)
def diagnose_precision(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    findings: list[dict[str, Any]] = []
    usage = capture.resource_usage

    for resource in capture.resources.values():
        if not resource.is_texture:
            continue
        entry = usage.get(resource.api_id)
        if entry is None:
            continue
        fmt = resource.format.upper()
        write_count = len(entry["write_draws"])

        if _LOW_PRECISION.search(fmt) and write_count > 4 and resource.is_render_target:
            findings.append(
                _finding(
                    "warning",
                    "low_precision_accumulation",
                    (
                        f"Resource {resource.api_id} ({resource.format}) is written by "
                        f"{write_count} draws; repeated accumulation at this bit depth can band."
                    ),
                    "Consider R16G16B16A16_FLOAT for accumulation targets.",
                    resource_id=resource.api_id,
                    format=resource.format,
                    write_draw_count=write_count,
                    passes=entry["passes"][:6],
                )
            )
        if "R11G11B10" in fmt and resource.is_render_target:
            findings.append(
                _finding(
                    "info",
                    "no_alpha_channel",
                    f"Resource {resource.api_id} uses R11G11B10_FLOAT, which has no alpha channel.",
                    "Confirm no pass relies on alpha from this target.",
                    resource_id=resource.api_id,
                    format=resource.format,
                )
            )
        if resource.is_depth_stencil and "D16" in fmt:
            findings.append(
                _finding(
                    "warning",
                    "low_precision_depth",
                    f"Depth resource {resource.api_id} uses a 16-bit format.",
                    "Use D32_FLOAT for large depth ranges to avoid z-fighting.",
                    resource_id=resource.api_id,
                    format=resource.format,
                )
            )

    for draw in capture.draw_calls:
        shader = draw.shader(ShaderStage.PS)
        if shader is None:
            continue
        half_outputs = [
            element.to_dict()
            for element in shader.output_signature
            if element.component_type in ("float16", "uint16", "int16")
        ]
        if half_outputs:
            findings.append(
                _finding(
                    "info",
                    "half_precision_output",
                    f"Draw {draw.index} pixel shader emits half-precision outputs.",
                    "Acceptable for colour, risky for positions or accumulations.",
                    draw_index=draw.index,
                    outputs=half_outputs[:4],
                )
            )
            if len(findings) > 200:
                break

    return _envelope(
        findings,
        args,
        checks=["low precision accumulation", "missing alpha", "16-bit depth", "half outputs"],
    )


@tool(
    name="diagnose-reflection-mismatch",
    summary=(
        "Detect mismatches between what shaders declare and what the pipeline supplies: "
        "unbound registers, root signature tables that are too small, and vertex layout "
        "gaps against the vertex shader input signature."
    ),
    category="diagnostics",
    parameters=with_session(
        PAGE_PARAMS,
        max_draws={"type": "integer", "description": "How many draws to inspect. Default 400."},
    ),
    returns="Findings pinpointing the draw, stage and register involved.",
    examples=["pix-tool-set diagnose-reflection-mismatch"],
)
def diagnose_reflection_mismatch(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    findings: list[dict[str, Any]] = []
    max_draws = int(args.get("max_draws") or 400)

    if not capture.disassembly_available:
        result = ToolResult.partial(
            {"findings": [], "severity_counts": {}, "clean": None}
        )
        result.degrade(
            "Reflection checks need shader disassembly, which is unavailable here.",
            reason=capture.disassembly_unavailable_reason,
        )
        return result

    inspected = 0
    for draw in capture.draw_calls:
        if inspected >= max_draws:
            break
        pso = draw.pipeline_state
        if pso is None:
            continue
        inspected += 1
        signature = capture.root_signatures.get(draw.root_signature_id or -1)

        # vertex layout vs VS input signature
        vertex_shader = draw.shader(ShaderStage.VS)
        if vertex_shader is not None and pso.input_layout:
            declared = {
                (element["semantic"].upper(), element["semantic_index"])
                for element in pso.input_layout
            }
            for element in vertex_shader.input_signature:
                key = (element.semantic_name.upper(), element.semantic_index)
                if element.semantic_name and key not in declared:
                    findings.append(
                        _finding(
                            "warning",
                            "vertex_input_missing",
                            (
                                f"Draw {draw.index} vertex shader reads "
                                f"{element.semantic_name}{element.semantic_index}, "
                                "which the input layout does not provide."
                            ),
                            "Add the element to the input layout or stop reading it in the shader.",
                            draw_index=draw.index,
                            semantic=element.semantic_name,
                            semantic_index=element.semantic_index,
                            pso_id=pso.api_id,
                        )
                    )

        # declared registers vs available root parameters
        for shader in draw.shaders:
            declared_bindings = shader.resource_bindings
            if not declared_bindings:
                continue
            srv_needed = sum(1 for entry in declared_bindings if entry["id"].startswith("T"))
            uav_needed = sum(1 for entry in declared_bindings if entry["id"].startswith("U"))
            cbv_needed = sum(1 for entry in declared_bindings if entry["id"].startswith("CB"))

            srv_available = len(draw.srvs)
            uav_available = len(draw.uavs)
            cbv_available = len(draw.cbvs)

            for label, needed, available in (
                ("SRV", srv_needed, srv_available),
                ("UAV", uav_needed, uav_available),
                ("CBV", cbv_needed, cbv_available),
            ):
                if needed > available:
                    findings.append(
                        _finding(
                            "error",
                            "binding_shortfall",
                            (
                                f"Draw {draw.index} stage {shader.stage.value} declares {needed} "
                                f"{label}(s) but only {available} are bound."
                            ),
                            f"Check the root signature table size and the descriptor writes for {label}s.",
                            draw_index=draw.index,
                            stage=shader.stage.value,
                            binding_type=label,
                            declared=needed,
                            bound=available,
                            pso_id=pso.api_id,
                            root_signature_id=draw.root_signature_id,
                        )
                    )

        # root signature table sizes vs resolved views
        if signature is not None:
            for binding in draw.bindings:
                parameter = signature.parameter(binding.root_index)
                if parameter is None:
                    findings.append(
                        _finding(
                            "warning",
                            "root_parameter_unknown",
                            (
                                f"Draw {draw.index} binds root index {binding.root_index}, "
                                "which the root signature does not declare."
                            ),
                            "Verify the root signature matches the one used at capture time.",
                            draw_index=draw.index,
                            root_index=binding.root_index,
                            root_signature_id=signature.api_id,
                        )
                    )
                    continue
                declared_size = signature.table_size(binding.root_index)
                if declared_size > 0 and binding.resolved_views:
                    if len(binding.resolved_views) < declared_size:
                        findings.append(
                            _finding(
                                "info",
                                "descriptor_table_partial",
                                (
                                    f"Draw {draw.index} root {binding.root_index} declares "
                                    f"{declared_size} descriptors but only "
                                    f"{len(binding.resolved_views)} were written."
                                ),
                                "Unwritten descriptors read as undefined; initialise the full range.",
                                draw_index=draw.index,
                                root_index=binding.root_index,
                                declared=declared_size,
                                resolved=len(binding.resolved_views),
                            )
                        )
        if len(findings) > 400:
            break

    return _envelope(
        findings,
        args,
        inspected_draws=inspected,
        checks=["vertex layout vs VS inputs", "declared vs bound registers", "table completeness"],
    )


@tool(
    name="diagnose-mobile-risks",
    summary=(
        "Flag patterns that behave badly on mobile/tiled GPUs: large render targets, many "
        "render target switches, MSAA plus post-processing, deep G-buffers, dependent reads "
        "of a just-written target, and heavy per-draw binding counts."
    ),
    category="diagnostics",
    parameters=with_session(
        PAGE_PARAMS,
        target_resolution={
            "type": "integer",
            "description": "Reference pixel budget for a mobile target. Default 2073600 (1080p).",
        },
    ),
    returns="Findings with the resource or draw involved and a mobile-specific recommendation.",
    examples=["pix-tool-set diagnose-mobile-risks"],
)
def diagnose_mobile_risks(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    findings: list[dict[str, Any]] = []
    budget = int(args.get("target_resolution") or 1920 * 1080)
    usage = capture.resource_usage

    # 1. oversized render targets
    for resource in capture.resources.values():
        if not (resource.is_texture and resource.is_render_target):
            continue
        if resource.api_id not in usage:
            continue
        if resource.pixel_count > budget * 1.5:
            findings.append(
                _finding(
                    "warning",
                    "oversized_render_target",
                    (
                        f"Render target {resource.api_id} is {resource.width}x{resource.height} "
                        f"({resource.pixel_count} pixels), above the mobile budget."
                    ),
                    "Render at a lower internal resolution and upscale, or use dynamic resolution.",
                    resource_id=resource.api_id,
                    description=resource.describe(),
                    estimated_bytes=resource.size_bytes,
                )
            )
        if resource.sample_count > 1:
            findings.append(
                _finding(
                    "warning",
                    "msaa_render_target",
                    f"Render target {resource.api_id} uses {resource.sample_count}x MSAA.",
                    "On tiled GPUs prefer resolving inside the tile, and avoid sampling MSAA targets.",
                    resource_id=resource.api_id,
                    sample_count=resource.sample_count,
                )
            )

    # 2. render target switch churn
    switches = 0
    previous: list[int] | None = None
    for draw in capture.draw_calls:
        if draw.kind is not EventKind.DRAW:
            continue
        current = list(draw.render_target_resource_ids)
        if current and current != previous:
            switches += 1
            previous = current
    if switches > 60:
        findings.append(
            _finding(
                "warning",
                "render_target_churn",
                f"{switches} render target switches in the frame.",
                "Each switch flushes tile memory on mobile; group draws by target.",
                switch_count=switches,
            )
        )

    # 3. deep G-buffer (many simultaneous targets)
    for draw in capture.draw_calls:
        if len(draw.render_target_resource_ids) >= 4:
            total_bits = 0
            for resource_id in draw.render_target_resource_ids:
                resource = capture.resource(resource_id)
                if resource is not None:
                    total_bits += format_bits_per_pixel(resource.format)
            findings.append(
                _finding(
                    "warning",
                    "deep_gbuffer",
                    (
                        f"Draw {draw.index} writes {len(draw.render_target_resource_ids)} targets "
                        f"totalling {total_bits} bits per pixel."
                    ),
                    "Tiled GPUs have limited tile memory; consider fewer or narrower targets.",
                    draw_index=draw.index,
                    target_count=len(draw.render_target_resource_ids),
                    bits_per_pixel=total_bits,
                    pass_name=draw.pass_name,
                )
            )
            break

    # 4. dependent read of a freshly written target
    for resource_id, entry in usage.items():
        writes = entry["write_draws"]
        reads = entry["read_draws"]
        if not writes or not reads:
            continue
        for write_index in writes:
            following = [index for index in reads if write_index < index <= write_index + 2]
            if following:
                resource = capture.resource(resource_id)
                findings.append(
                    _finding(
                        "info",
                        "immediate_dependent_read",
                        (
                            f"Resource {resource_id} is read at draw {following[0]} right after "
                            f"being written at draw {write_index}."
                        ),
                        "On tiled GPUs this forces a tile flush; batch the producer and consumer apart.",
                        resource_id=resource_id,
                        write_draw=write_index,
                        read_draw=following[0],
                        description=resource.describe() if resource else None,
                    )
                )
                break
        if len([f for f in findings if f["topic"] == "immediate_dependent_read"]) >= 10:
            break

    # 5. heavy per-draw binding counts
    heavy = [
        draw
        for draw in capture.draw_calls
        if len(draw.srvs) + len(draw.uavs) > 48
    ]
    if heavy:
        findings.append(
            _finding(
                "info",
                "large_descriptor_tables",
                f"{len(heavy)} draw(s) bind more than 48 SRV/UAV descriptors.",
                "Mobile drivers handle large tables poorly; trim to what the shader reads.",
                example_draw_indices=[draw.index for draw in heavy[:8]],
            )
        )

    # 6. compute-heavy frame
    dispatches = [d for d in capture.draw_calls if d.kind is EventKind.DISPATCH]
    total_threads = sum(d.thread_count for d in dispatches)
    if total_threads > 50_000_000:
        findings.append(
            _finding(
                "warning",
                "compute_heavy_frame",
                f"The frame launches about {total_threads:,} compute threads.",
                "Mobile compute throughput is far lower; move work off the critical path or downsample.",
                dispatch_count=len(dispatches),
                total_threads=total_threads,
            )
        )

    return _envelope(
        findings,
        args,
        budget_pixels=budget,
        checks=[
            "oversized targets",
            "MSAA",
            "render target churn",
            "deep g-buffer",
            "dependent reads",
            "descriptor table size",
            "compute load",
        ],
    )
