"""Acceptance baseline for the multi-queue Queue ID work.

Written before the two candidate branches land, deliberately: a baseline agreed
after seeing an implementation tends to be shaped by it. These numbers come from
the current pixrev-dev state and any fix must preserve or improve them, never
regress them.

Both plan A (queue attribution) and plan B (draw_index as the primary selector)
must satisfy the invariants below. Anything a branch changes here needs an
explicit justification, not a quiet edit to this file.

Usage:
    python tests/acceptance_queue_baseline.py [session-name]

Exit code 0 means the branch is consistent with the baseline.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set.context import ToolContext  # noqa: E402
from pix_tool_set.engine.model import EventKind  # noqa: E402
from pix_tool_set.session import SessionStore  # noqa: E402

SESSION = sys.argv[1] if len(sys.argv) > 1 else "Tiled"

# Measured on pixrev-dev @ 4c46552. These are facts about Tiled.wpix, not
# preferences, so a branch that changes them has either fixed something real or
# broken something real -- either way it must be explained.
EXPECTED = {
    "draw_calls": 2786,
    "draws_without_queue_id": 90,
    "draws_with_queue_id": 2696,
    "passes_without_queue_id": 72,
    "events": 22155,
    "events_with_global_id": 5334,
    "indirect_calls": 187,
    "indirect_empty_bindings": 0,
    "indirect_without_rootsig": 0,
    "descriptor_tables_bound": 3536,
    "descriptor_tables_empty": 0,
    "resource_3026_reads": 19,
    "resource_3026_writes": 2,
}

failures: list[str] = []
notes: list[str] = []


def check(name: str, actual, expected) -> None:
    ok = actual == expected
    mark = "ok  " if ok else "FAIL"
    print(f"  {mark} {name:<34} actual={actual!r:>8}  expected={expected!r}")
    if not ok:
        failures.append(f"{name}: got {actual!r}, baseline {expected!r}")


def main() -> int:
    if SessionStore().get(SESSION) is None:
        print(f"No session named {SESSION!r}.")
        return 2
    capture = ToolContext.from_cwd().capture({"session": SESSION})
    draws = capture.draw_calls

    print("1. draw call inventory")
    check("draw_calls", len(draws), EXPECTED["draw_calls"])
    missing = [d for d in draws if d.queue_id is None]
    check("draws_without_queue_id", len(missing), EXPECTED["draws_without_queue_id"])
    check(
        "draws_with_queue_id",
        len(draws) - len(missing),
        EXPECTED["draws_with_queue_id"],
    )
    check(
        "passes_without_queue_id",
        len({d.pass_name for d in missing}),
        EXPECTED["passes_without_queue_id"],
    )

    print("\n2. event list")
    check("events", len(capture.events), EXPECTED["events"])
    check(
        "events_with_global_id",
        sum(1 for e in capture.events if e.global_id is not None),
        EXPECTED["events_with_global_id"],
    )

    print("\n3. ExecuteIndirect bindings must stay resolved")
    indirect = [d for d in draws if d.kind is EventKind.EXECUTE_INDIRECT]
    check("indirect_calls", len(indirect), EXPECTED["indirect_calls"])
    check(
        "indirect_empty_bindings",
        sum(1 for d in indirect if not d.bindings),
        EXPECTED["indirect_empty_bindings"],
    )
    check(
        "indirect_without_rootsig",
        sum(1 for d in indirect if d.root_signature_id is None),
        EXPECTED["indirect_without_rootsig"],
    )

    print("\n4. descriptor coverage")
    coverage = capture.descriptor_coverage
    check(
        "descriptor_tables_bound",
        coverage["descriptor_tables_bound"],
        EXPECTED["descriptor_tables_bound"],
    )
    check("descriptor_tables_empty", coverage["tables_empty"], EXPECTED["descriptor_tables_empty"])

    print("\n5. the resource that started all this (ScreenProbeSceneDepth)")
    usage = capture.resource_usage.get(3026, {})
    check("resource_3026_reads", len(usage.get("read_draws", [])), EXPECTED["resource_3026_reads"])
    check(
        "resource_3026_writes",
        len(usage.get("write_draws", [])),
        EXPECTED["resource_3026_writes"],
    )

    print("\n6. every draw must remain addressable by draw_index")
    unreachable = []
    for draw in draws:
        resolved = capture.resolve_draw(draw_index=draw.index, queue_id=None)
        if resolved is None or resolved.index != draw.index:
            unreachable.append(draw.index)
    check("draws_unreachable_by_index", len(unreachable), 0)

    print("\n6b. every draw must also be addressable by global_id")
    unreachable_gid = []
    for draw in draws:
        if draw.global_id is None:
            continue
        resolved = capture.resolve_draw(global_id=draw.global_id)
        if resolved is None or resolved.index != draw.index:
            unreachable_gid.append(draw.global_id)
    check("draws_unreachable_by_global_id", len(unreachable_gid), 0)

    print("\n7. no synthesised Queue IDs (guard against a tempting wrong fix)")
    # If a branch invents ids for the missing queue, these draws would suddenly
    # have queue_id set. That must only happen with a real event list, never by
    # derivation -- the per-queue-index hypothesis was measured and rejected.
    still_missing = sum(1 for d in draws if d.queue_id is None)
    if still_missing == 0:
        notes.append(
            "every draw now has a queue_id: verify this came from a real "
            "per-queue event list and not from synthesising ids"
        )
        print("  WARN all draws have a queue_id now -- must be justified, see note")
    else:
        print(f"  ok   {still_missing} draws still honestly report no queue_id")

    print("\n8. queue attribution, if the branch provides it (plan A only)")
    sample = missing[0] if missing else None
    if sample is not None and hasattr(sample, "queue_name"):
        dist = Counter(getattr(d, "queue_name", None) for d in draws)
        print(f"  info queue distribution: {dict(dist)}")
        unattributed = [d.index for d in draws if not getattr(d, "queue_name", None)]
        check("draws_without_queue_attribution", len(unattributed), 0)
    else:
        print("  skip branch does not expose queue attribution")

    print("\n" + "=" * 72)
    for note in notes:
        print(f"note: {note}")
    if failures:
        print(f"BASELINE VIOLATED: {len(failures)} check(s)")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("BASELINE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
