"""One-shot pass -> shader bindings, plus binding trust classification.

Report section 3 documented a three-step manual recipe (list-passes -> read
first_draw_index -> shader-bindings) with five separate pitfalls. Both tools here
collapse that into a single call and make the trust boundary explicit instead of
leaving it to the caller to discover.
"""

from __future__ import annotations

from typing import Any

from ..context import ToolContext
from ..engine.model import RootParameterKind, ShaderStage
from ..errors import invalid_argument, not_found
from ..results import ToolResult
from ._common import (
    PAGE_PARAMS,
    PASS_SELECTOR,
    page_args,
    page_envelope,
    pass_identity,
    resolve_pass,
    tool,
    with_session,
)

_TRUST_NOTE = (
    "Declared registers come from the shader bytecode reflection and are authoritative. "
    "Runtime descriptor-table contents are reconstructed from the C++ export; when PIX did "
    "not record the real descriptor writes for a draw the table reads as filler, so every "
    "table reports a `trust` level rather than pretending the mapping is exact."
)

_STAGES = [stage.value for stage in ShaderStage]


def _pipeline_note(draw) -> str:
    """State, in words, what the pipeline field means for this action.

    A raytracing state object is not modelled as a PSO, so ``pso_id`` is None
    and ``stages`` is empty for a DispatchRays. Returning an empty stages list
    with no explanation would read as "PIX recorded nothing", which is the same
    shape as a real gap -- so the note has to say which one it is.
    """
    if draw.state_object_id is not None:
        return (
            f"This action runs under raytracing state object {draw.state_object_id} "
            "(SetPipelineState1). State objects are not yet modelled, so no shader "
            "stages are reported; the root bindings above are still the compute root "
            "arguments the dispatch reads. Do not infer a shader from pso_id -- it is "
            "null on purpose."
        )
    if draw.pso_id is None:
        return "No pipeline state was bound before this action."
    return ""


def _classify_table(binding, declared_counts: dict[str, int]) -> dict[str, Any]:
    """Decide how much to trust one descriptor table's reconstructed contents."""
    views = binding.resolved_views
    kinds = {view.kind.value for view in views}
    rids = {view.resource_id for view in views if view.resource_id is not None}
    # Count what the descriptors *address*, not merely which resource they name.
    #
    # A single texture rightfully occupies several slots of one table, each at a
    # different mip / array slice / plane. UE5's ReduceHZB dispatch is the
    # reference case: mips 8 and 9 of Nanite.PreviousOccluderHZB are bound as two
    # separate UAVs, which the PIX GUI lists as two rows. Counting distinct
    # resource_ids alone yields 1 there and used to trip the filler heuristic
    # below, reporting "the real descriptors were not recorded" for a table that
    # was in fact recorded perfectly. Keying on the subresource tuple keeps that
    # case honest while still catching true filler, where the same slot value is
    # duplicated across the window at an identical mip and slice.
    subresources = {
        view.subresource_key() for view in views if view.resource_id is not None
    }

    expected = 0
    for kind in kinds:
        expected = max(expected, declared_counts.get(kind, 0))

    # Samplers carry no resource at all, so every resource-based check below is
    # structurally inapplicable to them. Judging a sampler table by
    # distinct_resource_ids always found zero and downgraded a correctly
    # recovered table to `partial`, which is why this branch comes first.
    if kinds == {"SAMPLER"}:
        if not views:
            return {
                "trust": "unavailable",
                "reason": (
                    "PIX recorded no sampler descriptor writes for this table window; the "
                    "shader's declared sampler registers are the only reliable answer."
                ),
                "distinct_resource_ids": [],
            }
        declared_samplers = declared_counts.get("SAMPLER", 0)
        if declared_samplers and len(views) >= declared_samplers:
            return {
                "trust": "reliable",
                "reason": (
                    f"Sampler table resolved {len(views)} descriptor(s), covering the "
                    f"shader's {declared_samplers} declared sampler register(s). Samplers "
                    "reference no resource, so no resource ids are reported."
                ),
                "distinct_resource_ids": [],
            }
        return {
            "trust": "partial",
            "reason": (
                f"Sampler table resolved {len(views)} descriptor(s) against "
                f"{declared_samplers or 'an unknown number of'} declared register(s); "
                "sampler state itself is not reconstructed."
            ),
            "distinct_resource_ids": [],
        }

    if not views:
        trust = "unavailable"
        reason = (
            "PIX recorded no descriptor writes for this table window; the shader's declared "
            "registers are the only reliable answer."
        )
    elif len(subresources) == 1 and expected > 1:
        trust = "filler"
        reason = (
            f"All {len(views)} slot(s) address the same subresource while the shader declares "
            f"{expected}; this window holds PIX initialisation filler, not the real binding."
        )
    elif binding.table_confidence == "exact" and expected and len(views) >= expected:
        trust = "reliable"
        reason = "Table is fully bounded and slot count matches the shader declaration."
    elif (
        binding.table_confidence == "bounded"
        and expected
        and len(views) >= expected
        and len(subresources) >= expected
    ):
        # `bounded` only means the walk stopped before the root parameter's full span,
        # which is the normal case when a root signature declares more slots than the
        # shader uses: UE5 declares 16 UAVs here and the shader binds 8, so stopping at
        # the 9th is correct rather than uncertain. Once the walk yielded one distinct
        # subresource per declared register, the mapping is as confirmed as `exact` is,
        # and grouping it with genuinely unconfirmed reconstructions told the caller to
        # distrust an answer that is in fact sound.
        trust = "reliable"
        distinct_note = (
            "each resolving to a distinct resource"
            if len(rids) >= expected
            else f"resolving to {len(subresources)} distinct subresources of "
            f"{len(rids)} resource(s)"
        )
        reason = (
            f"Table stopped at the shader's {expected} declared register(s), {distinct_note}; "
            "the root signature simply reserves a wider span."
        )
    else:
        trust = "partial"
        reason = (
            f"Table expanded to {len(views)} slot(s) "
            f"(confidence={binding.table_confidence or 'unknown'}); treat register->resource "
            "mapping as unconfirmed."
        )
    payload: dict[str, Any] = {
        "trust": trust,
        "reason": reason,
        "distinct_resource_ids": sorted(rids),
    }
    # Surfaced only when it adds information, i.e. when one resource is bound at
    # several subresources -- precisely the shape that used to be misreported.
    if len(subresources) > len(rids):
        payload["distinct_subresource_count"] = len(subresources)
        payload["subresources"] = [
            label
            for label in (
                view.subresource_label()
                for view in views
                if view.resource_id is not None
            )
            if label
        ]
    return payload


def _collect(capture, draw, stage_filter: str | None, max_views: int) -> dict[str, Any]:
    shaders = [draw.shader(stage_filter)] if stage_filter else draw.shaders
    shaders = [shader for shader in shaders if shader is not None]

    declared_counts = {"SRV": 0, "UAV": 0, "CBV": 0, "SAMPLER": 0}
    stage_rows: list[dict[str, Any]] = []
    for shader in shaders:
        declared = shader.resource_bindings
        for entry in declared:
            ident = entry.get("id", "")
            if ident.startswith("T"):
                declared_counts["SRV"] += 1
            elif ident.startswith("U"):
                declared_counts["UAV"] += 1
            elif ident.startswith("CB"):
                declared_counts["CBV"] += 1
            elif ident.startswith("S"):
                declared_counts["SAMPLER"] += 1
        stage_rows.append(
            {
                "stage": shader.stage.value,
                "shader": shader.to_dict(),
                "declared_count": len(declared),
                "declared_registers": declared,
                "num_threads": shader.num_threads,
            }
        )

    signature = capture.root_signatures.get(draw.root_signature_id or -1)
    tables: list[dict[str, Any]] = []
    root_descriptors: list[dict[str, Any]] = []

    for binding in draw.bindings:
        if binding.kind is RootParameterKind.DESCRIPTOR_TABLE:
            row = binding.to_dict(max_views=max_views)
            row.update(_classify_table(binding, declared_counts))
            parameter = signature.parameter(binding.root_index) if signature else None
            if parameter is not None:
                row["declared_ranges"] = parameter.ranges
                row["declared_descriptor_count"] = parameter.num_descriptors
            resolved = []
            for view in binding.resolved_views[:max_views]:
                item = view.to_dict()
                resource = (
                    capture.resource(view.resource_id)
                    if view.resource_id is not None
                    else None
                )
                if resource is not None:
                    item["resource"] = resource.to_dict()
                resolved.append(item)
            row["resolved"] = resolved
            tables.append(row)
        else:
            entry: dict[str, Any] = {
                "root_index": binding.root_index,
                "binding_kind": binding.kind.value,
                "resource_id": binding.resource_id,
                "byte_offset": binding.va_offset,
                "trust": "reliable" if binding.resource_id is not None else "unavailable",
            }
            resource = (
                capture.resource(binding.resource_id)
                if binding.resource_id is not None
                else None
            )
            if resource is not None:
                entry["resource"] = resource.to_dict()
            root_descriptors.append(entry)

    return {
        "draw_index": draw.index,
        "global_id": draw.global_id,
        "api": draw.api,
        "effective_kind": draw.effective_kind.value,
        "pso_id": draw.pso_id,
        "state_object_id": draw.state_object_id,
        "root_signature_id": draw.root_signature_id,
        "stages": stage_rows,
        "declared_totals": declared_counts,
        "root_descriptors": root_descriptors,
        "descriptor_tables": tables,
        "descriptor_heap_ids": draw.descriptor_heap_ids,
        "pipeline_note": _pipeline_note(draw),
    }


@tool(
    name="pass-bindings",
    summary=(
        "Shader bindings for a whole pass in one call: resolves the pass by name or index, "
        "picks its representative draws, and returns each shader's declared registers "
        "together with the runtime bindings and how much to trust them."
    ),
    category="shaders",
    parameters=with_session(
        PASS_SELECTOR,
        stage={
            "type": "string",
            "enum": _STAGES,
            "description": "Restrict to one shader stage, e.g. CS.",
        },
        max_views={
            "type": "integer",
            "description": "Views to list per descriptor table. Default 128 (large enough for UE5 tables).",
        },
        per_pso={
            "type": "boolean",
            "description": "Report one representative draw per distinct PSO. Default true.",
        },
        max_draws={"type": "integer", "description": "Cap on reported draws. Default 8."},
        all_matches={
            "type": "boolean",
            "description": "When the name matches several passes, report all of them.",
        },
    ),
    returns=(
        "Pass identity, per-stage declared registers (authoritative), runtime root "
        "descriptors and descriptor tables each tagged with a trust level."
    ),
    examples=[
        'pix-tool-set pass-bindings --pass-name TileClassificationBuildLists --stage CS',
        "pix-tool-set pass-bindings --pass-index 270",
        "pix-tool-set pass-bindings --queue-id 18704",
        'pix-tool-set pass-bindings --pass-name TileClassification --all-matches',
    ],
    notes=_TRUST_NOTE,
)
def pass_bindings(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    max_views = int(args.get("max_views") or 128)
    max_draws = int(args.get("max_draws") or 8)
    per_pso = args.get("per_pso")
    per_pso = True if per_pso is None else bool(per_pso)
    stage_filter = args.get("stage")

    if bool(args.get("all_matches")) and args.get("pass_name"):
        needle = str(args["pass_name"]).lower()
        entries = [p for p in capture.passes if needle in p["name"].lower()]
        if not entries:
            raise not_found("pass", args["pass_name"], "Run list-passes to see valid names.")
    else:
        entries = [resolve_pass(capture, args)]

    pass_payloads: list[dict[str, Any]] = []
    trust_tally: dict[str, int] = {}

    for entry in entries:
        marker_path = tuple(entry["marker_path"])
        draws = [d for d in capture.draw_calls if d.marker_path == marker_path]
        if per_pso:
            chosen: list[Any] = []
            seen: set[int | None] = set()
            for draw in draws:
                if draw.pso_id in seen:
                    continue
                seen.add(draw.pso_id)
                chosen.append(draw)
        else:
            chosen = draws

        draw_rows = []
        for draw in chosen[:max_draws]:
            row = _collect(capture, draw, stage_filter, max_views)
            for table in row["descriptor_tables"]:
                trust_tally[table["trust"]] = trust_tally.get(table["trust"], 0) + 1
            for descriptor in row["root_descriptors"]:
                trust_tally[descriptor["trust"]] = trust_tally.get(descriptor["trust"], 0) + 1
            draw_rows.append(row)

        pass_payloads.append(
            {
                "pass_index": entry["pass_index"],
                "name": entry["name"],
                "marker_path": entry["marker_path"],
                "event_count": entry["event_count"],
                "draw_count": entry["draw_count"],
                "dispatch_count": entry["dispatch_count"],
                "distinct_pso_ids": entry["pso_ids"],
                "first_global_id": entry.get("first_global_id"),
                "first_queue_id": entry.get("first_queue_id"),
                "marker_queue_id": entry.get("marker_queue_id"),
                "total_draws_in_pass": len(draws),
                "draws_reported": len(draw_rows),
                "draws": draw_rows,
            }
        )

    result = ToolResult.success(
        {
            "passes": pass_payloads,
            "pass_count": len(pass_payloads),
            "trust_summary": trust_tally,
            "trust_levels": {
                "reliable": "Value is taken straight from the recorded call or matches the shader declaration.",
                "partial": "Reconstructed but unconfirmed; do not rely on register->resource mapping.",
                "filler": "Window holds PIX initialisation filler; the real descriptors were not recorded.",
                "unavailable": "No descriptor data recorded for this table.",
            },
        }
    )
    if trust_tally.get("filler") or trust_tally.get("unavailable"):
        result.degrade(
            "Some runtime descriptor tables could not be recovered from the export; "
            "use the declared_registers of each stage as the authoritative answer.",
            filler_tables=trust_tally.get("filler", 0),
            unavailable_tables=trust_tally.get("unavailable", 0),
        )
    if len(entries) == 1 and pass_payloads and pass_payloads[0]["total_draws_in_pass"] > max_draws:
        result.add_diagnostic(
            "info",
            f"Pass has {pass_payloads[0]['total_draws_in_pass']} draws; reported "
            f"{pass_payloads[0]['draws_reported']}. Raise --max-draws or set --per-pso false for more.",
        )
    return result


@tool(
    name="find-pass",
    summary=(
        "Resolve a pass name to the identifiers other tools need: pass index, draw index, "
        "global id and PSO ids. Lists every match so ambiguous names are obvious."
    ),
    category="events",
    parameters=with_session(
        PAGE_PARAMS,
        name={"type": "string", "description": "Pass name or substring to look up."},
        global_id={
            "type": "integer",
            "description": (
                "PIX Global ID of any event inside the pass. Resolves across all "
                "queues, including passes the exported event list does not cover. "
                "The pass is found by exact marker_path match, never by gid range."
            ),
        },
        queue_id={
            "type": "integer",
            "description": (
                "Exported event list row id, for a marker or an action alike. Only passes "
                "on the exported queue have one; --name or --global-id reaches every pass "
                "and is the way in when a pass is missing from the event list."
            ),
        },
    ),
    returns="Every matching pass with the ids needed by draw-state / shader-bindings.",
    examples=[
        'pix-tool-set find-pass --name TileClassificationBuildLists',
        "pix-tool-set find-pass --queue-id 18704",
        "pix-tool-set find-pass --global-id 5099",
    ],
)
def find_pass(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    offset, limit = page_args(args, default_limit=25)

    def row(entry: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "pass_index": entry["pass_index"],
            "name": entry["name"],
            "marker_path": entry["marker_path"],
            "subsystem": entry["marker_path"][-2] if len(entry["marker_path"]) > 1 else "",
            "draw_index": entry["first_draw_index"],
            "last_draw_index": entry["last_draw_index"],
            "global_id": entry["first_global_id"],
            "queue_id": entry.get("first_queue_id"),
            "marker_queue_id": entry.get("marker_queue_id"),
            "event_count": entry["event_count"],
            "draw_count": entry["draw_count"],
            "dispatch_count": entry["dispatch_count"],
            "pso_ids": entry["pso_ids"],
        }
        # find-pass exists to hand out identifiers, so it is the one place a null id is
        # most likely to be read as "this pass is broken". Borrow the shared explanation
        # rather than leaving the caller to infer why two of the four ids are empty.
        identity = pass_identity(entry)
        if "queue_id_unavailable" in identity:
            payload["queue_id_unavailable"] = identity["queue_id_unavailable"]
        return payload

    global_id = args.get("global_id")
    if global_id is not None:
        entry = capture.find_pass_by_event(global_id=global_id)
        if entry is None:
            # The id may name a real event whose marker has no draws (so no pass
            # entry was built). That is a different case from "id is not in this
            # capture at all", and the caller's next step differs: the former still
            # has a marker context worth naming.
            ev = capture.event_by_global_id(int(global_id))
            if ev is not None:
                marker = ev.marker_path[-1] if ev.marker_path else ev.name
                raise not_found(
                    "pass",
                    f"global_id={global_id}",
                    f"Global ID {global_id} is {ev.name!r} under marker {marker!r}, but "
                    f"that marker contains no draw calls, so no pass entry was built for "
                    f"it. Use --name to find a neighbouring pass, or list-draw-calls to "
                    f"see the draws in the surrounding passes.",
                )
            cmd = capture.command_by_global_id(int(global_id)) if hasattr(capture, "command_by_global_id") else None
            if cmd is not None:
                raise not_found(
                    "pass",
                    f"global_id={global_id}",
                    f"Global ID {global_id} is a {cmd['api']} command not enclosed by a "
                    f"pass marker. Use --name to find a pass by name.",
                )
            raise not_found(
                "pass",
                f"global_id={global_id}",
                f"No event carries Global ID {global_id}, and it is not an ExecuteIndirect "
                f"expansion. Use --name to find a pass by name.",
            )
        return ToolResult.success(
            {
                "query": f"global_id={global_id}",
                "matches": [row(entry)],
                **page_envelope(1, 1, limit, 1),
            }
        )

    queue_id = args.get("queue_id")
    if queue_id is not None:
        entry = capture.find_pass_by_event(queue_id=queue_id)
        label = f"queue_id={queue_id}"
        if entry is None:
            raise not_found(
                "pass",
                label,
                "Use locate-event to confirm the id exists in this capture.",
            )
        return ToolResult.success(
            {
                "query": label,
                "matches": [row(entry)],
                "next_step": (
                    "Feed draw_index into `pass-bindings`, `draw-state` or `shader-bindings`, "
                    "or pass the same --queue-id straight to `pass-bindings`."
                ),
                **page_envelope(1, 0, limit, 1),
            }
        )

    if not args.get("name"):
        raise invalid_argument("name/global_id/queue_id", "provide one of them")
    needle = str(args["name"]).lower()

    matches = [row(e) for e in capture.passes if needle in e["name"].lower()]

    total = len(matches)
    window = matches[offset : offset + limit] if limit else matches[offset:]
    if not matches:
        raise not_found("pass", args["name"], "Run list-passes to browse available names.")

    result = ToolResult.success(
        {
            "query": args["name"],
            "matches": window,
            "next_step": (
                "Feed draw_index into `pass-bindings`, `draw-state` or `shader-bindings`."
            ),
            **page_envelope(total, offset, limit, len(window)),
        }
    )
    if total > 1:
        result.add_diagnostic(
            "info",
            f"{total} passes share this name; use marker_path or subsystem to pick the right one.",
        )
    return result
