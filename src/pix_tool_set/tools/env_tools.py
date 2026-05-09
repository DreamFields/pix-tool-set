"""Environment self-check: one call that reports every missing dependency.

Why a tool and not a README paragraph
-------------------------------------
Setting up a new machine used to mean running a real analysis and reading whatever
failed first. That is a bad loop: the failures arrive one at a time, and the two
worst ones point away from their cause (a missing Agility SDK shows up as "cannot
open include file: d3d12.h", a missing Visual Studio 2026 shows up as a CMake
generator error). This tool asks every question up front and answers them all in
one envelope, so a fresh checkout can be validated before any capture is opened.

It needs no session, touches no capture, and installs nothing. See
``engine/envcheck.py`` for what each probe actually does.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..context import ToolContext
from ..engine import envcheck
from ..results import ToolResult
from ._common import object_schema, tool

_NOTE = (
    "Read-only: nothing is installed, downloaded or configured. Two tiers are reported "
    "separately because their requirements differ - 'core' is what reading a .wpix needs "
    "(Windows x64, Python 3.11+, a Microsoft PIX install for pixtool.exe and "
    "dxcompiler.dll), 'replay' is what rebuilding the exported C++ project additionally "
    "needs (CMake, a Visual Studio C++ toolset, a Windows SDK, a D3D12 GPU, and the D3D12 "
    "Agility SDK package). WinPixEventRuntime is vendored in this repository, so it never "
    "has to be installed. A check with required=false describes a fallback rather than a "
    "hard dependency, so its absence does not block anything."
)


@tool(
    name="env-check",
    summary=(
        "Check this machine for everything the toolkit depends on - PIX, dxcompiler, "
        "CMake, the Visual Studio generator, the Windows SDK, a D3D12 device, the "
        "vendored WinPixEventRuntime and the Agility SDK package - and report what is "
        "missing plus how to fix it."
    ),
    category="diagnostics",
    parameters=object_schema(
        scope={
            "type": "string",
            "enum": ["all", "core", "replay"],
            "description": (
                "Which tier to probe. 'core' = what reading a .wpix needs; 'replay' = what "
                "rebuilding and running the exported project needs; 'all' (default) = both."
            ),
        },
        pixtool={
            "type": "string",
            "description": "Path to pixtool.exe when auto-detection fails, same as elsewhere.",
        },
        export_dir={
            "type": "string",
            "description": (
                "An export directory to inspect for a cached D3D12AgilitySdk.nupkg. "
                "Defaults to the active session's export when one exists."
            ),
        },
        session={
            "type": "string",
            "description": (
                "Session whose export directory should be inspected. Optional: with no "
                "session at all the environment is still fully checked."
            ),
        },
        check_network={
            "type": "boolean",
            "description": (
                "Also test that nuget.org is reachable for the Agility SDK download. "
                "Off by default so the check stays offline and fast."
            ),
        },
    ),
    returns=(
        "Per-check results with tier, ok, detail, what was found and a fix for each "
        "failure, plus a 'ready' summary saying whether read-only analysis and GPU "
        "replay can work on this machine."
    ),
    examples=[
        "pix-tool-set env-check",
        "pix-tool-set env-check --scope replay",
        "pix-tool-set env-check --check-network --compact",
    ],
    requires_session=False,
    notes=_NOTE,
)
def env_check(args: dict[str, Any], context: ToolContext) -> ToolResult:
    scope = str(args.get("scope") or "all")

    # An export directory is a bonus, never a requirement: the whole point is to be
    # usable on a machine where no capture has been opened yet.
    export_dir: Path | None = None
    if args.get("export_dir"):
        candidate = Path(str(args["export_dir"])).expanduser()
        export_dir = candidate if candidate.is_dir() else None
    else:
        try:
            record = context.store.resolve(session=args.get("session"))
            if record.export_dir and Path(record.export_dir).is_dir():
                export_dir = Path(record.export_dir)
        except Exception:  # noqa: BLE001 - no session is a normal state here
            export_dir = None

    report = envcheck.run_checks(
        scope=scope,
        pixtool=args.get("pixtool") or context.pixtool_path,
        export_dir=export_dir,
        check_network=bool(args.get("check_network")),
    )

    summary_lines = [
        f"{'ok  ' if check['ok'] else ('MISS' if check['required'] else 'opt ')} "
        f"{check['tier']:<6} {check['name']}: {check['detail']}"
        for check in report["checks"]
    ]
    data = {
        **report,
        "summary": summary_lines,
        "next_step": _next_step(report),
    }

    result = ToolResult.success(data)
    for name in report["missing"]["core"]:
        check = next(c for c in report["checks"] if c["name"] == name)
        result.degrade(
            f"Core dependency missing: {name} - {check['detail']}",
            fix=check.get("fix"),
        )
    for name in report["missing"]["replay"]:
        check = next(c for c in report["checks"] if c["name"] == name)
        result.degrade(
            f"Replay dependency missing: {name} - {check['detail']}",
            fix=check.get("fix"),
        )
    # Optional gaps do not degrade the result - the machine still works - but they are
    # reported so a caller is not surprised when a fallback route is unavailable later.
    for name in report["optional_missing"]:
        check = next(c for c in report["checks"] if c["name"] == name)
        result.add_diagnostic(
            "info",
            f"Optional capability absent: {name} - {check['detail']}",
            fix=check.get("fix"),
        )
    return result


def _next_step(report: dict[str, Any]) -> str:
    """One actionable sentence, so a caller need not rank the failures itself."""
    if report["missing"]["core"]:
        first = next(
            c for c in report["checks"] if c["name"] == report["missing"]["core"][0]
        )
        return first.get("fix") or f"Resolve {first['name']} first; nothing works without it."
    if report["missing"]["replay"]:
        first = next(
            c for c in report["checks"] if c["name"] == report["missing"]["replay"][0]
        )
        return (
            f"Static analysis is ready. For GPU replay: {first.get('fix') or first['name']}"
        )
    if report["ready"].get("gpu_replay"):
        return "Everything is in place; run `session-open --capture <file.wpix>` to start."
    return (
        "Core dependencies are in place; run `session-open --capture <file.wpix>` to start. "
        "Run `env-check --scope replay` before using the rebuild/replay tools."
    )
