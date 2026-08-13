"""Check the parsed raytracing state objects against the Tiled.wpix baseline.

Every number here was read directly out of ``CreatePSOs.cpp``, so a failure means
the parser drifted rather than that the capture changed.

The important assertions are the negative and the structural ones. RTPSO 3930
declares zero exports of its own and reaches all of its shaders through 66
EXISTING_COLLECTION references built up over three AddToStateObject segments, so
a parser that reads only the final desc returns "this pipeline has no shaders" --
a wrong answer that looks like a valid one. Two checks guard that specifically:
the expanded list must be non-empty, and it must NOT contain exports from a
collection the pipeline does not reference (which would mean the expansion had
degenerated into "merge everything").
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pix_tool_set.engine.capture import Capture  # noqa: E402
from pix_tool_set.engine.model import StateObjectType  # noqa: E402
from pix_tool_set.session import SessionStore  # noqa: E402

SESSION = "Tiled"

EXPECTED_OBJECT_COUNT = 81
EXPECTED_COLLECTIONS = 79
EXPECTED_PIPELINES = 2

failures: list[str] = []
checks = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    status = "ok  " if ok else "FAIL"
    print(f"  {status} {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(label)


def main() -> int:
    record = SessionStore().resolve(session=SESSION)
    capture = Capture(
        Path(record.capture_path) if record.capture_path else None,
        Path(record.export_dir),
        Path(record.event_csv) if record.event_csv else None,
    )
    objects = capture.state_objects

    print("1. object inventory")
    check(
        f"{EXPECTED_OBJECT_COUNT} state objects parsed",
        len(objects) == EXPECTED_OBJECT_COUNT,
        str(len(objects)),
    )
    collections = [
        o for o in objects.values() if o.type is StateObjectType.COLLECTION
    ]
    pipelines = [
        o for o in objects.values() if o.type is StateObjectType.RAYTRACING_PIPELINE
    ]
    check(
        f"{EXPECTED_COLLECTIONS} collections", len(collections) == EXPECTED_COLLECTIONS,
        str(len(collections)),
    )
    # Two, not four: the export has four RAYTRACING_PIPELINE *desc segments*, but
    # three of them belong to object 3930's AddToStateObject chain.
    check(
        f"{EXPECTED_PIPELINES} raytracing pipelines",
        len(pipelines) == EXPECTED_PIPELINES,
        str(len(pipelines)),
    )

    print("\n2. collection 3892 field by field")
    obj = objects.get(3892)
    if obj is None:
        check("state object 3892 exists", False)
        return _report()
    check("type is collection", obj.type is StateObjectType.COLLECTION, obj.type.value)
    check("max_payload_size == 16", obj.max_payload_size == 16, str(obj.max_payload_size))
    check(
        "max_attribute_size == 8", obj.max_attribute_size == 8, str(obj.max_attribute_size)
    )
    check(
        "max_recursion_depth == 1",
        obj.max_recursion_depth == 1,
        str(obj.max_recursion_depth),
    )
    check(
        "global root signature 3889",
        obj.global_root_signature_id == 3889,
        str(obj.global_root_signature_id),
    )
    check(
        "allow_state_object_additions flag",
        "allow_state_object_additions" in obj.flags,
        str(obj.flags),
    )
    by_name = {export.name: export for export in obj.exports}
    check("two exports", len(obj.exports) == 2, str(len(obj.exports)))
    chs = by_name.get("CHS_b5acc26ab7153489")
    ahs = by_name.get("AHS_b5acc26ab7153489")
    check(
        "CHS original name is the HLSL entry point",
        chs is not None and chs.original_name == "LumenHardwareRayTracingMaterialCHS",
        chs.original_name if chs else "missing",
    )
    check(
        "AHS original name is the HLSL entry point",
        ahs is not None and ahs.original_name == "LumenHardwareRayTracingMaterialAHS",
        ahs.original_name if ahs else "missing",
    )
    # Stage from hit-group membership is a fact, not an inference; the source must
    # say so, otherwise a caller cannot tell it from a name-prefix guess.
    check(
        "CHS stage derived from the hit group, not the name",
        chs is not None and chs.stage_source == "hit_group",
        chs.stage_source if chs else "missing",
    )
    check(
        "local root signature 3893 on both exports",
        chs is not None
        and ahs is not None
        and chs.local_root_signature_id == 3893
        and ahs.local_root_signature_id == 3893,
    )
    group = obj.hit_groups[0] if obj.hit_groups else None
    check("one hit group", len(obj.hit_groups) == 1, str(len(obj.hit_groups)))
    check(
        "hit group name",
        group is not None and group.name == "HitGroup_b5acc26ab7153489",
        group.name if group else "missing",
    )
    check("hit group type triangles", group is not None and group.type == "triangles")
    # AnyHit precedes ClosestHit in D3D12_HIT_GROUP_DESC; a swapped read would
    # attribute the wrong shader to every hit in the frame.
    check(
        "closest hit / any hit not swapped",
        group is not None
        and group.closest_hit == "CHS_b5acc26ab7153489"
        and group.any_hit == "AHS_b5acc26ab7153489",
        f"chs={group.closest_hit} ahs={group.any_hit}" if group else "missing",
    )
    check("no intersection shader", group is not None and group.intersection == "")
    # The literal in Read(dxilData, 6896) is a compressed byte count, not an index.
    check(
        "DXIL compressed size 6896 recorded as a size",
        chs is not None and chs.dxil_compressed_size == 6896,
        str(chs.dxil_compressed_size) if chs else "missing",
    )
    check(
        "DXIL blob index is a stream ordinal, not the byte count",
        chs is not None and chs.dxil_blob_index == 297,
        str(chs.dxil_blob_index) if chs else "missing",
    )

    print("\n3. RTPSO 3930: the collection graph must be expanded")
    rtpso = objects.get(3930)
    if rtpso is None:
        check("state object 3930 exists", False)
        return _report()
    check(
        "type is raytracing_pipeline",
        rtpso.type is StateObjectType.RAYTRACING_PIPELINE,
        rtpso.type.value,
    )
    check(
        "three AddToStateObject desc segments",
        rtpso.desc_segment_count == 3,
        str(rtpso.desc_segment_count),
    )
    # Deliberately asserted as zero: this proves the expanded numbers below come
    # from following references rather than from reading the body directly.
    check(
        "declares no exports of its own",
        len(rtpso.exports) == 0,
        str(len(rtpso.exports)),
    )
    check(
        "references 66 collections",
        len(rtpso.existing_collection_ids) == 66,
        str(len(rtpso.existing_collection_ids)),
    )
    check(
        "expands to 84 exports",
        len(rtpso.resolved_exports) == 84,
        str(len(rtpso.resolved_exports)),
    )
    check(
        "expands to 58 hit groups",
        len(rtpso.resolved_hit_groups) == 58,
        str(len(rtpso.resolved_hit_groups)),
    )
    raygens = [
        export.name
        for export in rtpso.resolved_exports
        if export.stage is not None and export.stage.value == "RAYGEN"
    ]
    check("four raygen exports after expansion", len(raygens) == 4, str(raygens))
    check(
        "shader config 64/8 taken from the pipeline, not a collection",
        rtpso.max_payload_size == 64 and rtpso.max_attribute_size == 8,
        f"{rtpso.max_payload_size}/{rtpso.max_attribute_size}",
    )

    print("\n4. negative: expansion must not swallow unreferenced collections")
    other = objects.get(3891)
    if other is None:
        check("state object 3891 exists", False)
        return _report()
    reachable = set(rtpso.resolved_state_object_ids)
    foreign = [
        api_id for api_id in other.existing_collection_ids if api_id not in reachable
    ]
    check(
        "3891 references collections 3930 cannot reach",
        bool(foreign),
        f"{len(foreign)} of {len(other.existing_collection_ids)}",
    )
    foreign_exports = {
        export.name
        for api_id in foreign
        for export in objects[api_id].exports
        if api_id in objects
    }
    leaked = foreign_exports & {export.name for export in rtpso.resolved_exports}
    check(
        "none of those collections' exports leaked into 3930",
        not leaked,
        str(sorted(leaked)[:4]),
    )

    print("\n5. referential integrity across all 81 objects")
    dangling = {
        api_id: obj.missing_collection_ids
        for api_id, obj in objects.items()
        if obj.missing_collection_ids
    }
    check("no dangling EXISTING_COLLECTION references", not dangling, str(dangling))
    unknown_rs = [
        (obj.api_id, export.name, export.local_root_signature_id)
        for obj in objects.values()
        for export in obj.exports
        if export.local_root_signature_id is not None
        and export.local_root_signature_id not in capture.root_signatures
    ]
    check(
        "every local root signature exists in the capture",
        not unknown_rs,
        str(unknown_rs[:3]),
    )
    stray_stage = [
        (obj.api_id, export.name)
        for obj in objects.values()
        for export in obj.exports
        if export.stage is not None and not export.stage_source
    ]
    check(
        "no stage is reported without saying how it was derived",
        not stray_stage,
        str(stray_stage[:3]),
    )

    print("\n6. draw-side linkage")
    ray_draws = [draw for draw in capture.draw_calls if draw.state_object_id is not None]
    check("two actions bind a state object", len(ray_draws) == 2, str(len(ray_draws)))
    check(
        "both resolve to a StateObject",
        all(draw.state_object is not None for draw in ray_draws),
    )
    check(
        "state object ids are 3891 and 3930",
        sorted(draw.state_object_id for draw in ray_draws) == [3891, 3930],
        str(sorted(draw.state_object_id for draw in ray_draws)),
    )
    check(
        "no raytracing action also reports a PSO",
        all(draw.pso_id is None for draw in ray_draws),
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
    print("PASS: state object parsing matches the Tiled.wpix baseline")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
