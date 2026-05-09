"""Verify pixel-value-history / pixel-history-replay against the PIX GUI baseline.

The baseline is a screenshot of the PIX Debug panel's Pixel History for GBufferA at
(810, 284) in Tiled.wpix, which shows exactly four rows:

    Global ID  Event          Previous Value                    New Value
    0          Recreation #1  R:0x0 G:0x0 B:0x0 A:0x0           R:0.4995(0x1FF) G:1.0000(0x3FF) B:0.4995(0x1FF) A:0.3333(0x1)
    3828       Clear          R:0.4995 G:1.0000 B:0.4995 A:0.3333  R:0 G:0 B:0 A:0
    3851       Draw           R:0 G:0 B:0 A:0                   Failed depth/stencil test
    3854       Draw           R:0 G:0 B:0 A:0                   R:0.4995(0x1FF) G:1.0000(0x3FF) B:0.4995(0x1FF) A:0.3333(0x1)

Assertions are on *semantics*, never on wording: a row is checked by its raw integer
channel fields and by its verdict constant, so rephrasing a message cannot break the
test and cannot make it pass either.

Layered so the parts that need no GPU still run and still fail loudly:

  * ``--no-replay`` and pure-decode checks always run. They cover the bit unpacking,
    the candidate set, the depth evidence, the consistency checker and the honesty of
    every "value unavailable" path.
  * The measured rows require a built replay. Without one they are reported as
    ``SKIPPED (no measurement available)`` and the script exits non-zero, because a
    test that silently passes without measuring the thing it is named after is worse
    than no test.

Run:  python tests/verify_pixel_value_history.py [--replay] [--skip-build]
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from pix_tool_set.cli import main as cli_main  # noqa: E402
from pix_tool_set.engine import pixelprobe  # noqa: E402

SESSION = "Tiled"
RESOURCE_ID = 756          # GBufferA
PIXEL_X, PIXEL_Y = 810, 284

# The GUI truth, as raw integer channel fields. Raw fields rather than floats because
# they are exact and because they are what proves the bit layout was read correctly:
# 0.4995 could come from several wrong interpretations, 0x1FF from only one.
GBUFFERA_VALUE = (511, 1023, 511, 1)      # 0x1FF, 0x3FF, 0x1FF, 0x1
ZERO_VALUE = (0, 0, 0, 0)
R10G10B10A2_WIDTHS = (10, 10, 10, 2)

# Global IDs the GUI lists, in order.
GID_RECREATION = 0
GID_CLEAR = 3828
GID_DRAW_FAILED = 3851
GID_DRAW_WROTE = 3854

_failures: list[str] = []
_skips: list[str] = []


def check(condition: bool, label: str, detail: str = "") -> bool:
    if condition:
        print(f"  [ok]   {label}")
        return True
    message = f"{label}" + (f" -- {detail}" if detail else "")
    print(f"  [FAIL] {message}")
    _failures.append(message)
    return False


def skip(label: str, detail: str) -> None:
    print(f"  [SKIP] {label} -- {detail}")
    _skips.append(f"{label}: {detail}")


def run_tool(argv: list[str]) -> dict:
    """Invoke the CLI in-process and return the parsed envelope."""
    buffer = io.StringIO()
    saved = sys.argv
    sys.argv = ["pix-tool-set", *argv]
    try:
        with contextlib.redirect_stdout(buffer):
            try:
                cli_main()
            except SystemExit:
                pass
    finally:
        sys.argv = saved
    text = buffer.getvalue()
    start = text.find("{")
    if start < 0:
        raise AssertionError(f"no JSON in tool output: {text[:400]}")
    return json.loads(text[start:])


def raw_of(value: dict | None) -> tuple | None:
    return tuple(value["raw"]) if value else None


# ======================================================================
# 1. bit unpacking -- the identity proof
# ======================================================================
def test_bit_unpacking() -> None:
    print("\n[1] R10G10B10A2_UNORM bit field unpacking")
    packed = 511 | (1023 << 10) | (511 << 20) | (1 << 30)
    value = pixelprobe.decode_pixel(packed.to_bytes(4, "little"), 24)
    if not check(value is not None, "the reference bit pattern decodes"):
        return
    check(value.raw == GBUFFERA_VALUE, "raw fields are 0x1FF/0x3FF/0x1FF/0x1",
          f"got {value.raw}")
    check(value.bit_widths == R10G10B10A2_WIDTHS,
          "channel widths are 10/10/10/2", f"got {value.bit_widths}")
    check(value.channels == ("R", "G", "B", "A"),
          "channels are reported in RGBA order", f"got {value.channels}")
    # The GUI's own numbers, to four decimals.
    check(abs(value.normalised[0] - 511 / 1023) < 1e-6, "R normalises to 0.4995")
    check(abs(value.normalised[1] - 1.0) < 1e-9, "G normalises to 1.0000")
    check(abs(value.normalised[3] - 1 / 3) < 1e-6,
          "A normalises to 0.3333 (2-bit channel, not 8-bit)")
    zero = pixelprobe.decode_pixel((0).to_bytes(4, "little"), 24)
    check(raw_of(zero.to_dict()) == ZERO_VALUE, "an all-zero texel decodes to zeros")
    check(not value.equals(zero), "the two reference values compare as different")
    check(value.equals(pixelprobe.decode_pixel(packed.to_bytes(4, "little"), 24)),
          "equality is exact on a round trip")
    check(pixelprobe.decode_pixel(packed.to_bytes(4, "little"), 999_999) is None,
          "an unknown format yields None rather than a guessed value")


# ======================================================================
# 2. verdicts are honest
# ======================================================================
def test_verdict_honesty() -> None:
    print("\n[2] verdict honesty: measurement vs inference")
    zero = pixelprobe.decode_pixel((0).to_bytes(4, "little"), 24)
    packed = 511 | (1023 << 10) | (511 << 20) | (1 << 30)
    written = pixelprobe.decode_pixel(packed.to_bytes(4, "little"), 24)

    def sample(phase: str, value, recorded=True, readable=True, reason=""):
        return pixelprobe.Sample(
            slot=0, global_id=GID_DRAW_FAILED, phase=phase, resource_id=RESOURCE_ID,
            x=PIXEL_X, y=PIXEL_Y, recorded=recorded, readable=readable,
            dxgi_format=24, raw_bytes=b"", value=value, reason=reason,
        )

    unchanged = pixelprobe.classify_event(sample("before", zero), sample("after", zero))
    check(unchanged["verdict"] == pixelprobe.VERDICT_UNCHANGED,
          "unchanged with no depth evidence stays 'value_unchanged'",
          f"got {unchanged['verdict']}")
    check(unchanged["verdict_is_inferred"] is False,
          "that verdict is labelled as measured, not inferred")

    inferred = pixelprobe.classify_event(
        sample("before", zero), sample("after", zero),
        depth_evidence={"depth_test_enabled": True, "depth_stencil_bound": True},
    )
    check(inferred["verdict"] == pixelprobe.VERDICT_DEPTH_FAILED,
          "unchanged + depth test + bound DSV yields the depth/stencil verdict",
          f"got {inferred['verdict']}")
    check(inferred["verdict_is_inferred"] is True,
          "the depth verdict is explicitly flagged as an inference")

    no_dsv = pixelprobe.classify_event(
        sample("before", zero), sample("after", zero),
        depth_evidence={"depth_test_enabled": True, "depth_stencil_bound": False},
    )
    check(no_dsv["verdict"] == pixelprobe.VERDICT_UNCHANGED,
          "without a bound DSV the depth conclusion is withheld",
          f"got {no_dsv['verdict']}")

    wrote = pixelprobe.classify_event(sample("before", zero), sample("after", written))
    check(wrote["verdict"] == pixelprobe.VERDICT_WROTE,
          "a changed texel is reported as written")

    missing = pixelprobe.classify_event(
        sample("before", zero),
        sample("after", None, recorded=False, readable=False, reason="no copy recorded"),
    )
    check(missing["verdict"] == pixelprobe.VERDICT_UNKNOWN,
          "an unmeasured event is 'not_sampled', never conflated with 'unchanged'",
          f"got {missing['verdict']}")
    check(bool(missing.get("reason")),
          "and it carries a reason instead of a bare null")


# ======================================================================
# 3. adjacent-pair consistency
# ======================================================================
def test_consistency_checker() -> None:
    print("\n[3] adjacent-pair consistency checking")
    zero = pixelprobe.decode_pixel((0).to_bytes(4, "little"), 24)
    packed = 511 | (1023 << 10) | (511 << 20) | (1 << 30)
    written = pixelprobe.decode_pixel(packed.to_bytes(4, "little"), 24)

    def sample(gid, phase, value):
        return pixelprobe.Sample(
            slot=0, global_id=gid, phase=phase, resource_id=RESOURCE_ID,
            x=PIXEL_X, y=PIXEL_Y, recorded=True, readable=True,
            dxgi_format=24, raw_bytes=b"", value=value,
        )

    coherent = {
        GID_CLEAR: {"before": sample(GID_CLEAR, "before", written),
                    "after": sample(GID_CLEAR, "after", zero)},
        GID_DRAW_FAILED: {"before": sample(GID_DRAW_FAILED, "before", zero),
                          "after": sample(GID_DRAW_FAILED, "after", zero)},
        GID_DRAW_WROTE: {"before": sample(GID_DRAW_WROTE, "before", zero),
                         "after": sample(GID_DRAW_WROTE, "after", written)},
    }
    order = [GID_CLEAR, GID_DRAW_FAILED, GID_DRAW_WROTE]
    report = pixelprobe.check_consistency(order, coherent)
    check(report["mismatches"] == 0,
          "the GUI's own sequence is internally consistent", str(report))
    check(report["self_consistent"] is True, "and is reported as self-consistent")

    broken = dict(coherent)
    broken[GID_DRAW_FAILED] = {
        "before": sample(GID_DRAW_FAILED, "before", written),   # should be zero
        "after": sample(GID_DRAW_FAILED, "after", zero),
    }
    bad = pixelprobe.check_consistency(order, broken)
    check(bad["mismatches"] == 1,
          "an inconsistency is detected rather than smoothed over", str(bad))
    check(bad["self_consistent"] is False, "and invalidates the self-consistency flag")


# ======================================================================
# 4. candidate set and static rows (no GPU needed)
# ======================================================================
def test_static_history() -> dict:
    print("\n[4] candidate set and statically known rows (--no-replay)")
    envelope = run_tool([
        "pixel-history-replay", "--session", SESSION,
        "--resource-id", str(RESOURCE_ID),
        "--x", str(PIXEL_X), "--y", str(PIXEL_Y), "--no-replay",
    ])
    data = envelope.get("data") or {}
    rows = {row["global_id"]: row for row in data.get("history", [])}

    check(data["resource"]["format"] == "DXGI_FORMAT_R10G10B10A2_UNORM",
          "the target is the R10G10B10A2 GBufferA", str(data["resource"]))
    check(data["resource"]["dimensions"] == "1532x764",
          "with the dimensions from the GUI", str(data["resource"]))

    for gid in (GID_RECREATION, GID_CLEAR, GID_DRAW_FAILED, GID_DRAW_WROTE):
        check(gid in rows, f"the history contains the GUI's row for Global ID {gid}",
              f"present: {sorted(rows)}")

    # Row 1: initial contents, statically readable, must equal the GUI's New Value.
    recreation = rows.get(GID_RECREATION, {})
    check(raw_of(recreation.get("new_value")) == GBUFFERA_VALUE,
          "row gid 0 (Recreation) New Value matches the GUI bit for bit",
          f"got {raw_of(recreation.get('new_value'))}")
    check(recreation.get("is_synthetic_event") is True,
          "and is flagged as PIX's synthetic frame-start row, not an API call")

    # Row 2: the clear, whose recorded colour is all zeros in the export.
    clear = rows.get(GID_CLEAR, {})
    check(clear.get("event_type") == "clear",
          "row gid 3828 is a clear", str(clear.get("event_type")))
    check(clear.get("api") == "ClearRenderTargetView",
          "specifically ClearRenderTargetView", str(clear.get("api")))
    check(tuple(clear.get("recorded_clear_value") or ()) == (0.0, 0.0, 0.0, 0.0),
          "and its recorded clear colour is all zeros, as the GUI's New Value shows",
          str(clear.get("recorded_clear_value")))

    # Rows 3 and 4: both draws, both writing this resource at RTV slot 1, both with
    # the bound depth target the GUI's depth verdict depends on.
    for gid in (GID_DRAW_FAILED, GID_DRAW_WROTE):
        row = rows.get(gid, {})
        check(row.get("event_type") == "draw", f"row gid {gid} is a draw")
        check(row.get("rtv_slot") == 1,
              f"row gid {gid} writes this resource at RTV slot 1",
              str(row.get("rtv_slot")))
        evidence = row.get("depth_evidence") or {}
        check(evidence.get("depth_stencil_bound") is True,
              f"row gid {gid} has a depth target bound")
        check(evidence.get("depth_test_enabled") is True,
              f"row gid {gid} runs with depth testing enabled")
        check(evidence.get("depth_stencil_resource_id") == 1985,
              f"row gid {gid} tests against DSV resource 1985",
              str(evidence.get("depth_stencil_resource_id")))

    # gui_global_id must be present on every row so a GUI screenshot can be compared.
    check(all("gui_global_id" in row for row in data.get("history", [])),
          "every row carries gui_global_id for GUI cross-reference")
    check(all(row.get("gui_id_offset") == 0 for row in data.get("history", [])),
          "none of these events is an ExecuteIndirect, so no id offset is applied")

    # Unmeasured rows must say so rather than present a null as a value.
    for gid in (GID_CLEAR, GID_DRAW_FAILED, GID_DRAW_WROTE):
        row = rows.get(gid, {})
        check(row.get("verdict") == pixelprobe.VERDICT_UNKNOWN,
              f"row gid {gid} reports 'not_sampled' without a replay",
              str(row.get("verdict")))
    return data


# ======================================================================
# 5. the measured rows -- the actual GUI baseline
# ======================================================================
def test_measured_history(skip_build: bool, keep_probe: bool) -> None:
    print("\n[5] measured Previous/New values (requires a built replay)")
    # An absolute output directory, because the replay runs with its cwd set to the
    # export; a relative one silently lands somewhere else and looks like "no values".
    output_dir = (_ROOT / "pixel-dumps").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    argv = [
        "pixel-history-replay", "--session", SESSION,
        "--resource-id", str(RESOURCE_ID),
        "--x", str(PIXEL_X), "--y", str(PIXEL_Y),
        "--output", str(output_dir),
        "--build-timeout", "5400",
        "--settle-seconds", "900",
    ]
    if skip_build:
        argv.append("--skip-build")
    if keep_probe:
        argv.append("--keep-probe")
    envelope = run_tool(argv)
    data = envelope.get("data") or {}
    rows = {row["global_id"]: row for row in data.get("history", [])}
    measured = data.get("measured_event_count", 0)

    if not measured:
        detail = (
            "the replay produced no values; "
            f"replay={data.get('replay')} trace={data.get('trace')}"
        )
        for gid in (GID_CLEAR, GID_DRAW_FAILED, GID_DRAW_WROTE):
            skip(f"row gid {gid} measured values", detail)
        return

    # Row 2: Clear turns the initial value into zeros.
    clear = rows.get(GID_CLEAR, {})
    check(raw_of(clear.get("previous_value")) == GBUFFERA_VALUE,
          "gid 3828 Previous Value is the initial 0x1FF/0x3FF/0x1FF/0x1",
          str(raw_of(clear.get("previous_value"))))
    check(raw_of(clear.get("new_value")) == ZERO_VALUE,
          "gid 3828 New Value is all zeros",
          str(raw_of(clear.get("new_value"))))

    # Row 3: the draw the GUI reports as failing the depth/stencil test.
    failed = rows.get(GID_DRAW_FAILED, {})
    check(raw_of(failed.get("previous_value")) == ZERO_VALUE,
          "gid 3851 Previous Value is all zeros",
          str(raw_of(failed.get("previous_value"))))
    check(raw_of(failed.get("new_value")) == ZERO_VALUE,
          "gid 3851 leaves the texel unchanged, which is what the GUI's "
          "'Failed depth/stencil test' means for this pixel",
          str(raw_of(failed.get("new_value"))))
    check(failed.get("verdict") in
          (pixelprobe.VERDICT_DEPTH_FAILED, pixelprobe.VERDICT_UNCHANGED),
          "gid 3851's verdict is either the depth conclusion or the bare "
          "measurement, never 'wrote_value'", str(failed.get("verdict")))
    if failed.get("verdict") == pixelprobe.VERDICT_DEPTH_FAILED:
        check(failed.get("verdict_is_inferred") is True,
              "and if the depth conclusion is claimed, it is marked as inferred")

    # Row 4: the draw that writes the final value.
    wrote = rows.get(GID_DRAW_WROTE, {})
    check(raw_of(wrote.get("previous_value")) == ZERO_VALUE,
          "gid 3854 Previous Value is all zeros",
          str(raw_of(wrote.get("previous_value"))))
    check(raw_of(wrote.get("new_value")) == GBUFFERA_VALUE,
          "gid 3854 New Value is 0x1FF/0x3FF/0x1FF/0x1, matching the GUI",
          str(raw_of(wrote.get("new_value"))))
    check(wrote.get("verdict") == pixelprobe.VERDICT_WROTE,
          "gid 3854 is reported as having written the texel",
          str(wrote.get("verdict")))

    consistency = data.get("consistency") or {}
    check(consistency.get("mismatches") == 0,
          "no adjacent pair disagrees, so the history is a complete account",
          str(consistency))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", action="store_true",
                        help="also run the GPU replay (builds the export; slow)")
    parser.add_argument("--skip-build", action="store_true",
                        help="with --replay, reuse a matching existing executable "
                             "(only possible after a previous --keep-probe run)")
    parser.add_argument("--keep-probe", action="store_true",
                        help="leave the probe installed so a later --skip-build can "
                             "reuse this build")
    options = parser.parse_args()

    print("=" * 74)
    print(f"pixel history vs PIX GUI baseline: resource {RESOURCE_ID} at "
          f"({PIXEL_X}, {PIXEL_Y}) of session {SESSION!r}")
    print("=" * 74)

    test_bit_unpacking()
    test_verdict_honesty()
    test_consistency_checker()
    test_static_history()

    if options.replay:
        test_measured_history(options.skip_build, options.keep_probe)
    else:
        for gid in (GID_CLEAR, GID_DRAW_FAILED, GID_DRAW_WROTE):
            skip(f"row gid {gid} measured values",
                 "pass --replay to build the export and measure on the GPU")

    print("\n" + "=" * 74)
    if _failures:
        print(f"FAILED: {len(_failures)} check(s)")
        for item in _failures:
            print(f"  - {item}")
    if _skips:
        print(f"NOT MEASURED: {len(_skips)} check(s)")
        for item in _skips:
            print(f"  - {item}")
    if not _failures and not _skips:
        print("PASSED: every GUI baseline row was verified against a measurement.")
        return 0
    if _failures:
        return 1
    # Skips alone are still a non-zero exit: the four-row baseline is the point of
    # this script, and reporting success without having measured it would be the
    # false-pass this project's rules forbid.
    print("INCOMPLETE: static checks passed, but the measured rows were not verified.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
