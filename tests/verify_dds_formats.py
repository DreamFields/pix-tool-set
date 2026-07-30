"""Unit-check the small-float reconstruction used by R11G11B10.

The earlier ad-hoc probe used a wrong bit pattern and looked like a failure, so
pin the expectations down properly. An 11-bit float has 6 mantissa bits and 5
exponent bits with bias 15, so 1.0 is exponent field 15 with zero mantissa.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set.engine import dds  # noqa: E402

CASES_11BIT = [
    (0, 0.0),
    (15 << 6, 1.0),
    (16 << 6, 2.0),
    (14 << 6, 0.5),
    ((15 << 6) | 32, 1.5),
    ((15 << 6) | 16, 1.25),
]

CASES_10BIT = [
    (0, 0.0),
    (15 << 5, 1.0),
    (16 << 5, 2.0),
    (14 << 5, 0.5),
    ((15 << 5) | 16, 1.5),
]


def main() -> int:
    failures = 0
    print("11-bit float (6 mantissa, 5 exponent, bias 15)")
    for bits, expected in CASES_11BIT:
        got = dds._float_from_bits(bits, 6, 5)
        ok = abs(got - expected) < 1e-6
        failures += int(not ok)
        print(f"   bits={bits:>5d}  expect={expected:<7} got={got:<12} "
              f"{'ok' if ok else 'MISMATCH'}")

    print("\n10-bit float (5 mantissa, 5 exponent, bias 15)")
    for bits, expected in CASES_10BIT:
        got = dds._float_from_bits(bits, 5, 5)
        ok = abs(got - expected) < 1e-6
        failures += int(not ok)
        print(f"   bits={bits:>5d}  expect={expected:<7} got={got:<12} "
              f"{'ok' if ok else 'MISMATCH'}")

    # Denormals: exponent 0 with a nonzero mantissa.
    smallest = dds._float_from_bits(1, 6, 5)
    print(f"\nsmallest 11-bit denormal: {smallest:.10g}")
    print(f"   expected 2^-15 / 64 = {(2.0 ** -14) / 64:.10g}")

    # A packed R11G11B10 pixel of (1.0, 2.0, 1.0).
    packed = (15 << 6) | ((16 << 6) << 11) | ((15 << 5) << 22)
    channels = dds._unpack_r11g11b10(packed, True)
    print(f"\npacked (1.0, 2.0, 1.0) -> {channels}")
    ok = all(
        abs(a - b) < 1e-6 for a, b in zip(channels, (1.0, 2.0, 1.0))
    )
    failures += int(not ok)
    print(f"   {'ok' if ok else 'MISMATCH'}")

    # R10G10B10A2 round trip: max in every channel is 1.0.
    full = 0x3FF | (0x3FF << 10) | (0x3FF << 20) | (0x3 << 30)
    channels = dds._unpack_r10g10b10a2(full, True)
    print(f"\npacked all-max R10G10B10A2 -> {channels}")
    ok = all(abs(v - 1.0) < 1e-9 for v in channels)
    failures += int(not ok)
    print(f"   {'ok' if ok else 'MISMATCH'}")

    print(f"\nRESULT: {'PASS' if failures == 0 else f'FAIL ({failures})'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
