"""Independently verify the probe agent's two strongest claims.

The probe reported two findings that would change the design, so they are
re-derived here from scratch rather than trusted. Both are cheap to check and
both are falsifiable.

Claim 1 -- "gid 5099 is the expanded child of the ExecuteIndirect at 5098, and
every unattributed Global ID inside the export's range is exactly
ExecuteIndirect+1, 187 out of 187."
If true, the 5098 -> 5100 discontinuity is a missing comment in the exporter, not
PIX skipping a number, and the same rule explains all of them.

Claim 2 -- "the 5190 Global IDs attributed to the 3D queue are exactly the set
present in the CSV, in both directions."
This is the load-bearing claim: it is what licenses treating the C++ export as
the authoritative source of queue ownership. A single leak in either direction
would sink it.

Deliberately independent of tmp_probe/: this file re-parses the export and the
CSV itself, so agreement means two implementations agree, not that one was
echoed back.
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

EXPORT = Path(r"C:\Users\vinmeng\Desktop\ManyLights\debug\Tiled.pixcache\cpp")
CSV_PATH = Path(r"C:\Users\vinmeng\Desktop\ManyLights\debug\Tiled.pixcache\Tiled.events.csv")

RE_GID = re.compile(r"//\s*GlobalId\s*=\s*(\d+)")
RE_CL_CALL = re.compile(r"GetCommandList\((\d+)\)->(\w+)\(")
RE_QUEUE_CALL = re.compile(r"GetCommandQueue\((\d+)\)->(\w+)\(")
RE_CL_ASSIGN = re.compile(r"commandLists\[(\d+)\]\s*=\s*GetCommandList\((\d+)\)")
RE_SUBMIT = re.compile(r"GetCommandQueue\((\d+)\)->ExecuteCommandLists")
RE_OBJ_NAME = re.compile(r"GetObject\((\d+)\)->SetName\(LR\"\((.*?)\)\"\)")

failures: list[str] = []


def check(ok: bool, message: str) -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {message}")
    if not ok:
        failures.append(message)


def gid_to_api_from_commandlists() -> dict[int, tuple[int, str]]:
    """Global ID -> (command_list_id, api) for calls that carry a GlobalId comment."""
    out: dict[int, tuple[int, str]] = {}
    for path in sorted(EXPORT.glob("CommandLists*.cpp")):
        pending: int | None = None
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = RE_GID.search(line)
            if match:
                pending = int(match.group(1))
                continue
            match = RE_CL_CALL.search(line)
            if match and pending is not None:
                out[pending] = (int(match.group(1)), match.group(2))
                pending = None
    return out


def queue_level_gids() -> dict[int, int]:
    """Global ID -> queue object id, for queue-level ops in RenderFrameWorker."""
    out: dict[int, int] = {}
    for path in sorted(EXPORT.glob("RenderFrameWorker*.cpp")):
        pending: int | None = None
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = RE_GID.search(line)
            if match:
                pending = int(match.group(1))
                continue
            match = RE_QUEUE_CALL.search(line)
            if match and pending is not None:
                out[pending] = int(match.group(1))
                pending = None
    return out


def commandlist_to_queue() -> dict[int, set[int]]:
    mapping: dict[int, set[int]] = {}
    for path in sorted(EXPORT.glob("RenderFrameWorker*.cpp")):
        pending: list[int] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = RE_CL_ASSIGN.search(line)
            if match:
                pending.append(int(match.group(2)))
                continue
            match = RE_SUBMIT.search(line)
            if match:
                queue = int(match.group(1))
                for cl in pending:
                    mapping.setdefault(cl, set()).add(queue)
                pending = []
    return mapping


def csv_gids() -> set[int]:
    out: set[int] = set()
    with CSV_PATH.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.reader(handle, skipinitialspace=True)
        header = [c.strip() for c in next(reader)]
        gi = header.index("Global ID")
        for row in reader:
            shift = max(len(row) - len(header), 0)
            idx = gi + shift
            if idx < len(row):
                text = row[idx].strip()
                if text.isdigit():
                    out.add(int(text))
    return out


def main() -> int:
    names = {}
    for path in sorted(EXPORT.glob("FrameResources*.cpp")):
        for match in RE_OBJ_NAME.finditer(path.read_text(encoding="utf-8", errors="replace")):
            names[int(match.group(1))] = match.group(2)

    cl_gids = gid_to_api_from_commandlists()
    q_gids = queue_level_gids()
    cl2q = commandlist_to_queue()
    present = csv_gids()

    print("1. inputs")
    print(f"  command-list gids with a comment : {len(cl_gids)}")
    print(f"  queue-level gids                 : {len(q_gids)}")
    print(f"  command lists mapped to a queue  : {len(cl2q)}")
    print(f"  gids present in the CSV           : {len(present)}")

    print("\n2. claim 1: unattributed gids are exactly ExecuteIndirect + 1")
    attributed: dict[int, int] = {}
    for gid, (cl, _api) in cl_gids.items():
        queues = cl2q.get(cl)
        if queues and len(queues) == 1:
            attributed[gid] = next(iter(queues))
    attributed.update(q_gids)

    lo, hi = min(attributed), max(attributed)
    unattributed = [g for g in range(lo, hi + 1) if g not in attributed]
    indirect_gids = {g for g, (_cl, api) in cl_gids.items() if api == "ExecuteIndirect"}
    child_of_indirect = [g for g in unattributed if (g - 1) in indirect_gids]
    print(f"  gid range {lo}..{hi}, unattributed inside: {len(unattributed)}")
    print(f"  of those, exactly ExecuteIndirect+1     : {len(child_of_indirect)}")
    check(
        len(unattributed) > 0 and len(child_of_indirect) == len(unattributed),
        f"every unattributed gid is an indirect child ({len(child_of_indirect)}/{len(unattributed)})",
    )
    check(5099 in unattributed, "5099 is among them")
    check(5098 in indirect_gids, "5098 is an ExecuteIndirect")

    print("\n3. claim 2: 3D-queue gids == CSV gids, both directions")
    # Match the queue name exactly. A prefix match also catches
    # "3D Queue Fence (GPU 0)" (object 2963), which is a fence, never a
    # submission target -- confirmed by zero GetCommandQueue(2963) calls against
    # 61 GetFence(2963) calls. Only objects actually submitted to count as queues.
    submitted_queues = {q for qs in cl2q.values() for q in qs} | set(q_gids.values())
    three_d = [
        obj
        for obj, name in names.items()
        if name == "3D Queue (GPU 0)" and obj in submitted_queues
    ]
    check(len(three_d) == 1, f"exactly one 3D queue object found: {three_d}")
    if not three_d:
        return 1
    q3d = three_d[0]
    derived_3d = {g for g, q in attributed.items() if q == q3d}
    print(f"  gids attributed to queue {q3d} ({names[q3d]!r}): {len(derived_3d)}")
    leak_in = sorted(g for g in present if g in attributed and attributed[g] != q3d)
    leak_out = sorted(g for g in derived_3d if g not in present)
    print(f"  non-3D gids that appear in the CSV : {len(leak_in)} {leak_in[:5]}")
    print(f"  3D gids missing from the CSV       : {len(leak_out)} {leak_out[:5]}")
    check(not leak_in, "no non-3D gid leaks into the CSV")
    check(not leak_out, "no 3D gid is missing from the CSV")

    print("\n4. the gap 4685..5270 should be entirely non-3D")
    gap = [g for g in range(4685, 5271) if g in attributed]
    bad = [g for g in gap if attributed[g] == q3d]
    dist = {}
    for g in gap:
        dist[names.get(attributed[g], attributed[g])] = dist.get(
            names.get(attributed[g], attributed[g]), 0
        ) + 1
    print(f"  attributable gids inside the gap: {len(gap)} -> {dist}")
    check(not bad, f"no 3D-queue gid hides inside the gap ({bad[:5]})")

    print("\n5. queue ownership must be unambiguous")
    multi = {cl: qs for cl, qs in cl2q.items() if len(qs) > 1}
    check(not multi, f"no command list submitted to more than one queue ({list(multi)[:5]})")

    print("\n" + "=" * 70)
    if failures:
        print(f"VERIFICATION FAILED: {len(failures)}")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("VERIFIED: both probe claims hold under an independent derivation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
