"""Show how to get shader bindings for a whole pass.

pass -> its draw calls -> shader-bindings per draw -> aggregate distinct resources
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set import call_tool  # noqa: E402

SESSION = sys.argv[1] if len(sys.argv) > 1 else None
PASS_QUERY = sys.argv[2] if len(sys.argv) > 2 else None


def run(tool: str, **args):
    if SESSION:
        args.setdefault("session", SESSION)
    payload = call_tool(tool, args)
    if payload["status"] == "error":
        raise SystemExit(f"{tool}: {payload['error']['code']}: {payload['error']['message']}")
    return payload["data"]


def main() -> int:
    # 1. pick a pass that actually has draws
    passes = run("list-passes", limit=200, sort_by="draws")["passes"]
    if PASS_QUERY:
        chosen = next((p for p in passes if PASS_QUERY.lower() in p["name"].lower()), None)
        if chosen is None:
            raise SystemExit(f"no pass matching {PASS_QUERY!r}")
    else:
        chosen = next((p for p in passes if p["draw_count"] > 0), passes[0])

    print("=" * 76)
    print(f"pass #{chosen['pass_index']}  {chosen['name']}")
    print(f"  marker_path : {' / '.join(chosen['marker_path'])}")
    print(f"  events      : {chosen['event_count']} "
          f"(draw={chosen['draw_count']} dispatch={chosen['dispatch_count']})")
    print("=" * 76)

    # 2. the draws that belong to this pass
    info = run("pass-info", pass_index=chosen["pass_index"], max_draws=200)
    draws = info.get("draw_calls", [])
    print(f"\npass-info already reports aggregate binding counts:")
    print(f"  {info['resource_summary']}")

    # 3. per-draw shader bindings, aggregated
    print(f"\n--- shader-bindings for each draw ({min(len(draws), 5)} shown) ---")
    srv_ids: dict[int, int] = {}
    uav_ids: dict[int, int] = {}
    cbv_ids: dict[int, int] = {}
    stages_seen: set[str] = set()

    for entry in draws[:5]:
        data = run("shader-bindings", draw_index=entry["draw_index"], max_views=64)
        print(f"\n  draw #{data['draw_index']}  pso={data['pso_id']} "
              f"rootsig={(data.get('root_signature') or {}).get('root_signature_id')}")
        for stage in data["stages"]:
            stages_seen.add(stage["stage"])
            print(f"    {stage['stage']}: declares {stage['declared_count']} register(s)")
            for reg in stage["declared_registers"][:6]:
                print(f"       {reg['id']:>5s} {reg['hlsl_bind']:>6s}  "
                      f"{reg['type']:<12s} {reg['name']}")
        for binding in data["root_bindings"]:
            for view in binding.get("resolved", []):
                rid = view.get("resource_id")
                if rid is None:
                    continue
                kind = view["view_kind"]
                bucket = {"SRV": srv_ids, "UAV": uav_ids, "CBV": cbv_ids}.get(kind)
                if bucket is not None:
                    bucket[rid] = bucket.get(rid, 0) + 1

    # 4. aggregate view for the whole pass
    print("\n" + "=" * 76)
    print("distinct resources bound across the sampled draws of this pass")
    print("=" * 76)
    for label, bucket in (("SRV", srv_ids), ("UAV", uav_ids), ("CBV", cbv_ids)):
        print(f"\n{label}  ({len(bucket)} distinct)")
        for rid, count in sorted(bucket.items(), key=lambda kv: -kv[1])[:8]:
            res = call_tool("texture-info", {"resource_id": rid,
                                             **({"session": SESSION} if SESSION else {}),
                                             "max_views": 1, "max_draws": 1})
            desc = ""
            if res["status"] != "error":
                desc = res["data"]["texture"]["description"]
            print(f"   res#{rid:<6d} bound {count}x  {desc[:52]}")

    print(f"\nstages present in this pass: {sorted(stages_seen)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
