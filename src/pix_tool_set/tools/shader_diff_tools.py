"""shader-edit-diff: replay a frame twice, with and without the shader patch, and
report what the edit changed in a UAV.

Why this exists
---------------
``shader-edit-apply --patch`` proves a shader *compiles* and gets patched in. It says
nothing about what the edit did to the pixels. Answering that by hand takes five steps
and about ten minutes:

  1. run ``read-uav`` to dump the UAV with the patch active,
  2. rename ``edited_CreatePipelineState_<pso>_<stage>.dxil`` so it stops being found,
  3. run ``read-uav`` again to dump the original shader's output,
  4. decode both dumps,
  5. difference them and build something you can look at.

Every one of those steps is mechanical, and step 2 is the one that bites: forget to
rename the file back and the patch looks lost. So this tool does all five, and puts the
rename inside a ``try``/``finally`` so the patch name is restored whatever happens -
including a build failure, a replay that never dumps, or a Ctrl-C.

The two mechanisms it is built on
---------------------------------
*No recompile is needed to switch versions.* The override that ``shader-edit-apply``
writes is ``Helpers::ReadFileBytes(LR"(edited_...dxil)")`` guarded by
``if (!editedBytes.empty())``, so a file it cannot open silently leaves the recorded
bytecode in place. Renaming the .dxil is therefore a complete, reversible A/B switch
over one already-built executable - which is why both dumps come from a single build.

*Reading a compute UAV is already solved.* ``read-uav`` injects a readback probe, and
this tool reuses that whole pipeline - ``uavprobe.install``/``restore`` for the probe,
``_run_probe`` for one armed replay, ``read_sidecar``/``depad``/``as_image``/
``statistics``/``to_rgb_png`` for the decode - rather than growing a second copy that
would drift from it.

What it will not do
-------------------
It refuses before replaying anything if the PSO has no patch, or if the patch file
exists but ``CreatePSOs.cpp`` does not reference it. Both cases would otherwise burn
two three-minute replays to discover that the two sides are identical, which reads as
"the edit did nothing" when the truth is "the edit was never wired in".
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..context import ToolContext
from ..engine import screencap, uavprobe
from ..engine.model import ShaderStage
from ..errors import PixToolError, invalid_argument, not_found
from ..results import ToolResult
from ._common import DRAW_SELECTOR, resolve_draw, tool, with_session
from .replay_render_tools import configure_and_build, export_root

# read-uav owns "which resource did the user mean" and "run one armed replay". Both are
# imported rather than reimplemented, and rather than being re-exported from that module
# under public names, because widening another module's API is not this change's business.
from .uav_readback_tools import _resolve_target, _run_probe

_STAGES = [stage.value for stage in ShaderStage]

#: Suffix appended to the patch .dxil to disable it. Any name the loader cannot find
#: works; a visible, obviously-temporary one means a crashed run leaves a clue rather
#: than a mystery.
HOLD_SUFFIX = ".hold"

#: Default sum-of-channels delta, in 8-bit units, above which a pixel counts as changed.
#: 6 tolerates the last-bit wobble of a recompile without hiding a real edit: a 10-bit
#: channel's least significant step is 0.25 of an 8-bit level.
DEFAULT_THRESHOLD = 6

_NOTE = (
    "Replays the frame twice over one build - once with the shader patch active and once "
    "with its .dxil renamed so the loader falls back to the recorded bytecode - then "
    "decodes both readbacks and differences them. Switching versions needs no recompile "
    "because shader-edit-apply's override reads the bytecode from a file and keeps the "
    "original when that file is missing. The rename is undone in a finally block, so the "
    "patch is never left disabled even if the build, the replay or the decode fails. "
    "Requires a patch made by shader-edit-apply --patch; the tool refuses up front rather "
    "than replaying twice to discover there is nothing to compare."
)

_SEMANTICS = (
    "BEFORE is the shader as captured, AFTER is the edited shader. Both sides are what "
    "the GPU wrote into this resource during a replay, read out of a READBACK copy, so "
    "the difference is caused by the shader edit and by nothing else: same executable, "
    "same frame, same probe, one renamed file between them."
)


# ======================================================================
# what to toggle
# ======================================================================
def _resolve_stage(draw, args: dict[str, Any]) -> str:
    """The stage whose patch is being toggled."""
    wanted = args.get("stage")
    if wanted:
        stage = str(wanted).upper()
        if draw.shader(stage) is None:
            available = ", ".join(s.stage.value for s in draw.shaders) or "none"
            raise not_found(
                "shader stage",
                stage,
                f"This event binds {available}.",
            )
        return stage

    stages = [shader.stage.value for shader in draw.shaders]
    if not stages:
        raise not_found(
            "shader",
            f"draw {draw.index}",
            "This event binds no shader, so there is no patch to toggle.",
        )
    if len(stages) > 1:
        raise invalid_argument(
            "stage",
            "this event binds " + ", ".join(stages) + "; name the patched one with --stage",
        )
    return stages[0]


def _patch_path(root: Path, pso_id: Any, stage: str, args: dict[str, Any]) -> Path:
    """Where shader-edit-apply put this PSO's replacement bytecode."""
    override = args.get("patch_file")
    if override:
        return Path(str(override)).expanduser()
    return root / f"edited_CreatePipelineState_{pso_id}_{stage}.dxil"


def _require_patch(root: Path, patch: Path, pso_id: Any, stage: str, queue_hint: str) -> dict[str, Any]:
    """Fail before any replay unless there is a live patch to compare against.

    Two failures are distinguished because they need different fixes: no .dxil at all
    means the patch was never made, while a .dxil that CreatePSOs.cpp never reads means
    it was made and then reverted (or built for a different PSO). Both would otherwise
    surface as "the two sides are identical" after six minutes of replaying.
    """
    if not patch.exists():
        raise PixToolError(
            code="shader_patch_missing",
            message=f"PSO {pso_id} has no patched {stage} bytecode to compare against.",
            stage="shader",
            paths=[str(patch)],
            suggestion=(
                "There is nothing to diff until an edit has been applied. Run "
                f"shader-edit-begin {queue_hint} --stage {stage}, edit the HLSL, then "
                f"shader-edit-apply {queue_hint} --stage {stage} --source <file.hlsl> "
                "--patch. That writes the .dxil this tool toggles."
            ),
        )

    info: dict[str, Any] = {
        "path": str(patch),
        "bytes": patch.stat().st_size,
        "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(patch.stat().st_mtime)),
    }

    creator = root / "CreatePSOs.cpp"
    if creator.exists():
        referenced = patch.name in creator.read_text(encoding="utf-8", errors="replace")
        info["referenced_by_createpsos"] = referenced
        if not referenced:
            raise PixToolError(
                code="shader_patch_inert",
                message=(
                    f"{patch.name} exists but CreatePSOs.cpp never reads it, so the "
                    "replay would run the captured shader on both sides."
                ),
                stage="export",
                paths=[str(patch), str(creator)],
                suggestion=(
                    "The override was removed, or CreatePSOs.cpp was restored from its "
                    ".orig backup after the patch was written. Re-run shader-edit-apply "
                    "--patch to wire it back in."
                ),
            )
    stale = patch.with_name(patch.name + HOLD_SUFFIX)
    if stale.exists():
        info["stale_hold_file"] = str(stale)
    return info


@contextmanager
def _patch_disabled(patch: Path) -> Iterator[Path]:
    """Rename the patch away for the duration of the block, then always rename it back.

    This is the whole reason the tool exists as a tool: the disable/enable pair has to
    survive every failure mode, and a human doing it by hand eventually does not. The
    restore runs in ``finally``, so an exception, a failed replay or a Ctrl-C all leave
    the patch under its original name.
    """
    held = patch.with_name(patch.name + HOLD_SUFFIX)
    if held.exists():
        # A previous run died between rename and restore. Its file is the same bytecode
        # under the wrong name, so it is discarded rather than allowed to block this one.
        held.unlink()
    os.replace(patch, held)
    try:
        yield held
    finally:
        if held.exists():
            os.replace(held, patch)


# ======================================================================
# decoding both sides onto one scale
# ======================================================================
def _shared_range(reports: list[dict[str, Any]], normalised: bool) -> tuple[float, float]:
    """One display range covering both surfaces.

    A per-image contrast stretch is right for a single picture and wrong for a
    comparison: it would map two different value ranges onto the same 0..255 and hide
    exactly the change being measured. UNORM data needs no stretch at all.
    """
    if normalised:
        return 0.0, 1.0
    low, high = float("inf"), float("-inf")
    for report in reports:
        for entry in (report.get("channels") or [])[:3]:
            low = min(low, float(entry.get("min", 0.0)))
            high = max(high, float(entry.get("max", 0.0)))
    if low > high:
        return 0.0, 1.0
    return low, high


def _bgra_rows(image, low: float, high: float) -> list[bytearray]:
    """Decode the surface to 8-bit BGRA rows through a caller-chosen range.

    BGRA because that is what ``screencap.encode_png_rgb`` consumes, so the rows feed
    the PNG writer directly and are the same numbers the difference is computed from.
    """
    span = (high - low) or 1.0
    rows: list[bytearray] = []
    for y in range(image.height):
        row = bytearray(image.width * 4)
        for x in range(image.width):
            values = image.pixel(x, y)
            if not isinstance(values, (list, tuple)):
                triple: Any = (values, values, values)
            elif len(values) >= 3:
                triple = values[:3]
            else:
                triple = (values[0],) * 3
            offset = x * 4
            for index, value in enumerate(triple):
                if value != value:  # NaN
                    level = 0
                else:
                    level = int((value - low) / span * 255.0)
                    level = 0 if level < 0 else (255 if level > 255 else level)
                row[offset + (2 - index)] = level
            row[offset + 3] = 255
        rows.append(row)
    return rows


def _compare(
    before: list[bytearray], after: list[bytearray], threshold: int
) -> tuple[dict[str, Any], list[bytearray]]:
    """Count what changed, by how much, and build the difference map.

    Exhaustive, not sampled: "how much of the image did the edit touch" is a claim about
    every pixel, and a 1.5 megapixel surface costs a few seconds against a replay that
    costs minutes.
    """
    height = min(len(before), len(after))
    width = (min(len(before[0]), len(after[0])) // 4) if height else 0

    changed = differing = compared = 0
    sums = [0, 0, 0]
    peaks = [0, 0, 0]
    diff_rows: list[bytearray] = []

    for y in range(height):
        row_before, row_after = before[y], after[y]
        out = bytearray(width * 4)
        for x in range(width):
            offset = x * 4
            deltas = (
                abs(row_before[offset + 2] - row_after[offset + 2]),
                abs(row_before[offset + 1] - row_after[offset + 1]),
                abs(row_before[offset] - row_after[offset]),
            )
            total = deltas[0] + deltas[1] + deltas[2]
            for index in range(3):
                sums[index] += deltas[index]
                if deltas[index] > peaks[index]:
                    peaks[index] = deltas[index]
            compared += 1
            if total:
                differing += 1
            if total > threshold:
                changed += 1
            level = 255 if total > 255 else total
            out[offset] = out[offset + 1] = out[offset + 2] = level
            out[offset + 3] = 255
        diff_rows.append(out)

    names = ("R", "G", "B")
    report: dict[str, Any] = {
        "width": width,
        "height": height,
        "compared_pixels": compared,
        "changed_pixels": changed,
        "changed_share_percent": round(100.0 * changed / compared, 2) if compared else 0.0,
        "differing_pixels": differing,
        "differing_share_percent": round(100.0 * differing / compared, 2) if compared else 0.0,
        "threshold_8bit": threshold,
        "threshold_means": (
            f"a pixel counts as changed when |dR|+|dG|+|dB| exceeds {threshold} of 255"
        ),
        "mean_abs_delta_8bit": {
            name: round(sums[index] / compared, 2) if compared else 0.0
            for index, name in enumerate(names)
        },
        "max_abs_delta_8bit": {name: peaks[index] for index, name in enumerate(names)},
    }
    return report, diff_rows


def _side_by_side(before: list[bytearray], after: list[bytearray], gap: int = 8) -> tuple[bytearray, int, int]:
    """BEFORE | AFTER in one image, so the pair can be judged at a glance."""
    height = min(len(before), len(after))
    width = (min(len(before[0]), len(after[0])) // 4) if height else 0
    total_width = width * 2 + gap
    blob = bytearray()
    spacer = bytearray(gap * 4)
    for index in range(gap):
        spacer[index * 4 + 3] = 255
    for y in range(height):
        blob += before[y][: width * 4]
        blob += spacer
        blob += after[y][: width * 4]
    return blob, total_width, height


def _write_png(path: Path, rows: list[bytearray], width: int, height: int) -> dict[str, Any]:
    blob = bytearray()
    for y in range(height):
        blob += rows[y][: width * 4]
    encoded = screencap.encode_png_rgb(blob, width, height)
    path.write_bytes(encoded)
    return {"path": str(path), "bytes": len(encoded)}


def _channel_table(
    before: dict[str, Any], after: dict[str, Any], normalised: bool
) -> list[dict[str, Any]]:
    """Per-channel means on both sides with their delta, so the change has a number."""
    table: list[dict[str, Any]] = []
    left = {entry["channel"]: entry for entry in (before.get("channels") or [])}
    right = {entry["channel"]: entry for entry in (after.get("channels") or [])}
    for name in [entry["channel"] for entry in (before.get("channels") or [])]:
        low, high = left.get(name, {}), right.get(name, {})
        row: dict[str, Any] = {
            "channel": name,
            "before_mean": low.get("mean"),
            "after_mean": high.get("mean"),
        }
        if low.get("mean") is not None and high.get("mean") is not None:
            row["delta_mean"] = round(high["mean"] - low["mean"], 6)
        if normalised:
            row["before_mean_8bit"] = low.get("mean_8bit")
            row["after_mean_8bit"] = high.get("mean_8bit")
            if low.get("mean_8bit") is not None and high.get("mean_8bit") is not None:
                row["delta_mean_8bit"] = round(high["mean_8bit"] - low["mean_8bit"], 2)
        table.append(row)
    return table


# ======================================================================
def _decode(dump_path: Path) -> tuple[Any, Any, dict[str, Any]]:
    """Sidecar, decoded image and statistics for one probe dump."""
    dump = uavprobe.read_sidecar(dump_path)
    blob = dump.bin_path.read_bytes()
    packed = uavprobe.depad(blob, dump)
    image = uavprobe.as_image(packed, dump)
    return dump, image, uavprobe.statistics(image)


@tool(
    name="shader-edit-diff",
    summary=(
        "Replay the frame with and without the shader patch active and report what the "
        "edit changed in a UAV: BEFORE/AFTER/DIFF/SIDE_BY_SIDE images plus the changed "
        "pixel count and per-channel means. One command for the five manual steps."
    ),
    category="shaders",
    parameters=with_session(
        DRAW_SELECTOR,
        stage={
            "type": "string",
            "enum": _STAGES,
            "description": (
                "Patched stage to toggle. Optional when the event binds only one stage."
            ),
        },
        name={
            "type": "string",
            "description": (
                "Declared UAV name to observe, e.g. RWNormalTexture. Resolved through the "
                "shader's reflection and the descriptor table at --queue-id."
            ),
        },
        resource_id={
            "type": "integer",
            "description": "Observe this resource directly, skipping name resolution.",
        },
        output={
            "type": "string",
            "description": (
                "Directory for both raw dumps and the BEFORE/AFTER/DIFF/SIDE_BY_SIDE PNGs."
            ),
        },
        patch_file={
            "type": "string",
            "description": (
                "Patch .dxil to toggle. Defaults to the edited_CreatePipelineState_<pso>_"
                "<stage>.dxil that shader-edit-apply --patch writes into the export."
            ),
        },
        diff_threshold={
            "type": "integer",
            "description": (
                "Sum-of-channels delta in 8-bit units above which a pixel counts as "
                f"changed. Default {DEFAULT_THRESHOLD}."
            ),
        },
        settle_seconds={
            "type": "integer",
            "description": (
                "Seconds to let each replay run while waiting for its dump. Default 240, "
                "spent twice; a multi-gigabyte capture needs minutes to reach its frame."
            ),
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
            "description": (
                "Run the existing executable without rebuilding. Only valid when the "
                "readback probe is already compiled into it."
            ),
        },
        no_vendored_winpixruntime={
            "type": "boolean",
            "description": (
                "Ignore the WinPixEventRuntime vendored in pix-tool-set and download it "
                "from nuget instead."
            ),
        },
        keep_probe={
            "type": "boolean",
            "description": (
                "Leave the injected readback probe in the export so the next call skips "
                "the rebuild. Default false: the export is restored from its .orig backups."
            ),
        },
        source_state={
            "type": "integer",
            "description": (
                "D3D12_RESOURCE_STATES the resource is in when the probe copies it. "
                "Default 8 (UNORDERED_ACCESS), where a compute UAV is left."
            ),
        },
    ),
    returns=(
        "The patch that was toggled and the confirmation it was restored, per-channel "
        "statistics for both sides with their deltas, the changed pixel count and share, "
        "and the four written PNGs."
    ),
    examples=[
        "pix-tool-set shader-edit-diff --queue-id 18704 --stage CS --name RWNormalTexture --output G:\\diff",
        "pix-tool-set shader-edit-diff --queue-id 18704 --resource-id 3032 --keep-probe",
    ],
    notes=_NOTE,
)
def shader_edit_diff(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    root = export_root(context, args)

    if args.get("queue_id") is None and args.get("draw_index") is None:
        raise invalid_argument(
            "queue_id",
            "the event says which PSO's patch to toggle, so it cannot be inferred from a "
            "resource alone; pass --queue-id",
        )

    draw = resolve_draw(capture, args, what="dispatch")
    stage = _resolve_stage(draw, args)
    queue_hint = (
        f"--queue-id {args['queue_id']}"
        if args.get("queue_id") is not None
        else f"--draw-index {args.get('draw_index')}"
    )
    patch = _patch_path(root, draw.pso_id, stage, args)
    patch_info = _require_patch(root, patch, draw.pso_id, stage, queue_hint)

    target = _resolve_target(capture, args)
    resource_id = target["resource_id"]
    resource = capture.resource(resource_id)
    if resource is None:
        raise not_found("resource", resource_id, "Run list-resources to find a valid id.")

    settle = int(args.get("settle_seconds") or 240)
    timeout = int(args.get("build_timeout") or 1800)
    generator = str(args.get("generator") or "Visual Studio 18 2026")
    state = int(args.get("source_state") or uavprobe.STATE_UNORDERED_ACCESS)
    threshold = int(args.get("diff_threshold") or DEFAULT_THRESHOLD)
    keep_probe = bool(args.get("keep_probe"))

    label = str(target.get("declared_name") or args.get("name") or f"resource_{resource_id}")
    output = (
        Path(str(args["output"])).expanduser()
        if args.get("output")
        else context.resolve_output(None, "shader-diff")
    )
    output.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")

    data: dict[str, Any] = {
        "export_dir": str(root),
        "event": {
            "draw_index": draw.index,
            "global_id": draw.global_id,
            "queue_id": draw.queue_id,
            "api": draw.api,
            "pass_name": draw.pass_name,
            "pso_id": draw.pso_id,
            "stage": stage,
        },
        "patch": patch_info,
        "observing": target,
        "resource": resource.to_dict(),
        "mechanism": (
            "shader-edit-apply's override reads the replacement bytecode from "
            f"{patch.name} and keeps the captured bytecode when that file cannot be "
            f"opened, so renaming it to {patch.name}{HOLD_SUFFIX} switches the shader "
            "back with no recompile. One build therefore serves both sides."
        ),
        "contents_are": _SEMANTICS,
    }
    diagnostics: list[tuple[str, str]] = []

    injection = uavprobe.install(root)
    data["probe_injection"] = injection
    patch_restored: bool | None = None
    try:
        # `--skip-build` only holds when the probe is already compiled in. Injected just
        # now means the existing exe predates it, and both replays would come back empty.
        # Build instead of refusing: the caller's intent is a diff, and a build is the
        # only way to get one.
        skip_build = bool(args.get("skip_build"))
        downgraded = skip_build and bool(injection.get("rebuild_needed"))
        if downgraded:
            skip_build = False
            diagnostics.append((
                "warning",
                "--skip-build was ignored: the readback probe had to be injected just "
                "now, so the existing executable cannot produce a dump. The project was "
                "built instead, rather than running two replays that would both come "
                "back empty.",
            ))
        if skip_build:
            executables = sorted(
                (root / "build" / "Release").glob("*.exe"), key=lambda p: -p.stat().st_size
            )
            if not executables:
                raise not_found(
                    "built executable",
                    str(root / "build" / "Release"),
                    "Nothing to run; drop --skip-build so the project gets built.",
                )
            exe = executables[0]
            data["build"] = {"skipped": True, "executable": str(exe)}
        else:
            steps = configure_and_build(
                root, generator, timeout, bool(args.get("force_reconfigure")), args
            )
            if downgraded:
                steps["skip_build_ignored"] = (
                    "the probe was injected during this run, so a build was required"
                )
            data["build"] = steps
            exe = Path(steps["executable"])
        data["build"]["serves_both_sides"] = (
            "The same executable runs twice; only the presence of the patch .dxil differs."
        )

        # --- AFTER: the patch is already active, so this side needs no file surgery.
        after_run = _run_probe(root, exe, output / f"after_{stamp}", resource_id, state, settle)
        data["after_run"] = after_run
        if not after_run.get("dump"):
            result = ToolResult.partial(data)
            result.degrade(
                "The patched replay produced no readback dump within the settle window, "
                "so there is nothing to compare and the original side was not run.",
                reason="the probe writes its sentinel only after dumping, and neither appeared",
                alternative=(
                    "Raise --settle-seconds; a multi-gigabyte capture can take minutes to "
                    "reach its first frame. The patch was left enabled."
                ),
            )
            for level, message in diagnostics:
                result.add_diagnostic(level, message)
            return result

        # --- BEFORE: rename the patch away, replay, and always rename it back.
        with _patch_disabled(patch) as held:
            data["patch"]["disabled_as"] = str(held)
            before_run = _run_probe(
                root, exe, output / f"before_{stamp}", resource_id, state, settle
            )
            data["before_run"] = before_run
        patch_restored = patch.exists()
        data["patch"]["restored"] = patch_restored
        data["patch"]["restored_to"] = str(patch)

        if not before_run.get("dump"):
            result = ToolResult.partial(data)
            result.degrade(
                "The unpatched replay produced no readback dump within the settle window, "
                "so only the patched side is available.",
                reason="the probe's sentinel never appeared on the second run",
                alternative=(
                    "Raise --settle-seconds and try again. The patch file name was "
                    "restored regardless."
                ),
            )
            for level, message in diagnostics:
                result.add_diagnostic(level, message)
            return result

        after_dump, after_image, after_stats = _decode(Path(after_run["dump"]))
        before_dump, before_image, before_stats = _decode(Path(before_run["dump"]))

        data["decoded"] = {
            "format": before_image.format_name,
            "width": before_image.width,
            "height": before_image.height,
            "channels": len(before_stats.get("channels") or []),
            "storage_units_per_pixel": before_image.component_count,
        }
        data["before"] = {"readback": before_dump.to_dict(), "statistics": before_stats}
        data["after"] = {"readback": after_dump.to_dict(), "statistics": after_stats}

        if (before_image.width, before_image.height) != (after_image.width, after_image.height):
            diagnostics.append((
                "warning",
                f"The two dumps decode to different sizes "
                f"({before_image.width}x{before_image.height} and "
                f"{after_image.width}x{after_image.height}); the comparison covers the "
                "overlapping region only.",
            ))

        normalised = before_image.format_name.endswith(("UNORM", "UNORM_SRGB"))
        low, high = _shared_range([before_stats, after_stats], normalised)
        before_rows = _bgra_rows(before_image, low, high)
        after_rows = _bgra_rows(after_image, low, high)

        comparison, diff_rows = _compare(before_rows, after_rows, threshold)
        comparison["channels"] = _channel_table(before_stats, after_stats, normalised)
        comparison["display_range"] = (
            "UNORM 0..1 mapped to 0..255"
            if normalised
            else f"both sides mapped through a shared range of {round(low, 6)}..{round(high, 6)}"
        )
        share = comparison["changed_share_percent"]
        comparison["verdict"] = (
            f"the edit changed {comparison['changed_pixels']} of "
            f"{comparison['compared_pixels']} pixels ({share}% of the surface) by more "
            f"than {threshold}/255"
            if comparison["changed_pixels"]
            else (
                f"no pixel differs by more than {threshold}/255; "
                f"{comparison['differing_pixels']} differ at all"
            )
        )
        data["comparison"] = comparison

        width, height = comparison["width"], comparison["height"]
        files: list[dict[str, Any]] = []
        for entry, image, side in (
            (before_dump, before_image, "BEFORE"),
            (after_dump, after_image, "AFTER"),
        ):
            files.append({
                "path": str(entry.bin_path),
                "bytes": entry.bin_path.stat().st_size,
                "side": side,
                "layout": "raw readback, row pitch padding intact",
            })
            files.append({
                "path": str(entry.sidecar_path),
                "bytes": entry.sidecar_path.stat().st_size,
                "side": side,
                "layout": "layout sidecar written by the probe",
            })

        png_before = _write_png(output / f"{label}_BEFORE.png", before_rows, width, height)
        png_before.update({"side": "BEFORE", "shows": "the shader as captured"})
        png_after = _write_png(output / f"{label}_AFTER.png", after_rows, width, height)
        png_after.update({"side": "AFTER", "shows": "the edited shader"})
        png_diff = _write_png(output / f"{label}_DIFF.png", diff_rows, width, height)
        png_diff.update({
            "side": "DIFF",
            "shows": "|BEFORE - AFTER| summed over RGB as grey, clamped at 255",
        })

        side_blob, side_width, side_height = _side_by_side(before_rows, after_rows)
        side_path = output / f"{label}_SIDE_BY_SIDE.png"
        side_encoded = screencap.encode_png_rgb(side_blob, side_width, side_height)
        side_path.write_bytes(side_encoded)
        files.extend([
            png_before,
            png_after,
            png_diff,
            {
                "path": str(side_path),
                "bytes": len(side_encoded),
                "side": "SIDE_BY_SIDE",
                "shows": f"BEFORE | AFTER, {side_width}x{side_height}",
            },
        ])
        data["files"] = files
    finally:
        # The patch name outlives everything else here, so it is checked last and
        # reported even when the body raised.
        if patch_restored is None:
            patch_restored = patch.exists()
            data.setdefault("patch", {})["restored"] = patch_restored
        if keep_probe:
            data["probe_cleanup"] = {
                "action": "left the probe installed, as --keep-probe was given",
                "left_behind": [
                    str(root / uavprobe.PROBE_SOURCE_NAME),
                    f"{root / 'RenderFrame.cpp'} (calls {uavprobe.PROBE_FUNCTION}())",
                    f"{root / 'CMakeLists.txt'} (lists {uavprobe.PROBE_SOURCE_NAME})",
                ],
                "restore_with": "read-uav or shader-edit-diff without --keep-probe",
            }
        else:
            data["probe_cleanup"] = uavprobe.restore(root)

    result = ToolResult.success(
        data, output_paths=[entry["path"] for entry in data.get("files", [])]
    )
    for level, message in diagnostics:
        result.add_diagnostic(level, message)

    if not patch_restored:
        result.degrade(
            f"The patch could not be restored to {patch.name}.",
            reason="the rename back failed, so the edit is still disabled",
            alternative=(
                f"Rename {patch.name}{HOLD_SUFFIX} back to {patch.name} by hand; the "
                "bytecode itself is intact."
            ),
        )
    else:
        result.add_diagnostic(
            "info",
            f"{patch.name} is back under its original name, so the patch is active again. "
            "The rename is undone in a finally block, so this holds even when a step "
            "above fails.",
        )

    if data.get("comparison", {}).get("changed_pixels") == 0:
        result.degrade(
            "Both replays wrote the same values into this resource.",
            reason=(
                "The readbacks succeeded and the patch was active on one side only, so "
                "either the edit does not affect this resource, or it affects a different "
                "dispatch than the one selected."
            ),
            alternative=(
                "Check with pass-bindings that this UAV is the register the edited shader "
                "writes, and that --queue-id names the dispatch that was patched."
            ),
        )

    result.add_diagnostic("info", _SEMANTICS)
    return result
