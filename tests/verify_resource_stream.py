"""Verify the full resources.bin index: every blob addressable and decodable."""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set.context import ToolContext, clear_capture_cache  # noqa: E402
from pix_tool_set.errors import PixToolError  # noqa: E402


def main() -> int:
    clear_capture_cache()
    capture = ToolContext.from_cwd().capture({"session": "tiled"})
    reads = capture._resource_reads
    stream = capture._resource_stream

    declared = sum(read.compressed_size for read in reads)
    actual = stream.file_size

    print("=" * 74)
    print(f"Read calls numbered : {len(reads):,}")
    print(f"declared bytes      : {declared:,}")
    print(f"resources.bin size  : {actual:,}")
    print(f"delta               : {actual - declared:,}")
    print(f"by kind             : {dict(Counter(r.owner_kind for r in reads))}")
    print("=" * 74)

    # Decompress every blob. This is the real proof the ordering is right.
    ok = fail = 0
    failures: list[tuple[int, str, int]] = []
    for read in reads:
        try:
            stream.read_index(read.index)
            ok += 1
        except PixToolError:
            fail += 1
            if len(failures) < 8:
                failures.append((read.index, read.owner_kind, read.compressed_size))

    print(f"\ndecompressed ok : {ok:,}/{len(reads):,}")
    print(f"failed          : {fail}")
    for index, kind, size in failures:
        print(f"   blob {index} kind={kind} size={size:,}")

    plan = capture._modification_plan
    if plan is not None:
        readable = 0
        for blob_index in sorted(plan.blob_sizes):
            try:
                capture._load_blob(blob_index)
                readable += 1
            except PixToolError:
                pass
        print(f"\nmodification blobs decodable: {readable}/{len(plan.blob_sizes)}")
        print(f"CPU-patched resources       : {plan.resource_count}")
        print(f"page writes                 : {plan.write_count}")

    print(f"\nresources with captured data : {len(capture._resource_blob_index):,}")
    shader = capture.shaders[0]
    print(f"shader blob still valid      : {len(shader.bytecode):,} B "
          f"magic={shader.bytecode[:4]!r}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
