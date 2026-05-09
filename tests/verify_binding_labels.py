"""Check the reproduced Binding column against the PIX GUI screenshot.

Ground truth for GBufferA (resource 756) in Tiled.wpix, read off the PIX resource
history view. GUI Global IDs are used as the key; for ExecuteIndirect the GUI
shows the expanded child's id, which is our id + 1.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pix_tool_set.engine import bindinglabel  # noqa: E402
from pix_tool_set.engine.capture import Capture  # noqa: E402
from pix_tool_set.session import SessionStore  # noqa: E402

RESOURCE = 756

GUI = {
    3851: "OM RTV 1",
    3854: "OM RTV 1",
    3893: "CS SRV 2",
    3968: "CS SRV 7",
    4891: "CS SRV 1",
    4904: "CS SRV 1",
    4908: "CS SRV 1",
    4919: "CS SRV 1",
    5206: "CS SRV 1",
    5210: "CS SRV 1",
    5216: "CS SRV 1",
    5275: "CS SRV 1",
    5286: "CS SRV 1",
    5378: "CS SRV 1",
    5387: "CS SRV 1",
    5396: "CS SRV 1",
    5409: "PS SRV 8",
    5417: "CS SRV 2",
    5484: "PS SRV 3",
    5592: "PS SRV 1",
}


def main() -> int:
    store = SessionStore()
    record = store.resolve(session="Tiled")
    capture = Capture(
        Path(record.capture_path) if record.capture_path else None,
        Path(record.export_dir),
        Path(record.event_csv) if record.event_csv else None,
    )

    by_gui_gid = {}
    for draw in capture.draw_calls:
        if draw.global_id is None:
            continue
        by_gui_gid[draw.global_id] = draw
        if draw.api == "ExecuteIndirect":
            by_gui_gid.setdefault(draw.global_id + 1, draw)

    match = miss = 0
    print(f"{'gid':>6}  {'GUI':<12} {'ours':<26} verdict")
    print("-" * 84)
    for gid, expected in sorted(GUI.items()):
        draw = by_gui_gid.get(gid)
        if draw is None:
            print(f"{gid:>6}  {expected:<12} {'<no such event>':<26} MISS")
            miss += 1
            continue
        labels = bindinglabel.labels_for(capture, draw, RESOURCE)
        texts = [entry.text for entry in labels]
        ok = expected in texts
        primary = texts[0] if texts else "<none>"
        print(
            f"{gid:>6}  {expected:<12} {primary:<26} "
            f"{'MATCH' if ok else 'MISMATCH ' + str(texts)}"
        )
        if ok:
            match += 1
        else:
            miss += 1

    print()
    print(f"match={match}  miss={miss}  of {len(GUI)}")
    return 0 if miss == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
