"""Answer "show me this pass's shader source" as completely as the capture allows.

A UE5 capture almost never embeds HLSL: the DXBC container carries an ILDN chunk
(the PDB *name*) rather than ILDB/SPDB (the PDB *contents*). So the honest answer
has three tiers, and this tool reports which tier applies:

  1. embedded HLSL         - only with /Zi /Qembed_debug
  2. external PDB          - resolvable if a symbol path is supplied
  3. DXIL disassembly      - always available, plus the entry point name, which is
                             what lets a human find the original .usf
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..context import ToolContext
from ..engine import dxbc, shaderpdb
from ..engine.model import ShaderStage
from ..errors import not_found
from ..results import ToolResult
from ._common import PASS_SELECTOR, resolve_pass, tool, with_session

_STAGES = [stage.value for stage in ShaderStage]

_SOURCE_TIERS = {
    "pdb-hlsl": "Real HLSL recovered from the engine's shader PDB.",
    "embedded-hlsl": "Real HLSL was embedded in the capture's shader container.",
    "dxil-disassembly": (
        "No HLSL available; returning DXIL text plus the entry point name."
    ),
    "unavailable": "Neither source nor disassembly could be produced.",
}

_NOTE = (
    "Original HLSL is not stored inside a .wpix unless the shader was built with "
    "/Zi /Qembed_debug, which UE5 does not do (it compiles with -Zi -Qstrip_debug and "
    "writes a separate PDB). Point --pdb-dirs at the engine's ShaderSymbols output, e.g. "
    r"<Project>\Saved\ShaderSymbols\PCD3D_SM6, and this tool recovers the real source "
    "through IDxcPdbUtils, falling back to parsing the PDB container directly. Without a "
    "symbol directory it returns the DXIL disassembly plus the entry point name."
)


def _default_pdb_dirs(context: ToolContext, args: dict[str, Any]) -> list[Path]:
    """Symbol directories saved on the session by session-set-pdb-dirs."""
    try:
        # `resolve`, not `get`: with no explicit --session the name is None, and `get`
        # matches by name only, so the stored directories would never be found. `resolve`
        # falls back to the active session the way every other command does.
        record = context.store.resolve(
            session=args.get("session"),
            capture_path=args.get("capture"),
            export_dir=args.get("export_dir"),
        )
    except Exception:
        # Having no session is not fatal: this tool degrades to DXIL disassembly.
        return []
    if record is None:
        return []
    return [Path(str(p)).expanduser() for p in (record.shader_pdb_dirs or [])]


def _resolve_source(shader, search_dirs: list[Path]) -> dict[str, Any]:
    """Try to recover real HLSL for one shader from the symbol directories."""
    outcome: dict[str, Any] = {
        "pdb_path": None,
        "recovered": False,
        "method": None,
        "detail": None,
        "compile_args": [],
        "section_names": [],
    }
    if not search_dirs:
        outcome["detail"] = "no --pdb-dirs supplied"
        return outcome

    pdb_path = shaderpdb.find_pdb(
        search_dirs, shader.shader_hash or "", shader.debug_name or ""
    )
    if pdb_path is None:
        outcome["detail"] = (
            f"no PDB named {shader.debug_name or shader.shader_hash!r} in the supplied dirs"
        )
        return outcome

    outcome["pdb_path"] = str(pdb_path)
    report = shaderpdb.extract_sources(pdb_path)
    outcome.update(
        {
            "recovered": bool(report.get("ok")),
            "method": report.get("method"),
            "detail": report.get("detail"),
            "compile_args": report.get("compile_args") or [],
            "section_names": report.get("section_names") or [],
            "entry_file": report.get("entry_file"),
            "shader_body_source": report.get("shader_body_source"),
        }
    )
    outcome["_full_text"] = report.get("full_text") or ""
    outcome["_body"] = report.get("shader_body") or ""
    return outcome


def _dxr_export_rows(
    capture,
    draw,
    search_dirs: list[Path],
    stage_filter: str | None,
    export_filter: str | None,
    max_lines: int,
    out_dir: Path | None,
    want_body: bool,
    want_entry: bool,
) -> tuple[list[dict[str, Any]], list[str], set[str], dict[str, Any]]:
    """Source rows for a DISPATCH_RAYS action, whose shaders live on a state object.

    A raytracing shader is an export of a DXIL_LIBRARY inside a COLLECTION, so
    ``draw.shaders`` is empty and the PSO-stage walk finds nothing. Refusing to
    answer here would be wrong: the exports, their PDBs and their HLSL are all
    reachable, just through the state object rather than through a PSO. This is the
    DXR sibling of the per-shader loop above.
    """
    state_object = draw.state_object
    if state_object is None:
        return [], [], set(), {"reason": "the action names a state object the export does not contain"}

    exports = list(state_object.resolved_exports)
    if stage_filter:
        wanted = str(stage_filter).upper()
        exports = [e for e in exports if (e.stage.value if e.stage else "") == wanted]
    if export_filter:
        exact = [e for e in exports if e.name == export_filter]
        exports = exact or [e for e in exports if e.original_name == export_filter]

    # One HLSL entry point is compiled into many collections under different mangled
    # names. Returning all of them would repeat the same source dozens of times, so
    # de-duplicate by entry point and record how many exports each row stands for.
    grouped: dict[str, list[Any]] = {}
    for export in exports:
        key = export.original_name or export.name
        grouped.setdefault(key, []).append(export)

    rows: list[dict[str, Any]] = []
    output_paths: list[str] = []
    tiers: set[str] = set()

    for entry_name, group in grouped.items():
        export = group[0]
        blob = b""
        if export.dxil_blob_index is not None:
            try:
                blob = capture._load_blob(export.dxil_blob_index)
            except Exception:  # noqa: BLE001
                blob = b""

        debug_name = ""
        shader_hash = ""
        disassembly = ""
        if blob:
            try:
                container = dxbc.DxbcContainer.parse(blob)
                debug_name = container.debug_name or ""
                shader_hash = container.shader_hash or ""
            except ValueError:
                pass

        recovery: dict[str, Any] = {
            "pdb_path": None,
            "recovered": False,
            "method": None,
            "detail": None,
            "compile_args": [],
        }
        text = ""
        tier = "unavailable"

        pdb_path = None
        if search_dirs:
            pdb_path = shaderpdb.find_pdb(search_dirs, shader_hash, debug_name)
            if pdb_path is None:
                # Some engines file the PDB by entry point rather than container hash.
                pdb_path = shaderpdb.find_pdb(search_dirs, "", entry_name or "")
        if pdb_path is not None:
            report = shaderpdb.extract_sources(pdb_path)
            recovery.update(
                {
                    "pdb_path": str(pdb_path),
                    "recovered": bool(report.get("ok")),
                    "method": report.get("method"),
                    "detail": report.get("detail"),
                    "compile_args": report.get("compile_args") or [],
                }
            )
            if report.get("ok"):
                full = report.get("full_text") or ""
                body = report.get("shader_body") or ""
                text = (body if want_body and body else full) or full
                tier = "pdb-hlsl"
                if want_entry and text:
                    sliced = shaderpdb.slice_entry_function(text, entry_name)
                    if sliced:
                        text = sliced
                        recovery["scope"] = "entry-function"
                    else:
                        recovery["scope"] = "translation-unit"
        elif search_dirs:
            recovery["detail"] = (
                f"no PDB named {debug_name or shader_hash or entry_name!r} in the supplied dirs"
            )
        else:
            recovery["detail"] = "no --pdb-dirs supplied"

        if tier != "pdb-hlsl" and blob:
            try:
                disassembly = dxbc.ShaderDisassembler().disassemble(blob)
            except Exception:  # noqa: BLE001
                disassembly = ""
            if disassembly:
                text = disassembly
                tier = "dxil-disassembly"
        tiers.add(tier)

        stage_tag = export.stage.value if export.stage else "LIB"
        if out_dir is not None and text:
            suffix = "dxil.txt" if tier == "dxil-disassembly" else "hlsl"
            safe = f"so{state_object.api_id}_{export.name}_{entry_name}"[:80]
            safe = safe.replace("/", "_").replace(":", "_").strip()
            path = out_dir / f"{safe}.{suffix}"
            path.write_text(text, encoding="utf-8", errors="replace")
            output_paths.append(str(path))

        lines = text.splitlines()
        truncated = bool(max_lines) and len(lines) > max_lines
        rows.append(
            {
                "stage": stage_tag,
                "stage_source": export.stage_source,
                "source_tier": tier,
                "export_name": export.name,
                "entry_point": entry_name,
                "shader_hash": shader_hash,
                "pdb_name": debug_name,
                "byte_size": len(blob) if blob else None,
                "defining_state_object_id": export.defining_state_object_id,
                "aliased_export_count": len(group),
                "aliased_export_names": [e.name for e in group][:12],
                "pdb_recovery": recovery,
                "line_count": len(lines),
                "truncated": truncated,
                "text": "\n".join(lines[:max_lines]) if truncated else text,
            }
        )

    rows.sort(key=lambda row: (str(row.get("stage")), str(row.get("entry_point"))))
    meta = {
        "state_object_id": state_object.api_id,
        "export_total": len(state_object.resolved_exports),
        "export_returned": len(rows),
        "hit_group_total": len(state_object.resolved_hit_groups)
        if hasattr(state_object, "resolved_hit_groups")
        else None,
    }
    return rows, output_paths, tiers, meta


@tool(
    name="pass-shader-source",
    summary=(
        "Source view for a pass's shaders. Recovers real HLSL from the engine's shader "
        "PDBs when --pdb-dirs is supplied, otherwise returns the DXIL disassembly, and "
        "always states which tier the answer came from."
    ),
    category="shaders",
    parameters=with_session(
        PASS_SELECTOR,
        stage={"type": "string", "enum": _STAGES, "description": "Restrict to one stage."},
        export_name={
            "type": "string",
            "description": (
                "Raytracing only: restrict to one DXIL library export, by mangled name "
                "(CHS_<hash>) or HLSL entry point. Ignored for rasterisation passes."
            ),
        },
        max_lines={
            "type": "integer",
            "description": "Inline text line cap. Default 120; 0 means no limit.",
        },
        body_only={
            "type": "boolean",
            "description": (
                "Return the authored translation unit rather than the whole preprocessed "
                "text. Default true when real source was recovered."
            ),
        },
        entry_only={
            "type": "boolean",
            "description": (
                "Return only the entry function and its attributes, skipping UE5's several "
                "hundred lines of generated helpers. Default true when the function can be "
                "located."
            ),
        },
        output_dir={
            "type": "string",
            "description": "Write the full text per stage into this directory.",
        },
        pdb_dirs={
            "type": "array",
            "description": (
                "Directories holding shader PDBs, e.g. "
                r"F:\Project\Saved\ShaderSymbols\PCD3D_SM6."
            ),
        },
    ),
    returns="Per-stage source tier, entry point, compile args and source or disassembly text.",
    examples=[
        "pix-tool-set pass-shader-source --queue-id 18461",
        'pix-tool-set pass-shader-source --queue-id 18461 --pdb-dirs "F:\\JL_TMR\\UnrealEngine\\Games\\JyGame\\Saved\\ShaderSymbols\\PCD3D_SM6"',
        'pix-tool-set pass-shader-source --pass-name "Light Grid Create" --stage CS --max-lines 0',
    ],
    notes=_NOTE,
)
def pass_shader_source(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)

    entry = resolve_pass(capture, args)

    draw = capture.draw_call(entry["first_draw_index"])
    if draw is None:
        raise not_found("draw", entry["first_draw_index"])

    stage_filter = args.get("stage")
    shaders = [draw.shader(stage_filter)] if stage_filter else draw.shaders
    shaders = [s for s in shaders if s is not None]

    max_lines = args.get("max_lines")
    max_lines = 120 if max_lines is None else int(max_lines)

    supplied = [Path(str(p)).expanduser() for p in (args.get("pdb_dirs") or [])]
    search_dirs = supplied or _default_pdb_dirs(context, args)
    out_dir = Path(str(args["output_dir"])).expanduser() if args.get("output_dir") else None
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)

    want_body = args.get("body_only")
    want_body = True if want_body is None else bool(want_body)
    want_entry = args.get("entry_only")
    want_entry = True if want_entry is None else bool(want_entry)

    if not shaders and draw.state_object_id is not None:
        # A raytracing dispatch. Its shaders are DXIL library exports on the state
        # object, not PSO stages, so answer from there instead of refusing: every
        # ingredient (export list, container hash, PDB, HLSL) is available.
        rows, output_paths, tiers, rt_meta = _dxr_export_rows(
            capture,
            draw,
            search_dirs,
            stage_filter,
            args.get("export_name"),
            max_lines,
            out_dir,
            want_body,
            want_entry,
        )
        if not rows:
            state_object = draw.state_object
            names = sorted(
                {f"{e.name} ({e.original_name})" for e in state_object.resolved_exports}
            )[:20] if state_object is not None else []
            raise not_found(
                "shader",
                args.get("export_name") or stage_filter or "any",
                "This raytracing pass is bound to state object "
                f"{draw.state_object_id}, but no export matched the filter. Available "
                "exports: " + (", ".join(names) or "<none>"),
            )
        data = {
            "pass_index": entry["pass_index"],
            "pass_name": entry["name"],
            "marker_path": entry["marker_path"],
            "draw_index": draw.index,
            "global_id": draw.global_id,
            "queue_id": entry.get("first_queue_id"),
            "pso_id": None,
            "binding_shape": "raytracing",
            "raytracing": rt_meta,
            "pdb_dirs_used": [str(p) for p in search_dirs],
            "stages": rows,
            "source_tiers": _SOURCE_TIERS,
        }
        if tiers <= {"pdb-hlsl", "embedded-hlsl"}:
            result = ToolResult.success(data, output_paths=output_paths)
            result.add_diagnostic(
                "info",
                "Raytracing pass: these are DXIL library exports of state object "
                f"{rt_meta.get('state_object_id')}, de-duplicated by HLSL entry point "
                "(aliased_export_count says how many mangled exports share each row). "
                "There is no PSO, so pso_id is null by pipeline shape, not by data loss.",
            )
            return result
        result = ToolResult.partial(data, output_paths=output_paths)
        result.degrade(
            "Returning DXIL disassembly for at least one raytracing export instead of "
            "original source.",
            reason=(
                "the supplied --pdb-dirs did not contain a matching PDB"
                if search_dirs
                else "no --pdb-dirs was supplied"
            ),
            remedy=(
                r"Pass --pdb-dirs <Project>\Saved\ShaderSymbols\PCD3D_SM6 to recover "
                "real source."
            ),
        )
        return result

    if not shaders:
        raise not_found("shader", stage_filter or "any", "This pass binds no such stage.")

    output_paths: list[str] = []
    rows: list[dict[str, Any]] = []
    tiers: set[str] = set()

    for shader in shaders:
        recovery = _resolve_source(shader, search_dirs)
        body = recovery.pop("_body", "")
        full = recovery.pop("_full_text", "")
        embedded = shader.embedded_source

        if recovery["recovered"]:
            tier = "pdb-hlsl"
            text = (body if want_body and body else full) or full

            if want_entry:
                sliced = shaderpdb.slice_entry_function(text, shader.entry_point)
                if sliced:
                    text = sliced
                    recovery["scope"] = "entry-function"
                else:
                    recovery["scope"] = "translation-unit"
                    recovery["scope_note"] = (
                        f"entry function {shader.entry_point!r} could not be isolated; "
                        "returning the whole unit"
                    )
            else:
                recovery["scope"] = "translation-unit"
        elif embedded:
            tier = "embedded-hlsl"
            text = embedded
        elif shader.disassembly:
            tier = "dxil-disassembly"
            text = shader.disassembly
        else:
            tier = "unavailable"
            text = ""
        tiers.add(tier)

        if out_dir is not None and text:
            suffix = "dxil.txt" if tier == "dxil-disassembly" else "hlsl"
            safe = entry["name"][:40].replace("/", "_").replace(":", "_").strip()
            path = out_dir / f"{safe}.{shader.stage.value}.{suffix}"
            path.write_text(text, encoding="utf-8", errors="replace")
            output_paths.append(str(path))

        lines = text.splitlines()
        truncated = bool(max_lines) and len(lines) > max_lines
        rows.append(
            {
                "stage": shader.stage.value,
                "source_tier": tier,
                "entry_point": shader.entry_point,
                "num_threads": shader.num_threads,
                "shader_hash": shader.shader_hash,
                "byte_size": shader.byte_size,
                "pdb_name": shader.debug_name,
                "pdb_recovery": recovery,
                "line_count": len(lines),
                "truncated": truncated,
                "text": "\n".join(lines[:max_lines]) if truncated else text,
            }
        )

    data = {
        "pass_index": entry["pass_index"],
        "pass_name": entry["name"],
        "marker_path": entry["marker_path"],
        "draw_index": draw.index,
        "global_id": draw.global_id,
        "queue_id": entry.get("first_queue_id"),
        "pso_id": draw.pso_id,
        "pdb_dirs_used": [str(p) for p in search_dirs],
        "stages": rows,
        "source_tiers": _SOURCE_TIERS,
    }
    # A null queue_id here means the pass sits on a queue the event list export missed,
    # not that the shader lookup failed. The two look identical in the payload, and the
    # shader data itself comes from the PDB and export, so it is unaffected.
    if data["queue_id"] is None:
        data["queue_id_unavailable"] = (
            "This pass has no row in the exported event list, which covers a single "
            "command queue. Address it by pass_index or draw_index."
        )

    if tiers <= {"pdb-hlsl", "embedded-hlsl"}:
        result = ToolResult.success(data, output_paths=output_paths)
        result.add_diagnostic(
            "info",
            "Real HLSL recovered. UE5 injects a generated prologue ahead of the authored "
            "body; pass --body-only false to see all of it.",
        )
        return result

    result = ToolResult.partial(data, output_paths=output_paths)
    entries = ", ".join(f"{row['stage']}={row['entry_point'] or '?'}" for row in rows)
    if search_dirs:
        reason = "the supplied --pdb-dirs did not contain a matching PDB"
        remedy = (
            "Check that the shader hash exists in the symbol directory, or search the "
            "engine tree for the entry point name."
        )
    else:
        reason = "no --pdb-dirs was supplied and the capture embeds no HLSL"
        remedy = (
            r"Pass --pdb-dirs <Project>\Saved\ShaderSymbols\PCD3D_SM6 to recover real source."
        )
    result.degrade(
        "Returning DXIL disassembly instead of original source.",
        reason=reason,
        entry_points=entries,
        remedy=remedy,
    )
    return result


@tool(
    name="session-set-pdb-dirs",
    summary=(
        "Remember the shader PDB directories for this session so pass-shader-source "
        "recovers real HLSL without repeating the path every call."
    ),
    category="session",
    parameters=with_session(
        pdb_dirs={
            "type": "array",
            "description": (
                "Directories holding <hash>.pdb shader symbols, e.g. "
                r"F:\Project\Saved\ShaderSymbols\PCD3D_SM6."
            ),
        },
        clear={"type": "boolean", "description": "Forget the stored directories."},
    ),
    returns="The directories now stored on the session, with an existence check for each.",
    examples=[
        'pix-tool-set session-set-pdb-dirs --pdb-dirs "F:\\JL_TMR\\UnrealEngine\\Games\\JyGame\\Saved\\ShaderSymbols\\PCD3D_SM6"',
        "pix-tool-set session-set-pdb-dirs --clear",
    ],
    notes=_NOTE,
)
def session_set_pdb_dirs(args: dict[str, Any], context: ToolContext) -> ToolResult:
    # `resolve`, not `get`: `get(None)` never matches, so this command could not target
    # the active session, which is the one the user means when no --session is given.
    # It raises a structured session_not_found/session_missing error instead of returning
    # None, and the CLI renders that as the error payload, so no local guard is needed.
    # Only the name is forwarded, as in session-close: this command writes the record
    # back, and resolving by --capture can synthesise a record for a capture that was
    # never opened, which `put` would then register as a real session.
    record = context.store.resolve(session=args.get("session"))

    if args.get("clear"):
        record.shader_pdb_dirs = []
    else:
        supplied = [str(Path(str(p)).expanduser()) for p in (args.get("pdb_dirs") or [])]
        if not supplied:
            raise not_found(
                "pdb_dirs", "<empty>", "Pass --pdb-dirs, or --clear to forget them."
            )
        record.shader_pdb_dirs = supplied
    context.store.put(record)

    rows = []
    for directory in record.shader_pdb_dirs:
        path = Path(directory)
        count = 0
        if path.exists():
            try:
                count = sum(1 for _ in path.glob("*.pdb"))
            except OSError:
                count = -1
        rows.append({"path": directory, "exists": path.exists(), "pdb_count": count})

    result = ToolResult.success(
        {"session": record.name, "shader_pdb_dirs": rows}
    )
    missing = [row["path"] for row in rows if not row["exists"]]
    if missing:
        result.add_diagnostic("warning", f"These directories do not exist: {missing}")
    return result
