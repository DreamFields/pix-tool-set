from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_expected(relative_path: str) -> dict:
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


def _display_names(payload: dict) -> set[str]:
    return {str(resource.get("display_name")) for resource in payload.get("resources", [])}


def _view_types(payload: dict) -> set[str]:
    return {str(resource.get("view_type")) for resource in payload.get("resources", [])}


def test_scenario_03_graphics_event_resource_expectation() -> None:
    payload = _load_expected("data/train/scenario_03_graphics_pipeline_with_db_and_pdb/expected_output/s3_tc01_event_resources.json")

    assert payload["status"] == "success"
    assert payload["global_id"] == "3854"
    assert payload["resource_count"] == 26
    assert {"VB", "IB", "CBV", "SRV", "Sampler", "RTV", "Depth", "Stencil"}.issubset(_view_types(payload))
    assert {
        "VB 0",
        "VB 4",
        "VB 5",
        "IB",
        "CBV 0 : View",
        "CBV 1 : Scene",
        "CBV 2 : LocalVF",
        "Sampler 0 : OpaqueBasePass_DBufferATextureSampler",
        "RTV 0 : SceneColor",
        "Depth : SceneDepthZ",
        "Stencil : SceneDepthZ",
    }.issubset(_display_names(payload))


def test_scenario_05_compute_event_resource_expectation() -> None:
    payload = _load_expected("data/train/scenario_05_compute_pipeline_with_db_and_pdb/expected_output/s5_tc01_event_resources.json")

    assert payload["status"] == "success"
    assert payload["global_id"] == "3968"
    assert payload["resource_count"] == 15
    assert {"CBV", "SRV", "UAV"}.issubset(_view_types(payload))
    assert {"VB", "IB", "RTV", "Depth", "Stencil"}.isdisjoint(_view_types(payload))
    assert {
        "CBV 0 : _RootShaderParameters",
        "CBV 1 : View",
        "CBV 2 : VirtualShadowMap",
        "CBV 3 : ForwardLightStruct",
        "SRV Buffer 0 : VirtualShadowMap_LightGridData",
        "SRV Texture 6 : SceneTexturesStruct_SceneDepthTexture",
        "UAV Texture 0 : OutPageRequestFlags",
        "UAV Texture 1 : OutPageReceiverMasks",
    }.issubset(_display_names(payload))
