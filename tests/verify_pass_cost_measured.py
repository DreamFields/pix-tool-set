"""Confirm pass-cost now reports measured GPU time instead of only an estimate."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set import call_tool  # noqa: E402
from pix_tool_set.context import clear_capture_cache  # noqa: E402


def main() -> int:
    clear_capture_cache()
    payload = call_tool("pass-cost", {"session": "tiled", "limit": 10})
    data = payload["data"]

    print("=" * 78)
    print(f"status : {payload['status']}")
    print(f"model  : {data['model']}")
    print(f"measured passes : {data['measured_pass_count']}")
    print(f"measured total  : {data['measured_total_ms']} ms")
    print(f"timing column   : {data['timing_column']}")
    print(f"sort_by         : {data['sort_by']}")
    print("=" * 78)

    print(f"\n{'ms':>10s} {'share%':>7s}  {'events':>6s}  pass")
    for row in data["passes"]:
        ms = row.get("measured_duration_ms") or 0.0
        share = row.get("measured_share_percent") or 0.0
        events = row.get("measured_event_count") or 0
        print(f"{ms:>10.3f} {share:>7.2f}  {events:>6d}  {row['name'][:44]}")

    print()
    for entry in payload.get("diagnostics", [])[:3]:
        print(f"  [{entry['level']}] {entry['message'][:100]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
