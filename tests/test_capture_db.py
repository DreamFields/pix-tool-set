from __future__ import annotations

from pathlib import Path

from pix_tool_set.capture_db import (
    DATABASE_SCHEMA_VERSION,
    build_capture_database,
    connect_database,
    database_path,
    is_database_current,
    load_event,
    load_event_bound_resources,
    load_resource_references,
    load_resource_shader_accesses,
    load_same_named_resource_ids,
    load_shader_source_cache,
    store_shader_source_cache,
)
from pix_tool_set.indexer import build_index
from pix_tool_set import resource_history


def _sample_index(export_dir: Path) -> dict:
    fingerprints = [{"path": str(export_dir / "CommandLists_000.cpp"), "size": 10, "mtime_ns": 1}]
    events = [
        {
            "global_id": "1",
            "name": "Setup",
            "event_type": "PIXBeginEvent",
            "is_shader_event": False,
            "shader_stage_group": None,
            "file": str(export_dir / "CommandLists_000.cpp"),
            "line": 10,
            "marker_path": ["Frame"],
            "pso_id": None,
            "root_descriptor_tables": {},
            "root_constant_buffer_views": {},
            "input_assembler": {},
            "output_merger": {},
            "resource_refs": [{"line": 11, "text": "Use(GetResource(100).Get())"}],
        },
        {
            "global_id": "2",
            "name": "Dispatch",
            "event_type": "Dispatch",
            "is_shader_event": True,
            "shader_stage_group": "compute",
            "file": str(export_dir / "CommandLists_000.cpp"),
            "line": 20,
            "marker_path": ["Frame", "Pass"],
            "pso_id": "7",
            "root_descriptor_tables": {
                "0": {"stage": "Compute", "root_index": "0", "heap_id": "1", "descriptor_index": "400", "line": 19, "text": "SetComputeRootDescriptorTable"}
            },
            "root_constant_buffer_views": {
                "1": {"stage": "Compute", "root_index": "1", "resource_id": "200", "offset": "0", "line": 18, "text": "SetComputeRootConstantBufferView"}
            },
            "input_assembler": {},
            "output_merger": {},
            "resource_refs": [],
        },
    ]
    return {
        "version": 5,
        "generated_utc": "2026-01-01T00:00:00+00:00",
        "export_dir": str(export_dir),
        "fingerprints": fingerprints,
        "events": events,
        "events_by_global_id": {event["global_id"]: event for event in events},
        "shader_event_global_ids": ["2"],
        "pso_index": {"7": {"pso_id": "7", "stages": [{"stage": "CS", "blob_path": str(export_dir / "extracted_shaders" / "pso_7_CS.cso")}]}},
        "descriptor_index": {
            "400": [{"descriptor_index": "400", "heap_id": "1", "resource_id": "100", "view_type": "UAV", "call": "CreateUnorderedAccessView_Buffer", "file": str(export_dir / "Descriptors.cpp"), "line": 5, "text": "CreateUnorderedAccessView_Buffer"}]
        },
        "resource_names": {
            "100": {"resource_id": "100", "name": "SharedBuffer", "file": str(export_dir / "FrameResources.cpp"), "line": 1},
            "101": {"resource_id": "101", "name": "SharedBuffer", "file": str(export_dir / "FrameResources.cpp"), "line": 2},
            "200": {"resource_id": "200", "name": "Constants", "file": str(export_dir / "FrameResources.cpp"), "line": 3},
        },
        "resource_refs_by_resource_id": {"100": [{"global_id": "1", "line": 11, "text": "Use(GetResource(100).Get())"}]},
        "diagnostics": {},
    }


def test_build_capture_database_creates_schema_and_reuses_current_database(tmp_path: Path) -> None:
    index = _sample_index(tmp_path)

    result = build_capture_database(tmp_path, index)

    assert result["cache_hit"] is False
    assert result["schema_version"] == DATABASE_SCHEMA_VERSION
    assert Path(result["database_path"]) == database_path(tmp_path)
    assert result["table_counts"]["events"] == 2
    assert result["table_counts"]["resources"] == 3
    assert result["table_counts"]["resource_aliases"] == 3
    assert result["table_counts"]["resource_references"] == 1
    assert result["table_counts"]["descriptor_writes"] == 1
    assert result["table_counts"]["root_bindings"] == 2
    assert result["table_counts"]["event_bound_resources"] >= 2
    assert is_database_current(database_path(tmp_path), index["fingerprints"]) is True

    cached = build_capture_database(tmp_path, index)

    assert cached["cache_hit"] is True
    assert cached["table_counts"] == result["table_counts"]


def test_capture_database_loads_events_aliases_references_and_bound_resources(tmp_path: Path) -> None:
    index = _sample_index(tmp_path)
    result = build_capture_database(tmp_path, index)
    db_path = result["database_path"]

    event = load_event(db_path, "2")
    aliases = load_same_named_resource_ids(db_path, "SharedBuffer", "100")
    refs = load_resource_references(db_path, aliases)
    bound = load_event_bound_resources(db_path, "2")
    shader_accesses = load_resource_shader_accesses(db_path, aliases)

    assert event is not None
    assert event["global_id"] == "2"
    assert aliases == {"100", "101"}
    assert [ref["global_id"] for ref in refs] == ["1"]
    assert any(item["view_type"] == "CBV" and item["resource_id"] == "200" for item in bound)
    assert any(item["view_type"] == "UAV" and item["resource_id"] == "100" for item in bound)
    assert [item["event"]["global_id"] for item in shader_accesses] == ["2"]


def test_build_index_creates_capture_database_and_reports_diagnostics(tmp_path: Path) -> None:
    (tmp_path / "CommandLists_000.cpp").write_text(
        """
// GlobalId = 1
GetCommandList(1)->SetPipelineState(GetPipelineState(7));
GetCommandList(1)->SetComputeRootDescriptorTable(0, GetGpuDescriptor(g_descriptorHeap_1.Get(), 400));
GetCommandList(1)->Dispatch(1, 1, 1);
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "Descriptors.cpp").write_text(
        "CreateUnorderedAccessView_Buffer(GetResource(100).Get(), nullptr, nullptr, GetCpuDescriptor(g_descriptorHeap_1.Get(), 400));\n",
        encoding="utf-8",
    )
    (tmp_path / "FrameResources.cpp").write_text('GetObject(100)->SetName(L"(SharedBuffer)");\n', encoding="utf-8")
    (tmp_path / "CreatePSOs.cpp").write_text("", encoding="utf-8")

    first = build_index(tmp_path)
    second = build_index(tmp_path)

    assert first["cache_hit"] is False
    assert Path(first["database_path"]).exists()
    assert first["database_cache_hit"] is False
    assert first["database_table_counts"]["events"] == 1
    assert first["diagnostics"]["database_path"] == first["database_path"]
    assert second["cache_hit"] is True
    assert second["database_cache_hit"] is True
    assert second["database_table_counts"] == first["database_table_counts"]


def test_get_event_resource_writes_runtime_resolved_bindings_back_to_database(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "CommandLists_000.cpp").write_text(
        """
// GlobalId = 2
GetCommandList(1)->SetComputeRootDescriptorTable(0, GetGpuDescriptor(g_descriptorHeap_1.Get(), 400));
GetCommandList(1)->Dispatch(1, 1, 1);
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "Descriptors.cpp").write_text(
        "CreateUnorderedAccessView_Buffer(GetResource(100).Get(), nullptr, nullptr, GetCpuDescriptor(g_descriptorHeap_1.Get(), 400));\n",
        encoding="utf-8",
    )
    (tmp_path / "FrameResources.cpp").write_text('GetObject(100)->SetName(L"(SharedBuffer)");\n', encoding="utf-8")
    (tmp_path / "CreatePSOs.cpp").write_text("", encoding="utf-8")

    shader_source_calls: list[str] = []

    def fake_get_event_shader_source(export_dir, global_id, pdb_search_paths=None, refresh=False):
        shader_source_calls.append(str(global_id))
        return {"stages": [{"resolver_result": {"result": {"sources": [{"content": "RWStructuredBuffer<uint> RWSharedBuffer;"}]}}}]}

    monkeypatch.setattr(resource_history, "get_event_shader_source", fake_get_event_shader_source)

    first = resource_history.get_event_resource(tmp_path, 2, pdb_search_paths=["shader.pdb"])
    second = resource_history.get_event_resource(tmp_path, 2)

    assert first["diagnostics"]["database_hit"] is False
    assert first["resources"][0]["display_name"] == "SharedBuffer:RWSharedBuffer"
    assert second["diagnostics"]["database_hit"] is True
    assert second["diagnostics"]["query_mode"] == "sqlite"
    assert second["resources"][0]["display_name"] == "SharedBuffer:RWSharedBuffer"
    assert shader_source_calls == ["2"]


def test_resource_access_history_uses_database_queries_without_shader_scan(tmp_path: Path) -> None:
    (tmp_path / "CommandLists_000.cpp").write_text(
        """
// GlobalId = 1
Use(GetResource(100).Get());
// GlobalId = 2
GetCommandList(1)->SetComputeRootDescriptorTable(0, GetGpuDescriptor(g_descriptorHeap_1.Get(), 400));
GetCommandList(1)->Dispatch(1, 1, 1);
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "Descriptors.cpp").write_text(
        "CreateUnorderedAccessView_Buffer(GetResource(100).Get(), nullptr, nullptr, GetCpuDescriptor(g_descriptorHeap_1.Get(), 400));\n",
        encoding="utf-8",
    )
    (tmp_path / "FrameResources.cpp").write_text('GetObject(100)->SetName(L"(SharedBuffer)");\n', encoding="utf-8")
    (tmp_path / "CreatePSOs.cpp").write_text("", encoding="utf-8")

    result = resource_history.get_resource_access_history(tmp_path, 2, "SharedBuffer")

    assert result["diagnostics"]["database_hit"] is True
    assert result["diagnostics"]["query_mode"] == "sqlite"
    assert result["diagnostics"]["shader_event_scan_count"] == 0
    assert [row["global_id"] for row in result["access_history"]] == ["1", "2"]
    assert [row["binding"] for row in result["access_history"]] == ["API Parameters [0]", "CS UAV 0"]


def test_shader_source_cache_round_trips_resolved_sources(tmp_path: Path) -> None:
    index = _sample_index(tmp_path)
    build_capture_database(tmp_path, index)
    db_path = database_path(tmp_path)

    store_shader_source_cache(
        db_path,
        "7",
        [
            {
                "stage": "CS",
                "blob_path": str(tmp_path / "extracted_shaders" / "pso_7_CS.cso"),
                "blob_size": 123,
                "format": "DXBC",
                "debug_name": "shader.pdb",
                "resolver_result": {"status": "success", "result": {"sources": [{"content": "RWStructuredBuffer<uint> RWSharedBuffer;"}]}} ,
            }
        ],
    )

    stages = load_shader_source_cache(db_path, "7")

    assert len(stages) == 1
    assert stages[0]["stage"] == "CS"
    assert stages[0]["resolver_result"]["status"] == "cached"
    assert stages[0]["resolver_result"]["result"]["sources"][0]["content"] == "RWStructuredBuffer<uint> RWSharedBuffer;"


def test_build_database_writes_queryable_tables(tmp_path: Path) -> None:
    index = _sample_index(tmp_path)
    result = build_capture_database(tmp_path, index)
    db_path = result["database_path"]

    event = load_event(db_path, "2")
    aliases = load_same_named_resource_ids(db_path, "SharedBuffer", "100")
    refs = load_resource_references(db_path, aliases)
    bound = load_event_bound_resources(db_path, "2")
    shader_accesses = load_resource_shader_accesses(db_path, aliases)

    assert event is not None
    assert event["global_id"] == "2"
    assert aliases == {"100", "101"}
    assert [ref["global_id"] for ref in refs] == ["1"]
    assert any(item["view_type"] == "CBV" and item["resource_id"] == "200" for item in bound)
    assert any(item["view_type"] == "UAV" and item["resource_id"] == "100" for item in bound)
    assert [item["event"]["global_id"] for item in shader_accesses] == ["2"]


def test_build_database_persists_optional_shader_bindings(tmp_path: Path) -> None:
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    index = _sample_index(export_dir)
    index["shader_bindings"] = [
        {
            "pso_id": "99",
            "stage": "CS",
            "shader_binding_name": "InputTexture",
            "register_type": "t",
            "shader_binding_slot": 0,
            "register_space": 0,
            "view_type": "SRV",
            "resource_dimension": "Texture",
            "declaration_type": "Texture2D",
        }
    ]

    result = build_capture_database(export_dir, index, refresh=True)

    assert result["table_counts"]["shader_bindings"] == 1
    with connect_database(result["database_path"]) as connection:
        row = connection.execute("SELECT pso_id, stage, binding_name, register_type, register_slot, view_type FROM shader_bindings").fetchone()
        resource_count = connection.execute("SELECT COUNT(*) FROM event_bound_resources").fetchone()[0]
    assert row["pso_id"] == "99"
    assert row["stage"] == "CS"
    assert row["binding_name"] == "InputTexture"
    assert row["register_type"] == "t"
    assert row["register_slot"] == 0
    assert row["view_type"] == "SRV"
    assert resource_count > 0


def test_resource_access_history_uses_event_bound_resources(tmp_path: Path) -> None:
    (tmp_path / "CommandLists_000.cpp").write_text(
        """
// GlobalId = 1
Use(GetResource(100).Get());
// GlobalId = 2
GetCommandList(1)->SetComputeRootDescriptorTable(0, GetGpuDescriptor(g_descriptorHeap_1.Get(), 400));
GetCommandList(1)->Dispatch(1, 1, 1);
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "Descriptors.cpp").write_text(
        "CreateUnorderedAccessView_Buffer(GetResource(100).Get(), nullptr, nullptr, GetCpuDescriptor(g_descriptorHeap_1.Get(), 400));\n",
        encoding="utf-8",
    )
    (tmp_path / "FrameResources.cpp").write_text('GetObject(100)->SetName(L"(SharedBuffer)");\n', encoding="utf-8")
    (tmp_path / "CreatePSOs.cpp").write_text("", encoding="utf-8")

    result = resource_history.get_resource_access_history(tmp_path, 2, "SharedBuffer")

    assert result["diagnostics"]["database_hit"] is True
    assert result["diagnostics"]["query_mode"] == "sqlite"
    assert result["diagnostics"]["shader_event_scan_count"] == 0
    assert [row["global_id"] for row in result["access_history"]] == ["1", "2"]
    assert [row["binding"] for row in result["access_history"]] == ["API Parameters [0]", "CS UAV 0"]