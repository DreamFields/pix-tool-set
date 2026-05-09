from __future__ import annotations

from pathlib import Path

from pix_tool_set.context import ToolContext
from pix_tool_set.cpp_export import validate_cpp_export
from pix_tool_set import resource_history
from pix_tool_set.registry import get_registry
from pix_tool_set.tools import load_builtin_tools
from pix_tool_set.tools import event_analysis_tools
from pix_tool_set.tools import resource_history_tools


def test_builtin_tools_are_registered() -> None:
    load_builtin_tools()
    names = {tool.name for tool in get_registry().list_tools()}
    assert "extract-shader-events-tree" in names
    assert "analyze-events" in names
    assert "get-event-shader-source" in names
    assert "get-event-resource" in names
    assert "get-resource-access-history" in names
    assert "get-event-resource-history" not in names


def test_cli_and_mcp_names_are_unified() -> None:
    load_builtin_tools()
    for tool in get_registry().list_tools():
        assert tool.cli_name() == tool.mcp_name() == tool.name


def test_cpp_export_validation_accepts_minimal_export(tmp_path: Path) -> None:
    for name in ("CMakeLists.txt", "CreatePSOs.cpp", "resources.bin", "RenderFrame.cpp", "CommandLists_000.cpp"):
        (tmp_path / name).write_text("", encoding="utf-8")
    assert validate_cpp_export(tmp_path) == []


def test_analyze_events_accepts_nullable_limits(tmp_path: Path, monkeypatch) -> None:
    def fake_build_shader_event_tree(export_dir: str, *, refresh: bool = False) -> dict:
        assert export_dir == "export"
        assert refresh is False
        return {
            "metadata": {"source": "test"},
            "tree": [
                {
                    "event_type": "Draw",
                    "is_shader_event": True,
                    "shader_stage_group": "graphics",
                    "pso_id": 7,
                    "marker_path": ["Frame", "Pass"],
                    "children": [],
                }
            ],
        }

    monkeypatch.setattr(event_analysis_tools, "build_shader_event_tree", fake_build_shader_event_tree)

    result = event_analysis_tools.analyze_events(
        {"export_dir": "export", "top_limit": None, "sample_limit": None},
        ToolContext(workspace=tmp_path),
    )

    assert result.status == "success"
    assert result.data["summary"]["shader_event_count"] == 1
    assert result.data["examples"]["pso_ids"] == ["7"]






def test_resource_access_history_accepts_nullable_descriptor_scan_count(tmp_path: Path, monkeypatch) -> None:
    def fake_get_resource_access_history(
        export_dir: str,
        global_id: int,
        resource: str,
        *,
        descriptor_scan_count: int = 8,
        refresh: bool = False,
    ) -> dict:
        assert export_dir == "export"
        assert global_id == 1590
        assert resource == "RayTracing.LightGrid:RWLightGrid"
        assert descriptor_scan_count == 8
        assert refresh is False
        return {
            "status": "success",
            "event": {"global_id": "1590"},
            "resource": {
                "resource_id": "839",
                "resource_name": "RayTracing.LightGrid",
                "display_name": "RayTracing.LightGrid:RWLightGrid",
            },
            "access_history": [
                {
                    "global_id": "1590",
                    "binding": "CS UAV 0",
                    "read_write": "Read/Write",
                    "states": "STATE_UNORDERED_ACCESS",
                }
            ],
            "diagnostics": {"cache_hit": True},
        }

    monkeypatch.setattr(resource_history_tools, "default_output_path", lambda export_dir, filename: tmp_path / filename)
    monkeypatch.setattr(resource_history_tools, "write_json_file", lambda output_path, payload: str(output_path))
    monkeypatch.setattr(resource_history, "get_resource_access_history", fake_get_resource_access_history)

    result = resource_history_tools.get_resource_access_history_tool(
        {
            "export_dir": "export",
            "global_id": 1590,
            "resource": "RayTracing.LightGrid:RWLightGrid",
            "descriptor_scan_count": None,
        },
        ToolContext(workspace=tmp_path),
    )

    assert result.status == "success"
    assert result.data["resource"] == "RayTracing.LightGrid:RWLightGrid"
    assert result.data["resource_id"] == "839"
    assert result.data["access_count"] == 1


def test_get_event_resource_uses_shader_declared_bindings(monkeypatch) -> None:
    event = {
        "global_id": "1590",
        "root_descriptor_tables": {
            "1": {"stage": "Compute", "root_index": "1", "descriptor_index": "15800"},
            "0": {"stage": "Compute", "root_index": "0", "descriptor_index": "15802"},
        },
        "root_constant_buffer_views": {
            "2": {"stage": "Compute", "root_index": "2", "resource_id": "2291", "offset": "3674880"},
        },
    }
    fake_index = {
        "events_by_global_id": {"1590": event},
        "descriptor_index": {
            "15800": [{"descriptor_index": "15800", "resource_id": "839", "view_type": "UAV"}],
            "15801": [{"descriptor_index": "15801", "resource_id": "532", "view_type": "UAV"}],
            "15802": [{"descriptor_index": "15802", "resource_id": "292", "view_type": "SRV"}],
            "15803": [{"descriptor_index": "15803", "resource_id": "361", "view_type": "SRV"}],
        },
        "resource_names": {
            "2291": {"name": "Resource Allocator Underlying Buffer"},
            "292": {"name": "LightBuffer"},
            "361": {"name": "RayTracing.LightTranslatedClipBoxData"},
            "839": {"name": "RayTracing.LightGrid"},
            "532": {"name": "RayTracing.LightGridData"},
        },
        "cache_hit": False,
    }
    shader_source = """
cbuffer _RootShaderParameters : register(b0) { uint SceneLightCount; }
StructuredBuffer<FRTLightingData> SceneLights;
StructuredBuffer<float4> SceneLightTranslatedClipBoxData;
RWTexture2DArray<uint> RWLightGrid;
RWBuffer<uint> RWLightGridData;
"""

    monkeypatch.setattr(resource_history, "build_index", lambda export_dir, refresh=False: fake_index)
    monkeypatch.setattr(
        resource_history,
        "get_event_shader_source",
        lambda export_dir, global_id, pdb_search_paths=None, refresh=False: {
            "stages": [{"resolver_result": {"result": {"sources": [{"content": shader_source}]}}}]
        },
    )

    result = resource_history.get_event_resource("export", 1590, pdb_search_paths=["shader.pdb"])

    assert [item["view_type"] for item in result["resources"]] == ["CBV", "SRV", "SRV", "UAV", "UAV"]
    assert [item["display_name"] for item in result["resources"]] == [
        "Resource Allocator Underlying Buffer:_RootShaderParameters",
        "LightBuffer:SceneLights",
        "RayTracing.LightTranslatedClipBoxData:SceneLightTranslatedClipBoxData",
        "RayTracing.LightGrid:RWLightGrid",
        "RayTracing.LightGridData:RWLightGridData",
    ]
    assert result["diagnostics"]["shader_binding_counts"] == {"CBV": 1, "SRV": 2, "UAV": 2, "Sampler": 0}


def test_get_event_resource_supports_multiple_cbvs_textures_and_static_samplers(monkeypatch) -> None:
    event = {
        "global_id": "1263",
        "root_descriptor_tables": {
            "0": {"stage": "Compute", "root_index": "0", "descriptor_index": "3000"},
            "1": {"stage": "Compute", "root_index": "1", "descriptor_index": "4000"},
        },
        "root_constant_buffer_views": {
            "2": {"stage": "Compute", "root_index": "2", "resource_id": "900", "offset": "0"},
            "3": {"stage": "Compute", "root_index": "3", "resource_id": "901", "offset": "0"},
            "4": {"stage": "Compute", "root_index": "4", "resource_id": "902", "offset": "0"},
        },
    }
    fake_index = {
        "events_by_global_id": {"1263": event},
        "descriptor_index": {
            "3000": [{"descriptor_index": "3000", "resource_id": "100", "view_type": "SRV", "call": "CreateShaderResourceView_Tex2D"}],
            "3001": [{"descriptor_index": "3001", "resource_id": "101", "view_type": "SRV", "call": "CreateShaderResourceView_Buffer"}],
            "3002": [{"descriptor_index": "3002", "resource_id": "102", "view_type": "SRV", "call": "CreateShaderResourceView_Buffer"}],
            "3003": [{"descriptor_index": "3003", "resource_id": "103", "view_type": "SRV", "call": "CreateShaderResourceView_Buffer"}],
            "3004": [{"descriptor_index": "3004", "resource_id": "104", "view_type": "SRV", "call": "CreateShaderResourceView_Buffer"}],
            "3005": [{"descriptor_index": "3005", "resource_id": "105", "view_type": "SRV", "call": "CreateShaderResourceView_Buffer"}],
            "4000": [{"descriptor_index": "4000", "resource_id": "200", "view_type": "UAV", "call": "CreateUnorderedAccessView_Buffer"}],
            "4001": [{"descriptor_index": "4001", "resource_id": "201", "view_type": "UAV", "call": "CreateUnorderedAccessView_Buffer"}],
            "4002": [{"descriptor_index": "4002", "resource_id": "202", "view_type": "UAV", "call": "CreateUnorderedAccessView_Buffer"}],
            "4003": [{"descriptor_index": "4003", "resource_id": "203", "view_type": "UAV", "call": "CreateUnorderedAccessView_Buffer"}],
            "4004": [{"descriptor_index": "4004", "resource_id": "204", "view_type": "UAV", "call": "CreateUnorderedAccessView_Buffer"}],
        },
        "resource_names": {
            "900": {"name": "Resource Allocator Underlying Buffer"},
            "901": {"name": "Resource Allocator Underlying Buffer"},
            "902": {"name": "Resource Allocator Underlying Buffer"},
            "100": {"name": "HZBFurthest"},
            "101": {"name": "ViewSpacePosAndRadiusData"},
            "102": {"name": "ViewSpaceDirAndPreprocAngleData"},
            "103": {"name": "ViewSpaceRectPlanesData"},
            "104": {"name": "ViewSpaceClipBoxData"},
            "105": {"name": "IndirectionIndices"},
            "200": {"name": "NumCulledLightsGrid"},
            "201": {"name": "CulledLightDataGrid"},
            "202": {"name": "CulledLightDataAllocator"},
            "203": {"name": "CulledLightLinkAllocator"},
            "204": {"name": "CulledLightLinks"},
        },
        "cache_hit": False,
    }
    shader_source = """
cbuffer _RootShaderParameters : register(b0) { uint LightGridPixelSizeShift; }
cbuffer View {
    float4 BufferSizeAndInvSize;
    float4 BufferBilinearUVMinMax;
    float2 BufferToSceneTextureScale;
}
Texture2D<float> HZBTexture : register(t0);
Texture2DArray<float> HZBTextureArray;
StructuredBuffer<float4> LightViewSpacePositionAndRadius : register(t1);
StructuredBuffer<float4> LightViewSpaceDirAndPreprocAngle : register(t2);
StructuredBuffer<float4> LightViewSpaceRectPlanes : register(t3);
StructuredBuffer<float4> LightViewSpaceClipBoxData : register(t4);
Buffer<uint> IndirectionIndices : register(t5);
RWStructuredBuffer<uint> RWNumCulledLightsGrid : register(u0);
RWBuffer<uint> RWCulledLightDataGrid16Bit : register(u1);
RWStructuredBuffer<uint> RWCulledLightDataAllocator : register(u2);
RWStructuredBuffer<uint> RWCulledLightLinkAllocator : register(u3);
RWStructuredBuffer<uint> RWCulledLightLinks : register(u4);
SamplerState D3DStaticPointClampedSampler : register(s1, space1000);
"""

    monkeypatch.setattr(resource_history, "build_index", lambda export_dir, refresh=False: fake_index)
    monkeypatch.setattr(
        resource_history,
        "get_event_shader_source",
        lambda export_dir, global_id, pdb_search_paths=None, refresh=False: {
            "stages": [{"resolver_result": {"result": {"sources": [{"content": shader_source}]}}}]
        },
    )

    result = resource_history.get_event_resource("export", 1263, pdb_search_paths=["shader.pdb"])

    assert [item["display_name"] for item in result["resources"]] == [
        "Resource Allocator Underlying Buffer:_RootShaderParameters",
        "Resource Allocator Underlying Buffer:View",
        "Resource Allocator Underlying Buffer:ReflectionCaptureSM5",
        "HZBFurthest:HZBTexture",
        "ViewSpacePosAndRadiusData:LightViewSpacePositionAndRadius",
        "ViewSpaceDirAndPreprocAngleData:LightViewSpaceDirAndPreprocAngle",
        "ViewSpaceRectPlanesData:LightViewSpaceRectPlanes",
        "ViewSpaceClipBoxData:LightViewSpaceClipBoxData",
        "IndirectionIndices:IndirectionIndices",
        "NumCulledLightsGrid:RWNumCulledLightsGrid",
        "CulledLightDataGrid:RWCulledLightDataGrid16Bit",
        "CulledLightDataAllocator:RWCulledLightDataAllocator",
        "CulledLightLinkAllocator:RWCulledLightLinkAllocator",
        "CulledLightLinks:RWCulledLightLinks",
        "D3DStaticPointClampedSampler",
    ]
    assert [item["resource_dimension"] for item in result["resources"]] == [
        "Buffer",
        "Buffer",
        "Buffer",
        "Texture",
        "Buffer",
        "Buffer",
        "Buffer",
        "Buffer",
        "Buffer",
        "Buffer",
        "Buffer",
        "Buffer",
        "Buffer",
        "Buffer",
        "Sampler",
    ]
    assert result["resources"][-1]["view_type"] == "Static Sampler"
    assert result["resources"][-1]["register_space"] == 1000
    assert [item["shader_binding_slot"] for item in result["resources"]] == [0, 1, 2, 0, 1, 2, 3, 4, 5, 0, 1, 2, 3, 4, 1]
    assert result["diagnostics"]["shader_binding_counts"] == {"CBV": 2, "SRV": 7, "UAV": 5, "Sampler": 1}


def test_get_event_resource_supports_overlapping_descriptor_tables(monkeypatch) -> None:
    event = {
        "global_id": "1374",
        "root_descriptor_tables": {
            "1": {"stage": "Compute", "root_index": "1", "descriptor_index": "212117"},
            "0": {"stage": "Compute", "root_index": "0", "descriptor_index": "212121"},
        },
        "root_constant_buffer_views": {
            "2": {"stage": "Compute", "root_index": "2", "resource_id": "900", "offset": "0"},
            "3": {"stage": "Compute", "root_index": "3", "resource_id": "901", "offset": "0"},
        },
    }
    fake_index = {
        "events_by_global_id": {"1374": event},
        "descriptor_index": {
            "212117": [{"descriptor_index": "212117", "resource_id": "200", "view_type": "UAV", "call": "CreateUnorderedAccessView_Buffer"}],
            "212118": [{"descriptor_index": "212118", "resource_id": "201", "view_type": "UAV", "call": "CreateUnorderedAccessView_Tex2D"}],
            "212119": [{"descriptor_index": "212119", "resource_id": "202", "view_type": "UAV", "call": "CreateUnorderedAccessView_Tex2D"}],
            "212120": [{"descriptor_index": "212120", "resource_id": "203", "view_type": "UAV", "call": "CreateUnorderedAccessView_Tex2D"}],
            "212121": [{"descriptor_index": "212121", "resource_id": "100", "view_type": "SRV", "call": "CreateShaderResourceView_Buffer"}],
            "212122": [{"descriptor_index": "212122", "resource_id": "101", "view_type": "SRV", "call": "CreateShaderResourceView_Buffer"}],
            "212123": [{"descriptor_index": "212123", "resource_id": "102", "view_type": "SRV", "call": "CreateShaderResourceView_Buffer"}],
            "212124": [{"descriptor_index": "212124", "resource_id": "103", "view_type": "SRV", "call": "CreateShaderResourceView_Tex2D"}],
            "212125": [{"descriptor_index": "212125", "resource_id": "104", "view_type": "SRV", "call": "CreateShaderResourceView_Tex2D"}],
            "212126": [{"descriptor_index": "212126", "resource_id": "105", "view_type": "SRV", "call": "CreateShaderResourceView_Buffer"}],
            "212127": [{"descriptor_index": "212127", "resource_id": "106", "view_type": "SRV", "call": "CreateShaderResourceView_Buffer"}],
            "212128": [{"descriptor_index": "212128", "resource_id": "107", "view_type": "SRV", "call": "CreateShaderResourceView_Buffer"}],
            "212129": [{"descriptor_index": "212129", "resource_id": "108", "view_type": "SRV", "call": "CreateShaderResourceView_Tex2D"}],
        },
        "resource_names": {
            "900": {"name": "Resource Allocator Underlying Buffer"},
            "901": {"name": "Resource Allocator Underlying Buffer"},
            "100": {"name": "Lumen.Cards"},
            "101": {"name": "Lumen.PageTable"},
            "102": {"name": "Lumen.TileShadowDownsampleFactorAtlas"},
            "103": {"name": "Lumen.SceneDirectLighting"},
            "104": {"name": "Lumen.SceneIndirectLighting"},
            "105": {"name": "Resource PoolAllocator Underlying Buffer"},
            "106": {"name": "Resource PoolAllocator Underlying Buffer"},
            "107": {"name": "Resource PoolAllocator Underlying Buffer"},
            "108": {"name": "Lumen.SceneNumFramesAccumulatedAtlas"},
            "200": {"name": "Lumen.ResampledCardCaptureTileShadowDownsampleFactorAtlas"},
            "201": {"name": "Lumen.ResampledCardCaptureDirectLighting"},
            "202": {"name": "Lumen.ResampledCardCaptureIndirectLighting"},
            "203": {"name": "Lumen.ResampledCardCaptureNumFramesAccumulated"},
        },
        "cache_hit": False,
    }
    shader_source = """
cbuffer _RootShaderParameters : register(b0) { uint NumCards; }
cbuffer LumenCardScene : register(b1) { uint NumCardPages; }
StructuredBuffer<float4> LumenCardScene_CardData;
ByteAddressBuffer LumenCardScene_PageTableBuffer;
Buffer<uint4> TileShadowDownsampleFactorAtlasForResampling;
Texture2D DirectLightingAtlas;
Texture2D IndirectLightingAtlas;
Buffer<uint4> NewCardTileResampleData;
Buffer<uint4> NewCardPageResampleData;
Buffer<uint4> RectCoordBuffer;
Texture2D RadiosityNumFramesAccumulatedAtlas;
RWBuffer<uint> RWTileShadowDownsampleFactorAtlas;
RWTexture2D<float4> RWDirectLightingCardCaptureAtlas;
RWTexture2D<float4> RWRadiosityCardCaptureAtlas;
RWTexture2D<float4> RWRadiosityNumFramesAccumulatedCardCaptureAtlas;
SamplerState D3DStaticBilinearClampedSampler : register(s3, space1000);
"""

    monkeypatch.setattr(resource_history, "build_index", lambda export_dir, refresh=False: fake_index)
    monkeypatch.setattr(
        resource_history,
        "get_event_shader_source",
        lambda export_dir, global_id, pdb_search_paths=None, refresh=False: {
            "stages": [{"resolver_result": {"result": {"sources": [{"content": shader_source}]}}}]
        },
    )

    result = resource_history.get_event_resource("export", 1374, pdb_search_paths=["shader.pdb"])

    assert [item["display_name"] for item in result["resources"]] == [
        "Resource Allocator Underlying Buffer:_RootShaderParameters",
        "Resource Allocator Underlying Buffer:LumenCardScene",
        "Lumen.Cards:LumenCardScene_CardData",
        "Lumen.PageTable:LumenCardScene_PageTableBuffer",
        "Lumen.TileShadowDownsampleFactorAtlas:TileShadowDownsampleFactorAtlasForResampling",
        "Lumen.SceneDirectLighting:DirectLightingAtlas",
        "Lumen.SceneIndirectLighting:IndirectLightingAtlas",
        "Resource PoolAllocator Underlying Buffer:NewCardTileResampleData",
        "Resource PoolAllocator Underlying Buffer:NewCardPageResampleData",
        "Resource PoolAllocator Underlying Buffer:RectCoordBuffer",
        "Lumen.SceneNumFramesAccumulatedAtlas:RadiosityNumFramesAccumulatedAtlas",
        "Lumen.ResampledCardCaptureTileShadowDownsampleFactorAtlas:RWTileShadowDownsampleFactorAtlas",
        "Lumen.ResampledCardCaptureDirectLighting:RWDirectLightingCardCaptureAtlas",
        "Lumen.ResampledCardCaptureIndirectLighting:RWRadiosityCardCaptureAtlas",
        "Lumen.ResampledCardCaptureNumFramesAccumulated:RWRadiosityNumFramesAccumulatedCardCaptureAtlas",
        "D3DStaticBilinearClampedSampler",
    ]
    assert [item["shader_binding_slot"] for item in result["resources"]] == [0, 1, 0, 1, 2, 3, 4, 5, 6, 7, 8, 0, 1, 2, 3, 3]
    assert result["diagnostics"]["shader_binding_counts"] == {"CBV": 2, "SRV": 9, "UAV": 4, "Sampler": 1}


def test_get_event_resource_supports_graphics_pipeline_stages(monkeypatch) -> None:
    event = {
        "global_id": "1635",
        "shader_stage_group": "graphics_or_indirect",
        "input_assembler": {
            "vertex_buffers": [
                {"stage": "IA", "slot": 0, "resource_id": "384"},
                {"stage": "IA", "slot": 1, "resource_id": "34"},
            ],
            "index_buffer": {"stage": "IA", "slot": None, "resource_id": "384"},
        },
        "output_merger": {
            "render_targets": [
                {"stage": "OM", "slot": 0, "resource_id": "937"},
                {"stage": "OM", "slot": 1, "resource_id": "713"},
                {"stage": "OM", "slot": 2, "resource_id": "783"},
                {"stage": "OM", "slot": 3, "resource_id": "823"},
                {"stage": "OM", "slot": 4, "resource_id": "790"},
                {"stage": "OM", "slot": 5, "resource_id": "734"},
            ],
            "depth_stencil": {"stage": "OM", "resource_id": "914"},
        },
        "root_descriptor_tables": {
            "3": {"stage": "Graphics", "root_index": "3", "heap_id": "2290", "descriptor_index": "0"},
            "1": {"stage": "Graphics", "root_index": "1", "heap_id": "2290", "descriptor_index": "68"},
            "2": {"stage": "Graphics", "root_index": "2", "heap_id": "1128", "descriptor_index": "200405"},
            "0": {"stage": "Graphics", "root_index": "0", "heap_id": "1128", "descriptor_index": "200412"},
        },
        "root_constant_buffer_views": {
            "7": {"stage": "Graphics", "root_index": "7", "resource_id": "2291", "offset": "0"},
            "8": {"stage": "Graphics", "root_index": "8", "resource_id": "33", "offset": "0"},
            "9": {"stage": "Graphics", "root_index": "9", "resource_id": "33", "offset": "0"},
            "10": {"stage": "Graphics", "root_index": "10", "resource_id": "33", "offset": "0"},
            "4": {"stage": "Graphics", "root_index": "4", "resource_id": "2291", "offset": "0"},
            "5": {"stage": "Graphics", "root_index": "5", "resource_id": "33", "offset": "0"},
            "6": {"stage": "Graphics", "root_index": "6", "resource_id": "2291", "offset": "0"},
        },
    }
    fake_index = {
        "events_by_global_id": {"1635": event},
        "descriptor_index": {
            "0": [{"descriptor_index": "0", "heap_id": "2290", "resource_id": "999", "view_type": "UAV", "call": "CreateUnorderedAccessView_Tex2D"}],
            "200405": [{"descriptor_index": "200405", "heap_id": "1128", "resource_id": "33", "view_type": "SRV", "call": "CreateShaderResourceView_Buffer"}],
            "200406": [{"descriptor_index": "200406", "heap_id": "1128", "resource_id": "33", "view_type": "SRV", "call": "CreateShaderResourceView_Buffer"}],
            "200407": [{"descriptor_index": "200407", "heap_id": "1128", "resource_id": "574", "view_type": "SRV", "call": "CreateShaderResourceView_Buffer"}],
            "200408": [{"descriptor_index": "200408", "heap_id": "1128", "resource_id": "123", "view_type": "SRV", "call": "CreateShaderResourceView_Buffer"}],
            "200409": [{"descriptor_index": "200409", "heap_id": "1128", "resource_id": "222", "view_type": "SRV", "call": "CreateShaderResourceView_Buffer"}],
            "200410": [{"descriptor_index": "200410", "heap_id": "1128", "resource_id": "33", "view_type": "SRV", "call": "CreateShaderResourceView_Buffer"}],
            "200411": [{"descriptor_index": "200411", "heap_id": "1128", "resource_id": "1655", "view_type": "SRV", "call": "CreateShaderResourceView_Tex2D"}],
            "200412": [{"descriptor_index": "200412", "heap_id": "1128", "resource_id": "222", "view_type": "SRV", "call": "CreateShaderResourceView_Buffer"}],
            "200413": [{"descriptor_index": "200413", "heap_id": "1128", "resource_id": "1192", "view_type": "SRV", "call": "CreateShaderResourceView_Tex2D"}],
            "200414": [{"descriptor_index": "200414", "heap_id": "1128", "resource_id": "1201", "view_type": "SRV", "call": "CreateShaderResourceView_Tex2D"}],
            "200415": [{"descriptor_index": "200415", "heap_id": "1128", "resource_id": "1192", "view_type": "SRV", "call": "CreateShaderResourceView_Tex2D"}],
            "200416": [{"descriptor_index": "200416", "heap_id": "1128", "resource_id": "1656", "view_type": "SRV", "call": "CreateShaderResourceView_Tex2D"}],
            "200417": [{"descriptor_index": "200417", "heap_id": "1128", "resource_id": "1803", "view_type": "SRV", "call": "CreateShaderResourceView_Tex2D"}],
        },
        "resource_names": {
            "384": {"name": "Resource PoolAllocator Underlying Buffer"},
            "34": {"name": "PoolAllocator Heap"},
            "2291": {"name": "Resource Allocator Underlying Buffer"},
            "33": {"name": "Resource Allocator Underlying Buffer"},
            "574": {"name": "InstanceCulling.InstanceIdsBuffer"},
            "123": {"name": "GPUScene.InstanceSceneData"},
            "222": {"name": "GPUScene.PrimitiveData"},
            "1192": {"name": "BlackAlphaOneDummy"},
            "1201": {"name": "DefaultNormal8Bit"},
            "937": {"name": "SceneColor"},
            "713": {"name": "GBufferA"},
            "783": {"name": "GBufferB"},
            "823": {"name": "GBufferC"},
            "790": {"name": "GBufferD"},
            "734": {"name": "GBufferG"},
            "914": {"name": "SceneDepthZ"},
        },
        "cache_hit": False,
    }
    vs_source = """
cbuffer View : register(b0) { float4 ViewRect; }
cbuffer Scene : register(b1) { float4 SceneData; }
cbuffer LandscapeContinuousLODParameters : register(b2) { float4 LOD; }
cbuffer LandscapeParameters : register(b3) { float4 Landscape; }
Buffer<uint> View_LandscapeIndirection;
Buffer<uint> View_LandscapePerComponentData;
StructuredBuffer<uint> InstanceCulling_InstanceIdsBuffer;
StructuredBuffer<uint> Scene_GPUScene_GPUSceneInstanceSceneData;
StructuredBuffer<uint> Scene_GPUScene_GPUSceneInstancePayloadData;
StructuredBuffer<uint> Scene_GPUScene_GPUScenePrimitiveSceneData;
Buffer<uint> LandscapeContinuousLODParameters_SectionLODBias;
Texture2D LandscapeParameters_HeightmapTexture;
SamplerState LandscapeParameters_HeightmapTextureSampler;
"""
    ps_source = """
cbuffer View : register(b0) { float4 ViewRect; }
cbuffer OpaqueBasePass : register(b1) { float4 BasePass; }
cbuffer LandscapeParameters : register(b2) { float4 Landscape; }
cbuffer Material : register(b3) { float4 MaterialData; }
StructuredBuffer<uint> Scene_GPUScene_GPUScenePrimitiveSceneData;
Texture2D OpaqueBasePass_DBufferATexture;
Texture2D OpaqueBasePass_DBufferBTexture;
Texture2D OpaqueBasePass_DBufferCTexture;
Texture2D LandscapeParameters_NormalmapTexture;
Texture2D Material_Texture2D_0;
SamplerState View_MaterialTextureBilinearWrapedSampler;
SamplerState OpaqueBasePass_DBufferATextureSampler;
SamplerState LandscapeParameters_NormalmapTextureSampler;
SamplerState Material_Texture2D_0Sampler;
"""

    monkeypatch.setattr(resource_history, "build_index", lambda export_dir, refresh=False: fake_index)
    monkeypatch.setattr(
        resource_history,
        "get_event_shader_source",
        lambda export_dir, global_id, pdb_search_paths=None, refresh=False: {
            "stages": [
                {"stage": "PS", "resolver_result": {"result": {"sources": [{"content": ps_source}]}}},
                {"stage": "VS", "resolver_result": {"result": {"sources": [{"content": vs_source}]}}},
            ]
        },
    )

    result = resource_history.get_event_resource("export", 1635, pdb_search_paths=["shader.pdb"])

    assert [item["stage"] for item in result["resources"]] == ["IA", "IA", "IA"] + ["VS"] * 12 + ["PS"] * 12 + ["OM"] * 8
    assert [item["display_name"] for item in result["resources"]] == [
        "Resource PoolAllocator Underlying Buffer",
        "PoolAllocator Heap",
        "Resource PoolAllocator Underlying Buffer",
        "Resource Allocator Underlying Buffer:View",
        "Resource Allocator Underlying Buffer:Scene",
        "Resource Allocator Underlying Buffer:LandscapeContinuousLODParameters",
        "Resource Allocator Underlying Buffer:LandscapeParameters",
        "Resource Allocator Underlying Buffer:View_LandscapeIndirection",
        "Resource Allocator Underlying Buffer:View_LandscapePerComponentData",
        "InstanceCulling.InstanceIdsBuffer:InstanceCulling_InstanceIdsBuffer",
        "GPUScene.InstanceSceneData:Scene_GPUScene_GPUSceneInstanceSceneData",
        "GPUScene.PrimitiveData:Scene_GPUScene_GPUScenePrimitiveSceneData",
        "Resource Allocator Underlying Buffer:LandscapeContinuousLODParameters_SectionLODBias",
        "LandscapeParameters_HeightmapTexture",
        "LandscapeParameters_HeightmapTextureSampler",
        "Resource Allocator Underlying Buffer:View",
        "Resource Allocator Underlying Buffer:LandscapeParameters",
        "Resource Allocator Underlying Buffer:Material",
        "GPUScene.PrimitiveData:Scene_GPUScene_GPUScenePrimitiveSceneData",
        "BlackAlphaOneDummy:OpaqueBasePass_DBufferATexture",
        "DefaultNormal8Bit:OpaqueBasePass_DBufferBTexture",
        "BlackAlphaOneDummy:OpaqueBasePass_DBufferCTexture",
        "LandscapeParameters_NormalmapTexture",
        "Material_Texture2D_0",
        "View_MaterialTextureBilinearWrapedSampler",
        "OpaqueBasePass_DBufferATextureSampler",
        "LandscapeParameters_NormalmapTextureSampler",
        "SceneColor",
        "GBufferA",
        "GBufferB",
        "GBufferC",
        "GBufferD",
        "GBufferG",
        "SceneDepthZ",
        "SceneDepthZ",
    ]
    assert [item["view_type"] for item in result["resources"][:3]] == ["VB", "VB", "IB"]
    assert [item["root_index"] for item in result["resources"] if item["stage"] == "VS" and item["view_type"] == "CBV"] == ["7", "8", "9", "10"]
    assert [item["root_index"] for item in result["resources"] if item["stage"] == "PS" and item["view_type"] == "CBV"] == ["4", "5", "6"]
    assert [item["view_type"] for item in result["resources"][-8:]] == ["RTV", "RTV", "RTV", "RTV", "RTV", "RTV", "Depth", "Stencil"]


def test_get_event_resource_supports_graphics_pipeline_stage_1632_layout(monkeypatch) -> None:
    event = {
        "global_id": "1632",
        "shader_stage_group": "graphics_or_indirect",
        "input_assembler": {},
        "output_merger": {},
        "root_descriptor_tables": {
            "1": {"stage": "Graphics", "root_index": "1", "heap_id": "2290", "descriptor_index": "0", "line": 1},
            "2": {"stage": "Graphics", "root_index": "2", "heap_id": "1128", "descriptor_index": "200397", "line": 2},
            "0": {"stage": "Graphics", "root_index": "0", "heap_id": "1128", "descriptor_index": "200401", "line": 3},
        },
        "root_constant_buffer_views": {
            "5": {"stage": "Graphics", "root_index": "5", "resource_id": "2291", "offset": "0", "line": 4},
            "6": {"stage": "Graphics", "root_index": "6", "resource_id": "2291", "offset": "0", "line": 5},
            "7": {"stage": "Graphics", "root_index": "7", "resource_id": "33", "offset": "0", "line": 6},
            "3": {"stage": "Graphics", "root_index": "3", "resource_id": "2291", "offset": "0", "line": 7},
            "4": {"stage": "Graphics", "root_index": "4", "resource_id": "33", "offset": "0", "line": 8},
        },
    }
    fake_index = {
        "events_by_global_id": {"1632": event},
        "descriptor_index": {
            "200397": [{"descriptor_index": "200397", "heap_id": "1128", "resource_id": "574", "view_type": "SRV", "call": "CreateShaderResourceView_Buffer"}],
            "200398": [{"descriptor_index": "200398", "heap_id": "1128", "resource_id": "123", "view_type": "SRV", "call": "CreateShaderResourceView_Buffer"}],
            "200399": [{"descriptor_index": "200399", "heap_id": "1128", "resource_id": "222", "view_type": "SRV", "call": "CreateShaderResourceView_Buffer"}],
            "200400": [{"descriptor_index": "200400", "heap_id": "1128", "resource_id": "32", "view_type": "SRV", "call": "CreateShaderResourceView_Buffer", "text": "DXGI_FORMAT_R8G8B8A8_SNORM"}],
            "200401": [{"descriptor_index": "200401", "heap_id": "1128", "resource_id": "222", "view_type": "SRV", "call": "CreateShaderResourceView_Buffer"}],
            "200402": [{"descriptor_index": "200402", "heap_id": "1128", "resource_id": "1192", "view_type": "SRV", "call": "CreateShaderResourceView_Tex2D"}],
            "200403": [{"descriptor_index": "200403", "heap_id": "1128", "resource_id": "1201", "view_type": "SRV", "call": "CreateShaderResourceView_Tex2D"}],
            "200404": [{"descriptor_index": "200404", "heap_id": "1128", "resource_id": "1192", "view_type": "SRV", "call": "CreateShaderResourceView_Tex2D"}],
        },
        "resource_names": {
            "2291": {"name": "Resource Allocator Underlying Buffer"},
            "33": {"name": "Resource Allocator Underlying Buffer"},
            "32": {"name": "Resource PoolAllocator Underlying Buffer"},
            "574": {"name": "InstanceCulling.InstanceIdsBuffer"},
            "123": {"name": "GPUScene.InstanceSceneData"},
            "222": {"name": "GPUScene.PrimitiveData"},
            "1192": {"name": "BlackAlphaOneDummy"},
            "1201": {"name": "DefaultNormal8Bit"},
        },
        "cache_hit": False,
    }
    vs_source = """
cbuffer View { float4 View_TranslatedWorldToClip; }
cbuffer Scene { float4 Scene_GPUScene_GPUSceneFrameNumber; }
cbuffer GPUSkinPassThroughVFLooseParameters { float4 GPUSkinPassThroughVFLooseParameters_FrameNumber; }
cbuffer LocalVF { float4 LocalVF_VertexFetch_Parameters; }
StructuredBuffer<uint> InstanceCulling_InstanceIdsBuffer;
StructuredBuffer<uint> Scene_GPUScene_GPUSceneInstanceSceneData;
StructuredBuffer<uint> Scene_GPUScene_GPUSceneInstancePayloadData;
StructuredBuffer<uint> Scene_GPUScene_GPUScenePrimitiveSceneData;
Buffer<uint> GPUSkinPassThroughVFLooseParameters_PreviousPositionBuffer;
Buffer<uint> GPUSkinPassThroughVFLooseParameters_PreSkinnedTangentBuffer;
Buffer<uint> LocalVF_VertexFetch_PreSkinPositionBuffer;
Buffer<uint> LocalVF_VertexFetch_PackedTangentsBuffer;
Buffer<uint> LocalVF_VertexFetch_ColorComponentsBuffer;
float4 Main() : SV_Position { return View_TranslatedWorldToClip + Scene_GPUScene_GPUSceneFrameNumber + LocalVF_VertexFetch_Parameters; }
"""
    ps_source = """
cbuffer View { float4 View_TranslatedWorldToClip; }
cbuffer OpaqueBasePass { float4 OpaqueBasePass_Shared_UseBasePassSkylight; }
cbuffer Material { float4 Material_PreshaderBuffer; }
StructuredBuffer<uint> Scene_GPUScene_GPUScenePrimitiveSceneData;
Texture2D OpaqueBasePass_DBufferATexture;
Texture2D OpaqueBasePass_DBufferBTexture;
Texture2D OpaqueBasePass_DBufferCTexture;
SamplerState OpaqueBasePass_DBufferATextureSampler;
"""

    monkeypatch.setattr(resource_history, "build_index", lambda export_dir, refresh=False: fake_index)
    monkeypatch.setattr(
        resource_history,
        "get_event_shader_source",
        lambda export_dir, global_id, pdb_search_paths=None, refresh=False: {
            "stages": [
                {"stage": "PS", "resolver_result": {"result": {"sources": [{"content": ps_source}]}}},
                {"stage": "VS", "resolver_result": {"result": {"sources": [{"content": vs_source}]}}},
            ]
        },
    )

    result = resource_history.get_event_resource("export", 1632, pdb_search_paths=["shader.pdb"])

    assert [item["display_name"] for item in result["resources"]] == [
        "Resource Allocator Underlying Buffer:View",
        "Resource Allocator Underlying Buffer:Scene",
        "Resource Allocator Underlying Buffer:LocalVF",
        "InstanceCulling.InstanceIdsBuffer:InstanceCulling_InstanceIdsBuffer",
        "GPUScene.InstanceSceneData:Scene_GPUScene_GPUSceneInstanceSceneData",
        "GPUScene.PrimitiveData:Scene_GPUScene_GPUScenePrimitiveSceneData",
        "Resource PoolAllocator Underlying Buffer:LocalVF_VertexFetch_PackedTangentsBuffer",
        "Resource Allocator Underlying Buffer:View",
        "Resource Allocator Underlying Buffer:Material",
        "GPUScene.PrimitiveData:Scene_GPUScene_GPUScenePrimitiveSceneData",
        "BlackAlphaOneDummy:OpaqueBasePass_DBufferATexture",
        "DefaultNormal8Bit:OpaqueBasePass_DBufferBTexture",
        "BlackAlphaOneDummy:OpaqueBasePass_DBufferCTexture",
        "OpaqueBasePass_DBufferATextureSampler",
    ]
