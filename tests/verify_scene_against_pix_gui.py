"""Field-by-field comparison of the PS 'Scene' cbuffer against the PIX GUI.

Ground truth transcribed from the PIX constant-buffer view for Queue ID 17765
(Emit Scene Depth/Resolve/Velocity), covering all 77 rows from offset 0 to the
trailing pad at 316.

Keyed on offset rather than name, because the PIX name column is truncated and
offsets are unambiguous. Integers must be equal; floats must agree to the
precision PIX prints; vector rows must match element by element.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set import call_tool  # noqa: E402
from pix_tool_set.context import clear_capture_cache  # noqa: E402

QUEUE_ID = 17765

# (offset, expected value) exactly as PIX shows it. None = PIX prints no value.
PIX_TRUTH: list[tuple[int, object]] = [
    (0, 8),
    (4, 255),
    (8, 768),
    (12, 59369),
    (16, 29),
    (20, 30),
    (24, 0),
    (28, 6.22177e-43),
    (32, 14150),
    (36, 437),
    (40, 7779),
    (44, 437),
    (48, 4442),
    (52, 437),
    (56, 7776),
    (60, 437),
    (64, 14178),
    (68, 6.12367e-43),
    (72, 1.82731e28),
    (76, 4.59093e-41),
    (80, 27),
    (84, 437),
    (88, 2),
    (92, 437),
    (96, [4294967295, 0, 0, 0]),
    (112, 16),
    (116, 439),
    (120, 6855),
    (124, 439),
    (128, 218),
    (132, 6.1517e-43),
    (136, -6.94327e37),
    (140, 9.24857e-44),
    (144, 0),
    (148, 66),
    (152, 899),
    (156, 6.1517e-43),
    (160, 0),
    (164, 0),
    (168, 0),
    (172, 0),
    (176, 898),
    (180, 437),
    (184, 898),
    (188, 6.12367e-43),
    (192, 5210),
    (196, 439),
    (200, 5211),
    (204, 439),
    (208, 5212),
    (212, 439),
    (216, 5213),
    (220, 6.1517e-43),
    (224, [1, 1]),
    (232, 917),
    (236, 437),
    (240, 917),
    (244, 437),
    (248, 2),
    (252, 6.12367e-43),
    (256, 3324),
    (260, 439),
    (264, 1),
    (268, 1),
    (272, 1),
    (276, 6.1517e-43),
    (280, 5.82588e-10),
    (284, 6.17973e-43),
    (288, 5875),
    (292, 439),
    (296, 6498),
    (300, 6.1517e-43),
    (304, 3358),
    (308, 437),
    (312, 1),
    (316, None),  # trailing pad
]

REL_TOL = 1e-4


def close(actual, expected) -> bool:
    if expected is None:
        return actual is None
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            return False
        return all(close(a, e) for a, e in zip(actual, expected))
    if isinstance(expected, float):
        if not isinstance(actual, (int, float)):
            return False
        if expected == 0.0:
            return abs(actual) < 1e-45
        return abs(actual - expected) <= abs(expected) * REL_TOL
    if isinstance(actual, float) and float(expected).is_integer():
        return abs(actual - expected) < 1e-6
    return actual == expected


def show(value) -> str:
    if value is None:
        return "(no value)"
    if isinstance(value, list):
        return "{" + ", ".join(
            f"{v:g}" if isinstance(v, float) else str(v) for v in value
        ) + "}"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def main() -> int:
    clear_capture_cache()
    payload = call_tool(
        "pass-values",
        {
            "session": "tiled",
            "queue_id": QUEUE_ID,
            "stage": "PS",
            "cbuffer": "Scene",
            "max_bytes": 512,
            "include_views": False,
        },
    )
    if payload["status"] == "error":
        print(payload["error"])
        return 1

    fields: dict[int, dict] = {}
    name_of: dict[int, str] = {}
    for record in payload["data"]["root_bindings"]:
        for block in record.get("cbuffer_fields") or []:
            if block.get("cbuffer") != "Scene":
                continue
            for field in block["fields"]:
                offset = field.get("offset")
                if offset is not None:
                    fields[int(offset)] = field
                    name_of[int(offset)] = field.get("name") or ""

    print("=" * 104)
    print(f"{'offset':>7s}  {'PIX':<24s} {'ours':<24s} {'verdict':<9s} field")
    print("=" * 104)

    passed = failed = missing = 0
    for offset, expected in PIX_TRUTH:
        got = fields.get(offset)
        if got is None:
            print(f"  {offset:>5d}  {show(expected):<24s} {'-':<24s} {'MISSING':<9s}")
            missing += 1
            continue
        actual = got.get("value")
        ok = close(actual, expected)
        verdict = "MATCH" if ok else "DIFFER"
        print(
            f"  {offset:>5d}  {show(expected)[:24]:<24s} {show(actual)[:24]:<24s} "
            f"{verdict:<9s} {name_of[offset][:40]}"
        )
        passed += int(ok)
        failed += int(not ok)

    print("=" * 104)
    print(f"match {passed} | differ {failed} | missing {missing}")

    extra = sorted(set(fields) - {offset for offset, _ in PIX_TRUTH})
    if extra:
        print(f"offsets we report that PIX does not list: {extra}")
    return 0 if failed == 0 and missing == 0 and not extra else 1


if __name__ == "__main__":
    raise SystemExit(main())
