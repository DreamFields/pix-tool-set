"""Read the actual VALUES a pass's shader was configured with."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set import call_tool  # noqa: E402
from pix_tool_set.context import clear_capture_cache  # noqa: E402

QUEUE_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 18385


def main() -> int:
    clear_capture_cache()
    found = call_tool("find-pass", {"session": "tiled", "queue_id": QUEUE_ID})
    match = found["data"]["matches"][0]
    draw_index = match["draw_index"]
    print("=" * 78)
    print(f"Queue ID {QUEUE_ID} -> {match['name']}  (draw #{draw_index})")
    print("=" * 78)

    payload = call_tool(
        "constant-buffer", {"session": "tiled", "draw_index": draw_index, "max_bytes": 256}
    )
    print(f"status           : {payload['status']}")
    data = payload["data"]
    print(f"values_available : {data['values_available']}")

    for supplier in data["root_cbv_suppliers"]:
        print(f"\nroot[{supplier['root_index']}] rid={supplier['resource_id']} "
              f"offset={supplier['byte_offset']}")
        print(f"  values_available : {supplier.get('values_available')}")
        if supplier.get("values_detail"):
            print(f"  detail : {supplier['values_detail']}")
        print(f"  bytes_read : {supplier.get('bytes_read')}")
        for block in supplier.get("decoded") or []:
            print(f"\n  cbuffer {block['cbuffer']}:")
            print(f"  {'offset':>7s}  {'type':<9s} {'value':<34s} name")
            for field in block["fields"]:
                value = field["value"]
                if isinstance(value, list):
                    shown = "[" + ", ".join(
                        f"{v:.4g}" if isinstance(v, float) else str(v) for v in value
                    ) + "]"
                else:
                    shown = f"{value:.6g}" if isinstance(value, float) else str(value)
                print(
                    f"  {str(field['offset']):>7s}  {field['type']:<9s} "
                    f"{shown[:34]:<34s} {field['name']}"
                )
        if supplier.get("hexdump"):
            print("\n  first bytes:")
            for line in supplier["hexdump"][:4]:
                print("    " + line)

    for entry in payload.get("diagnostics", [])[:3]:
        print(f"\n[{entry['level']}] {entry['message'][:110]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
