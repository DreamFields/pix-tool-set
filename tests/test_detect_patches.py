"""Unit tests for detect_patches and baseline fingerprinting.

Tests the D5 (baseline gate) patch detection logic without needing a PIX capture.
Creates mock export directories with the markers that shader-edit-apply writes
and verifies that detect_patches finds them correctly.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from pix_tool_set.tools.replay_session_tools import detect_patches, _export_fingerprint


@pytest.fixture
def mock_export():
    """A mock export directory with no patches (clean baseline)."""
    d = Path(tempfile.mkdtemp())
    (d / "CreatePSOs.cpp").write_text(
        "void CreatePipelineState_42() {\n"
        "  PSO.Stage[0] = { ... };\n"
        "}\n",
        encoding="utf-8",
    )
    (d / "resources.bin").write_bytes(b"\x00" * 100)
    (d / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.20)\n")
    return d


def test_detect_patches_clean(mock_export):
    """No patches in a clean export."""
    patches = detect_patches(mock_export)
    assert patches == []


def test_detect_patches_dxil_file(mock_export):
    """An edited_*.dxil file is detected as a patch."""
    (mock_export / "edited_CreatePipelineState_42_CS.dxil").write_bytes(b"shader")
    patches = detect_patches(mock_export)
    assert len(patches) == 1
    assert patches[0]["pso_id"] == "42"
    assert patches[0]["stage"] == "CS"


def test_detect_patches_marker_in_createpsos(mock_export):
    """The // pix-tool-set marker in CreatePSOs.cpp is detected."""
    text = (mock_export / "CreatePSOs.cpp").read_text()
    text = text.replace(
        "PSO.Stage[0] = { ... };",
        "PSO.Stage[0] = { ... };\n"
        "    // pix-tool-set: CS replaced by shader-edit-apply\n"
        "    auto editedBytes = Helpers::ReadFileBytes(L\"edited_CreatePipelineState_42_CS.dxil\");\n"
        "    if (!editedBytes.empty()) PSO.Stage[0].pShaderBytecode = { editedBytes.data(), editedBytes.size() };",
    )
    (mock_export / "CreatePSOs.cpp").write_text(text, encoding="utf-8")
    patches = detect_patches(mock_export)
    # Should find the marker (and possibly the .dxil file if it exists, but we didn't create one).
    assert any(p["stage"] == "CS" for p in patches)


def test_detect_patches_multiple(mock_export):
    """Multiple patches are all detected."""
    (mock_export / "edited_CreatePipelineState_42_CS.dxil").write_bytes(b"a")
    (mock_export / "edited_CreatePipelineState_43_PS.dxil").write_bytes(b"b")
    patches = detect_patches(mock_export)
    assert len(patches) >= 2
    stages = {p["stage"] for p in patches}
    assert "CS" in stages
    assert "PS" in stages


def test_export_fingerprint_stable(mock_export):
    """The fingerprint is stable when files don't change."""
    fp1 = _export_fingerprint(mock_export)
    fp2 = _export_fingerprint(mock_export)
    assert fp1 == fp2


def test_export_fingerprint_changes(mock_export):
    """The fingerprint changes when a file is modified."""
    fp1 = _export_fingerprint(mock_export)
    (mock_export / "CreatePSOs.cpp").write_text("different content", encoding="utf-8")
    fp2 = _export_fingerprint(mock_export)
    assert fp1 != fp2
