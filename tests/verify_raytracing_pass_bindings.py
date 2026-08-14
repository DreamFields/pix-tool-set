"""Regression: pass-bindings must answer a raytracing pass, not disclaim it.

Why this file exists
--------------------
``pass-bindings`` used to run the rasterisation path on every action. For a ray
dispatch that meant ``draw.shaders`` came back empty, the pass reported zero stages
and zero descriptor tables, and the payload explained the hole with the sentence
"State objects are not yet modelled". By then they *were* modelled: for the very same
``Tiled.wpix`` dispatch, ``shader-bindings`` already resolved the state object's
exports, its hit groups and the RayGen record panel.

So the toolkit held the answer and told the caller it did not exist. That is the worst
failure shape available -- an error invites a retry, while a confident "nothing here"
ends the investigation. The checks below pin the three things that made it possible:

  1. the stale capability claim is gone from every payload,
  2. a ray dispatch is answered with the raytracing binding shape,
  3. the empty rasterisation keys are explicitly labelled as structural, not missing.

Baseline: session ``Tiled`` (``C:\\Users\\vinmeng\\Desktop\\ManyLights\\debug\\Tiled.wpix``).
Global IDs 5312 and 5367 are the two ids from the report that triggered this fix.
"""

from __future__ import annotations

import sys

from pix_tool_set import call_tool

CAPTURE = r"C:\Users\vinmeng\Desktop\ManyLights\debug\Tiled.wpix"
SESSION = "Tiled"

# The exact panel PIX shows for RayGen_fb3c7b0c9e02fb73, in PIX's own (register, space)
# order. Hard-coded rather than recomputed so a change in the sort would be caught.
EXPECTED_RAYGEN_CBVS = [
    ("_RootShaderParameters", 0, 1),
    ("SceneTexturesStruct", 1, 1),
    ("View", 1, 4),
    ("LumenCardScene", 2, 1),
    ("ReflectionStruct", 3, 1),
    ("ReflectionCaptureSM5", 4, 1),
    ("FogStruct", 5, 1),
    ("ForwardLightStruct", 6, 1),
    ("RaytracingLightGridData", 7, 1),
]

EXPECTED_RAYGEN_SAMPLERS = [
    ("D3DStaticPointClampedSampler", 1, 1000),
    ("D3DStaticBilinearClampedSampler", 3, 1000),
]

FORBIDDEN_PHRASES = (
    "not yet modelled",
    "are not modelled",
    "not modelled",
)

_checks: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    _checks.append((bool(ok), label, detail))
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  ({detail})" if detail else ""))


def _walk_strings(node):
    """Every string anywhere in the payload, so a stale claim cannot hide in a nested
    note field that a targeted assertion would miss."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _walk_strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_strings(value)


def main() -> int:
    call_tool("session-open", {"capture": CAPTURE})

    print("\n1. a ray dispatch is answered, not disclaimed")
    for global_id, expected_pass, expected_so in (
        (5312, "ReflectionHardwareRayTracingRGS default", 3891),
        (5367, "ReflectionHardwareRayTracingRGS hit-lighting", 3930),
    ):
        payload = call_tool("pass-bindings", {"session": SESSION, "global_id": global_id})
        data = payload["data"]
        entry = data["passes"][0]
        draw = entry["draws"][0]

        check(
            entry["name"] == expected_pass,
            f"global_id={global_id} resolves to the right pass",
            entry["name"],
        )
        check(
            draw["binding_shape"] == "raytracing",
            f"global_id={global_id} uses the raytracing binding shape",
            draw["binding_shape"],
        )
        check(
            draw["state_object_id"] == expected_so,
            f"global_id={global_id} names state object {expected_so}",
            str(draw["state_object_id"]),
        )
        # The regression itself: this list used to be empty for both ids.
        check(
            draw["stages"] == ["ANYHIT", "CLOSESTHIT", "MISS", "RAYGEN"],
            f"global_id={global_id} reports its four derived DXR stages",
            str(draw["stages"]),
        )
        check(
            len(draw.get("exports", [])) > 0,
            f"global_id={global_id} lists shader exports",
            f"{len(draw.get('exports', []))} exports",
        )
        check(
            len(draw.get("hit_groups", [])) > 0,
            f"global_id={global_id} lists hit groups",
            f"{len(draw.get('hit_groups', []))} hit groups",
        )
        check(
            len(draw.get("global_root_bindings", [])) > 0,
            f"global_id={global_id} lists global root bindings",
            f"{len(draw.get('global_root_bindings', []))} bindings",
        )
        # pso_id being null is correct and must stay null: a caller that "fixes" it by
        # falling back to the last PSO would report a rasterisation shader for a ray
        # dispatch, which is a wrong answer rather than a missing one.
        check(
            draw["pso_id"] is None,
            f"global_id={global_id} keeps pso_id null by design",
            repr(draw["pso_id"]),
        )

        print("\n   the stale capability claim is gone")
        for phrase in FORBIDDEN_PHRASES:
            hits = [text for text in _walk_strings(data) if phrase in text.lower()]
            check(
                not hits,
                f"global_id={global_id} payload never says {phrase!r}",
                f"{len(hits)} occurrence(s)",
            )

        print("\n   empty rasterisation keys are labelled structural, not missing")
        check(
            draw["descriptor_tables"] == [],
            f"global_id={global_id} descriptor_tables is empty",
        )
        check(
            "rasterisation_fields_note" in draw,
            f"global_id={global_id} explains why it is empty",
        )
        check(
            "global_root_bindings" in (draw.get("rasterisation_fields_note") or ""),
            f"global_id={global_id} note names where the real answer is",
        )
        check(
            data.get("raytracing_draws_reported", 0) > 0,
            f"global_id={global_id} envelope counts the ray dispatches",
            str(data.get("raytracing_draws_reported")),
        )
        # An info diagnostic, not a degradation: the answer is complete.
        check(
            payload["status"] == "success",
            f"global_id={global_id} is success, not partial",
            payload["status"],
        )

    print("\n2. the RayGen record panel matches PIX exactly")
    data = call_tool("pass-bindings", {"session": SESSION, "global_id": 5367})["data"]
    draw = data["passes"][0]["draws"][0]
    raygen = next(
        (
            export
            for export in draw["exports"]
            if export.get("name") == "RayGen_fb3c7b0c9e02fb73"
        ),
        None,
    )
    check(raygen is not None, "RayGen_fb3c7b0c9e02fb73 is present")
    if raygen is not None:
        bindings = raygen.get("bindings") or {}
        actual_cbvs = [
            (row["name"], row["register"], row["space"])
            for row in bindings.get("cbuffers", [])
        ]
        actual_samplers = [
            (row["name"], row["slot"], row["space"])
            for row in bindings.get("static_samplers", [])
        ]
        check(
            actual_cbvs == EXPECTED_RAYGEN_CBVS,
            "all nine CBVs match the PIX panel, in PIX's order",
            f"{len(actual_cbvs)} rows",
        )
        # The ordering trap: View is cb1,space4 and must follow SceneTexturesStruct's
        # cb1,space1, which declaration order gets wrong.
        names = [name for name, _, _ in actual_cbvs]
        if "View" in names and "SceneTexturesStruct" in names:
            check(
                names.index("View") > names.index("SceneTexturesStruct"),
                "View [1,space=4] sorts after SceneTexturesStruct [1,space=1]",
            )
        check(
            actual_samplers == EXPECTED_RAYGEN_SAMPLERS,
            "both static samplers match the PIX panel",
            f"{len(actual_samplers)} rows",
        )

    print("\n3. --stage accepts DXR stages and filters exports")
    data = call_tool(
        "pass-bindings", {"session": SESSION, "global_id": 5367, "stage": "RAYGEN"}
    )["data"]
    draw = data["passes"][0]["draws"][0]
    stages_seen = {(export.get("stage") or "").upper() for export in draw["exports"]}
    check(
        draw.get("exports_filtered_to_stage") == "RAYGEN",
        "the filter is echoed back",
        str(draw.get("exports_filtered_to_stage")),
    )
    check(
        stages_seen == {"RAYGEN"},
        "only RAYGEN exports survive the filter",
        str(sorted(stages_seen)),
    )
    check(
        len(draw["exports"]) > 0,
        "and the filtered list is not empty",
        f"{len(draw['exports'])} exports",
    )

    print("\n4. pass-bindings and shader-bindings agree")
    # They share one builder now; if they ever diverge again, the shared view was
    # bypassed and this catches it before a caller sees two different answers.
    pass_draw = call_tool("pass-bindings", {"session": SESSION, "global_id": 5367})[
        "data"
    ]["passes"][0]["draws"][0]
    shader_data = call_tool("shader-bindings", {"session": SESSION, "global_id": 5367})[
        "data"
    ]
    check(
        pass_draw["stages"] == shader_data["stages"],
        "same stage list from both tools",
        f"{pass_draw['stages']} vs {shader_data['stages']}",
    )
    check(
        len(pass_draw["exports"]) == len(shader_data["exports"]),
        "same export count from both tools",
        f"{len(pass_draw['exports'])} vs {len(shader_data['exports'])}",
    )
    check(
        len(pass_draw["global_root_bindings"]) == len(shader_data["global_root_bindings"]),
        "same global root binding count from both tools",
        f"{len(pass_draw['global_root_bindings'])} vs "
        f"{len(shader_data['global_root_bindings'])}",
    )
    check(
        pass_draw["state_object_id"] == shader_data["state_object_id"],
        "same state object from both tools",
    )

    print("\n5. the rasterisation path is untouched")
    raster = call_tool(
        "pass-bindings",
        {
            "session": SESSION,
            "pass_name": "TileClassificationBuildLists",
            "stage": "CS",
        },
    )
    raster_draw = raster["data"]["passes"][0]["draws"][0]
    check(
        raster_draw["binding_shape"] == "rasterisation",
        "a compute pass still uses the rasterisation shape",
        raster_draw["binding_shape"],
    )
    check(
        len(raster_draw["stages"]) > 0,
        "and still reports its PSO stages",
        f"{len(raster_draw['stages'])} stage(s)",
    )
    check(
        "declared_totals" in raster_draw,
        "and still reports declared_totals",
    )
    check(
        len(raster_draw["descriptor_tables"]) > 0,
        "and still reports descriptor tables",
        f"{len(raster_draw['descriptor_tables'])} table(s)",
    )

    passed = sum(1 for ok, _, _ in _checks if ok)
    total = len(_checks)
    print("\n" + "=" * 68)
    print(f"checks: {passed}/{total} passed")
    if passed == total:
        print("PASS: pass-bindings answers raytracing passes with real data")
        return 0
    print("FAIL: a raytracing pass is being disclaimed instead of answered")
    for ok, label, detail in _checks:
        if not ok:
            print(f"  - {label} ({detail})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
