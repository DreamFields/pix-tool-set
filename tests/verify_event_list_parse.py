"""Regression probe for event-list CSV parsing.

pixtool writes `Queue ID, Parent, Name, Global ID` with a space after each
comma, and wraps a Name containing commas in quotes. The quote therefore starts
one character late, so a strict csv.reader does not treat it as a quoting
character and splits the name across cells:

    16298, 16295, "AccessModePass[Graphics] (Textures: 0, Buffers: 2)",

parsed as 5 cells instead of 4. 28 rows in Tiled.events.csv are affected. The
name came out truncated at the comma and every later column shifted left, so
Global ID was read out of the wrong cell.

Run:
    python tests/verify_event_list_parse.py [session-name]
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set.engine import eventlist  # noqa: E402
from pix_tool_set.session import SessionStore  # noqa: E402

SESSION = sys.argv[1] if len(sys.argv) > 1 else "Tiled"

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {message}")
    if not condition:
        failures.append(message)


def main() -> int:
    record = SessionStore().get(SESSION)
    if record is None or not record.event_csv:
        print(f"No session named {SESSION!r} with an event list.")
        return 2
    csv_path = Path(record.event_csv)
    if not csv_path.exists():
        print(f"Event list missing: {csv_path}")
        return 2

    print("1. locate rows with an embedded comma in Name")
    with csv_path.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
        strict = csv.reader(handle)
        header = [c.strip() for c in next(strict)]
        raw_rows = list(strict)
    over_split = [r for r in raw_rows if len(r) != len(header)]
    print(f"  header={header}")
    check(bool(over_split), f"found {len(over_split)} over-split rows to exercise")

    events = eventlist.parse_event_list(csv_path)
    by_queue = {event.queue_id: event for event in events}
    check(len(events) == len(raw_rows), f"parsed every row ({len(events)}/{len(raw_rows)})")

    print("\n2. names are rejoined, not truncated at the comma")
    for row in over_split[:6]:
        queue_id = int(row[0].strip())
        event = by_queue.get(queue_id)
        if event is None:
            check(False, f"queue {queue_id} present in parsed events")
            continue
        expected_fragments = [c.strip().strip('"') for c in row[2:-1] if c.strip()]
        joined_ok = all(frag.strip('"') in event.name for frag in expected_fragments)
        check(joined_ok, f"queue {queue_id}: name keeps all fragments -> {event.name!r}")
        check('"' not in event.name, f"queue {queue_id}: no stray quote in name")

    print("\n3. no name ends mid-parenthesis (the old truncation signature)")
    truncated = [
        e for e in events if e.name.count("(") > e.name.count(")")
    ]
    check(not truncated, f"balanced parentheses everywhere ({[e.name for e in truncated][:3]})")

    print("\n4. Global ID is read from the right column on over-split rows")
    strays = [
        e.queue_id
        for e in events
        if e.global_id is not None and not (0 <= e.global_id <= 10_000_000)
    ]
    check(not strays, f"no absurd global ids ({strays[:5]})")
    # A shifted read used to land on a name fragment, which _to_int turns into
    # None; so also assert the known-good rows still carry their ids.
    with_ids = sum(1 for e in events if e.global_id is not None)
    check(with_ids > 5000, f"{with_ids} events still carry a Global ID")

    print("\n5. tree links survive")
    rooted = sum(1 for e in events if e.parent is not None)
    check(rooted > len(events) * 0.5, f"{rooted}/{len(events)} events have a parent")

    print("\n" + "=" * 66)
    if failures:
        print(f"FAILED: {len(failures)} check(s)")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("PASS: event-list CSV parses cleanly, including quoted names")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
