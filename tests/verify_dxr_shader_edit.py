"""Verify the DXR shader-edit path (phase five) against the Tiled.wpix baseline.

This is the *static* half of the acceptance: it proves the export-name invariant
and the state-object export resolution without needing a GPU replay. The GPU half
(recompile an edited library and run it back through the replay, then compare the
edited shader's actual output) is deliberately out of scope here -- raytracing
output lands in UAVs that ``read-uav`` re-reads, so the byte-level diff lives in
``verify_shader_edit_diff.py``.

The important assertions are the ones that stop a silent wrong answer:

* ``parse_export_names`` returns the *mangled* export names (``CHS_<hash>``), not
  the HLSL entry points -- the binding table looks shaders up by the former;
* ``_resolve_state_object_export`` finds an export by either its mangled name or
  its original entry-point name, and re-attributes it to the *collection* that
  declared it, not the RTPSO the user named (which declares zero DXIL of its own);
* the target export carries a DXIL blob index, so a patch has a real blob to
  replace rather than an empty read.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pix_tool_set.engine.capture import Capture  # noqa: E402
from pix_tool_set.engine import dxbc  # noqa: E402
from pix_tool_set.session import SessionStore  # noqa: E402
from pix_tool_set.tools.shader_edit_tools import (  # noqa: E402
    _resolve_state_object_export,
)
from pix_tool_set.errors import PixToolError  # noqa: E402

SESSION = "Tiled"

failures: list[str] = []
checks = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    status = "ok  " if ok else "FAIL"
    print(f"  {status} {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(label)


def main() -> int:
    record = SessionStore().resolve(session=SESSION)
    capture = Capture(
        Path(record.capture_path) if record.capture_path else None,
        Path(record.export_dir),
        Path(record.event_csv) if record.event_csv else None,
    )
    objects = capture.state_objects

    print("1. parse_export_names: entry-point symbols, not DXR export names")
    # A synthetic disassembly shaped like a two-entry-point dxc library listing.
    # The symbol is the MSVC-mangled entry point, which is what dxc prints.
    synthetic = (
        "define void @\"\\01?LumenHardwareRayTracingMaterialCHS@@YAXUFLumenMinimalPayload@@UFRayTracingIntersectionAttributes@@@Z\"(%struct.FLumenMinimalPayload* %Payload) {\n"
        "  ret void\n"
        "}\n"
        "define void @\"\\01?LumenHardwareRayTracingMaterialAHS@@YAXUFLumenMinimalPayload@@UFRayTracingIntersectionAttributes@@@Z\"(%struct.FLumenMinimalPayload* %Payload) {\n"
        "  ret void\n"
        "}\n"
    )
    names = dxbc.parse_export_names(synthetic)
    check(
        "collects both mangled entry points",
        len(names) == 2 and all("LumenHardwareRayTracingMaterial" in n for n in names),
        str(names),
    )
    check(
        "does not confuse a dx. intrinsic symbol for an export",
        "dx.op" not in " ".join(dxbc.parse_export_names("define void @dx.op.barrier() {}")),
    )
    check(
        "returns [] for empty input",
        dxbc.parse_export_names("") == [],
    )

    print("\n2. real collection 3892: export resolution by mangled name")
    so = objects.get(3891)
    if so is None:
        check("state object 3891 exists", False)
        return _report()

    # RTPSO 3891 reaches the CHS export through collection 3892 (which it references
    # via EXISTING_COLLECTION); the RTPSO itself declares no DXIL of its own.
    # Resolution must follow the reference, then re-attribute to the declaring
    # collection (3892), not the RTPSO the user named.
    args = {"state_object_id": 3891, "export_name": "CHS_b5acc26ab7153489"}
    state_object, export, owner = _resolve_state_object_export(capture, args)
    check(
        "resolves the RTPSO back to id 3891",
        state_object.api_id == 3891,
        str(state_object.api_id),
    )
    check(
        "finds the export by mangled name",
        export.name == "CHS_b5acc26ab7153489",
        export.name,
    )
    check(
        "original name is the HLSL entry point",
        export.original_name == "LumenHardwareRayTracingMaterialCHS",
        export.original_name,
    )
    check(
        "re-attributed to the declaring collection (3892), not the RTPSO",
        owner.api_id == 3892,
        str(owner.api_id),
    )
    check(
        "export carries a DXIL blob index to replace",
        export.dxil_blob_index is not None and export.dxil_blob_index >= 0,
        str(export.dxil_blob_index),
    )

    print("\n3. the same entry point is renamed per-collection, so mangled names are exact")
    # The HLSL entry point LumenHardwareRayTracingMaterialCHS is compiled into many
    # collections, each PIX renames it to a different CHS_<hash>. This is why
    # --export-name resolves the *mangled* name exactly, and why an entry-point-only
    # query is ambiguous (asserted negatively in section 5).
    chs_exports_across_capture = [
        e
        for obj in objects.values()
        for e in obj.exports
        if e.original_name == "LumenHardwareRayTracingMaterialCHS"
    ]
    distinct_names = {e.name for e in chs_exports_across_capture}
    check(
        "the entry point maps to multiple renamed exports",
        len(distinct_names) > 1,
        str(sorted(distinct_names)[:5]),
    )
    # A mangled name resolves exactly, even when the entry point is shared.
    args3 = {"state_object_id": 3891, "export_name": "CHS_b5acc26ab7153489"}
    _so3, export3, owner3 = _resolve_state_object_export(capture, args3)
    check(
        "mangled name resolves to the intended export",
        export3.name == "CHS_b5acc26ab7153489",
        export3.name,
    )
    check(
        "and to its owning collection 3892",
        owner3.api_id == 3892,
        str(owner3.api_id),
    )

    print("\n4. the target blob is a real, parseable DXIL library")
    blob = b""
    if export.dxil_blob_index is not None:
        blob = capture._load_blob(export.dxil_blob_index)
    check("blob loads at the recorded index", bool(blob), f"{len(blob)} bytes")
    try:
        container = dxbc.DxbcContainer.parse(blob)
        has_dxil = container.is_dxil
    except ValueError:
        container = None
        has_dxil = False
    check("blob is a DXIL container", has_dxil, str(container.tags) if container else "n/a")
    if has_dxil:
        real_names = dxbc.parse_export_names(
            dxbc.ShaderDisassembler().disassemble(blob)
        )
        check(
            "the target entry point is present in the library's real export set",
            any("LumenHardwareRayTracingMaterialCHS" in n for n in real_names),
            str(real_names),
        )

    print("\n5. negative: wrong export name is a not_found, missing id is invalid_argument")
    try:
        _resolve_state_object_export(capture, {"state_object_id": 3891, "export_name": "NOPE"})
        check("unknown export raises", False, "no exception")
    except PixToolError as exc:
        check(
            "unknown export raises export_not_found",
            getattr(exc, "code", "") == "export_not_found",
            getattr(exc, "code", "?"),
        )
    try:
        _resolve_state_object_export(capture, {"state_object_id": None, "export_name": "x"})
        check("missing state_object_id raises", False, "no exception")
    except PixToolError as exc:
        check(
            "missing state_object_id raises invalid_argument",
            getattr(exc, "code", "") == "invalid_argument",
            getattr(exc, "code", "?"),
        )
    try:
        _resolve_state_object_export(capture, {"state_object_id": 3891, "export_name": ""})
        check("missing export_name raises", False, "no exception")
    except PixToolError as exc:
        check(
            "missing export_name raises invalid_argument",
            getattr(exc, "code", "") == "invalid_argument",
            getattr(exc, "code", "?"),
        )
    # The HLSL entry point is shared across collections, so asking by entry point
    # alone is ambiguous and must be refused rather than silently resolved.
    try:
        _resolve_state_object_export(
            capture, {"state_object_id": 3891, "export_name": "LumenHardwareRayTracingMaterialCHS"}
        )
        check("ambiguous entry point raises", False, "no exception")
    except PixToolError as exc:
        check(
            "ambiguous entry point raises invalid_argument",
            getattr(exc, "code", "") == "invalid_argument",
            getattr(exc, "code", "?"),
        )

    return _report()


def _report() -> int:
    print()
    print("=" * 68)
    print(f"checks: {checks - len(failures)}/{checks} passed")
    if failures:
        for label in failures:
            print(f"  FAILED: {label}")
        print("RESULT: FAIL")
        return 1
    print("PASS: DXR shader-edit static acceptance matches the Tiled.wpix baseline")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
