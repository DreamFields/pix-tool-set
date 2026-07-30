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
from ..engine.model import ShaderStage
from ..errors import not_found
from ..results import ToolResult
from ._common import tool, with_session

_STAGES = [stage.value for stage in ShaderStage]

_NOTE = (
    "Original HLSL/USF text is only inside a capture when the shader was compiled with "
    "/Zi /Qembed_debug. UE5 ships shaders with a separate PDB, so this tool reports the "
    "entry point name and PDB name it can recover, which together identify the source "
    "file, and returns the DXIL disassembly as the information-equivalent fallback."
)


def _pdb_search(shader, search_dirs: list[Path]) -> dict[str, Any]:
    """Look for the shader's external PDB in the supplied directories."""
    name = (shader.debug_name or "").strip()
    if not name:
        return {"searched": False, "reason": "shader declares no PDB name"}
    hits: list[str] = []
    for directory in search_dirs:
        if not directory.exists():
            continue
        candidate = directory / name
        if candidate.exists():
            hits.append(str(candidate))
        else:
            hits.extend(str(p) for p in directory.rglob(name))
    return {
        "searched": True,
        "pdb_name": name,
        "search_dirs": [str(d) for d in search_dirs],
        "found": hits,
    }


@tool(
    name="pass-shader-source",
    summary=(
        "Best available source view for a pass's shaders: embedded HLSL when present, "
        "otherwise the entry point name, PDB name and DXIL disassembly, with an explicit "
        "statement of which tier the answer came from."
    ),
    category="shaders",
    parameters=with_session(
        pass_name={"type": "string", "description": "Pass name (substring match)."},
        pass_index={"type": "integer", "description": "Pass index from list-passes."},
        global_id={"type": "integer", "description": "PIX GUI 'Global ID' inside the pass."},
        queue_id={"type": "integer", "description": "PIX GUI 'Queue ID' inside the pass."},
        stage={"type": "string", "enum": _STAGES, "description": "Restrict to one stage."},
        max_lines={
            "type": "integer",
            "description": "Inline disassembly line cap. Default 120; 0 means no limit.",
        },
        output_dir={
            "type": "string",
            "description": "Write the full text per stage into this directory.",
        },
        pdb_dirs={
            "type": "array",
            "description": "Directories to search for the shader's external PDB.",
        },
    ),
    returns="Per-stage source tier, entry point, PDB name, and disassembly text.",
    examples=[
        "pix-tool-set pass-shader-source --queue-id 18461",
        'pix-tool-set pass-shader-source --pass-name "Light Grid Create" --stage CS',
        "pix-tool-set pass-shader-source --queue-id 18461 --output-dir ./src_dump",
    ],
    notes=_NOTE,
)
def pass_shader_source(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)

    global_id = args.get("global_id")
    queue_id = args.get("queue_id")
    if global_id is not None or queue_id is not None:
        entry = capture.find_pass_by_event(global_id=global_id, queue_id=queue_id)
        label = f"global_id={global_id}" if global_id is not None else f"queue_id={queue_id}"
    elif args.get("pass_index") is not None:
        entry = capture.find_pass(int(args["pass_index"]))
        label = f"pass_index={args['pass_index']}"
    elif args.get("pass_name"):
        entry = capture.find_pass(str(args["pass_name"]))
        label = f"pass_name={args['pass_name']!r}"
    else:
        raise not_found("pass", "<no selector>", "Pass --pass-name/--pass-index/--queue-id.")

    if entry is None:
        raise not_found("pass", label, "Run list-passes or find-pass to get a valid id.")

    draw = capture.draw_call(entry["first_draw_index"])
    if draw is None:
        raise not_found("draw", entry["first_draw_index"])

    stage_filter = args.get("stage")
    shaders = [draw.shader(stage_filter)] if stage_filter else draw.shaders
    shaders = [s for s in shaders if s is not None]
    if not shaders:
        raise not_found("shader", stage_filter or "any", "This pass binds no such stage.")

    max_lines = args.get("max_lines")
    max_lines = 120 if max_lines is None else int(max_lines)

    pdb_dirs = [Path(str(p)).expanduser() for p in (args.get("pdb_dirs") or [])]
    out_dir = Path(str(args["output_dir"])).expanduser() if args.get("output_dir") else None
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)

    output_paths: list[str] = []
    rows: list[dict[str, Any]] = []
    tiers: set[str] = set()

    for shader in shaders:
        embedded = shader.embedded_source
        disassembly = shader.disassembly or ""
        if embedded:
            tier = "embedded-hlsl"
            text = embedded
        elif disassembly:
            tier = "dxil-disassembly"
            text = disassembly
        else:
            tier = "unavailable"
            text = ""
        tiers.add(tier)

        pdb = _pdb_search(shader, pdb_dirs) if pdb_dirs else {
            "searched": False,
            "pdb_name": shader.debug_name,
            "reason": "no pdb_dirs supplied",
        }

        if out_dir is not None and text:
            suffix = "hlsl" if tier == "embedded-hlsl" else "dxil.txt"
            path = out_dir / f"{entry['name'][:40].replace('/', '_').strip()}.{shader.stage.value}.{suffix}"
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
                "pdb_lookup": pdb,
                "has_embedded_source": shader.has_embedded_source,
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
        "stages": rows,
        "source_tiers": {
            "embedded-hlsl": "Original HLSL recovered from the shader container.",
            "dxil-disassembly": (
                "No HLSL in the capture; returning DXIL text plus the entry point name, "
                "which identifies the original shader function."
            ),
            "unavailable": "Neither source nor disassembly could be produced.",
        },
    }

    if tiers == {"embedded-hlsl"}:
        return ToolResult.success(data, output_paths=output_paths)

    result = ToolResult.partial(data, output_paths=output_paths)
    entries = ", ".join(
        f"{row['stage']}={row['entry_point'] or '?'}" for row in rows
    )
    result.degrade(
        "This capture does not embed HLSL for these shaders, so the disassembly is "
        "returned instead of original source.",
        reason="DXBC container carries ILDN (external PDB name) rather than ILDB/SPDB.",
        entry_points=entries,
        remedy=(
            "Search the UE5 tree for the entry point name to find the .usf, or pass "
            "--pdb-dirs pointing at the shader PDB output directory."
        ),
    )
    return result
