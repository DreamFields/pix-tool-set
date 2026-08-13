"""Regression: pass-bindings must not misreport a mip-chain write as filler.

Reference case, taken from the PIX GUI of Tiled.wpix at Global ID 3167
(pass ``ReduceHZB(mips=[8;9] Furthest) 4x2``). The GUI lists five CS bindings:

    CBV 0     : Resource Allocator Underlying Buffer : _RootShaderParameters
    SRV Tex 0 : Nanite.PreviousOccluderHZB           : ParentTextureMip
    UAV Tex 0 : Nanite.PreviousOccluderHZB           : FurthestHZBOutput_0
    UAV Tex 1 : Nanite.PreviousOccluderHZB           : FurthestHZBOutput_1
    Sampler 0 : ParentTextureMipSampler

Both UAVs address the *same* texture, at mips 8 and 9 respectively, which is
what the pass name says it does. The old classifier counted distinct
resource_ids only, saw 1 against 2 declared UAV registers, and declared the
table PIX initialisation filler -- telling the caller to distrust a table that
the export had recorded exactly. The sampler table was likewise downgraded to
`partial` because samplers own no resource id to count.

Run:  python tests/verify_mip_subresource_bindings.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

GLOBAL_ID = 3167
EXPECTED_RESOURCE = 791
EXPECTED_UAV_MIPS = [8, 9]
EXPECTED_SRV_MIP = 7


def run_tool() -> dict:
    proc = subprocess.run(
        [sys.executable, "-m", "pix_tool_set.cli", "--compact", "pass-bindings",
         "--global-id", str(GLOBAL_ID)],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    if not proc.stdout.strip():
        raise SystemExit(f"tool produced no output\n{proc.stderr}")
    return json.loads(proc.stdout)


def check(payload: dict) -> list[str]:
    failures: list[str] = []
    draw = payload["data"]["passes"][0]["draws"][0]
    tables = {t["root_index"]: t for t in draw["descriptor_tables"]}

    uav = next(
        (t for t in tables.values()
         if t["views"] and t["views"][0]["view_kind"] == "UAV"),
        None,
    )
    if uav is None:
        failures.append("no UAV descriptor table reported")
    else:
        if uav["trust"] != "reliable":
            failures.append(
                f"UAV table trust={uav['trust']!r}, expected 'reliable' "
                f"(mips {EXPECTED_UAV_MIPS} are two real bindings, not filler)"
            )
        mips = [v.get("mip_slice") for v in uav["views"]]
        if mips != EXPECTED_UAV_MIPS:
            failures.append(f"UAV mip slices {mips}, expected {EXPECTED_UAV_MIPS}")
        rids = {v["resource_id"] for v in uav["views"]}
        if rids != {EXPECTED_RESOURCE}:
            failures.append(f"UAV resource ids {rids}, expected {{{EXPECTED_RESOURCE}}}")

    srv = next(
        (t for t in tables.values()
         if t["views"] and t["views"][0]["view_kind"] == "SRV"),
        None,
    )
    if srv is None:
        failures.append("no SRV descriptor table reported")
    else:
        if srv["trust"] != "reliable":
            failures.append(f"SRV table trust={srv['trust']!r}, expected 'reliable'")
        got = srv["views"][0].get("mip_slice")
        if got != EXPECTED_SRV_MIP:
            failures.append(f"SRV mip_slice={got}, expected {EXPECTED_SRV_MIP}")

    sampler = next(
        (t for t in tables.values()
         if t["views"] and t["views"][0]["view_kind"] == "SAMPLER"),
        None,
    )
    if sampler is None:
        failures.append("no sampler descriptor table reported")
    elif sampler["trust"] != "reliable":
        failures.append(
            f"sampler table trust={sampler['trust']!r}, expected 'reliable' "
            "(samplers own no resource id, so resource counting must not apply)"
        )

    cbv = [d for d in draw["root_descriptors"] if d["binding_kind"] == "root_cbv"]
    if not cbv:
        failures.append("no root CBV reported")
    elif cbv[0]["trust"] != "reliable":
        failures.append(f"root CBV trust={cbv[0]['trust']!r}, expected 'reliable'")

    tally = payload["data"]["trust_summary"]
    if tally.get("filler"):
        failures.append(f"trust_summary still reports filler tables: {tally}")
    if tally.get("unavailable"):
        failures.append(f"trust_summary reports unavailable tables: {tally}")

    return failures


def main() -> int:
    payload = run_tool()
    failures = check(payload)
    draw = payload["data"]["passes"][0]["draws"][0]

    print(f"pass    : {payload['data']['passes'][0]['name']}")
    print(f"status  : {payload['status']}")
    print(f"trust   : {payload['data']['trust_summary']}")
    for table in draw["descriptor_tables"]:
        kind = table["views"][0]["view_kind"] if table["views"] else "?"
        subres = [v.get("subresource", "") for v in table["views"]]
        print(f"  root {table['root_index']} {kind:<7} trust={table['trust']:<10} {subres}")

    if failures:
        print("\nFAIL")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("\nPASS  all five GUI bindings reproduced, no false filler verdict")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
