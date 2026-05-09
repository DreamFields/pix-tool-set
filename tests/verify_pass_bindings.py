"""Compare the report's 3-step recipe against the new one-shot tools."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set import call_tool  # noqa: E402

SESSION = sys.argv[1] if len(sys.argv) > 1 else "tiled"
NAME = sys.argv[2] if len(sys.argv) > 2 else "TileClassificationBuildLists"


def run(tool, **args):
    args.setdefault("session", SESSION)
    payload = call_tool(tool, args)
    if payload["status"] == "error":
        raise SystemExit(f"{tool}: {payload['error']['code']}: {payload['error']['message']}")
    return payload


def main() -> int:
    print("=" * 78)
    print("NEW: single call -> pass-bindings")
    print("=" * 78)
    payload = run("pass-bindings", pass_name=NAME, stage="CS", all_matches=True)
    data = payload["data"]
    print(f"status        : {payload['status']}")
    print(f"passes found  : {data['pass_count']}")
    print(f"trust summary : {data['trust_summary']}")
    for entry in payload["diagnostics"]:
        print(f"  [{entry['level']}] {entry['message'][:100]}")

    for pass_entry in data["passes"]:
        print("\n" + "-" * 78)
        print(f"pass #{pass_entry['pass_index']}  {pass_entry['name']}")
        print(f"  subsystem : {pass_entry['marker_path'][-2]}")
        print(f"  dispatches: {pass_entry['dispatch_count']}  psos={pass_entry['distinct_pso_ids']}")
        for draw in pass_entry["draws"]:
            print(f"  draw_index={draw['draw_index']} global_id={draw['global_id']}")
            for stage in draw["stages"]:
                shader = stage["shader"]
                print(
                    f"    {stage['stage']} hash={shader['shader_hash'][:16]}… "
                    f"{shader['byte_size']}B  declares {stage['declared_count']}"
                )
                for reg in stage["declared_registers"]:
                    print(
                        f"       {reg['hlsl_bind']:>6s} {reg['type']:<8s} "
                        f"{reg['format']:>6s} {reg['dimension']:<8s} {reg['name']}"
                    )
            print(f"    declared totals: {draw['declared_totals']}")
            for descriptor in draw["root_descriptors"]:
                res = descriptor.get("resource", {})
                print(
                    f"    root[{descriptor['root_index']}] {descriptor['binding_kind']:10s} "
                    f"rid={descriptor['resource_id']} trust={descriptor['trust']}  "
                    f"{res.get('description', '')[:34]}"
                )
            for table in draw["descriptor_tables"]:
                print(
                    f"    root[{table['root_index']}] table views={table['view_count']:<3d} "
                    f"trust={table['trust']:<12s} rids={table['distinct_resource_ids'][:5]}"
                )
                print(f"       reason: {table['reason'][:88]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
