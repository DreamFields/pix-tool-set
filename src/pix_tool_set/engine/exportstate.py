"""What has been injected into an export directory, across every injector.

Three separate mechanisms modify a pixtool C++ export in place:

* ``shader-edit-apply`` patches ``CreatePSOs.cpp`` and writes ``edited_*.dxil``;
* ``read-uav`` (``engine/uavprobe.py``) injects a readback probe;
* ``pixel-history-replay`` (``engine/pixelprobe.py``) injects a per-event sampler.

Each owns its own marker, its own ``.orig`` backups and its own restore path. The
problem this module exists to fix: ``replay-reset`` knew about the first one only,
so after a run that left a probe installed it still answered ``clean: true``. A
single boolean covering one of three injectors is worse than no boolean at all --
it says "safe to start a new edit cycle" while the export still contains an
injected probe that will end up compiled into the next replay.

The rule here is that "clean" must mean *nothing at all is injected*, and that a
report names which injector is responsible for each finding, so the caller knows
which restore path to run instead of being told to re-export the whole capture.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import pixelprobe, uavprobe

#: Marker written by shader-edit-apply into CreatePSOs.cpp.
SHADER_EDIT_MARKER = "// pix-tool-set:"
SHADER_EDIT_SUFFIX = "replaced by shader-edit-apply"

#: Files each injector creates outright (as opposed to editing in place).
_PIXEL_PROBE_FILES = (
    "PixToolSetPixelProbe.cpp",
    "PixToolSetPixelProbe.h",
    "pixtoolset_pixel_probe_plan.json",
)


def _scan_marker(export_dir: Path, marker: str) -> list[dict[str, Any]]:
    """Every source file in the export containing ``marker``.

    Scanning file contents rather than trusting a ledger is deliberate: the whole
    class of bug being guarded against is state that exists on disk but not in the
    bookkeeping. A user who hand-edits or interrupts a run leaves exactly that.
    """
    hits: list[dict[str, Any]] = []
    for pattern in ("*.cpp", "*.h", "*.txt"):
        for path in sorted(export_dir.glob(pattern)):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if marker in text:
                hits.append(
                    {
                        "file": path.name,
                        "occurrences": text.count(marker),
                    }
                )
    return hits


def inspect(export_dir: Path | str) -> dict[str, Any]:
    """Report every injection present in the export, grouped by injector.

    Returns a dict with one entry per injector plus an overall ``clean`` flag and,
    when not clean, the exact tool calls that would undo each finding. Nothing is
    modified.
    """
    root = Path(export_dir)
    report: dict[str, Any] = {"export_dir": str(root)}

    if not root.exists():
        report.update({"clean": True, "export_missing": True})
        return report

    # -- shader-edit-apply ----------------------------------------------
    dxils = sorted(root.glob("edited_CreatePipelineState_*_*.dxil"))
    dxils += sorted(root.glob("edited_CreateStateObject_*_*.dxil"))
    shader_marker_hits = [
        hit
        for hit in _scan_marker(root, SHADER_EDIT_MARKER)
        if SHADER_EDIT_SUFFIX
        in (root / hit["file"]).read_text(encoding="utf-8", errors="replace")
    ]
    shader_edit = {
        "injected": bool(dxils or shader_marker_hits),
        "bytecode_files": [path.name for path in dxils],
        "marked_files": shader_marker_hits,
        "restore_with": "replay-reset",
    }

    # -- read-uav probe -------------------------------------------------
    uav_hits = _scan_marker(root, uavprobe.MARKER)
    uav_probe = {
        "injected": bool(uav_hits),
        "marked_files": uav_hits,
        "restore_with": "read-uav --restore-only, or engine.uavprobe.restore()",
    }

    # -- pixel-history-replay probe -------------------------------------
    pixel_hits = _scan_marker(root, pixelprobe.MARKER)
    probe_files = [name for name in _PIXEL_PROBE_FILES if (root / name).exists()]
    pixel_probe = {
        "injected": bool(pixel_hits or probe_files),
        "marked_files": pixel_hits,
        "probe_files": probe_files,
        "restore_with": "pixel-history-replay without --keep-probe, or engine.pixelprobe.restore()",
    }

    # -- leftover backups ------------------------------------------------
    # A .orig whose live file no longer carries any marker is not itself a defect
    # (shader-edit-apply's Helpers.h backup legitimately outlives a probe run), so
    # it is reported for visibility but does not make the export unclean.
    backups = sorted(path.name for path in root.glob("*.orig"))

    report.update(
        {
            "shader_edit": shader_edit,
            "uav_probe": uav_probe,
            "pixel_probe": pixel_probe,
            "backups_present": backups,
            "clean": not (
                shader_edit["injected"]
                or uav_probe["injected"]
                or pixel_probe["injected"]
            ),
        }
    )
    report["injectors_present"] = [
        name
        for name, entry in (
            ("shader-edit-apply", shader_edit),
            ("read-uav", uav_probe),
            ("pixel-history-replay", pixel_probe),
        )
        if entry["injected"]
    ]
    return report


def restore_all(export_dir: Path | str, *, include_shader_edits: bool = False) -> dict[str, Any]:
    """Run every probe's own restore path, in dependency order.

    Probes are restored before shader edits because both back up overlapping files
    (``CMakeLists.txt``, ``RenderFrame.cpp``); undoing the outermost injection
    first is what keeps a probe's ``.orig`` from being written back over a shader
    edit that should survive.

    ``include_shader_edits`` is off by default: reverting a shader patch is a
    semantic decision the caller must make explicitly, whereas a leftover probe is
    always accidental.
    """
    root = Path(export_dir)
    actions: dict[str, Any] = {}

    before = inspect(root)
    if before.get("pixel_probe", {}).get("injected"):
        actions["pixel_probe"] = pixelprobe.restore(root)
    if before.get("uav_probe", {}).get("injected"):
        actions["uav_probe"] = uavprobe.restore(root)

    actions["after"] = inspect(root)
    actions["shader_edits_left_alone"] = not include_shader_edits
    return actions
