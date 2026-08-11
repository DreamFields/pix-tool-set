"""Replay session management: baseline gate, edit ledger, reset, full-frame dump.

These tools wrap the build-and-run pipeline in `replay_render_tools` with the
discipline a debugging session needs:

  * **replay-baseline-check** (D5): build and run with *no* shader patches, capture
    the output, and cache it as the trusted baseline. If patches are already applied
    the tool refuses, because a baseline taken over a patched export is not a
    baseline — it is a verdict about the patch, wearing a baseline's clothes. Every
    later comparison trusts this gate; if it is wrong, every comparison is wrong.

  * **replay-edits** (D3): list every shader-edit-apply patch currently in the
    export, grouped by the ledger that `shader-edit-apply` writes. This is the
    "what did I change?" view that an agent needs before deciding whether to reset.

  * **replay-reset** (D4): revert all patches — restore `CreatePSOs.cpp` from its
    `.orig` backup, delete every `edited_*.dxil`, and clear the ledger. The export
    returns to its post-`session-open` state so a new edit cycle can begin.

  * **frame-replay-dump** (D2): build and run the full frame, then dump every
    resource the frame touches (RTs, UAVs, depth) alongside the final backbuffer.
    This is the "what actually happened?" view for a whole frame, not just one pass.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from ..context import ToolContext
from ..engine.editledger import EditLedger
from ..engine import activity, exportstate, framesnapshot, screencap, uavprobe


from ..errors import PixToolError, not_found
from ..results import ToolResult
from ._common import tool, with_session
from .replay_render_tools import _export_root, _configure_and_build, _await_window, _await_content
import subprocess

# Regex to find patched-stage markers in CreatePSOs.cpp.
# The marker written by _patch_export is:
#   // pix-tool-set: <stage> replaced by shader-edit-apply
_PATCH_MARKER = "// pix-tool-set:"
_PATCH_SUFFIX = "replaced by shader-edit-apply"


# ======================================================================
# Patch detection — pure function, unit-testable without a capture.
# ======================================================================

def detect_patches(export_dir: Path) -> list[dict[str, Any]]:
    """Scan the export directory for shader-edit-apply patches.

    Returns a list of patch records, each with:
      - ``pso_id``: the PSO whose stage was patched
      - ``stage``: the stage that was patched (VS, PS, CS, etc.)
      - ``bytecode_file``: path to the ``edited_*.dxil`` override file
      - ``marker_line``: the marker text found in ``CreatePSOs.cpp``

    An empty list means the export is clean (no patches), which is what
    ``replay-baseline-check`` requires.
    """
    patches: list[dict[str, Any]] = []
    export_dir = Path(export_dir)

    # 1. Scan for edited_*.dxil files — these are the bytecode overrides.
    for dxil in sorted(export_dir.glob("edited_CreatePipelineState_*_*.dxil")):
        # edited_CreatePipelineState_2972_PS.dxil → pso_id=2972, stage=PS
        stem = dxil.stem  # edited_CreatePipelineState_2972_PS
        parts = stem.split("_")
        if len(parts) >= 4:
            pso_id = parts[-2]
            stage = parts[-1]
            patches.append({
                "pso_id": pso_id,
                "stage": stage,
                "bytecode_file": str(dxil),
                "marker_line": "",
            })

    # 2. Scan CreatePSOs.cpp for override markers.
    create_psos = export_dir / "CreatePSOs.cpp"
    if create_psos.exists():
        text = create_psos.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            stripped = line.strip()
            if _PATCH_MARKER in stripped and _PATCH_SUFFIX in stripped:
                # Try to associate with a PSO by looking at the enclosing function.
                # The marker is inside void CreatePipelineState_<pso_id>().
                # Walk backwards to find the function declaration.
                func_start = text.rfind("void CreatePipelineState_", 0, text.find(stripped))
                pso_id = "?"
                if func_start != -1:
                    func_end = text.find("()", func_start)
                    if func_end != -1:
                        pso_id = text[func_start + len("void CreatePipelineState_"):func_end]
                # Extract stage from the marker: "// pix-tool-set: <stage> replaced..."
                stage = stripped.split(_PATCH_MARKER)[1].split("replaced")[0].strip()

                # Deduplicate against the file scan above.
                existing = next(
                    (p for p in patches if p["pso_id"] == pso_id and p["stage"] == stage),
                    None,
                )
                if existing:
                    existing["marker_line"] = stripped
                else:
                    patches.append({
                        "pso_id": pso_id,
                        "stage": stage,
                        "bytecode_file": "",
                        "marker_line": stripped,
                    })

    return patches


def _export_fingerprint(export_dir: Path) -> str:
    """Hash the key export files so a cache miss is detected after a re-export."""
    h = hashlib.sha256()
    for name in ("CreatePSOs.cpp", "resources.bin", "CMakeLists.txt"):
        path = export_dir / name
        if path.exists():
            h.update(name.encode())
            h.update(str(path.stat().st_mtime).encode())
            h.update(str(path.stat().st_size).encode())
    return h.hexdigest()[:16]


# ======================================================================
# replay-baseline-check (D5)
# ======================================================================

_BASELINE_NOTE = (
    "Builds and runs the exported replay project with NO shader patches, captures the "
    "output, and caches it as the trusted baseline for all later comparisons. If the "
    "export has any shader-edit-apply patches, the tool refuses — a baseline taken over "
    "a patched export is not a baseline. Subsequent calls reuse the cached baseline "
    "unless the export has changed (re-exported, re-built, or patched and reset). "
    "Requires CMake and a Visual Studio toolchain."
)


@tool(
    name="replay-baseline-check",
    summary=(
        "Build and run the null-patch replay (no shader edits), capture its output, "
        "and cache it as the trusted baseline. Refuses if patches are applied."
    ),
    category="meta",
    parameters=with_session(
        force={
            "type": "boolean",
            "description": "Rebuild and re-capture even if a cached baseline exists.",
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
        tolerance={
            "type": "number",
            "description": (
                "Maximum colour-space distance (0-1) between the baseline and a re-run "
                "that is still considered 'the same'. Default 0.02. Used when comparing "
                "a cached baseline to a fresh capture."
            ),
        },
    ),
    returns="Baseline capture path, colour summary, build details, and cache status.",
    examples=[
        "pix-tool-set replay-baseline-check",
        "pix-tool-set replay-baseline-check --force",
        "pix-tool-set replay-baseline-check --settle-seconds 300",
    ],
    notes=_BASELINE_NOTE,
)
def replay_baseline_check(args: dict[str, Any], context: ToolContext) -> ToolResult:
    root = _export_root(context, args)

    # --- D5: refuse if patches are applied. A baseline over a patched export is not
    # a baseline — it is a verdict about the patch, wearing a baseline's clothes.
    # Every later comparison trusts this gate; if it is wrong, every comparison is wrong.
    patches = detect_patches(root)
    if patches:
        patch_list = "\n".join(
            f"  - pso {p['pso_id']} {p['stage']}: {p.get('bytecode_file') or p.get('marker_line')}"
            for p in patches
        )
        raise PixToolError(
            code="patches_present",
            message=(
                f"The export has {len(patches)} shader-edit-apply patch(es) applied. "
                "A baseline must be taken with no patches, otherwise it measures the "
                "patch, not the replay infrastructure.\n\nPatches found:\n"
                + patch_list
            ),
            stage="baseline",
            details={"patches": patches},
            suggestion=(
                "Run replay-reset to revert all patches, then run replay-baseline-check again."
            ),
        )

    # --- Check for cached baseline.
    cache_file = root / "baseline.json"
    fingerprint = _export_fingerprint(root)
    if not args.get("force") and cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if cached.get("fingerprint") == fingerprint:
                # Verify the capture file still exists.
                capture_path = Path(cached.get("capture_path", ""))
                if capture_path.exists():
                    cached["cache_hit"] = True
                    result = ToolResult.success(cached)
                    result.add_diagnostic(
                        "info",
                        "Reusing the cached baseline. Pass --force to rebuild and re-capture.",
                    )
                    return result
        except (json.JSONDecodeError, KeyError):
            pass  # Corrupt cache; fall through to rebuild.

    # --- Build and run.
    settle = int(args.get("settle_seconds") or 150)
    timeout = int(args.get("build_timeout") or 1800)
    generator = str(args.get("generator") or "Visual Studio 18 2026")

    steps = _configure_and_build(
        root, generator, timeout, bool(args.get("force_reconfigure")), args
    )
    exe = Path(steps["executable"])

    # The working directory must be the export root: resources.bin and any
    # edited_*.dxil are resolved relative to it.
    process = subprocess.Popen([str(exe)], cwd=str(root))
    data: dict[str, Any] = {
        "export_dir": str(root),
        "build": steps,
        "fingerprint": fingerprint,
    }

    try:
        deadline = time.time() + settle
        window = _await_window(process.pid, deadline, min_pixels=200 * 200)
        if window is None:
            raise PixToolError(
                code="replay_window_unavailable",
                message="The replay started but never produced a window with pixels to read.",
                stage="baseline",
                suggestion="Raise --settle-seconds; a multi-gigabyte capture can take minutes.",
                details={"pid": process.pid},
            )
        data["window"] = window.to_dict()

        awaited = _await_content(window.hwnd, deadline, min_score=0.02)
        if awaited is None:
            raise PixToolError(
                code="window_capture_failed",
                message="Neither PrintWindow nor a screen BitBlt returned usable pixels.",
                stage="baseline",
                suggestion="Make sure the window is not fully occluded or on a blanked display.",
                details={"window": window.to_dict()},
            )
        pixels, width, height, method, wait_info = awaited
        data["wait"] = wait_info

        blank = wait_info["content_score"] < 0.02
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = f"baseline_{stamp}_{width}x{height}.png"
        target = activity.renders_dir() / name
        written = screencap.write_png(target, pixels, width, height)

        summary = screencap.colour_summary(pixels, width, height)
        regions = screencap.viewport_blankness(pixels, width, height)
        data["capture"] = {
            "path": str(target),
            "bytes": written,
            "width": width,
            "height": height,
            "method": method,
            "colour": summary,
            "regions": regions,
            "shows_rendered_frame": not blank,
        }

        # --- Cache the baseline.
        cache = {
            "fingerprint": fingerprint,
            "capture_path": str(target),
            "colour": summary,
            "regions": regions,
            "timestamp": stamp,
            "shows_rendered_frame": not blank,
        }
        cache_file.write_text(json.dumps(cache, indent=2), encoding="utf-8")
        data["cache"] = {"path": str(cache_file), "written": True}

    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
        data["run"] = {"stopped": True}

    result = ToolResult.success(data, output_paths=[data["capture"]["path"]])
    if blank:
        result.degrade(
            "The baseline capture is blank — the replay never showed a rendered frame. "
            "Every comparison against this baseline would be meaningless. Raise "
            "--settle-seconds and try again.",
            reason="the replay window stayed blank",
        )
    else:
        result.add_diagnostic(
            "info",
            "Baseline established. Subsequent replay-render calls with --compare-to "
            "will diff against this capture. The baseline is cached in baseline.json "
            "and reused until the export changes or --force is passed.",
        )
    return result


# ======================================================================
# replay-edits (D3)
# ======================================================================

_EDITS_NOTE = (
    "Lists every shader-edit-apply patch currently in the export, as recorded by the "
    "edit ledger. Each entry shows the PSO, stage, shader hash, scope, timestamp, and "
    "the bytecode file. This is the 'what did I change?' view before deciding whether "
    "to reset or continue editing."
)


@tool(
    name="replay-edits",
    summary="List all shader-edit-apply patches currently in the export.",
    category="meta",
    parameters=with_session(),
    returns="List of patch records from the edit ledger, or an empty list if clean.",
    examples=[
        "pix-tool-set replay-edits",
    ],
    notes=_EDITS_NOTE,
)
def replay_edits(args: dict[str, Any], context: ToolContext) -> ToolResult:
    root = _export_root(context, args)
    ledger = EditLedger(root)

    entries = ledger.list_entries()
    # Cross-check against the filesystem: the ledger is the source of truth for
    # what was intentionally applied, but detect_patches catches anything the
    # ledger missed (e.g. a manual edit, or a ledger that was deleted).
    filesystem_patches = detect_patches(root)

    data = {
        "ledger_entries": entries,
        "ledger_count": len(entries),
        "filesystem_patches": filesystem_patches,
        "filesystem_count": len(filesystem_patches),
        "consistent": len(entries) == len(filesystem_patches),
    }
    result = ToolResult.success(data)
    if not data["consistent"]:
        result.degrade(
            f"The ledger records {len(entries)} patch(es) but the filesystem has "
            f"{len(filesystem_patches)} patch file(s). They may have diverged because "
            "the ledger was deleted, a patch was applied manually, or the export was "
            "re-exported. Run replay-reset to clean up and start fresh.",
            reason="ledger and filesystem disagree",
        )
    if not entries:
        result.add_diagnostic(
            "info",
            "No patches in the ledger. The export is clean — safe to run "
            "replay-baseline-check.",
        )
    return result


# ======================================================================
# replay-reset (D4)
# ======================================================================

_RESET_NOTE = (
    "Revert all shader-edit-apply patches in the export: restore CreatePSOs.cpp from "
    "its .orig backup, delete every edited_*.dxil override file, and clear the edit "
    "ledger. The export returns to its post-session-open state so a new edit cycle "
    "can begin. The baseline cache is also invalidated, because the export has changed.\n\n"
    "Three separate mechanisms inject into an export -- shader-edit-apply, the read-uav "
    "readback probe, and the pixel-history-replay sampler -- so 'clean' is reported per "
    "injector rather than as one flag. Leftover probes are restored too unless "
    "--keep-probes is given: a probe left installed would be compiled into the next "
    "replay, and reporting the export clean while one is present is the failure this "
    "tool used to have."
)


@tool(
    name="replay-reset",
    summary="Revert all shader-edit-apply patches and clear the edit ledger.",
    category="meta",
    parameters=with_session(
        keep_baseline={
            "type": "boolean",
            "description": "Do not invalidate the baseline cache. Default false.",
        },
        keep_probes={
            "type": "boolean",
            "description": (
                "Leave the read-uav and pixel-history-replay probes installed. Default "
                "false: probes are restored, because one left behind is compiled into "
                "the next replay. The export is still reported as not clean when they "
                "are kept, so the state is never misrepresented."
            ),
        },
    ),
    returns="List of reverted patches and cleaned files.",
    examples=[
        "pix-tool-set replay-reset",
        "pix-tool-set replay-reset --keep-probes",
    ],
    notes=_RESET_NOTE,
)
def replay_reset(args: dict[str, Any], context: ToolContext) -> ToolResult:
    root = _export_root(context, args)
    ledger = EditLedger(root)

    state_before = exportstate.inspect(root)

    # 1. Revert patches recorded in the ledger.
    ledger_reverted = ledger.reset()

    # 2. Restore CreatePSOs.cpp from .orig if it exists.
    create_psos = root / "CreatePSOs.cpp"
    orig_backup = root / "CreatePSOs.cpp.orig"
    orig_restored = False
    if orig_backup.exists():
        orig_backup.replace(create_psos)
        orig_restored = True

    # 3. Delete any remaining edited_*.dxil files (catches anything the ledger missed).
    deleted_dxils: list[str] = []
    for dxil in root.glob("edited_CreatePipelineState_*_*.dxil"):
        dxil.unlink()
        deleted_dxils.append(str(dxil))

    # 4. Clear the ledger.
    ledger.clear()

    # 5. Restore the readback/sampling probes. They are a different injector from
    #    shader-edit-apply and were previously invisible here, which is how a reset
    #    could report the export clean while a probe was still installed.
    probes_restored: dict[str, Any] = {}
    if not args.get("keep_probes"):
        probes_restored = exportstate.restore_all(root)

    # 6. Invalidate the baseline cache (the export has changed).
    baseline_invalidated = False
    if not args.get("keep_baseline"):
        cache_file = root / "baseline.json"
        if cache_file.exists():
            cache_file.unlink()
            baseline_invalidated = True

    state_after = exportstate.inspect(root)

    data = {
        "orig_restored": orig_restored,
        "deleted_dxils": deleted_dxils,
        "deleted_dxil_count": len(deleted_dxils),
        "ledger_reverted": ledger_reverted,
        "baseline_invalidated": baseline_invalidated,
        "probes_restored": probes_restored,
        # Reported per injector: a single boolean cannot say "the shader patches are
        # gone but a probe is still installed", and that is exactly the state that
        # used to be mislabelled as clean.
        "shader_edits_clean": not state_after["shader_edit"]["injected"],
        "uav_probe_clean": not state_after["uav_probe"]["injected"],
        "pixel_probe_clean": not state_after["pixel_probe"]["injected"],
        "clean": state_after["clean"],
        "injectors_present": state_after["injectors_present"],
        "state_before": state_before,
        "state_after": state_after,
    }
    result = ToolResult.success(data)
    if not data["clean"]:
        remaining = ", ".join(state_after["injectors_present"])
        if args.get("keep_probes"):
            result.add_diagnostic(
                "warning",
                f"Injections remain by request (--keep-probes): {remaining}. The export "
                "is not clean; replay-baseline-check will not be meaningful until they "
                "are restored.",
            )
        else:
            result.degrade(
                f"Injections are still present after reset: {remaining}. Each injector "
                "has its own restore path; see state_after for the files involved.",
                reason="post-reset inspection still finds injected markers",
            )
    else:
        result.add_diagnostic(
            "info",
            "Export is clean across all three injectors (shader edits, read-uav probe, "
            "pixel-history probe) — safe to run replay-baseline-check or start a new "
            "edit cycle.",
        )
    return result



# ======================================================================
# frame-replay-dump (D2)
# ======================================================================

_DUMP_NOTE = (
    "Builds and runs the exported replay project with a readback probe, and dumps every "
    "writable resource the frame touches (UAVs, render targets, depth buffers) in a single "
    "replay. This is the full-frame 'what actually happened?' view: instead of probing one "
    "resource at a time, it captures them all at once so cross-resource relationships are "
    "visible. Each dump records its last-read and last-write draw index so the caller can "
    "correlate the GPU state with the command stream. When the probe does not finish cleanly "
    "(replay crashed, settle window too short), the result is marked frame_end_unreliable "
    "so no downstream comparison trusts it. Requires CMake and a Visual Studio toolchain."
)


@tool(
    name="frame-replay-dump",
    summary=(
        "Replay the full frame and dump every writable resource (UAVs, RTs, depth) in "
        "one run. The full-frame 'what actually happened?' view."
    ),
    category="meta",
    parameters=with_session(
        output={
            "type": "string",
            "description": (
                "Directory for all dump files. Defaults to the activity log directory. "
                "Ignored when --snapshot is used, which allocates its own directory."
            ),
        },
        snapshot={
            "type": "boolean",
            "description": (
                "Write this dump into a new numbered snapshot directory beside the "
                "export (<capture>.pixcache/snapshots/NNNN-label/), together with a "
                "manifest recording which shader edits were applied when it was taken. "
                "One directory per edit, so 'the frame before this change' and 'after' "
                "both remain on disk and cannot be mixed up. Use snapshot-list to see "
                "them."
            ),
        },
        snapshot_label={
            "type": "string",
            "description": (
                "Name for the snapshot directory. Defaults to a label derived from the "
                "current edit state ('baseline', 'edit-PS-a1b2c3'). The sequence number "
                "is always prefixed and is never reused."
            ),
        },
        snapshot_note={
            "type": "string",
            "description": "Free-text note stored in the snapshot manifest.",
        },

        resource_types={
            "type": "array",
            "items": {"type": "string", "enum": ["uav", "rt", "depth", "all"]},
            "description": (
                "Which resource types to dump. 'uav' = compute UAVs, 'rt' = render targets, "
                "'depth' = depth/stencil, 'all' = everything. Default ['uav', 'rt', 'depth']."
            ),
        },
        at={
            "type": "string",
            "enum": ["frame-end", "last-read"],
            "description": (
                "When to capture each resource. 'frame-end' (default) dumps at frame end. "
                "'last-read' records the last-read draw index for each resource; the dump "
                "still happens at frame end but the metadata allows correlation with the "
                "last-read point. True per-resource last-read timing requires a probe "
                "modification not yet implemented."
            ),
        },
        settle_seconds={
            "type": "integer",
            "description": "Seconds to let the replay run while waiting for dumps. Default 300.",
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
        no_vendored_winpixruntime={
            "type": "boolean",
            "description": "Download WinPixEventRuntime from nuget instead of using the vendored copy.",
        },
        keep_probe={
            "type": "boolean",
            "description": "Leave the probe in the export. Default false.",
        },
        max_resources={
            "type": "integer",
            "description": (
                "Cap the number of resources to dump. Default 32; a full frame can touch "
                "hundreds, and each adds a READBACK allocation. Resources are prioritised "
                "by write count (most-written first)."
            ),
        },
        source_state={
            "type": "integer",
            "description": (
                "D3D12_RESOURCE_STATES the resources are in when the probe copies them. "
                "Default 8 (UNORDERED_ACCESS)."
            ),
        },
    ),
    returns="List of dumped resources, their statistics, last-read/write draw indices, and frame_end_unreliable flag.",
    examples=[
        "pix-tool-set frame-replay-dump",
        "pix-tool-set frame-replay-dump --resource-types uav --max-resources 8",
        "pix-tool-set frame-replay-dump --at last-read --output G:\\dumps",
    ],
    notes=_DUMP_NOTE,
)
def frame_replay_dump(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    root = _export_root(context, args)

    # --- Enumerate writable resources from the capture's resource_usage map.
    resource_types = args.get("resource_types") or ["uav", "rt", "depth"]
    if "all" in resource_types:
        resource_types = ["uav", "rt", "depth"]

    usage = capture.resource_usage
    coverage = capture.descriptor_coverage

    targets: list[dict[str, Any]] = []
    for rid, info in usage.items():
        is_uav = bool(info.get("write_draws") and not info.get("render_target_draws") and not info.get("depth_draws"))
        is_rt = bool(info.get("render_target_draws"))
        is_depth = bool(info.get("depth_draws"))

        type_match = (
            ("uav" in resource_types and is_uav)
            or ("rt" in resource_types and is_rt)
            or ("depth" in resource_types and is_depth)
        )
        if not type_match:
            continue

        resource = capture.resource(rid)
        if resource is None:
            continue

        # Determine resource type label.
        if is_rt:
            rtype = "rt"
        elif is_depth:
            rtype = "depth"
        else:
            rtype = "uav"

        # Last-read and last-write draw indices for correlation.
        read_draws = info.get("read_draws", [])
        write_draws = info.get("write_draws", [])
        last_read = max(read_draws) if read_draws else None
        last_write = max(write_draws) if write_draws else None

        targets.append({
            "resource_id": rid,
            "type": rtype,
            "resource": resource.to_dict(),
            "last_read_draw": last_read,
            "last_write_draw": last_write,
            "write_count": len(write_draws),
            "read_count": len(read_draws),
            "passes": info.get("passes", []),
        })

    # Prioritise by write count (most-written first) and cap.
    targets.sort(key=lambda t: -t["write_count"])
    max_resources = int(args.get("max_resources") or 32)
    capped = len(targets) > max_resources
    targets = targets[:max_resources]

    if not targets:
        result = ToolResult.success({
            "export_dir": str(root),
            "targets": [],
            "frame_end_unreliable": False,
            "message": "No writable resources matched the filter.",
        })
        result.add_diagnostic(
            "warning",
            "No writable resources matched the --resource-types filter. Check that the "
            "capture has descriptor data; run pass-bindings to verify.",
        )
        return result

    resource_ids = [t["resource_id"] for t in targets]

    # --- Install the probe, build, and run.
    settle = int(args.get("settle_seconds") or 300)
    timeout = int(args.get("build_timeout") or 1800)
    generator = str(args.get("generator") or "Visual Studio 18 2026")
    state = int(args.get("source_state") or uavprobe.STATE_UNORDERED_ACCESS)
    keep_probe = bool(args.get("keep_probe"))

    data: dict[str, Any] = {
        "export_dir": str(root),
        "targets": targets,
        "resource_count": len(targets),
        "capped": capped,
        "at": args.get("at") or "frame-end",
        "descriptor_coverage": coverage,
    }
    diagnostics: list[tuple[str, str]] = []

    injection = uavprobe.install(root)
    data["probe_injection"] = injection
    try:
        skip_build = bool(args.get("skip_build"))
        downgraded = skip_build and bool(injection.get("rebuild_needed"))
        if downgraded:
            skip_build = False
            diagnostics.append((
                "warning",
                "--skip-build was ignored: the probe had to be injected just now.",
            ))
        if skip_build:
            executables = sorted(
                (root / "build" / "Release").glob("*.exe"), key=lambda p: -p.stat().st_size
            )
            if not executables:
                raise not_found("built executable", str(root / "build" / "Release"))
            data["build"] = {"skipped": True, "executable": str(executables[0])}
            exe = executables[0]
        else:
            steps = _configure_and_build(
                root, generator, timeout, bool(args.get("force_reconfigure")), args
            )
            data["build"] = steps
            exe = Path(steps["executable"])

        # --- Run the probe with all resource IDs as comma-separated targets.
        # A snapshot allocates its own directory and records the edit state that
        # produced this dump, which is what makes two dumps comparable later. Its
        # manifest is written before the run, so an interrupted replay still leaves
        # a record of what was being attempted.
        snapshot_record: dict[str, Any] | None = None
        if bool(args.get("snapshot")):
            snapshot_record = framesnapshot.create(
                root,
                label=args.get("snapshot_label"),
                note=str(args.get("snapshot_note") or ""),
            )
            output_dir = Path(snapshot_record["path"])
            data["snapshot"] = snapshot_record
            if args.get("output"):
                diagnostics.append((
                    "info",
                    "--output was ignored because --snapshot allocates its own "
                    f"directory: {output_dir}",
                ))
        elif args.get("output"):
            output_dir = Path(str(args["output"]))
        else:
            output_dir = activity.renders_dir()
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        prefix = output_dir / f"framedump_{stamp}"


        environment = dict(os.environ)
        environment[uavprobe.ENV_TARGETS] = ",".join(str(rid) for rid in resource_ids)
        environment[uavprobe.ENV_OUT] = str(prefix)
        environment[uavprobe.ENV_STATE] = str(state)

        process = subprocess.Popen([str(exe)], cwd=str(root), env=environment)
        run_info: dict[str, Any] = {
            "pid": process.pid,
            "working_directory": str(root),
            "targets": resource_ids,
        }
        started = time.time()
        frame_end_unreliable = False
        try:
            # Wait for the probe to finish (sentinel file appears).
            deadline = started + settle
            while time.time() < deadline:
                if uavprobe.summarise_probe_log(prefix).get("finished"):
                    break
                time.sleep(2.0)

            run_info["seconds"] = round(time.time() - started, 1)
            run_info["probe"] = uavprobe.summarise_probe_log(prefix)

            if not run_info["probe"].get("finished"):
                frame_end_unreliable = True
                diagnostics.append((
                    "warning",
                    "The probe did not write its sentinel within the settle window. The "
                    "dumps may be incomplete or absent. This result is marked "
                    "frame_end_unreliable.",
                ))
        finally:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                frame_end_unreliable = True
            run_info["stopped"] = True

        data["run"] = run_info
        data["frame_end_unreliable"] = frame_end_unreliable

        # --- Decode each resource dump.
        dumps: list[dict[str, Any]] = []
        for target in targets:
            rid = target["resource_id"]
            dump_bin = output_dir / f"{prefix.name}_{rid}.bin"
            dump_sidecar = Path(str(dump_bin) + ".txt")

            entry: dict[str, Any] = {
                "resource_id": rid,
                "type": target["type"],
                "last_read_draw": target["last_read_draw"],
                "last_write_draw": target["last_write_draw"],
                "dump_file": str(dump_bin) if dump_bin.exists() else None,
                "dumped": dump_bin.exists() and dump_sidecar.exists(),
            }

            if entry["dumped"]:
                try:
                    dump = uavprobe.read_sidecar(dump_bin)
                    blob = dump_bin.read_bytes()
                    packed = uavprobe.depad(blob, dump)
                    image = uavprobe.as_image(packed, dump)
                    stats = uavprobe.statistics(image)
                    entry["statistics"] = stats
                    entry["format"] = image.format_name
                    entry["width"] = image.width
                    entry["height"] = image.height
                    entry["bytes_read"] = len(blob)

                    # Also write a PNG for visual inspection.
                    encoded = uavprobe.to_rgb_png(image)
                    if encoded is not None:
                        png_blob, mapping = encoded
                        png_path = dump_bin.with_suffix(".png")
                        png_path.write_bytes(png_blob)
                        entry["png_file"] = str(png_path)
                except Exception as exc:
                    entry["decode_error"] = f"{type(exc).__name__}: {exc}"

            dumps.append(entry)

        data["dumps"] = dumps
        data["dumped_count"] = sum(1 for d in dumps if d.get("dumped"))

        if snapshot_record is not None:
            # A snapshot whose replay did not finish cleanly is kept, not discarded:
            # deleting it would destroy the evidence of the failure. It is flagged
            # instead, so a later comparison can refuse it rather than silently
            # diffing partial data.
            data["snapshot"] = framesnapshot.finalise(
                root,
                snapshot_record,
                dump_summary={
                    "resource_count": len(targets),
                    "dumped_count": data["dumped_count"],
                    "resource_types": resource_types,
                    "at": data["at"],
                    "capped": capped,
                },
                reliable=not frame_end_unreliable,
            )

    finally:
        if keep_probe:
            data["probe_cleanup"] = {"action": "left installed (--keep-probe)"}
        else:
            data["probe_cleanup"] = uavprobe.restore(root)

    result = ToolResult.success(data)
    for level, message in diagnostics:
        result.add_diagnostic(level, message)

    if frame_end_unreliable:
        result.degrade(
            "The frame-end dump is unreliable — the probe did not finish cleanly. "
            "Do not trust these dumps for comparison; re-run with a longer --settle-seconds.",
            reason="probe did not write its sentinel",
        )

    result.add_diagnostic(
        "info",
        f"Dumped {data.get('dumped_count', 0)} of {len(targets)} resources in one replay. "
        "Each dump records the resource's last-read and last-write draw index for "
        "correlation with the command stream.",
    )
    if isinstance(data.get("snapshot"), dict):
        snap = data["snapshot"]
        edits = (snap.get("edit_state") or {}).get("edit_count", 0)
        result.add_diagnostic(
            "info",
            f"Snapshot {snap.get('sequence')} written to {snap.get('path')} "
            f"({edits} shader edit(s) applied when it was taken). Run snapshot-list to "
            "see every snapshot, or snapshot-compare to diff two of them.",
        )
    return result


