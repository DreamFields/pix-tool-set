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
    "captured one, because the recorded command lists bind by slot. The original .wpix is "
    "never modified."
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
        entry = capture.find_pass_by_event(queue_id=draw.queue_id)
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

    if args.get("patch"):
        patch = _patch_export(capture, draw, shader, outcome.blob, dxil_path)
        data["patch"] = patch
        written.extend(patch.get("files_written", []))
        result = ToolResult.success(data, output_paths=written)
        result.add_diagnostic(
            "info",
            "The exported C++ project now loads the new bytecode from a file instead of "
            "resources.bin. Rebuild it with CMake and run it to see the edited shader "
            "execute; the .wpix itself is unchanged.",
        )
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
    return result


# ======================================================================
def _patch_export(capture, draw, shader, blob: bytes, dxil_path: Path) -> dict[str, Any]:
    """Redirect the PSO's stage bytecode to a file, in the exported C++ project.

    The generated `CreatePipelineState_<pso>` reads a packed blob out of
    resources.bin and slices each stage out of it by size.  Rather than rewrite that
    stream, the stage assignment is replaced with a read of a side file, which keeps
    the edit small, reviewable and reversible.
    """
    export_dir = Path(capture.export_dir)
    target = export_dir / "CreatePSOs.cpp"
    if not target.exists():
        raise not_found("CreatePSOs.cpp", str(target), "Re-run session-open to export again.")

    function = f"CreatePipelineState_{draw.pso_id}"
    text = target.read_text(encoding="utf-8", errors="replace")
    start = text.find(f"void {function}()")
    if start == -1:
        raise not_found(
            "PSO creation function",
            function,
            f"pso {draw.pso_id} may be a state object rather than a pipeline state.",
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

    stage = shader.stage.value
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
    if marker in text:
        raise PixToolError(
            code="already_patched",
            message=f"{function} was already patched for {stage}.",
            stage="export",
            paths=[str(target)],
            suggestion=(
                "Restore CreatePSOs.cpp from the .orig backup beside it, then apply again."
            ),
        )

    payload = export_dir / f"edited_{function}_{stage}.dxil"
    payload.write_bytes(blob)

    # Append an override instead of replacing the original assignment. The toolkit
    # parses this same file to learn each PSO's shader sizes, and the sequential
    # resources.bin read must stay put, so the recorded line is left untouched and
    # simply overridden on the next statement.
    override = (
        f"\n    {marker}\n"
        f"    static std::vector<BYTE> editedBytes_{stage} = "
        f"Helpers::ReadFileBytes(LR\"({payload.name})\");\n"
        f"    if (!editedBytes_{stage}.empty())\n"
        f"        pssDesc.{stage} = {{ editedBytes_{stage}.data(), editedBytes_{stage}.size() }};"
    )
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
