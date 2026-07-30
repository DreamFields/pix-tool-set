"""Measure value-reading coverage across the whole frame."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set.context import ToolContext, clear_capture_cache  # noqa: E402


def main() -> int:
    clear_capture_cache()
    capture = ToolContext.from_cwd().capture({"session": "tiled"})

    cbv_ok = cbv_stale = cbv_none = 0
    draws_with_cbv = 0
    for draw in capture.draw_calls:
        binding = next(
            (
                b
                for b in draw.bindings
                if b.kind.value == "root_cbv" and b.resource_id is not None
            ),
            None,
        )
        if binding is None:
            continue
        draws_with_cbv += 1
        page = (binding.va_offset or 0) // 4096
        status = capture.resource_page_status(binding.resource_id, page)
        sources = capture.resource_data_sources(binding.resource_id)
        if sources["initial_blob_index"] is None:
            cbv_none += 1
        elif status["rewritten"] and not status["patched"]:
            cbv_stale += 1
        else:
            cbv_ok += 1

    print("=" * 70)
    print("root CBV values")
    print("=" * 70)
    print(f"  draws with a root CBV : {draws_with_cbv:,}")
    print(f"  trustworthy values    : {cbv_ok:,}")
    print(f"  stale (patch missing) : {cbv_stale:,}")
    print(f"  no captured bytes     : {cbv_none:,}")

    total = len(capture.resources)
    with_data = len(capture._resource_blob_index)
    print("\n" + "=" * 70)
    print("resource contents")
    print("=" * 70)
    print(f"  resources in capture  : {total:,}")
    print(f"  with captured bytes   : {with_data:,}  ({100.0 * with_data / total:.1f}%)")

    plan = capture._modification_plan
    if plan is not None:
        print(f"  CPU-patched resources : {plan.resource_count}")
        print(f"  page writes replayed  : {plan.write_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
