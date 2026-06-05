import json

scenario_01 = {
    "scenario_name": "Simple Compute Dispatch",
    "description": "A minimal compute shader dispatch scenario with one UAV resource and one constant buffer. Used to test basic functionality of all three tools.",
    "capture_db": "capture_db/capture.db",
    "test_cases": [
        {
            "id": "s1_tc01",
            "tool": "db-get-event-shader-source",
            "description": "Retrieve shader source for a simple compute dispatch event (GlobalID=2).",
            "input": {"global_id": 2},
            "expected_output_file": "expected_output/s1_tc01_shader_source.json",
            "assertions": [
                "status == success",
                "stage_count >= 1",
                "stages[0].stage == 'CS'",
                "stages[0].resolver_result.result.sources[0].content contains 'RWStructuredBuffer'"
            ]
        },
        {
            "id": "s1_tc02",
            "tool": "db-get-event-resource",
            "description": "Retrieve all bound resources for a compute dispatch event (GlobalID=2).",
            "input": {"global_id": 2},
            "expected_output_file": "expected_output/s1_tc02_event_resources.json",
            "assertions": [
                "status == success",
                "resource_count >= 2",
                "resources contain view_type == 'UAV'",
                "resources contain view_type == 'CBV'",
                "one resource has resource_name == 'SharedBuffer'",
                "one resource has resource_name == 'Constants'"
            ]
        },
        {
            "id": "s1_tc03",
            "tool": "db-get-resource-access-history",
            "description": "Retrieve access history for 'SharedBuffer' from compute dispatch event (GlobalID=2).",
            "input": {"global_id": 2, "resource": "SharedBuffer"},
            "expected_output_file": "expected_output/s1_tc03_access_history.json",
            "assertions": [
                "status == success",
                "access_count >= 2",
                "access_history contain source == 'resource_references'",
                "access_history contain source == 'event_bound_resources'",
                "one access has binding == 'CS UAV 0'"
            ]
        },
        {
            "id": "s1_tc04",
            "tool": "db-get-resource-access-history",
            "description": "Retrieve access history using resource_id selector instead of name (GlobalID=2, resource=100).",
            "input": {"global_id": 2, "resource": "100"},
            "expected_output_file": "expected_output/s1_tc04_access_history_by_id.json",
            "assertions": [
                "status == success",
                "access_count >= 2",
                "resource.resource_id == '100'"
            ]
        },
        {
            "id": "s1_tc05",
            "tool": "db-get-event-shader-source",
            "description": "Request shader source for a non-shader event (GlobalID=1) - should return partial status.",
            "input": {"global_id": 1},
            "expected_output_file": "expected_output/s1_tc05_non_shader_event.json",
            "assertions": [
                "status == partial or success",
                "stage_count == 0 or stages is empty"
            ]
        }
    ]
}

scenario_02 = {
    "scenario_name": "Graphics Pipeline with VS/PS",
    "description": "A typical graphics rendering scenario with vertex shader and pixel shader, including vertex buffers, index buffer, render targets, depth stencil, and multiple descriptor tables (SRV, UAV, CBV, Sampler).",
    "capture_db": "capture_db/capture.db",
    "test_cases": [
        {
            "id": "s2_tc01",
            "tool": "db-get-event-shader-source",
            "description": "Retrieve shader source for a graphics draw event (GlobalID=10). Expects VS and PS stages.",
            "input": {"global_id": 10},
            "expected_output_file": "expected_output/s2_tc01_shader_source.json",
            "assertions": [
                "status == success",
                "stage_count == 2",
                "stages contain stage == 'VS'",
                "stages contain stage == 'PS'",
                "VS source contains 'POSITION' or 'SV_Position'",
                "PS source contains 'SV_Target' or 'float4'"
            ]
        },
        {
            "id": "s2_tc02",
            "tool": "db-get-event-resource",
            "description": "Retrieve all bound resources for a graphics draw event (GlobalID=10). Expects VB, IB, SRV, CBV, RTV, Depth.",
            "input": {"global_id": 10},
            "expected_output_file": "expected_output/s2_tc02_event_resources.json",
            "assertions": [
                "status == success",
                "resource_count >= 6",
                "resources contain view_type == 'VB'",
                "resources contain view_type == 'IB'",
                "resources contain view_type == 'SRV'",
                "resources contain view_type == 'CBV'",
                "resources contain view_type == 'RTV'",
                "resources contain view_type == 'Depth'"
            ]
        },
        {
            "id": "s2_tc03",
            "tool": "db-get-resource-access-history",
            "description": "Retrieve access history for vertex buffer 'VertexBuffer0' from draw event (GlobalID=10).",
            "input": {"global_id": 10, "resource": "VertexBuffer0"},
            "expected_output_file": "expected_output/s2_tc03_access_history_vb.json",
            "assertions": [
                "status == success",
                "access_count >= 1",
                "one access has binding == 'IA VB'",
                "resource.resource_name == 'VertexBuffer0'"
            ]
        },
        {
            "id": "s2_tc04",
            "tool": "db-get-resource-access-history",
            "description": "Retrieve access history for render target 'SceneColor' from draw event (GlobalID=10).",
            "input": {"global_id": 10, "resource": "SceneColor"},
            "expected_output_file": "expected_output/s2_tc04_access_history_rtv.json",
            "assertions": [
                "status == success",
                "access_count >= 1",
                "one access has binding == 'OM RTV'"
            ]
        },
        {
            "id": "s2_tc05",
            "tool": "db-get-resource-access-history",
            "description": "Retrieve access history for depth stencil resource 'SceneDepth' from draw event (GlobalID=10).",
            "input": {"global_id": 10, "resource": "SceneDepth"},
            "expected_output_file": "expected_output/s2_tc05_access_history_depth.json",
            "assertions": [
                "status == success",
                "access_count >= 1",
                "one access has binding == 'OM Depth' or 'OM Stencil'"
            ]
        },
        {
            "id": "s2_tc06",
            "tool": "db-get-event-resource",
            "description": "Retrieve bound resources with pdb_search_paths provided to trigger shader-declared resource resolution (GlobalID=10).",
            "input": {"global_id": 10, "pdb_search_paths": ["shaders/"]},
            "expected_output_file": "expected_output/s2_tc06_event_resources_with_pdb.json",
            "assertions": [
                "status == success",
                "resource_count >= 6",
                "diagnostics.refreshed_source_cache == true or refreshed_from_database == true"
            ]
        }
    ]
}

scenario_03 = {
    "scenario_name": "Multi-Pass Rendering",
    "description": "A multi-pass rendering scenario with multiple Dispatch and Draw calls sharing resources across events. Tests resource aliasing, cross-event access history, and multi-stage shaders.",
    "capture_db": "capture_db/capture.db",
    "test_cases": [
        {
            "id": "s3_tc01",
            "tool": "db-get-event-shader-source",
            "description": "Retrieve shader source for a compute prepass event (GlobalID=20).",
            "input": {"global_id": 20},
            "expected_output_file": "expected_output/s3_tc01_prepass_shader.json",
            "assertions": [
                "status == success",
                "stage_count >= 1",
                "stages[0].stage == 'CS'"
            ]
        },
        {
            "id": "s3_tc02",
            "tool": "db-get-event-shader-source",
            "description": "Retrieve shader source for a graphics main pass event (GlobalID=30). Expects VS/PS.",
            "input": {"global_id": 30},
            "expected_output_file": "expected_output/s3_tc02_mainpass_shader.json",
            "assertions": [
                "status == success",
                "stage_count == 2",
                "stages contain stage == 'VS'",
                "stages contain stage == 'PS'"
            ]
        },
        {
            "id": "s3_tc03",
            "tool": "db-get-event-resource",
            "description": "Retrieve bound resources for compute prepass (GlobalID=20). Expects UAV write to GBuffer0.",
            "input": {"global_id": 20},
            "expected_output_file": "expected_output/s3_tc03_prepass_resources.json",
            "assertions": [
                "status == success",
                "resource_count >= 1",
                "resources contain resource_name == 'GBuffer0'",
                "resources contain view_type == 'UAV'"
            ]
        },
        {
            "id": "s3_tc04",
            "tool": "db-get-event-resource",
            "description": "Retrieve bound resources for graphics main pass (GlobalID=30). Expects SRV read from GBuffer0.",
            "input": {"global_id": 30},
            "expected_output_file": "expected_output/s3_tc04_mainpass_resources.json",
            "assertions": [
                "status == success",
                "resource_count >= 4",
                "resources contain resource_name == 'GBuffer0'",
                "resources contain view_type == 'SRV'",
                "resources contain view_type == 'RTV'"
            ]
        },
        {
            "id": "s3_tc05",
            "tool": "db-get-resource-access-history",
            "description": "Retrieve full access history for GBuffer0 starting from prepass (GlobalID=20). Should show UAV write in prepass and SRV read in main pass.",
            "input": {"global_id": 20, "resource": "GBuffer0"},
            "expected_output_file": "expected_output/s3_tc05_gbuffer0_history.json",
            "assertions": [
                "status == success",
                "access_count >= 2",
                "one access has binding == 'CS UAV 0' and global_id == '20'",
                "one access has binding == 'PS SRV 0' and global_id == '30'"
            ]
        },
        {
            "id": "s3_tc06",
            "tool": "db-get-resource-access-history",
            "description": "Retrieve access history for aliased resource 'SharedBuffer' which has multiple resource_ids (100, 101).",
            "input": {"global_id": 20, "resource": "SharedBuffer"},
            "expected_output_file": "expected_output/s3_tc06_aliased_resource_history.json",
            "assertions": [
                "status == success",
                "access_count >= 3",
                "access_history contains resource_id '100'",
                "access_history contains resource_id '101'"
            ]
        },
        {
            "id": "s3_tc07",
            "tool": "db-get-event-shader-source",
            "description": "Retrieve shader source using resolver_path parameter override (GlobalID=20).",
            "input": {"global_id": 20, "pdb_search_paths": ["shaders/"], "resolver_path": "native/pdb_resolver/bin/pdb-resolver.exe"},
            "expected_output_file": "expected_output/s3_tc07_shader_with_resolver.json",
            "assertions": [
                "status == success",
                "stage_count >= 1",
                "diagnostics.refreshed_source_cache == true"
            ]
        }
    ]
}

scenario_04 = {
    "scenario_name": "Edge Cases and Error Handling",
    "description": "Covers edge cases including invalid global IDs, non-existent resources, empty bindings, events with no shader source cache, and special characters in resource names.",
    "capture_db": "capture_db/capture.db",
    "test_cases": [
        {
            "id": "s4_tc01",
            "tool": "db-get-event-shader-source",
            "description": "Request shader source for a non-existent GlobalID (99999). Should return partial/empty stages.",
            "input": {"global_id": 99999},
            "expected_output_file": "expected_output/s4_tc01_invalid_global_id_shader.json",
            "assertions": [
                "status == partial or success",
                "event is null or not present",
                "stage_count == 0"
            ]
        },
        {
            "id": "s4_tc02",
            "tool": "db-get-event-resource",
            "description": "Request resources for a non-existent GlobalID (99999). Should return partial status with empty resources.",
            "input": {"global_id": 99999},
            "expected_output_file": "expected_output/s4_tc02_invalid_global_id_resources.json",
            "assertions": [
                "status == partial",
                "resource_count == 0",
                "diagnostics.reason is not null"
            ]
        },
        {
            "id": "s4_tc03",
            "tool": "db-get-resource-access-history",
            "description": "Request access history for a resource name that does not exist on the event (GlobalID=2, resource='NonExistentResource'). Should raise resource_not_bound error.",
            "input": {"global_id": 2, "resource": "NonExistentResource"},
            "expected_output_file": "expected_output/s4_tc03_nonexistent_resource.json",
            "assertions": [
                "status == error or partial",
                "error.code == 'resource_not_bound' or diagnostics.reason contains 'not bound'"
            ]
        },
        {
            "id": "s4_tc04",
            "tool": "db-get-event-resource",
            "description": "Request resources for an event with zero bound resources (GlobalID=50, an empty marker event).",
            "input": {"global_id": 50},
            "expected_output_file": "expected_output/s4_tc04_empty_bindings.json",
            "assertions": [
                "status == partial or success",
                "resource_count == 0"
            ]
        },
        {
            "id": "s4_tc05",
            "tool": "db-get-event-shader-source",
            "description": "Request shader source for an event whose PSO has no cached shader source (GlobalID=60, PSO without shader cache).",
            "input": {"global_id": 60},
            "expected_output_file": "expected_output/s4_tc05_no_shader_cache.json",
            "assertions": [
                "status == partial or success",
                "stage_count == 0",
                "diagnostics.reason contains 'No resolved shader source cache'"
            ]
        },
        {
            "id": "s4_tc06",
            "tool": "db-get-resource-access-history",
            "description": "Request access history for resource with special characters in name (GlobalID=2, resource='MyResource:Sub#1').",
            "input": {"global_id": 2, "resource": "MyResource:Sub#1"},
            "expected_output_file": "expected_output/s4_tc06_special_chars_resource.json",
            "assertions": [
                "status == success",
                "resource.resource_name == 'MyResource:Sub#1'"
            ]
        },
        {
            "id": "s4_tc07",
            "tool": "db-get-event-resource",
            "description": "Request resources with refresh=true to force database rebuild (GlobalID=2).",
            "input": {"global_id": 2, "refresh": True},
            "expected_output_file": "expected_output/s4_tc07_refresh_resources.json",
            "assertions": [
                "status == success",
                "resource_count >= 2"
            ]
        },
        {
            "id": "s4_tc08",
            "tool": "db-get-event-shader-source",
            "description": "Request shader source with output_path specified to test file writing (GlobalID=2).",
            "input": {"global_id": 2, "output_path": "expected_output/s4_tc08_shader_output.json"},
            "expected_output_file": "expected_output/s4_tc08_shader_output.json",
            "assertions": [
                "status == success",
                "output_paths is not empty",
                "output_paths[0] ends with 's4_tc08_shader_output.json'"
            ]
        }
    ]
}

def write_json(data, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"Written: {path}")

base = "g:/pix-tool-set/data/train"
write_json(scenario_01, f"{base}/scenario_01_simple_compute/test_cases.json")
write_json(scenario_02, f"{base}/scenario_02_graphics_pipeline/test_cases.json")
write_json(scenario_03, f"{base}/scenario_03_multi_pass/test_cases.json")
write_json(scenario_04, f"{base}/scenario_04_edge_cases/test_cases.json")
print("All scenarios generated successfully")
