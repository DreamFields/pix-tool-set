"""Print the full read/write timeline of one resource as a readable table."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

SESSION = sys.argv[1] if len(sys.argv) > 1 else "Tiled"
RESOURCE = sys.argv[2] if len(sys.argv) > 2 else "3026"


def main() -> int:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pix_tool_set.cli",
            "resource-usage",
            "--session",
            SESSION,
            "--resource-id",
            RESOURCE,
        ],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[1] / "src"),
    )
    payload = json.loads(proc.stdout)
    data = payload["data"]

    print(data["resource"]["description"])
    summary = data["summary"]
    print(
        f"reads={summary['read_draw_count']}  writes={summary['write_draw_count']}  "
        f"passes={summary['pass_count']}  descriptors={summary['descriptor_count']}"
    )
    print()
    print(f"{'draw':>6} {'global':>7} {'queue':>7} {'api':<16} {'access':<12} pass")
    print("-" * 100)
    for entry in data["timeline"]:
        gid = entry.get("global_id")
        qid = entry.get("queue_id")
        print(
            f"{entry['draw_index']:>6} "
            f"{gid if gid is not None else '-':>7} "
            f"{qid if qid is not None else '-':>7} "
            f"{entry['api']:<16} "
            f"{'+'.join(entry['access']):<12} "
            f"{entry['pass_name']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
