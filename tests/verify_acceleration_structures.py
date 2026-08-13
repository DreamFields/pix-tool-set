"""Check acceleration structure parsing against the Tiled.wpix baseline.

Section 5 is the one that matters most, and it is a negative assertion: triangle
and vertex counts must be ``None``. The export replays bottom-level structures from
a driver-private serialized blob, so it contains no geometry description at all.
The blob sizes are right there and correlate loosely with geometry volume, which
makes "estimate triangles from blob size" a tempting future optimisation and a
fabrication. This test exists to make that change fail loudly.

Section 3 guards the other silent-wrong-answer risk: instances 1 and 2 point at
the same BLAS address, so a parser that de-duplicated by address would report two
scene objects as one.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pix_tool_set.engine.capture import Capture  # noqa: E402
from pix_tool_set.session import SessionStore  # noqa: E402

SESSION = "Tiled"

failures: list[str] = []
checks = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(label)


def main() -> int:
    record = SessionStore().resolve(session=SESSION)
    capture = Capture(
        Path(record.capture_path) if record.capture_path else None,
        Path(record.export_dir),
        Path(record.event_csv) if record.event_csv else None,
    )
    builds = capture.acceleration_structure_builds

    print("1. build inventory")
    check("three AS builds", len(builds) == 3, str(len(builds)))
    check(
        "global ids 3752..3754",
        [build.global_id for build in builds] == [3752, 3753, 3754],
        str([build.global_id for build in builds]),
    )
    check("all top level", all(build.type == "top_level" for build in builds))
    check(
        "all prefer_fast_trace",
        all(build.flags == ["prefer_fast_trace"] for build in builds),
        str([build.flags for build in builds]),
    )
    check(
        "built inside the RayTracingBuildScene marker",
        all(
            build.marker_path and build.marker_path[-1] == "RayTracingBuildScene"
            for build in builds
        ),
        str([build.marker_path[-1:] for build in builds]),
    )

    print("\n2. the build that carries instances")
    build = builds[0]
    check("NumDescs is 3", build.num_descs == 3, str(build.num_descs))
    check("three instance descs parsed", len(build.instances) == 3, str(len(build.instances)))
    check(
        "descs layout is array", build.descs_layout == "array", build.descs_layout
    )
    check(
        "destination is resource 3223 at 14153472",
        build.dest_resource_id == 3223 and build.dest_byte_offset == 14153472,
        f"{build.dest_resource_id}+{build.dest_byte_offset}",
    )
    check(
        "scratch is resource 571 at 11272192",
        build.scratch_resource_id == 571 and build.scratch_byte_offset == 11272192,
        f"{build.scratch_resource_id}+{build.scratch_byte_offset}",
    )
    # GetGpuva(0, 0) is the null source of a fresh build. Reporting resource 0 here
    # would invent an update-in-place that never happened.
    check(
        "null source address is not reported as resource 0",
        build.source_resource_id is None,
        str(build.source_resource_id),
    )

    print("\n3. instance fields, read positionally after the transform")
    first = build.instances[0]
    check(
        "instance 0 id/mask/hitgroup/flags == 3/5/6/6",
        (
            first.instance_id,
            first.instance_mask,
            first.contribution_to_hit_group_index,
            first.flags,
        )
        == (3, 5, 6, 6),
        f"{first.instance_id}/{first.instance_mask}/"
        f"{first.contribution_to_hit_group_index}/{first.flags}",
    )
    check(
        "instance 0 BLAS at (3226, 21678848)",
        (first.blas_resource_id, first.blas_byte_offset) == (3226, 21678848),
        f"{first.blas_resource_id}+{first.blas_byte_offset}",
    )
    check(
        "instance 0 transform has 12 floats, scale 15 preserved",
        len(first.transform) == 12 and first.transform[0] == 15.0,
        f"{len(first.transform)} floats, [0]={first.transform[0] if first.transform else None}",
    )
    second, third = build.instances[1], build.instances[2]
    # Two instances of one BLAS. A parser that keyed instances by address would
    # collapse them and under-report the scene.
    check(
        "instances 1 and 2 share a BLAS and are not de-duplicated",
        (second.blas_resource_id, second.blas_byte_offset)
        == (third.blas_resource_id, third.blas_byte_offset)
        == (3226, 21574656),
        f"{second.blas_byte_offset} vs {third.blas_byte_offset}",
    )
    check(
        "their differing transforms are kept",
        second.transform[0] != third.transform[0],
        f"{second.transform[0]} vs {third.transform[0]}",
    )
    check(
        "instances 1 and 2 both use hit group index 4",
        second.contribution_to_hit_group_index
        == third.contribution_to_hit_group_index
        == 4,
    )

    print("\n4. serialized blobs and AS resources")
    serialized = capture.serialized_acceleration_structures
    check("655 serialized blocks", len(serialized) == 655, str(len(serialized)))
    head = serialized[0]
    check(
        "first block is 3313592 serialized / 3313536 deserialized",
        head.serialized_size == 3313592 and head.deserialized_size == 3313536,
        f"{head.serialized_size}/{head.deserialized_size}",
    )
    check("first block belongs to resource 3222", head.resource_id == 3222)
    as_resources = {
        resource.api_id
        for resource in capture.resources.values()
        if "RAYTRACING_ACCELERATION_STRUCTURE" in (resource.flags or "")
        or "RAYTRACING_ACCELERATION_STRUCTURE" in (resource.initial_state or "")
    }
    expected = {3222, 3223, 3224, 3225, 3226, 3227, 3228}
    check(
        "AS resources 3222..3228 all identified",
        expected <= as_resources,
        str(sorted(expected - as_resources)),
    )

    print("\n5. NEGATIVE: geometry counts must stay unavailable")
    for build in builds:
        payload = build.to_dict()
        check(
            f"gid {build.global_id}: triangle_count is None, not a number",
            payload["triangle_count"] is None,
            repr(payload["triangle_count"]),
        )
        check(
            f"gid {build.global_id}: vertex_count is None, not a number",
            payload["vertex_count"] is None,
            repr(payload["vertex_count"]),
        )
        check(
            f"gid {build.global_id}: the reason is stated, not left blank",
            bool(payload.get("geometry_note")),
        )
    check(
        "geometry_available is False on every build",
        all(not build.geometry_available for build in builds),
    )

    print("\n6. instance to hit group, through the table stride")
    table = capture.shader_binding_tables.get("1415_2")
    check("table 1415_2 available for the mapping", table is not None)
    if table is not None and table.hit_group is not None:
        stride = table.hit_group.stride_in_bytes
        offset = first.contribution_to_hit_group_index * stride
        check(
            "instance 0 index 6 maps to record offset 768",
            offset == 768,
            f"{first.contribution_to_hit_group_index} x {stride} = {offset}",
        )
        match = next(
            (
                record
                for record in table.records
                if record.table == "hit_group" and record.offset == offset
            ),
            None,
        )
        check(
            "and that record names a hit group the pipeline exports",
            match is not None
            and table.state_object is not None
            and table.state_object.identifier_owner(match.shader_identifier)
            == "hit_group",
            match.shader_identifier if match else "no record at that offset",
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
    print("PASS: acceleration structures match the Tiled.wpix baseline")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
