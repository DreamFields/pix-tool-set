"""Verify HLSL recovery from the UE5 shader PDB directory."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set.engine.shaderpdb import extract_sources  # noqa: E402

SYMBOLS = Path(r"F:\JL_TMR\UnrealEngine\Games\JyGame\Saved\ShaderSymbols\PCD3D_SM6")
HASH = sys.argv[1] if len(sys.argv) > 1 else "3e92071c09a522dfa4e259e557334efc"


def main() -> int:
    report = extract_sources(SYMBOLS / f"{HASH}.pdb")
    print("=" * 78)
    print(f"pdb    : {Path(report['pdb']).name}")
    print(f"ok     : {report['ok']}")
    print(f"method : {report['method']}")
    print(f"detail : {report['detail']}")
    print(f"files  : {len(report['files'])}")
    print(f"entry  : {report['entry_file']}")
    if report["compile_args"]:
        print(f"args   : {' '.join(report['compile_args'][:14])}")
    print("=" * 78)

    for name, text in sorted(
        report["files"].items(), key=lambda kv: -len(kv[1])
    )[:10]:
        print(f"  {len(text):>9,d}  {name}")

    entry = report["entry_file"]
    text = report["files"].get(entry) or report["full_text"]
    if text:
        print("\n" + "-" * 78)
        print(f"first 46 lines of {entry}")
        print("-" * 78)
        for line in text.splitlines()[:46]:
            print("  " + line)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
