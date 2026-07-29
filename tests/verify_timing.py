"""Verify measured GPU timing is wired into the tools."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set import call_tool  # noqa: E402
from pix_tool_set.context import clear_capture_cache  # noqa: E402

SESSION = "tiled"
GLOBAL_ID = 3893
QUEUE_ID = 18704


def run(tool, **args):
    args.setdefault("session", SESSION)
    return call_tool(tool, args)


def main() -> int:
    clear_capture_cache()

    print("=" * 78)
    print("export-timing (should reuse the cached CSV)")
    print("=" * 78)
    payload = run("export-timing")
    print(f"  status={payload['status']}")
    data = payload.get("data") or {}
    print(f"  reused_cache={data.get('reused_cache')}")
    timing = data.get("timing") or {}
    print(f"  timing_column={timing.get('timing_column')}")
    print(f"  measured_events={timing.get('measured_events')}")
    print(f"  counter_columns={len(timing.get('counter_columns') or [])}")

    print("\n" + "=" * 78)
    print(f"event-timing --global-id {GLOBAL_ID}  (the GUI id under test)")
    print("=" * 78)
    payload = run("event-timing", global_id=GLOBAL_ID)
    if payload["status"] == "error":
        print(f"  ERROR {payload['error']['message']}")
    else:
        d = payload["data"]
        ev = d["event"]
        print(f"  queue_id     : {ev['queue_id']}")
        print(f"  global_id    : {ev['global_id']}")
        print(f"  name         : {ev['name']}")
        print(f"  duration     : {ev['duration_ms']} ms  ({ev['duration_ns']:,} ns)")
        print(f"  pass         : {d['pass']}")

    print("\n" + "=" * 78)
    print("event-timing --group-by pass  (top 10 measured passes)")
    print("=" * 78)
    payload = run("event-timing", group_by="pass", limit=10)
    if payload["status"] == "error":
        print(f"  ERROR {payload['error']['message']}")
    else:
        d = payload["data"]
        print(f"  measured_events : {d['measured_events']:,}")
        print(f"  total           : {d['total_duration_ms']} ms")
        print(f"  {'ms':>10s}  {'events':>6s}  pass")
        for row in d["rows"]:
            print(f"  {row['duration_ms']:>10.3f}  {row['measured_events']:>6d}  {row['name'][:48]}")

    print("\n" + "=" * 78)
    print("cross-check: measured vs estimated (pass-cost)")
    print("=" * 78)
    payload = run("pass-cost", limit=5)
    print(f"  pass-cost status={payload['status']}")
    for entry in payload.get("diagnostics", [])[:2]:
        print(f"    [{entry['level']}] {entry['message'][:96]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
