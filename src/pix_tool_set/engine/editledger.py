"""Edit ledger: durable record of every shader-edit-apply patch in an export.

The ledger exists because the filesystem alone cannot answer "what did I change, and
in what order?" A ``.orig`` backup tells you the export *was* patched, but not which
shader hash was patched, with what scope, or whether the five ``edited_*.dxil`` files
are five independent edits or one ``--scope shader`` edit across five sibling PSOs.

The ledger is a JSON file (``editledger.json``) in the export directory. It is
intentionally not a database: the data is small, the queries are linear scans, and
the file is human-readable so a developer can inspect it beside the C++ project.

Design decisions worth stating:

  * **Group entries**: a ``--scope shader`` patch that touches N PSOs creates one
    group record + N PSO records. The group record carries the shader hash, stage,
    scope, source file, and compile args; each PSO record carries its pso_id and
    bytecode file. This mirrors how the caller thinks ("I patched the lighting CS")
    rather than how the filesystem stores it (N separate .dxil files).

  * **Idempotent add**: re-patching the same (pso, stage) with --force updates the
    existing record instead of creating a duplicate, so the ledger stays accurate
    even after multiple round-trips.

  * **Reset returns what it did**: the reset method returns a list of actions taken
    (files deleted, backups restored) so the caller can report them without
    re-scanning the filesystem.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


_LEDGER_FILE = "editledger.json"


class EditLedger:
    """Durable record of shader-edit-apply patches in an export directory.

    The ledger is loaded from ``editledger.json`` on construction and saved on
    every mutation. It is safe to construct multiple instances over the same
    directory (each re-reads the file), but concurrent writes are not guarded
    — the tool API is sequential, and a lock file would be over-engineering for
    a single-user debugging session.
    """

    def __init__(self, export_dir: Path | str):
        self._dir = Path(export_dir)
        self._path = self._dir / _LEDGER_FILE
        self._entries: list[dict[str, Any]] = []
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._entries = json.loads(
                    self._path.read_text(encoding="utf-8")
                ).get("entries", [])
            except (json.JSONDecodeError, KeyError):
                self._entries = []

    def _save(self) -> None:
        data = {
            "version": 1,
            "entries": self._entries,
            "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self._path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def list_entries(self) -> list[dict[str, Any]]:
        """All patch entries, grouped by group_id then ordered by timestamp."""
        return list(self._entries)

    @property
    def is_empty(self) -> bool:
        return len(self._entries) == 0

    @property
    def count(self) -> int:
        return len(self._entries)

    def groups(self) -> dict[str, list[dict[str, Any]]]:
        """Entries grouped by group_id, for the 'what did I change?' view."""
        out: dict[str, list[dict[str, Any]]] = {}
        for entry in self._entries:
            gid = entry.get("group_id", entry.get("id", "?"))
            out.setdefault(gid, []).append(entry)
        return out

    def find(self, pso_id, stage: str) -> dict[str, Any] | None:
        """Find the entry for a specific (pso_id, stage), if any."""
        pso_str = str(pso_id)
        for entry in self._entries:
            if str(entry.get("pso_id")) == pso_str and entry.get("stage") == stage:
                return entry
        return None

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def add_group(
        self,
        *,
        stage: str,
        shader_hash: str,
        scope: str,
        target_psos: list[int],
        source_file: str = "",
        compile_args_file: str = "",
        bytecode_files: dict[int, str] | None = None,
        binding_check: dict[str, Any] | None = None,
    ) -> str:
        """Record a patch group (one or more PSO patches sharing the same shader).

        For ``--scope pso`` (single PSO), this creates one entry.
        For ``--scope shader`` (multiple PSOs), this creates N entries sharing
        a group_id.

        If an entry for a (pso_id, stage) already exists, it is updated in place
        rather than duplicated — this is what makes --force re-patches accurate.

        Returns the group_id.
        """
        group_id = f"edit-{int(time.time())}-{len(self._entries):03d}"
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        bytecode_files = bytecode_files or {}
        binding_check = binding_check or {}

        for pso_id in target_psos:
            pso_str = str(pso_id)
            entry = {
                "id": f"{group_id}-{pso_str}",
                "group_id": group_id,
                "pso_id": pso_str,
                "stage": stage,
                "shader_hash": shader_hash,
                "scope": scope,
                "timestamp": timestamp,
                "source_file": source_file,
                "compile_args_file": compile_args_file,
                "bytecode_file": bytecode_files.get(pso_id, ""),
                "binding_check": binding_check,
            }

            # Idempotent: update existing entry for this (pso, stage) if present.
            existing_idx = None
            for i, e in enumerate(self._entries):
                if str(e.get("pso_id")) == pso_str and e.get("stage") == stage:
                    existing_idx = i
                    break

            if existing_idx is not None:
                # Preserve the original timestamp and id; update everything else.
                entry["timestamp"] = self._entries[existing_idx].get("timestamp", timestamp)
                entry["id"] = self._entries[existing_idx].get("id", entry["id"])
                self._entries[existing_idx] = entry
            else:
                self._entries.append(entry)

        self._save()
        return group_id

    def clear(self) -> None:
        """Remove all entries from the ledger."""
        self._entries = []
        self._save()

    def add_experiment(
        self,
        *,
        experiment_id: str,
        label: str = "",
        overrides: list[str] | None = None,
        scope: str = "",
        pso_id: int | None = None,
        files_touched: list[str] | None = None,
        changes: list[dict[str, Any]] | None = None,
    ) -> str:
        """Record one replay-override experiment in the same ledger.

        Overrides carry no bytecode files, so ``reset`` leaves the files to
        ``override.restore_overrides``; the entry exists so replay-edits can
        answer "what did I change?" for state overrides too.
        """
        entry = {
            "id": f"override-{experiment_id}",
            "kind": "override",
            "experiment_id": experiment_id,
            "label": label,
            "overrides": list(overrides or []),
            "scope": scope,
            "pso_id": str(pso_id) if pso_id is not None else "",
            "files_touched": list(files_touched or []),
            "changes": list(changes or []),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self._entries.append(entry)
        self._save()
        return entry["id"]

    def reset(self) -> list[dict[str, Any]]:
        """Revert all patches: delete bytecode files and return what was done.

        This does NOT restore CreatePSOs.cpp from .orig — that is done by the
        caller (replay-reset), because the .orig backup is a single file shared
        across all patches and restoring it is a one-shot operation, not per-entry.

        Returns a list of action records describing what was deleted.
        """
        actions: list[dict[str, Any]] = []
        for entry in self._entries:
            bc_file = entry.get("bytecode_file", "")
            if bc_file:
                path = Path(bc_file)
                if path.is_absolute():
                    path = self._dir / path.name
                if path.exists():
                    path.unlink()
                    actions.append({
                        "action": "deleted",
                        "file": str(path),
                        "pso_id": entry.get("pso_id"),
                        "stage": entry.get("stage"),
                    })
        self.clear()
        return actions

    # ------------------------------------------------------------------
    # Checkpoints — saved diff results for cross-edit comparison
    # ------------------------------------------------------------------

    def add_checkpoint(
        self,
        name: str,
        comparison: dict[str, Any],
        before_stats: dict[str, Any] | None = None,
        after_stats: dict[str, Any] | None = None,
        patch_info: dict[str, Any] | None = None,
        files: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Save a diff result as a named checkpoint for later comparison.

        Checkpoints let you compare how the diff changes across multiple incremental
        shader edits: edit v1 → diff --checkpoint v1, edit v2 → diff --checkpoint v2,
        then compare v1 and v2 to see what the second edit changed.
        """
        ledger_data = self._read_full()
        checkpoints = ledger_data.setdefault("checkpoints", [])

        checkpoint = {
            "name": name,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "comparison": comparison,
            "before_stats": before_stats or {},
            "after_stats": after_stats or {},
            "patch": patch_info or {},
            "files": files or [],
        }

        # Replace if a checkpoint with the same name exists.
        existing_idx = next(
            (i for i, c in enumerate(checkpoints) if c.get("name") == name),
            None,
        )
        if existing_idx is not None:
            checkpoint["timestamp"] = checkpoints[existing_idx].get("timestamp", checkpoint["timestamp"])
            checkpoints[existing_idx] = checkpoint
        else:
            checkpoints.append(checkpoint)

        self._write_full(ledger_data)
        return checkpoint

    def list_checkpoints(self) -> list[dict[str, Any]]:
        """All saved checkpoints (name, timestamp, changed pixel count)."""
        ledger_data = self._read_full()
        checkpoints = ledger_data.get("checkpoints", [])
        return [
            {
                "name": c.get("name", "?"),
                "timestamp": c.get("timestamp", ""),
                "changed_pixels": (c.get("comparison") or {}).get("changed_pixels", 0),
                "changed_share_percent": (c.get("comparison") or {}).get("changed_share_percent", 0.0),
            }
            for c in checkpoints
        ]

    def get_checkpoint(self, name: str) -> dict[str, Any] | None:
        """Retrieve a full checkpoint by name."""
        ledger_data = self._read_full()
        for c in ledger_data.get("checkpoints", []):
            if c.get("name") == name:
                return c
        return None

    def compare_checkpoints(self, name_a: str, name_b: str) -> dict[str, Any]:
        """Compare two checkpoints and return the delta."""
        a = self.get_checkpoint(name_a)
        b = self.get_checkpoint(name_b)
        if a is None:
            return {"error": f"checkpoint {name_a!r} not found"}
        if b is None:
            return {"error": f"checkpoint {name_b!r} not found"}

        comp_a = a.get("comparison") or {}
        comp_b = b.get("comparison") or {}
        return {
            "a": name_a,
            "b": name_b,
            "a_changed_pixels": comp_a.get("changed_pixels", 0),
            "b_changed_pixels": comp_b.get("changed_pixels", 0),
            "delta_changed_pixels": comp_b.get("changed_pixels", 0) - comp_a.get("changed_pixels", 0),
            "a_changed_share": comp_a.get("changed_share_percent", 0.0),
            "b_changed_share": comp_b.get("changed_share_percent", 0.0),
            "delta_changed_share": round(
                comp_b.get("changed_share_percent", 0.0) - comp_a.get("changed_share_percent", 0.0), 2
            ),
            "a_mean_delta": (comp_a.get("mean_abs_delta_8bit") or {}),
            "b_mean_delta": (comp_b.get("mean_abs_delta_8bit") or {}),
            "a_timestamp": a.get("timestamp", ""),
            "b_timestamp": b.get("timestamp", ""),
        }

    # ------------------------------------------------------------------
    # Internal: full-file read/write (entries + checkpoints)
    # ------------------------------------------------------------------

    def _read_full(self) -> dict[str, Any]:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, KeyError):
                pass
        return {"version": 1, "entries": [], "checkpoints": []}

    def _write_full(self, data: dict[str, Any]) -> None:
        data["version"] = 1
        data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self._path),
            "entries": self._entries,
            "count": len(self._entries),
            "groups": len(self.groups()),
        }
