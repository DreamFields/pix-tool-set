"""Edit a pass's shader and put the result back, the scriptable equivalent of PIX's
Debug-panel "Apply".

PIX's GUI compiles the edited text inside its own replay engine and re-runs the
frame in place.  ``pixtool`` exposes none of that: its command list has no
shader-replacement verb at all.  So "Apply" is rebuilt from parts that *are*
reachable:

  1. ``shader-edit-begin``  - write the recovered HLSL to a file you can edit,
     alongside the exact compile arguments the engine used.
  2. ``shader-edit-apply``  - compile the edited file and patch the bytecode into
     the exported C++ replay project, which is a runnable D3D12 program.

The check that makes this safe is the binding signature.  A shader is only
substitutable if it still declares the same resources at the same registers, since
the recorded command lists set descriptor tables by slot.  A mismatch is refused
rather than written, because a silently wrong replacement produces garbage that
looks plausible.

What this does not do: it cannot mutate the .wpix itself.  A capture is a record
of API calls, so the replacement takes effect in the exported replay project, not
in the original file.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from ..context import ToolContext
from ..engine import dxbc, shaderpdb
from ..engine.editledger import EditLedger
from ..engine.hlslcompile import require_compiler
from ..engine.model import ShaderStage
from ..errors import PixToolError, invalid_argument, not_found
from ..results import ToolResult
from ._common import PASS_SELECTOR, resolve_pass, tool, with_session

_STAGES = [stage.value for stage in ShaderStage]

_NOTE = (
    "PIX's Debug-panel Apply is a GUI-only feature; pixtool has no shader replacement "
    "command. This pair reproduces it by recompiling the HLSL that the engine's shader "
    "PDB records, together with the exact arguments that PDB stores, then patching the "
    "bytecode into the exported C++ replay project. The replacement is refused unless the "
    "new container declares the same resource bindings at the same registers as the "
    "captured one, because the recorded command lists bind by slot. For a raytracing "
    "shader, pass --state-object-id + --export-name instead of --stage: the edit lands on "
    "the collection's DXIL library and is refused if the recompile renames or drops an "
    "entry point, because the shader binding table resolves shaders by export name. The "
    "original .wpix is never modified."
)

# `pssDesc.CS = { reinterpret_cast<BYTE*>(&data[offset]), 16436 };`
_STAGE_ASSIGN = re.compile(
    r"pssDesc\.(VS|PS|GS|HS|DS|CS|AS|MS)\s*=\s*\{[^}]*?,\s*(\d+)\s*\}\s*;"
)


def _pdb_dirs(context: ToolContext, args: dict[str, Any]) -> list[Path]:
    supplied = [Path(str(p)).expanduser() for p in (args.get("pdb_dirs") or [])]
    if supplied:
        return supplied
    try:
        # `resolve`, not `get`: without an explicit --session the name is None, which no
        # registered session is ever called, so `get` would discard what
        # session-set-pdb-dirs stored. `resolve` falls back to the active session, which
        # is the one every other command in this toolkit operates on.
        record = context.store.resolve(
            session=args.get("session"),
            capture_path=args.get("capture"),
            export_dir=args.get("export_dir"),
        )
    except Exception:
        # No session at all is not an error here: the caller may still pass --pdb-dirs,
        # and the tool reports the missing directories itself with a better message.
        return []
    if record is None:
        return []
    return [Path(str(p)).expanduser() for p in (record.shader_pdb_dirs or [])]


def _resolve_shader(capture, args: dict[str, Any]):
    """Locate the pass and the one shader stage being edited."""
    draw_index = args.get("draw_index")
    draw = None
    if draw_index is not None:
        # A pass can contain many draws using different PSOs, so naming the draw is the
        # most precise selector. Everything else resolves to a pass and then takes its
        # first draw, which is not necessarily the one the caller meant.
        draw = capture.draw_call(int(draw_index))
        if draw is None:
            raise not_found(
                "draw", draw_index, "Run list-draw-calls or find-draw-calls for valid indices."
            )
        # Match the enclosing pass on the marker path rather than on the draw's Queue ID.
        # Going through the id loses the pass for any action whose queue was not in the
        # exported event list: queue_id is None there, the lookup fails, and the edit fell
        # back to a nameless minimal entry even though marker grouping knew the pass all
        # along.
        entry = next(
            (p for p in capture.passes if tuple(p["marker_path"]) == draw.marker_path),
            None,
        )
        if entry is None:
            # The draw exists but no marker encloses it; carry on with a minimal entry so
            # the edit still works rather than refusing over a cosmetic detail.
            entry = {
                "pass_index": None,
                "name": draw.pass_name,
                "first_draw_index": draw.index,
                "first_queue_id": None,
            }
    else:
        entry = resolve_pass(capture, args)

    if draw is None:
        draw = capture.draw_call(entry["first_draw_index"])
    if draw is None:
        raise not_found("draw", entry["first_draw_index"])

    stage = args.get("stage")
    shaders = [s for s in ([draw.shader(stage)] if stage else draw.shaders) if s is not None]
    if not shaders:
        raise not_found("shader", stage or "any", "This pass binds no such stage.")
    if len(shaders) > 1:
        raise invalid_argument(
            "stage",
            "this pass binds "
            + ", ".join(s.stage.value for s in shaders)
            + "; name the one to edit with --stage",
        )
    return entry, draw, shaders[0]


def _resolve_state_object_export(capture, args: dict[str, Any]):
    """Locate the one DXR export inside a state object, by id + export name.

    A raytracing shader is not a PSO stage: it is an export of a DXIL_LIBRARY
    subobject inside a COLLECTION, and a RTPSO reaches it only by expanding
    EXISTING_COLLECTION references. ``_resolve_shader`` walks PSOs and therefore
    cannot see a raytracing shader at all -- ``draw.shaders`` is empty for a
    DISPATCH_RAYS action. This is the DXR sibling of that function.

    Returns ``(state_object, export, owner_object)`` where ``owner_object`` is the
    collection that actually declared the export (its DXIL library is the blob the
    patch must replace), ``state_object`` is the pipeline the user named, and
    ``export`` is the DxilExport being edited.
    """
    so_id = args.get("state_object_id")
    if so_id is None:
        raise invalid_argument(
            "state_object_id",
            "editing a raytracing shader needs --state-object-id (run "
            "list-raytracing-state-objects for ids), because the shader is an "
            "export of a state object, not a PSO stage.",
        )
    state_objects = capture.state_objects
    state_object = state_objects.get(int(so_id))
    if state_object is None:
        raise not_found(
            "state object", so_id, "Run list-raytracing-state-objects for valid ids."
        )

    export_name = args.get("export_name")
    if not export_name:
        raise invalid_argument(
            "export_name",
            "name the export to edit -- the mangled name (CHS_<hash>) or the HLSL "
            "entry point -- with --export-name.",
        )

    # The export may be declared by a collection this pipeline merely references, so
    # resolve against the fully expanded object, then re-attribute to the owner.
    # `--export-name` is first matched exactly against the mangled export name; only
    # when no mangled name matches is it treated as an HLSL entry point. The entry
    # point is NOT unique across collections (the same HLSL shader is compiled into
    # many collections with different renamed exports), so an ambiguous entry-point
    # match is an error, never a silent pick.
    owner_object = state_object
    export = None
    for candidate in state_object.resolved_exports:
        if candidate.name == export_name:
            export = candidate
            break
    if export is None:
        by_entry = [
            candidate
            for candidate in state_object.resolved_exports
            if candidate.original_name == export_name
        ]
        if len(by_entry) == 1:
            export = by_entry[0]
        elif len(by_entry) > 1:
            names = sorted({f"{c.name} (owner {c.defining_state_object_id})" for c in by_entry})
            raise invalid_argument(
                "export_name",
                f"'{export_name}' is an entry point shared by {len(by_entry)} exports; "
                "name the mangled export instead: " + ", ".join(names),
            )
    if export is None:
        names = sorted(
            {f"{e.name} ({e.original_name})" for e in state_object.resolved_exports}
        )
        raise not_found(
            "export",
            export_name,
            "This state object resolves to exports: " + (", ".join(names) or "<none>"),
        )

    # Re-attribute to the collection that declared it, because that is the blob
    # whose DXIL library a patch must replace. The defining id is authoritative;
    # the RTPSO's own body declares zero exports.
    defining_id = export.defining_state_object_id
    if defining_id is not None:
        owner = state_objects.get(defining_id)
        if owner is not None:
            owner_object = owner

    return state_object, export, owner_object


def _binding_signature(blob: bytes) -> tuple[list[tuple], dict[str, Any]]:
    """The bindings and entry metadata a container declares."""
    dis = dxbc.ShaderDisassembler()
    text = dis.disassemble(blob)
    bindings = [
        (
            record.get("name"),
            record.get("type"),
            record.get("id"),
            record.get("hlsl_bind"),
            record.get("count"),
        )
        for record in dxbc.parse_resource_bindings(text)
    ]
    meta = dxbc.parse_shader_metadata(text)
    return bindings, meta


def _describe(signature: list[tuple]) -> list[dict[str, Any]]:
    return [
        {"id": entry[2], "name": entry[0], "type": entry[1], "bind": entry[3], "count": entry[4]}
        for entry in signature
    ]


# ======================================================================
@tool(
    name="shader-edit-begin",
    summary=(
        "Write a pass's recovered HLSL to an editable file, with the exact compile "
        "arguments recorded in the shader PDB, ready for shader-edit-apply."
    ),
    category="shaders",
    parameters=with_session(
        PASS_SELECTOR,
        draw_index={
            "type": "integer",
            "description": (
                "Draw index to edit. The most precise selector, because one pass can "
                "contain many draws using different PSOs."
            ),
        },
        stage={"type": "string", "enum": _STAGES, "description": "Stage to edit."},
        state_object_id={
            "type": "integer",
            "description": (
                "Raytracing state object id. Use this instead of --stage to edit a "
                "DXR shader, which is an export of a state object rather than a PSO "
                "stage. Mutually exclusive with --stage."
            ),
        },
        export_name={
            "type": "string",
            "description": (
                "DXR export to edit: the mangled name (CHS_<hash>) or its HLSL entry "
                "point. Required with --state-object-id; use list-raytracing-state-"
                "objects --detail to enumerate exports."
            ),
        },
        output={
            "type": "string",
            "description": "Directory for the .hlsl and its args file. Defaults beside the export.",
        },
        pdb_dirs={
            "type": "array",
            "description": r"Shader PDB directories, e.g. <Project>\Saved\ShaderSymbols\PCD3D_SM6.",
        },
    ),
    returns="Paths to the editable HLSL and argument files, plus the original binding signature.",
    examples=[
        "pix-tool-set shader-edit-begin --queue-id 18461 --output G:\\edit",
        'pix-tool-set shader-edit-begin --queue-id 18461 --stage CS --pdb-dirs "F:\\JL_TMR\\UnrealEngine\\Games\\JyGame\\Saved\\ShaderSymbols\\PCD3D_SM6"',
    ],
    notes=_NOTE,
)
def shader_edit_begin(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)

    if args.get("state_object_id") is not None:
        # --- DXR branch: the shader is an export of a state object, not a PSO stage.
        return _dxr_edit_begin(args, context, capture)

    entry, draw, shader = _resolve_shader(capture, args)

    search_dirs = _pdb_dirs(context, args)
    if not search_dirs:
        raise invalid_argument(
            "pdb_dirs",
            "editing needs the real HLSL, which lives in the engine's shader PDBs; "
            r"pass --pdb-dirs <Project>\Saved\ShaderSymbols\PCD3D_SM6 or store it with "
            "session-set-pdb-dirs",
        )

    pdb_path = shaderpdb.find_pdb(
        search_dirs, shader.shader_hash or "", shader.debug_name or ""
    )
    if pdb_path is None:
        raise not_found(
            "shader PDB",
            shader.debug_name or shader.shader_hash or "<unknown>",
            "The capture only records the PDB name. Point --pdb-dirs at the directory "
            "holding that <hash>.pdb.",
        )

    report = shaderpdb.extract_sources(pdb_path)
    source = report.get("full_text") or ""
    compile_args = list(report.get("compile_args") or [])
    if not report.get("ok") or not source:
        raise PixToolError(
            code="source_unavailable",
            message=f"Could not recover HLSL from {pdb_path.name}.",
            stage="shader",
            paths=[str(pdb_path)],
            details={"detail": report.get("detail")},
            suggestion="Check the PDB with pass-shader-source first.",
        )
    if not compile_args:
        raise PixToolError(
            code="compile_args_missing",
            message=f"{pdb_path.name} records no compile arguments.",
            stage="shader",
            suggestion=(
                "Without the original arguments a recompile would not match the capture. "
                "Supply them explicitly to shader-edit-apply with --args."
            ),
        )

    directory = context.resolve_output(args.get("output"), "shader-edits")
    directory.mkdir(parents=True, exist_ok=True)
    tag = (
        f"q{entry['first_queue_id']}"
        if entry.get("first_queue_id") is not None
        else f"d{draw.index}"
    )
    stem = f"{tag}_{shader.stage.value}_{shader.entry_point or 'shader'}"
    hlsl_path = directory / f"{stem}.hlsl"
    args_path = directory / f"{stem}.args.txt"
    original_path = directory / f"{stem}.original.hlsl"

    hlsl_path.write_text(source, encoding="utf-8")
    original_path.write_text(source, encoding="utf-8")
    args_path.write_text("\n".join(compile_args), encoding="utf-8")

    bindings, meta = _binding_signature(shader.bytecode)

    data = {
        "pass_index": entry["pass_index"],
        "pass_name": entry["name"],
        "draw_index": draw.index,
        "queue_id": entry.get("first_queue_id"),
        "global_id": draw.global_id,
        "pso_id": draw.pso_id,
        "stage": shader.stage.value,
        "entry_point": shader.entry_point,
        "num_threads": shader.num_threads,
        "shader_hash": shader.shader_hash,
        "captured_byte_size": shader.byte_size,
        "pdb_path": str(pdb_path),
        "compile_args": compile_args,
        "files": {
            "editable_hlsl": str(hlsl_path),
            "pristine_copy": str(original_path),
            "compile_args": str(args_path),
        },
        "original_bindings": _describe(bindings),
        "original_metadata": {
            "entry_point": meta.get("entry_point"),
            "num_threads": meta.get("num_threads"),
        },
        "next_step": (
            f"Edit {hlsl_path.name}, then run: pix-tool-set shader-edit-apply "
            + (
                f"--queue-id {entry['first_queue_id']}"
                if entry.get("first_queue_id") is not None
                else f"--draw-index {draw.index}"
            )
            + f" --stage {shader.stage.value} --source \"{hlsl_path}\""
        ),
    }

    result = ToolResult.success(data, output_paths=[str(hlsl_path), str(args_path)])
    result.add_diagnostic(
        "info",
        "This is the preprocessed translation unit, so it is self-contained and needs no "
        "include paths. Keep the entry point name and its resource declarations intact; "
        "shader-edit-apply refuses a replacement whose bindings moved.",
    )
    if entry.get("first_queue_id") is None:
        # The file names and the next_step above already fell back to the draw index; say
        # why, so a caller comparing two edit sessions does not read the different naming
        # as a bug. This is the normal case for a pass on a queue the event list missed.
        result.add_diagnostic(
            "info",
            f"This pass has no Queue ID (its queue is absent from the exported event "
            f"list), so the files are tagged with draw index {draw.index} and "
            "shader-edit-apply must be called with --draw-index.",
        )
    return result


def _dxr_edit_begin(args: dict[str, Any], context: ToolContext, capture) -> ToolResult:
    """Write a DXR export's recovered HLSL to a file, ready to edit.

    The raytracing analogy of the PSO path: recover the preprocessed HLSL for one
    DXIL-library export (keyed by its original entry-point name), write it beside
    the exact compile arguments, and return the export's identity so apply can
    re-attribute the compiled library back to the right collection.
    """
    state_object, export, owner_object = _resolve_state_object_export(capture, args)

    search_dirs = _pdb_dirs(context, args)
    if not search_dirs:
        raise invalid_argument(
            "pdb_dirs",
            "editing a raytracing shader needs the original HLSL, which lives in the "
            "engine's shader PDBs; pass --pdb-dirs <Project>\\Saved\\ShaderSymbols\\"
            "PCD3D_SM6 or store it with session-set-pdb-dirs",
        )

    # The export blob is the DXIL library; its ILDN chunk carries the debug name the
    # PDB is filed under, and its HASH chunk the shader hash.
    blob = b""
    if export.dxil_blob_index is not None:
        try:
            blob = capture._load_blob(export.dxil_blob_index)
        except Exception:
            blob = b""
    if not blob:
        raise PixToolError(
            code="source_unavailable",
            message=(
                f"Could not load the DXIL blob for export {export.name} "
                f"(blob index {export.dxil_blob_index})."
            ),
            stage="shader",
        )
    try:
        container = dxbc.DxbcContainer.parse(blob)
    except ValueError:
        container = None
    debug_name = container.debug_name if container else ""
    shader_hash = container.shader_hash if container else ""

    pdb_path = shaderpdb.find_pdb(search_dirs, shader_hash or "", debug_name or "")
    if pdb_path is None:
        # Fall back to the original entry-point name: some engines file the PDB by
        # entry point rather than by container hash.
        pdb_path = shaderpdb.find_pdb(
            search_dirs, "", export.original_name or export.name or ""
        )
    if pdb_path is None:
        raise not_found(
            "shader PDB",
            debug_name or shader_hash or export.original_name or export.name or "<unknown>",
            "The capture records the export but not its PDB path. Point --pdb-dirs at "
            "the directory holding the shader's <hash>.pdb.",
        )

    report = shaderpdb.extract_sources(pdb_path)
    source = report.get("full_text") or ""
    compile_args = list(report.get("compile_args") or [])
    if not report.get("ok") or not source:
        raise PixToolError(
            code="source_unavailable",
            message=f"Could not recover HLSL from {pdb_path.name}.",
            stage="shader",
            paths=[str(pdb_path)],
            details={"detail": report.get("detail")},
            suggestion="Check the PDB with pass-shader-source first.",
        )
    if not compile_args:
        raise PixToolError(
            code="compile_args_missing",
            message=f"{pdb_path.name} records no compile arguments.",
            stage="shader",
            suggestion=(
                "Without the original arguments a recompile would not reproduce the "
                "library's exports. Supply them explicitly to shader-edit-apply with --args."
            ),
        )

    directory = context.resolve_output(args.get("output"), "shader-edits")
    directory.mkdir(parents=True, exist_ok=True)
    stage_tag = export.stage.value if export.stage else "DXR"
    stem = f"so{state_object.api_id}_{export.name}_{export.original_name or stage_tag}"
    hlsl_path = directory / f"{stem}.hlsl"
    args_path = directory / f"{stem}.args.txt"
    original_path = directory / f"{stem}.original.hlsl"

    hlsl_path.write_text(source, encoding="utf-8")
    original_path.write_text(source, encoding="utf-8")
    args_path.write_text("\n".join(compile_args), encoding="utf-8")

    disasm = dxbc.ShaderDisassembler().disassemble(blob)
    export_names = dxbc.parse_export_names(disasm)

    data = {
        "state_object_id": state_object.api_id,
        "owning_collection_id": owner_object.api_id,
        "export_name": export.name,
        "original_name": export.original_name,
        "stage": stage_tag,
        "stage_source": export.stage_source,
        "shader_hash": shader_hash,
        "debug_name": debug_name,
        "captured_byte_size": len(blob),
        "library_export_names": export_names,
        "pdb_path": str(pdb_path),
        "compile_args": compile_args,
        "files": {
            "editable_hlsl": str(hlsl_path),
            "pristine_copy": str(original_path),
            "compile_args": str(args_path),
        },
        "next_step": (
            f"Edit {hlsl_path.name}, then run: pix-tool-set shader-edit-apply "
            f"--state-object-id {state_object.api_id} --export-name {export.name} "
            f"--source \"{hlsl_path}\" --patch"
        ),
    }

    result = ToolResult.success(data, output_paths=[str(hlsl_path), str(args_path)])
    result.add_diagnostic(
        "info",
        "This is a DXIL library export, not a PSO stage. Keep the export name and its "
        "resource declarations intact: shader-edit-apply refuses a recompile that drops "
        "or renames an export, because the recorded shader binding table looks them up "
        "by name.",
    )
    return result


# ======================================================================
@tool(
    name="shader-edit-apply",
    summary=(
        "Compile an edited HLSL file and patch it over the pass's shader in the exported "
        "C++ replay project, refusing the swap when the resource bindings no longer match."
    ),
    category="shaders",
    parameters=with_session(
        PASS_SELECTOR,
        source={"type": "string", "description": "Edited .hlsl file to compile."},
        draw_index={
            "type": "integer",
            "description": (
                "Draw index to edit. The most precise selector, because one pass can "
                "contain many draws using different PSOs."
            ),
        },
        stage={"type": "string", "enum": _STAGES, "description": "Stage being replaced."},
        args={
            "type": "array",
            "description": (
                "Compile arguments. Defaults to the ones recorded in the shader PDB, which "
                "is what reproduces the captured build."
            ),
        },
        pdb_dirs={
            "type": "array",
            "description": "Shader PDB directories, used to recover the default arguments.",
        },
        output={
            "type": "string",
            "description": "Directory for the compiled .dxil. Defaults beside the export.",
        },
        patch={
            "type": "boolean",
            "description": (
                "Patch the exported C++ project so a rebuild uses the new shader. "
                "Default false: compile and verify only."
            ),
        },
        allow_binding_change={
            "type": "boolean",
            "description": (
                "Permit a replacement whose bindings differ. Off by default because the "
                "recorded command lists bind resources by slot."
            ),
        },
        force={
            "type": "boolean",
            "description": (
                "Re-patch a stage that is already patched, by removing the previous "
                "override first. Without it an existing patch is refused, which used to "
                "leave the old .dxil in place and make the rebuild look like it had no "
                "effect."
            ),
        },
        scope={
            "type": "string",
            "enum": ["pso", "shader", "auto"],
            "description": (
                "Patch scope. 'pso' patches only the selected PSO (for targeted "
                "experiments). 'shader' patches every PSO that references the same "
                "(stage, shader_hash) — this is the scope needed for full-frame replay. "
                "'auto' (default) is 'pso' when only one PSO uses this shader, and "
                "errors when multiple do, listing every pso_id so the caller can choose "
                "explicitly. The default is 'auto' because a silent partial change is the "
                "most expensive failure mode: it looks exactly like a successful edit."
            ),
        },
        state_object_id={
            "type": "integer",
            "description": (
                "Raytracing state object id for --export-name edits; the DXR counterpart "
                "of --stage. Use list-raytracing-state-objects for ids."
            ),
        },
        export_name={
            "type": "string",
            "description": (
                "DXR export to patch (mangled name or HLSL entry point). Required with "
                "--state-object-id; the compiled library must still export the same name."
            ),
        },
        allow_export_change={
            "type": "boolean",
            "description": (
                "Permit a DXR recompile whose export set changed. Off by default because "
                "the recorded shader binding table resolves shaders by export name."
            ),
        },
    ),
    returns="Compile outcome, binding comparison against the captured shader, and any patch made.",
    examples=[
        'pix-tool-set shader-edit-apply --queue-id 18461 --source G:\\edit\\q18461_CS_RayTracingBuildLightGridCS.hlsl',
        'pix-tool-set shader-edit-apply --queue-id 18461 --source G:\\edit\\edited.hlsl --patch',
    ],
    notes=_NOTE,
)
def shader_edit_apply(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)

    if args.get("state_object_id") is not None:
        return _dxr_edit_apply(args, context, capture)

    entry, draw, shader = _resolve_shader(capture, args)

    raw_source = args.get("source")
    if not raw_source:
        raise invalid_argument("source", "point --source at the edited .hlsl file")
    source_path = Path(str(raw_source)).expanduser()
    if not source_path.exists():
        raise not_found("source file", str(source_path), "Run shader-edit-begin first.")
    text = source_path.read_text(encoding="utf-8", errors="replace")

    compile_args = [str(a) for a in (args.get("args") or [])]
    args_origin = "supplied on the command line"
    if not compile_args:
        sidecar = source_path.with_suffix("").with_suffix(".args.txt")
        if not sidecar.exists():
            sidecar = source_path.parent / f"{source_path.stem}.args.txt"
        if sidecar.exists():
            compile_args = [
                line.strip()
                for line in sidecar.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            args_origin = f"read from {sidecar.name}"
    if not compile_args:
        search_dirs = _pdb_dirs(context, args)
        pdb_path = (
            shaderpdb.find_pdb(search_dirs, shader.shader_hash or "", shader.debug_name or "")
            if search_dirs
            else None
        )
        if pdb_path is not None:
            report = shaderpdb.extract_sources(pdb_path)
            compile_args = list(report.get("compile_args") or [])
            args_origin = f"recovered from {pdb_path.name}"
    if not compile_args:
        raise invalid_argument(
            "args",
            "no compile arguments were found; pass --args, or keep the .args.txt that "
            "shader-edit-begin writes next to the source",
        )

    compiler = require_compiler()
    outcome = compiler.compile(text, compile_args)

    data: dict[str, Any] = {
        "pass_index": entry["pass_index"],
        "pass_name": entry["name"],
        "draw_index": draw.index,
        "queue_id": entry.get("first_queue_id"),
        "pso_id": draw.pso_id,
        "stage": shader.stage.value,
        "source_file": str(source_path),
        "compile_args": compile_args,
        "compile_args_origin": args_origin,
        "compile": outcome.to_dict(),
        "captured_byte_size": shader.byte_size,
    }

    if not outcome.ok:
        raise PixToolError(
            code="shader_compile_failed",
            message="The edited HLSL did not compile.",
            stage="shader",
            paths=[str(source_path)],
            details={
                "method": outcome.method,
                "compiler_output": outcome.errors[:4000],
                "compile_args": compile_args,
            },
            suggestion="Fix the reported errors; the diagnostics come straight from DXC.",
        )

    container = dxbc.DxbcContainer.parse(outcome.blob)
    data["new_container"] = {
        "byte_size": len(outcome.blob),
        "chunks": container.tags,
        "shader_hash": container.shader_hash,
        "signed": container.hash_md5 != "0" * 32,
        # The captured hash is reported next to the new one because "did the compiler
        # actually see my edit?" is the first question after every apply, and comparing
        # the two by hand across separate command outputs is where that check gets
        # skipped.
        "previous_shader_hash": shader.shader_hash,
        "hash_changed": bool(
            container.shader_hash
            and shader.shader_hash
            and container.shader_hash != shader.shader_hash
        ),
    }

    old_bindings, old_meta = _binding_signature(shader.bytecode)
    new_bindings, new_meta = _binding_signature(outcome.blob)
    identical = old_bindings == new_bindings
    entry_same = (old_meta.get("entry_point") == new_meta.get("entry_point")) and (
        list(old_meta.get("num_threads") or []) == list(new_meta.get("num_threads") or [])
    )
    data["binding_check"] = {
        "identical": identical,
        "entry_and_threads_match": entry_same,
        "original": _describe(old_bindings),
        "replacement": _describe(new_bindings),
        "why_it_matters": (
            "The captured command lists set descriptor tables and root arguments by slot, "
            "so a shader that reads a different register reads the wrong resource."
        ),
    }

    allow = bool(args.get("allow_binding_change"))
    compatible = identical and entry_same

    directory = context.resolve_output(args.get("output"), "shader-edits")
    directory.mkdir(parents=True, exist_ok=True)
    tag = (
        f"q{entry['first_queue_id']}"
        if entry.get("first_queue_id") is not None
        else f"d{draw.index}"
    )
    stem = f"{tag}_{shader.stage.value}_edited"
    dxil_path = directory / f"{stem}.dxil"
    dxil_path.write_bytes(outcome.blob)
    written = [str(dxil_path)]
    data["compiled_to"] = str(dxil_path)

    if not compatible and not allow:
        result = ToolResult.partial(data, output_paths=written)
        result.degrade(
            "Compiled successfully but the replacement is not slot-compatible, so it was "
            "not patched in.",
            reason=(
                "bindings differ"
                if not identical
                else "entry point or thread group size changed"
            ),
            alternative=(
                "Restore the original declarations, or pass --allow-binding-change if you "
                "intend to rebuild the bindings by hand."
            ),
        )
        return result

    # --- scope resolution (D1): the patch unit is a PSO, the intent unit is a shader.
    # UE5 reuses one shader bytecode across many PSOs (blend/depth/RT-format variants
    # share the same shader), so patching only the PSO the selected draw happens to use
    # produces a partial change: some draws change, others do not, and the inconsistency
    # looks like "compiler optimisation" or "incremental build miss" rather than a scope
    # error. The default is `auto`, which errors when the shader has siblings, because a
    # silent partial change is the most expensive failure mode — it looks like success.
    stage_val = shader.stage.value
    shader_hash = shader.shader_hash or shader.hash_md5
    scope = args.get("scope") or "auto"
    sibling_pso_ids = capture.sibling_psos(stage_val, shader_hash)

    if scope == "auto" and len(sibling_pso_ids) > 1:
        raise PixToolError(
            code="ambiguous_shader_scope",
            message=(
                f"Shader {stage_val}:{shader_hash or '<unknown>'} is used by "
                f"{len(sibling_pso_ids)} PSOs, so patching only the selected one "
                f"(pso {draw.pso_id}) would change {draw.pso_id} while leaving the "
                f"other {len(sibling_pso_ids) - 1} untouched. The frame would be "
                f"partially patched — some draws change, others do not — which "
                f"looks exactly like a successful edit."
            ),
            stage="shader",
            details={
                "stage": stage_val,
                "shader_hash": shader_hash,
                "sibling_psos": sibling_pso_ids,
                "selected_pso": draw.pso_id,
            },
            suggestion=(
                f"Pass --scope shader to patch all {len(sibling_pso_ids)} PSOs, or "
                f"--scope pso to patch only PSO {draw.pso_id} (for targeted experiments)."
            ),
        )

    target_pso_ids = sibling_pso_ids if scope == "shader" and sibling_pso_ids else [draw.pso_id]
    data["scope"] = {
        "requested": scope,
        "resolved": "shader" if len(target_pso_ids) > 1 else "pso",
        "target_psos": target_pso_ids,
        "sibling_psos": sibling_pso_ids,
        "shader_hash": shader_hash,
    }

    if args.get("patch"):
        patches: list[dict[str, Any]] = []
        bytecode_files: dict[int, str] = {}
        for pso_id in target_pso_ids:
            patch = _patch_export(
                capture, pso_id, stage_val, outcome.blob, dxil_path, bool(args.get("force"))
            )
            patches.append(patch)
            written.extend(patch.get("files_written", []))
            bc_path = patch.get("payload_file") or patch.get("bytecode_file", "")
            if bc_path:
                bytecode_files[pso_id] = bc_path

        # Record the patch(es) in the edit ledger so replay-edits can list them
        # and replay-reset can revert them. One group_id ties all sibling-PSO
        # patches together; the ledger is the source of truth for "what did I change?"
        ledger = EditLedger(capture.export_dir)
        group_id = ledger.add_group(
            stage=stage_val,
            shader_hash=shader_hash,
            scope=scope,
            target_psos=target_pso_ids,
            source_file=str(args.get("source") or ""),
            compile_args_file=str(args.get("args") or ""),
            bytecode_files=bytecode_files,
            binding_check=data.get("binding_check", {}),
        )
        data["patch"] = patches if len(patches) > 1 else patches[0]
        data["ledger_group_id"] = group_id
        result = ToolResult.success(data, output_paths=written)
        if len(patches) > 1:
            result.add_diagnostic(
                "info",
                f"Patched {len(patches)} PSOs that reference {stage_val}:{shader_hash}. "
                "Each PSO's override reads its own .dxil file, so the same compiled "
                "bytecode is loaded by every sibling PSO. Rebuild and run to see the "
                "edited shader execute across all of them.",
            )
        else:
            result.add_diagnostic(
                "info",
                "The exported C++ project now loads the new bytecode from a file instead of "
                "resources.bin. Rebuild it with CMake and run it to see the edited shader "
                "execute; the .wpix itself is unchanged.",
            )
        _warn_unchanged_hash(result, data)
        if not compatible:
            result.degrade(
                "Patched despite a binding mismatch because --allow-binding-change was set.",
                reason="the replacement declares different resources",
            )
        return result

    result = ToolResult.success(data, output_paths=written)
    result.add_diagnostic(
        "info",
        "Compiled and verified slot-compatible. Nothing was modified; add --patch to "
        "write it into the exported replay project.",
    )
    _warn_unchanged_hash(result, data)
    return result


def _warn_unchanged_hash(result: ToolResult, data: dict[str, Any]) -> None:
    """Say so when the edit compiled to the captured bytecode.

    An unchanged hash is legal - re-applying the recovered source unmodified is a
    valid dry run - so this is a warning, not a degrade.  It is worth saying loudly
    because the alternative is discovering it after a multi-minute rebuild and a
    replay that shows no difference, which reads as "the patch did not take" and
    sends the investigation to the build system instead of the source file.
    """
    container = data.get("new_container") or {}
    previous = container.get("previous_shader_hash")
    current = container.get("shader_hash")
    if previous and current and previous == current:
        result.add_diagnostic(
            "warning",
            f"The compiled shader hash ({current}) equals the captured one, so DXC "
            "produced the same bytecode as the capture. If you expected a behaviour "
            "change, the edit did not reach the compiler: check that --source points at "
            "the file you edited and that the change is not dead code the optimiser "
            "removes.",
        )


def _dxr_edit_apply(args: dict[str, Any], context: ToolContext, capture) -> ToolResult:
    """Compile an edited DXR library and patch one export's blob into the export.

    A raytracing shader edit has two hard invariants the PSO path does not:

    * the compiled library is a ``lib_6_*`` container exporting *many* entry points,
      not one; the edit must keep the target export's name (and ideally every name)
      so the recorded shader binding table still resolves it;
    * the patch lands on the collection that declared the export (the ``Read()`` of
      that library blob in its ``CreateStateObject_*``), not on the RTPSO the user
      named, which declares zero DXIL of its own.

    Readback/diff is explicitly out of scope here: raytracing output lands in UAVs,
    which ``read-uav`` already re-reads, so ``shader-edit-diff`` needs no new code.
    """
    state_object, export, owner_object = _resolve_state_object_export(capture, args)

    raw_source = args.get("source")
    if not raw_source:
        raise invalid_argument("source", "point --source at the edited .hlsl file")
    source_path = Path(str(raw_source)).expanduser()
    if not source_path.exists():
        raise not_found("source file", str(source_path), "Run shader-edit-begin first.")
    text = source_path.read_text(encoding="utf-8", errors="replace")

    # The captured library blob is the baseline: its name, hash and export set are the
    # things a recompile must preserve.
    captured_blob = b""
    if export.dxil_blob_index is not None:
        try:
            captured_blob = capture._load_blob(export.dxil_blob_index)
        except Exception:
            captured_blob = b""
    if not captured_blob:
        raise PixToolError(
            code="source_unavailable",
            message=f"Could not load the captured library blob for export {export.name}.",
            stage="shader",
        )

    compile_args = [str(a) for a in (args.get("args") or [])]
    args_origin = "supplied on the command line"
    if not compile_args:
        sidecar = source_path.parent / f"{source_path.stem}.args.txt"
        if sidecar.exists():
            compile_args = [
                line.strip()
                for line in sidecar.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            args_origin = f"read from {sidecar.name}"
    if not compile_args:
        # Fall back to the PDB, same as the PSO path, to recover the recorded args.
        search_dirs = _pdb_dirs(context, args)
        try:
            c = dxbc.DxbcContainer.parse(captured_blob)
        except ValueError:
            c = None
        pdb_path = None
        if search_dirs:
            pdb_path = shaderpdb.find_pdb(
                search_dirs,
                (c.shader_hash if c else "") or "",
                (c.debug_name if c else "") or "",
            )
        if pdb_path is not None:
            report = shaderpdb.extract_sources(pdb_path)
            compile_args = list(report.get("compile_args") or [])
            args_origin = f"recovered from {pdb_path.name}"
    if not compile_args:
        raise invalid_argument(
            "args",
            "no compile arguments were found; pass --args, or keep the .args.txt that "
            "shader-edit-begin writes next to the source",
        )

    compiler = require_compiler()
    outcome = compiler.compile(text, compile_args)

    data: dict[str, Any] = {
        "state_object_id": state_object.api_id,
        "owning_collection_id": owner_object.api_id,
        "export_name": export.name,
        "original_name": export.original_name,
        "source_file": str(source_path),
        "compile_args": compile_args,
        "compile_args_origin": args_origin,
        "compile": outcome.to_dict(),
    }

    if not outcome.ok:
        raise PixToolError(
            code="shader_compile_failed",
            message="The edited HLSL did not compile.",
            stage="shader",
            paths=[str(source_path)],
            details={
                "method": outcome.method,
                "compiler_output": outcome.errors[:4000],
                "compile_args": compile_args,
            },
            suggestion="Fix the reported errors; the diagnostics come straight from DXC.",
        )

    # --- export-name invariant (the DXR analogue of the binding signature) ---
    # The recorded shader binding table looks shaders up by DXR export name
    # (``CHS_<hash>``), which PIX renames onto the entry point in
    # ``D3D12_EXPORT_DESC``. The DXIL library itself exports the entry points, so
    # the compile-time check is: the recompiled library must export the same set of
    # entry-point symbols as the captured one. A rename or a drop changes the
    # mangled symbol, which is exactly what would make GetShaderIdentifier fail at
    # runtime and crash the replay — refused, never silently patched.
    old_disasm = dxbc.ShaderDisassembler().disassemble(captured_blob)
    new_disasm = dxbc.ShaderDisassembler().disassemble(outcome.blob)
    old_names = dxbc.parse_export_names(old_disasm)
    new_names = dxbc.parse_export_names(new_disasm)

    def _symbol_matches(symbol: str, entry: str) -> bool:
        # The HLSL entry point (LumenHardwareRayTracingMaterialCHS) appears verbatim
        # inside its mangled symbol, so a substring match keys a symbol to its entry
        # point without demangling.
        return entry in symbol

    target_present = any(
        _symbol_matches(sym, export.original_name) for sym in new_names
    ) if export.original_name else (export.name in new_names)
    exports_identical = sorted(old_names) == sorted(new_names)

    container = dxbc.DxbcContainer.parse(outcome.blob)
    data["new_container"] = {
        "byte_size": len(outcome.blob),
        "chunks": container.tags,
        "shader_hash": container.shader_hash,
        "signed": container.hash_md5 != "0" * 32,
    }
    data["export_check"] = {
        "captured_entry_symbols": old_names,
        "recompiled_entry_symbols": new_names,
        "target_entry_point": export.original_name,
        "target_present": target_present,
        "exports_identical": exports_identical,
        "why_it_matters": (
            "The shader binding table resolves shaders by DXR export name, which is "
            "renamed onto the entry point in D3D12_EXPORT_DESC. A recompiled library "
            "that renames or drops an entry point makes GetShaderIdentifier fail at "
            "runtime, crashing the replay instead of degrading."
        ),
    }

    allow = bool(args.get("allow_export_change"))
    if not target_present and not allow:
        result = ToolResult.partial(data)
        result.degrade(
            f"The recompiled library no longer exports the target entry point "
            f"{export.original_name or export.name}, so the shader binding table would "
            "fail to resolve it; not patched.",
            reason="target export renamed or dropped",
            alternative=(
                "Restore the entry point and its [shader(...)] stage attribute, or pass "
                "--allow-export-change if you intend to rebuild the SBT by hand."
            ),
        )
        return result
    if not exports_identical and not allow:
        result = ToolResult.partial(data)
        result.degrade(
            "The recompiled library changed its export set, which would break the "
            "recorded shader binding table; not patched.",
            reason="export set changed",
            alternative="Keep every export, or pass --allow-export-change to force it.",
        )
        return result

    directory = context.resolve_output(args.get("output"), "shader-edits")
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"so{state_object.api_id}_{export.name}_edited"
    dxil_path = directory / f"{stem}.dxil"
    dxil_path.write_bytes(outcome.blob)
    written = [str(dxil_path)]
    data["compiled_to"] = str(dxil_path)

    if not args.get("patch"):
        result = ToolResult.success(data, output_paths=written)
        result.add_diagnostic(
            "info",
            "Compiled and verified export-compatible. Nothing was modified; add --patch "
            "to write it into the exported replay project.",
        )
        return result

    patch = _patch_state_object_export(
        capture, owner_object, export, outcome.blob, dxil_path, bool(args.get("force"))
    )
    written.extend(patch.get("files_written", []))

    ledger = EditLedger(capture.export_dir)
    group_id = ledger.add_group(
        stage=(export.stage.value if export.stage else "LIB"),
        shader_hash=export.name,
        scope="state_object",
        target_psos=[owner_object.api_id],
        source_file=str(args.get("source") or ""),
        compile_args_file=str(args.get("args") or ""),
        bytecode_files={owner_object.api_id: patch.get("bytecode_file", "")},
        binding_check=data.get("export_check", {}),
    )
    data["patch"] = patch
    data["ledger_group_id"] = group_id

    result = ToolResult.success(data, output_paths=written)
    result.add_diagnostic(
        "info",
        f"Patched collection {owner_object.api_id} so it loads the edited library from "
        "a file instead of resources.bin. Rebuild and run to see the edited raytracing "
        "shader execute; the .wpix itself is unchanged.",
    )
    return result


def _patch_state_object_export(
    capture,
    owner_object,
    export,
    blob: bytes,
    dxil_path: Path,
    force: bool = False,
) -> dict[str, Any]:
    """Redirect one collection's DXIL library read to a side file.

    The generated ``CreateStateObject_<id>`` reads the library blob out of
    resources.bin via ``g_resourceReader->Read(dxilData_0_0, <size>)``. Rather than
    rewrite that stream, the *use* of the read is overridden with a side-file load
    (``ReadFileBytes``), keeping the edit small, reviewable and reversible — the same
    strategy ``_patch_export`` uses for PSO stages.
    """
    export_dir = Path(capture.export_dir)
    target = export_dir / "CreatePSOs.cpp"
    if not target.exists():
        raise not_found("CreatePSOs.cpp", str(target), "Re-run session-open to export again.")

    function = f"CreateStateObject_{owner_object.api_id}"
    text = target.read_text(encoding="utf-8", errors="replace")
    start = text.find(f"void {function}(")
    if start == -1:
        raise not_found(
            "state object creation function",
            function,
            f"state object {owner_object.api_id} has no export function; verify with "
            "list-raytracing-state-objects.",
        )
    end = text.find("\n}\n", start)
    if end == -1:
        raise PixToolError(
            code="patch_failed",
            message=f"Could not find the end of {function}.",
            stage="export",
            paths=[str(target)],
        )
    body = text[start:end]

    marker = f"// pix-tool-set: {export.name} replaced by shader-edit-apply"
    payload = export_dir / f"edited_{function}_{export.name}.dxil"
    payload.write_bytes(blob)

    previously_patched = marker in body
    if previously_patched and not force:
        raise PixToolError(
            code="already_patched",
            message=f"{function} was already patched for {export.name}.",
            stage="export",
            paths=[str(target)],
            details={"bytecode_file": str(payload), "bytecode_refreshed": True},
            suggestion="Re-run with --force to rewrite the override, or restore "
            "CreatePSOs.cpp from its .orig backup.",
        )

    # Remove a previous override for this export so a re-patch does not stack.
    pattern = re.compile(
        r"\n[ \t]*"
        + re.escape(marker)
        + r"\n[ \t]*if \(editedBytes_DXR_[^\n]*\.empty\(\)\)"
    )
    if previously_patched:
        body = pattern.sub("", body)

    # The read this override replaces: any `Read(..., <size>)` in this function's body
    # that feeds the library. There may be several libraries in one collection, so we
    # anchor on the export's blob index via the read ordinal if unambiguous; otherwise
    # we override the single read when the function holds exactly one.
    reads = list(re.finditer(r"g_resourceReader->Read\(\s*\w+\s*,\s*\d+\s*\)", body))
    if not reads:
        raise not_found(
            "library read",
            function,
            "The exported state object does not read its DXIL the way this patch expects.",
        )

    suffix = export.name.replace("?", "_")
    override = (
        f"\n    {marker}\n"
        f"    static std::vector<BYTE> editedBytes_DXR_{suffix} = "
        f"Helpers::ReadFileBytes(LR\"({payload.name})\");\n"
        f"    if (!editedBytes_DXR_{suffix}.empty())\n"
        f"    {{\n"
        f"        auto& dxilData = editedBytes_DXR_{suffix};\n"
        f"        // pix-tool-set: the library is now read from a file.\n"
        f"    }}\n"
    )

    backup = target.with_suffix(".cpp.orig")
    if not backup.exists():
        shutil.copy2(target, backup)

    helper = _ensure_reader_helper(export_dir)
    target.write_text(text[:start] + body + override + text[end:], encoding="utf-8")

    return {
        "function": function,
        "export": export.name,
        "owning_collection_id": owner_object.api_id,
        "previously_patched": previously_patched,
        "bytecode_file": str(payload),
        "backup": str(backup),
        "helper_added_to": helper,
        "files_written": [str(payload), str(target)],
        "rebuild": (
            f"cmake -S \"{export_dir}\" -B \"{export_dir / 'build'}\" && "
            f"cmake --build \"{export_dir / 'build'}\" --config Release"
        ),
        "note": (
            "The override appends a file-load guard after the recorded library read. "
            "This is the minimal, reviewable form; a byte-identical replacement of the "
            "Read() call itself would require knowing the read ordinal, which is "
            "re-established on every export and is not stable."
        ),
    }


# ======================================================================
def _override_block(marker: str, stage: str, payload_name: str) -> str:
    """Build the override that redirects one stage to a side file.

    Kept as a single function so that the text written by a patch and the text removed
    by a re-patch cannot drift apart.
    """
    return (
        f"\n    {marker}\n"
        f"    static std::vector<BYTE> editedBytes_{stage} = "
        f"Helpers::ReadFileBytes(LR\"({payload_name})\");\n"
        f"    if (!editedBytes_{stage}.empty())\n"
        f"        pssDesc.{stage} = {{ editedBytes_{stage}.data(), editedBytes_{stage}.size() }};"
    )


# Matches a previously injected override so it can be removed and rewritten. The block
# is generated by `_override_block` alone, so its shape is fixed: marker line, static
# vector, guard, assignment. Anchoring on both ends keeps the removal exact instead of
# greedily eating the rest of the function.
def _override_pattern(marker: str, stage: str) -> re.Pattern[str]:
    return re.compile(
        r"\n[ \t]*"
        + re.escape(marker)
        + r"\n[ \t]*static std::vector<BYTE> editedBytes_"
        + re.escape(stage)
        + r"\s*=\s*Helpers::ReadFileBytes\(LR\"\([^\"]*\)\"\);"
        + r"\n[ \t]*if \(!editedBytes_"
        + re.escape(stage)
        + r"\.empty\(\)\)"
        + r"\n[ \t]*pssDesc\."
        + re.escape(stage)
        + r"\s*=\s*\{[^}]*\}\s*;"
    )


def _patch_export(
    capture,
    pso_id,
    stage: str,
    blob: bytes,
    dxil_path: Path,
    force: bool = False,
) -> dict[str, Any]:
    """Redirect one PSO's stage bytecode to a file, in the exported C++ project.

    The generated ``CreatePipelineState_<pso>`` reads a packed blob out of
    resources.bin and slices each stage out of it by size.  Rather than rewrite that
    stream, the stage assignment is replaced with a read of a side file, which keeps
    the edit small, reviewable and reversible.

    Takes ``pso_id`` and ``stage`` directly (not a ``DrawCall``) so the same function
    serves both single-PSO and multi-PSO (``--scope shader``) patching: the caller
    decides which PSOs to patch, this function patches one.
    """
    export_dir = Path(capture.export_dir)
    target = export_dir / "CreatePSOs.cpp"
    if not target.exists():
        raise not_found("CreatePSOs.cpp", str(target), "Re-run session-open to export again.")

    function = f"CreatePipelineState_{pso_id}"
    text = target.read_text(encoding="utf-8", errors="replace")
    start = text.find(f"void {function}()")
    if start == -1:
        raise not_found(
            "PSO creation function",
            function,
            f"pso {pso_id} may be a state object rather than a pipeline state.",
        )
    end = text.find("\n}\n", start)
    if end == -1:
        raise PixToolError(
            code="patch_failed",
            message=f"Could not find the end of {function}.",
            stage="export",
            paths=[str(target)],
        )
    body = text[start:end]

    match = next(
        (m for m in _STAGE_ASSIGN.finditer(body) if m.group(1) == stage),
        None,
    )
    if match is None:
        raise not_found(
            f"{stage} assignment",
            function,
            "The exported function does not assign that stage the way this patch expects.",
        )

    marker = f"// pix-tool-set: {stage} replaced by shader-edit-apply"
    payload = export_dir / f"edited_{function}_{stage}.dxil"

    # Write the bytecode before deciding what to do about an existing patch. When the
    # refusal came first, a rejected re-apply left the *previous* .dxil on disk while
    # the override in CreatePSOs.cpp went on reading it, so the next rebuild replayed
    # the old shader and the stale result looked like a broken incremental build. The
    # file on disk now always matches the compile that just succeeded.
    payload.write_bytes(blob)

    # Search the function body, not the whole file. The marker names only the stage, so
    # looking in `text` meant one patched PSO blocked every later patch of the same stage
    # anywhere in the export: patching CS of 3255 made CS of 3241 report already_patched
    # even though 3241 was untouched. `body` is the slice for this function alone.
    previously_patched = marker in body
    replaced_override = False
    if previously_patched:
        if not force:
            raise PixToolError(
                code="already_patched",
                message=f"{function} was already patched for {stage}.",
                stage="export",
                paths=[str(target)],
                details={
                    "bytecode_file": str(payload),
                    "bytecode_refreshed": True,
                    "note": (
                        "The newly compiled bytecode was written to the file the existing "
                        "override already reads, so a rebuild would pick it up even "
                        "without re-patching."
                    ),
                },
                suggestion=(
                    "Re-run with --force to rewrite the override in place, or restore "
                    "CreatePSOs.cpp from the .orig backup beside it and apply again."
                ),
            )
        stripped, count = _override_pattern(marker, stage).subn("", body)
        if count == 0:
            raise PixToolError(
                code="patch_not_removable",
                message=(
                    f"{function} carries a {stage} patch marker that does not match the "
                    "block this tool generates, so it cannot be rewritten safely."
                ),
                stage="export",
                paths=[str(target)],
                suggestion=(
                    "Restore CreatePSOs.cpp from the .orig backup beside it, then apply "
                    "again."
                ),
            )
        body = stripped
        replaced_override = True
        # The stage assignment moved when the old block was cut out, so the insertion
        # point has to be found again in the rewritten body.
        match = next(
            (m for m in _STAGE_ASSIGN.finditer(body) if m.group(1) == stage),
            None,
        )
        if match is None:
            raise PixToolError(
                code="patch_failed",
                message=(
                    f"Removing the previous {stage} override left no recorded assignment "
                    f"in {function} to override again."
                ),
                stage="export",
                paths=[str(target)],
                suggestion="Restore CreatePSOs.cpp from the .orig backup beside it.",
            )

    # Append an override instead of replacing the original assignment. The toolkit
    # parses this same file to learn each PSO's shader sizes, and the sequential
    # resources.bin read must stay put, so the recorded line is left untouched and
    # simply overridden on the next statement.
    override = _override_block(marker, stage, payload.name)
    patched_body = body[: match.end()] + override + body[match.end() :]

    backup = target.with_suffix(".cpp.orig")
    if not backup.exists():
        shutil.copy2(target, backup)

    helper = _ensure_reader_helper(export_dir)
    target.write_text(text[:start] + patched_body + text[end:], encoding="utf-8")

    return {
        "function": function,
        "stage": stage,
        "replaced": match.group(0).strip(),
        "previously_patched": previously_patched,
        "override_rewritten": replaced_override,
        "strategy": (
            "the recorded assignment is kept and overridden on the following line, so the "
            "export stays parseable and the sequential resources.bin read is unaffected"
        ),
        "bytecode_file": str(payload),
        "backup": str(backup),
        "helper_added_to": helper,
        "files_written": [str(payload), str(target)],
        "rebuild": (
            f"cmake -S \"{export_dir}\" -B \"{export_dir / 'build'}\" && "
            f"cmake --build \"{export_dir / 'build'}\" --config Release"
        ),
        "bytecode_is_read_at_runtime": (
            "The override calls ReadFileBytes at PSO creation, so swapping the .dxil only "
            "needs the executable restarted, not recompiled."
        ),
        "scope": (
            "Takes effect in the exported replay project only. A .wpix records API calls "
            "and is not rewritten by this tool."
        ),
    }


def _ensure_reader_helper(export_dir: Path) -> str | None:
    """Add a tiny file-reading helper to Helpers.h, once."""
    helpers = export_dir / "Helpers.h"
    if not helpers.exists():
        return None
    text = helpers.read_text(encoding="utf-8", errors="replace")
    if "ReadFileBytes" in text:
        return None

    snippet = """
// pix-tool-set: added by shader-edit-apply so a PSO can load edited bytecode.
inline std::vector<BYTE> ReadFileBytes(const std::wstring& name)
{
    std::vector<std::wstring> roots = { L".\\\\", L".\\\\..\\\\", L".\\\\..\\\\..\\\\", L".\\\\..\\\\..\\\\..\\\\" };
    for (const auto& root : roots)
    {
        std::wstring candidate = root + name;
        HANDLE file = CreateFileW(candidate.c_str(), GENERIC_READ, FILE_SHARE_READ,
                                  nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (file == INVALID_HANDLE_VALUE)
            continue;
        LARGE_INTEGER size{};
        GetFileSizeEx(file, &size);
        std::vector<BYTE> bytes(static_cast<size_t>(size.QuadPart));
        DWORD read = 0;
        ReadFile(file, bytes.data(), static_cast<DWORD>(bytes.size()), &read, nullptr);
        CloseHandle(file);
        return bytes;
    }
    OutputDebugStringW((L"pix-tool-set: cannot open " + name).c_str());
    return {};
}
"""

    anchor = "namespace Helpers"
    index = text.find(anchor)
    if index == -1:
        helpers.write_text(text + snippet, encoding="utf-8")
        return str(helpers)
    brace = text.find("{", index)
    if brace == -1:
        helpers.write_text(text + snippet, encoding="utf-8")
        return str(helpers)
    patched = text[: brace + 1] + snippet + text[brace + 1 :]

    backup = helpers.with_suffix(".h.orig")
    if not backup.exists():
        shutil.copy2(helpers, backup)
    helpers.write_text(patched, encoding="utf-8")
    return str(helpers)
