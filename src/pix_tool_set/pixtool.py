"""Locating and driving ``pixtool.exe``.

Everything that needs to touch the real PIX installation goes through here, so
the rest of the toolkit stays testable and the failure messages stay uniform.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .errors import PixToolError, pixtool_missing

PIX_ROOTS = (
    r"C:\Program Files\Microsoft PIX",
    r"C:\Program Files (x86)\Microsoft PIX",
)

REQUIRED_EXPORT_FILES = (
    "CMakeLists.txt",
    "CreatePSOs.cpp",
    "resources.bin",
)


def find_pix_install(explicit: str | Path | None = None) -> Path | None:
    """Newest PIX install directory, or None."""
    if explicit:
        candidate = Path(explicit)
        if candidate.name.lower() == "pixtool.exe" and candidate.exists():
            return candidate.parent
        if (candidate / "pixtool.exe").exists():
            return candidate

    env = os.environ.get("PIXTOOL_PATH")
    if env:
        candidate = Path(env)
        if candidate.name.lower() == "pixtool.exe" and candidate.exists():
            return candidate.parent
        if (candidate / "pixtool.exe").exists():
            return candidate

    best: tuple[tuple[int, ...], Path] | None = None
    for root in PIX_ROOTS:
        root_path = Path(root)
        if not root_path.is_dir():
            continue
        for child in root_path.iterdir():
            if not (child / "pixtool.exe").exists():
                continue
            key = tuple(int(x) for x in re.findall(r"\d+", child.name)) or (0,)
            if best is None or key > best[0]:
                best = (key, child)
    if best is not None:
        return best[1]

    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry and (Path(entry) / "pixtool.exe").exists():
            return Path(entry)
    return None


def find_pixtool(explicit: str | Path | None = None) -> Path:
    install = find_pix_install(explicit)
    if install is None:
        raise pixtool_missing()
    return install / "pixtool.exe"


def validate_export(export_dir: Path) -> list[str]:
    missing = [name for name in REQUIRED_EXPORT_FILES if not (export_dir / name).exists()]
    if not list(export_dir.glob("CommandLists*.cpp")):
        missing.append("CommandLists*.cpp")
    return missing


@dataclass(slots=True)
class PixTool:
    exe: Path
    verbosity: str = "trace"

    @classmethod
    def locate(cls, explicit: str | Path | None = None) -> "PixTool":
        return cls(exe=find_pixtool(explicit))

    @property
    def install_dir(self) -> Path:
        return self.exe.parent

    def _run(
        self,
        args: Sequence[str],
        log_path: Path | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess:
        command = [str(self.exe), f"--output={self.verbosity}", *args]
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise PixToolError(
                code="pixtool_timeout",
                message=f"pixtool timed out after {timeout}s",
                stage="pixtool",
                suggestion="Raise the timeout, or export once with `session-open` and reuse the cache.",
            ) from exc
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(
                f"$ {' '.join(command)}\n\n{proc.stdout}\n{proc.stderr}",
                encoding="utf-8",
                errors="replace",
            )
        return proc

    # ------------------------------------------------------------------
    def export_to_cpp(
        self,
        capture: Path,
        out_dir: Path,
        *,
        force: bool = True,
        timeout: float | None = 10800,
        log_path: Path | None = None,
    ) -> Path:
        args = ["open-capture", str(capture), "export-to-cpp", str(out_dir)]
        if force:
            args.append("--force")
        args += ["--use-winpixeventruntime", "--use-agilitySdk"]
        proc = self._run(args, log_path, timeout)
        if proc.returncode != 0 or not out_dir.exists():
            raise PixToolError(
                code="export_failed",
                message=f"pixtool export-to-cpp failed (exit {proc.returncode}).",
                stage="export",
                paths=[str(capture), str(out_dir)],
                suggestion="Confirm the capture replays in the PIX UI on this GPU/driver.",
                details={"stdout_tail": proc.stdout[-2000:]},
            )
        return out_dir

    def save_event_list(
        self,
        capture: Path,
        out_csv: Path,
        *,
        counters: Sequence[str] = (),
        counter_groups: Sequence[str] = (),
        timeout: float | None = 3600,
        log_path: Path | None = None,
    ) -> Path:
        out_csv.parent.mkdir(parents=True, exist_ok=True)
        args = ["open-capture", str(capture), "save-event-list", str(out_csv)]
        for pattern in counters:
            args.append(f"--counters={pattern}")
        for pattern in counter_groups:
            args.append(f"--counter-groups={pattern}")
        proc = self._run(args, log_path, timeout)
        if proc.returncode != 0 or not out_csv.exists():
            raise PixToolError(
                code="event_list_failed",
                message=f"pixtool save-event-list failed (exit {proc.returncode}).",
                stage="export",
                paths=[str(capture)],
                details={"stdout_tail": proc.stdout[-2000:]},
            )
        return out_csv

    def save_screenshot(self, capture: Path, out_png: Path, timeout: float | None = 1800) -> Path:
        out_png.parent.mkdir(parents=True, exist_ok=True)
        proc = self._run(
            ["open-capture", str(capture), "save-screenshot", str(out_png)], None, timeout
        )
        if proc.returncode != 0 or not out_png.exists():
            raise PixToolError(
                code="screenshot_failed",
                message=f"pixtool save-screenshot failed (exit {proc.returncode}).",
                stage="export",
                details={"stdout_tail": proc.stdout[-1500:]},
            )
        return out_png

    def save_resource(
        self,
        capture: Path,
        out_path: Path,
        *,
        global_id: int | None = None,
        marker: str | None = None,
        rtv: int | None = None,
        depth: bool = False,
        timeout: float | None = 1800,
    ) -> Path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        args = ["open-capture", str(capture), "save-resource", str(out_path)]
        if global_id is not None:
            args.append(f"--global-id={global_id}")
        if marker is not None:
            args.append(f"--marker={marker}")
        if depth:
            args.append("--depth")
        elif rtv is not None:
            args.append(f"--rtv={rtv}")
        proc = self._run(args, None, timeout)
        if proc.returncode != 0 or not out_path.exists():
            raise PixToolError(
                code="save_resource_failed",
                message=f"pixtool save-resource failed (exit {proc.returncode}).",
                stage="export",
                suggestion="Confirm the event id has that render target bound.",
                details={"stdout_tail": proc.stdout[-1500:]},
            )
        return out_path

    def list_counters(self, capture: Path, timeout: float | None = 1800) -> list[str]:
        proc = self._run(["open-capture", str(capture), "list-counters"], None, timeout)
        return [
            line.strip()
            for line in proc.stdout.splitlines()
            if line.strip() and not line.startswith(("$", "Usage", " "))
        ]
