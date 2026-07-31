"""Verify root CBV to cbuffer register matching across the whole frame.

The defect this guards against: decoding every cbuffer layout against every root
CBV. On a graphics draw with cb0/cb1/cb2 bound that yields three answers per
buffer, two of them silently wrong. Correct behaviour is one layout per root
parameter, joined on the shader register the root signature declares.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set import call_tool  # noqa: E402
from pix_tool_set.context import ToolContext, clear_capture_cache  # noqa: E402
from pix_tool_set.engine.model import RootParameterKind  # noqa: E402


def main() -> int:
    clear_capture_cache()
    capture = ToolContext.from_cwd().capture({"session": "Tiled"})

    multi = 0
    checked = 0
    unmatched = 0
    over_decoded = 0
    samples: list[str] = []

    for draw in capture.draw_calls:
        cbvs = [
            b
            for b in draw.bindings
            if b.kind is RootParameterKind.CBV and b.resource_id is not None
        ]
        if len(cbvs) < 2:
            continue
        multi += 1
        if multi > 60:
            break
        signature = capture.root_signatures.get(draw.root_signature_id)
        if signature is None:
            unmatched += 1
            continue
        registers = {
            p.index: (p.shader_register, str(getattr(p, "visibility", "") or ""))
            for p in signature.parameters
            if p.kind is RootParameterKind.CBV
        }
        # The same register declared twice is legal when the two parameters target
        # different stages, so it is only worth noting when visibility matches too.
        seen: dict[tuple[int, str], int] = {}
        for index, key in registers.items():
            if key in seen and len(samples) < 6:
                samples.append(
                    f"draw {draw.index}: root[{seen[key]}] and root[{index}] share "
                    f"register b{key[0]} with the same visibility {key[1]!r}"
                )
            seen[key] = index
        checked += 1

        payload = call_tool(
            "pass-values",
            {
                "session": "Tiled",
                "draw_index": draw.index,
                "max_bytes": 64,
                "include_views": False,
            },
        )
        if payload["status"] == "error":
            continue
        for record in payload["data"]["root_bindings"]:
            blocks = record.get("cbuffer_fields") or []
            if len(blocks) > 1:
                over_decoded += 1
                if len(samples) < 6:
                    samples.append(
                        f"draw {draw.index} root[{record['root_index']}] "
                        f"decoded {len(blocks)} layouts"
                    )

    print("=" * 72)
    print(f"draws with 2+ root CBVs sampled : {checked}")
    print(f"no root signature recovered     : {unmatched}")
    print(f"bindings decoding >1 layout     : {over_decoded}  (must be 0)")
    print("=" * 72)
    for line in samples:
        print(f"  {line}")

    # The specific case from the question.
    payload = call_tool(
        "pass-values",
        {
            "session": "Tiled",
            "queue_id": 17765,
            "stage": "PS",
            "cbuffer": "Scene",
            "max_bytes": 512,
            "include_views": False,
        },
    )
    blocks = [
        block
        for record in payload["data"]["root_bindings"]
        for block in (record.get("cbuffer_fields") or [])
    ]
    print(f"\nqueue 17765 PS Scene: {len(blocks)} block(s) decoded")
    for record in payload["data"]["root_bindings"]:
        if record.get("cbuffer_fields"):
            print(f"  root[{record['root_index']}] cb{record.get('shader_register')} "
                  f"matched={record.get('register_matched')} "
                  f"rid={record['resource_id']}")
    ok = over_decoded == 0 and len(blocks) == 1
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
