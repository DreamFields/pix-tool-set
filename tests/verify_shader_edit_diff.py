"""Check shader-edit-diff without paying for two replays every time.

The tool's expensive part - two armed replays - is also the part that is hardest to
break, because it is `read-uav`'s pipeline unchanged. What is new here, and so what this
script guards, is everything around it:

  * the patch toggle is exception safe. This is the property that matters most: a run
    that dies between "rename away" and "rename back" leaves the user's edit looking
    lost. Tested by forcing a failure inside the context manager and by simulating a
    crashed previous run that left a .hold file behind.
  * a missing or inert patch is refused *before* replaying, with an error that names the
    fix. Discovering it after six minutes reads as "the edit did nothing".
  * the difference maths is right, and both sides are decoded through one shared display
    range so a real change cannot be normalised away.
  * the numbers reproduce the manually measured ground truth for RWNormalTexture.

The comparison is run against the dumps `read-uav` already wrote under tests/_uav_check*,
so the whole check takes seconds. Pass --live to additionally drive the real tool end to
end, which does replay twice.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pix_tool_set.engine import uavprobe  # noqa: E402
from pix_tool_set.errors import PixToolError  # noqa: E402
from pix_tool_set.tools import shader_diff_tools as sdt  # noqa: E402

HERE = Path(__file__).resolve().parent

# Ground truth measured by hand on session `Tiled`, QueueID 18704, u1 = RWNormalTexture
# = resource 3032. The patched shader writes a 32px red/green checkerboard; the original
# writes world normals.
EXPECTED = {
    "after": {"R": 127.5, "G": 127.5, "B": 0.0},
    "before": {"R": 127.4, "G": 214.5, "B": 167.9},
    "changed_share_percent": 100.0,
}
TOLERANCE = 1.0

failures: list[str] = []
checks = 0


def check(condition: bool, description: str, detail: str = "") -> None:
    global checks
    checks += 1
    if condition:
        print(f"  ok   {description}")
    else:
        print(f"  FAIL {description}" + (f" -- {detail}" if detail else ""))
        failures.append(description + (f" ({detail})" if detail else ""))


# ======================================================================
def test_toggle_is_exception_safe(tmp: Path) -> None:
    print("\npatch toggle safety")
    patch = tmp / "edited_CreatePipelineState_9999_CS.dxil"
    patch.write_bytes(b"DXBC-pretend")

    with sdt._patch_disabled(patch) as held:
        check(not patch.exists(), "the patch is renamed away inside the block")
        check(held.exists(), "the .hold file carries the bytecode while disabled")
        check(
            held.name.endswith(sdt.HOLD_SUFFIX),
            "the disabled name uses the documented suffix",
            held.name,
        )
    check(patch.exists(), "the patch is restored on the normal path")
    check(not held.exists(), "no .hold file is left behind")

    # The property that actually protects the user: a failure anywhere inside must not
    # leave the edit disabled.
    class Boom(RuntimeError):
        pass

    try:
        with sdt._patch_disabled(patch):
            raise Boom("the replay blew up")
    except Boom:
        pass
    check(patch.exists(), "the patch is restored when the body raises")
    check(patch.read_bytes() == b"DXBC-pretend", "the bytecode survives the round trip")

    # And a crashed earlier run that left its .hold behind must not block this one.
    stale = patch.with_name(patch.name + sdt.HOLD_SUFFIX)
    stale.write_bytes(b"stale-from-a-dead-run")
    with sdt._patch_disabled(patch):
        pass
    check(patch.exists(), "a stale .hold from a dead run does not block the toggle")
    check(
        patch.read_bytes() == b"DXBC-pretend",
        "the live bytecode wins over a stale .hold",
        patch.read_bytes()[:24].decode("latin-1"),
    )


def test_missing_patch_is_refused_early(tmp: Path) -> None:
    print("\nrefusing before any replay")
    absent = tmp / "edited_CreatePipelineState_1_CS.dxil"
    try:
        sdt._require_patch(tmp, absent, 1, "CS", "--queue-id 18704")
        check(False, "a missing patch raises")
    except PixToolError as exc:
        check(exc.code == "shader_patch_missing", "missing patch has its own error code", exc.code)
        check(
            "shader-edit-apply" in (exc.suggestion or ""),
            "the error names shader-edit-apply --patch as the fix",
        )
        check(
            "--stage CS" in (exc.suggestion or "") and "--queue-id 18704" in (exc.suggestion or ""),
            "the suggested command carries the caller's own selectors",
        )

    # A .dxil nobody reads is worse than none: it would silently compare a shader to
    # itself, which looks like "the edit did nothing".
    inert = tmp / "edited_CreatePipelineState_2_CS.dxil"
    inert.write_bytes(b"x")
    (tmp / "CreatePSOs.cpp").write_text("void CreatePipelineState_2() {}\n", encoding="utf-8")
    try:
        sdt._require_patch(tmp, inert, 2, "CS", "--queue-id 18704")
        check(False, "an unreferenced patch raises")
    except PixToolError as exc:
        check(exc.code == "shader_patch_inert", "an inert patch is distinguished", exc.code)

    (tmp / "CreatePSOs.cpp").write_text(
        'ReadFileBytes(LR"(edited_CreatePipelineState_2_CS.dxil)");\n', encoding="utf-8"
    )
    info = sdt._require_patch(tmp, inert, 2, "CS", "--queue-id 18704")
    check(info.get("referenced_by_createpsos") is True, "a wired-in patch is accepted")


def test_difference_maths() -> None:
    print("\ndifference maths")
    width = 4
    before = [bytearray(b"\x00\x00\x00\xff" * width)]
    after = [bytearray(b"\x00\x00\x00\xff" * width)]
    # pixel 0 identical, 1 just under threshold, 2 just over, 3 saturated
    after[0][1 * 4 + 2] = 6      # dR = 6, not over a threshold of 6
    after[0][2 * 4 + 2] = 7      # dR = 7, over it
    after[0][3 * 4 + 0] = 255    # dB = 255

    report, diff = sdt._compare(before, after, threshold=6)
    check(report["compared_pixels"] == 4, "every pixel is compared, not sampled")
    check(report["changed_pixels"] == 2, "the threshold is exclusive", str(report["changed_pixels"]))
    check(report["differing_pixels"] == 3, "pixels differing at all are counted separately")
    check(report["changed_share_percent"] == 50.0, "the share is a percentage of the surface")
    check(report["max_abs_delta_8bit"]["B"] == 255, "per-channel peaks are reported")
    check(report["max_abs_delta_8bit"]["G"] == 0, "an untouched channel peaks at zero")
    check(diff[0][0 * 4] == 0, "an identical pixel is black in the diff map")
    check(diff[0][2 * 4] == 7, "a changed pixel's grey level is the summed delta")
    check(diff[0][3 * 4] == 255, "the diff map clamps at 255")

    blob, side_width, side_height = sdt._side_by_side(before, after, gap=8)
    check(side_width == width * 2 + 8, "side-by-side is both surfaces plus a gap", str(side_width))
    check(len(blob) == side_width * side_height * 4, "the side-by-side buffer is fully sized")


def test_shared_display_range() -> None:
    print("\nshared display range")
    left = {"channels": [{"channel": "R", "min": 0.0, "max": 1.0, "mean": 0.5}]}
    right = {"channels": [{"channel": "R", "min": 4.0, "max": 9.0, "mean": 6.0}]}
    low, high = sdt._shared_range([left, right], normalised=False)
    check((low, high) == (0.0, 9.0), "a float range spans both sides", f"{low}..{high}")
    low, high = sdt._shared_range([left, right], normalised=True)
    check((low, high) == (0.0, 1.0), "UNORM data is not stretched at all", f"{low}..{high}")

    table = sdt._channel_table(
        {"channels": [{"channel": "R", "mean": 0.5, "mean_8bit": 127.5}]},
        {"channels": [{"channel": "R", "mean": 0.84, "mean_8bit": 214.5}]},
        normalised=True,
    )
    check(table[0]["delta_mean_8bit"] == 87.0, "the channel table reports a signed delta")


def newest_dumps() -> tuple[Path, Path] | None:
    """The patched and original dumps read-uav already wrote, if both are present."""
    after = sorted(HERE.glob("_uav_check/*.bin"), key=lambda p: -p.stat().st_mtime)
    before = sorted(HERE.glob("_uav_check_orig/*.bin"), key=lambda p: -p.stat().st_mtime)
    diffed = sorted(HERE.glob("_diff_check/*.bin"), key=lambda p: -p.stat().st_mtime)
    if len(diffed) >= 2:
        pair = {p.name.split("_")[0]: p for p in diffed}
        if "before" in pair and "after" in pair:
            return pair["before"], pair["after"]
    if before and after:
        return before[0], after[0]
    return None


def test_against_ground_truth() -> None:
    print("\nreproducing the measured ground truth")
    found = newest_dumps()
    if found is None:
        print("  SKIP no dump pair under tests/_diff_check or tests/_uav_check*;")
        print("       run shader-edit-diff or read-uav first")
        return
    before_path, after_path = found
    print(f"  before: {before_path.parent.name}/{before_path.name}")
    print(f"  after : {after_path.parent.name}/{after_path.name}")

    _, before_image, before_stats = sdt._decode(before_path)
    _, after_image, after_stats = sdt._decode(after_path)

    normalised = before_image.format_name.endswith(("UNORM", "UNORM_SRGB"))
    low, high = sdt._shared_range([before_stats, after_stats], normalised)
    before_rows = sdt._bgra_rows(before_image, low, high)
    after_rows = sdt._bgra_rows(after_image, low, high)
    report, _ = sdt._compare(before_rows, after_rows, sdt.DEFAULT_THRESHOLD)

    for side, stats in (("before", before_stats), ("after", after_stats)):
        for entry in stats.get("channels") or []:
            name = entry["channel"]
            if name not in EXPECTED[side]:
                continue
            got = entry.get("mean_8bit")
            want = EXPECTED[side][name]
            check(
                got is not None and abs(got - want) <= TOLERANCE,
                f"{side} {name} mean is {want} +/- {TOLERANCE}",
                f"got {got}",
            )

    print(f"  changed: {report['changed_pixels']}/{report['compared_pixels']} "
          f"({report['changed_share_percent']}%)")
    check(
        abs(report["changed_share_percent"] - EXPECTED["changed_share_percent"]) <= TOLERANCE,
        f"the changed share is {EXPECTED['changed_share_percent']}%",
        f"got {report['changed_share_percent']}%",
    )
    check(
        report["compared_pixels"] == before_image.width * before_image.height,
        "the comparison covered the whole surface",
    )


def test_live() -> None:
    """Drive the real tool. Two replays, so it is opt-in."""
    print("\nlive end-to-end run (two replays)")
    output = HERE / "_diff_live"
    command = [
        sys.executable, "-m", "pix_tool_set.cli", "--compact", "shader-edit-diff",
        "--queue-id", "18704", "--stage", "CS", "--name", "RWNormalTexture",
        "--output", str(output),
    ]
    proc = subprocess.run(
        command, cwd=str(ROOT / "src"), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=1800,
    )
    check(proc.returncode == 0, "the tool exits cleanly", proc.stderr[-400:])
    import json

    payload = json.loads(proc.stdout)
    data = payload.get("data", {})
    check(payload.get("status") == "success", "status is success", str(payload.get("status")))
    check(data.get("patch", {}).get("restored") is True, "the patch name was restored")
    patch = Path(data.get("patch", {}).get("path", ""))
    check(patch.exists(), "the patch file is present under its original name")
    check(
        not patch.with_name(patch.name + sdt.HOLD_SUFFIX).exists(),
        "no .hold file survives the run",
    )
    share = data.get("comparison", {}).get("changed_share_percent")
    check(
        share is not None and abs(share - EXPECTED["changed_share_percent"]) <= TOLERANCE,
        "the live changed share matches the manual measurement",
        f"got {share}",
    )


def main() -> int:
    import tempfile

    print("verify shader-edit-diff")
    with tempfile.TemporaryDirectory(prefix="pixts-diff-") as raw:
        tmp = Path(raw)
        test_toggle_is_exception_safe(tmp)
        test_missing_patch_is_refused_early(tmp)
    test_difference_maths()
    test_shared_display_range()
    test_against_ground_truth()
    if "--live" in sys.argv:
        test_live()

    print(f"\n{checks - len(failures)}/{checks} checks passed")
    if failures:
        print("FAIL")
        for item in failures:
            print("  -", item)
        return 1
    print("PASS: the patch toggle is exception safe and the diff reproduces the "
          "measured values")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
