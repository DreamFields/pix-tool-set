"""Measure whether PIX's Global ID is capture-wide unique, or only per-queue.

The claim under test: "Global ID is a frame-global monotonically increasing
counter, unique across the whole capture, not scoped to one command queue."

This matters because the toolkit currently treats Queue ID as the primary event
selector and Global ID as output-only. Queue ID is row order in a single-queue
CSV export, so it cannot address the actions that ran on any other queue (90 of
2786 on Tiled.wpix). If the claim holds, Global ID is a strictly better primary
selector: it comes from the C++ export, covers every queue, and never silently
resolves to an unrelated event.

Two independent id sources are compared:
  * the C++ export: ``// GlobalId = N`` comments, one per action, all queues.
  * the exported event list CSV: the ``Global ID`` column, one queue only.

The decisive test is #4: the queue-less actions' Global IDs must not collide
with a CSV row that describes a different action. A per-queue counter would
produce exactly such collisions, and``Capture._reconcile_marker_paths``
already resolves marker paths through this lookup, so a collision would mean
the toolkit is already reporting wrong marker paths today.

Usage:
    python tests/verify_global_id_uniqueness.py [session-name ...]
"""

from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set.context import ToolContext, clear_capture_cache  # noqa: E402
from pix_tool_set.engine.model import EventKind  # noqa: E402
from pix_tool_set.session import SessionStore  # noqa: E402

SESSIONS = sys.argv[1:] or ["Tiled", "NoTiled"]

_RE_GLOBAL_ID = re.compile(r"//\s*GlobalId\s*=\s*(\d+)")

PASSED: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    PASSED.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


def info(label: str, detail: str = "") -> None:
    print(f"  [info] {label}" + (f"  {detail}" if detail else ""))


def raw_global_ids(export_dir: Path) -> Counter[int]:
    """Every``// GlobalId = N`` in the export, counted, parser bypassed.

    Read straight off disk so a bug in CommandListParser (e.g. dropping a
    pending id) cannot make the ids look more unique than they are.
    """
    counts: Counter[int] = Counter()
    for path in sorted(export_dir.glob("CommandLists_*.cpp")):
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                match = _RE_GLOBAL_ID.search(line)
                if match:
                    counts[int(match.group(1))] += 1
    return counts


def run_session(name: str) -> None:
    print("=" * 78)
    print(f"Global ID uniqueness on session {name!r}")
    print("=" * 78)

    clear_capture_cache()
    capture = ToolContext.from_cwd().capture({"session": name})
    draws = capture.draw_calls
    events = capture.events
    export_dir = capture.export_dir

    print("\n1. raw ids in the C++ export (all queues, parser bypassed)")
    raw = raw_global_ids(export_dir)
    duplicates = {gid: n for gid, n in raw.items() if n > 1}
    info("distinct GlobalId comments", f"{len(raw)} (total occurrences {sum(raw.values())})")
    if raw:
        info("id range", f"min={min(raw)} max={max(raw)}")
    check(
        "no Global ID appears twice in the export",
        not duplicates,
        f"duplicates={len(duplicates)}"
        + (f" e.g. {list(duplicates.items())[:5]}" if duplicates else ""),
    )

    print("\n2. ids as the parser attached them to actions")
    with_gid = [d for d in draws if d.global_id is not None]
    gid_counts = Counter(d.global_id for d in with_gid)
    parsed_dupes = {gid: n for gid, n in gid_counts.items() if n > 1}
    info("actions", f"{len(draws)}")
    check(
        "every action carries a Global ID",
        len(with_gid) == len(draws),
        f"with_global_id={len(with_gid)} without={len(draws) - len(with_gid)}",
    )
    check(
        "Global ID is unique across all actions",
        not parsed_dupes,
        f"colliding_ids={len(parsed_dupes)}"
        + (f" e.g. {list(parsed_dupes.items())[:5]}" if parsed_dupes else ""),
    )
    with_qid = [d for d in draws if d.queue_id is not None]
    info(
        "coverage comparison",
        f"global_id covers {len(with_gid)}/{len(draws)}, "
        f"queue_id covers {len(with_qid)}/{len(draws)}",
    )

    print("\n3. is it one counter, or one counter per queue?")
    by_queue: dict[str, list[int]] = defaultdict(list)
    for draw in with_gid:
        by_queue[draw.queue_name or "<unattributed>"].append(draw.global_id)
    for queue_name, ids in sorted(by_queue.items()):
        ordered = sorted(ids)
        info(
            f"queue {queue_name!r}",
            f"actions={len(ids)} gid_min={ordered[0]} gid_max={ordered[-1]}",
        )
    names = sorted(by_queue)
    overlaps = []
    for i, left in enumerate(names):
        for right in names[i + 1 :]:
            shared = set(by_queue[left]) & set(by_queue[right])
            if shared:
                overlaps.append((left, right, sorted(shared)[:5], len(shared)))
    check(
        "no two queues share a Global ID",
        not overlaps,
        f"overlapping_pairs={len(overlaps)}"
        + (f" e.g. {overlaps[:2]}" if overlaps else ""),
    )
    if len(names) > 1:
        starts = {n: min(by_queue[n]) for n in names}
        # A per-queue counter restarts near zero on every queue. A frame-global
        # one gives each queue a start that reflects submission order.
        near_zero = [n for n, start in starts.items() if start<= 1]
        check(
            "not every queue restarts its numbering at 0/1",
            len(near_zero) <= 1,
            f"queues_starting_at_0_or_1={near_zero}",
        )
        info("per-queue first id", str(starts))

    print("\n4. DECISIVE: do the queue-less actions collide with CSV rows?")
    by_global = {e.global_id: e for e in events if e.global_id is not None}
    queueless = [d for d in draws if d.queue_id is None and d.global_id is not None]
    info("actions with no Queue ID", f"{len(queueless)}")
    present_in_csv = [d for d in queueless if d.global_id in by_global]
    check(
        "a queue-less action's Global ID is absent from the single-queue CSV",
        not present_in_csv,
        f"unexpectedly_present={len(present_in_csv)}"
        + (
            f" e.g. draw {present_in_csv[0].index} gid={present_in_csv[0].global_id} "
            f"api={present_in_csv[0].api!r} vs csv row "
            f"{by_global[present_in_csv[0].global_id].name!r}"
            if present_in_csv
            else ""
        ),
    )

    print("\n5. where the CSV and the export overlap, they must describe the same action")
    kind_of = {
        EventKind.DRAW: "draw",
        EventKind.DISPATCH: "dispatch",
        EventKind.DISPATCH_RAYS: "dispatch_rays",
        EventKind.EXECUTE_INDIRECT: "execute_indirect",
    }
    mismatched = []
    matched = 0
    for draw in with_gid:
        event = by_global.get(draw.global_id)
        if event is None:
            continue
        matched += 1
        # The CSV names the API call; compare on the coarse action kind, which is
        # what a per-queue collision would break (a draw landing on a dispatch row).
        if kind_of.get(event.kind) != kind_of.get(draw.kind):
            mismatched.append((draw.index, draw.global_id, draw.api, event.name, event.kind.value))
    info("actions matched against a CSV row", f"{matched}")
    check(
        "matched rows agree on the action kind",
        not mismatched,
        f"mismatched={len(mismatched)}" + (f" e.g. {mismatched[:3]}" if mismatched else ""),
    )

    print("\n6. is the counter monotonic in submission order?")
    ids_in_order = [d.global_id for d in draws if d.global_id is not None]
    ascending = all(a< b for a, b in zip(ids_in_order, ids_in_order[1:]))
    inversions = sum(1 for a, b in zip(ids_in_order, ids_in_order[1:]) if a >= b)
    check(
        "Global ID ascends with draw_index",
        ascending,
        f"inversions={inversions}"
        + ("" if ascending else " -- export file order is not submission order"),
    )
    per_queue_ascending = all(
        all(a < b for a, b in zip(ids, ids[1:]))
        for ids in (
            [d.global_id for d in draws if (d.queue_name or "<unattributed>") == q
             and d.global_id is not None]
            for q in by_queue
        )
    )
    info("ascends within each queue", str(per_queue_ascending))

    print("\n7. the CSV column itself")
    csv_gids = [e.global_id for e in events if e.global_id is not None]
    csv_dupes = {gid: n for gid, n in Counter(csv_gids).items() if n > 1}
    info("rows", f"{len(events)} of which {len(csv_gids)} carry a Global ID")
    check("Global ID is unique within the CSV", not csv_dupes, f"duplicates={len(csv_dupes)}")
    if csv_gids:
        info("csv id range", f"min={min(csv_gids)} max={max(csv_gids)}")
    # More CSV rows carry an id than there are actions: PIX numbers non-action
    # commands (copies, clears, barriers...) too. Reported so the plan does not
    # assume "global_id == action".
    kinds = Counter(e.kind.value for e in events if e.global_id is not None)
    info("kinds carrying a Global ID", str(dict(kinds.most_common())))

    print("\n8. limits a global_id selector has to respect")
    markers = [e for e in events if e.kind is EventKind.MARKER]
    check(
        "no marker carries a Global ID (so Queue ID cannot be retired)",
        all(e.global_id is None for e in markers),
        f"markers={len(markers)} with_global_id="
        f"{sum(1 for e in markers if e.global_id is not None)}",
    )
    # Both ids are plain integers and the Queue ID space (row order) contains the
    # whole Global ID space, so no amount of range checking can tell a caller's
    # 1611 apart. Named parameters are the only defence.
    qids = {e.queue_id for e in events}
    gids = {e.global_id for e in events if e.global_id is not None} | set(gid_counts)
    info(
        "id spaces",
        f"queue_id {min(qids)}..{max(qids)} ({len(qids)}), "
        f"global_id {min(gids)}..{max(gids)} ({len(gids)}), "
        f"valid in both = {len(gids & qids)}",
    )
    confusable = sum(1 for d in with_gid if d.queue_id is not None and d.queue_id == d.global_id)
    check(
        "no action has queue_id == global_id, so a mix-up is always wrong",
        confusable == 0,
        f"coincidences={confusable}",
    )
    # The Global IDs a global_id selector cannot resolve to a C++ action: PIX
    # numbers each sub-action an ExecuteIndirect expands to, and the export only
    # emits the ExecuteIndirect itself. These need their own error wording.
    action_kinds = (
        EventKind.DRAW,
        EventKind.DISPATCH,
        EventKind.DISPATCH_RAYS,
        EventKind.EXECUTE_INDIRECT,
    )
    orphans = [
        e
        for e in events
        if e.kind in action_kinds and e.global_id is not None and e.global_id not in gid_counts
    ]
    from_indirect = sum(
        1 for e in orphans if e.parent is not None and e.parent.kind is EventKind.EXECUTE_INDIRECT
    )
    info(
        "CSV action rows with no C++ action",
        f"{len(orphans)} of which {from_indirect} are children of an ExecuteIndirect",
    )
    check(
        "every such row is an ExecuteIndirect expansion, not an unexplained gap",
        len(orphans) == from_indirect,
        f"unexplained={len(orphans) - from_indirect}",
    )
    print()


def main() -> int:
    store = SessionStore()
    ran = 0
    for name in SESSIONS:
        if store.get(name) is None:
            print(f"skip: no session named {name!r}")
            continue
        run_session(name)
        ran += 1
    if not ran:
        print("No usable session.")
        return 2
    print("=" * 78)
    print(f"{sum(PASSED)}/{len(PASSED)} checks passed")
    print("=" * 78)
    return 0 if all(PASSED) else 1


if __name__ == "__main__":
    raise SystemExit(main())
