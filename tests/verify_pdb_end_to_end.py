"""End to end: PIX GUI Queue ID -> real HLSL via the engine's shader PDBs."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set import call_tool  # noqa: E402
from pix_tool_set.context import clear_capture_cache  # noqa: E402

SYMBOLS = r"F:\JL_TMR\UnrealEngine\Games\JyGame\Saved\ShaderSymbols\PCD3D_SM6"
QUEUE_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 18461


def main() -> int:
    clear_capture_cache()
    payload = call_tool(
        "pass-shader-source",
        {
            "session": "tiled",
            "queue_id": QUEUE_ID,
            "pdb_dirs": [SYMBOLS],
            "max_lines": 0,
        },
    )
    print("=" * 78)
    print(f"status : {payload['status']}")
    if payload["status"] == "error":
        print(payload["error"])
        return 1
    data = payload["data"]
    print(f"pass   : {data['pass_name']}  (pass_index={data['pass_index']})")
    print(f"queue  : {data['queue_id']}   global={data['global_id']}   pso={data['pso_id']}")
    print("=" * 78)

    for row in data["stages"]:
        rec = row["pdb_recovery"]
        print(f"\nstage        : {row['stage']}")
        print(f"source_tier  : {row['source_tier']}")
        print(f"entry_point  : {row['entry_point']}")
        print(f"num_threads  : {row['num_threads']}")
        print(f"pdb          : {Path(rec['pdb_path']).name if rec.get('pdb_path') else None}")
        print(f"method       : {rec.get('method')}")
        print(f"body_source  : {rec.get('shader_body_source')}")
        print(f"sections     : {rec.get('section_names')}")
        print(f"lines        : {row['line_count']:,}")
        if rec.get("compile_args"):
            print(f"compile_args : {' '.join(rec['compile_args'][:12])}")

        text = row["text"]
        lines = text.splitlines()
        print("\n" + "-" * 78)
        print(f"first 40 lines of the authored body ({len(lines):,} lines total)")
        print("-" * 78)
        for line in lines[:40]:
            print("  " + line)

        # show that the entry point really is in there
        needle = row["entry_point"]
        if needle:
            hit = next(
                (n for n, l in enumerate(lines, 1) if needle in l), None
            )
            print(f"\n  entry point {needle!r} found at line: {hit}")
            if hit:
                for line in lines[max(hit - 3, 0) : hit + 12]:
                    print("    " + line)
    for entry in payload.get("diagnostics", []):
        print(f"\n[{entry['level']}] {entry['message'][:110]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
