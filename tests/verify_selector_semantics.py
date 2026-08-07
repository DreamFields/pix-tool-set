"""Verify what each event selector can and cannot address on a multi-queue capture.

The toolkit originally treated the exported event list's Queue ID as its single event
identifier, on the reasoning that every row of that list carries one whereas Global ID
appears only on actions. That is true of the list itself and false of the capture: the
export covers a single command queue, so actions submitted elsewhere have no row at all.
This file pins down the consequences that decision had to be relaxed for.

Three properties are asserted, in order of how much damage getting them wrong causes:

1. ``draw_index`` addresses every action, including the ones with no Queue ID. If this
   breaks there is no way to reach part of the frame.
2. A Queue ID miss is diagnosed specifically. The column is row order in this export, so
   every integer below the row count names *some* row -- an id copied out of the PIX GUI
   of a multi-queue capture is answered with confident data about an unrelated event
   rather than an error. Only the wording of a near miss can warn about that.
3. The existing Queue ID path still resolves exactly as before, so relaxing the rule
   costs nothing for captures that only ever had one queue.

Usage:
    python tests/verify_selector_semantics.py [session-name]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set import call_tool  # noqa: E402
from pix_tool_set.context import ToolContext, clear_capture_cache  # noqa: E402
from pix_tool_set.engine.model import EventKind  # noqa: E402
from pix_tool_set.session import SessionStore  # noqa: E402
from pix_tool_set.tools._common import queue_id_coverage  # noqa: E402

SESSION = sys.argv[1] if len(sys.argv) > 1 else "Tiled"

# Measured on Tiled.wpix. Named here so a change in the capture's shape shows up as a
# failure in this file rather than as a silently weaker test.
EXPECTED_DRAWS = 2786
EXPECTED_WITHOUT_QUEUE_ID = 90
EXPECTED_EVENT_ROWS = 22155

# A pass that exists only in the C++ export's marker tree, used as the worked example of
# "I can see it in the PIX GUI, how do I name it here?".
SAMPLE_PASS = "CompactTraces WaveOps"
SAMPLE_DRAW_INDICES = (2671, 2676)

PASSED: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    PASSED.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


def run(tool: str, **args):
    args.setdefault("session", SESSION)
    return call_tool(tool, args)


def main() -> int:
    if SessionStore().get(SESSION) is None:
        print(f"No session named {SESSION!r}.")
        return 2
    clear_capture_cache()
    capture = ToolContext.from_cwd().capture({"session": SESSION})
    draws = capture.draw_calls
    coverage = queue_id_coverage(capture)

    print("=" * 78)
    print(f"selector semantics on {SESSION}")
    print("=" * 78)

    print("\n1. the shape of the problem")
    check("draw count", len(draws) == EXPECTED_DRAWS, f"draws={len(draws)}")
    check(
        "actions with no Queue ID",
        coverage["draws_without_queue_id"] == EXPECTED_WITHOUT_QUEUE_ID,
        f"missing={coverage['draws_without_queue_id']}",
    )
    check(
        "coverage helper agrees the export is single-queue",
        coverage["event_list_is_single_queue"] is True,
        f"rows={coverage['event_list_rows']}",
    )

    print("\n2. the Queue ID column is row order, which is why a wrong id is dangerous")
    qids = [event.queue_id for event in capture.events]
    check(
        "qids == range(0, row count)",
        qids == list(range(len(capture.events))),
        f"rows={len(qids)}",
    )
    check(
        "row count as measured",
        len(capture.events) == EXPECTED_EVENT_ROWS,
        f"rows={len(capture.events)}",
    )

    print("\n3. draw_index addresses every action, including the 90 without a Queue ID")
    unreachable = []
    missing_unreachable = []
    for draw in draws:
        resolved = capture.resolve_draw(draw_index=draw.index, queue_id=None)
        if resolved is None or resolved.index != draw.index:
            unreachable.append(draw.index)
            if draw.queue_id is None:
                missing_unreachable.append(draw.index)
    check("every draw_index resolves", not unreachable, f"unreachable={len(unreachable)}")
    check(
        "the queue-less actions resolve too",
        not missing_unreachable,
        f"unreachable_without_queue_id={len(missing_unreachable)}",
    )

    print("\n4. a tool driven by draw_index alone works on a queue-less action")
    sample = next((d for d in draws if d.queue_id is None), None)
    if sample is None:
        check("a queue-less action exists to test with", False)
    else:
        payload = run("draw-state", draw_index=sample.index)
        check(
            "draw-state by draw_index succeeds",
            payload["status"] != "error",
            f"draw_index={sample.index} status={payload['status']}",
        )
        messages = " ".join(d.get("message", "") for d in payload.get("diagnostics", []))
        check(
            "and says why the Queue ID is absent",
            "no queue id" in messages.lower(),
            "diagnostic present" if "no queue id" in messages.lower() else "no diagnostic",
        )
        # Check what the message has to establish, not how it words it. The merged
        # wording names the actual queue instead of saying "several command queues",
        # which is strictly more useful, and an assertion on the old phrasing would
        # have rejected the better message.
        lowered = messages.lower()
        check(
            "the null id is not passed off as data",
            "does not cover that queue" in lowered
            and "cannot be derived" in lowered
            and f"draw_index={sample.index}" in messages,
            f"names the queue and the working selector: {'yes' if 'does not cover that queue' in lowered else 'no'}",
        )

    print("\n5. a Queue ID miss is diagnosed, and warns about the row-order trap")
    beyond = len(capture.events) + 1000
    payload = run("draw-state", queue_id=beyond)
    check("out-of-range id errors", payload["status"] == "error", f"queue_id={beyond}")
    if payload["status"] == "error":
        hint = payload["error"].get("suggestion") or ""
        check("says the list has no such row", "none carries this id" in hint)
        check("warns the column is row order", "row number" in hint)

    # An id that names a real row which is not an action: the near miss that proves ids
    # are not validated against intent, only against existence.
    non_action = next(
        (
            event
            for event in capture.events
            if event.kind not in (EventKind.DRAW, EventKind.DISPATCH, EventKind.EXECUTE_INDIRECT)
            and event.global_id is None
        ),
        None,
    )
    if non_action is not None:
        payload = run("draw-state", queue_id=non_action.queue_id)
        check(
            "in-range non-action id errors",
            payload["status"] == "error",
            f"queue_id={non_action.queue_id} row={non_action.name!r}",
        )
        if payload["status"] == "error":
            hint = payload["error"].get("suggestion") or ""
            check("names the row actually hit", non_action.name in hint)
            check("distinguishes it from a non-existent id", "is not an action" in hint)

    print("\n6. a pass missing from the event list is diagnosed, not silently empty")
    missing_pass = next(
        (p for p in capture.passes if p.get("first_queue_id") is None), None
    )
    if missing_pass is None:
        check("a queue-less pass exists to test with", False)
    else:
        payload = run("find-pass", name=missing_pass["name"])
        check(
            "find-pass reaches it by name",
            payload["status"] != "error",
            f"name={missing_pass['name']!r}",
        )
        if payload["status"] != "error":
            row = payload["data"]["matches"][0]
            check("and hands out a draw_index", row["draw_index"] is not None,
                  f"draw_index={row['draw_index']}")
            check(
                "and explains the null Queue ID",
                "queue_id_unavailable" in row,
                row.get("queue_id_unavailable", "")[:48],
            )

    print("\n7. locating a pass seen in the PIX GUI, by name only")
    payload = run("locate-event", pass_name=SAMPLE_PASS)
    check("locate-event accepts a pass name", payload["status"] != "error")
    if payload["status"] != "error":
        data = payload["data"]
        check(
            "lands on the expected action",
            data["draw_index"] == SAMPLE_DRAW_INDICES[0],
            f"draw_index={data['draw_index']}",
        )
        check("reports it as ExecuteIndirect", data["api"] == "ExecuteIndirect",
              f"api={data['api']}")
    payload = run("find-draw-calls", pass_name=SAMPLE_PASS)
    if payload["status"] != "error":
        found = [d["draw_index"] for d in payload["data"]["draw_calls"]]
        check(
            "find-draw-calls returns both indirect calls",
            tuple(found) == SAMPLE_DRAW_INDICES,
            f"draw_indices={found}",
        )

    print("\n8. the existing Queue ID path is unchanged")
    with_id = [d for d in draws if d.queue_id is not None]
    mismatched = []
    for draw in with_id:
        resolved = capture.resolve_draw(draw_index=None, queue_id=draw.queue_id)
        if resolved is None or resolved.index != draw.index:
            mismatched.append(draw.index)
    check(
        "every existing Queue ID still resolves to the same draw",
        not mismatched,
        f"checked={len(with_id)} mismatched={len(mismatched)}",
    )
    probe = with_id[len(with_id) // 2]
    payload = run("draw-state", queue_id=probe.queue_id)
    resolved_index = (
        (payload.get("data") or {}).get("draw_call", {}).get("draw_index")
        if payload["status"] != "error"
        else None
    )
    check(
        "draw-state by queue_id still succeeds",
        payload["status"] != "error" and resolved_index == probe.index,
        f"queue_id={probe.queue_id} -> draw_index={resolved_index} (expected {probe.index})",
    )

    print("\n9. Global ID remains output-only")
    payload = run("find-pass", global_id=probe.global_id)
    check(
        "find-pass still rejects a global_id",
        payload["status"] == "error",
        f"status={payload['status']}",
    )

    print("\n" + "=" * 78)
    print(f"{sum(PASSED)}/{len(PASSED)} checks passed")
    print("=" * 78)
    return 0 if all(PASSED) else 1


if __name__ == "__main__":
    raise SystemExit(main())
