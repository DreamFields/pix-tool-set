"""Raytracing binding views shared by ``shader-bindings`` and ``pass-bindings``.

This module exists because of a concrete failure, not for tidiness. ``pass-bindings``
used to run the rasterisation path unconditionally: it read ``draw.shaders``, which is
empty for every ray dispatch, and then explained the hole with the sentence "State
objects are not yet modelled". By then state objects *were* modelled -- the DXR work
had landed and ``shader-bindings`` was already resolving exports, hit groups and the
RayGen record panel off the same capture. So the toolkit held the answer and told the
caller it did not exist, which is the one failure mode worse than an error: a confident
"nothing here" that stops the investigation.

Keeping the view in one place is what prevents that from recurring. The alternative --
``pass_binding_tools`` importing a private ``_raytracing_bindings`` out of
``shader_tools`` -- would couple two tool modules through a private name and leave the
next DXR improvement to be applied twice, which is how the two drifted apart in the
first place.

The global / local split is preserved verbatim from the original implementation and is
load-bearing, not cosmetic. Global root arguments are bound once on the command list
for the whole dispatch; local root arguments come from individual shader records in the
binding table. Concatenating them would report one dispatch as having dozens of
bindings and would destroy the only information that makes a local binding meaningful:
which shader record it belongs to.
"""

from __future__ import annotations

from typing import Any

# Reused wherever a payload reports zero shader stages for a raytracing action, so the
# reading is never left to the caller. The old note said state objects were unmodelled,
# which was stale and actively misleading; this one states the true reason (a DXIL
# library declares no stages the way a PSO does) and names the field that resolves it.
RAYTRACING_STAGE_NOTE = (
    "This action runs under a raytracing state object (SetPipelineState1), so pso_id is "
    "null by design and the PSO-shaped `stages` list is empty. That is the pipeline's "
    "shape, not missing data: a DXR pipeline is a set of DXIL library exports, reported "
    "under `exports` / `hit_groups` with a derived stage each. Do not read the empty "
    "`stages` as an unmodelled pipeline."
)


def export_binding_view(capture, export) -> dict[str, Any]:
    """The PIX record-panel view of one DxilExport's declared bindings.

    The Resource Bindings table in the DXIL disassembly is split into the two groups the
    PIX Root Signature panel shows: CBVs, each ``[register, space]`` naming the cbuffer
    it declares, and static samplers, each ``[slot, space]``. The ``space``/``register``
    come straight from the ``hlsl_bind`` cell (``cb0,space1`` / ``s1,space1000``), and an
    absent space reads as 0 to match PIX, which always prints the space.

    The names can only come from here. A global root signature parameter carries a
    register and a space but no semantic name, and a raytracing shader is not a PSO, so
    ``draw.shader()`` cannot reach it -- reflecting the DXIL library export directly is
    the only path to ``_RootShaderParameters`` and friends.
    """
    bindings = capture.export_resource_bindings(export)
    cbuffers: list[dict[str, Any]] = []
    samplers: list[dict[str, Any]] = []
    for entry in bindings:
        kind = (entry.get("type") or "").lower()
        if kind == "cbuffer":
            cbuffers.append(
                {
                    "name": entry.get("name"),
                    "register": entry.get("register"),
                    "space": entry.get("register_space"),
                }
            )
        elif kind == "sampler":
            samplers.append(
                {
                    "name": entry.get("name"),
                    "slot": entry.get("register"),
                    "space": entry.get("register_space"),
                }
            )
    # PIX lists the CBVs ordered by (register, space), not by declaration order; the
    # table's own order interleaves spaces (View's cb1,space4 precedes
    # SceneTexturesStruct's cb1,space1). Sorting keeps the output identical to the panel.
    cbuffers.sort(key=lambda row: (row["register"], row["space"]))
    samplers.sort(key=lambda row: (row["slot"], row["space"]))
    return {
        "cbuffers": cbuffers,
        "static_samplers": samplers,
        "binding_count": len(bindings),
    }


def global_root_binding_rows(capture, draw, max_views: int) -> list[dict[str, Any]]:
    """Root arguments bound on the command list for the whole ray dispatch."""
    signature = capture.root_signatures.get(draw.root_signature_id or -1)
    rows: list[dict[str, Any]] = []
    for binding in draw.bindings:
        row = binding.to_dict(max_views=max_views)
        parameter = signature.parameter(binding.root_index) if signature else None
        if parameter is not None:
            row["root_parameter"] = parameter.to_dict()
        resolved = []
        for view in binding.resolved_views[:max_views]:
            entry = view.to_dict()
            resource = (
                capture.resource(view.resource_id)
                if view.resource_id is not None
                else None
            )
            if resource is not None:
                entry["resource"] = resource.to_dict()
            resolved.append(entry)
        row["resolved"] = resolved
        rows.append(row)
    return rows


def local_root_binding_rows(capture, state_object, sbt) -> list[dict[str, Any]]:
    """Local root arguments, grouped per shader record.

    The grouping is the payload: a local binding only means something together with the
    shader whose record carries it. Records written past the region this dispatch reads
    are skipped, because they are not executed by it.
    """
    rows: list[dict[str, Any]] = []
    for record in sbt.records:
        if not record.root_constants and not record.root_gpuvas:
            continue
        if not record.in_declared_region:
            continue
        rows.append(
            {
                "table": record.table,
                "offset": record.offset,
                "shader_identifier": record.shader_identifier,
                "identifier_kind": state_object.identifier_owner(record.shader_identifier),
                "root_constants": list(record.root_constants),
                "root_gpuvas": [
                    {
                        "resource_id": resource_id,
                        "byte_offset": byte_offset,
                        "resource": (
                            capture.resource(resource_id).to_dict()
                            if capture.resource(resource_id) is not None
                            else None
                        ),
                    }
                    for resource_id, byte_offset in record.root_gpuvas
                ],
            }
        )
    return rows


def raytracing_binding_payload(
    capture, draw, *, max_views: int = 128
) -> tuple[dict[str, Any], list[tuple[str, str, dict[str, Any]]]]:
    """Everything a ray dispatch has bound, plus the degradations to report.

    Returns ``(payload, degradations)`` rather than a ToolResult so both callers can
    splice it into their own envelope shape -- ``shader-bindings`` answers about one
    action, ``pass-bindings`` about every representative draw in a pass. Each
    degradation is ``(message, reason, extra)`` and must be surfaced; swallowing one
    turns a partial answer back into a confident wrong one.
    """
    state_object = draw.state_object
    sbt = draw.shader_binding_table
    signature = capture.root_signatures.get(draw.root_signature_id or -1)
    degradations: list[tuple[str, str, dict[str, Any]]] = []

    payload: dict[str, Any] = {
        "state_object_id": draw.state_object_id,
        "root_signature": signature.to_dict() if signature else None,
        # Named global_* so no caller can mistake this for the dispatch's whole
        # binding set.
        "global_root_bindings": global_root_binding_rows(capture, draw, max_views),
        "stages": [],
        "raytracing_stage_note": RAYTRACING_STAGE_NOTE,
    }

    if state_object is None:
        degradations.append(
            (
                f"State object {draw.state_object_id} is referenced by this action but was "
                f"not found in CreatePSOs.cpp, so no shaders can be listed. The global "
                f"root bindings above are still valid.",
                "state_object_missing_from_export",
                {},
            )
        )
        return payload, degradations

    exports = state_object.resolved_exports
    payload["state_object"] = state_object.to_dict()
    payload["stages"] = sorted(
        {export.stage.value for export in exports if export.stage is not None}
    )
    export_rows = [export.to_dict() for export in exports]
    # Reflect each raygen export's DXIL so the record panel can be reproduced.
    for export, row in zip(exports, export_rows):
        if export.stage is not None and export.stage.value == "RAYGEN":
            row["bindings"] = export_binding_view(capture, export)
    payload["exports"] = export_rows
    payload["hit_groups"] = [
        group.to_dict() for group in state_object.resolved_hit_groups
    ]
    payload["stage_source_note"] = (
        "Stages on the exports above are derived, not declared; each carries a "
        "stage_source saying how. Only 'hit_group' is stated by the export."
    )

    if sbt is None:
        degradations.append(
            (
                "No shader binding table could be located for this dispatch, so the export "
                "list is every shader the pipeline could launch, not the ones this dispatch "
                "selected. Local root arguments are unavailable for the same reason.",
                "shader_binding_table_unresolved",
                {},
            )
        )
        return payload, degradations

    payload["shader_binding_table"] = sbt.to_dict()
    payload["local_root_bindings_by_record"] = local_root_binding_rows(
        capture, state_object, sbt
    )
    payload["binding_model_note"] = (
        "global_root_bindings are bound once on the command list and apply to the whole "
        "dispatch. local_root_bindings_by_record come from individual shader records and "
        "apply only to the shader that record names. The two sets are deliberately not "
        "combined."
    )

    unresolved = sbt.unresolved_identifiers
    if unresolved:
        degradations.append(
            (
                f"{len(unresolved)} shader record(s) name identifiers this state object "
                f"does not export: {unresolved[:8]}. The shader list may be incomplete.",
                "shader_record_identifier_unresolved",
                {},
            )
        )
    return payload, degradations
