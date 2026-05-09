"""Verify that every tool naming an event accepts Global ID, not just Queue ID.

This file exists because of the fourth blind spot found in this toolkit, and the one
that was hardest to see: a tool can answer correctly, cover raytracing, model every
binding -- and still be unreachable, because the id the caller has is not an id the
parameter table accepts.

The inversion that made it a real bug rather than an inconvenience: ``pixtool
save-resource`` takes a Global ID on its command line, and ``_texture_export`` used to
document that Queue ID was "the only event identifier accepted as input", translating it
into the Global ID it actually needed. So the reliable id -- shown in the PIX GUI, unique
across every queue -- had to be hand-converted into the unreliable one, whose column is
row order in this export. A Queue ID from a multi-queue capture therefore did not fail;
it resolved to an unrelated event and returned confident data about it. Fourteen tools
rejected ``--global-id`` outright while their own internals either wanted it or already
supported it.

Four properties are asserted, in order of how much damage getting them wrong causes:

1. Every one of the fourteen tools accepts ``global_id`` at the CLI parameter level. A
   rejected argument is at least loud; this is the floor.
2. It is honoured, not merely swallowed. A parameter accepted and ignored is strictly
   worse than one rejected, because the caller believes the event was selected.
3. Both ids select the same event bit for bit. This is the anti-drift guard: it is what
   would catch a future refactor that reads global_id but resolves it differently.
4. A Global ID absent from the capture fails instead of being forwarded. pixtool would
   accept any integer and save whatever state it finds, so without validation an unused
   id returns a plausible-looking image for an event that never ran.

Plus one regression specific to ``debug-pixel-shader``: an explicit selector that
resolves to nothing must fail, not fall through to the pixel-coverage search. Falling
through answered with whichever draw happened to cover the pixel, in a payload that
looked like a successful lookup of the requested id.

Usage:
    python tests/verify_selector_completeness.py [session-name]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set import call_tool  # noqa: E402
from pix_tool_set.context import ToolContext, clear_capture_cache  # noqa: E402
from pix_tool_set.registry import get_registry  # noqa: E402
from pix_tool_set.session import SessionStore  # noqa: E402

# Importing the package does not populate the registry -- the @tool decorators run when
# the individual tool modules are imported, which ``load_builtin_tools`` does and which
# otherwise only happens on the first dispatch. Inspecting parameter tables before any
# dispatch would read an empty registry and report every tool as unregistered.
from pix_tool_set.tools import load_builtin_tools  # noqa: E402

SESSION = sys.argv[1] if len(sys.argv) > 1 else "Tiled"

# The fourteen tools that named an event but had no global_id parameter. Kept as a
# literal list so that a tool losing the parameter again is a failure here, rather than
# something a "scan whatever is registered" test would silently stop covering.
TOOLS_FIXED = (
    "find-draw-calls",
    "list-draw-calls",
    "pass-cost",
    "event-timing",
    "debug-pixel-shader",
    "pick-pixel",
    "sample-pixel-region",
    "texture-pixel-stats",
    "read-texture-pixels",
    "read-resource-texture",
    "read-replay-target",
    "export-texture",
    "export-uav-slice",
    "find-depth-content",
)

# A raytracing pass reached through its ExecuteIndirect expansion: global_id 5367 names
# the sub-action PIX expanded, and resolves to draw 2711 in pass 347. Used for the pass
# selectors, because it is also the case that exposed the raytracing blind spot.
RT_GLOBAL_ID = 5367
RT_DRAW_INDEX = 2711
RT_PASS_INDEX = 347
RT_PASS_NAME = "ReflectionHardwareRayTracingRGS hit-lighting"

# A rasterisation draw that binds GBufferA (object 756) as a render target, so pixtool
# can actually save it. The same event under all three ids, which is what makes the
# equivalence assertions meaningful.
PIXEL_GLOBAL_ID = 3854
PIXEL_QUEUE_ID = 18593
PIXEL_DRAW_INDEX = 2473
PIXEL_X, PIXEL_Y = 810, 284
GBUFFER_A_SIZE = {"width": 1532, "height": 764}

# Far beyond the capture's id range, so it can only be answered by forwarding an
# unvalidated id.
BOGUS_GLOBAL_ID = 999999

PASSED: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    PASSED.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


def run(tool: str, **args):
    args.setdefault("session", SESSION)
    return call_tool(tool, args)


def data_of(payload: dict) -> dict:
    return payload.get("data") or {}


def main() -> int:
    if SessionStore().get(SESSION) is None:
        print(f"No session named {SESSION!r}.")
        return 2
    clear_capture_cache()
    load_builtin_tools()
    capture = ToolContext.from_cwd().capture({"session": SESSION})
    registry = get_registry()

    print("=" * 78)
    print(f"selector completeness on {SESSION}")
    print("=" * 78)

    # ----------------------------------------------------------------------
    print("\n1. every fixed tool declares global_id")
    # The parameter table is what argparse builds the CLI from, so a missing entry here
    # is exactly the "unrecognized arguments: --global-id" the caller saw.
    for name in TOOLS_FIXED:
        try:
            props = (registry.get(name).parameters or {}).get("properties", {})
        except Exception as exc:  # a missing tool is a failure, not a crash
            check(f"{name} accepts global_id", False, f"not registered: {exc}")
            continue
        check(
            f"{name} accepts global_id",
            "global_id" in props,
            "" if "global_id" in props else f"declares {sorted(props)[:8]}",
        )

    # ----------------------------------------------------------------------
    print("\n2. no tool naming an event is left behind")
    # A forward-looking guard rather than a restatement of section 1: any tool that
    # takes queue_id is naming an event, so it needs the cross-queue selector too.
    # Without this, a tool added later repeats the whole mistake unnoticed.
    missing: list[str] = []
    for spec in registry.list_tools():
        props = (spec.parameters or {}).get("properties", {})
        if "queue_id" in props and "global_id" not in props:
            missing.append(spec.name)
    check(
        "no tool takes queue_id without global_id",
        not missing,
        f"offenders={missing}" if missing else "",
    )

    # ----------------------------------------------------------------------
    print("\n3. pass selectors honour the id, not just accept it")
    payload = run("list-draw-calls", global_id=RT_GLOBAL_ID)
    rows = data_of(payload).get("draw_calls", [])
    check(
        "list-draw-calls --global-id restricts to that pass",
        payload["status"] == "success"
        and len(rows) == 1
        and rows[0].get("draw_index") == RT_DRAW_INDEX,
        f"status={payload['status']} rows={len(rows)} "
        f"draw_index={rows[0].get('draw_index') if rows else None}",
    )

    payload = run("find-draw-calls", global_id=RT_GLOBAL_ID)
    rows = data_of(payload).get("draw_calls", [])
    check(
        "find-draw-calls --global-id restricts to that pass",
        payload["status"] == "success"
        and len(rows) == 1
        and rows[0].get("draw_index") == RT_DRAW_INDEX,
        f"status={payload['status']} rows={len(rows)}",
    )

    payload = run("pass-cost", global_id=RT_GLOBAL_ID, no_measure=True)
    passes = data_of(payload).get("passes", [])
    check(
        "pass-cost --global-id reports exactly that pass",
        payload["status"] == "success"
        and len(passes) == 1
        and passes[0].get("pass_index") == RT_PASS_INDEX
        and passes[0].get("name") == RT_PASS_NAME,
        f"status={payload['status']} passes={[(p.get('pass_index'), p.get('name')) for p in passes]}",
    )

    # A whole-frame call must stay a whole-frame call: if the id leaked in as a default
    # the tools above would look correct while every unfiltered query silently narrowed.
    payload = run("pass-cost", no_measure=True, limit=5)
    check(
        "pass-cost without a selector still spans the frame",
        payload["status"] == "success" and data_of(payload).get("total", 0) > 1,
        f"total={data_of(payload).get('total')}",
    )

    # ----------------------------------------------------------------------
    print("\n4. the two ids name the same event")
    by_gid = run("pick-pixel", global_id=PIXEL_GLOBAL_ID, x=PIXEL_X, y=PIXEL_Y)
    by_qid = run("pick-pixel", queue_id=PIXEL_QUEUE_ID, x=PIXEL_X, y=PIXEL_Y)
    check(
        "pick-pixel --global-id succeeds",
        by_gid["status"] == "success",
        f"status={by_gid['status']}",
    )
    check(
        "pick-pixel reads GBufferA's dimensions",
        data_of(by_gid).get("image_size") == GBUFFER_A_SIZE,
        f"size={data_of(by_gid).get('image_size')}",
    )
    # The core anti-drift assertion: same event, two ids, identical pixels.
    check(
        "global_id and queue_id give bit-identical channels",
        by_gid["status"] == by_qid["status"] == "success"
        and data_of(by_gid).get("channels") == data_of(by_qid).get("channels"),
        f"gid={data_of(by_gid).get('channels')} qid={data_of(by_qid).get('channels')}",
    )
    # Say which selector drove the export, so a caller comparing two runs can tell
    # whether the event was chosen or merely fallen back onto.
    provenance = [
        entry.get("event_selected_by")
        for entry in (by_gid.get("diagnostics") or [])
        if entry.get("event_selected_by")
    ]
    check(
        "diagnostics report the selector used",
        provenance == ["global_id"],
        f"event_selected_by={provenance}",
    )
    gids = [
        entry.get("global_id")
        for entry in (by_gid.get("diagnostics") or [])
        if entry.get("global_id") is not None
    ]
    check(
        "the id reaches pixtool unchanged",
        gids == [PIXEL_GLOBAL_ID],
        f"global_id={gids}",
    )

    # ----------------------------------------------------------------------
    print("\n5. draw resolution agrees across selectors")
    resolved_gid = capture.resolve_draw(global_id=PIXEL_GLOBAL_ID)
    resolved_qid = capture.resolve_draw(queue_id=PIXEL_QUEUE_ID)
    check(
        "engine resolves both ids to one draw",
        resolved_gid is not None
        and resolved_qid is not None
        and resolved_gid.index == resolved_qid.index == PIXEL_DRAW_INDEX,
        f"gid->{resolved_gid.index if resolved_gid else None} "
        f"qid->{resolved_qid.index if resolved_qid else None}",
    )

    payload = run("read-resource-texture", global_id=PIXEL_GLOBAL_ID, target="rt0")
    check(
        "read-resource-texture --global-id resolves a target",
        payload["status"] in ("success", "partial")
        and data_of(payload).get("resource_id") is not None,
        f"status={payload['status']} resource_id={data_of(payload).get('resource_id')}",
    )

    # ----------------------------------------------------------------------
    print("\n6. an id absent from the capture fails")
    # pixtool validates nothing, so this has to be caught before the command line is
    # built or the caller gets an image of an event that never ran.
    for tool, extra in (
        ("pick-pixel", {"x": 1, "y": 1}),
        ("texture-pixel-stats", {}),
        ("export-texture", {}),
    ):
        payload = run(tool, global_id=BOGUS_GLOBAL_ID, **extra)
        code = (payload.get("error") or {}).get("code")
        check(
            f"{tool} rejects an unused global_id",
            payload["status"] == "error" and code in ("event_not_found", "not_found"),
            f"status={payload['status']} code={code}",
        )

    payload = run("event-timing", global_id=BOGUS_GLOBAL_ID, no_measure=True)
    check(
        "event-timing rejects an unused global_id",
        payload["status"] == "error",
        f"status={payload['status']} code={(payload.get('error') or {}).get('code')}",
    )

    # ----------------------------------------------------------------------
    print("\n7. debug-pixel-shader does not fall back past a failed selector")
    # The dangerous shape: coordinates are supplied too, so the coverage search *can*
    # answer. It must not, because the answer would describe a different event than the
    # id names while looking like a successful lookup.
    payload = run(
        "debug-pixel-shader", global_id=BOGUS_GLOBAL_ID, x=PIXEL_X, y=PIXEL_Y
    )
    check(
        "a bogus global_id is an error, not a coverage-search answer",
        payload["status"] == "error",
        f"status={payload['status']} code={(payload.get('error') or {}).get('code')}",
    )
    check(
        "the failure names the selector the caller used",
        str(BOGUS_GLOBAL_ID) in str(payload.get("error") or {}),
        f"error={str(payload.get('error'))[:100]}",
    )
    # And the same call with coordinates only still works, so the guard did not simply
    # disable the coverage search.
    payload = run("debug-pixel-shader", x=PIXEL_X, y=PIXEL_Y)
    check(
        "coordinate-only lookup still uses the coverage search",
        payload["status"] in ("success", "partial"),
        f"status={payload['status']}",
    )

    print("\n" + "=" * 78)
    print(f"{sum(PASSED)}/{len(PASSED)} checks passed")
    print("=" * 78)
    return 0 if all(PASSED) else 1


if __name__ == "__main__":
    raise SystemExit(main())
