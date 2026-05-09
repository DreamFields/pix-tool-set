"""Field-by-field comparison against the PIX GUI's own readout.

Ground truth transcribed from the PIX constant-buffer view for Queue ID 18385
(RayTracingBuildInstanceBuffer). Any mismatch here is a real defect, so the
comparison is exact rather than eyeballed: integers must be equal, floats must
agree to the precision PIX displays.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set import call_tool  # noqa: E402
from pix_tool_set.context import clear_capture_cache  # noqa: E402

# (offset, name, expected) exactly as PIX shows it.
PIX_TRUTH: list[tuple[int, str, object]] = [
    (0, "GPUSceneInstanceDataTileSizeLog2", 8),
    (4, "GPUSceneInstanceDataTileSizeMask", 255),
    (8, "GPUSceneInstanceDataTileStride", 768),
    (12, "GPUSceneFrameNumber", 59369),
    (16, "GPUSceneMaxAllocatedInstanceId", 29),
    (20, "GPUSceneMaxPersistentPrimitiveIndex", 30),
    (24, "GPUSceneNumLightmapDataItems", 0),
    (128, "MaxNumInstances", 3),
    (132, "NumGroups", 1),
    (136, "NumInstanceDescriptors", 3),
    (140, "BaseGroupDescriptorIndex", 0),
    (144, "BaseInstanceDescriptorIndex", 0),
    (160, "PreViewTranslationHigh", [-4877.11, -1759.08, -1330.95]),
    (176, "PreViewTranslationLow", [-0.000216702, -2.56451e-05, 5.08402e-05]),
    (188, "CullingRadius", 15000.0),
    (192, "FarFieldCullingRadius", 1e06),
    (196, "AngleThresholdRatioSq", 0.000304679),
    (208, "ViewOrigin", [0.0, 0.0, 0.0]),
    (220, "CullingMode", 3),
    (224, "CullUsingGroups", 0),
    (240, "OutputStatsOffset", 0),
    (244, "pad", None),  # PIX lists it with no value
]

REL_TOL = 1e-4


def close(actual, expected) -> bool:
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            return False
        return all(close(a, e) for a, e in zip(actual, expected))
    if isinstance(expected, float):
        if not isinstance(actual, (int, float)):
            return False
        if expected == 0.0:
            return abs(actual) < 1e-9
        return abs(actual - expected) <= abs(expected) * REL_TOL
    return actual == expected


def main() -> int:
    clear_capture_cache()
    payload = call_tool(
        "pass-values", {"session": "Tiled", "queue_id": 18385, "max_bytes": 256}
    )
    if payload["status"] == "error":
        print(payload["error"])
        return 1

    fields: dict[str, dict] = {}
    for record in payload["data"]["root_bindings"]:
        for block in record.get("cbuffer_fields") or []:
            if block.get("cbuffer") != "_RootShaderParameters":
                continue
            for field in block["fields"]:
                if field.get("name"):
                    fields[field["name"]] = field

    print("=" * 96)
    print(f"{'offset':>7s}  {'field':<38s} {'PIX':<22s} {'ours':<22s} verdict")
    print("=" * 96)

    passed = failed = missing = skipped = 0
    for offset, name, expected in PIX_TRUTH:
        got = fields.get(name)
        if got is None:
            print(f"  {offset:>5d}  {name:<38s} {str(expected):<22s} {'-':<22s} MISSING")
            missing += 1
            continue
        if got.get("offset") != offset:
            print(
                f"  {offset:>5d}  {name:<38s} {'offset ' + str(offset):<22s} "
                f"{'offset ' + str(got.get('offset')):<22s} OFFSET MISMATCH"
            )
            failed += 1
            continue
        if expected is None:
            print(f"  {offset:>5d}  {name:<38s} {'(no value)':<22s} "
                  f"{str(got.get('value'))[:22]:<22s} skipped")
            skipped += 1
            continue
        actual = got.get("value")
        ok = close(actual, expected)
        shown_e = (
            "{" + ", ".join(f"{v:g}" for v in expected) + "}"
            if isinstance(expected, list)
            else f"{expected:g}" if isinstance(expected, float) else str(expected)
        )
        shown_a = (
            "{" + ", ".join(f"{v:g}" for v in actual) + "}"
            if isinstance(actual, list)
            else f"{actual:g}" if isinstance(actual, float) else str(actual)
        )
        print(f"  {offset:>5d}  {name:<38s} {shown_e[:22]:<22s} {shown_a[:22]:<22s} "
              f"{'MATCH' if ok else 'DIFFER'}")
        passed += int(ok)
        failed += int(not ok)

    print("=" * 96)
    print(f"match {passed} | differ {failed} | missing {missing} | skipped {skipped}")
    extra = set(fields) - {name for _, name, _ in PIX_TRUTH} - {"_RootShaderParameters"}
    if extra:
        print(f"fields we report that PIX does not list: {sorted(extra)}")
    return 0 if failed == 0 and missing == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
