"""Check that dxc's binding table survives a column that overflows its width.

dxc renders `; Resource Bindings:` with fixed column widths and only guarantees a
single space of padding, so a value wider than its column swallows the separator.
Splitting on whitespace then shifts every later cell one column left. The sample
below is real output for QueueID 18704 of Tiled.wpix, where `unorm_f32` (9 chars)
overflows the 7-char Format column and arrives glued to `UAV`.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set.engine.dxbc import parse_resource_bindings

SAMPLE = """
; Resource Bindings:
;
; Name                                 Type  Format         Dim      ID      HLSL Bind  Count
; ------------------------------ ---------- ------- ----------- ------- -------------- ------
; _RootShaderParameters             cbuffer      NA          NA     CB0            cb0     1
; D3DStaticPointClampedSampler      sampler      NA          NA      S0   s1,space1000     1
; ForwardLightStruct_NumCulledLightsGrid   texture  struct         r/o      T0             t0     1
; SceneTexturesStruct_SceneDepthTexture   texture     f32          2d      T1             t1     1
; BlueNoise_ScalarTexture           texture     f32          2d      T4             t4     1
; RWDepthTexture                        UAV     f32          2d      U0             u0     1
; RWLumenTileBitmask                    UAV     u32     2darray      U2             u2     1
; RWDownsampledWorldNormal2x2           UAVunorm_f32          2d      U7             u7     1
;
target datalayout = "e-m:e"
"""

EXPECTED = {
    "CB0": ("_RootShaderParameters", "cbuffer", "NA", "NA", "cb0"),
    "S0": ("D3DStaticPointClampedSampler", "sampler", "NA", "NA", "s1,space1000"),
    # Name overflows its 30-char column here, which is why slicing by the rule line's
    # widths cannot work either.
    "T0": ("ForwardLightStruct_NumCulledLightsGrid", "texture", "struct", "r/o", "t0"),
    "T1": ("SceneTexturesStruct_SceneDepthTexture", "texture", "f32", "2d", "t1"),
    "T4": ("BlueNoise_ScalarTexture", "texture", "f32", "2d", "t4"),
    "U0": ("RWDepthTexture", "UAV", "f32", "2d", "u0"),
    "U2": ("RWLumenTileBitmask", "UAV", "u32", "2darray", "u2"),
    # The regression: Format overflowed and glued itself onto Type.
    "U7": ("RWDownsampledWorldNormal2x2", "UAV", "unorm_f32", "2d", "u7"),
}


def main() -> int:
    rows = parse_resource_bindings(SAMPLE)
    by_id = {row["id"]: row for row in rows}

    failures: list[str] = []
    if len(rows) != len(EXPECTED):
        failures.append(f"row count {len(rows)}, expected {len(EXPECTED)}")

    for bind_id, (name, type_, fmt, dim, hlsl) in EXPECTED.items():
        row = by_id.get(bind_id)
        if row is None:
            failures.append(f"{bind_id}: missing from the parsed table")
            continue
        actual = (row["name"], row["type"], row["format"], row["dimension"], row["hlsl_bind"])
        if actual != (name, type_, fmt, dim, hlsl):
            failures.append(
                f"{bind_id}: got {actual}, expected {(name, type_, fmt, dim, hlsl)}"
            )
        if row["count"] != "1":
            failures.append(f"{bind_id}: count {row['count']!r}, expected '1'")

    for line in (f"{r['id']:4s} {r['name']:40s} type={r['type']:9s} "
                 f"format={r['format']:10s} dim={r['dimension']:8s} bind={r['hlsl_bind']}"
                 for r in rows):
        print(line)

    if failures:
        print("\nFAIL")
        for item in failures:
            print("  -", item)
        return 1
    print("\nPASS: every cell landed in its own column")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
