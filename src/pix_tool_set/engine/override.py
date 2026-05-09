"""State-level overrides for the exported replay C++ project (gap three).

An override is a pinned text rewrite of the export -- the same project
``shader-edit-apply`` patches, through a different door. No shader is touched;
instead the PSO descriptor text (blend, cull, depth, stencil, write mask) or the
command-list draw calls (skip / solo) are rewritten in place.

Two properties are load-bearing:

  * Every file touched gets a ``<name>.override-backup`` copy before the first
    rewrite, so ``restore_overrides`` can return the export byte-for-byte. No
    state exists that cannot be rolled back.
  * Every rewrite is reported with the lines it changed and the number of draws
    it affects, so a ``dry_run`` answers "is this override worth a rebuild"
    before any build is paid for.

Scope follows the same rule the shader-edit tools established: a PSO is shared
by many draws, so editing the PSO text affects every user. ``scope=draw`` clones
the PSO under a new id and repoints only the target draw at the clone, keeping
every other draw untouched.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BACKUP_SUFFIX = ".override-backup"

# Expanded D3D12 defaults, used when an override targets a state written as
# ``CD3DX12_*_DESC(D3D12_DEFAULT)`` -- the default form has no position to edit,
# so it is replaced with the explicit constructor carrying the override.
_RASTERIZER_DEFAULT = (
    "D3D12_FILL_MODE_SOLID, D3D12_CULL_MODE_BACK, FALSE, 0, 0.f, 0.f, TRUE, "
    "FALSE, FALSE, 0, D3D12_CONSERVATIVE_RASTERIZATION_MODE_OFF"
)
_DEPTH_STENCIL_DEFAULT = (
    "TRUE, D3D12_DEPTH_WRITE_MASK_ALL, D3D12_COMPARISON_FUNC_LESS, FALSE, 255, 255, "
    "D3D12_STENCIL_OP_KEEP, D3D12_STENCIL_OP_KEEP, D3D12_STENCIL_OP_KEEP, "
    "D3D12_COMPARISON_FUNC_ALWAYS, D3D12_STENCIL_OP_KEEP, D3D12_STENCIL_OP_KEEP, "
    "D3D12_STENCIL_OP_KEEP, D3D12_COMPARISON_FUNC_ALWAYS"
)

_CULL_MODES = {
    "front": "D3D12_CULL_MODE_FRONT",
    "back": "D3D12_CULL_MODE_BACK",
    "none": "D3D12_CULL_MODE_NONE",
}

# Colour-write channels. Isolating one channel is the fastest way to answer "which
# channel is wrong", so any combination is accepted rather than only the full set.
_WRITE_MASK_CHANNELS = {
    "R": "D3D12_COLOR_WRITE_ENABLE_RED",
    "G": "D3D12_COLOR_WRITE_ENABLE_GREEN",
    "B": "D3D12_COLOR_WRITE_ENABLE_BLUE",
    "A": "D3D12_COLOR_WRITE_ENABLE_ALPHA",
}


def parse_write_mask(value: str) -> tuple[str | None, str | None]:
    """Turn a channel string into the D3D12 write-mask expression.

    Accepts any combination of RGBA in any order plus ``NONE``/``0`` for "write
    nothing". Returns (expression, error); exactly one is ever non-None.

      ``RGBA`` -> ``D3D12_COLOR_WRITE_ENABLE_ALL``   (the all-channels shorthand)
      ``RG``   -> ``D3D12_COLOR_WRITE_ENABLE_RED | D3D12_COLOR_WRITE_ENABLE_GREEN``
      ``NONE`` -> ``0``
    """
    text = str(value or "").strip().upper()
    if not text:
        return None, "write_mask needs a channel set, e.g. write_mask=RGBA or write_mask=R"
    if text in ("NONE", "0"):
        return "0", None
    unknown = sorted(set(text) - set(_WRITE_MASK_CHANNELS))
    if unknown:
        return None, (
            f"unknown write_mask channel(s) {''.join(unknown)!r}; use any combination "
            "of R, G, B, A, or NONE"
        )
    channels = [channel for channel in "RGBA" if channel in text]
    if len(channels) == 4:
        return "D3D12_COLOR_WRITE_ENABLE_ALL", None
    return " | ".join(_WRITE_MASK_CHANNELS[channel] for channel in channels), None

# The draw APIs a skip/solo rewrite may act on, in the order the export emits them.
_DRAW_API_NAMES = (
    "DrawInstanced",
    "DrawIndexedInstanced",
    "Dispatch",
    "DispatchRays",
    "DispatchMesh",
    "ExecuteIndirect",
)


def parse_override(text: str) -> dict[str, Any]:
    """Turn one user-supplied override string into a spec dict."""
    kind, _, value = text.strip().partition("=")
    spec: dict[str, Any] = {"kind": kind}
    if value:
        spec["value"] = value
    return spec


def _backup(path: Path) -> None:
    backup = Path(str(path) + BACKUP_SUFFIX)
    if not backup.exists():
        backup.write_bytes(path.read_bytes())


def _write_text(path: Path, text: str) -> None:
    """Write ``text`` back preserving the file's original line endings.

    ``read_text`` normalises CRLF to LF, so a naive ``write_text`` would flip a
    CRLF export to LF and break the byte-for-byte restore promise. This writes
    with no translation and re-applies CRLF when the original file had it.
    """
    had_crlf = b"\r\n" in path.read_bytes()
    if had_crlf:
        text = text.replace("\n", "\r\n")
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _edit_lines(
    text: str, spec: dict[str, Any]
) -> tuple[str, list[dict[str, Any]]]:
    """Apply one PSO-state override to a CreatePSOs.cpp function body."""
    kind = spec["kind"]
    changes: list[dict[str, Any]] = []

    def rewrite(pattern: re.Pattern, replacement, what: str) -> str:
        nonlocal text
        new_text, count = pattern.subn(replacement, text)
        if count:
            changes.append({"what": what, "count": count})
        return new_text

    if kind == "cull":
        target = _CULL_MODES.get(str(spec.get("value", "")))
        if target is None:
            changes.append({"error": f"unknown cull value {spec.get('value')!r}"})
            return text, changes
        default = re.compile(r"CD3DX12_RASTERIZER_DESC\d?\(\s*D3D12_DEFAULT\s*\)")
        if default.search(text):
            expanded = _RASTERIZER_DEFAULT.replace("D3D12_CULL_MODE_BACK", target)
            text = rewrite(
                default,
                lambda _m: f"CD3DX12_RASTERIZER_DESC({expanded})",
                "expanded D3D12_DEFAULT rasterizer",
            )
        text = rewrite(
            re.compile(
                r"(CD3DX12_RASTERIZER_DESC\d?\(\s*D3D12_FILL_MODE_\w+\s*,\s*)"
                r"D3D12_CULL_MODE_\w+"
            ),
            rf"\g<1>{target}",
            "cull mode in constructor",
        )
        text = rewrite(
            re.compile(r"(RasterizerState\.CullMode\s*=\s*)D3D12_CULL_MODE_\w+"),
            rf"\g<1>{target}",
            "CullMode field assignment",
        )
    elif kind == "depth_test_off":
        text = rewrite(
            re.compile(r"(CD3DX12_DEPTH_STENCIL_DESC\d?\(\s*)TRUE"),
            r"\g<1>FALSE",
            "depth enable in constructor",
        )
        text = rewrite(
            re.compile(r"(DepthStencilState\.DepthEnable\s*=\s*)TRUE"),
            r"\g<1>FALSE",
            "DepthEnable field assignment",
        )
        default = re.compile(r"CD3DX12_DEPTH_STENCIL_DESC\d?\(\s*D3D12_DEFAULT\s*\)")
        if default.search(text):
            expanded = _DEPTH_STENCIL_DEFAULT.replace(
                "TRUE, D3D12_DEPTH_WRITE_MASK_ALL",
                "FALSE, D3D12_DEPTH_WRITE_MASK_ALL",
                1,
            )
            text = rewrite(
                default,
                lambda _m: f"CD3DX12_DEPTH_STENCIL_DESC({expanded})",
                "expanded D3D12_DEFAULT depth-stencil",
            )
    elif kind == "depth_write_off":
        text = rewrite(
            re.compile(
                r"(CD3DX12_DEPTH_STENCIL_DESC\d?\(\s*TRUE\s*,\s*)"
                r"D3D12_DEPTH_WRITE_MASK_\w+"
            ),
            r"\g<1>D3D12_DEPTH_WRITE_MASK_ZERO",
            "depth write mask in constructor",
        )
        text = rewrite(
            re.compile(
                r"(DepthStencilState\.DepthWriteMask\s*=\s*)D3D12_DEPTH_WRITE_MASK_\w+"
            ),
            r"\g<1>D3D12_DEPTH_WRITE_MASK_ZERO",
            "DepthWriteMask field assignment",
        )
        default = re.compile(r"CD3DX12_DEPTH_STENCIL_DESC\d?\(\s*D3D12_DEFAULT\s*\)")
        if default.search(text):
            expanded = _DEPTH_STENCIL_DEFAULT.replace(
                "D3D12_DEPTH_WRITE_MASK_ALL", "D3D12_DEPTH_WRITE_MASK_ZERO", 1
            )
            text = rewrite(
                default,
                lambda _m: f"CD3DX12_DEPTH_STENCIL_DESC({expanded})",
                "expanded D3D12_DEFAULT depth-stencil",
            )
    elif kind == "stencil_off":
        text = rewrite(
            re.compile(
                r"(CD3DX12_DEPTH_STENCIL_DESC\d?\(\s*(?:TRUE|FALSE)\s*,\s*"
                r"D3D12_DEPTH_WRITE_MASK_\w+\s*,\s*D3D12_COMPARISON_FUNC_\w+\s*,\s*)"
                r"TRUE"
            ),
            r"\g<1>FALSE",
            "stencil enable in constructor",
        )
        text = rewrite(
            re.compile(r"(DepthStencilState\.StencilEnable\s*=\s*)TRUE"),
            r"\g<1>FALSE",
            "StencilEnable field assignment",
        )
        default = re.compile(r"CD3DX12_DEPTH_STENCIL_DESC\d?\(\s*D3D12_DEFAULT\s*\)")
        if not changes:
            if default.search(text):
                # D3D12_DEFAULT already carries StencilEnable = FALSE, so there is
                # nothing to rewrite. Reported as a no-op so the caller can tell
                # "already off" apart from "the override found no place to apply".
                changes.append(
                    {
                        "what": "stencil already disabled (D3D12_DEFAULT)",
                        "count": 0,
                        "no_op": True,
                    }
                )
            else:
                changes.append(
                    {
                        "what": (
                            "stencil already disabled, or no stencil state written in "
                            "a recognised form"
                        ),
                        "count": 0,
                        "no_op": True,
                    }
                )
    elif kind == "blend_off":
        text = rewrite(
            re.compile(r"(blendDesc\.RenderTarget\[\d+\]\s*=\s*\{\s*)TRUE"),
            r"\g<1>FALSE",
            "BlendEnable in render target aggregate",
        )
        text = rewrite(
            re.compile(r"(RenderTarget\[\d+\]\.BlendEnable\s*=\s*)TRUE"),
            r"\g<1>FALSE",
            "BlendEnable field assignment",
        )
    elif kind == "write_mask":
        target, error = parse_write_mask(str(spec.get("value", "")))
        if error is not None or target is None:
            changes.append({"error": error})
            return text, changes
        text = rewrite(
            re.compile(
                r"(RenderTarget\[\d+\]\s*=\s*\{[^}]*?)"
                r"(?:D3D12_COLOR_WRITE_ENABLE_[\w]+(?:\s*\|\s*D3D12_COLOR_WRITE_ENABLE_[\w]+)*|0)"
                r"(\s*\})"
            ),
            lambda m: f"{m.group(1)}{target}{m.group(2)}",
            f"write mask in render target aggregate -> {target}",
        )
        text = rewrite(
            re.compile(
                r"(RenderTarget\[\d+\]\.RenderTargetWriteMask\s*=\s*)"
                r"(?:D3D12_COLOR_WRITE_ENABLE_[\w]+(?:\s*\|\s*D3D12_COLOR_WRITE_ENABLE_[\w]+)*|0)"
            ),
            lambda m: f"{m.group(1)}{target}",
            f"RenderTargetWriteMask field assignment -> {target}",
        )
    else:
        changes.append({"error": f"unknown override kind {kind!r}"})
    return text, changes


def _find_function_body(text: str, function: str) -> tuple[int, int] | None:
    """Start/end offsets of one ``void <function>() { ... }`` body."""
    marker = f"void {function}("
    start = text.find(marker)
    if start < 0:
        return None
    open_brace = text.find("{", start)
    if open_brace < 0:
        return None
    depth = 0
    for index in range(open_brace, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return open_brace, index + 1
    return None


def _clone_pso(
    text: str, pso_id: int, new_id: int, overrides: list[dict[str, Any]]
) -> tuple[str, list[dict[str, Any]]]:
    """Duplicate one CreatePipelineState_<id> body under a new id, then override it.

    Only self-references that speak in pipeline ids are repointed
    (``CreateAndTrackPipelineState(<id>`` / ``GetPipelineState(<id>)``); the
    remaining numbers in the body are sizes and indices that must stay put.
    """
    located = _find_function_body(text, f"CreatePipelineState_{pso_id}")
    if located is None:
        return text, [{"error": f"CreatePipelineState_{pso_id} not found"}]
    _brace, end = located
    marker = f"void CreatePipelineState_{pso_id}("
    start = text.find(marker)
    if start < 0:
        return text, [{"error": f"CreatePipelineState_{pso_id} signature not found"}]
    body = text[start:end]
    clone = body.replace(
        f"CreatePipelineState_{pso_id}", f"CreatePipelineState_{new_id}", 1
    )
    clone = re.sub(
        rf"(CreateAndTrackPipelineState\(\s*|GetPipelineState\(\s*){pso_id}\b",
        rf"\g<1>{new_id}",
        clone,
    )
    overridden, changes = apply_pso_overrides_to_text(clone, overrides)
    text = text[:end] + "\n" + overridden + text[end:]
    changes.append({"what": f"cloned PSO {pso_id} as {new_id}", "count": 1})
    return text, changes


def apply_pso_overrides_to_text(
    body: str, overrides: list[dict[str, Any]]
) -> tuple[str, list[dict[str, Any]]]:
    """Apply every PSO-state override to one function body text."""
    changed = body
    report: list[dict[str, Any]] = []
    for spec in overrides:
        changed, changes = _edit_lines(changed, spec)
        for entry in changes:
            entry["kind"] = spec["kind"]
        report.extend(changes)
    return changed, report


def _draw_call_lines(text: str) -> list[tuple[int, str, int | None]]:
    """(line number, line text, Global ID) of every draw call in one file.

    The export writes a ``// GlobalId = N`` comment directly above each recorded
    call, so the id is carried forward from the most recent comment and cleared
    once a draw consumes it: a draw with no comment above it reports None rather
    than silently inheriting the previous draw's id.

    Selection is deliberately not done here. ``skip_draw`` and ``solo_draw`` are
    opposite filters over this same list, and keeping both in one place is what
    stops them from drifting apart.
    """
    out: list[tuple[int, str, int | None]] = []
    pending_gid: int | None = None
    for number, line in enumerate(text.splitlines(), 1):
        match = re.search(r"//\s*GlobalId\s*=\s*(\d+)", line)
        if match:
            pending_gid = int(match.group(1))
            continue
        if any(f"->{api}(" in line for api in _DRAW_API_NAMES):
            out.append((number, line, pending_gid))
            pending_gid = None
    return out


def _rewrite_command_lists(
    root: Path, spec: dict[str, Any], target_global_ids: set[int] | None,
    dry_run: bool,
) -> dict[str, Any]:
    """Apply a skip_draw / solo_draw override across CommandLists_*.cpp.

    The two kinds are exact opposites over the same draw list:

      * ``skip_draw`` comments out the target draws, leaving the rest running.
      * ``solo_draw`` comments out every draw EXCEPT the targets, so the frame
        contains only them.

    Both therefore need a target set. ``solo_draw`` without one would comment out
    the whole frame, which is never a useful experiment, so it is rejected rather
    than performed.
    """
    kind = spec["kind"]
    report: dict[str, Any] = {
        "kind": kind, "files": [], "lines_changed": 0, "draws_kept": 0,
    }
    if kind not in ("skip_draw", "solo_draw"):
        return report

    targets = target_global_ids or set()
    if not targets:
        report["error"] = (
            f"{kind} needs a draw selector: skip_draw without a target changes "
            "nothing, and solo_draw without one would comment out every draw in "
            "the frame."
        )
        return report

    files = sorted(root.glob("CommandLists_*.cpp"))
    for path in files:
        original_text = path.read_text(encoding="utf-8", errors="replace")
        lines = original_text.splitlines()
        changed = 0
        for number, _line, global_id in _draw_call_lines(original_text):
            is_target = global_id is not None and global_id in targets
            # skip acts on the targets; solo acts on everything but the targets.
            should_comment = is_target if kind == "skip_draw" else not is_target
            if not should_comment:
                if kind == "solo_draw":
                    report["draws_kept"] += 1
                continue
            stripped = lines[number - 1].lstrip()
            if stripped.startswith("//"):
                continue
            lines[number - 1] = "// pix-tool-set override: " + lines[number - 1]
            changed += 1
        if changed and not dry_run:
            _backup(path)
            joined = "\n".join(lines)
            if original_text.endswith("\n"):
                joined += "\n"
            _write_text(path, joined)
            report["files"].append(str(path))
        report["lines_changed"] += changed
    return report


def _repoint_draw(
    root: Path, pso_id: int, new_id: int, global_ids: set[int], dry_run: bool
) -> dict[str, Any]:
    """Point the target draws at the cloned PSO instead of the original."""
    report: dict[str, Any] = {"files": [], "draws_repointed": 0}
    pattern = re.compile(rf"SetPipelineState\(\s*GetPipelineState\(\s*{pso_id}\s*\)\s*\)")
    replacement = f"SetPipelineState(GetPipelineState({new_id}))"
    files = sorted(root.glob("CommandLists_*.cpp"))
    for path in files:
        original_text = path.read_text(encoding="utf-8", errors="replace")
        lines = original_text.splitlines()
        pending_gid: int | None = None
        changed = 0
        for number, line in enumerate(lines, 1):
            match = re.search(r"//\s*GlobalId\s*=\s*(\d+)", line)
            if match:
                pending_gid = int(match.group(1))
                continue
            if pending_gid in global_ids and pattern.search(line):
                lines[number - 1], count = pattern.subn(replacement, line)
                changed += count
                pending_gid = None
        if changed and not dry_run:
            _backup(path)
            joined = "\n".join(lines)
            if original_text.endswith("\n"):
                joined += "\n"
            _write_text(path, joined)
            report["files"].append(str(path))
        report["draws_repointed"] += changed
    return report


@dataclass(slots=True)
class OverrideReport:
    experiment_id: str = ""
    scope: str = "draw"
    pso_id: int | None = None
    new_pso_id: int | None = None
    dry_run: bool = False
    files_touched: list[str] = field(default_factory=list)
    changes: list[dict[str, Any]] = field(default_factory=list)
    affected_draw_count: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "scope": self.scope,
            "pso_id": self.pso_id,
            "cloned_pso_id": self.new_pso_id,
            "dry_run": self.dry_run,
            "files_touched": self.files_touched,
            "changes": self.changes,
            "affected_draw_count": self.affected_draw_count,
            "notes": self.notes,
        }


def apply_override(
    root: Path,
    *,
    overrides: list[dict[str, Any]],
    pso_id: int | None,
    target_global_ids: set[int] | None,
    scope: str = "draw",
    dry_run: bool = False,
    affected_draw_count: int = 0,
) -> OverrideReport:
    """Apply one override experiment to the export; roll back with restore_overrides."""
    report = OverrideReport(
        experiment_id=f"exp-{int(time.time())}",
        scope=scope,
        pso_id=pso_id,
        dry_run=dry_run,
        affected_draw_count=affected_draw_count,
    )
    report.notes.append(
        "Overrides rewrite the exported C++ replay project only; the .wpix capture "
        "is never modified. Run replay-render to see the effect, replay-reset to undo."
    )

    pso_overrides = [
        spec for spec in overrides if spec["kind"] not in ("skip_draw", "solo_draw")
    ]
    draw_overrides = [
        spec for spec in overrides if spec["kind"] in ("skip_draw", "solo_draw")
    ]

    if pso_overrides:
        if pso_id is None:
            report.changes.append(
                {"error": "PSO-state overrides need a target pso_id or draw selector"}
            )
            return report
        create_psos = root / "CreatePSOs.cpp"
        if not create_psos.exists():
            report.changes.append({"error": "CreatePSOs.cpp not found in the export"})
            return report
        text = create_psos.read_text(encoding="utf-8", errors="replace")
        located = _find_function_body(text, f"CreatePipelineState_{pso_id}")
        if located is None:
            report.changes.append(
                {"error": f"CreatePipelineState_{pso_id} not found in CreatePSOs.cpp"}
            )
            return report
        start, end = located

        if scope == "pso":
            body = text[start:end]
            overridden, changes = apply_pso_overrides_to_text(body, pso_overrides)
            if changes:
                if not dry_run:
                    _backup(create_psos)
                    _write_text(create_psos, text[:start] + overridden + text[end:])
                report.files_touched.append(str(create_psos))
            report.changes.extend(changes)
        else:  # scope == "draw": clone the PSO, repoint only the target draws
            if not target_global_ids:
                report.changes.append(
                    {
                        "error": (
                            "scope=draw needs a draw selector so only the target draw "
                            "is repointed at the clone"
                        )
                    }
                )
                return report
            new_id = 9000000 + int(pso_id)
            report.new_pso_id = new_id
            text, changes = _clone_pso(text, pso_id, new_id, pso_overrides)
            if not dry_run:
                _backup(create_psos)
                _write_text(create_psos, text)
            report.files_touched.append(str(create_psos))
            report.changes.extend(changes)
            repoint = _repoint_draw(root, pso_id, new_id, target_global_ids, dry_run)
            report.changes.append(
                {
                    "what": "repointed target draws to the cloned PSO",
                    "count": repoint["draws_repointed"],
                }
            )

    for spec in draw_overrides:
        row = _rewrite_command_lists(root, spec, target_global_ids, dry_run)
        report.files_touched.extend(row["files"])
        entry: dict[str, Any] = {
            "kind": spec["kind"],
            "count": row["lines_changed"],
            "files": row["files"],
        }
        if row.get("error"):
            entry["error"] = row["error"]
        if spec["kind"] == "solo_draw":
            entry["draws_kept"] = row.get("draws_kept", 0)
            entry["what"] = (
                f"commented out {row['lines_changed']} draw(s), kept "
                f"{row.get('draws_kept', 0)}"
            )
        report.changes.append(entry)

    return report


def restore_overrides(root: Path) -> list[dict[str, Any]]:
    """Roll every override back: restore backups and drop the backup files."""
    actions: list[dict[str, Any]] = []
    for path in sorted(Path(root).glob("*" + BACKUP_SUFFIX)):
        original = Path(str(path)[: -len(BACKUP_SUFFIX)])
        path.replace(original)
        actions.append({"action": "restored", "file": str(original)})
    return actions
