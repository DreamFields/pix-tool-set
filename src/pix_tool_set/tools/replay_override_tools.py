"""State-level overrides and the hypothesis bisector (gap three).

``replay-override`` rewrites the exported replay project's fixed-function state
without touching a shader: turn blending off, flip cull, drop depth test/write,
disable stencil, force the write mask, or comment out draws. Every override is
a pinned text edit with a backup, so ``replay-reset`` restores the export
byte-for-byte. ``bisect-render-state`` automates the elimination loop over a
candidate override set against a pixel-region judge.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

from ..context import ToolContext
from ..engine import override as override_mod
from ..engine import screencap
from ..engine.editledger import EditLedger
from ..errors import PixToolError, invalid_argument
from ..results import ToolResult
from ._common import DRAW_SELECTOR, resolve_draw, tool, with_session
from .replay_render_tools import (
    _await_content,
    _await_window,
    _configure_and_build,
    _export_root,
)

_OVERRIDE_NOTE = (
    "Overrides are pinned text rewrites of the exported C++ replay project; the "
    ".wpix capture is never modified, exactly like shader-edit-apply. Each touched "
    "file is backed up first, so replay-reset restores the export byte-for-byte. A "
    "PSO is shared by many draws: scope=pso edits the PSO itself and affects every "
    "user (reported as affected_draw_count); scope=draw clones the PSO under a new "
    "id and repoints only the target draw, leaving every other draw untouched. Run "
    "--dry-run first to see which lines change before paying for a rebuild."
)

_KNOWN_OVERRIDES = (
    "blend_off",
    "cull=front|back|none",
    "depth_test_off",
    "depth_write_off",
    "stencil_off",
    "write_mask=RGBA|any channel combination|NONE",
    "skip_draw",
    "solo_draw",
)


def _target_from_args(capture, args: dict[str, Any]) -> tuple[int | None, set[int]]:
    """Resolve pso_id and target global ids from the selectors."""
    pso_id = args.get("pso_id")
    global_ids: set[int] = set()
    if pso_id is None:
        draw = resolve_draw(capture, args, what="draw for state override")
        if draw.pso_id is None:
            raise invalid_argument(
                "draw_index/global_id/queue_id",
                "The selected action binds no PSO, so there is no pipeline state "
                "to override. Use a draw with a bound PSO.",
            )
        pso_id = draw.pso_id
        if draw.global_id is not None:
            global_ids = {draw.global_id}
    return int(pso_id), global_ids


@tool(
    name="replay-override",
    summary=(
        "Rewrite the exported replay project's fixed-function state without touching "
        "shaders: blend_off, cull=none/front/back, depth_test_off, depth_write_off, "
        "stencil_off, write_mask=<any RGBA subset or NONE>, skip_draw, solo_draw. A "
        "pinned text edit with a backup, undone by replay-reset."
    ),
    category="meta",
    parameters=with_session(
        DRAW_SELECTOR,
        pso_id={
            "type": "integer",
            "description": "Pipeline state to override. Alternative to a draw selector.",
        },
        overrides={
            "type": "array",
            "items": {"type": "string"},
            "description": f"Override list, e.g. ['blend_off', 'cull=none']. Known: {', '.join(_KNOWN_OVERRIDES)}",
        },
        scope={
            "type": "string",
            "enum": ["pso", "draw"],
            "description": (
                "pso edits the shared PSO (affects every draw using it); draw clones "
                "the PSO and repoints only the selected draw. Default draw."
            ),
        },
        dry_run={
            "type": "boolean",
            "description": "Report which lines would change without writing anything.",
        },
        label={
            "type": "string",
            "description": "Short name for the experiment, recorded in the ledger.",
        },
        required=["overrides"],
    ),
    returns="What changed (or would change): files, line edits, affected draw count, clone id.",
    examples=[
        "pix-tool-set replay-override --draw-index 2461 --overrides blend_off",
        "pix-tool-set replay-override --pso-id 3184 --overrides 'cull=none' --scope pso --dry-run",
        "pix-tool-set replay-override --draw-index 2461 --overrides skip_draw",
        "pix-tool-set replay-override --draw-index 2461 --overrides solo_draw",
        "pix-tool-set replay-override --draw-index 2461 --overrides 'write_mask=R'",
    ],
    notes=_OVERRIDE_NOTE,
)
def replay_override(args: dict[str, Any], context: ToolContext) -> ToolResult:
    root = _export_root(context, args)
    overrides = [override_mod.parse_override(item) for item in args["overrides"]]
    unknown = [
        spec["kind"]
        for spec in overrides
        if spec["kind"]
        not in (
            "blend_off", "cull", "depth_test_off", "depth_write_off",
            "stencil_off", "write_mask", "skip_draw", "solo_draw",
        )
    ]
    if unknown:
        raise invalid_argument("overrides", f"unknown override kind(s): {', '.join(unknown)}")

    capture = context.capture(args)
    pso_id, global_ids = _target_from_args(capture, args)
    scope = str(args.get("scope") or "draw")
    dry_run = bool(args.get("dry_run"))

    affected = 0
    if scope == "pso":
        affected = sum(1 for d in capture.draw_calls if d.pso_id == pso_id)
    else:
        affected = len(global_ids)

    report = override_mod.apply_override(
        root,
        overrides=overrides,
        pso_id=pso_id,
        target_global_ids=global_ids,
        scope=scope,
        dry_run=dry_run,
        affected_draw_count=affected,
    )

    data = {
        **report.to_dict(),
        "label": args.get("label") or report.experiment_id,
        "known_overrides": list(_KNOWN_OVERRIDES),
    }

    if not dry_run:
        ledger = EditLedger(root)
        ledger.add_experiment(
            experiment_id=report.experiment_id,
            label=str(args.get("label") or ""),
            overrides=args["overrides"],
            scope=scope,
            pso_id=pso_id,
            files_touched=report.files_touched,
            changes=report.changes,
        )
        data["ledger_recorded"] = True

    result = ToolResult.success(data)
    if dry_run:
        result.add_diagnostic(
            "info",
            "Dry run: nothing was written. Re-run without --dry-run to apply, then "
            "replay-render to observe, replay-reset to undo.",
        )
    else:
        result.add_diagnostic(
            "info",
            "Applied to the export only. Run replay-render to capture the result, "
            "snapshot-compare to diff against the baseline, replay-reset to undo.",
        )
    if scope == "pso" and affected > 1:
        result.add_diagnostic(
            "warning",
            f"scope=pso edits the shared PSO {pso_id}, affecting {affected} draws. "
            "Use scope=draw to isolate the change to one draw.",
        )
    return result


#: Sampling budget for the region judge. A pure-Python per-pixel loop over a large
#: region costs seconds, and the bisector calls it once per round, so the region is
#: sub-sampled on a regular grid above this many pixels. The judge answers "did the
#: region get brighter/darker", which a uniform grid answers just as well as an
#: exhaustive walk -- but max_luminance can miss a lone bright pixel, so the sample
#: step is reported alongside the metrics rather than hidden.
_JUDGE_SAMPLE_BUDGET = 65536


def _region_luminance(
    bgra: bytearray, width: int, height: int, region: dict[str, Any]
) -> dict[str, float]:
    """Mean/max luminance inside a pixel region of a BGRA frame.

    Clamps the region to the frame, then walks it on a stride chosen so at most
    ``_JUDGE_SAMPLE_BUDGET`` pixels are read. ``sample_step`` of 1 means every
    pixel in the region was examined.
    """
    x0 = max(0, min(int(region.get("x", 0)), width - 1))
    y0 = max(0, min(int(region.get("y", 0)), height - 1))
    x1 = max(x0 + 1, min(int(region.get("x", 0)) + int(region.get("width", width)), width))
    y1 = max(y0 + 1, min(int(region.get("y", 0)) + int(region.get("height", height)), height))

    span = (x1 - x0) * (y1 - y0)
    step = 1
    while span // (step * step) > _JUDGE_SAMPLE_BUDGET:
        step += 1

    total = 0.0
    peak = 0.0
    count = 0
    for y in range(y0, y1, step):
        row = y * width * 4
        for x in range(x0, x1, step):
            offset = row + x * 4
            b, g, r = bgra[offset], bgra[offset + 1], bgra[offset + 2]
            lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0
            total += lum
            if lum > peak:
                peak = lum
            count += 1
    return {
        "mean_luminance": total / count if count else 0.0,
        "max_luminance": peak,
        "pixel_count": count,
        "region_pixels": span,
        "sample_step": step,
    }


def _judge_holds(metrics: dict[str, float], judge: dict[str, Any]) -> bool:
    metric = judge.get("metric", "mean_luminance")
    value = float(metrics.get(metric, 0.0))
    threshold = float(judge.get("value", 0.0))
    op = judge.get("op", ">")
    return {
        ">": value > threshold,
        "<": value < threshold,
        ">=": value >= threshold,
        "<=": value <= threshold,
        "==": abs(value - threshold) < 1e-6,
    }.get(op, False)


def _render_and_evaluate(
    root: Path,
    judge: dict[str, Any],
    args: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, float]]:
    """Build + run the replay, grab the frame, evaluate the region judge."""
    settle = int(args.get("settle_seconds") or 150)
    timeout = int(args.get("build_timeout") or 1800)
    generator = str(args.get("generator") or "Visual Studio 18 2026")
    steps = _configure_and_build(
        root, generator, timeout, bool(args.get("force_reconfigure")), args
    )
    exe = Path(steps["executable"])
    process = subprocess.Popen([str(exe)], cwd=str(root))
    run_info: dict[str, Any] = {"build": steps, "pid": process.pid}
    try:
        deadline = time.time() + settle
        window = _await_window(process.pid, deadline, min_pixels=200 * 200)
        if window is None:
            raise PixToolError(
                code="replay_window_unavailable",
                message="The replay never produced a window with pixels to read.",
                stage="bisect",
                suggestion="Raise --settle-seconds.",
            )
        awaited = _await_content(window.hwnd, deadline, min_score=0.02)
        if awaited is None:
            raise PixToolError(
                code="window_capture_failed",
                message="No usable pixels were captured from the replay window.",
                stage="bisect",
                suggestion="Make sure the window is visible.",
            )
        pixels, width, height, _method, wait_info = awaited
        metrics = _region_luminance(pixels, width, height, judge.get("region", {}))
        metrics["holds"] = _judge_holds(metrics, judge)
        run_info["wait"] = wait_info
        return run_info, metrics
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()


@tool(
    name="bisect-render-state",
    summary=(
        "Automatically shrink a candidate override set to the smallest subset that "
        "reproduces (or removes) a screen symptom, judged by a pixel-region metric "
        "over the replayed frame. The agent supplies the hypothesis list; the tool "
        "runs the elimination loop."
    ),
    category="meta",
    parameters=with_session(
        DRAW_SELECTOR,
        pso_id={"type": "integer", "description": "Pipeline state the overrides apply to."},
        overrides={
            "type": "array",
            "items": {"type": "string"},
            "description": f"Candidate overrides. Known: {', '.join(_KNOWN_OVERRIDES)}",
        },
        judge={
            "type": "object",
            "description": (
                "Region and predicate to judge the frame. e.g. "
                '{"region": {"x": 0, "y": 0, "width": 200, "height": 200}, '
                '"metric": "mean_luminance", "op": ">", "value": 0.01}'
            ),
        },
        scope={"type": "string", "enum": ["pso", "draw"], "description": "Default pso."},
        settle_seconds={"type": "integer", "description": "Seconds to wait for the frame. Default 150."},
        build_timeout={"type": "integer", "description": "Build timeout. Default 1800."},
        generator={"type": "string", "description": "CMake generator."},
        force_reconfigure={"type": "boolean", "description": "Wipe and reconfigure the build."},
        dry_run={
            "type": "boolean",
            "description": "Print the planned rounds without building anything.",
        },
        required=["overrides", "judge"],
    ),
    returns="The minimal override subset, per-round metrics, and the final verdict.",
    examples=[
        "pix-tool-set bisect-render-state --pso-id 3184 "
        "--overrides blend_off depth_test_off write_mask=RGBA "
        "--judge '{\"region\":{\"x\":0,\"y\":0,\"width\":200,\"height\":200}}'",
    ],
    notes=(
        "Every round rebuilds and reruns the export, so each round costs minutes on "
        "a large capture; start with --dry-run to see the round plan. The judge reads "
        "the presented frame: a symptom that never reaches the backbuffer cannot be "
        "bisected this way (use read-uav instead)."
    ),
)
def bisect_render_state(args: dict[str, Any], context: ToolContext) -> ToolResult:
    root = _export_root(context, args)
    capture = context.capture(args)
    pso_id, global_ids = _target_from_args(capture, args)
    overrides = [override_mod.parse_override(item) for item in args["overrides"]]
    judge = args["judge"] or {}
    scope = str(args.get("scope") or "pso")

    # skip_draw / solo_draw select by Global ID, so they are only meaningful with a
    # draw selector. Rejecting up front beats discovering it after a build round.
    draw_kinds = [
        spec["kind"] for spec in overrides if spec["kind"] in ("skip_draw", "solo_draw")
    ]
    if draw_kinds and not global_ids:
        raise invalid_argument(
            "draw_index/global_id/queue_id",
            f"{', '.join(sorted(set(draw_kinds)))} select draws by Global ID, so a "
            "draw selector is required. Pass --draw-index (or --global-id) instead "
            "of only --pso-id.",
        )

    rounds: list[dict[str, Any]] = []
    plan: list[list[str]] = []

    def evaluate(subset: list[dict[str, Any]]) -> bool:
        override_mod.restore_overrides(root)
        report = override_mod.apply_override(
            root,
            overrides=subset,
            pso_id=pso_id,
            target_global_ids=global_ids,
            scope=scope,
            dry_run=False,
        )
        errors = [c["error"] for c in report.changes if c.get("error")]
        run_info, metrics = _render_and_evaluate(root, judge, args)
        rounds.append(
            {
                "subset": [f"{s['kind']}{'=' + s['value'] if s.get('value') else ''}" for s in subset],
                "metrics": metrics,
                "override_errors": errors,
                "build_seconds": (run_info.get("build") or {}).get("build", {}).get("seconds"),
            }
        )
        return bool(metrics.get("holds"))

    if args.get("dry_run"):
        # Mirror the real loop: one round for the full set, then one attempt per
        # candidate. Reporting fewer rounds than the run performs would make an
        # agent underestimate the cost, which is the whole point of asking first.
        labels = [
            f"{s['kind']}{'=' + s['value'] if s.get('value') else ''}" for s in overrides
        ]
        plan.append(list(labels))
        if len(labels) > 1:
            for index in range(len(labels)):
                plan.append([label for i, label in enumerate(labels) if i != index])
        return ToolResult.success(
            {
                "dry_run": True,
                "planned_rounds": len(plan),
                "round_subsets": plan,
                "note": (
                    "Each round rebuilds and reruns the export. Round 1 applies the "
                    "full set; each later round attempts to drop one override and "
                    "keeps the drop only if the judge still holds. This is the "
                    "worst-case plan: a successful drop shrinks the set, so some "
                    "later attempts become cheaper or are skipped once one "
                    "override remains."
                ),
            }
        )

    if not evaluate(overrides):
        override_mod.restore_overrides(root)
        result = ToolResult.success(
            {
                "minimal_overrides": None,
                "reproducible": False,
                "rounds": rounds,
                "conclusion": (
                    "The full override set does not satisfy the judge, so the symptom is "
                    "not driven by these states, or it never reaches the backbuffer. "
                    "Check with read-uav whether the pass output changes at all."
                ),
            }
        )
        result.degrade("The judge did not hold under the full override set.")
        return result

    # Greedy elimination: try to drop each candidate once, keeping the drop when
    # the judge still holds. Iterating over a fixed snapshot of the candidates
    # and rebuilding the subset by identity is what keeps this correct -- indexing
    # into ``current`` would go stale the moment a round shortens it.
    current = list(overrides)
    for candidate_spec in list(overrides):
        if len(current) <= 1:
            break
        trial = [spec for spec in current if spec is not candidate_spec]
        if len(trial) == len(current):
            continue
        if evaluate(trial):
            current = trial

    override_mod.restore_overrides(root)
    minimal = [
        f"{s['kind']}{'=' + s['value'] if s.get('value') else ''}" for s in current
    ]
    result = ToolResult.success(
        {
            "minimal_overrides": minimal,
            "minimal_count": len(minimal),
            "reproducible": True,
            "rounds": rounds,
            "conclusion": (
                f"The minimal subset is {minimal or 'empty'}: removing any of these "
                "breaks the judge. Apply it with replay-override and diff with "
                "snapshot-compare to close the loop."
            ),
        }
    )
    result.add_diagnostic(
        "info",
        f"Bisection took {len(rounds)} round(s) of build+run. Overrides were rolled "
        "back after the run; re-apply the minimal subset with replay-override.",
    )
    return result
