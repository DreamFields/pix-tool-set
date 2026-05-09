"""Find a pass whose cbuffer page is NOT rewritten, to prove value decoding works."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set import call_tool  # noqa: E402
from pix_tool_set.context import ToolContext, clear_capture_cache  # noqa: E402


def main() -> int:
    clear_capture_cache()
    capture = ToolContext.from_cwd().capture({"session": "tiled"})

    clean: list[tuple[int, int, int]] = []
    for draw in capture.draw_calls:
        for binding in draw.bindings:
            if binding.kind.value != "root_cbv" or binding.resource_id is None:
                continue
            sources = capture.resource_data_sources(binding.resource_id)
            if sources["initial_blob_index"] is None:
                continue
            page = binding.va_offset // 4096
            if page not in capture.resource_written_pages(binding.resource_id):
                clean.append((draw.index, binding.resource_id, page))
            break

    print(f"draws whose root CBV page was never CPU-rewritten: {len(clean)}")
    if not clean:
        print("none - every root CBV in this frame is patched at runtime")
        return 0

    for draw_index, rid, page in clean[:3]:
        draw = capture.draw_call(draw_index)
        print("\n" + "=" * 78)
        print(f"draw #{draw_index}  {draw.pass_name}   rid={rid} page={page}")
        print("=" * 78)
        payload = call_tool(
            "constant-buffer",
            {"session": "tiled", "draw_index": draw_index, "max_bytes": 128},
        )
        data = payload["data"]
        print(f"status={payload['status']} values_available={data['values_available']}")
        for supplier in data["root_cbv_suppliers"]:
            if not supplier.get("decoded"):
                continue
            for block in supplier["decoded"]:
                print(f"\n  cbuffer {block['cbuffer']}")
                for field in block["fields"][:14]:
                    value = field["value"]
                    if isinstance(value, list):
                        shown = "[" + ", ".join(
                            f"{v:.4g}" if isinstance(v, float) else str(v) for v in value
                        ) + "]"
                    else:
                        shown = f"{value:.6g}" if isinstance(value, float) else str(value)
                    print(
                        f"    +{str(field['offset']):>5s} {field['type']:<9s} "
                        f"{shown[:30]:<30s} {field['name']}"
                    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
