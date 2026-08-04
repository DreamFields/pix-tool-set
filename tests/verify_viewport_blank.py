"""Regression for the region-level blankness test, against real captures.

Why this exists: `content_score` measures how much of a frame differs from its most
common colour, over the whole inset window. That number cannot distinguish a rendered
scene from a tool panel floating over an unrendered viewport, and on the Tiled capture it
did not: a 600x400 GPU Visualizer panel in the corner of a 1280x720 window scored 0.2803,
sailed past the 0.02 gate, and the whole-window diff that followed read as a verdict
about a shader patch when the 3D viewport had never drawn anything at all.

The samples here are the frames that produced that mistake and two that must not be
caught by the fix:

  * replay_baseline-18704_...1280x720.png - the UI-over-black-viewport capture. Must be
    called out as content confined to part of the frame.
  * RWNormalTexture_BEFORE.png / _AFTER.png - real UAV exports, content edge to edge.
    Must stay "content across frame", or the new test would start rejecting good frames.

Synthetic cases cover the shapes no fixture happens to contain. PNG decoding reuses
engine/png.py, which already unfilters scanlines for the depth exports, so nothing new is
hand-rolled here.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set.engine import png as pngmod  # noqa: E402
from pix_tool_set.engine import screencap  # noqa: E402

SAMPLES = Path(__file__).resolve().parent / "_edit18704"
BASELINE = SAMPLES / "baseline.png" / "replay_baseline-18704_20260804-153653_1280x720.png"
BEFORE = SAMPLES / "RWNormalTexture_BEFORE.png"
AFTER = SAMPLES / "RWNormalTexture_AFTER.png"

PASSED: list[str] = []
FAILED: list[str] = []
SKIPPED: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    if condition:
        PASSED.append(label)
        print(f"  PASS  {label}")
    else:
        FAILED.append(f"{label}: {detail}")
        print(f"  FAIL  {label} :: {detail}")
    return condition


def load_bgra(path: Path) -> tuple[bytearray, int, int]:
    """A PNG on disk as the tightly packed BGRA rows screencap works in."""
    image = pngmod.parse(path)
    samples = image.samples
    channels = image.channels
    shift = 8 if image.bit_depth == 16 else 0
    out = bytearray(image.width * image.height * 4)
    for index in range(image.width * image.height):
        base = index * channels
        if channels >= 3:
            r, g, b = samples[base], samples[base + 1], samples[base + 2]
        else:
            r = g = b = samples[base]
        if shift:
            r, g, b = r >> shift, g >> shift, b >> shift
        offset = index * 4
        out[offset] = b
        out[offset + 1] = g
        out[offset + 2] = r
        out[offset + 3] = 255
    return out, image.width, image.height


def report(label: str, facts: dict) -> None:
    print(
        f"    {label}: verdict={facts['verdict']} "
        f"blank_cells={facts['blank_cells']}/{facts['cells']} "
        f"({facts['blank_cell_share']}) flat={facts['flat_cell_share']} "
        f"centre_blank={facts['centre_blank_share']} "
        f"near_black={facts['near_black_cell_share']} "
        f"largest_blank_rect_share="
        f"{(facts['largest_blank_rect'] or {}).get('share_of_frame')}"
    )
    content = facts["largest_content_rect"]
    if content:
        print(
            f"    {label}: largest content {content['width']}x{content['height']} "
            f"at ({content['x']},{content['y']}) share={content['share_of_frame']}"
        )


# ----------------------------------------------------------------------
def synth(width: int, height: int, colour: tuple[int, int, int]) -> bytearray:
    b, g, r = colour[2], colour[1], colour[0]
    return bytearray(bytes((b, g, r, 255)) * (width * height))


def paint_noise(
    frame: bytearray, width: int, x0: int, y0: int, x1: int, y1: int
) -> None:
    """Fill a box with a varying pattern, so no cell inside it reads as flat."""
    for y in range(y0, y1):
        row = y * width
        for x in range(x0, x1):
            offset = (row + x) * 4
            frame[offset] = (x * 7 + y * 13) % 256
            frame[offset + 1] = (x * 3 + y * 29) % 256
            frame[offset + 2] = (x * 17 + y * 5) % 256


def stage_real_captures() -> None:
    print("[1] the capture that caused the wrong verdict is called out")
    if not BASELINE.exists():
        SKIPPED.append(f"missing sample {BASELINE}")
        print(f"  SKIP  sample not present: {BASELINE}")
        return

    pixels, width, height = load_bgra(BASELINE)
    check("baseline decodes to the recorded size", (width, height) == (1280, 720),
          f"{width}x{height}")

    # The score that started this: high enough to look like a rendered frame.
    score = screencap.content_score(pixels, width, height)
    print(f"    content_score={score:.4f} (unchanged behaviour, for reference)")
    check("content_score still passes the 0.02 gate on this frame", score >= 0.02,
          f"{score:.4f}")
    check("content_score is close to the 0.2803 measured in the field",
          abs(score - 0.2803) < 0.05, f"{score:.4f}")

    facts = screencap.viewport_blankness(pixels, width, height)
    report("baseline", facts)
    check("baseline is not called a fully blank frame",
          facts["verdict"] != "frame_blank", facts["verdict"])
    check("baseline is called content confined to part of the frame",
          facts["verdict"] == "content_confined_to_part_of_frame", facts["verdict"])
    check("baseline sets main_area_blank", facts["main_area_blank"] is True)
    check("most of the baseline reads as blank", facts["blank_cell_share"] >= 0.5,
          str(facts["blank_cell_share"]))
    check("the blank part of the baseline is near black",
          facts["near_black_cell_share"] >= 0.5, str(facts["near_black_cell_share"]))
    check("the baseline's middle is blank", facts["centre_blank_share"] >= 0.5,
          str(facts["centre_blank_share"]))

    content = facts["largest_content_rect"]
    check("the content is located", content is not None)
    if content:
        check("the located content is in the upper left",
              content["x"] < width // 2 and content["y"] < height // 2, str(content))
        check("the located content is a minority of the frame",
              content["share_of_frame"] < 0.4, str(content["share_of_frame"]))
    check("the note explains why the frame cannot be measured whole",
          "not the scene" in facts["note"], facts["note"])


def stage_normal_frames() -> None:
    print("[2] frames with content edge to edge are left alone")
    for path in (BEFORE, AFTER):
        if not path.exists():
            SKIPPED.append(f"missing sample {path}")
            print(f"  SKIP  sample not present: {path}")
            continue
        pixels, width, height = load_bgra(path)
        facts = screencap.viewport_blankness(pixels, width, height)
        report(path.name, facts)
        check(f"{path.name} is content across the frame",
              facts["verdict"] == "content_across_frame", facts["verdict"])
        check(f"{path.name} does not set main_area_blank",
              facts["main_area_blank"] is False, str(facts))
        check(f"{path.name} has few blank cells", facts["blank_cell_share"] < 0.45,
              str(facts["blank_cell_share"]))
        rect = facts["largest_content_rect"]
        check(f"{path.name} reports content over most of the frame",
              rect is not None and rect["share_of_frame"] > 0.5,
              str(rect))

    # The reason the first attempt got this wrong: BEFORE is two large fields of flat
    # saturated colour. Flat per cell, and entirely real content, so flatness alone
    # cannot be the test for blankness.
    if BEFORE.exists():
        pixels, width, height = load_bgra(BEFORE)
        facts = screencap.viewport_blankness(pixels, width, height)
        check("flat saturated fields are recognised as flat",
              facts["flat_cell_share"] > 0.8, str(facts["flat_cell_share"]))
        check("flat saturated fields are still not blank",
              facts["blank_cell_share"] == 0.0, str(facts["blank_cell_share"]))


def stage_synthetic_shapes() -> None:
    print("[3] the shapes no fixture happens to contain")
    width, height = 640, 400

    flat = synth(width, height, (0, 0, 0))
    facts = screencap.viewport_blankness(flat, width, height)
    report("all black", facts)
    check("an all-black frame is frame_blank", facts["verdict"] == "frame_blank",
          facts["verdict"])
    check("an all-black frame is entirely near black",
          facts["near_black_cell_share"] == 1.0, str(facts["near_black_cell_share"]))

    flat_white = synth(width, height, (250, 250, 250))
    facts = screencap.viewport_blankness(flat_white, width, height)
    report("all white", facts)
    check("a flat white frame is frame_blank", facts["verdict"] == "frame_blank",
          facts["verdict"])
    check("a flat white frame is not reported as black",
          facts["near_black_cell_share"] == 0.0, str(facts["near_black_cell_share"]))

    # A mid grey carries as little as white does, but is neither dark nor bright; the
    # uniform-frame test is what catches it.
    flat_grey = synth(width, height, (128, 128, 128))
    facts = screencap.viewport_blankness(flat_grey, width, height)
    report("all mid grey", facts)
    check("a uniformly flat frame of any colour is frame_blank",
          facts["verdict"] == "frame_blank", facts["verdict"])

    corner = synth(width, height, (0, 0, 0))
    paint_noise(corner, width, 0, 0, 300, 200)
    facts = screencap.viewport_blankness(corner, width, height)
    report("corner panel", facts)
    check("a panel over a black frame is content_confined_to_part_of_frame",
          facts["verdict"] == "content_confined_to_part_of_frame", facts["verdict"])

    full = synth(width, height, (0, 0, 0))
    paint_noise(full, width, 0, 0, width, height)
    facts = screencap.viewport_blankness(full, width, height)
    report("full noise", facts)
    check("a fully textured frame is content_across_frame",
          facts["verdict"] == "content_across_frame", facts["verdict"])
    check("a fully textured frame has no blank cells",
          facts["blank_cells"] == 0, str(facts["blank_cells"]))

    # A black sky over a rendered ground is blank at the top but not in the middle, and
    # must not be mistaken for an empty viewport.
    sky = synth(width, height, (0, 0, 0))
    paint_noise(sky, width, 0, height // 3, width, height)
    facts = screencap.viewport_blankness(sky, width, height)
    report("black sky", facts)
    check("a black sky over rendered ground is content_across_frame",
          facts["verdict"] == "content_across_frame", facts["verdict"])

    tiny = synth(20, 20, (0, 0, 0))
    facts = screencap.viewport_blankness(tiny, 20, 20)
    check("a tiny frame is refused rather than guessed", facts["verdict"] == "too_small",
          facts["verdict"])
    check("a refused frame does not claim the main area is blank",
          facts["main_area_blank"] is False, str(facts))


def stage_content_score_untouched() -> None:
    print("[4] content_score's own behaviour is unchanged")
    # The measured shape from verify_replay_render.py: a blank window whose chrome alone
    # scored 0.049 on a naive whole-image test. The inset must still hold it near zero.
    frame = synth(1280, 720, (253, 253, 253))
    for y in range(0, 32):
        for x in range(1280):
            offset = (y * 1280 + x) * 4
            frame[offset : offset + 3] = bytes((240, 240, 240))
    for y in range(720):
        for x in range(0, 8):
            offset = (y * 1280 + x) * 4
            frame[offset : offset + 3] = bytes((0, 0, 0))
    score = screencap.content_score(frame, 1280, 720)
    check("a blank window with chrome still scores below the gate", score < 0.02,
          f"{score:.4f}")
    check("content_score still returns a float", isinstance(score, float))
    check("a tiny frame still scores 0.0",
          screencap.content_score(synth(10, 10, (0, 0, 0)), 10, 10) == 0.0)


def stage_conclusion_split() -> None:
    print("[5] the measurement and what it proves are stated separately")
    from pix_tool_set.tools import replay_render_tools as rrt

    across = {"main_area_blank": False, "verdict": "content_across_frame"}
    confined = {"main_area_blank": True, "verdict": "content_confined_to_part_of_frame"}

    same = {"comparable": True, "visibly_different": False}
    rrt._state_what_the_diff_proves(same, across)
    check("two valid frames that match support a conclusion",
          same["supports_conclusion"] is True, str(same))
    check("the conclusion names the backbuffer, not a failed patch",
          same["conclusion"].startswith("unchanged_on_valid_frames"), same["conclusion"])
    check("the conclusion still separates 'not applied' from 'not presented'",
          "read-uav" in same["conclusion"], same["conclusion"])

    blocked = {"comparable": True, "visibly_different": False}
    rrt._state_what_the_diff_proves(blocked, confined)
    check("a confined current frame blocks the conclusion",
          blocked["supports_conclusion"] is False, str(blocked))
    check("the measurement is still reported",
          blocked["visibly_different"] is False, str(blocked))
    check("the reason is named", "main area carries nothing" in
          blocked["inconclusive_because"], blocked["inconclusive_because"])
    check("the wording refuses to call this a failed patch",
          "not evidence that the patch failed" in blocked["conclusion"],
          blocked["conclusion"])
    check("read-uav is offered as the way forward",
          "read-uav" in blocked["conclusion"], blocked["conclusion"])

    from_blank_reference = {
        "comparable": True,
        "visibly_different": False,
        "reference_regions": confined,
    }
    rrt._state_what_the_diff_proves(from_blank_reference, across)
    check("a confined reference frame also blocks the conclusion",
          from_blank_reference["supports_conclusion"] is False,
          str(from_blank_reference))
    check("the blocking frame is identified as the reference",
          "reference capture" in from_blank_reference["inconclusive_because"],
          from_blank_reference["inconclusive_because"])

    differs = {"comparable": True, "visibly_different": True}
    rrt._state_what_the_diff_proves(differs, across)
    check("a real difference on valid frames is stated as changed",
          differs["conclusion"].startswith("changed"), differs["conclusion"])

    incomparable = {"comparable": False, "reason": "nothing to compare"}
    rrt._state_what_the_diff_proves(incomparable, across)
    check("an incomparable pair is left untouched",
          "conclusion" not in incomparable, str(incomparable))

    # A reference recorded before region analysis existed has no such facts, and absence
    # must not be read as "the area was fine".
    legacy = {"comparable": True, "visibly_different": False}
    rrt._state_what_the_diff_proves(legacy, across)
    check("a reference without region facts does not fabricate them",
          "reference_regions" not in legacy, str(legacy))


def main() -> int:
    stage_real_captures()
    print()
    stage_normal_frames()
    print()
    stage_synthetic_shapes()
    print()
    stage_content_score_untouched()
    print()
    stage_conclusion_split()

    print()
    print(f"{len(PASSED)} passed, {len(FAILED)} failed, {len(SKIPPED)} skipped")
    for entry in FAILED:
        print("  -", entry)
    for entry in SKIPPED:
        print("  ~", entry)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
