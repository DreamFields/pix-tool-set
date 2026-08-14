"""Contract check for the raytracing tools: fields, degrade codes, pagination.

Separate from the parser tests because a correct parse can still be reported
badly. What is checked here is that each tool keeps the promises a caller relies
on when it cannot see the export: that a stage is always accompanied by its
source, that an absent callable table reads as null rather than empty, that a
raytracing action no longer produces the old ``state_object_unmodelled`` degrade,
and that an ambiguous hit-group mapping is refused instead of guessed.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pix_tool_set import call_tool  # noqa: E402

SESSION = "Tiled"
RAY_DRAW = 2705
RAY_DRAW_HIT_LIGHTING = 2711

failures: list[str] = []
checks = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(label)


def codes(result: dict) -> list[str]:
    return [entry.get("reason", "") for entry in result.get("diagnostics", [])]


def main() -> int:
    print("1. list-raytracing-work")
    result = call_tool("list-raytracing-work", {"session": SESSION})
    data = result["data"]
    check("status success", result["status"] == "success", result["status"])
    check("five entries", data["total"] == 5, str(data["total"]))
    kinds = [row["work"] for row in data["raytracing_work"]]
    # Builds must precede dispatches: the order is the point of the timeline.
    check(
        "builds come before dispatches",
        kinds
        == [
            "acceleration_structure_build",
            "acceleration_structure_build",
            "acceleration_structure_build",
            "dispatch_rays",
            "dispatch_rays",
        ],
        str(kinds),
    )
    dispatches = [row for row in data["raytracing_work"] if row["work"] == "dispatch_rays"]
    check(
        "each dispatch carries draw_index, global_id and state_object_id",
        all(
            row.get("draw_index") is not None
            and row.get("global_id") is not None
            and row.get("state_object_id") is not None
            for row in dispatches
        ),
    )
    # The API name is ExecuteIndirect while the effective kind is dispatch_rays;
    # both are reported so neither reading of the frame is contradicted.
    check(
        "api stays ExecuteIndirect while effective_kind is dispatch_rays",
        all(
            row["api"] == "ExecuteIndirect" and row["effective_kind"] == "dispatch_rays"
            for row in dispatches
        ),
        str([(row["api"], row["effective_kind"]) for row in dispatches]),
    )
    check(
        "ray counts are reported",
        [row["ray_count"] for row in dispatches] == [232, 2],
        str([row["ray_count"] for row in dispatches]),
    )

    print("\n2. describe-state-object")
    result = call_tool(
        "describe-state-object", {"session": SESSION, "state_object_id": 3930}
    )
    data = result["data"]
    check("status success", result["status"] == "success", result["status"])
    check("84 exports after expansion", data["total"] == 84, str(data["total"]))
    check(
        "every export states how its stage was derived",
        all(
            export.get("stage") is None or export.get("stage_source")
            for export in data["exports"]
        ),
    )
    check("stage_source_note present", bool(data.get("stage_source_note")))
    check(
        "consumers name the dispatch that binds it",
        [row["draw_index"] for row in data["consumers"]] == [RAY_DRAW_HIT_LIGHTING],
        str(data["consumers"]),
    )
    check(
        "resolved_state_object_ids includes the object itself plus its collections",
        data["resolved_state_object_ids"][0] == 3930
        and len(data["resolved_state_object_ids"]) == 67,
        str(len(data["resolved_state_object_ids"])),
    )

    paged = call_tool(
        "describe-state-object",
        {"session": SESSION, "state_object_id": 3930, "limit": 10, "offset": 80},
    )
    check(
        "pagination reports the true total, not the page size",
        paged["data"]["total"] == 84 and paged["data"]["returned"] == 4,
        f"total={paged['data']['total']} returned={paged['data']['returned']}",
    )
    check("last page has no next_offset", paged["data"]["next_offset"] is None)

    unexpanded = call_tool(
        "describe-state-object",
        {"session": SESSION, "state_object_id": 3930, "expand": False},
    )
    # Zero here is the honest answer to a different question, so it must not be a
    # degrade -- but it must come with the explanation, or it reads as a bug.
    check(
        "expand=false returns zero exports without degrading",
        unexpanded["data"]["total"] == 0 and unexpanded["status"] == "success",
        f"{unexpanded['data']['total']} / {unexpanded['status']}",
    )
    check(
        "and explains why zero is correct there",
        any("expand=false" in entry["message"] for entry in unexpanded["diagnostics"]),
    )

    by_draw = call_tool(
        "describe-state-object", {"session": SESSION, "draw_index": RAY_DRAW}
    )
    check(
        "resolvable from a draw selector",
        by_draw["data"]["state_object"]["state_object_id"] == 3891,
        str(by_draw["data"]["state_object"]["state_object_id"]),
    )

    print("\n3. describe-shader-table")
    result = call_tool(
        "describe-shader-table", {"session": SESSION, "draw_index": RAY_DRAW}
    )
    data = result["data"]
    sbt = data["shader_binding_table"]
    check("status success", result["status"] == "success", result["status"])
    check("dispatch block present", sbt["dispatch"]["ray_count"] == 232)
    check(
        "absent callable table is null, not an empty region",
        sbt["tables"]["callable"] is None,
        repr(sbt["tables"]["callable"]),
    )
    check(
        "region size and buffer size are separate fields",
        sbt["tables"]["raygen"]["size_in_bytes"] == 64
        and sbt["tables"]["raygen"]["buffer_size_in_bytes"] == 2715136,
    )
    check(
        "every record says whether the dispatch reads it",
        all("in_declared_region" in record for record in data["records"]),
    )
    out_of_region = [
        record for record in data["records"] if not record["in_declared_region"]
    ]
    check(
        "records outside the declared region carry an explanation",
        all(record.get("note") for record in out_of_region),
        str(len(out_of_region)),
    )
    filtered = call_tool(
        "describe-shader-table",
        {"session": SESSION, "indirect_buffer_key": "1415_1", "table": "hit_group"},
    )
    check(
        "table filter narrows to eight hit group records",
        filtered["data"]["total"] == 8,
        str(filtered["data"]["total"]),
    )

    print("\n4. shader-bindings on a ray dispatch")
    result = call_tool("shader-bindings", {"session": SESSION, "draw_index": RAY_DRAW})
    data = result["data"]
    # This is the regression that matters: it used to be partial with an empty
    # stage list and a state_object_unmodelled reason.
    check("status success, no longer partial", result["status"] == "success", result["status"])
    check(
        "the old state_object_unmodelled degrade is gone",
        "state_object_unmodelled" not in codes(result),
        str(codes(result)),
    )
    check("stages are populated", bool(data["stages"]), str(data["stages"]))
    check("17 exports listed", len(data["exports"]) == 17, str(len(data["exports"])))
    # Global and local bindings must stay in separate fields, and the legacy
    # merged field must not reappear.
    check(
        "global root bindings are named as such",
        "global_root_bindings" in data and "root_bindings" not in data,
        str(sorted(k for k in data if "binding" in k)),
    )
    check(
        "local root bindings are grouped per record",
        all(
            "shader_identifier" in row and "root_constants" in row
            for row in data["local_root_bindings_by_record"]
        ),
    )
    check("binding model is explained", bool(data.get("binding_model_note")))

    print("\n4b. raygen record-panel bindings align with the PIX GUI (gid 5367)")
    # The RayGen Root Signature panel for ReflectionHardwareRayTracingRGS lists
    # nine CBVs by (register, space) plus two static samplers at space=1000.
    result = call_tool(
        "shader-bindings", {"session": SESSION, "draw_index": RAY_DRAW_HIT_LIGHTING}
    )
    data = result["data"]
    by_name = {export["name"]: export for export in data["exports"]}
    raygen = by_name.get("RayGen_fb3c7b0c9e02fb73")
    check("target raygen export carries a binding view", raygen and "bindings" in raygen)
    if raygen and "bindings" in raygen:
        cb = raygen["bindings"]["cbuffers"]
        expected_cbs = [
            ("_RootShaderParameters", 0, 1),
            ("SceneTexturesStruct", 1, 1),
            ("View", 1, 4),
            ("LumenCardScene", 2, 1),
            ("ReflectionStruct", 3, 1),
            ("ReflectionCaptureSM5", 4, 1),
            ("FogStruct", 5, 1),
            ("ForwardLightStruct", 6, 1),
            ("RaytracingLightGridData", 7, 1),
        ]
        got_cbs = [(row["name"], row["register"], row["space"]) for row in cb]
        check("nine CBVs in (register, space) order", got_cbs == expected_cbs, str(got_cbs))
        sm = raygen["bindings"]["static_samplers"]
        expected_sm = [
            ("D3DStaticPointClampedSampler", 1, 1000),
            ("D3DStaticBilinearClampedSampler", 3, 1000),
        ]
        got_sm = [(row["name"], row["slot"], row["space"]) for row in sm]
        check(
            "two static samplers at space=1000",
            got_sm == expected_sm,
            str(got_sm),
        )

    print("\n5. pipeline-state falls through to the state object")
    result = call_tool("pipeline-state", {"session": SESSION, "draw_index": RAY_DRAW})
    data = result["data"]
    check("resolved_kind is raytracing", data.get("resolved_kind") == "raytracing")
    check("pipeline_state is null", data.get("pipeline_state") is None)
    check(
        "state object is returned instead",
        data.get("state_object", {}).get("state_object_id") == 3891,
    )
    check("shader count reported", data.get("shader_count") == 17, str(data.get("shader_count")))
    check("points at describe-state-object", "describe-state-object" in data.get("note", ""))

    print("\n6. list-pipeline-states includes state objects")
    result = call_tool("list-pipeline-states", {"session": SESSION, "limit": 1})
    check(
        "81 state objects listed alongside the PSOs",
        result["data"]["state_object_count"] == 81,
        str(result["data"]["state_object_count"]),
    )
    ray_only = call_tool(
        "list-pipeline-states", {"session": SESSION, "kind": "raytracing", "limit": 1}
    )
    check(
        "kind=raytracing returns no PSO rows",
        ray_only["data"]["total"] == 0 and ray_only["data"]["state_object_count"] == 81,
        f"psos={ray_only['data']['total']} sos={ray_only['data']['state_object_count']}",
    )
    used = call_tool(
        "list-pipeline-states",
        {"session": SESSION, "kind": "raytracing", "used_only": True, "limit": 1},
    )
    check(
        "used_only narrows to the two dispatched pipelines",
        used["data"]["state_object_count"] == 2,
        str(used["data"]["state_object_count"]),
    )

    print("\n7. analyze-acceleration-structures")
    result = call_tool("analyze-acceleration-structures", {"session": SESSION})
    data = result["data"]
    check("status success", result["status"] == "success", result["status"])
    check(
        "geometry availability is stated explicitly",
        data["geometry_availability"]["triangle_counts_available"] is False
        and bool(data["geometry_availability"]["reason"]),
    )
    check(
        "the limitation is also raised as a diagnostic",
        any("driver-private" in entry["message"] for entry in result["diagnostics"]),
    )
    ambiguous = call_tool(
        "analyze-acceleration-structures",
        {"session": SESSION, "resolve_hit_groups": True},
    )
    # Two tables mean the mapping is ambiguous; refusing is the correct answer.
    check(
        "ambiguous hit-group mapping is refused, not guessed",
        ambiguous["status"] == "partial"
        and "hit_group_resolution_unavailable" in codes(ambiguous),
        str(codes(ambiguous)),
    )
    resolved = call_tool(
        "analyze-acceleration-structures",
        {
            "session": SESSION,
            "resolve_hit_groups": True,
            "indirect_buffer_key": "1415_2",
        },
    )
    check(
        "with a table named, instances resolve to hit groups",
        resolved["status"] == "success"
        and resolved["data"]["builds"][0]["instances_resolved"][0]["hit_group"]
        == "HitGroup_ee4e6808208cbd63",
    )

    print("\n8. errors name the next step")
    # call_tool returns an error envelope rather than raising, so the assertions
    # read the envelope. What matters is not that it failed but that the message
    # tells the caller what to do next.
    unknown = call_tool(
        "describe-state-object", {"session": SESSION, "state_object_id": 999999}
    )
    error = unknown.get("error") or {}
    # The code is derived from the resource kind, so it is "state object_not_found"
    # here, matching the existing convention (cf. "pipeline state_not_found").
    check(
        "unknown state object id is a not-found error",
        unknown["status"] == "error" and error.get("code", "").endswith("_not_found"),
        f"{unknown['status']} / {error.get('code')}",
    )
    check(
        "and the message names the valid id range",
        "Known ids" in str(error),
        str(error.get("message", ""))[:80],
    )
    non_ray = call_tool("describe-shader-table", {"session": SESSION, "draw_index": 0})
    error = non_ray.get("error") or {}
    check(
        "a non-raytracing draw is told so, not given an empty table",
        non_ray["status"] == "error"
        and error.get("code", "").endswith("_not_found")
        and "not a raytracing dispatch" in str(error),
        str(error.get("suggestion", ""))[:80],
    )
    check(
        "and is pointed at list-raytracing-work",
        "list-raytracing-work" in str(error),
    )

    return _report()


def _report() -> int:
    print()
    print("=" * 68)
    print(f"checks: {checks - len(failures)}/{checks} passed")
    if failures:
        for label in failures:
            print(f"  FAILED: {label}")
        print("RESULT: FAIL")
        return 1
    print("PASS: raytracing tools honour their contracts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
