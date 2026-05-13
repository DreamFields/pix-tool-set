from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import PixToolError

REQUIRED_EXPORT_FILES = (
    "CMakeLists.txt",
    "CreatePSOs.cpp",
    "resources.bin",
    "RenderFrame.cpp",
)


@dataclass(frozen=True, slots=True)
class CppExportInfo:
    capture_path: Path | None
    export_dir: Path
    created: bool
    required_files: tuple[str, ...] = REQUIRED_EXPORT_FILES

    def to_dict(self) -> dict[str, Any]:
        return {
            "capture_path": str(self.capture_path) if self.capture_path else None,
            "export_dir": str(self.export_dir),
            "created": self.created,
            "required_files": list(self.required_files),
        }


def default_export_dir(capture_path: Path, workspace: Path) -> Path:
    """Default export directory: same path as PIX file, using PIX filename as folder name"""
    if capture_path is None:
        return workspace / "exports" / "capture"
    # Export directory is in the same path as the PIX file, using PIX filename (without extension) as folder name
    return capture_path.parent / capture_path.stem


def find_pixtool(explicit_path: str | None = None) -> Path | None:
    candidates: list[Path] = []
    if explicit_path:
        candidates.append(Path(explicit_path))
    env_path = os.environ.get("PIXTOOL_PATH")
    if env_path:
        candidates.append(Path(env_path))
    pix_root = Path("C:/Program Files/Microsoft PIX")
    if pix_root.exists():
        for version in sorted(pix_root.iterdir(), reverse=True):
            candidates.append(version / "pixtool.exe")
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry:
            candidates.append(Path(entry) / "pixtool.exe")

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def validate_cpp_export(export_dir: Path) -> list[str]:
    missing = [name for name in REQUIRED_EXPORT_FILES if not (export_dir / name).exists()]
    command_lists = list(export_dir.glob("CommandLists*.cpp"))
    if not command_lists:
        missing.append("CommandLists*.cpp")
    return missing


def ensure_cpp_export(args: dict[str, Any], workspace: Path) -> CppExportInfo:
    capture_raw = args.get("capture_path") or args.get("capture")
    export_raw = args.get("export_dir") or args.get("cpp_export_dir")

    capture_path = Path(capture_raw).expanduser().resolve() if capture_raw else None
    if capture_path is not None and not capture_path.exists():
        raise PixToolError(
            code="capture_not_found",
            message=f"Capture file does not exist: {capture_path}",
            stage="cpp_export_check",
            paths=[str(capture_path)],
            suggestion="Check the capture path or pass an existing export_dir.",
        )

    export_dir = Path(export_raw).expanduser().resolve() if export_raw else None
    if export_dir is None:
        if capture_path is None:
            raise PixToolError(
                code="cpp_export_input_missing",
                message="capture_path or export_dir is required.",
                stage="cpp_export_check",
                suggestion="Pass capture_path to auto-resolve the export directory, or pass export_dir directly.",
            )
        export_dir = default_export_dir(capture_path, workspace).resolve()

    missing = validate_cpp_export(export_dir) if export_dir.exists() else list(REQUIRED_EXPORT_FILES)
    if not missing:
        args["export_dir"] = str(export_dir)
        if capture_path is not None:
            args["capture_path"] = str(capture_path)
        return CppExportInfo(capture_path=capture_path, export_dir=export_dir, created=False)

    auto_export = bool(args.get("auto_export", False))
    if not auto_export:
        raise PixToolError(
            code="cpp_export_missing_or_incomplete",
            message=f"C++ export is missing or incomplete: {export_dir}",
            stage="cpp_export_check",
            paths=[str(export_dir)],
            suggestion="Create the PIX C++ export or pass auto_export=true with a valid capture_path.",
            details={"missing": missing},
        )

    if capture_path is None:
        raise PixToolError(
            code="capture_required_for_auto_export",
            message="capture_path is required when auto_export is true.",
            stage="cpp_export_check",
            suggestion="Pass capture_path together with auto_export=true.",
        )

    pixtool = find_pixtool(args.get("pixtool_path"))
    if pixtool is None:
        raise PixToolError(
            code="pixtool_not_found",
            message="pixtool.exe was not found.",
            stage="cpp_export",
            suggestion="Set PIXTOOL_PATH or pass pixtool_path.",
        )

    export_dir.mkdir(parents=True, exist_ok=True)
    cmd = [str(pixtool), "open-capture", str(capture_path), "export-to-cpp", str(export_dir), "--force"]
    try:
        completed = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800
        )
    except subprocess.TimeoutExpired as exc:
        raise PixToolError(
            code="cpp_export_timeout",
            message="PIX C++ export timed out after 30 minutes.",
            stage="cpp_export",
            paths=[str(capture_path), str(export_dir)],
            details={"command": cmd, "stdout": exc.stdout, "stderr": exc.stderr},
            suggestion="The capture file may be very large. Try exporting manually or using a smaller capture file.",
        ) from exc
    if completed.returncode != 0:
        raise PixToolError(
            code="cpp_export_failed",
            message="PIX C++ export failed.",
            stage="cpp_export",
            paths=[str(capture_path), str(export_dir)],
            details={"stdout": completed.stdout, "stderr": completed.stderr, "command": cmd},
        )

    missing_after = validate_cpp_export(export_dir)
    if missing_after:
        raise PixToolError(
            code="cpp_export_incomplete_after_export",
            message="PIX C++ export completed but required files are still missing.",
            stage="cpp_export_check",
            paths=[str(export_dir)],
            details={"missing": missing_after},
        )

    args["export_dir"] = str(export_dir)
    args["capture_path"] = str(capture_path)
    return CppExportInfo(capture_path=capture_path, export_dir=export_dir, created=True)
