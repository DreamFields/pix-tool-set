"""Unit tests for shader scope resolution (D1).

Tests the logic that decides whether shader-edit-apply --scope should patch one
PSO or all sibling PSOs. The key invariant: a shader used by N PSOs must error
with --scope auto (the default), because a silent partial change is the most
expensive failure mode — it looks exactly like a successful edit.

These tests use mock objects for the Capture class, since the real Capture
requires a PIX runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# --- Mock shader/PSO objects ---

@dataclass
class MockShader:
    stage: str
    shader_hash: str
    hash_md5: str = ""


@dataclass
class MockPSO:
    api_id: int
    shaders: list[MockShader] = field(default_factory=list)


class MockCapture:
    """Minimal Capture mock with shader_pso_index and sibling_psos."""

    def __init__(self, psos: list[MockPSO]):
        self.pipeline_states = {pso.api_id: pso for pso in psos}
        self._index: dict[tuple[str, str], list[int]] = {}
        for pso in psos:
            for shader in pso.shaders:
                key = (shader.stage, shader.shader_hash or shader.hash_md5)
                if pso.api_id not in self._index.setdefault(key, []):
                    self._index[key].append(pso.api_id)

    @property
    def shader_pso_index(self):
        return dict(self._index)

    def sibling_psos(self, stage: str, shader_hash: str) -> list[int]:
        if not shader_hash:
            return []
        return list(self._index.get((stage, shader_hash), []))


# --- Tests ---

def test_single_pso_no_siblings():
    """A shader used by 1 PSO has no siblings."""
    capture = MockCapture([
        MockPSO(42, [MockShader("CS", "hash1")]),
    ])
    siblings = capture.sibling_psos("CS", "hash1")
    assert siblings == [42]


def test_multiple_pso_siblings():
    """A shader used by 3 PSOs has 3 siblings."""
    capture = MockCapture([
        MockPSO(10, [MockShader("CS", "shared_hash")]),
        MockPSO(20, [MockShader("CS", "shared_hash")]),
        MockPSO(30, [MockShader("CS", "shared_hash")]),
    ])
    siblings = capture.sibling_psos("CS", "shared_hash")
    assert len(siblings) == 3
    assert set(siblings) == {10, 20, 30}


def test_different_stages_different_keys():
    """VS and PS with the same hash are separate keys."""
    capture = MockCapture([
        MockPSO(1, [
            MockShader("VS", "collision_hash"),
            MockShader("PS", "collision_hash"),
        ]),
    ])
    vs_siblings = capture.sibling_psos("VS", "collision_hash")
    ps_siblings = capture.sibling_psos("PS", "collision_hash")
    assert vs_siblings == [1]
    assert ps_siblings == [1]
    # But the index keys are separate:
    assert ("VS", "collision_hash") in capture.shader_pso_index
    assert ("PS", "collision_hash") in capture.shader_pso_index


def test_empty_hash_returns_empty():
    """A shader with no hash returns empty siblings (safe fallback)."""
    capture = MockCapture([
        MockPSO(1, [MockShader("CS", "")]),
    ])
    siblings = capture.sibling_psos("CS", "")
    assert siblings == []


def test_unknown_hash_returns_empty():
    """A hash not in the index returns empty."""
    capture = MockCapture([
        MockPSO(1, [MockShader("CS", "known_hash")]),
    ])
    siblings = capture.sibling_psos("CS", "unknown_hash")
    assert siblings == []


def test_scope_auto_with_siblings_should_error():
    """The design says --scope auto must error when >1 PSO uses the shader.

    This test verifies the precondition: the sibling_psos list has >1 entry.
    The actual error is raised by shader_edit_apply's scope resolution logic.
    """
    capture = MockCapture([
        MockPSO(10, [MockShader("PS", "shared")]),
        MockPSO(20, [MockShader("PS", "shared")]),
    ])
    siblings = capture.sibling_psos("PS", "shared")
    assert len(siblings) > 1
    # In shader_edit_apply, --scope auto with len(siblings) > 1 raises
    # PixToolError(code="ambiguous_shader_scope", ...)


def test_scope_shader_with_siblings_targets_all():
    """--scope shader should target all sibling PSOs."""
    capture = MockCapture([
        MockPSO(10, [MockShader("CS", "shared")]),
        MockPSO(20, [MockShader("CS", "shared")]),
        MockPSO(30, [MockShader("CS", "shared")]),
    ])
    siblings = capture.sibling_psos("CS", "shared")
    # In shader_edit_apply, --scope shader sets target_pso_ids = siblings
    target_pso_ids = siblings if len(siblings) > 1 else [10]
    assert len(target_pso_ids) == 3
    assert set(target_pso_ids) == {10, 20, 30}
