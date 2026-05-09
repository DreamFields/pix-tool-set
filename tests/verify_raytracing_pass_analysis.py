"""Regression: no tool may report a raytracing pass as empty work.

Why this file exists
--------------------
Fixing ``pass-bindings`` raised the obvious follow-up: which *other* tools go blind on a
ray dispatch? A sweep for the three rasterisation-blindness patterns found two more real
defects, and they are worse than the original in one respect -- the original at least
returned ``partial``.

  * ``analyze-pass`` returned ``status=success`` with draw_count 0, triangles 0,
    shader_mix {}, and empty inputs/outputs for a pass that traces rays through 84
    shader exports. A confident "this pass does nothing" is the most expensive possible
    answer, because nothing downstream can tell it from the truth.
  * ``pass-shader-source`` failed with "This pass binds no such stage", which is simply
    false: the pass binds 84 shaders, they just are not PSO stages.

The root cause of the first one is worth stating because it is not DXR-specific:
``draw.srvs`` / ``draw.uavs`` only walk the resolved views of descriptor *tables*. A ray
dispatch binds nearly everything as a root descriptor, so the resource flow came back
empty -- and the same blind spot was under-reporting root-bound UAVs on ordinary compute
passes too.

Baseline: session ``Tiled``. gid 5367 is the raytracing pass; TileClassificationBuildLists
is the rasterisation control.
"""

from __future__ import annotations

import sys

from pix_tool_set import call_tool

CAPTURE = r"C:\Users\vinmeng\Desktop\ManyLights\debug\Tiled.wpix"
SESSION = "Tiled"

RAY_GID = 5367
RAY_DRAW = 2711
RAY_STATE_OBJECT = 3930
RASTER_PASS = "TileClassificationBuildLists"

_checks: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    _checks.append((bool(ok), label, detail))
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  ({detail})" if detail else ""))


def main() -> int:
    call_tool("session-open", {"capture": CAPTURE})

    print("\n1. analyze-pass no longer reports a ray dispatch as zero work")
    data = call_tool("analyze-pass", {"session": SESSION, "global_id": RAY_GID})["data"]

    check(
        data.get("pass_kind") == "raytracing",
        "the pass is labelled raytracing",
        str(data.get("pass_kind")),
    )
    check("raytracing" in data, "a raytracing workload block is present")
    ray = data.get("raytracing", {})
    check(
        ray.get("ray_dispatches", 0) > 0,
        "ray dispatches are counted",
        str(ray.get("ray_dispatches")),
    )
    check(
        ray.get("shader_exports", 0) == 84,
        "all 84 shader exports are counted",
        str(ray.get("shader_exports")),
    )
    check(
        ray.get("state_object_ids") == [RAY_STATE_OBJECT],
        f"state object {RAY_STATE_OBJECT} is named",
        str(ray.get("state_object_ids")),
    )
    check(
        ray.get("rays") == 2,
        "the 2x1x1 dispatch reports 2 rays",
        str(ray.get("rays")),
    )
    # The zero-shader_mix was the most misleading single field: it reads as "no shaders
    # ran". Filled from the export stage mix, since PSO stages do not exist here.
    mix = data.get("shader_mix") or {}
    check(bool(mix), "shader_mix is not empty", str(mix))
    check(
        mix.get("RAYGEN", 0) > 0 and mix.get("CLOSESTHIT", 0) > 0,
        "shader_mix carries DXR stages",
        f"RAYGEN={mix.get('RAYGEN')} CLOSESTHIT={mix.get('CLOSESTHIT')}",
    )
    # Root-bound resources: empty before the fix, because srvs/uavs only see tables.
    check(
        len(data.get("inputs", [])) > 0,
        "root-bound inputs are reported",
        f"{len(data.get('inputs', []))} resource(s)",
    )
    check(
        len(data.get("outputs", [])) > 0,
        "root-bound outputs are reported",
        f"{len(data.get('outputs', []))} resource(s)",
    )
    topics = [o["topic"] for o in data.get("observations", [])]
    check(
        "raytracing_pass" in topics,
        "an observation explains the inapplicable counters",
        str(topics),
    )
    note = ray.get("note") or ""
    check(
        "no PSO" in note or "no triangles" in note,
        "the note says why workload reads zero",
    )

    print("\n2. pass-shader-source points at the DXR route instead of denying it")
    payload = call_tool("pass-shader-source", {"session": SESSION, "global_id": RAY_GID})
    check(payload["status"] == "error", "still an error (this tool reads PSO stages)")
    suggestion = (payload.get("error") or {}).get("suggestion") or ""
    # The old text claimed the pass binds no such stage, which is factually wrong.
    check(
        "binds no such stage" not in suggestion,
        "the false 'binds no such stage' claim is gone",
    )
    check(
        str(RAY_STATE_OBJECT) in suggestion,
        "the state object id is named",
        f"contains {RAY_STATE_OBJECT}",
    )
    check(
        "describe-state-object" in suggestion,
        "describe-state-object is offered",
    )
    check("pass-bindings" in suggestion, "pass-bindings is offered")
    check(
        "shader-edit-begin" in suggestion,
        "the HLSL recovery route is offered",
    )
    check(
        "84" in suggestion,
        "the export count is quoted so the denial is self-refuting",
    )

    print("\n3. draw-state already handled it, and still does")
    draw_state = call_tool("draw-state", {"session": SESSION, "global_id": RAY_GID})
    check(draw_state["status"] == "success", "draw-state succeeds", draw_state["status"])

    pipeline = call_tool("pipeline-state", {"session": SESSION, "global_id": RAY_GID})["data"]
    check(
        pipeline.get("resolved_kind") == "raytracing",
        "pipeline-state resolves to the raytracing shape",
        str(pipeline.get("resolved_kind")),
    )
    check(
        pipeline.get("shader_count") == 84,
        "pipeline-state counts the exports",
        str(pipeline.get("shader_count")),
    )

    print("\n4. the rasterisation path is untouched")
    raster = call_tool("analyze-pass", {"session": SESSION, "pass_name": RASTER_PASS})["data"]
    check(
        raster.get("pass_kind") == "rasterisation",
        "a compute pass is still labelled rasterisation",
        str(raster.get("pass_kind")),
    )
    check(
        "raytracing" not in raster,
        "and carries no raytracing block",
    )
    check(
        (raster.get("shader_mix") or {}).get("CS") == 1,
        "its CS shader_mix is unchanged",
        str(raster.get("shader_mix")),
    )
    check(
        raster["workload"]["compute_threads"] > 0,
        "its thread count is unchanged",
        str(raster["workload"]["compute_threads"]),
    )
    # Root-bound UAVs were being dropped for ordinary compute passes too; this pass
    # declares u0..u3, so all four must appear.
    check(
        len(raster.get("outputs", [])) >= 4,
        "its four declared UAV outputs are all reported",
        f"{len(raster.get('outputs', []))} outputs",
    )

    passed = sum(1 for ok, _, _ in _checks if ok)
    total = len(_checks)
    print("\n" + "=" * 68)
    print(f"checks: {passed}/{total} passed")
    if passed == total:
        print("PASS: no tool reports a raytracing pass as empty work")
        return 0
    print("FAIL: a tool is still blind to raytracing work")
    for ok, label, detail in _checks:
        if not ok:
            print(f"  - {label} ({detail})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
