"""Verify PIX GUI Global ID / Queue ID can drive the pass tools directly.

Ground truth from the user's PIX GUI (Tiled.wpix):
    TileClassificationMark dispatch -> Global ID 3893, Queue ID 18704
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set import call_tool  # noqa: E402
from pix_tool_set.context import clear_capture_cache  # noqa: E402

SESSION = "tiled"
GLOBAL_ID = 3893
QUEUE_ID = 18704
EXPECT = "TileClassificationMark"

PASSED: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    PASSED.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


def run(tool: str, **args):
    args.setdefault("session", SESSION)
    return call_tool(tool, args)


def main() -> int:
    clear_capture_cache()
    print("=" * 78)
    print(f"GUI: {EXPECT}  Global ID={GLOBAL_ID}  Queue ID={QUEUE_ID}")
    print("=" * 78)

    print("\n1. locate-event")
    payload = run("locate-event", queue_id=QUEUE_ID)
    data = payload.get("data") or {}
    leaf = (data.get("event") or {}).get("marker_path", [""])[-1]
    check("queue_id -> pass leaf", leaf == EXPECT, f"leaf={leaf}")

    print("\n2. find-pass --queue-id (global_id must still be reported)")
    payload = run("find-pass", queue_id=QUEUE_ID)
    if payload["status"] == "error":
        check("find-pass by queue_id", False, payload["error"]["message"])
    else:
        m = payload["data"]["matches"][0]
        check("name", m["name"] == EXPECT, f"name={m['name']}")
        check("queue_id echoed", m["queue_id"] == QUEUE_ID, f"queue_id={m['queue_id']}")
        check("global_id still in output", m["global_id"] == GLOBAL_ID,
              f"global_id={m['global_id']}")
        check("draw_index resolved", m["draw_index"] is not None, f"draw_index={m['draw_index']}")

    print("\n3. a global id is now accepted as input")
    payload = run("find-pass", global_id=GLOBAL_ID)
    if payload["status"] == "error":
        check("find-pass --global_id", False, payload["error"]["message"])
    else:
        m = payload["data"]["matches"][0]
        check("find-pass --global_id resolves",
              m["name"] == EXPECT, f"name={m['name']}")

    print("\n4. pass-bindings --queue-id")
    payload = run("pass-bindings", queue_id=QUEUE_ID, stage="CS")
    if payload["status"] == "error":
        check("pass-bindings by queue_id", False, payload["error"]["message"])
    else:
        p = payload["data"]["passes"][0]
        check("name", p["name"] == EXPECT, f"name={p['name']}")
        check("queue_id in payload", p.get("first_queue_id") == QUEUE_ID,
              f"first_queue_id={p.get('first_queue_id')}")
        stages = p["draws"][0]["stages"]
        check("CS shader present", bool(stages), f"stages={[s['stage'] for s in stages]}")
        if stages:
            regs = stages[0]["declared_registers"]
            print(f"       CS declares {len(regs)} registers:")
            for reg in regs[:6]:
                print(f"         {reg['hlsl_bind']:>6s} {reg['type']:<8s} {reg['name']}")

    print("\n5. pass-bindings --queue-id")
    payload = run("pass-bindings", queue_id=QUEUE_ID, stage="CS")
    if payload["status"] == "error":
        check("pass-bindings by queue_id", False, payload["error"]["message"])
    else:
        check("name", payload["data"]["passes"][0]["name"] == EXPECT)

    print("\n6. marker queue_id (the pass row itself, no Global ID in GUI)")
    payload = run("find-pass", name=EXPECT)
    if payload["status"] != "error":
        marker_qid = payload["data"]["matches"][0].get("marker_queue_id")
        print(f"       marker_queue_id={marker_qid}")
        if marker_qid is not None:
            again = run("find-pass", queue_id=marker_qid)
            ok = again["status"] != "error" and again["data"]["matches"][0]["name"] == EXPECT
            check("marker queue_id also resolves", ok)

    print("\n" + "=" * 78)
    print(f"{sum(PASSED)}/{len(PASSED)} checks passed")
    print("=" * 78)
    return 0 if all(PASSED) else 1


if __name__ == "__main__":
    raise SystemExit(main())
