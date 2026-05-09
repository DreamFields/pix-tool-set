"""Capture the replay window's pixels, so an edited shader's result can be looked at.

Why this exists: `shader-edit-apply --patch` changes the exported project, and rebuilding
it produces a real Win32 program that presents to a swapchain. Until now the only way to
see the outcome was to alt-tab to that window. Recording a PNG makes the result an
artifact - reviewable in the activity viewer, diffable against the unpatched run, and
usable from a headless agent that cannot look at a screen.

Two mechanisms, in order of preference:

  * ``PrintWindow`` with ``PW_RENDERFULLCONTENT``. Asks the window to render itself into
    a DC. This is the only reliable route for a D3D12 flip-model swapchain, whose front
    buffer is not part of the desktop composition that a screen BitBlt would read.
  * ``BitBlt`` from the screen, as a fallback for the rare driver where PrintWindow
    returns an empty bitmap.

Everything is ctypes against user32/gdi32; no third-party imaging dependency. The PNG
encoder here writes 24-bit RGB, unlike the greyscale one in the texture tools, because a
window capture is colour and the whole point is to see the colour change.
"""

from __future__ import annotations

import ctypes
import struct
import time
import zlib
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

SW_RESTORE = 9
SW_SHOW = 5
PW_RENDERFULLCONTENT = 0x00000002
SRCCOPY = 0x00CC0020
BI_RGB = 0
DIB_RGB_COLORS = 0


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


_ENUM_PROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HWND, ctypes.POINTER(ctypes.c_ulong)
)


@dataclass(frozen=True, slots=True)
class WindowInfo:
    hwnd: int
    title: str
    client_width: int
    client_height: int
    minimised: bool
    visible: bool

    def to_dict(self) -> dict:
        return {
            "hwnd": f"0x{self.hwnd:X}",
            "title": self.title,
            "client_width": self.client_width,
            "client_height": self.client_height,
            "minimised": self.minimised,
            "visible": self.visible,
        }


# ----------------------------------------------------------------------
def list_windows(pid: int) -> list[WindowInfo]:
    """Every top-level window owned by a process.

    Deliberately not ``Process.MainWindowHandle``: that can be 0 while the replayer's
    worker thread is busy loading gigabytes, and the process also owns invisible
    message-only windows that must not be mistaken for the real one.
    """
    found: list[WindowInfo] = []

    def callback(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        rect = RECT()
        user32.GetClientRect(hwnd, ctypes.byref(rect))
        found.append(
            WindowInfo(
                hwnd=int(hwnd),
                title=buffer.value,
                client_width=rect.right - rect.left,
                client_height=rect.bottom - rect.top,
                minimised=bool(user32.IsIconic(hwnd)),
                visible=bool(user32.IsWindowVisible(hwnd)),
            )
        )
        return True

    user32.EnumWindows(_ENUM_PROC(callback), None)
    return found


def pick_window(pid: int) -> WindowInfo | None:
    """The window most likely to be the replayer's own.

    A minimised window reports a 0x0 client area, so size alone would reject exactly the
    case that needs restoring. Minimised-but-visible therefore wins over a small visible
    one, and invisible message windows are only considered as a last resort.
    """
    windows = list_windows(pid)
    if not windows:
        return None
    visible = [w for w in windows if w.visible] or windows
    minimised = [w for w in visible if w.minimised]
    if minimised:
        return minimised[0]
    return max(visible, key=lambda w: w.client_width * w.client_height)


def restore_window(hwnd: int, *, width: int = 0, height: int = 0) -> None:
    """Un-minimise, and optionally resize so the capture has a usable client area."""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.7)
    else:
        user32.ShowWindow(hwnd, SW_SHOW)
    if width > 0 and height > 0:
        user32.MoveWindow(hwnd, 60, 60, int(width), int(height), True)
        time.sleep(0.4)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.5)


# ----------------------------------------------------------------------
def _grab(hwnd: int, use_print_window: bool) -> tuple[bytearray, int, int] | None:
    """One capture attempt. Returns tightly packed BGRA rows, top-down."""
    rect = RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None

    window_dc = user32.GetDC(hwnd) if use_print_window else user32.GetDC(None)
    if not window_dc:
        return None
    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    gdi32.SelectObject(memory_dc, bitmap)
    try:
        if use_print_window:
            ok = user32.PrintWindow(hwnd, memory_dc, PW_RENDERFULLCONTENT)
        else:
            # Screen coordinates of the client area, for the BitBlt fallback.
            origin = RECT(0, 0, 0, 0)
            user32.GetWindowRect(hwnd, ctypes.byref(origin))
            ok = gdi32.BitBlt(
                memory_dc, 0, 0, width, height, window_dc, origin.left, origin.top, SRCCOPY
            )
        if not ok:
            return None

        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        # Negative height requests a top-down DIB, matching PNG's row order.
        info.bmiHeader.biHeight = -height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = BI_RGB

        buffer = ctypes.create_string_buffer(width * height * 4)
        copied = gdi32.GetDIBits(
            memory_dc, bitmap, 0, height, buffer, ctypes.byref(info), DIB_RGB_COLORS
        )
        if copied == 0:
            return None
        return bytearray(buffer.raw), width, height
    finally:
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd if use_print_window else None, window_dc)


def _is_blank(pixels: bytearray) -> bool:
    """A capture that is one flat colour tells us nothing; treat it as a failure."""
    if len(pixels) < 16:
        return True
    first = pixels[0:3]
    step = max(len(pixels) // (4 * 400), 1) * 4
    for offset in range(0, len(pixels) - 3, step):
        if pixels[offset : offset + 3] != first:
            return False
    return True


def content_score(bgra: bytearray, width: int, height: int) -> float:
    """How much of the frame's interior differs from its most common colour.

    A window exists long before the replay presents anything, and until then it holds a
    flat background. Waiting on the window alone therefore captures a blank page, which
    would compare "identical" to another blank capture and read as a verdict about a
    patch - worse than useless.

    The interior is sampled with a 12% inset because the chrome that PrintWindow includes
    (title bar, borders, a dark strip at the client edge) is enough on its own to push a
    naive whole-image score past a small threshold. Measured on a real blank replay window
    that scored 0.049 while showing nothing at all.
    """
    total = width * height
    if total == 0 or width < 40 or height < 40:
        return 0.0
    inset_x = max(int(width * 0.12), 8)
    inset_y = max(int(height * 0.12), 8)
    x0, x1 = inset_x, width - inset_x
    y0, y1 = inset_y, height - inset_y
    if x1 <= x0 or y1 <= y0:
        return 0.0

    step_x = max((x1 - x0) // 140, 1)
    step_y = max((y1 - y0) // 140, 1)
    tally: dict[tuple[int, int, int], int] = {}
    sampled = 0
    for y in range(y0, y1, step_y):
        row = y * width
        for x in range(x0, x1, step_x):
            offset = (row + x) * 4
            if offset + 3 > len(bgra):
                continue
            key = (bgra[offset], bgra[offset + 1], bgra[offset + 2])
            tally[key] = tally.get(key, 0) + 1
            sampled += 1
    if sampled == 0:
        return 0.0
    dominant = max(tally.values())
    return 1.0 - (dominant / sampled)


# ----------------------------------------------------------------------
# Region analysis.
#
# "Blank" here means unreadable, and that takes two conditions, not one. A cell
# must be near enough to a single colour *and* that colour must carry no
# information - near black or near white, the two states a window is left in when
# nothing has been drawn into it. Flatness alone is not enough: the world-normal
# exports this was tested against are large flat fields of saturated green and
# blue, entirely flat per cell and entirely real content. Requiring both keeps
# them out, because calling a rendered frame blank is the expensive mistake: it
# would invent a reason to distrust a capture that is fine.
_FLAT_CELL_SHARE = 0.985
_GREY_SPREAD = 24
_DARK_LEVEL = 24
_BRIGHT_LEVEL = 232


def _cell_profile(
    bgra: bytearray, width: int, x0: int, y0: int, x1: int, y1: int
) -> tuple[bool, tuple[int, int, int]]:
    """Is this cell near enough to one colour, and which colour is it?"""
    step_x = max((x1 - x0) // 16, 1)
    step_y = max((y1 - y0) // 16, 1)
    tally: dict[tuple[int, int, int], int] = {}
    sampled = 0
    for y in range(y0, y1, step_y):
        row = y * width
        for x in range(x0, x1, step_x):
            offset = (row + x) * 4
            if offset + 3 > len(bgra):
                continue
            key = (bgra[offset], bgra[offset + 1], bgra[offset + 2])
            tally[key] = tally.get(key, 0) + 1
            sampled += 1
    if sampled == 0:
        return True, (0, 0, 0)
    colour, count = max(tally.items(), key=lambda item: item[1])
    return (count / sampled) >= _FLAT_CELL_SHARE, colour


def _carries_nothing(colour: tuple[int, int, int]) -> bool:
    """Is this the colour of a surface nothing was drawn onto?

    Near black or near white, and unsaturated. A cleared viewport and an unpainted
    Win32 client area are both one of those two; a rendered surface, even a flat one,
    generally is not.
    """
    high, low = max(colour), min(colour)
    if high - low >= _GREY_SPREAD:
        return False
    return high <= _DARK_LEVEL or low >= _BRIGHT_LEVEL


def _largest_rect(
    mask: list[bool], columns: int, rows: int, wanted: bool
) -> tuple[int, int, int, int, int]:
    """Largest axis-aligned run of cells all equal to ``wanted``.

    The maximal-rectangle histogram scan, over a grid small enough that its cost is
    irrelevant. A rectangle rather than a connected blob because the question being
    answered is "is there a big empty area", and an L-shaped blob around a corner panel
    would answer it with a number that overstates how empty any one place is.
    """
    best = (0, 0, 0, 0, 0)  # area, x, y, width, height, in cells
    heights = [0] * columns
    for y in range(rows):
        for x in range(columns):
            heights[x] = heights[x] + 1 if mask[y * columns + x] == wanted else 0
        stack: list[tuple[int, int]] = []
        for x in range(columns + 1):
            h = heights[x] if x < columns else 0
            start = x
            while stack and stack[-1][1] >= h:
                left, height = stack.pop()
                area = height * (x - left)
                if area > best[0]:
                    best = (area, left, y - height + 1, x - left, height)
                start = left
            stack.append((start, h))
    return best


def viewport_blankness(bgra: bytearray, width: int, height: int, *, grid: int = 12) -> dict:
    """Where the content is, as a grid of blank / not-blank cells.

    ``content_score`` answers "is anything on screen at all", over the whole inset frame.
    That becomes the wrong question as soon as a window can show a UI panel over an
    unrendered viewport. Measured on a real replay of the Tiled capture: a 600x400 Slate
    panel in the corner of a 1280x720 window scored 0.2803 while the 3D viewport was
    solid black. That passed the 0.02 gate, so the capture was treated as a rendered
    frame, and the whole-window diff that followed - which could only ever have described
    the panel - read as a verdict about a shader patch.

    This reports *where* the content is instead of how much of it there is, so the three
    cases can be told apart: nothing rendered, something rendered but only in one part of
    the window, and a frame with content across it.

    Independent of ``content_score`` on purpose. That function's 12% inset and threshold
    are calibrated against a measured blank window, and nothing here changes them.
    """
    total_pixels = width * height
    if total_pixels == 0 or width < 40 or height < 40:
        return {
            "verdict": "too_small",
            "main_area_blank": False,
            "note": "the frame is too small to divide into regions, so its layout is unknown",
        }

    columns = max(1, min(grid, width // 8))
    rows = max(1, min(grid, height // 8))
    edges_x = [(index * width) // columns for index in range(columns + 1)]
    edges_y = [(index * height) // rows for index in range(rows + 1)]

    blank: list[bool] = []
    colours: list[tuple[int, int, int]] = []
    flat_cells = 0
    near_black = 0
    for row in range(rows):
        for column in range(columns):
            is_flat, colour = _cell_profile(
                bgra, width,
                edges_x[column], edges_y[row], edges_x[column + 1], edges_y[row + 1],
            )
            colours.append(colour)
            if is_flat:
                flat_cells += 1
            empty = is_flat and _carries_nothing(colour)
            blank.append(empty)
            if empty and max(colour) <= _DARK_LEVEL:
                near_black += 1

    cells = columns * rows
    blank_cells = sum(1 for flag in blank if flag)
    blank_share = blank_cells / cells

    # One flat colour over the whole window, whatever that colour is. This is the
    # un-presented window, and it is worth catching separately because a mid grey
    # would not read as "carries nothing" on its own.
    first = colours[0]
    uniform = flat_cells == cells and all(
        max(abs(c[i] - first[i]) for i in range(3)) <= 8 for c in colours
    )

    # The middle of the window, grid-aligned. This is where a 3D viewport lives when a
    # tool panel is docked over a corner, and testing it is what stops a flat sky - blank
    # cells, but at the top - from being mistaken for an empty viewport.
    cx0, cx1 = columns // 4, columns - columns // 4
    cy0, cy1 = rows // 4, rows - rows // 4
    centre = [
        blank[row * columns + column]
        for row in range(cy0, max(cy1, cy0 + 1))
        for column in range(cx0, max(cx1, cx0 + 1))
    ]
    centre_blank_share = (sum(1 for flag in centre if flag) / len(centre)) if centre else 0.0

    def as_rect(found: tuple[int, int, int, int, int]) -> dict | None:
        area, x, y, cell_w, cell_h = found
        if area == 0:
            return None
        x0, y0 = edges_x[x], edges_y[y]
        x1, y1 = edges_x[x + cell_w], edges_y[y + cell_h]
        return {
            "x": x0,
            "y": y0,
            "width": x1 - x0,
            "height": y1 - y0,
            "share_of_frame": round((x1 - x0) * (y1 - y0) / total_pixels, 4),
        }

    blank_rect = as_rect(_largest_rect(blank, columns, rows, True))
    content_rect = as_rect(_largest_rect(blank, columns, rows, False))
    blank_rect_share = blank_rect["share_of_frame"] if blank_rect else 0.0

    # A frame is only called part-empty when a large contiguous area carries nothing
    # *and* the middle is one of those areas. Requiring both is what keeps normally
    # rendered frames out of this bucket - measured at blank_cell_share 0.0 on two
    # full-frame world-normal exports, against 0.67 for the Slate panel capture.
    if uniform or blank_share >= 0.98:
        verdict = "frame_blank"
    elif blank_share >= 0.45 and blank_rect_share >= 0.25 and centre_blank_share >= 0.5:
        verdict = "content_confined_to_part_of_frame"
    else:
        verdict = "content_across_frame"

    if verdict == "frame_blank":
        note = (
            "every cell is one flat colour, so nothing was rendered and no measurement "
            "taken from this frame describes a scene"
        )
    elif verdict == "content_confined_to_part_of_frame":
        shape = (
            f"{content_rect['width']}x{content_rect['height']} at "
            f"({content_rect['x']},{content_rect['y']})"
            if content_rect
            else "a small area"
        )
        note = (
            f"content is confined to {shape}; {round(blank_rect_share * 100)}% of the "
            f"frame is one blank rectangle and {round(centre_blank_share * 100)}% of the "
            "middle carries nothing, so a whole-frame measurement describes that content "
            "and not the scene"
        )
    else:
        note = (
            "content is spread across the frame, so a whole-frame measurement describes "
            "what was rendered"
        )

    return {
        "grid": f"{columns}x{rows}",
        "cells": cells,
        "flat_cell_share": round(flat_cells / cells, 4),
        "blank_cells": blank_cells,
        "blank_cell_share": round(blank_share, 4),
        "near_black_cell_share": round(near_black / cells, 4),
        "centre_blank_share": round(centre_blank_share, 4),
        "largest_blank_rect": blank_rect,
        "largest_content_rect": content_rect,
        "main_area_blank": verdict in ("frame_blank", "content_confined_to_part_of_frame"),
        "verdict": verdict,
        "note": note,
    }


def capture_window(hwnd: int) -> tuple[bytearray, int, int, str] | None:
    """Grab a window's client area as BGRA rows, reporting which method worked."""
    for use_print_window, label in ((True, "PrintWindow(PW_RENDERFULLCONTENT)"),
                                    (False, "BitBlt from screen")):
        grabbed = _grab(hwnd, use_print_window)
        if grabbed is None:
            continue
        pixels, width, height = grabbed
        if _is_blank(pixels):
            continue
        return pixels, width, height, label
    return None


# ----------------------------------------------------------------------
def encode_png_rgb(bgra: bytearray, width: int, height: int) -> bytes:
    """24-bit RGB PNG from top-down BGRA rows. Hand-rolled to stay dependency-free."""
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0, no prediction
        row = bgra[y * width * 4 : (y + 1) * width * 4]
        for x in range(width):
            b, g, r = row[x * 4], row[x * 4 + 1], row[x * 4 + 2]
            raw.append(r)
            raw.append(g)
            raw.append(b)

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return (
            struct.pack(">I", len(payload))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    header = struct.pack(">2I5B", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


def colour_summary(bgra: bytearray, width: int, height: int) -> dict:
    """Mean channels plus hue tallies, so a change can be stated numerically.

    A screenshot alone does not prove anything to a caller that cannot see it, and it
    does not survive being summarised. Numbers do both.
    """
    step = max((width * height) // 40000, 1)
    count = 0
    r_sum = g_sum = b_sum = 0
    tally = {"magenta": 0, "red": 0, "green": 0, "blue": 0, "grey": 0}
    for index in range(0, width * height, step):
        offset = index * 4
        if offset + 3 > len(bgra):
            break
        b, g, r = bgra[offset], bgra[offset + 1], bgra[offset + 2]
        r_sum += r
        g_sum += g
        b_sum += b
        count += 1
        spread = max(r, g, b) - min(r, g, b)
        if spread < 24:
            tally["grey"] += 1
        elif r > 110 and b > 110 and g < r - 50 and g < b - 50:
            tally["magenta"] += 1
        elif r > g + 40 and r > b + 40:
            tally["red"] += 1
        elif g > r + 40 and g > b + 40:
            tally["green"] += 1
        elif b > r + 40 and b > g + 40:
            tally["blue"] += 1
    if count == 0:
        return {"sampled": 0}
    return {
        "sampled": count,
        "mean_rgb": [round(r_sum / count, 1), round(g_sum / count, 1), round(b_sum / count, 1)],
        "hue_share_percent": {
            key: round(100.0 * value / count, 1) for key, value in tally.items()
        },
    }


def compare(a: dict, b: dict) -> dict:
    """How far apart two colour summaries are, in terms a caller can act on."""
    if not a.get("sampled") or not b.get("sampled"):
        return {"comparable": False}
    mean_a, mean_b = a["mean_rgb"], b["mean_rgb"]
    delta = [round(mean_b[i] - mean_a[i], 1) for i in range(3)]
    largest = max(abs(value) for value in delta)
    shifts = {
        key: round(b["hue_share_percent"].get(key, 0.0) - value, 1)
        for key, value in a["hue_share_percent"].items()
    }
    return {
        "comparable": True,
        "mean_rgb_delta": delta,
        "largest_channel_delta": largest,
        "hue_share_delta_percent": shifts,
        # A few levels of drift is dithering or a UI blink, not a shader change.
        "visibly_different": largest >= 8.0 or any(abs(v) >= 3.0 for v in shifts.values()),
    }


def write_png(path: Path, bgra: bytearray, width: int, height: int) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = encode_png_rgb(bgra, width, height)
    path.write_bytes(blob)
    return len(blob)
