"""Regression for capturing the replay's rendered frame and showing it in the viewer.

The interesting failure here is not a crash but a *plausible wrong answer*: capturing the
replay window before it presents yields a blank white page, and two blank pages compare
as "identical", which reads as "the patch did nothing". A real magenta capture and a real
blank capture from the same replayer are kept as fixtures so that judgement stays honest.

Fixtures are generated on demand if absent, but the expensive path (build + run) is not
exercised here; tests/verify_shader_edit.py and the README recipe cover that.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set.engine import screencap  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    if condition:
        PASSED.append(label)
        print(f"  PASS  {label}")
    else:
        FAILED.append(f"{label}: {detail}")
        print(f"  FAIL  {label} :: {detail}")
    return condition


def synth(width: int, height: int, colour: tuple[int, int, int]) -> bytearray:
    """A flat BGRA frame, standing in for an un-presented window."""
    out = bytearray()
    b, g, r = colour[2], colour[1], colour[0]
    for _ in range(width * height):
        out += bytes((b, g, r, 255))
    return out


def synth_with_chrome(width: int, height: int) -> bytearray:
    """White interior plus a dark title strip and edge, like a blank replay window.

    This is the shape that fooled a whole-image score into reporting 0.049 - enough to
    pass a 0.02 threshold while showing nothing.
    """
    frame = synth(width, height, (253, 253, 253))
    for y in range(0, min(32, height)):
        for x in range(width):
            offset = (y * width + x) * 4
            frame[offset : offset + 3] = bytes((240, 240, 240))
    for y in range(height):
        for x in range(0, min(8, width)):
            offset = (y * width + x) * 4
            frame[offset : offset + 3] = bytes((0, 0, 0))
    return frame


# ----------------------------------------------------------------------
def stage_encoder(work: Path) -> None:
    print("[1] the RGB PNG encoder produces a valid, correctly shaped file")
    import struct

    width, height = 64, 40
    frame = synth(width, height, (255, 0, 255))
    path = work / "magenta.png"
    written = screencap.write_png(path, frame, width, height)
    blob = path.read_bytes()

    check("file written", written > 0 and path.exists())
    check("PNG signature", blob[:8] == b"\x89PNG\r\n\x1a\n")
    w, h, depth, colour_type = struct.unpack(">2IBB", blob[16:26])
    check("IHDR dimensions", (w, h) == (width, height), f"{w}x{h}")
    check("8-bit truecolour", (depth, colour_type) == (8, 2), f"{depth}/{colour_type}")

    from pix_tool_set.engine import png as pngmod

    decoded = pngmod.parse(path)
    check("round-trips through the decoder",
          (decoded.width, decoded.height) == (width, height))
    first = decoded.samples[:3]
    check("channel order preserved (R,G,B)", list(first) == [255, 0, 255], str(list(first)))


def stage_content_score() -> None:
    print("[2] a blank window scores zero while a rendered frame does not")
    blank = synth_with_chrome(1280, 720)
    blank_score = screencap.content_score(blank, 1280, 720)
    check("blank window scores ~0", blank_score < 0.005, f"{blank_score:.4f}")
    check("blank window is below the 0.02 gate", blank_score < 0.02, f"{blank_score:.4f}")

    # Interior content: half the inside is a different colour.
    rendered = synth_with_chrome(1280, 720)
    for y in range(100, 600):
        for x in range(100, 700):
            offset = (y * 1280 + x) * 4
            rendered[offset : offset + 3] = bytes((255, 0, 255))
    rendered_score = screencap.content_score(rendered, 1280, 720)
    check("rendered frame scores well above the gate", rendered_score > 0.10,
          f"{rendered_score:.4f}")
    check("rendered clearly separates from blank", rendered_score > blank_score * 20,
          f"{rendered_score:.4f} vs {blank_score:.4f}")

    check("a tiny window is refused rather than guessed",
          screencap.content_score(synth(10, 10, (0, 0, 0)), 10, 10) == 0.0)


def stage_colour_compare() -> None:
    print("[3] colour summaries state a change numerically")
    grey = synth(200, 120, (60, 60, 60))
    magenta = synth(200, 120, (255, 0, 255))

    grey_summary = screencap.colour_summary(grey, 200, 120)
    magenta_summary = screencap.colour_summary(magenta, 200, 120)
    check("grey is classified as grey",
          grey_summary["hue_share_percent"]["grey"] == 100.0, str(grey_summary))
    check("magenta is classified as magenta",
          magenta_summary["hue_share_percent"]["magenta"] == 100.0, str(magenta_summary))

    same = screencap.compare(grey_summary, grey_summary)
    check("identical frames are not 'different'", same["visibly_different"] is False)
    changed = screencap.compare(grey_summary, magenta_summary)
    check("grey to magenta is 'different'", changed["visibly_different"] is True)
    check("the delta names the channels",
          len(changed["mean_rgb_delta"]) == 3, str(changed.get("mean_rgb_delta")))
    check("hue shift is reported",
          changed["hue_share_delta_percent"]["magenta"] > 90.0,
          str(changed["hue_share_delta_percent"]))

    # Dithering-scale noise must not be called a change.
    nudged = synth(200, 120, (62, 62, 62))
    subtle = screencap.compare(grey_summary, screencap.colour_summary(nudged, 200, 120))
    check("a 2-level nudge is not a change", subtle["visibly_different"] is False,
          str(subtle))

    check("an empty summary is not comparable",
          screencap.compare({}, magenta_summary)["comparable"] is False)


def stage_window_enumeration() -> None:
    print("[4] window discovery survives the awkward cases")
    check("an unknown pid yields nothing", screencap.list_windows(999999) == [])
    check("an unknown pid picks nothing", screencap.pick_window(999999) is None)

    own = screencap.list_windows(os.getpid())
    check("enumerating our own process does not raise", isinstance(own, list))


def stage_render_store(work: Path) -> None:
    print("[5] renders are stored, served and path-guarded")
    log_dir = work / "log"
    os.environ["PIX_TOOL_SET_ACTIVITY_DIR"] = str(log_dir)

    import importlib

    from pix_tool_set.engine import activity

    importlib.reload(activity)

    frame = synth(80, 50, (255, 0, 255))
    name = "replay_test_20260101-000000_80x50.png"
    screencap.write_png(activity.renders_dir() / name, frame, 80, 50)

    check("render is readable by name", activity.read_render(name) is not None)
    check("render appears in the listing",
          any(row["name"] == name for row in activity.list_renders()),
          str(activity.list_renders()))
    check("traversal is refused", activity.read_render("../../secret.png") is None)
    check("non-png is refused", activity.read_render("notes.txt") is None)
    check("absolute path is refused", activity.read_render(r"C:\Windows\win.png") is None)
    check("unknown name is refused", activity.read_render("nope.png") is None)

    # And over HTTP.
    from http.server import ThreadingHTTPServer

    from pix_tool_set.tools import activity_tools

    server = activity_tools._Server(("127.0.0.1", 0), activity_tools._Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        with urllib.request.urlopen(f"{base}/api/render?name={name}", timeout=10) as resp:
            body = resp.read()
            check("render served over HTTP",
                  resp.status == 200 and body[:8] == b"\x89PNG\r\n\x1a\n", str(resp.status))
            check("served with an image content type",
                  resp.headers.get("Content-Type") == "image/png",
                  str(resp.headers.get("Content-Type")))
        with urllib.request.urlopen(f"{base}/api/renders", timeout=10) as resp:
            listing = json.loads(resp.read())
            check("render listing served", any(r["name"] == name for r in listing["renders"]))
        try:
            urllib.request.urlopen(f"{base}/api/render?name=../../x.png", timeout=10)
            check("HTTP traversal refused", False, "request succeeded")
        except urllib.error.HTTPError as exc:
            check("HTTP traversal refused", exc.code == 404, str(exc.code))
    finally:
        server.shutdown()
        server.server_close()


def stage_viewer_markup() -> None:
    print("[6] the viewer page can display a render")
    page = (
        Path(__file__).resolve().parents[1]
        / "src" / "pix_tool_set" / "viewer" / "activity.html"
    ).read_text(encoding="utf-8")

    check("render tab present", 'data-tab="render"' in page)
    check("render endpoint used", "api/render?name=" in page)
    check("standalone renders inlined", "boot.renders" in page)
    check("thumbnail strip present", 'id="strip"' in page)
    check("lightbox present", 'id="lightbox"' in page)
    check("comparison markup present", 'class="compare"' in page)
    check("blank/unchanged verdict surfaced", "visibly_different" in page)
    check("overview embeds the shot", "shotHtml(entry, url)" in page)


def stage_port_conflict() -> None:
    print("[7] a port already in use is refused, not silently shared")
    from pix_tool_set.tools import activity_tools

    # Let the OS pick a free port; a hard-coded one collides with leftovers from an
    # earlier run and would fail for the wrong reason.
    first = activity_tools._Server(("127.0.0.1", 0), activity_tools._Handler)
    port = first.server_address[1]
    thread = threading.Thread(target=first.serve_forever, daemon=True)
    thread.start()
    try:
        try:
            second = activity_tools._Server(("127.0.0.1", port), activity_tools._Handler)
            second.server_close()
            check("a second bind on the same port fails", False,
                  f"the bind on {port} succeeded, so two viewers could serve different logs")
        except OSError:
            check("a second bind on the same port fails", True)
        check("reuse is disabled", activity_tools._Server.allow_reuse_address is False)
    finally:
        first.shutdown()
        first.server_close()


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="pixts-render-"))
    print(f"work dir: {work}\n")
    try:
        stage_encoder(work)
        print()
        stage_content_score()
        print()
        stage_colour_compare()
        print()
        stage_window_enumeration()
        print()
        stage_render_store(work)
        print()
        stage_viewer_markup()
        print()
        stage_port_conflict()
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print()
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    for entry in FAILED:
        print("  -", entry)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
