from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from pix_tool_set.capture_db import connect_database, database_path
from pix_tool_set.cli import build_parser
from pix_tool_set.errors import PixToolError
from pix_tool_set.event_list_csv import parse_event_list_csv
from pix_tool_set.event_list_export import build_save_event_list_command, export_event_list_csv
from pix_tool_set.indexer import _build_event_id_map, build_index_from_capture
from pix_tool_set.registry import get_registry
from pix_tool_set.tools import load_builtin_tools


def test_build_save_event_list_command_adds_optional_counters() -> None:
    command = build_save_event_list_command("pixtool.exe", "capture.wpix", "events.csv", "gpu/*")

    assert command == ["pixtool.exe", "open-capture", "capture.wpix", "save-event-list", "events.csv", "--counters=gpu/*"]


def test_export_event_list_csv_reuses_current_csv(tmp_path: Path) -> None:
    capture = tmp_path / "frame.wpix"
    capture.write_text("capture", encoding="utf-8")
    csv_path = tmp_path / "out" / ".cache" / "pix-tool-set" / "event-list.csv"
    csv_path.parent.mkdir(parents=True)
    csv_path.write_text("GlobalId,Name\n1,Draw\n", encoding="utf-8")

    result = export_event_list_csv(capture_path=capture, export_dir=tmp_path / "out", pixtool_path=Path(__file__), runner=lambda command: pytest.fail("runner should not be called"))

    assert result.cache_hit is True
    assert result.refreshed is False
    assert result.paths.csv_path == csv_path


def test_export_event_list_csv_reports_command_failure(tmp_path: Path) -> None:
    capture = tmp_path / "frame.wpix"
    capture.write_text("capture", encoding="utf-8")

    def fail_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 7, "out", "bad")

    with pytest.raises(PixToolError) as exc_info:
        export_event_list_csv(capture_path=capture, export_dir=tmp_path / "out", refresh=True, pixtool_path=Path(__file__), runner=fail_runner)

    assert exc_info.value.code == "save_event_list_failed"
    assert exc_info.value.stage == "save_event_list"
    assert exc_info.value.details["returncode"] == 7


def test_parse_event_list_csv_maps_optional_fields_and_counters(tmp_path: Path) -> None:
    csv_path = tmp_path / "events.csv"
    csv_path.write_text(
        "GlobalId,Event Name,Depth,Start Time,Duration,GPU Busy\n"
        "1,Frame,0,0,10,\n"
        "2,Draw,1,1,2,98\n",
        encoding="utf-8",
    )

    parsed = parse_event_list_csv(csv_path)

    assert parsed["events"][1]["global_id"] == "2"
    assert parsed["events"][1]["parent_global_id"] == "1"
    assert parsed["events"][1]["event_list"]["depth"] == 1
    assert parsed["events"][1]["event_list"]["duration"] == "2"
    assert parsed["events"][1]["event_list"]["counters"] == {"GPU Busy": "98"}


def test_parse_event_list_csv_uses_queue_id_when_global_id_is_sparse(tmp_path: Path) -> None:
    csv_path = tmp_path / "events.csv"
    csv_path.write_text(
        "Queue ID, Parent, Name, Global ID\n"
        "0, -1, Wait, 1\n"
        "1, -1, Reset, \n"
        "2, 1, WriteBufferImmediate, 3, extra\n",
        encoding="utf-8",
    )

    parsed = parse_event_list_csv(csv_path)

    assert [event["global_id"] for event in parsed["events"]] == ["0", "1", "2"]
    assert parsed["events"][1]["parent_global_id"] == "-1"
    assert parsed["events"][2]["parent_global_id"] == "1"
    assert "extra_columns" in parsed["events"][2]["event_list"]["raw"]


def test_parse_event_list_csv_prefers_queue_id_over_global_id_header_order(tmp_path: Path) -> None:
    csv_path = tmp_path / "events.csv"
    csv_path.write_text(
        "Global ID, Queue ID, Parent, Name\n"
        "900, 0, -1, Wait\n"
        ", 1, 0, Dispatch\n",
        encoding="utf-8",
    )

    parsed = parse_event_list_csv(csv_path)

    assert [event["global_id"] for event in parsed["events"]] == ["0", "1"]
    assert parsed["events"][1]["parent_global_id"] == "0"


def test_parse_event_list_csv_requires_event_id_and_name(tmp_path: Path) -> None:
    csv_path = tmp_path / "events.csv"
    csv_path.write_text("GlobalId,Duration\n1,2\n", encoding="utf-8")

    with pytest.raises(PixToolError) as exc_info:
        parse_event_list_csv(csv_path)

    assert exc_info.value.code == "event_list_csv_missing_required_field"
    assert exc_info.value.details["missing_fields"] == ["name"]


def test_build_index_from_capture_imports_csv_into_database(tmp_path: Path) -> None:
    capture = tmp_path / "frame.wpix"
    capture.write_text("capture", encoding="utf-8")

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        Path(command[4]).write_text(
            "GlobalId,Name,Depth,Start Time,Duration,Counter A\n"
            "1,Frame,0,0,16,\n"
            "2,Dispatch,1,4,3,12\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "ok", "")

    index = build_index_from_capture(capture_path=capture, export_dir=tmp_path / "out", refresh=True, pixtool_path=Path(__file__), counters="Counter*", workspace=tmp_path, runner=runner)

    assert index["event_list_refreshed"] is True
    assert Path(index["event_list_csv_path"]).exists()
    assert Path(index["database_path"]) == database_path(tmp_path / "out")
    assert index["database_table_counts"]["events"] == 2
    with connect_database(index["database_path"]) as connection:
        row = connection.execute("SELECT event_depth, start_time, duration, counters_json FROM events WHERE global_id = '2'").fetchone()
    assert row["event_depth"] == 1
    assert row["start_time"] == "4"
    assert row["duration"] == "3"
    assert "Counter A" in row["counters_json"]


def test_build_event_id_map_uses_exact_id_before_name_order() -> None:
    csv_events = [
        {"global_id": "10", "name": "Dispatch", "event_type": "Dispatch"},
        {"global_id": "11", "name": "Draw", "event_type": "Draw"},
    ]
    cpp_events = [
        {"global_id": "99", "name": "Dispatch", "event_type": "Dispatch"},
        {"global_id": "11", "name": "Draw", "event_type": "Draw"},
    ]

    mapping = _build_event_id_map(csv_events, cpp_events)

    assert mapping[0]["queue_id"] == "10"
    assert mapping[0]["cpp_global_id"] == "99"
    assert mapping[0]["match_strategy"] == "name_order"
    assert mapping[0]["confidence"] == 0.75
    assert mapping[1]["queue_id"] == "11"
    assert mapping[1]["cpp_global_id"] == "11"
    assert mapping[1]["match_strategy"] == "exact_id"
    assert mapping[1]["confidence"] == 1.0


def test_build_event_id_map_reports_name_order_conflict() -> None:
    csv_events = [
        {"global_id": "1", "name": "Dispatch", "event_type": "Dispatch"},
        {"global_id": "2", "name": "Dispatch", "event_type": "Dispatch"},
    ]
    cpp_events = [{"global_id": "101", "name": "Dispatch", "event_type": "Dispatch"}]

    mapping = _build_event_id_map(csv_events, cpp_events)

    assert [item["status"] for item in mapping] == ["conflict", "conflict"]
    assert all(item["cpp_global_id"] is None for item in mapping)


def test_build_index_from_capture_persists_event_id_map_without_replacing_events(tmp_path: Path) -> None:
    capture = tmp_path / "frame.wpix"
    capture.write_text("capture", encoding="utf-8")
    export_dir = tmp_path / "out"
    export_dir.mkdir()
    (export_dir / "CommandLists001.cpp").write_text(
        "// GlobalId = 100\n"
        "commandList->Dispatch(1, 1, 1);\n",
        encoding="utf-8",
    )

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        Path(command[4]).write_text(
            "Queue ID,Parent,Name,Global ID\n"
            "1,-1,Dispatch,100\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "ok", "")

    index = build_index_from_capture(capture_path=capture, export_dir=export_dir, refresh=True, pixtool_path=Path(__file__), workspace=tmp_path, runner=runner)

    assert index["database_table_counts"]["events"] == 1
    assert index["database_table_counts"]["event_id_map"] == 1
    with connect_database(index["database_path"]) as connection:
        event_row = connection.execute("SELECT global_id FROM events").fetchone()
        map_row = connection.execute("SELECT queue_id, cpp_global_id, match_strategy, status FROM event_id_map").fetchone()
    assert event_row["global_id"] == "1"
    assert map_row["queue_id"] == "1"
    assert map_row["cpp_global_id"] == "100"
    assert map_row["match_strategy"] == "name_order"
    assert map_row["status"] == "matched"


def test_build_index_from_capture_imports_cpp_resource_facts_for_mapped_events(tmp_path: Path) -> None:
    capture = tmp_path / "frame.wpix"
    capture.write_text("capture", encoding="utf-8")
    export_dir = tmp_path / "out"
    export_dir.mkdir()
    (export_dir / "CommandLists001.cpp").write_text(
        "// GlobalId = 100\n"
        "commandList->SetComputeRootSignature(GetRootSignature(7));\n"
        "commandList->SetPipelineState(GetPipelineState(9));\n"
        "commandList->SetComputeRootConstantBufferView(0, GetGpuva(50, 0));\n"
        "commandList->SetComputeRootDescriptorTable(1, GetGpuDescriptor(g_descriptorHeap_2.Get(), 30));\n"
        "commandList->Dispatch(1, 1, 1);\n",
        encoding="utf-8",
    )
    (export_dir / "Descriptors001.cpp").write_text(
        "device->CreateShaderResourceView(GetResource(60).Get(), nullptr, GetCpuDescriptor(g_descriptorHeap_2.Get(), 30));\n",
        encoding="utf-8",
    )
    (export_dir / "FrameResources001.cpp").write_text(
        "// ApiObjectId = 7\n"
        "rootParameters[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;\n"
        "descriptorRanges[0] = { D3D12_DESCRIPTOR_RANGE_TYPE_SRV, 1, 0, 0, D3D12_DESCRIPTOR_RANGE_OFFSET_APPEND, 0 };\n"
        "rootParameters[1].DescriptorTable = { 1, descriptorRanges };\n"
        "CreateAndTrackRootSignature();\n"
        "GetObject(50)->SetName(L\"(CameraCB)\");\n"
        "GetObject(60)->SetName(L\"(InputTexture)\");\n",
        encoding="utf-8",
    )

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        Path(command[4]).write_text(
            "Queue ID,Parent,Name,Global ID\n"
            "1,-1,Dispatch,100\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "ok", "")

    index = build_index_from_capture(capture_path=capture, export_dir=export_dir, refresh=True, pixtool_path=Path(__file__), workspace=tmp_path, runner=runner)

    event = index["events_by_global_id"]["1"]
    assert event["cpp_global_id"] == "100"
    assert event["pso_id"] == "9"
    assert event["root_signature_id"] == "7"
    assert event["root_constant_buffer_views"]["0"]["resource_id"] == "50"
    assert event["root_descriptor_tables"]["1"]["descriptor_index"] == "30"
    with connect_database(index["database_path"]) as connection:
        event_row = connection.execute("SELECT pso_id, root_signature_id FROM events WHERE global_id = '1'").fetchone()
        resource_count = connection.execute("SELECT COUNT(*) FROM resources").fetchone()[0]
        descriptor_count = connection.execute("SELECT COUNT(*) FROM descriptor_writes").fetchone()[0]
        root_binding_count = connection.execute("SELECT COUNT(*) FROM root_bindings").fetchone()[0]
        layout_count = connection.execute("SELECT COUNT(*) FROM root_signature_layout").fetchone()[0]
        resolved_row = connection.execute("SELECT resource_id, view_type, source, confidence FROM event_bound_resources WHERE global_id = '1' AND descriptor_index = '30'").fetchone()
    assert event_row["pso_id"] == "9"
    assert event_row["root_signature_id"] == "7"
    assert resource_count == 2
    assert descriptor_count == 1
    assert root_binding_count == 2
    assert layout_count == 1
    assert resolved_row["resource_id"] == "60"
    assert resolved_row["view_type"] == "SRV"
    assert resolved_row["source"] == "database_resolved"
    assert resolved_row["confidence"] == 1.0


def test_build_index_tool_schema_matches_cli_and_mcp() -> None:
    load_builtin_tools()
    definition = get_registry().get("build-index")
    parser = build_parser()

    assert definition.parameters["required"] == ["capture_path"]
    assert set(definition.parameters["properties"]) == {"capture_path", "export_dir", "refresh", "pixtool_path", "counters"}
    assert definition.requires_cpp_export is False
    assert parser.prog == "pix-tool-set"
