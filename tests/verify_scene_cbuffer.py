"""Read one named cbuffer's values for a pass, e.g. the PS 'Scene' buffer."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set import call_tool  # noqa: E402
from pix_tool_set.context import clear_capture_cache  # noqa: E402

QUEUE_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 17765
STAGE = sys.argv[2] if len(sys.argv) > 2 else "PS"
CBUFFER = sys.argv[3] if len(sys.argv) > 3 else "Scene"
LIMIT = int(sys.argv[4]) if len(sys.argv) > 4 else 40


def fmt(value) -> str:
    if isinstance(value, list):
        if value and isinstance(value[0], list):
            return "[" + "; ".join(
                ", ".join(f"{v:.6g}" for v in row) for row in value[:2]
            ) + (" ...]" if len(value) > 2 else "]")
        return "{" + ", ".join(
            f"{v:.6g}" if isinstance(v, float) else str(v) for v in value
        ) + "}"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def main() -> int:
    clear_capture_cache()
    payload = call_tool(
        "pass-values",
        {
            "session": "tiled",
            "queue_id": QUEUE_ID,
            "stage": STAGE,
            "cbuffer": CBUFFER,
            "max_bytes": 512,
            "include_views": False,
        },
    )
    print("=" * 82)
    print(f"status : {payload['status']}")
    if payload["status"] == "error":
        print(payload["error"])
        return 1
    data = payload["data"]
    print(f"pass   : {data['pass_name']}   draw #{data['draw_index']}")
    print(f"stages : {data['stages']}   pso={data['pso_id']}")
    print("=" * 82)

    shown = 0
    for record in data["root_bindings"]:
        blocks = record.get("cbuffer_fields") or []
        if not blocks:
            continue
        values = record["values"]
        print(f"\nroot[{record['root_index']}]  cb{record.get('shader_register')}  "
              f"rid={record['resource_id']}  offset={values.get('byte_offset')}")
        print(f"   register_matched = {record.get('register_matched')}")
        print(f"   values_available = {values.get('values_available')}   "
              f"page={values.get('page')} rewritten={values.get('page_rewritten_during_frame')} "
              f"patches={values.get('page_patches_applied', 0)}")
        for block in blocks:
            fields = block["fields"]
            print(f"\n   cbuffer {block['cbuffer']}  ({block['stage']}, "
                  f"cb{block.get('shader_register')}, size={block.get('declared_size')}, "
                  f"{len(fields)} fields)")
            print(f"   {'offset':>7s}  {'type':<18s} {'value':<32s} name")
            for field in fields[:LIMIT]:
                shown_value = fmt(field["value"])
                if field.get("value_as_uint") is not None:
                    shown_value = f"{shown_value}  (=uint {field['value_as_uint']})"
                print(f"   {str(field['offset']):>7s}  {str(field['type'])[:18]:<18s} "
                      f"{shown_value[:44]:<44s} {field['name']}")
            if len(fields) > LIMIT:
                print(f"   ... {len(fields) - LIMIT} more field(s)")
            shown += 1

    if not shown:
        print(f"\nno cbuffer named {CBUFFER!r} decoded on stage {STAGE}")
    for entry in payload.get("diagnostics", [])[:2]:
        print(f"\n[{entry['level']}] {entry['message'][:130]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
