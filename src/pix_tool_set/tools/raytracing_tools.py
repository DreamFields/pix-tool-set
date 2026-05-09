"""Raytracing tools: state objects, shader binding tables, acceleration structures.

These answer the questions a DXR frame raises that no PSO-shaped tool can. Three
of them exist because the underlying data has three different shapes -- a pipeline
graph, a table of shader records, and a scene of acceleration structures -- and
folding them into one response would bury whichever one the caller cared about.

Every tool here reports how a stage was decided (``stage_source``) and refuses to
report geometry counts for bottom-level acceleration structures, because the
export does not contain them. Both rules exist for the same reason: an inference
presented as a measurement is worse than no answer, since nothing downstream can
tell the difference.
"""

from __future__ import annotations

from typing import Any, Optional

from ..context import ToolContext
from ..engine.model import EventKind, StateObject, StateObjectType
from ..errors import invalid_argument, not_found
from ..results import ToolResult
from ._common import (
    DRAW_SELECTOR,
    PAGE_PARAMS,
    note_missing_queue_id,
    page_args,
    page_envelope,
    resolve_draw,
    tool,
    with_session,
)

# Restated on every payload that quotes a stage, because the value looks identical
# whether it was read out of a hit group or guessed from a name prefix.
STAGE_SOURCE_NOTE = (
    "A DXIL library declares no stages, so every DXR stage here is derived. "
    "stage_source says how: 'hit_group' is stated by the export and is a fact; "
    "'shader_table' is positional evidence from the SBT; 'name_prefix' is a UE5 "
    "naming convention and is a guess. A null stage means no rule applied -- it "
    "does not mean the export is incomplete."
)

BLAS_GEOMETRY_NOTE = (
    "Bottom-level acceleration structures are replayed from a driver-private "
    "serialized blob (CopyRaytracingAccelerationStructure DESERIALIZE), not from "
    "D3D12_RAYTRACING_GEOMETRY_DESCs. Triangle and vertex counts are therefore not "
    "present in the export and are reported as null. Blob sizes are reported, but "
    "they are compressed driver structures and must not be used to estimate "
    "geometry."
)


def _resolve_state_object(capture, args: dict[str, Any]) -> StateObject:
    """From an explicit id, or from the state object bound at a draw."""
    state_object_id = args.get("state_object_id")
    if state_object_id is None:
        if not any(
            args.get(key) is not None
            for key in ("draw_index", "global_id", "queue_id")
        ):
            raise invalid_argument(
                "state_object_id/draw_index/global_id/queue_id",
                "provide a state object id, or a selector for a raytracing action",
            )
        draw = resolve_draw(capture, args, what="state object")
        if draw.state_object_id is None:
            raise not_found(
                "state object",
                f"draw_index={draw.index}",
                "This action binds no raytracing state object. Only an action after "
                "SetPipelineState1 has one; run list-raytracing-work to find those.",
            )
        state_object_id = draw.state_object_id

    state_object = capture.state_objects.get(int(state_object_id))
    if state_object is None:
        known = sorted(capture.state_objects)
        if not known:
            raise not_found(
                "state object",
                state_object_id,
                "This capture declares no raytracing state objects, so the frame does "
                "no raytracing. That is a fact about the capture, not a parse failure.",
            )
        raise not_found(
            "state object",
            state_object_id,
            f"Known ids run {known[0]}..{known[-1]} ({len(known)} objects). Run "
            "list-raytracing-work or describe-state-object on a draw to find one.",
        )
    return state_object


def _resolve_local_root_signature(capture, state_object: StateObject) -> dict[str, Any]:
    """Expand a state object's local root signatures into their parameter tables.

    The ids on ``local_root_signature_ids`` are shared across the object's exports
    and hit groups; the table behind each is what a PIX record panel shows (a CBV
    per space/register plus any static samplers). This returns one entry per id so
    the caller can key an export to it.
    """
    resolved = state_object.local_root_signatures()
    missing = state_object.missing_local_root_signatures
    return {
        "local_root_signatures": resolved,
        "missing_local_root_signature_ids": missing,
    }


@tool(
    name="describe-state-object",
    summary=(
        "A raytracing state object in full: shader exports with their original HLSL "
        "names, hit groups, payload/attribute sizes, and the collections it links in."
    ),
    category="pipeline",
    parameters=with_session(
        DRAW_SELECTOR,
        PAGE_PARAMS,
        state_object_id={
            "type": "integer",
            "description": (
                "ApiObjectId of the state object, as it appears in "
                "SetPipelineState1(GetStateObject(id)). Alternatively pass a draw "
                "selector to take the object bound at that action."
            ),
        },
        expand={
            "type": "boolean",
            "description": (
                "Follow EXISTING_COLLECTION references (default true). A raytracing "
                "pipeline built out of collections declares almost nothing itself, so "
                "with expand=false a RTPSO correctly reports zero exports -- that is "
                "what it declared, not what it can run. Set false only to inspect one "
                "object's own declarations."
            ),
        },
        stage={
            "type": "string",
            "description": (
                "Restrict exports to one derived DXR stage, e.g. RAYGEN or CLOSESTHIT."
            ),
        },
    ),
    returns=(
        "State object configuration, its export and hit group lists, the collection "
        "graph it resolves through, the local root signature parameter tables each "
        "set of shaders shares, and any dispatches that use it."
    ),
    examples=[
        "pix-tool-set describe-state-object --state-object-id 3930",
        "pix-tool-set describe-state-object --draw-index 2705",
        "pix-tool-set describe-state-object --state-object-id 3930 --stage RAYGEN",
    ],
    notes=STAGE_SOURCE_NOTE,
)
def describe_state_object(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    state_object = _resolve_state_object(capture, args)
    expand = args.get("expand")
    expand = True if expand is None else bool(expand)
    offset, limit = page_args(args)

    exports = state_object.resolved_exports if expand else state_object.exports
    hit_groups = state_object.resolved_hit_groups if expand else state_object.hit_groups

    stage = args.get("stage")
    if stage:
        wanted = str(stage).upper()
        exports = [
            export
            for export in exports
            if export.stage is not None and export.stage.value == wanted
        ]

    window = exports[offset : offset + limit] if limit else exports[offset:]

    consumers = [
        {
            "draw_index": draw.index,
            "global_id": draw.global_id,
            "queue_id": draw.queue_id,
            "pass_name": draw.pass_name,
            "api": draw.api,
            "shader_binding_table_key": draw.indirect_argument_buffer,
        }
        for draw in capture.draw_calls
        if draw.state_object_id == state_object.api_id
    ]

    local_rs = _resolve_local_root_signature(capture, state_object)
    resolved_by_id = local_rs["local_root_signatures"]

    def with_local_rs(entry: dict[str, Any], rs_id: Any) -> dict[str, Any]:
        if rs_id is not None and rs_id in resolved_by_id:
            entry["local_root_signature"] = resolved_by_id[rs_id]
        return entry

    export_rows = [
        with_local_rs(export.to_dict(), export.local_root_signature_id)
        for export in window
    ]
    hit_group_rows = [
        with_local_rs(group.to_dict(), group.local_root_signature_id)
        for group in hit_groups
    ]

    data: dict[str, Any] = {
        "state_object": state_object.to_dict(expand=expand),
        "exports": export_rows,
        "hit_groups": hit_group_rows,
        "resolved_state_object_ids": state_object.resolved_state_object_ids,
        "local_root_signatures": resolved_by_id,
        "missing_local_root_signature_ids": local_rs["missing_local_root_signature_ids"],
        "consumers": consumers,
        "consumer_count": len(consumers),
        "stage_source_note": STAGE_SOURCE_NOTE,
        **page_envelope(len(exports), offset, limit, len(window)),
    }

    result = ToolResult.success(data)

    missing = state_object.missing_collection_ids
    if missing:
        # A dangling collection means the expansion is short some shaders, and the
        # payload above would look complete. Say so rather than let a truncated
        # export list pass for the whole pipeline.
        result.degrade(
            f"{len(missing)} referenced collection(s) are not present in the export, so "
            f"the export list is incomplete: {missing}. Re-export CreatePSOs.cpp.",
            reason="state_object_collection_missing",
            missing_collection_ids=missing,
        )
    elif (
        expand
        and state_object.type is StateObjectType.RAYTRACING_PIPELINE
        and not exports
        and not stage
    ):
        result.degrade(
            "This raytracing pipeline resolved to zero shader exports, which should not "
            "happen for a pipeline that is actually dispatched. Treat it as a parse "
            "failure rather than an empty pipeline.",
            reason="state_object_expansion_empty",
        )
    if not expand and not state_object.exports:
        result.add_diagnostic(
            "info",
            "expand=false, so this lists only what the object itself declares. A "
            "raytracing pipeline assembled from collections declares no exports of its "
            "own; use expand=true for the shaders it can launch.",
        )
    if local_rs["missing_local_root_signature_ids"]:
        result.degrade(
            f"Local root signature id(s) {local_rs['missing_local_root_signature_ids']} "
            "are referenced by this object but absent from the root signature export, "
            "so their parameter tables could not be expanded.",
            reason="local_root_signature_missing",
            missing_local_root_signature_ids=local_rs["missing_local_root_signature_ids"],
        )
    return result


@tool(
    name="describe-shader-table",
    summary=(
        "The shader binding table one raytracing dispatch uses: ray dimensions, the "
        "four table regions, and every shader record with its local root arguments."
    ),
    category="pipeline",
    parameters=with_session(
        DRAW_SELECTOR,
        PAGE_PARAMS,
        indirect_buffer_key={
            "type": "string",
            "description": (
                "Indirect argument buffer name the table was built for, e.g. '1415_1'. "
                "Alternatively pass a draw selector for a raytracing action."
            ),
        },
        table={
            "type": "string",
            "enum": ["raygen", "miss", "hit_group", "callable"],
            "description": "Restrict records to one region.",
        },
    ),
    returns="Dispatch dimensions, region layout, and paged shader records.",
    examples=[
        "pix-tool-set describe-shader-table --draw-index 2705",
        "pix-tool-set describe-shader-table --indirect-buffer-key 1415_2 --table hit_group",
    ],
    notes=(
        "Region sizes come from D3D12_DISPATCH_RAYS_DESC and are not buffer sizes: a "
        "64-byte raygen region can live in a multi-megabyte allocation. A null region "
        "means the pipeline has no shader of that class, which is different from a "
        "region with zero records. Records flagged in_declared_region=false were "
        "written past the region this dispatch reads and are not executed by it."
    ),
)
def describe_shader_table(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    key = args.get("indirect_buffer_key")
    draw = None
    if key is None:
        if not any(
            args.get(name) is not None
            for name in ("draw_index", "global_id", "queue_id")
        ):
            raise invalid_argument(
                "indirect_buffer_key/draw_index/global_id/queue_id",
                "provide a table key, or a selector for a raytracing action",
            )
        draw = resolve_draw(capture, args, what="shader binding table")
        sbt = draw.shader_binding_table
        if sbt is None:
            # Three different causes, and the caller's next step differs for each,
            # so they are not collapsed into one message.
            if not draw.is_raytracing:
                raise not_found(
                    "shader binding table",
                    f"draw_index={draw.index}",
                    "This action is not a raytracing dispatch. Run list-raytracing-work "
                    "to find the ones that are.",
                )
            raise not_found(
                "shader binding table",
                f"draw_index={draw.index}",
                f"This raytracing action reads indirect argument buffer "
                f"{draw.indirect_argument_buffer!r}, and no CreateIndirectArgumentBuffer_* "
                f"function in the export writes a D3D12_DISPATCH_RAYS_DESC into that key. "
                f"The dispatch arguments were most likely produced on the GPU, in which "
                f"case they exist only at replay time.",
            )
        key = sbt.indirect_buffer_key

    sbt = capture.shader_binding_tables.get(str(key))
    if sbt is None:
        known = sorted(capture.shader_binding_tables)
        raise not_found(
            "shader binding table",
            key,
            f"Known table keys: {known or 'none in this capture'}.",
        )

    offset, limit = page_args(args)
    records = sbt.records
    table_filter = args.get("table")
    if table_filter:
        records = [record for record in records if record.table == table_filter]
    window = records[offset : offset + limit] if limit else records[offset:]

    state_object = sbt.state_object
    consumers = [
        {
            "draw_index": consumer.index,
            "global_id": consumer.global_id,
            "queue_id": consumer.queue_id,
            "pass_name": consumer.pass_name,
        }
        for consumer in capture.draw_calls
        if consumer.indirect_argument_buffer == sbt.indirect_buffer_key
    ]

    data = {
        "shader_binding_table": sbt.to_dict(),
        "records": [record.to_dict() for record in window],
        "state_object": state_object.to_dict() if state_object else None,
        "consumers": consumers,
        **page_envelope(len(records), offset, limit, len(window)),
    }
    result = ToolResult.success(data)

    unresolved = sbt.unresolved_identifiers
    if unresolved:
        # This is the joint check on state object expansion and table matching. A
        # record naming a shader the pipeline cannot reach means one of the two is
        # wrong, and the payload would otherwise look entirely plausible.
        result.degrade(
            f"{len(unresolved)} record identifier(s) are not exported by state object "
            f"{sbt.state_object_id}: {unresolved[:8]}. Either the collection graph was "
            f"not fully resolved or this table belongs to a different pipeline.",
            reason="shader_record_identifier_unresolved",
        )
    if draw is not None:
        note_missing_queue_id(result, draw, level="info")
    return result


@tool(
    name="list-raytracing-work",
    summary=(
        "Every raytracing operation in the frame in submission order: acceleration "
        "structure builds, then the dispatches, with their state objects and tables."
    ),
    category="events",
    parameters=with_session(
        PAGE_PARAMS,
        kind={
            "type": "string",
            "enum": ["all", "builds", "dispatches"],
            "description": "Restrict to AS builds or to ray dispatches. Default all.",
        },
    ),
    returns="Ordered raytracing timeline with selectors for each entry.",
    examples=[
        "pix-tool-set list-raytracing-work",
        "pix-tool-set list-raytracing-work --kind dispatches",
    ],
    notes=(
        "Dispatches are found by effective_kind, not by API name: a UE5 export contains "
        "no literal DispatchRays call, so filtering on the API name finds nothing while "
        "the frame is in fact tracing rays."
    ),
)
def list_raytracing_work(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    kind = args.get("kind") or "all"
    offset, limit = page_args(args)

    rows: list[dict[str, Any]] = []
    if kind in ("all", "builds"):
        for build in capture.acceleration_structure_builds:
            rows.append(
                {
                    "work": "acceleration_structure_build",
                    "global_id": build.global_id,
                    "type": build.type,
                    "flags": build.flags,
                    "instance_count": len(build.instances),
                    "dest_resource_id": build.dest_resource_id,
                    "pass_name": build.marker_path[-1] if build.marker_path else "",
                    "source": f"{build.source_file}:{build.source_line}",
                }
            )
    if kind in ("all", "dispatches"):
        for draw in capture.draw_calls:
            if draw.effective_kind is not EventKind.DISPATCH_RAYS and not draw.is_raytracing:
                continue
            sbt = draw.shader_binding_table
            state_object = draw.state_object
            rows.append(
                {
                    "work": "dispatch_rays",
                    "draw_index": draw.index,
                    "global_id": draw.global_id,
                    "queue_id": draw.queue_id,
                    "queue_name": draw.queue_name,
                    "api": draw.api,
                    "effective_kind": draw.effective_kind.value,
                    "pass_name": draw.pass_name,
                    "state_object_id": draw.state_object_id,
                    "shader_binding_table_key": draw.indirect_argument_buffer,
                    "dispatch_dimensions": (
                        [sbt.width, sbt.height, sbt.depth] if sbt else None
                    ),
                    "ray_count": sbt.ray_count if sbt else None,
                    "shader_count": (
                        len(state_object.resolved_exports) if state_object else None
                    ),
                }
            )

    total = len(rows)
    window = rows[offset : offset + limit] if limit else rows[offset:]
    data = {
        "raytracing_work": window,
        "summary": {
            "acceleration_structure_builds": len(capture.acceleration_structure_builds),
            "ray_dispatches": sum(
                1
                for draw in capture.draw_calls
                if draw.effective_kind is EventKind.DISPATCH_RAYS
            ),
            "state_objects": len(capture.state_objects),
            "shader_binding_tables": len(capture.shader_binding_tables),
        },
        **page_envelope(total, offset, limit, len(window)),
    }
    result = ToolResult.success(data)
    if not total:
        result.add_diagnostic(
            "info",
            "This frame submits no raytracing work: no acceleration structure builds and "
            "no dispatches under a state object. That is a fact about the capture.",
        )
    return result


@tool(
    name="analyze-acceleration-structures",
    summary=(
        "Acceleration structures in the frame: TLAS builds with their instances and "
        "hit-group indices, the AS resources, and the serialized blobs behind them."
    ),
    category="advanced",
    parameters=with_session(
        PAGE_PARAMS,
        detail={
            "type": "boolean",
            "description": "Include per-instance transforms and blob-level listings.",
        },
        resolve_hit_groups={
            "type": "boolean",
            "description": (
                "Map each instance's InstanceContributionToHitGroupIndex through a "
                "shader table's hit-group stride to name the hit group it uses. Needs a "
                "shader table; off by default because a frame can have several and the "
                "mapping is only valid for the one actually dispatched."
            ),
        },
        indirect_buffer_key={
            "type": "string",
            "description": (
                "Which shader table to resolve hit-group indices against, e.g. '1415_2'. "
                "Required when the capture has more than one and resolve_hit_groups is set."
            ),
        },
    ),
    returns=(
        "TLAS builds, instance descriptions, AS resources and serialized blob totals. "
        "Geometry counts are always null; see notes."
    ),
    examples=[
        "pix-tool-set analyze-acceleration-structures",
        "pix-tool-set analyze-acceleration-structures --detail --resolve-hit-groups "
        "--indirect-buffer-key 1415_2",
    ],
    notes=BLAS_GEOMETRY_NOTE,
)
def analyze_acceleration_structures(
    args: dict[str, Any], context: ToolContext
) -> ToolResult:
    capture = context.capture(args)
    detail = bool(args.get("detail"))
    offset, limit = page_args(args)

    builds = capture.acceleration_structure_builds
    window = builds[offset : offset + limit] if limit else builds[offset:]

    resolve = bool(args.get("resolve_hit_groups"))
    sbt = None
    resolve_error: Optional[str] = None
    if resolve:
        key = args.get("indirect_buffer_key")
        tables = capture.shader_binding_tables
        if key is not None:
            sbt = tables.get(str(key))
            if sbt is None:
                resolve_error = (
                    f"No shader table named {key!r}. Known keys: {sorted(tables) or 'none'}."
                )
        elif len(tables) == 1:
            sbt = next(iter(tables.values()))
        elif not tables:
            resolve_error = "This capture has no shader binding tables to resolve against."
        else:
            resolve_error = (
                f"This capture has {len(tables)} shader tables ({sorted(tables)}); an "
                "instance's hit-group index means something different in each, so pass "
                "indirect_buffer_key to say which dispatch you are asking about."
            )

    rows: list[dict[str, Any]] = []
    for build in window:
        entry = build.to_dict(detail=detail)
        if sbt is not None:
            entry["instances_resolved"] = [
                _resolve_instance_hit_group(sbt, instance)
                for instance in build.instances
            ]
        rows.append(entry)

    serialized = capture.serialized_acceleration_structures
    by_resource: dict[int, dict[str, Any]] = {}
    for blob in serialized:
        bucket = by_resource.setdefault(
            blob.resource_id,
            {"resource_id": blob.resource_id, "blob_count": 0, "serialized_bytes": 0},
        )
        bucket["blob_count"] += 1
        bucket["serialized_bytes"] += blob.serialized_size

    as_resources = [
        {
            "resource_id": resource.api_id,
            "name": resource.name,
            "size_bytes": resource.size_bytes,
            "initial_state": resource.initial_state,
        }
        for resource in capture.resources.values()
        if "RAYTRACING_ACCELERATION_STRUCTURE" in (resource.flags or "")
        or "RAYTRACING_ACCELERATION_STRUCTURE" in (resource.initial_state or "")
    ]

    data: dict[str, Any] = {
        "builds": rows,
        "acceleration_structure_resources": as_resources,
        "postbuild_info": {
            "available": bool(capture.postbuild_info),
            "count": len(capture.postbuild_info),
            "queries": [info.to_dict() for info in capture.postbuild_info],
            "note": (
                "Post-build info (actual / compacted / serialized size) is opt-in on "
                "the application's side: it only exists when the frame called "
                "EmitRaytracingAccelerationStructurePostbuildInfo. An empty list means "
                "this capture never asked the driver for it, not that parsing failed."
            ),
        },
        "serialized_blobs": {
            "total": len(serialized),
            "serialized_bytes": sum(blob.serialized_size for blob in serialized),
            "by_resource": sorted(
                by_resource.values(), key=lambda entry: -entry["serialized_bytes"]
            ),
        },
        "geometry_availability": {
            "triangle_counts_available": False,
            "vertex_counts_available": False,
            "geometry_descs_available": False,
            "reason": BLAS_GEOMETRY_NOTE,
        },
        **page_envelope(len(builds), offset, limit, len(window)),
    }
    if detail:
        data["serialized_blobs"]["blobs"] = [blob.to_dict() for blob in serialized]

    result = ToolResult.success(data)
    result.add_diagnostic("info", BLAS_GEOMETRY_NOTE)
    if resolve_error:
        result.degrade(resolve_error, reason="hit_group_resolution_unavailable")
    return result


def _resolve_instance_hit_group(sbt, instance) -> dict[str, Any]:
    """Name the hit group an instance's contribution index lands on.

    ``InstanceContributionToHitGroupIndex`` is a record index, so the byte offset
    is the index times the hit-group region's stride. Reporting the index alone
    leaves the caller to redo that multiplication against a stride they would have
    to look up separately, which is where the mistake usually happens.
    """
    entry: dict[str, Any] = {
        "index": instance.index,
        "instance_id": instance.instance_id,
        "contribution_to_hit_group_index": instance.contribution_to_hit_group_index,
        "shader_binding_table_key": sbt.indirect_buffer_key,
    }
    region = sbt.hit_group
    if region is None or not region.stride_in_bytes:
        entry["hit_group"] = None
        entry["note"] = (
            "That dispatch declares no hit-group region, so the contribution index "
            "addresses nothing in it."
        )
        return entry
    offset = instance.contribution_to_hit_group_index * region.stride_in_bytes
    entry["record_offset"] = offset
    match = next(
        (
            record
            for record in sbt.records
            if record.table == "hit_group" and record.offset == offset
        ),
        None,
    )
    if match is None:
        entry["hit_group"] = None
        entry["note"] = (
            f"No record was reconstructed at offset {offset} of the hit-group region. "
            "PIX only rebuilds the records the frame actually wrote, so an index into "
            "an unwritten slot has no shader to name -- it is not a lookup failure."
        )
        return entry
    entry["hit_group"] = match.shader_identifier
    entry["root_constants"] = list(match.root_constants)
    return entry


# ======================================================================
@tool(
    name="analyze-raytracing",
    summary=(
        "Whole-frame raytracing overview in one call: ray dispatches with their "
        "pipelines and tables, acceleration structure builds with their instances, "
        "the inline-raytracing compute passes, and measured GPU cost when cached."
    ),
    category="advanced",
    parameters=with_session(
        detail={
            "type": "boolean",
            "description": (
                "Include per-export listings, per-instance transforms and shader "
                "record dumps instead of just the counts and headline fields."
            ),
        },
        include_inline={
            "type": "boolean",
            "description": (
                "Also report compute passes that trace rays through TraceRayInline "
                "(DXR 1.1). These have no state object and no shader table, so every "
                "state-object tool is blind to them, yet they are raytracing work. "
                "Default true."
            ),
        },
        include_timing={
            "type": "boolean",
            "description": (
                "Attach measured GPU duration per dispatch. Only uses an existing "
                "cached measurement; this tool never triggers a replay. Run "
                "export-timing or event-timing first to populate it. Default true."
            ),
        },
    ),
    returns=(
        "One payload with: summary counts, ray_dispatches (pipeline + table + cost), "
        "acceleration_structures (builds, instances, blob totals), "
        "inline_raytracing passes, and the capability notes that apply."
    ),
    examples=[
        "pix-tool-set analyze-raytracing",
        "pix-tool-set analyze-raytracing --detail",
        "pix-tool-set analyze-raytracing --include-inline false",
    ],
    notes=(
        "This is the entry point for 'what raytracing does this frame do'. It answers "
        "in one call what otherwise takes list-raytracing-work, describe-state-object, "
        "describe-shader-table, analyze-acceleration-structures and event-timing. "
        + STAGE_SOURCE_NOTE
        + " "
        + BLAS_GEOMETRY_NOTE
    ),
)
def analyze_raytracing(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    detail = bool(args.get("detail"))
    include_inline = args.get("include_inline")
    include_inline = True if include_inline is None else bool(include_inline)
    include_timing = args.get("include_timing")
    include_timing = True if include_timing is None else bool(include_timing)

    # ---- measured cost, only if something already paid for the replay ----------
    timing_table = None
    timing_available = False
    if include_timing:
        try:
            from ..engine import timing as timing_mod

            # allow_export=False is the whole point: an overview must never silently
            # trigger a ~100s GPU replay. If nothing is cached, the answer is simply
            # structural and says so.
            timing_table, _report = timing_mod.ensure_timing(
                capture,
                counters=timing_mod.TIMING_GLOB,
                timeout=1800,
                force=False,
                allow_export=False,
            )
            timing_available = timing_table is not None
        except Exception:  # noqa: BLE001
            # A missing or unreadable cache is not an error here: cost is an optional
            # enrichment and the structural answer stands without it.
            timing_table = None
            timing_available = False

    # ---- ray dispatches --------------------------------------------------------
    dispatches: list[dict[str, Any]] = []
    for draw in capture.draw_calls:
        if draw.effective_kind is not EventKind.DISPATCH_RAYS and not draw.is_raytracing:
            continue
        sbt = draw.shader_binding_table
        state_object = draw.state_object

        entry: dict[str, Any] = {
            "draw_index": draw.index,
            "global_id": draw.global_id,
            "queue_id": draw.queue_id,
            "queue_name": draw.queue_name,
            "pass_name": draw.pass_name,
            "api": draw.api,
            "effective_kind": draw.effective_kind.value,
            "state_object_id": draw.state_object_id,
            "shader_binding_table_key": draw.indirect_argument_buffer,
            "dispatch_dimensions": [sbt.width, sbt.height, sbt.depth] if sbt else None,
            "ray_count": sbt.ray_count if sbt else None,
        }

        if state_object is not None:
            exports = list(state_object.resolved_exports)
            by_stage: dict[str, int] = {}
            for export in exports:
                key = export.stage.value if export.stage else "unknown"
                by_stage[key] = by_stage.get(key, 0) + 1
            entry["pipeline"] = {
                "max_payload_size": state_object.max_payload_size,
                "max_attribute_size": state_object.max_attribute_size,
                "max_recursion_depth": state_object.max_recursion_depth,
                "flags": list(state_object.flags or []),
                "export_count": len(exports),
                "hit_group_count": len(state_object.resolved_hit_groups),
                "collection_count": len(state_object.existing_collection_ids or []),
                "exports_by_stage": by_stage,
                "global_root_signature_id": state_object.global_root_signature_id,
            }
            # The distinct HLSL shaders behind the mangled exports: a UE5 RTPSO lists
            # the same entry point once per collection, so the raw export count says
            # little about how many shaders were actually authored.
            unique_entries: dict[str, str] = {}
            for export in exports:
                name = export.original_name or export.name
                if name and name not in unique_entries:
                    unique_entries[name] = export.stage.value if export.stage else "unknown"
            entry["pipeline"]["unique_entry_points"] = [
                {"entry_point": name, "stage": stage}
                for name, stage in sorted(unique_entries.items())
            ]
            if detail:
                entry["pipeline"]["exports"] = [
                    {
                        "name": export.name,
                        "original_name": export.original_name,
                        "stage": export.stage.value if export.stage else None,
                        "stage_source": export.stage_source,
                        "defining_state_object_id": export.defining_state_object_id,
                    }
                    for export in exports
                ]
                entry["pipeline"]["hit_groups"] = [
                    {
                        "name": group.name,
                        "type": group.type,
                        "closest_hit": group.closest_hit,
                        "any_hit": group.any_hit,
                        "intersection": group.intersection,
                    }
                    for group in state_object.resolved_hit_groups
                ]

        if sbt is not None:
            regions: dict[str, Any] = {}
            for label in ("raygen", "miss", "hit_group", "callable"):
                region = getattr(sbt, label, None)
                if region is None:
                    # A null region means the pipeline has no shader of that class,
                    # which is not the same as a region holding zero records.
                    regions[label] = None
                    continue
                regions[label] = {
                    "size_in_bytes": region.size_in_bytes,
                    "stride_in_bytes": region.stride_in_bytes,
                    "record_capacity": region.record_capacity,
                }
            counts: dict[str, int] = {}
            for record in sbt.records:
                counts[record.table] = counts.get(record.table, 0) + 1
            entry["shader_binding_table"] = {
                "indirect_buffer_key": sbt.indirect_buffer_key,
                "raygen_identifier": sbt.raygen_identifier,
                "regions": regions,
                "record_count": len(sbt.records),
                "records_by_table": counts,
                "records_outside_declared_regions": sum(
                    1 for record in sbt.records if not record.in_declared_region
                ),
            }
            if detail:
                entry["shader_binding_table"]["records"] = [
                    {
                        "table": record.table,
                        "offset": record.offset,
                        "shader_identifier": record.shader_identifier,
                        "in_declared_region": record.in_declared_region,
                        "root_constants": list(record.root_constants),
                    }
                    for record in sbt.records
                ]

        if timing_table is not None and draw.global_id is not None:
            measured = timing_table.lookup(global_id=draw.global_id)
            if measured is not None:
                entry["measured_cost"] = {
                    "duration_ns": measured.duration_ns,
                    "duration_ms": measured.duration_ms,
                }

        dispatches.append(entry)

    # ---- acceleration structures ----------------------------------------------
    builds: list[dict[str, Any]] = []
    total_instances = 0
    for build in capture.acceleration_structure_builds:
        total_instances += len(build.instances)
        row: dict[str, Any] = {
            "global_id": build.global_id,
            "type": build.type,
            "flags": list(build.flags or []),
            "instance_count": len(build.instances),
            "dest_resource_id": build.dest_resource_id,
            "pass_name": build.marker_path[-1] if build.marker_path else "",
            "source": f"{build.source_file}:{build.source_line}",
            "triangle_count": None,
            "vertex_count": None,
        }
        if detail and build.instances:
            row["instances"] = [
                {
                    "index": instance.index,
                    "instance_id": instance.instance_id,
                    "instance_mask": instance.instance_mask,
                    "contribution_to_hit_group_index": (
                        instance.contribution_to_hit_group_index
                    ),
                    "blas_resource_id": instance.blas_resource_id,
                    "transform": list(instance.transform or []),
                }
                for instance in build.instances
            ]
        builds.append(row)

    # ---- inline raytracing (DXR 1.1) ------------------------------------------
    inline_rows: list[dict[str, Any]] = []
    if include_inline:
        seen_passes: set[str] = set()
        for draw in capture.draw_calls:
            if draw.state_object_id is not None:
                continue
            if draw.effective_kind not in (EventKind.DISPATCH, EventKind.EXECUTE_INDIRECT):
                continue
            name = draw.pass_name or ""
            flat = name.replace(" ", "").lower()
            # UE5 names every inline path "...HardwareRayTracing...". Matching the pass
            # name is evidence, not proof, so the payload says so rather than implying
            # the export declared it.
            if "hardwareraytracing" not in flat:
                continue
            # The *IndirectArgs* passes only fill an indirect argument buffer for the
            # dispatch that follows; they trace no rays. Counting them as raytracing
            # work would overstate how much of the frame traces.
            if "indirectargs" in flat:
                continue
            if name in seen_passes:
                continue
            seen_passes.add(name)
            inline_rows.append(
                {
                    "draw_index": draw.index,
                    "global_id": draw.global_id,
                    "pass_name": name,
                    "api": draw.api,
                    "effective_kind": draw.effective_kind.value,
                    "pso_id": draw.pso_id,
                    "evidence": "pass_name",
                }
            )

    summary = {
        "ray_dispatches": len(dispatches),
        "acceleration_structure_builds": len(builds),
        "tlas_instances_total": total_instances,
        "state_objects_declared": len(capture.state_objects),
        "shader_binding_tables": len(capture.shader_binding_tables),
        "inline_raytracing_passes": len(inline_rows),
        "frame_does_raytracing": bool(dispatches or builds or inline_rows),
    }

    blob_total = 0
    blob_bytes = 0
    try:
        serialized = getattr(capture, "serialized_acceleration_structures", None)
        if serialized:
            blob_total = len(serialized)
            blob_bytes = sum(int(getattr(b, "serialized_size", 0) or 0) for b in serialized)
    except Exception:  # noqa: BLE001
        blob_total = 0

    data: dict[str, Any] = {
        "summary": summary,
        "ray_dispatches": dispatches,
        "acceleration_structures": {
            "builds": builds,
            "serialized_blob_count": blob_total or None,
            "serialized_bytes": blob_bytes or None,
            "geometry_availability": {
                "triangle_counts_available": False,
                "vertex_counts_available": False,
                "reason": BLAS_GEOMETRY_NOTE,
            },
        },
        "inline_raytracing": inline_rows,
        "timing": {
            "available": timing_available,
            "note": (
                "Measured TOP-to-EOP duration from a cached GPU replay."
                if timing_available
                else "No cached measurement. Run export-timing (one replay) to populate "
                "it, then re-run this tool; it never replays on its own."
            ),
        },
        "stage_source_note": STAGE_SOURCE_NOTE,
    }

    result = ToolResult.success(data)
    if not summary["frame_does_raytracing"]:
        result.add_diagnostic(
            "info",
            "This frame submits no raytracing work at all: no dispatches, no "
            "acceleration structure builds, no inline paths. That is a fact about the "
            "capture, not a parse failure.",
        )
        return result

    if dispatches and not timing_available and include_timing:
        result.add_diagnostic(
            "info",
            "Structural answer is complete; cost is absent because no timing replay "
            "has been cached for this session. Run export-timing once to add it.",
        )
    if inline_rows:
        result.add_diagnostic(
            "info",
            f"{len(inline_rows)} compute pass(es) trace rays through TraceRayInline. "
            "They have no state object or shader table, so describe-state-object and "
            "describe-shader-table cannot see them; read their HLSL with "
            "pass-shader-source --stage CS. Identified by UE5 pass naming, so treat it "
            "as evidence rather than a declaration.",
        )
    return result
