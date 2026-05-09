"""Measure how many of the capture's shaders can be resolved to real HLSL."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set.context import ToolContext  # noqa: E402
from pix_tool_set.engine import shaderpdb  # noqa: E402

SYMBOLS = [Path(r"F:\JL_TMR\UnrealEngine\Games\JyGame\Saved\ShaderSymbols\PCD3D_SM6")]
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 60


def main() -> int:
    capture = ToolContext.from_cwd().capture({"session": "tiled"})
    shaders = capture.shaders
    sample = shaders[:LIMIT]

    print("=" * 74)
    print(f"shaders in capture : {len(shaders)}")
    print(f"sampling           : {len(sample)}")
    print(f"symbol dir         : {SYMBOLS[0]}")
    print("=" * 74)

    found = recovered = entry_sliced = 0
    misses: list[str] = []
    for shader in sample:
        pdb = shaderpdb.find_pdb(SYMBOLS, shader.shader_hash or "", shader.debug_name or "")
        if pdb is None:
            misses.append(f"{shader.stage.value} {shader.shader_hash[:12]}")
            continue
        found += 1
        report = shaderpdb.extract_sources(pdb)
        if not report.get("ok"):
            continue
        recovered += 1
        body = report.get("shader_body") or report.get("full_text") or ""
        if shaderpdb.slice_entry_function(body, shader.entry_point):
            entry_sliced += 1

    print(f"\nPDB found on disk        : {found}/{len(sample)}")
    print(f"HLSL recovered           : {recovered}/{len(sample)}")
    print(f"entry function isolated  : {entry_sliced}/{len(sample)}")
    if misses:
        print(f"\nno PDB for {len(misses)} shader(s), first few:")
        for item in misses[:8]:
            print(f"  {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
