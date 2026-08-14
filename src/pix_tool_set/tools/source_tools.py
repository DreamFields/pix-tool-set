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
from ..engine import shaderpdb
from ..engine.model import ShaderStage
from ..errors import not_found
from ..results import ToolResult
from ._common import PASS_SELECTOR, resolve_pass, tool, with_session

_STAGES = [stage.value for stage in ShaderStage]

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
    if not shaders:
        # "This pass binds no such stage" is factually wrong for a ray dispatch: the
        # pass may bind dozens of shaders, they just live on a state object rather than
        # a PSO, so draw.shader() cannot see them. Naming the DXR route keeps the
        # message from closing off an investigation that is still perfectly possible.
        if draw.state_object_id is not None:
            state_object = draw.state_object
            export_count = (
                len(state_object.resolved_exports) if state_object is not None else 0
            )
            raise not_found(
                "shader",
                stage_filter or "any",
                f"This pass is a raytracing dispatch bound to state object "
                f"{draw.state_object_id}, whose shaders are DXIL library exports rather "
                f"than PSO stages"
                + (f" ({export_count} of them)" if export_count else "")
                + ". This tool reads PSO stages, so it cannot reach them. Use "
                f"`describe-state-object --draw-index {draw.index}` to list the exports, "
                f"`pass-bindings --draw-index {draw.index}` for their bindings, or "
                f"`shader-edit-begin --state-object-id {draw.state_object_id} "
                f"--export-name <name>` to recover one export's HLSL from the PDB.",
            )
        raise not_found("shader", stage_filter or "any", "This pass binds no such stage.")

    max_lines = args.get("max_lines")
    max_lines = 120 if max_lines is None else int(max_lines)

    supplied = [Path(str(p)).expanduser() for p in (args.get("pdb_dirs") or [])]
    search_dirs = supplied or _default_pdb_dirs(context, args)
    out_dir = Path(str(args["output_dir"])).expanduser() if args.get("output_dir") else None
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)

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
            want_body = args.get("body_only")
            want_body = True if want_body is None else bool(want_body)
            text = (body if want_body and body else full) or full

            want_entry = args.get("entry_only")
            want_entry = True if want_entry is None else bool(want_entry)
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
        "source_tiers": {
            "pdb-hlsl": "Real HLSL recovered from the engine's shader PDB.",
            "embedded-hlsl": "Real HLSL was embedded in the capture's shader container.",
            "dxil-disassembly": (
                "No HLSL available; returning DXIL text plus the entry point name."
            ),
            "unavailable": "Neither source nor disassembly could be produced.",
        },
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
