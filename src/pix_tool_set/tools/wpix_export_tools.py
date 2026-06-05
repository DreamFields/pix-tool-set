from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

from pix_tool_set.context import ToolContext
from pix_tool_set.cpp_export import (
    CppExportInfo,
    default_export_dir,
    find_pixtool,
    validate_cpp_export,
)
from pix_tool_set.errors import PixToolError
from pix_tool_set.registry import tool
from pix_tool_set.results import ToolResult


@tool(
    name="export-to-cpp",
    description=(
        "Export a PIX .wpix capture file to a C++ project directory using pixtool.exe. "
        "This is a long-running synchronous operation that may take several minutes for large captures; "
        "callers should wait for completion instead of retrying or assuming the tool is stuck."
    ),
    parameters={
        "type": "object",
        "properties": {
            "capture_path": {
                "type": "string",
                "description": "Path to the PIX .wpix capture file.",
            },
            "export_dir": {
                "type": "string",
                "description": "Target directory for the C++ export. Defaults to exports/<capture-stem>/cpp_export under the workspace.",
            },
            "pixtool_path": {
                "type": "string",
                "description": "Optional explicit path to pixtool.exe. If omitted, auto-detected from PIXTOOL_PATH env, default install location, or system PATH.",
            },
            "force": {
                "type": "boolean",
                "description": "Re-export even if the target directory already exists and appears valid.",
            },
        },
        "required": ["capture_path"],
        "additionalProperties": False,
    },
    requires_cpp_export=False,
)
def export_to_cpp(args: dict[str, Any], context: ToolContext) -> ToolResult:
    start_time = time.monotonic()
    capture_path = Path(args["capture_path"]).expanduser().resolve()
    if not capture_path.exists():
        raise PixToolError(
            code="capture_not_found",
            message=f"Capture file does not exist: {capture_path}",
            stage="export_to_cpp",
            paths=[str(capture_path)],
            suggestion="Check the capture path and try again.",
        )

    if capture_path.suffix.lower() != ".wpix":
        raise PixToolError(
            code="capture_not_wpix",
            message=f"Capture file is not a .wpix file: {capture_path}",
            stage="export_to_cpp",
            paths=[str(capture_path)],
            suggestion="Provide a valid PIX .wpix capture file.",
        )

    export_dir = (
        Path(args["export_dir"]).expanduser().resolve()
        if args.get("export_dir")
        else default_export_dir(capture_path, context.workspace).resolve()
    )

    force = bool(args.get("force", False))

    # Check if export already exists and is valid
    if not force and export_dir.exists():
        missing = validate_cpp_export(export_dir)
        if not missing:
            return ToolResult.success(
                {
                    "capture_path": str(capture_path),
                    "export_dir": str(export_dir),
                    "created": False,
                    "duration_seconds": round(time.monotonic() - start_time, 3),
                    "skipped_reason": "C++ export already exists and is valid. Use force=true to re-export.",
                }
            )

    pixtool = find_pixtool(args.get("pixtool_path"))
    if pixtool is None:
        raise PixToolError(
            code="pixtool_not_found",
            message="pixtool.exe was not found.",
            stage="export_to_cpp",
            suggestion="Set PIXTOOL_PATH env variable, add pixtool to PATH, or pass pixtool_path explicitly.",
        )

    # Clean existing export dir when force is True
    if force and export_dir.exists():
        shutil.rmtree(export_dir, ignore_errors=True)

    export_dir.mkdir(parents=True, exist_ok=True)

    import subprocess

    cmd = [str(pixtool), "open-capture", str(capture_path), "export-to-cpp", str(export_dir), "--force"]
    try:
        completed = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800
        )
    except subprocess.TimeoutExpired as exc:
        raise PixToolError(
            code="cpp_export_timeout",
            message="PIX C++ export timed out after 30 minutes.",
            stage="export_to_cpp",
            paths=[str(capture_path), str(export_dir)],
            details={"command": cmd, "stdout": exc.stdout, "stderr": exc.stderr},
            suggestion="The capture file may be very large. Try exporting manually or using a smaller capture file.",
        ) from exc
    if completed.returncode != 0:
        raise PixToolError(
            code="cpp_export_failed",
            message="PIX C++ export failed.",
            stage="export_to_cpp",
            paths=[str(capture_path), str(export_dir)],
            details={"stdout": completed.stdout, "stderr": completed.stderr, "command": cmd},
            suggestion="Ensure the .wpix file is valid and pixtool supports this capture version.",
        )

    missing_after = validate_cpp_export(export_dir)
    if missing_after:
        raise PixToolError(
            code="cpp_export_incomplete",
            message="PIX C++ export completed but required files are still missing.",
            stage="export_to_cpp",
            paths=[str(export_dir)],
            details={"missing": missing_after},
            suggestion="The capture file may be corrupted or the PIX version may be incompatible.",
        )

    export_info = CppExportInfo(capture_path=capture_path, export_dir=export_dir, created=True)
    result = export_info.to_dict()
    result["duration_seconds"] = round(time.monotonic() - start_time, 3)
    return ToolResult.success(
        result,
        output_paths=[str(export_dir)],
    )
