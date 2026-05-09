"""read-uav: read what a compute dispatch actually wrote into a UAV.

This is the tool for the one question the rest of the toolkit cannot answer. The
three existing paths to a texture each fail on a compute UAV for a different reason:

  * ``export-texture`` calls ``pixtool save-resource``, which exports *bound render
    targets*. A compute-only UAV is not one, so it fails with "PIXTOOL9 - Requested
    Render Target with specified index does not exist".
  * ``export-uav-slice`` and ``read-resource-texture`` read ``resources.bin``, which
    holds uploads and CPU writes. A UAV the GPU fills is never re-uploaded, so those
    tools correctly return the resource's *initial* bytes - a different thing from the
    dispatch's output, and one that usually reads as all zeros.
  * ``replay-render`` photographs the replay window. An intermediate G-Buffer UAV never
    reaches the backbuffer, so the window is unchanged even when the UAV is not.

So this tool replays the frame with a readback probe injected into the exported C++
project, and decodes the bytes the GPU wrote. The build pipeline is reused wholesale
from ``replay_render_tools``, because the two CMake traps handled there (a 0-byte
.nupkg from a failed SSL download, and a build directory configured by another
generator) apply identically and a second copy would drift.

The probe is a change to the user's export, so it is removed again on the way out.
``--keep-probe`` opts out, and either way the diagnostics say exactly which files were
touched: silently leaving an injected .cpp behind is not acceptable.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

from ..context import ToolContext
from ..engine import uavprobe
from ..engine.model import RootParameterKind, ViewKind
from ..errors import PixToolError, invalid_argument, not_found
from ..results import ToolResult
from ._common import DRAW_SELECTOR, resolve_draw, tool, with_session
from .replay_render_tools import configure_and_build, export_root

_NOTE = (
    "This is the only path to the contents of a compute-written UAV. It injects a "
    "readback probe into the exported C++ project, builds it, replays the frame, and "
    "copies the target resource out of a READBACK heap - so the values are what the GPU "
    "wrote during the replay, not the initial bytes that export-uav-slice reports. "
    "Requires CMake and a Visual Studio toolchain; the first run builds the export and "
    "takes minutes, later runs reuse it. The probe is removed and the export restored "
    "from its .orig backups afterwards unless --keep-probe is given. Row pitch padding "
    "is handled from the layout sidecar the probe writes, never guessed."
)

_SEMANTICS = (
    "These values are what the GPU wrote to this resource during the replay, read out "
    "of a READBACK copy taken after the frame's recorded work was submitted. That is a "
    "different question from export-uav-slice, which reports the initial bytes recorded "
    "in resources.bin and cannot see any GPU write."
)


# ======================================================================
# resolving which resource to read
# ======================================================================
def _uav_views(draw) -> list[tuple[int, Any]]:
    """Every UAV view bound at this draw, in descriptor-table order.

    Register order is table order: a UAV range starting at u0 maps slot n onto u(n).
    That is what makes ``--name RWNormalTexture`` resolvable to one resource rather
    than to a guess, now that ModifyDescriptors is folded into descriptor parsing.
    """
    out: list[tuple[int, Any]] = []
    for binding in draw.bindings:
        if binding.kind is not RootParameterKind.DESCRIPTOR_TABLE:
            continue
        slot = 0
        for view in binding.resolved_views:
            if view.kind is not ViewKind.UAV:
                continue
            out.append((slot, view))
            slot += 1
    return out


def _declared_uavs(draw) -> list[dict[str, Any]]:
    """The shader's declared UAV registers - the authoritative name/register mapping."""
    declared: list[dict[str, Any]] = []
    for shader in draw.shaders:
        for record in shader.resource_bindings:
            ident = (record.get("id") or "").upper()
            if not ident.startswith("U"):
                continue
            entry = dict(record)
            entry["stage"] = shader.stage.value
            suffix = ident[1:]
            entry["register_index"] = int(suffix) if suffix.isdigit() else None
            declared.append(entry)
    return declared


def _resolve_by_name(capture, draw, name: str) -> dict[str, Any]:
    """Map a declared UAV name onto the resource bound at its register.

    Two independent facts are combined: the shader's reflection gives the register
    (authoritative, it comes from the bytecode), and the descriptor table gives the
    resource at that slot. Both are reported so the answer can be checked rather than
    trusted.
    """
    wanted = name.strip().lower()
    declared = _declared_uavs(draw)
    match = next(
        (entry for entry in declared if (entry.get("name") or "").lower() == wanted),
        None,
    )
    if match is None:
        raise not_found(
            "declared UAV",
            name,
            "This draw's shaders declare: "
            + ", ".join(entry.get("name") or "?" for entry in declared)
            + ". Names come from the shader bytecode reflection.",
        )

    register = match.get("register_index")
    views = _uav_views(draw)
    if register is None or register >= len(views):
        raise not_found(
            "UAV descriptor",
            f"{name} ({match.get('id')})",
            (
                f"The shader declares {match.get('id')} but the descriptor table "
                f"expanded to {len(views)} UAV slot(s), so the register cannot be "
                "mapped to a resource. Pass --resource-id instead, or check "
                "pass-bindings for the table's trust level."
            ),
        )
    slot, view = views[register]
    if view.resource_id is None:
        raise not_found(
            "resource behind UAV",
            name,
            "The descriptor at that slot names no resource.",
        )
    return {
        "resource_id": int(view.resource_id),
        "resolved_by": "name",
        "declared_register": match.get("id"),
        "declared_name": match.get("name"),
        "declared_dimension": match.get("dimension"),
        "declared_stage": match.get("stage"),
        "descriptor_slot": slot,
        "descriptor": view.to_dict(),
        "mapping": (
            f"{match.get('id')} is descriptor slot {slot} of the UAV table, which holds "
            f"resource {view.resource_id}"
        ),
    }


def _resolve_target(capture, args: dict[str, Any]) -> dict[str, Any]:
    """Decide which resource to read, and record how that decision was made."""
    name = args.get("name")
    has_selector = args.get("queue_id") is not None or args.get("draw_index") is not None

    if name and not has_selector:
        raise invalid_argument(
            "queue_id",
            "resolving a UAV by name needs an event selector too, because the name only "
            "exists in the shader's declaration; pass --queue-id",
        )

    if name:
        draw = resolve_draw(capture, args, what="dispatch")
        found = _resolve_by_name(capture, draw, str(name))
        found["draw"] = {
            "draw_index": draw.index,
            "global_id": draw.global_id,
            "api": draw.api,
            "pso_id": draw.pso_id,
        }
        if args.get("resource_id") is not None and int(args["resource_id"]) != found[
            "resource_id"
        ]:
            raise invalid_argument(
                "resource_id/name",
                f"--name {name} resolves to resource {found['resource_id']} but "
                f"--resource-id says {args['resource_id']}; drop one of them",
            )
        return found

    if args.get("resource_id") is None:
        raise invalid_argument(
            "resource_id/name",
            "identify the UAV by --resource-id, or by --name plus --queue-id",
        )

    found = {
        "resource_id": int(args["resource_id"]),
        "resolved_by": "resource_id",
        "mapping": "taken from --resource-id without consulting any shader declaration",
    }
    if has_selector:
        draw = resolve_draw(capture, args, what="dispatch")
        found["draw"] = {
            "draw_index": draw.index,
            "global_id": draw.global_id,
            "api": draw.api,
            "pso_id": draw.pso_id,
        }
        for slot, view in _uav_views(draw):
            if view.resource_id == found["resource_id"]:
                found["descriptor_slot"] = slot
                found["descriptor"] = view.to_dict()
                declared = _declared_uavs(draw)
                match = next(
                    (e for e in declared if e.get("register_index") == slot), None
                )
                if match is not None:
                    found["declared_register"] = match.get("id")
                    found["declared_name"] = match.get("name")
                break
    return found


# ======================================================================
# running the probe
# ======================================================================
def _await_dump(prefix: Path, resource_id: int, deadline: float, mip: int = 0) -> Path | None:
    """Wait for the probe's sentinel, then hand back the dump it announced.

    The sentinel is written after the dumps, so its presence means "the probe ran to
    completion" rather than "a file appeared". Polling on the .bin alone would risk
    reading a partially written file, and a fixed sleep would either be wrong or slow.

    ``mip`` must match the suffix the probe chose, or a mip-N run would wait for a
    mip-0 filename that is never written and time out as "no dump produced".
    """
    suffix = f"_{resource_id}.bin" if mip == 0 else f"_{resource_id}_mip{mip}.bin"
    target = prefix.parent / f"{prefix.name}{suffix}"
    while time.time() < deadline:
        if uavprobe.summarise_probe_log(prefix).get("finished"):
            # The sidecar is written after the .bin, so wait for it too.
            if target.exists() and Path(str(target) + ".txt").exists():
                return target
            return None
        time.sleep(2.0)
    return None


def _run_probe(
    root: Path,
    exe: Path,
    prefix: Path,
    resource_id: int,
    state: int,
    settle: int,
    mip: int = 0,
) -> dict[str, Any]:
    """Replay the frame once with the probe armed, and wait for its dump."""
    environment = dict(os.environ)
    environment[uavprobe.ENV_TARGETS] = str(resource_id)
    environment[uavprobe.ENV_OUT] = str(prefix)
    environment[uavprobe.ENV_STATE] = str(state)
    environment[uavprobe.ENV_MIP] = str(mip)

    # The working directory must be the export root: resources.bin and any
    # edited_*.dxil are resolved relative to it.
    process = subprocess.Popen([str(exe)], cwd=str(root), env=environment)
    info: dict[str, Any] = {
        "pid": process.pid,
        "working_directory": str(root),
        "environment": {
            uavprobe.ENV_TARGETS: str(resource_id),
            uavprobe.ENV_OUT: str(prefix),
            uavprobe.ENV_STATE: str(state),
            uavprobe.ENV_MIP: str(mip),
        },
    }
    started = time.time()
    try:
        dump = _await_dump(prefix, resource_id, started + settle, mip)
        info["seconds"] = round(time.time() - started, 1)
        info["probe"] = uavprobe.summarise_probe_log(prefix)
        info["dump"] = str(dump) if dump else None
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
        info["stopped"] = True
    return info


# ======================================================================
@tool(
    name="read-uav",
    summary=(
        "Read what a compute dispatch actually wrote into a UAV, by replaying the frame "
        "with a readback probe. The only path that sees GPU writes rather than the "
        "recorded initial bytes."
    ),
    category="textures",
    parameters=with_session(
        DRAW_SELECTOR,
        resource_id={
            "type": "integer",
            "description": "Read this resource directly, skipping name resolution.",
        },
        name={
            "type": "string",
            "description": (
                "Declared UAV name to resolve, e.g. RWNormalTexture. Needs --queue-id "
                "as well, because the name only exists in the shader's declaration."
            ),
        },
        output={
            "type": "string",
            "description": "Directory for the raw .bin, its layout sidecar and the PNG.",
        },
        png={
            "type": "boolean",
            "description": "Also write a viewable RGB PNG. Default true.",
        },
        pixels={
            "type": "integer",
            "description": "Return this many decoded pixel values, spread across the image.",
        },
        at_x={"type": "integer", "description": "Return the value of a single pixel's column."},
        at_y={"type": "integer", "description": "Return the value of a single pixel's row."},
        settle_seconds={
            "type": "integer",
            "description": (
                "Seconds to let the replay run while waiting for the dump. Default 240; "
                "a multi-gigabyte capture needs minutes before its first frame."
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
                "probe is already compiled into it."
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
                "Leave the injected probe in the export so the next call skips the "
                "rebuild. Default false: the export is restored from its .orig backups."
            ),
        },
        source_state={
            "type": "integer",
            "description": (
                "D3D12_RESOURCE_STATES the resource is in when the probe copies it. "
                "Default 8 (UNORDERED_ACCESS), which is where a compute UAV is left."
            ),
        },
        mip={
            "type": "integer",
            "description": (
                "Mip level to read back. Default 0. A mip-chain pass binds one texture "
                "at several mips in a single dispatch (UE5's ReduceHZB writes mips 4..7 "
                "at once), so the mip must be named to get that UAV's own output."
            ),
        },
    ),
    returns=(
        "The resolved resource id and the descriptor facts behind it, the readback "
        "footprint, per-channel min/max/mean, written file paths, and what the values "
        "mean relative to export-uav-slice."
    ),
    examples=[
        "pix-tool-set read-uav --queue-id 18704 --name RWNormalTexture --output G:\\out",
        "pix-tool-set read-uav --resource-id 3032 --pixels 8",
        "pix-tool-set read-uav --resource-id 791 --mip 4 --output G:\\out",
        "pix-tool-set read-uav --queue-id 18704 --name RWNormalTexture --keep-probe",
    ],
    notes=_NOTE,
)
def read_uav(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    root = export_root(context, args)

    target = _resolve_target(capture, args)
    resource_id = target["resource_id"]
    resource = capture.resource(resource_id)
    if resource is None:
        raise not_found(
            "resource",
            resource_id,
            "Run list-resources to find a valid id.",
        )

    settle = int(args.get("settle_seconds") or 240)
    timeout = int(args.get("build_timeout") or 1800)
    generator = str(args.get("generator") or "Visual Studio 18 2026")
    state = int(args.get("source_state") or uavprobe.STATE_UNORDERED_ACCESS)
    keep_probe = bool(args.get("keep_probe"))

    # Rejected here rather than inside the probe: a bad mip would otherwise cost a
    # full rebuild and replay before failing, and the resource's mip count is already
    # known from the export.
    mip = int(args.get("mip") or 0)
    mip_levels = max(resource.mip_levels, 1)
    if mip < 0 or mip >= mip_levels:
        raise invalid_argument(
            "mip",
            f"resource {resource_id} has {mip_levels} mip level(s), so valid values are "
            f"0..{mip_levels - 1}; {mip} is out of range.",
        )

    data: dict[str, Any] = {
        "export_dir": str(root),
        "target": target,
        "resource": resource.to_dict(),
        "mip": mip,
        "contents_are": _SEMANTICS,
    }
    diagnostics: list[tuple[str, str]] = []

    if not resource.is_uav:
        diagnostics.append((
            "warning",
            f"Resource {resource_id} is not flagged as a UAV in the capture, so it may "
            "not be written by any dispatch. The readback is still valid; it just may "
            "not be the resource you meant.",
        ))

    injection = uavprobe.install(root)
    data["probe_injection"] = injection
    try:
        # `--skip-build` is only meaningful when the executable already contains the
        # probe. If it was injected just now, the built exe predates it and the replay
        # cannot produce a dump, so honouring the flag would burn a full settle window
        # to arrive at a guaranteed-empty result. Build instead and say why.
        skip_build = bool(args.get("skip_build"))
        downgraded = skip_build and bool(injection.get("rebuild_needed"))
        if downgraded:
            skip_build = False
            diagnostics.append((
                "warning",
                "--skip-build was ignored: the readback probe had to be injected just "
                "now, so the existing executable does not contain it and would produce "
                "no dump. The project was built instead.",
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
            data["build"] = {"skipped": True, "executable": str(executables[0])}
            exe = executables[0]
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

        output = Path(str(args["output"])) if args.get("output") else (
            context.resolve_output(None, "uav").parent
        )
        output.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        prefix = output / f"uav_{stamp}"

        run = _run_probe(root, exe, prefix, resource_id, state, settle, mip)
        data["run"] = run

        if not run.get("dump"):
            result = ToolResult.partial(data)
            result.degrade(
                "The replay produced no readback dump within the settle window.",
                reason=(
                    "The probe writes its sentinel only after dumping, and neither "
                    "appeared."
                ),
                alternative=(
                    "Raise --settle-seconds; a multi-gigabyte capture can take minutes "
                    "before its first frame. Check that the resource id is live in this "
                    "frame."
                ),
            )
            for level, message in diagnostics:
                result.add_diagnostic(level, message)
            return result

        dump = uavprobe.read_sidecar(Path(run["dump"]))
        data["readback"] = dump.to_dict()

        blob = dump.bin_path.read_bytes()
        packed = uavprobe.depad(blob, dump)
        data["readback"]["bytes_read"] = len(blob)
        data["readback"]["packed_bytes"] = len(packed)
        data["readback"]["pitch_padding_removed"] = (
            f"{dump.row_pitch} byte pitch carries {dump.row_size_bytes} bytes of pixels, "
            f"so {dump.row_pitch - dump.row_size_bytes} byte(s) per row were dropped"
        )
        if len(blob) < dump.total_bytes:
            diagnostics.append((
                "warning",
                f"The dump is {len(blob)} bytes but the footprint declares "
                f"{dump.total_bytes}; the surface is truncated and the statistics cover "
                "only the rows present.",
            ))

        image = uavprobe.as_image(packed, dump)
        data["statistics"] = uavprobe.statistics(image)
        # `component_count` is how many storage units one pixel unpacks into, which is 1
        # for bit-packed formats such as R10G10B10A2_UNORM even though they carry four
        # colour channels. Report both, so "components" cannot be mistaken for the number
        # of channels the statistics below describe.
        data["decoded"] = {
            "format": image.format_name,
            "width": image.width,
            "height": image.height,
            "storage_units_per_pixel": image.component_count,
            "channels": len(data["statistics"].get("channels") or []),
            "bytes_per_pixel": image.bytes_per_pixel,
        }

        want = int(args.get("pixels") or 0)
        if want:
            data["pixel_samples"] = uavprobe.sample_pixels(image, want)
        if args.get("at_x") is not None and args.get("at_y") is not None:
            x, y = int(args["at_x"]), int(args["at_y"])
            if 0 <= x < image.width and 0 <= y < image.height:
                data["pixel_at"] = {"x": x, "y": y, "value": image.pixel(x, y)}
            else:
                diagnostics.append((
                    "warning",
                    f"({x}, {y}) is outside the decoded {image.width}x{image.height} "
                    "surface, so no pixel was read.",
                ))

        files = [
            {
                "path": str(dump.bin_path),
                "bytes": len(blob),
                "layout": "raw readback, row pitch padding intact",
            },
            {
                "path": str(dump.sidecar_path),
                "bytes": dump.sidecar_path.stat().st_size,
                "layout": "layout sidecar written by the probe",
            },
        ]
        want_png = args.get("png")
        if want_png is None or bool(want_png):
            encoded = uavprobe.to_rgb_png(image)
            if encoded is not None:
                blob_png, mapping = encoded
                png_path = dump.bin_path.with_suffix(".png")
                png_path.write_bytes(blob_png)
                entry = {"path": str(png_path), "bytes": len(blob_png)}
                entry.update(mapping)
                files.append(entry)
        data["files"] = files
    finally:
        if keep_probe:
            data["probe_cleanup"] = {
                "action": "left the probe installed, as --keep-probe was given",
                "left_behind": [
                    str(root / uavprobe.PROBE_SOURCE_NAME),
                    f"{root / 'RenderFrame.cpp'} (calls {uavprobe.PROBE_FUNCTION}())",
                    f"{root / 'CMakeLists.txt'} (lists {uavprobe.PROBE_SOURCE_NAME})",
                ],
                "restore_with": (
                    "read-uav without --keep-probe, or copy the .orig backups back by hand"
                ),
            }
        else:
            data["probe_cleanup"] = uavprobe.restore(root)

    stats = data.get("statistics", {})
    result = ToolResult.success(
        data, output_paths=[entry["path"] for entry in data.get("files", [])]
    )
    for level, message in diagnostics:
        result.add_diagnostic(level, message)

    if stats.get("nonzero_share_percent") == 0.0:
        result.degrade(
            "Every sampled pixel is zero.",
            reason=(
                "The readback succeeded, so this is what the GPU left in the resource: "
                "either the dispatch did not run in this frame, or it wrote zeros."
            ),
            alternative=(
                "Check with pass-bindings that this resource is really the register you "
                "meant, and that the dispatch is inside the replayed frame."
            ),
        )

    result.add_diagnostic("info", _SEMANTICS)
    if keep_probe:
        result.add_diagnostic(
            "warning",
            f"The probe is still installed in {root}: {uavprobe.PROBE_SOURCE_NAME} was "
            f"added, RenderFrame.cpp calls {uavprobe.PROBE_FUNCTION}(), and "
            "CMakeLists.txt lists the new source. Both edited files have .orig backups "
            "beside them. Re-run without --keep-probe to restore them.",
        )
    else:
        result.add_diagnostic(
            "info",
            "The export was restored to its pre-injection state, so nothing was left "
            "changed. Pass --keep-probe to keep the probe compiled in and skip the "
            "rebuild next time.",
        )
    return result
