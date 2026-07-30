"""Look up a pass by PIX GUI Queue ID and inspect its shader source.

Question under test: Queue ID = 18461 in Tiled.wpix.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set import call_tool  # noqa: E402

SESSION = "tiled"
QUEUE_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 18461


def run(tool, **args):
    args.setdefault("session", SESSION)
    return call_tool(tool, args)


def main() -> int:
    print("=" * 78)
    print(f"Queue ID = {QUEUE_ID}")
    print("=" * 78)

    # 1. what event is this?
    payload = run("locate-event", queue_id=QUEUE_ID)
    if payload["status"] == "error":
        print(f"locate-event: {payload['error']['code']}: {payload['error']['message']}")
    else:
        data = payload["data"]
        ev = data.get("event") or {}
        print(f"\nevent name  : {ev.get('name')}")
        print(f"kind        : {ev.get('kind')}")
        print(f"global_id   : {ev.get('global_id')}")
        print(f"is_draw     : {ev.get('is_draw')}")
        print(f"child_count : {ev.get('child_count')}")
        print(f"marker_path : {' / '.join((ev.get('marker_path') or [])[-4:])}")
        print(f"draw_index  : {data.get('draw_index')}")

    # 2. which pass does it map to?
    payload = run("find-pass", queue_id=QUEUE_ID)
    if payload["status"] == "error":
        print(f"\nfind-pass: {payload['error']['code']}: {payload['error']['message']}")
        return 1
    match = payload["data"]["matches"][0]
    print(f"\npass_index      : {match['pass_index']}")
    print(f"pass name       : {match['name']}")
    print(f"subsystem       : {match['subsystem']}")
    print(f"draw_index      : {match['draw_index']}")
    print(f"global_id       : {match['global_id']}")
    print(f"queue_id        : {match['queue_id']}")
    print(f"marker_queue_id : {match['marker_queue_id']}")
    print(f"draws/dispatch  : {match['draw_count']}/{match['dispatch_count']}")
    print(f"pso_ids         : {match['pso_ids']}")

    # 3. what shaders does it use, and is real HLSL available?
    payload = run("pass-bindings", queue_id=QUEUE_ID)
    if payload["status"] == "error":
        print(f"\npass-bindings: {payload['error']['message']}")
        return 1
    entry = payload["data"]["passes"][0]
    print("\n" + "-" * 78)
    print("shaders in this pass")
    print("-" * 78)
    for draw in entry["draws"]:
        print(f"\ndraw_index={draw['draw_index']} pso={draw['pso_id']}")
        for stage in draw["stages"]:
            shader = stage["shader"]
            print(f"  {stage['stage']:4s} hash={shader['shader_hash']}")
            print(f"       size={shader['byte_size']:,} B  debug={shader.get('debug_name')}")

            info = run(
                "shader-info", pso_id=draw["pso_id"], stage=stage["stage"]
            )
            if info["status"] != "error":
                print(f"       has_embedded_source={info['data']['has_embedded_source']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
