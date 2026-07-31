"""Build and run the exported replay project, then record what it renders.

This closes the loop opened by shader-edit-apply. Patching the export changes what the
replay will do; this tool makes the outcome observable without a human alt-tabbing to a
window, which is what an agent needs and what a summarised transcript can carry.

Design notes worth stating:

  * The build is delegated to CMake, not reimplemented. Two environment traps are handled
    explicitly because both were hit in practice: CMake's SDK download can fail on SSL and
    leave a 0-byte .nupkg that only surfaces later as "cannot open d3d12.h", and a build
    directory configured by a different generator must be wiped before reconfiguring.
  * The replayer loads gigabytes before its first present, so the wait is polled against
    the window's own state rather than a fixed sleep.
  * The capture is written into the activity log's own directory, so the viewer can serve
    it next to the call that produced it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

from ..context import ToolContext
from ..engine import activity, screencap
from ..errors import PixToolError, invalid_argument, not_found
from ..results import ToolResult
from ._common import tool, with_session

_NOTE = (
    "Builds the exported C++ replay project with CMake, runs it, and captures the window "
    "it presents to. This is how an edited shader's effect becomes visible: patch with "
    "shader-edit-apply, then run this. The capture is stored with the activity log so "
    "activity-viewer can show it. Requires CMake and a Visual Studio toolchain. Note that "
    "a patched pass only changes the picture if its output actually reaches the "
    "backbuffer - see the README section on the present path."
)

_NUPKGS = {
    "D3D12AgilitySdk.nupkg": "https://www.nuget.org/api/v2/package/Microsoft.Direct3D.D3D12",
    "WinPixEventRuntime.nupkg": "https://www.nuget.org/api/v2/package/WinPixEventRuntime",
}


def _export_root(context: ToolContext, args: dict[str, Any]) -> Path:
    capture = context.capture(args)
    root = Path(capture.export_dir)
    if not (root / "CMakeLists.txt").exists():
        raise not_found(
            "CMakeLists.txt",
            str(root),
            "This export has no C++ project; re-run session-open so pixtool writes one.",
        )
    return root


def _repair_nupkgs(root: Path) -> list[dict[str, Any]]:
    """Replace the 0-byte packages CMake leaves behind when its download fails.

    CMake treats "file exists" as success, so an SSL failure during configure is only
    discovered much later as a missing d3d12.h. Fixing it here turns a confusing compile
    error into a handled step.
    """
    repaired: list[dict[str, Any]] = []
    for name, url in _NUPKGS.items():
        target = root / name
        if target.exists() and target.stat().st_size > 1024:
            continue
        note: dict[str, Any] = {"package": name, "was": "missing"}
        if target.exists():
            note["was"] = f"{target.stat().st_size} bytes (truncated by a failed download)"
            target.unlink()
        # A stale extraction directory would shadow the fresh package.
        for stale in (root / name.replace(".nupkg", ".zip"),):
            if stale.exists():
                stale.unlink()
        try:
            with urllib.request.urlopen(url, timeout=180) as response:
                blob = response.read()
            target.write_bytes(blob)
            note["now"] = f"{len(blob)} bytes"
            note["source"] = url
            repaired.append(note)
        except Exception as exc:  # noqa: BLE001
            note["error"] = f"{type(exc).__name__}: {exc}"
            repaired.append(note)
    return repaired


def _run(command: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
    proc = subprocess.run(
        command, cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _configure_and_build(
    root: Path, generator: str, timeout: int, force: bool
) -> dict[str, Any]:
    build_dir = root / "build"
    steps: dict[str, Any] = {"build_dir": str(build_dir)}

    repaired = _repair_nupkgs(root)
    if repaired:
        steps["dependencies_repaired"] = repaired
        # The extracted SDK is derived from the package, so it must be redone.
        for folder in ("AgilitySDK", "WinPixEventRuntime"):
            shutil.rmtree(root / folder, ignore_errors=True)
        force = True

    if force and build_dir.exists():
        shutil.rmtree(build_dir, ignore_errors=True)
        steps["build_dir_reset"] = True

    cached = build_dir / "CMakeCache.txt"
    if not cached.exists():
        code, log = _run(
            ["cmake", "-S", str(root), "-B", str(build_dir), "-G", generator, "-A", "x64"],
            root, timeout,
        )
        steps["configure"] = {"exit_code": code, "generator": generator}
        if code != 0:
            if "Does not match the generator used previously" in log:
                # Recover rather than making the caller work it out.
                shutil.rmtree(build_dir, ignore_errors=True)
                code, log = _run(
                    ["cmake", "-S", str(root), "-B", str(build_dir), "-G", generator,
                     "-A", "x64"],
                    root, timeout,
                )
                steps["configure"] = {
                    "exit_code": code,
                    "generator": generator,
                    "recovered_from": "a build directory configured by another generator",
                }
            if code != 0:
                raise PixToolError(
                    code="cmake_configure_failed",
                    message="CMake could not configure the exported project.",
                    stage="build",
                    suggestion="Check that CMake and a Visual Studio toolchain are installed.",
                    details={"log_tail": log[-2500:]},
                )
    else:
        steps["configure"] = {"skipped": "already configured"}

    started = time.time()
    code, log = _run(
        ["cmake", "--build", str(build_dir), "--config", "Release", "--parallel"],
        root, timeout,
    )
    steps["build"] = {"exit_code": code, "seconds": round(time.time() - started, 1)}
    if code != 0:
        raise PixToolError(
            code="cmake_build_failed",
            message="The exported project did not build.",
            stage="build",
            suggestion=(
                "If the error mentions d3d12.h, the Agility SDK package is missing; "
                "re-run with --force-reconfigure to fetch it again."
            ),
            details={"log_tail": log[-2500:]},
        )

    executables = sorted(
        (build_dir / "Release").glob("*.exe"),
        key=lambda p: -p.stat().st_size,
    )
    if not executables:
        raise not_found("built executable", str(build_dir / "Release"))
    steps["executable"] = str(executables[0])
    return steps


def _await_window(pid: int, deadline: float, min_pixels: int) -> screencap.WindowInfo | None:
    """Poll for a window with a usable client area, restoring it if minimised."""
    while time.time() < deadline:
        window = screencap.pick_window(pid)
        if window is not None:
            if window.minimised or window.client_width * window.client_height < min_pixels:
                screencap.restore_window(window.hwnd, width=1300, height=780)
                window = screencap.pick_window(pid)
            if window and window.client_width * window.client_height >= min_pixels:
                return window
        time.sleep(2.0)
    return None


def _await_content(
    hwnd: int, deadline: float, min_score: float
) -> tuple[bytearray, int, int, str, dict[str, Any]] | None:
    """Wait until the window shows a rendered frame, then until it stops changing.

    Two waits, because the failure modes differ. First the window exists but is a flat
    background - capturing then yields a blank page that would compare "identical" to any
    other blank page, which is worse than useless because it looks like a verdict.
    Second, the replay presents progressively, so an early grab can catch a partial frame.
    """
    history: list[float] = []
    stable_since: float | None = None
    best: tuple[bytearray, int, int, str] | None = None
    polls = 0

    while time.time() < deadline:
        polls += 1
        grabbed = screencap.capture_window(hwnd)
        if grabbed is not None:
            pixels, width, height, method = grabbed
            score = screencap.content_score(pixels, width, height)
            history.append(round(score, 4))
            if score >= min_score:
                if best is not None and abs(
                    screencap.content_score(best[0], best[1], best[2]) - score
                ) < 0.005:
                    # Two consecutive polls agree, so the frame has settled.
                    if stable_since is None:
                        stable_since = time.time()
                    if time.time() - stable_since >= 3.0:
                        return pixels, width, height, method, {
                            "content_score": round(score, 4),
                            "polls": polls,
                            "score_history": history[-12:],
                            "settled": True,
                        }
                else:
                    stable_since = None
                best = (pixels, width, height, method)
        time.sleep(3.0)

    if best is not None:
        return best[0], best[1], best[2], best[3], {
            "content_score": round(screencap.content_score(best[0], best[1], best[2]), 4),
            "polls": polls,
            "score_history": history[-12:],
            "settled": False,
        }
    return None


# ======================================================================
@tool(
    name="replay-render",
    summary=(
        "Build and run the exported replay project, then capture the frame it presents as "
        "a PNG so an edited shader's result can be seen."
    ),
    category="meta",
    parameters=with_session(
        output={
            "type": "string",
            "description": (
                "Where to write the PNG. Defaults to the activity log directory so the "
                "viewer can display it."
            ),
        },
        label={
            "type": "string",
            "description": "Short tag for this capture, e.g. 'baseline' or 'magenta-slate'.",
        },
        settle_seconds={
            "type": "integer",
            "description": "How long to let the replay run before capturing. Default 150.",
        },
        build_timeout={
            "type": "integer",
            "description": "Seconds allowed for configure and for build. Default 1800.",
        },
        generator={
            "type": "string",
            "description": "CMake generator. Default 'Visual Studio 18 2026'.",
        },
        force_reconfigure={
            "type": "boolean",
            "description": "Wipe the build directory first and reconfigure from scratch.",
        },
        skip_build={
            "type": "boolean",
            "description": "Run the existing executable without rebuilding.",
        },
        keep_running={
            "type": "boolean",
            "description": "Leave the replayer running after the capture.",
        },
        compare_to={
            "type": "string",
            "description": (
                "A previous capture's record id or PNG path. The colour summaries are "
                "diffed so a change can be stated numerically, not just looked at."
            ),
        },
    ),
    returns="Path of the captured PNG, its colour summary, and the build/run details.",
    examples=[
        "pix-tool-set replay-render --label baseline",
        "pix-tool-set replay-render --label after-edit --compare-to baseline",
        "pix-tool-set replay-render --skip-build --settle-seconds 60",
    ],
    notes=_NOTE,
)
def replay_render(args: dict[str, Any], context: ToolContext) -> ToolResult:
    root = _export_root(context, args)
    label = str(args.get("label") or "render").strip().replace(" ", "-")
    if not all(ch.isalnum() or ch in "-_" for ch in label):
        raise invalid_argument("label", "use letters, digits, dashes or underscores only")

    settle = int(args.get("settle_seconds") or 150)
    timeout = int(args.get("build_timeout") or 1800)
    generator = str(args.get("generator") or "Visual Studio 18 2026")

    data: dict[str, Any] = {"export_dir": str(root), "label": label}
    diagnostics: list[tuple[str, str]] = []

    if args.get("skip_build"):
        executables = sorted(
            (root / "build" / "Release").glob("*.exe"), key=lambda p: -p.stat().st_size
        )
        if not executables:
            raise not_found(
                "built executable",
                str(root / "build" / "Release"),
                "Nothing to run; drop --skip-build so the project gets built.",
            )
        data["build"] = {"skipped": True, "executable": str(executables[0])}
        exe = executables[0]
    else:
        steps = _configure_and_build(
            root, generator, timeout, bool(args.get("force_reconfigure"))
        )
        data["build"] = steps
        exe = Path(steps["executable"])

    # The working directory must be the export root: resources.bin and any
    # edited_*.dxil are resolved relative to it.
    process = subprocess.Popen([str(exe)], cwd=str(root))
    data["run"] = {"pid": process.pid, "working_directory": str(root)}

    try:
        deadline = time.time() + settle
        window = _await_window(process.pid, deadline, min_pixels=200 * 200)
        if window is None:
            raise PixToolError(
                code="replay_window_unavailable",
                message="The replay started but never produced a window with pixels to read.",
                stage="capture",
                suggestion=(
                    "Raise --settle-seconds; a multi-gigabyte capture can take minutes "
                    "before its first present."
                ),
                details={"pid": process.pid,
                         "windows": [w.to_dict() for w in screencap.list_windows(process.pid)]},
            )
        data["window"] = window.to_dict()

        # Wait for actual rendered content, not merely a window. Capturing a flat
        # background would compare "identical" to any other blank capture and read as a
        # verdict about the patch, which would be wrong.
        awaited = _await_content(window.hwnd, deadline, min_score=0.02)
        if awaited is None:
            raise PixToolError(
                code="window_capture_failed",
                message="Neither PrintWindow nor a screen BitBlt returned usable pixels.",
                stage="capture",
                suggestion="Make sure the window is not fully occluded or on a blanked display.",
                details={"window": window.to_dict()},
            )
        pixels, width, height, method, wait_info = awaited
        data["wait"] = wait_info

        blank = wait_info["content_score"] < 0.02
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = f"replay_{label}_{stamp}_{width}x{height}.png"
        target = (
            Path(str(args["output"])) / name
            if args.get("output")
            else activity.renders_dir() / name
        )
        written = screencap.write_png(target, pixels, width, height)

        summary = screencap.colour_summary(pixels, width, height)
        data["capture"] = {
            "path": str(target),
            "bytes": written,
            "width": width,
            "height": height,
            "method": method,
            "colour": summary,
            "shows_rendered_frame": not blank,
        }

        if blank:
            diagnostics.append((
                "warning",
                "The window never showed a rendered frame within the settle window, so "
                "this capture is a blank page and says nothing about the patch. Raise "
                "--settle-seconds and try again.",
            ))
        elif not wait_info.get("settled"):
            diagnostics.append((
                "warning",
                "The frame was still changing when the settle window expired, so this "
                "capture may be a partially replayed frame.",
            ))

        if args.get("compare_to"):
            if blank:
                data["comparison"] = {
                    "comparable": False,
                    "reason": "the current capture is blank, so a comparison would be meaningless",
                }
            else:
                data["comparison"] = _compare_with(str(args["compare_to"]), summary)
                if data["comparison"].get("comparable") and not data["comparison"].get(
                    "visibly_different"
                ):
                    diagnostics.append((
                        "warning",
                        "This render is not measurably different from the one compared "
                        "against. Either the patch did not take effect, or the patched "
                        "pass does not reach the backbuffer in this frame.",
                    ))
    finally:
        if not args.get("keep_running"):
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
            data["run"]["stopped"] = True
        else:
            data["run"]["stopped"] = False

    degraded = any(level == "warning" for level, _ in diagnostics)
    paths = [data["capture"]["path"]]
    result = (
        ToolResult.partial(data, output_paths=paths)
        if degraded
        else ToolResult.success(data, output_paths=paths)
    )
    for level, message in diagnostics:
        result.add_diagnostic(level, message)
    result.add_diagnostic(
        "info",
        "The capture shows whatever the replay presents to its swapchain. A patched pass "
        "whose output never reaches the backbuffer will render identically - that is a "
        "property of the frame, not a failed patch.",
    )
    return result


def _compare_with(reference: str, current: dict[str, Any]) -> dict[str, Any]:
    """Resolve a reference capture by label, record id or path, then diff the colours."""
    previous: dict[str, Any] | None = None
    source = ""
    reference_render = ""

    candidate = Path(reference)
    if candidate.suffix.lower() == ".png" and candidate.exists():
        return {
            "comparable": False,
            "reason": (
                "a PNG on disk carries no stored colour summary; compare against a label "
                "or a record id from activity-log instead"
            ),
            "reference": str(candidate),
        }

    # Search the activity log backwards for a matching replay-render call.
    for entry in reversed(activity.read_all()):
        if entry.get("tool") != "replay-render":
            continue
        if entry.get("id") == reference or (entry.get("args") or {}).get("label") == reference:
            envelope = activity.read_payload(entry["id"])
            shot = ((envelope or {}).get("data") or {}).get("capture", {})
            if shot.get("shows_rendered_frame") is False:
                # A blank baseline is not a reference; comparing against it would call any
                # blank capture "identical" and read as a verdict about the patch.
                continue
            found = shot.get("colour")
            if found:
                previous = found
                source = f"activity record {entry['id']}"
                reference_render = (entry.get("render") or {}).get("name") or ""
                break

    if previous is None:
        return {
            "comparable": False,
            "reason": f"no earlier replay-render capture matches {reference!r}",
            "hint": "Run activity-log --tool-name replay-render to see what is available.",
        }

    outcome = screencap.compare(previous, current)
    outcome["reference"] = source
    outcome["reference_colour"] = previous
    if reference_render:
        outcome["reference_render"] = reference_render
    return outcome
