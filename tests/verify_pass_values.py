"""End to end: read every bound resource's value for one pass."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set import call_tool  # noqa: E402
from pix_tool_set.context import clear_capture_cache  # noqa: E402

QUEUE_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 18385


def fmt(value) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(
            f"{v:.5g}" if isinstance(v, float) else str(v) for v in value
        ) + "]"
    return f"{value:.6g}" if isinstance(value, float) else str(value)


def main() -> int:
    clear_capture_cache()
    payload = call_tool(
        "pass-values",
        {"session": "tiled", "queue_id": QUEUE_ID, "element_type": "float4",
         "max_bytes": 128, "max_views": 8},
    )
    print("=" * 78)
    print(f"status : {payload['status']}")
    if payload["status"] == "error":
        print(payload["error"])
        return 1
    data = payload["data"]
    print(f"pass   : {data['pass_name']}  draw #{data['draw_index']}")
    print(f"stages : {data['stages']}   pso={data['pso_id']}")
    print(f"summary: {data['summary']}")
    print("=" * 78)

    for record in data["root_bindings"]:
        values = record["values"]
        print(f"\nroot[{record['root_index']}] {record['binding_kind']:10s} "
              f"rid={record['resource_id']}  {record.get('description', '')[:44]}")
        print(f"   available={values.get('values_available')} "
              f"page={values.get('page')} rewritten={values.get('page_rewritten_during_frame')} "
              f"patches={values.get('page_patches_applied', 0)}")
        if values.get("detail"):
            print(f"   detail: {values['detail'][:100]}")
        for block in record.get("cbuffer_fields") or []:
            print(f"\n   cbuffer {block['cbuffer']} ({block['stage']}):")
            for field in block["fields"]:
                if field["value"] is None:
                    continue
                print(f"     +{str(field['offset']):>5s} {field['type']:<10s} "
                      f"{fmt(field['value'])[:34]:<34s} {field['name']}")
        if values.get("elements"):
            print(f"   elements ({values['element_type']}): {values['elements'][:3]}")

    print("\n" + "-" * 78)
    print("descriptor table resources")
    print("-" * 78)
    for record in data["descriptor_table_resources"]:
        values = record["values"]
        flag = "ok" if values.get("values_available") else "n/a"
        print(f"  rid={record['resource_id']:<6d} {flag:4s} "
              f"{record.get('description', '')[:40]:<42s}")
        if values.get("elements"):
            print(f"      {values['element_type']}: {values['elements'][:2]}")
        elif values.get("detail"):
            print(f"      {values['detail'][:70]}")

    for entry in payload.get("diagnostics", [])[:2]:
        print(f"\n[{entry['level']}] {entry['message'][:120]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
