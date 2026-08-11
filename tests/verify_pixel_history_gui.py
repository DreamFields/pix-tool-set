"""Verify the pixel-history view against the PIX GUI Pixel History panel.

Ground truth: the PIX Pixel History panel for pixel (810, 284) of GBufferA
(resource 756) in Tiled.wpix, pinned at Global ID 5417. The panel shows exactly
four rows:

    gid 0     Recreation #1  (0,0,0,0)               -> (0.4995, 1.0, 0.4995, 0.3333)
    gid 3828  Clear          (0.4995,1,0.4995,.3333) -> (0, 0, 0, 0)
    gid 3851  Draw           (0,0,0,0)               -> Failed depth/stencil test
    gid 3854  Draw           (0,0,0,0)               -> (0.4995, 1.0, 0.4995, 0.3333)

This script checks the parts that are decidable without a GPU replay: which
events are in the history at all, and what they are. The per-event pixel *values*
and the depth/stencil verdict require replaying the frame and reading the pixel
back around each event; those are covered by verify_pixel_value_history.py.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SESSION = "Tiled"
RESOURCE = 756
X, Y = 810, 284

# The rows the GUI shows, and what kind of event each one is.
GUI_ROWS = [
    (3828, "clear"),
    (3851, "draw"),
    (3854, "draw"),
]
# gid 0 "Recreation #1" is PIX's pseudo-event for the resource's initial
# contents. It is not an API call in the export, so it is expected to come from
# the replay side rather than from static parsing.


def run() -> dict:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pix_tool_set.cli",
            "pixel-history",
            "--session",
            SESSION,
            "--resource-id",
            str(RESOURCE),
            "--x",
            str(X),
            "--y",
            str(Y),
            "--include-resource-events",
            "--max-events",
            "50",
        ],
        capture_output=True,
        text=True,
        cwd=str(SRC),
    )
    if not proc.stdout.strip():
        raise SystemExit(f"no output\n{proc.stderr[-3000:]}")
    return json.loads(proc.stdout)


def main() -> int:
    payload = run()
    if payload.get("status") not in ("success", "partial"):
        print(json.dumps(payload, indent=2)[:2500])
        return 1
    data = payload["data"]
    failures: list[str] = []

    history = data.get("combined_history") or []
    by_gid = {}
    for row in history:
        gid = row.get("gui_global_id")
        if gid is None:
            gid = row.get("global_id")
        if gid is not None:
            by_gid[int(gid)] = row

    print(f"pixel ({X}, {Y}) of resource {RESOURCE} "
          f"({data.get('render_target', {}).get('name')})")
    print(f"draw candidates: {data.get('candidate_count')}")
    print()
    print(f"{'gid':>6}  {'expected':<8} {'ours':<10} {'binding':<12} detail")
    print("-" * 88)

    for gid, expected_type in GUI_ROWS:
        row = by_gid.get(gid)
        if row is None:
            print(f"{gid:>6}  {expected_type:<8} {'<absent>':<10}")
            failures.append(f"gid {gid} ({expected_type}) missing from the history")
            continue
        actual = str(row.get("event_type"))
        ok = actual == expected_type
        detail = row.get("pass_name") or row.get("api") or ""
        if row.get("clear_value") is not None:
            detail += f"  clear_value={row['clear_value']}"
        if row.get("may_fail_depth_stencil"):
            detail += "  [depth test enabled]"
        print(
            f"{gid:>6}  {expected_type:<8} {actual:<10} "
            f"{str(row.get('binding')):<12} {detail[:44]}"
        )
        if not ok:
            failures.append(f"gid {gid}: event_type {actual!r} != {expected_type!r}")

    # -- the candidate set must be exactly the two draws the GUI lists ----
    draw_gids = sorted(
        int(r["gui_global_id"])
        for r in history
        if r.get("event_type") == "draw" and r.get("gui_global_id") is not None
    )
    expected_draws = [3851, 3854]
    print()
    if draw_gids != expected_draws:
        failures.append(
            f"draw rows are {draw_gids}, expected exactly {expected_draws}. "
            "A superset means the coverage test is too loose; a subset means a real "
            "writer was dropped."
        )
        print(f"check  draw rows {draw_gids} != {expected_draws}  FAIL")
    else:
        print(f"check  draw rows are exactly {expected_draws}  OK")

    # -- the clear must be reported with its clear colour ----------------
    clear = by_gid.get(3828)
    if clear is not None:
        value = clear.get("clear_value")
        if not value or any(abs(float(v)) > 1e-9 for v in value):
            failures.append(
                f"gid 3828 clear_value is {value!r}; the GUI shows the pixel going to "
                "all zeroes, so the clear colour must be (0,0,0,0)"
            )
        else:
            print("check  gid 3828 clears to (0,0,0,0), matching the GUI  OK")

    # -- gid 3851 must be flagged as depth-testable ----------------------
    row = by_gid.get(3851)
    if row is not None and not row.get("may_fail_depth_stencil"):
        failures.append(
            "gid 3851 is the row the GUI reports as 'Failed depth/stencil test', so it "
            "must at least be flagged as depth-tested here"
        )
    elif row is not None:
        print("check  gid 3851 flagged as depth-tested  OK")

    print()
    if failures:
        print(f"FAILED ({len(failures)})")
        for line in failures:
            print("  - " + line)
        return 1
    print("PASSED: the static part of the pixel history matches the PIX GUI panel.")
    print("note: per-event pixel values and the depth verdict need the replay path")
    print("      (verify_pixel_value_history.py).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
