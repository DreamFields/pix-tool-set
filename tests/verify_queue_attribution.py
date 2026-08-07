"""Regression probe for command-queue attribution on a multi-queue capture.

Tiled.wpix is submitted to three queues, but `pixtool save-event-list` writes one
CSV covering a single queue, so 90 of 2786 draws have no Queue ID at all. This
file asserts what the fix is allowed to claim: that every draw's *queue* is known
from the C++ export, and that no Queue ID was invented to paper over the gap.

The two halves matter equally. Attribution without the honesty check would pass
just as happily if queue_id had been synthesised, and a synthesised id is worse
than None: it is accepted by every selector and addresses the wrong row.

Run:
    python tests/verify_queue_attribution.py [session-name]
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set.context import ToolContext  # noqa: E402
from pix_tool_set.session import SessionStore  # noqa: E402

SESSION = sys.argv[1] if len(sys.argv) > 1 else "Tiled"

# Measured against the export, not chosen: FrameResources_000.cpp declares three
# queues and names them, and RenderFrameWorker_000.cpp submits every command list
# to exactly one of them. The two draw counts add up to the full draw list, which
# is what makes "no draw is unattributed" a real assertion rather than a tautology.
DIRECT_QUEUE = 1
COMPUTE_QUEUE = 11
COPY_QUEUE = 2988
EXPECTED_NAMES = {
    DIRECT_QUEUE: "3D Queue (GPU 0)",
    COMPUTE_QUEUE: "Compute Queue (GPU 0)",
    COPY_QUEUE: "Copy Queue (GPU 0)",
}
EXPECTED_DRAWS_WITH_ID = 2696
EXPECTED_DRAWS_WITHOUT_ID = 90

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {message}")
    if not condition:
        failures.append(message)


def main() -> int:
    if SessionStore().get(SESSION) is None:
        print(f"No session named {SESSION!r}.")
        return 2
    capture = ToolContext.from_cwd().capture({"session": SESSION})
    ownership = capture.command_queues
    draws = capture.draw_calls

    print("1. all three queues are found and named as PIX shows them")
    check(
        len(ownership.queues) == len(EXPECTED_NAMES),
        f"queue count is {len(ownership.queues)}, expected {len(EXPECTED_NAMES)}",
    )
    for api_id, expected_name in EXPECTED_NAMES.items():
        queue = ownership.queues.get(api_id)
        if queue is None:
            check(False, f"queue object {api_id} present")
            continue
        check(queue.name == expected_name, f"queue {api_id} named {queue.name!r}")
    # The type must come from D3D12_COMMAND_LIST_TYPE where it was exported, not
    # from the name, otherwise a renamed queue would silently change category.
    types = {api_id: ownership.queues[api_id].queue_type for api_id in EXPECTED_NAMES}
    check(types[DIRECT_QUEUE] == "direct", f"queue {DIRECT_QUEUE} type {types[DIRECT_QUEUE]!r}")
    check(types[COMPUTE_QUEUE] == "compute", f"queue {COMPUTE_QUEUE} type {types[COMPUTE_QUEUE]!r}")
    check(types[COPY_QUEUE] == "copy", f"queue {COPY_QUEUE} type {types[COPY_QUEUE]!r}")

    print("\n2. no draw is left without a queue")
    unattributed = [d.index for d in draws if not d.queue_name]
    check(not unattributed, f"every draw has a queue ({unattributed[:5]})")
    check(
        all(d.queue_object_id is not None for d in draws),
        "every draw carries a queue_object_id",
    )
    distribution = Counter(d.queue_name for d in draws)
    print(f"  info distribution: {dict(distribution)}")

    print("\n3. the draws missing a Queue ID all belong to the compute queue")
    missing = [d for d in draws if d.queue_id is None]
    check(
        len(missing) == EXPECTED_DRAWS_WITHOUT_ID,
        f"{len(missing)} draws lack a Queue ID, expected {EXPECTED_DRAWS_WITHOUT_ID}",
    )
    wrong_queue = [d.index for d in missing if d.queue_object_id != COMPUTE_QUEUE]
    check(
        not wrong_queue,
        f"all {len(missing)} attributed to queue {COMPUTE_QUEUE} ({wrong_queue[:5]})",
    )
    check(
        all(d.queue_name == EXPECTED_NAMES[COMPUTE_QUEUE] for d in missing),
        f"all named {EXPECTED_NAMES[COMPUTE_QUEUE]!r}",
    )

    print("\n4. the draws that have a Queue ID all belong to the queue the CSV covers")
    present = [d for d in draws if d.queue_id is not None]
    check(
        len(present) == EXPECTED_DRAWS_WITH_ID,
        f"{len(present)} draws have a Queue ID, expected {EXPECTED_DRAWS_WITH_ID}",
    )
    wrong_queue = [d.index for d in present if d.queue_object_id != DIRECT_QUEUE]
    check(
        not wrong_queue,
        f"all {len(present)} attributed to queue {DIRECT_QUEUE} ({wrong_queue[:5]})",
    )
    check(
        len(present) + len(missing) == len(draws),
        f"the two groups account for every draw ({len(draws)})",
    )

    print("\n5. no command list crosses queues")
    # If one did, "which queue ran this draw" would have no single answer and the
    # attribution above would be a guess. The parser deliberately reports such a
    # list as ownerless rather than picking a queue, so this must stay empty.
    check(
        not ownership.ambiguous_command_lists,
        f"no command list submitted to two queues ({ownership.ambiguous_command_lists})",
    )
    drawn_lists = {d.command_list_id for d in draws if d.command_list_id is not None}
    unmapped = sorted(drawn_lists - set(ownership.command_list_to_queue))
    check(not unmapped, f"every command list holding draws is mapped ({unmapped[:5]})")

    print("\n6. attribution did not become a licence to invent Queue IDs")
    # The whole point of plan A: it explains the gap, it does not fill it.
    check(
        len(missing) == EXPECTED_DRAWS_WITHOUT_ID,
        f"{len(missing)} draws still honestly report queue_id=None",
    )
    sample = missing[0]
    payload = sample.queue_attribution
    check(payload["queue_id"] is None, f"draw {sample.index} reports queue_id=None")
    check(
        payload["queue_id_available"] is False,
        f"draw {sample.index} declares its Queue ID unavailable",
    )
    check(
        payload["selector"] == {"draw_index": sample.index},
        f"draw {sample.index} names draw_index as the working selector",
    )
    check(bool(payload.get("reason")), "the payload explains why no Queue ID exists")

    print("\n7. frame-level summary agrees with the per-draw view")
    summary = capture.queue_attribution
    check(summary["queue_count"] == len(EXPECTED_NAMES), f"queue_count {summary['queue_count']}")
    check(
        summary["draws_without_queue_owner"] == 0,
        f"draws_without_queue_owner {summary['draws_without_queue_owner']}",
    )
    check(
        summary["event_list_covers_queue_object_ids"] == [DIRECT_QUEUE],
        f"event list covers {summary['event_list_covers_queue_object_ids']}",
    )
    check(
        summary["event_list_is_complete"] is False,
        "the summary admits the event list is incomplete",
    )
    by_id = {entry["queue_object_id"]: entry for entry in summary["queues"]}
    check(
        by_id[DIRECT_QUEUE]["draw_count"] == EXPECTED_DRAWS_WITH_ID,
        f"queue {DIRECT_QUEUE} draw_count {by_id[DIRECT_QUEUE]['draw_count']}",
    )
    check(
        by_id[COMPUTE_QUEUE]["draw_count"] == EXPECTED_DRAWS_WITHOUT_ID,
        f"queue {COMPUTE_QUEUE} draw_count {by_id[COMPUTE_QUEUE]['draw_count']}",
    )
    check(
        by_id[COMPUTE_QUEUE]["draws_with_queue_id"] == 0,
        f"queue {COMPUTE_QUEUE} draws_with_queue_id {by_id[COMPUTE_QUEUE]['draws_with_queue_id']}",
    )

    print("\n8. the queue qualifier narrows a lookup instead of guessing")
    reference = present[0]
    check(
        capture.resolve_draw(queue_id=reference.queue_id, queue_name="3D") is not None,
        f"queue_id={reference.queue_id} resolves when qualified with its own queue",
    )
    check(
        capture.resolve_draw(queue_id=reference.queue_id, queue_name="Compute") is None,
        f"queue_id={reference.queue_id} fails cleanly when qualified with another queue",
    )
    compute_draws = capture.draws_on_queue(queue_name="Compute")
    check(
        len(compute_draws) == EXPECTED_DRAWS_WITHOUT_ID,
        f"draws_on_queue('Compute') returns {len(compute_draws)} draws",
    )

    print("\n" + "=" * 70)
    if failures:
        print(f"FAILED: {len(failures)} check(s)")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("PASS: every draw's queue is known, and no Queue ID was invented")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
