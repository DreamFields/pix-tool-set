"""Acceptance targets for making Global ID an accepted selector.

Written before the implementation lands, deliberately: these three ids came from a
user reading them off the PIX GUI, and they are the whole point of the change.

    Global ID 5099 -> pass "CompactTraces WaveOps:1"
    Global ID 3893 -> pass "TileClassificationMark"
    Global ID 5367 -> pass "ReflectionHardwareRayTracingRGS hit-lighting"

They were chosen -- or rather, they chose themselves -- because all three fail for
different reasons, and a fix that handles one is not a fix:

  * 3893 is a Dispatch on the exported 3D queue. The engine already resolves it
    (``find_pass_by_event(global_id=3893)`` returns the pass today); only the tool
    layer refuses to accept the parameter. Cheap.
  * 5099 does not exist anywhere. Not in the C++ export, not in the event list CSV.
    It is the sub-Dispatch PIX expands out of the ExecuteIndirect at 5098, on the
    *compute* queue -- so the CSV has no row for it, and the export records only the
    ExecuteIndirect itself. Reaching it needs both a new expansion rule and a
    pass lookup that does not go through the CSV.
  * 5367 is a DispatchRays. It has a CSV row (Queue ID 20649) so the pass lookup
    already works, but no DrawCall carries it: the export replays a DispatchRays as
    ``ExecuteIndirect(GetCommandSignature(3890))`` where signature 3890 has
    ``command_type = DISPATCH_RAYS``. The action is 5366 / draw 2711, so the same
    N-1 expansion rule applies -- which is the point of including it, since a rule
    validated on one shape of id is not validated.

Reaching 5367 exposes a hazard the other two do not, and it is asserted on here
because arriving at a wrong answer confidently is worse than not arriving:

    The export sets the raytracing pipeline with ``SetPipelineState1(StateObject
    3930)`` on the line before. State objects are not modelled, so the parser
    reports the last plain ``SetPipelineState`` it saw instead -- compute PSO 3883,
    99 lines earlier. ``shader-bindings`` therefore answers a DispatchRays with a
    CS shader that has nothing to do with it. Once global_id resolves, that wrong
    answer becomes reachable by the very id the user asked about.

Three traps are asserted against explicitly, because each is tempting and each
gives a confident wrong answer:

  * Range containment. 5099 falls inside the gid range of *three* passes
    (LumenReflections, a ClearBuffer, and the target), two of which are exactly as
    narrow as each other. Picking by "which pass range contains it" is a coin flip.
  * Nearest preceding action. Correct on these ids by luck, wrong for 178 of the
    221 unused ids in the range, where the preceding command is a
    WriteBufferImmediate or a barrier and nothing legitimises the jump.
  * Inheriting the previous pipeline across a SetPipelineState1. See above.

Usage:
    python tests/acceptance_global_id_selector.py [session-name]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set import call_tool  # noqa: E402
from pix_tool_set.context import ToolContext, clear_capture_cache  # noqa: E402
from pix_tool_set.session import SessionStore  # noqa: E402

SESSION = sys.argv[1] if len(sys.argv) > 1 else "Tiled"

# The three targets, plus everything measured about them that the fix must respect.
TARGETS = [
    {
        "global_id": 5099,
        "pass_name": "CompactTraces WaveOps:1",
        "pass_index": 320,
        # Not an id of anything in the export: it is the ExecuteIndirect's expansion.
        "resolves_via": "execute_indirect_expansion",
        "expanded_from_global_id": 5098,
        "draw_index": 2671,
        "api": "ExecuteIndirect",
        # On the compute queue, so no Queue ID exists and none may be invented.
        "queue_id": None,
        "queue_name": "Compute Queue (GPU 0)",
        "in_event_csv": False,
        # ``kind``/``api`` must keep describing the real API call, which is an
        # ExecuteIndirect. What the GPU ends up running is the command signature's
        # command_type, already parsed but never surfaced as something queryable.
        "api_kind": "execute_indirect",
        "effective_kind": "dispatch",
    },
    {
        "global_id": 3893,
        "pass_name": "TileClassificationMark",
        "pass_index": 140,
        "resolves_via": "direct",
        "expanded_from_global_id": None,
        "draw_index": 2475,
        "api": "Dispatch",
        "queue_id": 18704,
        "queue_name": "3D Queue (GPU 0)",
        "in_event_csv": True,
        "api_kind": "dispatch",
        "effective_kind": "dispatch",
    },
    {
        "global_id": 5367,
        "pass_name": "ReflectionHardwareRayTracingRGS hit-lighting",
        "pass_index": 347,
        "resolves_via": "execute_indirect_expansion",
        "expanded_from_global_id": 5366,
        "draw_index": 2711,
        "api": "ExecuteIndirect",
        "queue_id": 20648,
        "queue_name": "3D Queue (GPU 0)",
        # Unlike 5099 this id does have a CSV row of its own (Queue ID 20649),
        # which is why the pass lookup succeeds today while the draw lookup does not.
        "in_event_csv": True,
        "api_kind": "execute_indirect",
        # Command signature 3890 is DISPATCH_RAYS. Nothing in the frame is
        # findable as raytracing work today: 0 actions report a raytracing kind,
        # so anyone asking "where does this frame trace rays" gets nothing.
        "effective_kind": "dispatch_rays",
    },
]

# The second DispatchRays in the frame, kept as a pair so a fix cannot be tuned to
# one instance. Its ExecuteIndirect (draw 2705) reports pso_id=0 rather than a wrong
# one, so it tests the "no pipeline at all" branch of the same hazard.
SECOND_RAY = {
    "global_id": 5312,
    "expanded_from_global_id": 5311,
    "draw_index": 2705,
    "pass_name": "ReflectionHardwareRayTracingRGS default",
}

# The raytracing pipeline the export really binds before draw 2711, and the compute
# PSO the parser currently reports in its place.
RT_STATE_OBJECT_ID = 3930
STALE_PSO_ID = 3883

# 3893 is also a valid row number in the event list, where it is an
# IASetVertexBuffers. The same integer therefore names two unrelated things
# depending on which parameter it is passed as, which is why the cross-hint below
# is part of the acceptance criteria and not a nicety.
CONFUSABLE_ID = 3893
CONFUSABLE_ROW_NAME = "IASetVertexBuffers"

PASSED: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    PASSED.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


def run(tool: str, **args):
    args.setdefault("session", SESSION)
    return call_tool(tool, args)


def main() -> int:
    if SessionStore().get(SESSION) is None:
        print(f"No session named {SESSION!r}.")
        return 2
    clear_capture_cache()
    capture = ToolContext.from_cwd().capture({"session": SESSION})

    print("=" * 78)
    print(f"Global ID selector acceptance on {SESSION}")
    print("=" * 78)

    for target in TARGETS:
        gid = target["global_id"]
        print(f"\nGlobal ID {gid} -> pass {target['pass_name']!r} "
              f"(via {target['resolves_via']})")

        # 1. the engine must place the id on a pass
        entry = capture.find_pass_by_event(global_id=gid)
        check(
            "engine: find_pass_by_event resolves it",
            entry is not None and entry["name"] == target["pass_name"],
            f"got={entry and (entry['pass_index'], entry['name'])}",
        )

        # 2. the engine must place it on the action that carries the work
        draw = capture.resolve_draw(global_id=gid)
        check(
            "engine: resolve_draw lands on the right action",
            draw is not None
            and draw.index == target["draw_index"]
            and draw.api == target["api"],
            f"got={draw and (draw.index, draw.api)}",
        )

        # 3. find-pass must accept it as a parameter, not just report it
        payload = run("find-pass", global_id=gid)
        matched = (
            (payload.get("data") or {}).get("matches", [{}])[0]
            if payload["status"] != "error"
            else {}
        )
        check(
            "find-pass --global-id names the pass",
            matched.get("name") == target["pass_name"],
            f"status={payload['status']} name={matched.get('name')!r}",
        )

        # 4. a tool that reads real data must work off it too
        payload = run("draw-state", global_id=gid)
        state_index = (
            (payload.get("data") or {}).get("draw_call", {}).get("draw_index")
            if payload["status"] != "error"
            else None
        )
        check(
            "draw-state --global-id reaches the action",
            state_index == target["draw_index"],
            f"status={payload['status']} draw_index={state_index}",
        )

        # 5. an id that had to be redirected must say so, out loud. Answering
        #    "5099 is draw 2671" without mentioning that 5099 is the expansion of
        #    5098 hides the one fact needed to check the answer.
        if target["resolves_via"] == "execute_indirect_expansion":
            messages = " ".join(
                d.get("message", "") for d in payload.get("diagnostics", [])
            )
            check(
                "the ExecuteIndirect redirection is disclosed",
                str(target["expanded_from_global_id"]) in messages
                and "executeindirect" in messages.lower(),
                f"diagnostics={messages[:120]!r}",
            )

        # 6. no invented Queue ID. The compute queue has no event list; a
        #    plausible-looking id here would address an unrelated row.
        check(
            "queue_id is reported honestly",
            draw is not None and draw.queue_id == target["queue_id"],
            f"queue_id={draw and draw.queue_id} expected={target['queue_id']}",
        )
        check(
            "the queue is still named",
            draw is not None and draw.queue_name == target["queue_name"],
            f"queue_name={draw and draw.queue_name!r}",
        )

        # 7. the API call must keep being described truthfully -- draw 2711 really
        #    is an ExecuteIndirect -- while what the GPU runs becomes queryable.
        #    Overwriting ``kind`` with 'dispatch_rays' would trade one wrong answer
        #    for another; a derived field costs nothing and breaks nothing.
        check(
            "kind still names the real API call",
            draw is not None and draw.kind.value == target["api_kind"],
            f"kind={draw and draw.kind.value} expected={target['api_kind']}",
        )
        payload = run("action-info", global_id=gid)
        data = payload.get("data") or {}
        check(
            "the work the GPU actually runs is exposed",
            data.get("effective_kind") == target["effective_kind"],
            f"effective_kind={data.get('effective_kind')!r} "
            f"expected={target['effective_kind']!r}",
        )

    print("\nraytracing work must be findable at all")
    payload = run("find-draw-calls", effective_kind="dispatch_rays")
    found = [d.get("draw_index") for d in ((payload.get("data") or {}).get("draw_calls") or [])]
    check(
        "the frame's two DispatchRays can be listed",
        sorted(found) == [SECOND_RAY["draw_index"], 2711],
        f"status={payload['status']} draw_indices={found}",
    )

    print("\nDispatchRays: the pipeline must not be inherited from the previous PSO")

    # SetPipelineState1 binds a state object, which is not modelled. The parser
    # keeps the last plain SetPipelineState instead, so a DispatchRays is answered
    # with an unrelated compute shader. Whatever the fix does -- model state
    # objects, or report nothing -- it must not keep presenting 3883 as the
    # pipeline of a raytracing dispatch.
    ray = next(t for t in TARGETS if t["effective_kind"] == "dispatch_rays")
    ray_draw = capture.draw_calls[ray["draw_index"]]
    check(
        "the stale compute PSO is no longer reported for a DispatchRays",
        ray_draw.pso_id != STALE_PSO_ID,
        f"pso_id={ray_draw.pso_id} (stale={STALE_PSO_ID})",
    )
    payload = run("shader-bindings", draw_index=ray["draw_index"])
    stages = [
        s.get("stage") for s in ((payload.get("data") or {}).get("stages") or [])
    ]
    check(
        "shader-bindings does not answer a DispatchRays with a CS",
        stages != ["CS"],
        f"status={payload['status']} stages={stages}",
    )
    if payload["status"] != "error":
        messages = " ".join(d.get("message", "") for d in payload.get("diagnostics", []))
        check(
            "and if the pipeline is unavailable it says so",
            bool(stages) or "state object" in messages.lower(),
            f"diagnostics={messages[:120]!r}",
        )
    # The RT pipeline the export really binds, so a fix can be checked against it.
    check(
        "the raytracing state object is reachable, or explicitly reported as unmodelled",
        capture.pipeline_state(RT_STATE_OBJECT_ID) is not None
        or ray_draw.pso_id is None,
        f"state_object({RT_STATE_OBJECT_ID})="
        f"{capture.pipeline_state(RT_STATE_OBJECT_ID)} pso_id={ray_draw.pso_id}",
    )

    # The second DispatchRays, so the rule is not tuned to one instance.
    second = capture.resolve_draw(global_id=SECOND_RAY["global_id"])
    check(
        "the frame's other DispatchRays resolves the same way",
        second is not None and second.index == SECOND_RAY["draw_index"],
        f"gid={SECOND_RAY['global_id']} -> draw_index={second and second.index}",
    )
    second_pass = capture.find_pass_by_event(global_id=SECOND_RAY["global_id"])
    check(
        "and lands on its own pass, not the hit-lighting one",
        second_pass is not None and second_pass["name"] == SECOND_RAY["pass_name"],
        f"pass={second_pass and second_pass['name']!r}",
    )

    print("\nthe traps that must stay shut")

    # Trap 1: range containment. Recomputed here rather than hard-coded so this
    # keeps testing the real shape of the capture.
    ranges = [
        (p["first_global_id"], p["last_global_id"], p["name"])
        for p in capture.passes
        if p["first_global_id"] is not None
    ]
    containing = [name for lo, hi, name in ranges if lo <= 5099 <= hi]
    check(
        "5099 is inside several pass gid ranges, so containment cannot decide",
        len(containing) > 1,
        f"containing={containing}",
    )
    # And if it is used anyway, it must not be what answers: the width tie means
    # the wrong pass wins on ordering alone.
    widths = {name: hi - lo for lo, hi, name in ranges if lo <= 5099 <= hi}
    narrowest = min(widths.values()) if widths else None
    tied = [n for n, w in widths.items() if w == narrowest]
    check(
        "narrowest-range tie-breaking is also undecidable here",
        len(tied) > 1,
        f"tied_at_width_{narrowest}={tied}",
    )

    # Trap 2: nearest preceding action as a general fallback.
    payload = run("draw-state", global_id=5100)
    check(
        "5100 (a WriteBufferImmediate) is refused, not silently snapped to a draw",
        payload["status"] == "error",
        f"status={payload['status']}",
    )
    if payload["status"] == "error":
        hint = (payload["error"].get("suggestion") or "").lower()
        check(
            "and the refusal explains what 5100 actually is",
            "writebufferimmediate" in hint or "not an action" in hint,
            f"suggestion={hint[:100]!r}",
        )

    # Trap 3: a marker holding no DrawCall produces no pass, so a gid inside it
    # cannot be placed. BuildRaytracingAccelerationStructure is the live example:
    # three CSV rows under RayTracingBuildScene, no DrawCall, no pass entry. This
    # must be reported as a known gap, not answered with the nearest pass.
    from pix_tool_set.engine.model import EventKind  # noqa: E402

    build_rows = [e for e in capture.events if e.kind is EventKind.RAYTRACING]
    check(
        "BuildRaytracingAccelerationStructure rows exist to test with",
        len(build_rows) > 0,
        f"rows={len(build_rows)}",
    )
    if build_rows:
        gid = build_rows[0].global_id
        payload = run("find-pass", global_id=gid)
        if payload["status"] == "error":
            hint = (payload["error"].get("suggestion") or "").lower()
            check(
                "an id in a draw-less marker is refused with its marker named",
                "raytracingbuildscene" in hint or "no pass" in hint,
                f"gid={gid} suggestion={hint[:110]!r}",
            )
        else:
            name = (payload.get("data") or {}).get("matches", [{}])[0].get("name")
            check(
                "or it is placed on its real marker, never on a neighbouring pass",
                name == "RayTracingBuildScene",
                f"gid={gid} name={name!r}",
            )

    print("\nthe same integer means two things: the cross-hint")
    row = capture.event_by_queue_id(CONFUSABLE_ID)
    check(
        f"row {CONFUSABLE_ID} of the event list is still a {CONFUSABLE_ROW_NAME}",
        row is not None and row.name == CONFUSABLE_ROW_NAME,
        f"row={row and row.name!r}",
    )
    payload = run("draw-state", queue_id=CONFUSABLE_ID)
    check(
        "passing it as queue_id still fails",
        payload["status"] == "error",
        f"status={payload['status']}",
    )
    if payload["status"] == "error":
        hint = payload["error"].get("suggestion") or ""
        check(
            "and the failure points at --global-id as the likely intent",
            "global_id" in hint.replace("-", "_").lower(),
            f"suggestion={hint[:140]!r}",
        )

    print("\nnothing that worked before may stop working")
    check(
        "draw_index still resolves every action",
        all(
            capture.resolve_draw(draw_index=d.index) is not None
            for d in capture.draw_calls
        ),
    )
    probe = next(d for d in capture.draw_calls if d.queue_id is not None)
    check(
        "queue_id still resolves on the exported queue",
        capture.resolve_draw(queue_id=probe.queue_id) is not None,
        f"queue_id={probe.queue_id}",
    )

    print("\n" + "=" * 78)
    print(f"{sum(PASSED)}/{len(PASSED)} checks passed")
    print("=" * 78)
    return 0 if all(PASSED) else 1


if __name__ == "__main__":
    raise SystemExit(main())
