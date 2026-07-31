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
