"""Unit tests for the EditLedger: add_group, add_checkpoint, compare, reset.

These test the pure Python logic without needing a PIX capture or GPU. The ledger
is a JSON file in a temp directory, so every test is self-contained and fast.

Covers D3 (ledger accounting) and D4 (reset semantics) from the design's §11.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from pix_tool_set.engine.editledger import EditLedger


@pytest.fixture
def tmp_ledger():
    """A fresh EditLedger in a temp directory."""
    d = Path(tempfile.mkdtemp())
    return EditLedger(d), d


# --- add_group ---

def test_add_group_single_pso(tmp_ledger):
    ledger, d = tmp_ledger
    gid = ledger.add_group(
        stage="CS", shader_hash="abc123", scope="pso",
        target_psos=[42],
        bytecode_files={42: "edited_CreatePipelineState_42_CS.dxil"},
    )
    entries = ledger.list_entries()
    assert len(entries) == 1
    assert entries[0]["pso_id"] == "42"
    assert entries[0]["stage"] == "CS"
    assert entries[0]["shader_hash"] == "abc123"
    assert entries[0]["scope"] == "pso"
    assert entries[0]["group_id"] == gid
    assert entries[0]["bytecode_file"] == "edited_CreatePipelineState_42_CS.dxil"


def test_add_group_multi_pso(tmp_ledger):
    """--scope shader: one group, N PSO entries sharing a group_id."""
    ledger, d = tmp_ledger
    gid = ledger.add_group(
        stage="PS", shader_hash="def456", scope="shader",
        target_psos=[10, 20, 30],
        bytecode_files={10: "a.dxil", 20: "b.dxil", 30: "c.dxil"},
    )
    entries = ledger.list_entries()
    assert len(entries) == 3
    assert all(e["group_id"] == gid for e in entries)
    assert all(e["scope"] == "shader" for e in entries)
    pso_ids = sorted(e["pso_id"] for e in entries)
    assert pso_ids == ["10", "20", "30"]


def test_add_group_idempotent_force(tmp_ledger):
    """Re-patching the same (pso, stage) with --force updates, not duplicates."""
    ledger, d = tmp_ledger
    ledger.add_group(
        stage="CS", shader_hash="h1", scope="pso", target_psos=[1],
        bytecode_files={1: "v1.dxil"},
    )
    ledger.add_group(
        stage="CS", shader_hash="h1", scope="pso", target_psos=[1],
        bytecode_files={1: "v2.dxil"},
    )
    entries = ledger.list_entries()
    assert len(entries) == 1  # updated, not duplicated
    assert entries[0]["bytecode_file"] == "v2.dxil"


# --- checkpoints ---

def test_add_and_list_checkpoints(tmp_ledger):
    ledger, d = tmp_ledger
    ledger.add_checkpoint("v1", {"changed_pixels": 100, "changed_share_percent": 5.0})
    ledger.add_checkpoint("v2", {"changed_pixels": 200, "changed_share_percent": 10.0})
    cps = ledger.list_checkpoints()
    assert len(cps) == 2
    assert cps[0]["name"] == "v1"
    assert cps[0]["changed_pixels"] == 100
    assert cps[1]["name"] == "v2"
    assert cps[1]["changed_pixels"] == 200


def test_compare_checkpoints(tmp_ledger):
    ledger, d = tmp_ledger
    ledger.add_checkpoint("v1", {
        "changed_pixels": 100,
        "changed_share_percent": 5.0,
        "mean_abs_delta_8bit": {"R": 1.0, "G": 2.0, "B": 3.0},
    })
    ledger.add_checkpoint("v2", {
        "changed_pixels": 200,
        "changed_share_percent": 10.0,
        "mean_abs_delta_8bit": {"R": 2.0, "G": 4.0, "B": 6.0},
    })
    delta = ledger.compare_checkpoints("v1", "v2")
    assert delta["a"] == "v1"
    assert delta["b"] == "v2"
    assert delta["delta_changed_pixels"] == 100
    assert delta["delta_changed_share"] == 5.0


def test_compare_nonexistent_checkpoint(tmp_ledger):
    ledger, d = tmp_ledger
    delta = ledger.compare_checkpoints("nonexistent", "also_nonexistent")
    assert "error" in delta


def test_checkpoint_overwrite(tmp_ledger):
    ledger, d = tmp_ledger
    ledger.add_checkpoint("v1", {"changed_pixels": 100, "changed_share_percent": 5.0})
    ledger.add_checkpoint("v1", {"changed_pixels": 200, "changed_share_percent": 10.0})
    cps = ledger.list_checkpoints()
    assert len(cps) == 1
    assert cps[0]["changed_pixels"] == 200


# --- reset ---

def test_reset_deletes_bytecode_files(tmp_ledger):
    ledger, d = tmp_ledger
    # Create fake bytecode files.
    bc1 = d / "edited_CreatePipelineState_1_CS.dxil"
    bc2 = d / "edited_CreatePipelineState_2_CS.dxil"
    bc1.write_bytes(b"shader1")
    bc2.write_bytes(b"shader2")

    ledger.add_group(
        stage="CS", shader_hash="h", scope="shader", target_psos=[1, 2],
        bytecode_files={1: str(bc1), 2: str(bc2)},
    )

    actions = ledger.reset()
    assert len(actions) == 2
    assert not bc1.exists()
    assert not bc2.exists()
    assert ledger.is_empty


def test_clear(tmp_ledger):
    ledger, d = tmp_ledger
    ledger.add_group(stage="CS", shader_hash="h", scope="pso", target_psos=[1])
    assert ledger.count == 1
    ledger.clear()
    assert ledger.is_empty
    assert ledger.count == 0


# --- persistence ---

def test_persistence(tmp_ledger):
    ledger, d = tmp_ledger
    ledger.add_group(stage="CS", shader_hash="h", scope="pso", target_psos=[1])
    ledger.add_checkpoint("v1", {"changed_pixels": 50})

    # Reload from disk.
    ledger2 = EditLedger(d)
    assert ledger2.count == 1
    assert len(ledger2.list_checkpoints()) == 1


def test_groups_view(tmp_ledger):
    ledger, d = tmp_ledger
    ledger.add_group(stage="CS", shader_hash="h1", scope="pso", target_psos=[1])
    ledger.add_group(stage="CS", shader_hash="h2", scope="shader", target_psos=[2, 3])
    groups = ledger.groups()
    assert len(groups) == 2
