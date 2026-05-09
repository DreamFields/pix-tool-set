"""Check the parsed shader binding tables against the Tiled.wpix baseline.

The last section is the joint acceptance test for phases one and two: every shader
identifier named by a record must be reachable from the state object the dispatch
binds. If the collection expansion silently missed a collection, or if a table was
matched to the wrong pipeline, orphaned identifiers appear here -- and nowhere
else, because both failures otherwise produce a well-formed, plausible payload.

Two baseline facts worth stating up front, since both invite a wrong reading:

* the raygen region is 64 bytes inside a 2,715,136-byte buffer, so region size and
  buffer size must be reported separately;
* ``CreateShaderTable_01`` writes a miss identifier at offset 131,072, past the end
  of the 131,072-byte hit-group region it fills. That record is real but is not
  read by this dispatch, so it must be neither counted as a hit group nor dropped.
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
    tables = capture.shader_binding_tables

    print("1. table inventory")
    check("two shader binding tables", len(tables) == 2, str(sorted(tables)))
    check("keyed by indirect buffer name", sorted(tables) == ["1415_1", "1415_2"])

    print("\n2. table 1415_1 against the desc")
    sbt = tables.get("1415_1")
    if sbt is None:
        check("table 1415_1 exists", False)
        return _report()
    check(
        "state object 3891, stated by QueryInterface",
        sbt.state_object_id == 3891,
        str(sbt.state_object_id),
    )
    check(
        "dispatch dimensions 232x1x1",
        (sbt.width, sbt.height, sbt.depth) == (232, 1, 1),
        f"{sbt.width}x{sbt.height}x{sbt.depth}",
    )
    check("ray count 232", sbt.ray_count == 232, str(sbt.ray_count))
    check(
        "raygen region is 64 bytes",
        sbt.raygen is not None and sbt.raygen.size_in_bytes == 64,
        str(sbt.raygen.size_in_bytes) if sbt.raygen else "missing",
    )
    # The distinction that makes a one-record table look like tens of thousands.
    check(
        "raygen buffer size reported separately from the region",
        sbt.raygen is not None and sbt.raygen.buffer_size_in_bytes == 2715136,
        str(sbt.raygen.buffer_size_in_bytes) if sbt.raygen else "missing",
    )
    check(
        "miss region 16384 stride 128",
        sbt.miss is not None
        and sbt.miss.size_in_bytes == 16384
        and sbt.miss.stride_in_bytes == 128,
    )
    check(
        "hit group region 131072 stride 128",
        sbt.hit_group is not None
        and sbt.hit_group.size_in_bytes == 131072
        and sbt.hit_group.stride_in_bytes == 128,
    )
    # None, not an empty region: "no callable shaders" and "a callable table with
    # zero records" are different statements about the pipeline.
    check("callable region absent, reported as None", sbt.callable_table is None)
    check(
        "raygen identifier read from the inline copy",
        sbt.raygen_identifier == "RayGen_2441381b5301eb11",
        sbt.raygen_identifier,
    )
    check(
        "reconstruction functions 00 and 01",
        sbt.reconstruction_functions == ["CreateShaderTable_00", "CreateShaderTable_01"],
        str(sbt.reconstruction_functions),
    )

    print("\n3. records of 1415_1")
    check("11 records total", len(sbt.records) == 11, str(len(sbt.records)))
    check("one raygen record", len(sbt.records_in("raygen")) == 1)
    check("one miss record", len(sbt.records_in("miss")) == 1)
    check(
        "eight hit group records", len(sbt.records_in("hit_group")) == 8,
        str(len(sbt.records_in("hit_group"))),
    )
    first_hit = next(
        (r for r in sbt.records if r.table == "hit_group" and r.offset == 0), None
    )
    check(
        "hit group record at offset 0 keeps its root constants verbatim",
        first_hit is not None
        and first_hit.root_constants == [3074, 0, 0, 536870915, 2208, 2212, 0, 0],
        str(first_hit.root_constants) if first_hit else "missing",
    )
    strides = sorted(r.offset for r in sbt.records if r.table == "hit_group")
    check(
        "hit group offsets step by the declared stride",
        strides == [0, 128, 256, 384, 512, 640, 768, 896],
        str(strides),
    )
    tail = [r for r in sbt.records if not r.in_declared_region]
    check(
        "the record at 131072 is flagged as outside the declared region",
        len(tail) == 1 and tail[0].offset == 131072,
        str([(r.offset, r.table) for r in tail]),
    )
    check(
        "and is not counted as a hit group",
        all(r.table != "hit_group" for r in tail),
        str([r.table for r in tail]),
    )
    check(
        "and is not silently dropped",
        any(r.shader_identifier == "Miss_e372c111d609dfde" for r in tail),
    )

    print("\n4. table 1415_2 and its GPU VA root arguments")
    other = tables.get("1415_2")
    if other is None:
        check("table 1415_2 exists", False)
        return _report()
    check("state object 3930", other.state_object_id == 3930, str(other.state_object_id))
    check(
        "dispatch dimensions 2x1x1",
        (other.width, other.height, other.depth) == (2, 1, 1),
        f"{other.width}x{other.height}x{other.depth}",
    )
    check(
        "two miss records, the second carrying a GPU VA",
        len(other.records_in("miss")) == 2,
        str(len(other.records_in("miss"))),
    )
    hit0 = next(
        (r for r in other.records if r.table == "hit_group" and r.offset == 0), None
    )
    # GetGpuva(414, 22016) is already a (resource, offset) pair in this export, so a
    # local root argument resolves to a known resource with no address reverse lookup.
    check(
        "local root GPU VAs resolve to (resource, offset) pairs",
        hit0 is not None and hit0.root_gpuvas == [(414, 22016), (414, 267776)],
        str(hit0.root_gpuvas) if hit0 else "missing",
    )
    check(
        "those resources exist in the capture",
        hit0 is not None
        and all(capture.resource(rid) is not None for rid, _ in hit0.root_gpuvas),
    )

    print("\n5. action linkage, via the indirect buffer name")
    ray_draws = [d for d in capture.draw_calls if d.is_raytracing]
    check("two raytracing actions", len(ray_draws) == 2, str(len(ray_draws)))
    for draw in ray_draws:
        linked = draw.shader_binding_table
        check(
            f"draw {draw.index} resolves its table",
            linked is not None
            and linked.indirect_buffer_key == draw.indirect_argument_buffer,
            str(linked.indirect_buffer_key if linked else None),
        )
        # A dispatch whose SetPipelineState1 object disagrees with the object its
        # table was built against would mean the two were matched incorrectly.
        check(
            f"draw {draw.index} pipeline agrees with its table",
            linked is not None and linked.state_object_id == draw.state_object_id,
            f"{linked.state_object_id if linked else None} vs {draw.state_object_id}",
        )

    print("\n6. joint check: every record identifier exists in its state object")
    for key, table in tables.items():
        state_object = table.state_object
        check(f"{key} resolves its state object", state_object is not None)
        if state_object is None:
            continue
        unresolved = table.unresolved_identifiers
        check(
            f"{key}: no orphan shader identifiers",
            not unresolved,
            str(unresolved[:5]),
        )
        kinds = {
            record.shader_identifier: state_object.identifier_owner(
                record.shader_identifier
            )
            for record in table.records
        }
        check(
            f"{key}: every identifier is an export or a hit group",
            all(kind is not None for kind in kinds.values()),
            str([name for name, kind in kinds.items() if kind is None][:3]),
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
    print("PASS: shader binding tables match the Tiled.wpix baseline")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
