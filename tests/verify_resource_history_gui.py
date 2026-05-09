"""Verify the resource-history view against the PIX GUI screenshot.

Ground truth: the PIX resource-history window for GBufferA in Tiled.wpix, as
supplied by the user. 25 rows, each with Global ID, Name, Binding, Read/Write and
States. This is the acceptance baseline for the alignment work; if it regresses,
the history no longer matches what PIX shows.

The GUI numbers an ExecuteIndirect's expanded child rather than the
ExecuteIndirect itself, so the ids below are the GUI's and are matched against
``gui_global_id``.
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

# (gui_global_id, binding, access-contains, state fragment)
GUI_ROWS: list[tuple[int, str, str, str]] = [
    (3139, "API Parameters [0]", "alias", ""),
    (3140, "API Parameters [0]", "discard", ""),
    (3828, "OM [None]", "write", ""),
    (3851, "OM RTV 1", "write", ""),
    (3854, "OM RTV 1", "write", ""),
    (3860, "API Parameters [1]", "state_transition", "PIXEL_SHADER_RESOURCE"),
    (3893, "CS SRV 2", "read", ""),
    (3968, "CS SRV 7", "read", ""),
    (4891, "CS SRV 1", "read", ""),
    (4904, "CS SRV 1", "read", ""),
    (4908, "CS SRV 1", "read", ""),
    (4919, "CS SRV 1", "read", ""),
    (5206, "CS SRV 1", "read", ""),
    (5210, "CS SRV 1", "read", ""),
    (5216, "CS SRV 1", "read", ""),
    (5275, "CS SRV 1", "read", ""),
    (5286, "CS SRV 1", "read", ""),
    (5378, "CS SRV 1", "read", ""),
    (5387, "CS SRV 1", "read", ""),
    (5396, "CS SRV 1", "read", ""),
    (5409, "PS SRV 8", "read", ""),
    (5417, "CS SRV 2", "read", ""),
    (5484, "PS SRV 3", "read", ""),
    (5592, "PS SRV 1", "read", ""),
    (5972, "API Parameters [2]", "state_transition", "STATE_RENDER_TARGET"),
]


def run_tool() -> dict:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pix_tool_set.cli",
            "resource-usage",
            "--session",
            SESSION,
            "--resource-id",
            str(RESOURCE),
            "--include-resource-events",
            "--max-events",
            "200",
        ],
        capture_output=True,
        text=True,
        cwd=str(SRC),
    )
    if not proc.stdout.strip():
        raise SystemExit(f"tool produced no output\n{proc.stderr[-3000:]}")
    return json.loads(proc.stdout)


def main() -> int:
    payload = run_tool()
    if payload.get("status") not in ("success", "partial"):
        print(json.dumps(payload, indent=2)[:3000])
        return 1
    data = payload["data"]
    failures: list[str] = []

    # -- check 1: the resource carries its engine name -------------------
    name = data["resource"].get("name")
    if name != "GBufferA":
        failures.append(f"resource name is {name!r}, expected 'GBufferA'")
    else:
        print("check 1  resource name = GBufferA  OK")

    # -- check 2: format, which validates resource identity --------------
    fmt = data["resource"].get("format")
    if fmt != "DXGI_FORMAT_R10G10B10A2_UNORM":
        failures.append(f"format is {fmt!r}, expected DXGI_FORMAT_R10G10B10A2_UNORM")
    else:
        print("check 2  format = R10G10B10A2_UNORM  OK")

    # -- check 3: every GUI row is present, with the right binding -------
    history = data.get("combined_history") or []
    by_gid: dict[int, list[dict]] = {}
    for row in history:
        gid = row.get("gui_global_id")
        if gid is None:
            gid = row.get("global_id")
        if gid is not None:
            by_gid.setdefault(int(gid), []).append(row)

    print()
    print(f"{'gid':>6}  {'GUI binding':<20} {'ours':<20} {'access':<18} verdict")
    print("-" * 90)
    matched = 0
    for gid, binding, access, state_fragment in GUI_ROWS:
        rows = by_gid.get(gid)
        if not rows:
            print(f"{gid:>6}  {binding:<20} {'<absent>':<20} {'':<18} MISS")
            failures.append(f"gid {gid} missing from history")
            continue
        row = rows[0]
        ours = str(row.get("binding"))
        accesses = row.get("access") or []
        ok_binding = ours == binding
        ok_access = any(access in str(a) for a in accesses)
        ok_state = True
        if state_fragment:
            states = row.get("states") or {}
            ok_state = state_fragment in str(states.get("after", "")) or state_fragment in str(
                states.get("before", "")
            )
        verdict = "MATCH" if (ok_binding and ok_access and ok_state) else "MISMATCH"
        if verdict == "MATCH":
            matched += 1
        else:
            detail = []
            if not ok_binding:
                detail.append(f"binding {ours!r} != {binding!r}")
            if not ok_access:
                detail.append(f"access {accesses} lacks {access!r}")
            if not ok_state:
                detail.append(f"state lacks {state_fragment!r}: {row.get('states')}")
            failures.append(f"gid {gid}: " + "; ".join(detail))
        print(
            f"{gid:>6}  {binding:<20} {ours:<20} "
            f"{','.join(str(a) for a in accesses):<18} {verdict}"
        )

    print()
    print(f"rows matched: {matched}/{len(GUI_ROWS)}")

    # -- check 4: no false positives ------------------------------------
    # gid 3836 (SkyAtmosphereEditor) renders to SceneColor, not GBufferA. It used
    # to appear here because the inline-RTV fallback accumulated every render
    # target the command list had ever created. It must stay out.
    gui_gids = {gid for gid, *_ in GUI_ROWS}
    extra = sorted(gid for gid in by_gid if gid not in gui_gids)
    if 3836 in extra:
        failures.append(
            "gid 3836 is back in GBufferA's history; the inline-RTV fallback is "
            "over-reporting render targets again"
        )
    if extra:
        print(f"check 4  extra rows not in the GUI: {extra}")
        for gid in extra:
            failures.append(f"gid {gid} is reported but the GUI does not list it")
    else:
        print("check 4  no rows beyond what the GUI shows  OK")

    # -- check 5: the state timeline is a closed chain -------------------
    timeline = data.get("state_timeline") or []
    broken = [row for row in timeline if row.get("inconsistent")]
    print(f"check 5  state transitions: {len(timeline)}, inconsistent: {len(broken)}")

    print()
    if failures:
        print(f"FAILED ({len(failures)})")
        for line in failures:
            print("  - " + line)
        return 1
    print("PASSED: the resource history matches the PIX GUI baseline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
