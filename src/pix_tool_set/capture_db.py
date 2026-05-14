from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .errors import PixToolError

DATABASE_SCHEMA_VERSION = 1
DATABASE_FILENAME = "capture.sqlite"


def database_path(export_dir: str | Path) -> Path:
    return Path(export_dir).resolve() / ".cache" / "pix-tool-set" / DATABASE_FILENAME


def connect_database(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_loads(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _execute_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS events (
            global_id TEXT PRIMARY KEY,
            event_order INTEGER NOT NULL,
            name TEXT,
            event_type TEXT,
            is_shader_event INTEGER NOT NULL DEFAULT 0,
            shader_stage_group TEXT,
            file TEXT,
            line INTEGER,
            parent_global_id TEXT,
            marker_path_json TEXT NOT NULL DEFAULT '[]',
            pso_id TEXT,
            event_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_events_order ON events(event_order);
        CREATE INDEX IF NOT EXISTS idx_events_shader ON events(is_shader_event, event_order);
        CREATE INDEX IF NOT EXISTS idx_events_pso ON events(pso_id);

        CREATE TABLE IF NOT EXISTS resources (
            resource_id TEXT PRIMARY KEY,
            name TEXT,
            file TEXT,
            line INTEGER,
            dimension TEXT,
            format TEXT,
            size_bytes INTEGER,
            width INTEGER,
            height INTEGER,
            depth INTEGER,
            mip_count INTEGER,
            array_size INTEGER,
            diagnostics_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS idx_resources_name ON resources(name);

        CREATE TABLE IF NOT EXISTS resource_aliases (
            name TEXT NOT NULL,
            resource_id TEXT NOT NULL,
            PRIMARY KEY (name, resource_id)
        );

        CREATE INDEX IF NOT EXISTS idx_resource_aliases_resource ON resource_aliases(resource_id);

        CREATE TABLE IF NOT EXISTS resource_references (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            resource_id TEXT NOT NULL,
            global_id TEXT NOT NULL,
            event_order INTEGER NOT NULL,
            file TEXT,
            line INTEGER,
            text TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_resource_references_resource ON resource_references(resource_id, event_order, line);
        CREATE INDEX IF NOT EXISTS idx_resource_references_event ON resource_references(global_id);

        CREATE TABLE IF NOT EXISTS descriptor_writes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            descriptor_index TEXT NOT NULL,
            heap_id TEXT,
            resource_id TEXT,
            view_type TEXT,
            call TEXT,
            file TEXT,
            line INTEGER,
            write_order INTEGER NOT NULL,
            text TEXT,
            diagnostics_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS idx_descriptor_writes_descriptor ON descriptor_writes(descriptor_index, heap_id, write_order);
        CREATE INDEX IF NOT EXISTS idx_descriptor_writes_resource ON descriptor_writes(resource_id);

        CREATE TABLE IF NOT EXISTS root_bindings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            global_id TEXT NOT NULL,
            event_order INTEGER NOT NULL,
            binding_type TEXT NOT NULL,
            stage TEXT,
            root_index TEXT,
            descriptor_index TEXT,
            heap_id TEXT,
            resource_id TEXT,
            offset TEXT,
            line INTEGER,
            text TEXT,
            binding_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS idx_root_bindings_event ON root_bindings(global_id, binding_type, root_index);
        CREATE INDEX IF NOT EXISTS idx_root_bindings_resource ON root_bindings(resource_id);

        CREATE TABLE IF NOT EXISTS event_bound_resources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            global_id TEXT NOT NULL,
            event_order INTEGER NOT NULL,
            resource_id TEXT,
            resource_name TEXT,
            view_type TEXT,
            shader_stage TEXT,
            binding_name TEXT,
            root_index TEXT,
            descriptor_index TEXT,
            root_descriptor_index TEXT,
            binding_slot INTEGER,
            source TEXT NOT NULL,
            confidence REAL,
            diagnostics_json TEXT NOT NULL DEFAULT '{}',
            resource_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS idx_event_bound_resources_event ON event_bound_resources(global_id);
        CREATE INDEX IF NOT EXISTS idx_event_bound_resources_resource ON event_bound_resources(resource_id, event_order);
        CREATE INDEX IF NOT EXISTS idx_event_bound_resources_name ON event_bound_resources(resource_name, event_order);

        CREATE TABLE IF NOT EXISTS shader_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pso_id TEXT,
            stage TEXT,
            blob_path TEXT,
            blob_size INTEGER,
            format TEXT,
            debug_name TEXT,
            extraction_status TEXT,
            source_status TEXT,
            source_text TEXT,
            source_summary TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(pso_id, stage, blob_path)
        );

        CREATE INDEX IF NOT EXISTS idx_shader_metadata_pso ON shader_metadata(pso_id, stage);

        CREATE TABLE IF NOT EXISTS shader_bindings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pso_id TEXT,
            stage TEXT,
            binding_name TEXT,
            register_type TEXT,
            register_slot INTEGER,
            register_space INTEGER,
            view_type TEXT,
            resource_dimension TEXT,
            declaration_type TEXT,
            binding_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS idx_shader_bindings_pso ON shader_bindings(pso_id, stage);
        CREATE INDEX IF NOT EXISTS idx_shader_bindings_name ON shader_bindings(binding_name);
        """
    )


def initialize_database(path: str | Path) -> None:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect_database(db_path) as connection:
        _execute_schema(connection)


def _metadata(connection: sqlite3.Connection) -> dict[str, str]:
    try:
        rows = connection.execute("SELECT key, value FROM metadata").fetchall()
    except sqlite3.DatabaseError:
        return {}
    return {str(row["key"]): str(row["value"]) for row in rows}


def _set_metadata(connection: sqlite3.Connection, values: dict[str, Any]) -> None:
    connection.executemany(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
        [(key, _json_dumps(value) if not isinstance(value, str) else value) for key, value in values.items()],
    )


def _fingerprints_match(stored: str | None, fingerprints: list[dict[str, Any]]) -> bool:
    return _json_loads(stored, default=None) == fingerprints


def is_database_current(path: str | Path, fingerprints: list[dict[str, Any]]) -> bool:
    db_path = Path(path)
    if not db_path.exists():
        return False
    try:
        with connect_database(db_path) as connection:
            metadata = _metadata(connection)
    except sqlite3.DatabaseError:
        return False
    return metadata.get("schema_version") == str(DATABASE_SCHEMA_VERSION) and _fingerprints_match(metadata.get("fingerprints"), fingerprints)


def table_counts(path: str | Path) -> dict[str, int]:
    tables = [
        "events",
        "resources",
        "resource_aliases",
        "resource_references",
        "descriptor_writes",
        "root_bindings",
        "event_bound_resources",
        "shader_metadata",
        "shader_bindings",
    ]
    with connect_database(path) as connection:
        counts: dict[str, int] = {}
        for table in tables:
            counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    return counts


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _event_order_map(index: dict[str, Any]) -> dict[str, int]:
    return {str(event.get("global_id")): position for position, event in enumerate(index.get("events", []))}


def _resource_name(index: dict[str, Any], resource_id: Any) -> str | None:
    if resource_id is None:
        return None
    resource = index.get("resource_names", {}).get(str(resource_id), {})
    return resource.get("name")


def _latest_descriptor_write(index: dict[str, Any], descriptor_index: int, heap_id: Any = None, max_line: Any = None) -> dict[str, Any] | None:
    writes = list(index.get("descriptor_index", {}).get(str(descriptor_index), []))
    if heap_id is not None:
        writes = [write for write in writes if str(write.get("heap_id")) == str(heap_id)]
    if max_line is not None:
        max_line_int = _int_or_none(max_line)
        if max_line_int is not None:
            before = [write for write in writes if _int_or_none(write.get("line")) is not None and int(write.get("line")) <= max_line_int]
            if before:
                writes = before
    return writes[-1] if writes else None


def _resource_dimension_from_descriptor(write: dict[str, Any] | None) -> str | None:
    if not write:
        return None
    text = f"{write.get('call') or ''} {write.get('text') or ''}"
    if "Tex" in text or "TEXTURE" in text:
        return "Texture"
    if "Buffer" in text or "BUFFER" in text:
        return "Buffer"
    return None


def _insert_events(connection: sqlite3.Connection, index: dict[str, Any]) -> None:
    rows = []
    for order, event in enumerate(index.get("events", [])):
        rows.append(
            (
                str(event.get("global_id")),
                order,
                event.get("name"),
                event.get("event_type"),
                1 if event.get("is_shader_event") else 0,
                event.get("shader_stage_group"),
                event.get("file"),
                _int_or_none(event.get("line")),
                event.get("parent_global_id"),
                _json_dumps(event.get("marker_path") or []),
                event.get("pso_id"),
                _json_dumps(event),
            )
        )
    connection.executemany(
        """
        INSERT INTO events(global_id, event_order, name, event_type, is_shader_event, shader_stage_group, file, line,
                           parent_global_id, marker_path_json, pso_id, event_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def _insert_resources(connection: sqlite3.Connection, index: dict[str, Any]) -> None:
    rows = []
    aliases = []
    for resource_id, resource in index.get("resource_names", {}).items():
        name = (resource or {}).get("name")
        rows.append((str(resource_id), name, (resource or {}).get("file"), _int_or_none((resource or {}).get("line")), _json_dumps({"status": "partial", "reason": "Detailed metadata has not been parsed yet."})))
        if name:
            aliases.append((str(name), str(resource_id)))
    connection.executemany(
        """
        INSERT INTO resources(resource_id, name, file, line, diagnostics_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        rows,
    )
    connection.executemany("INSERT OR IGNORE INTO resource_aliases(name, resource_id) VALUES (?, ?)", aliases)


def _insert_resource_references(connection: sqlite3.Connection, index: dict[str, Any]) -> None:
    event_orders = _event_order_map(index)
    events_by_global_id = index.get("events_by_global_id", {})
    rows = []
    for resource_id, refs in index.get("resource_refs_by_resource_id", {}).items():
        for ref in refs:
            global_id = str(ref.get("global_id"))
            event = events_by_global_id.get(global_id, {})
            rows.append(
                (
                    str(resource_id),
                    global_id,
                    event_orders.get(global_id, -1),
                    event.get("file"),
                    _int_or_none(ref.get("line")),
                    str(ref.get("text") or ""),
                )
            )
    connection.executemany(
        """
        INSERT INTO resource_references(resource_id, global_id, event_order, file, line, text)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def _insert_descriptor_writes(connection: sqlite3.Connection, index: dict[str, Any]) -> None:
    rows = []
    write_order = 0
    for descriptor_index in sorted(index.get("descriptor_index", {}), key=lambda value: int(value) if str(value).isdigit() else str(value)):
        for write in index.get("descriptor_index", {}).get(str(descriptor_index), []):
            rows.append(
                (
                    str(write.get("descriptor_index") or descriptor_index),
                    write.get("heap_id"),
                    write.get("resource_id"),
                    write.get("view_type"),
                    write.get("call"),
                    write.get("file"),
                    _int_or_none(write.get("line")),
                    write_order,
                    write.get("text"),
                    _json_dumps({} if write.get("resource_id") else {"status": "partial", "reason": "Descriptor write has no resolved resource id."}),
                )
            )
            write_order += 1
    connection.executemany(
        """
        INSERT INTO descriptor_writes(descriptor_index, heap_id, resource_id, view_type, call, file, line, write_order, text, diagnostics_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def _insert_root_bindings(connection: sqlite3.Connection, index: dict[str, Any]) -> None:
    rows = []
    for event_order, event in enumerate(index.get("events", [])):
        global_id = str(event.get("global_id"))
        for binding in (event.get("root_descriptor_tables") or {}).values():
            rows.append(
                (
                    global_id,
                    event_order,
                    "descriptor_table",
                    binding.get("stage"),
                    binding.get("root_index"),
                    binding.get("descriptor_index"),
                    binding.get("heap_id"),
                    None,
                    None,
                    _int_or_none(binding.get("line")),
                    binding.get("text"),
                    _json_dumps(binding),
                )
            )
        for binding in (event.get("root_constant_buffer_views") or {}).values():
            rows.append(
                (
                    global_id,
                    event_order,
                    "root_cbv",
                    binding.get("stage"),
                    binding.get("root_index"),
                    None,
                    None,
                    binding.get("resource_id"),
                    binding.get("offset"),
                    _int_or_none(binding.get("line")),
                    binding.get("text"),
                    _json_dumps(binding),
                )
            )
    connection.executemany(
        """
        INSERT INTO root_bindings(global_id, event_order, binding_type, stage, root_index, descriptor_index, heap_id,
                                  resource_id, offset, line, text, binding_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def _event_bound_resource_key(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        item.get("global_id"),
        item.get("resource_id"),
        item.get("view_type"),
        item.get("shader_stage"),
        item.get("root_index"),
        item.get("descriptor_index"),
        item.get("source"),
    )


def _append_event_bound_resource(rows: list[dict[str, Any]], index: dict[str, Any], event: dict[str, Any], event_order: int, item: dict[str, Any]) -> None:
    resource_id = str(item.get("resource_id")) if item.get("resource_id") is not None else None
    resource_name = item.get("resource_name") or _resource_name(index, resource_id)
    rows.append(
        {
            "global_id": str(event.get("global_id")),
            "event_order": event_order,
            "resource_id": resource_id,
            "resource_name": resource_name,
            "view_type": item.get("view_type"),
            "shader_stage": item.get("shader_stage") or item.get("stage"),
            "binding_name": item.get("binding_name") or item.get("shader_binding_name"),
            "root_index": item.get("root_index"),
            "descriptor_index": item.get("descriptor_index"),
            "root_descriptor_index": item.get("root_descriptor_index"),
            "binding_slot": _int_or_none(item.get("binding_slot") if item.get("binding_slot") is not None else item.get("shader_binding_slot")),
            "source": item.get("source") or "precomputed",
            "confidence": item.get("confidence"),
            "diagnostics_json": _json_dumps(item.get("diagnostics") or {}),
            "resource_json": _json_dumps(item),
        }
    )


def _precompute_event_bound_resources(index: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for event_order, event in enumerate(index.get("events", [])):
        global_id = str(event.get("global_id"))
        ia = event.get("input_assembler") or {}
        for vertex_buffer in ia.get("vertex_buffers") or []:
            item = dict(vertex_buffer, view_type="VB", source="input_assembler", shader_stage="IA", confidence=1.0)
            _append_event_bound_resource(rows, index, event, event_order, item)
        if ia.get("index_buffer"):
            item = dict(ia["index_buffer"], view_type="IB", source="input_assembler", shader_stage="IA", confidence=1.0)
            _append_event_bound_resource(rows, index, event, event_order, item)

        om = event.get("output_merger") or {}
        for render_target in om.get("render_targets") or []:
            item = dict(render_target, view_type="RTV", source="output_merger", shader_stage="OM", confidence=1.0)
            _append_event_bound_resource(rows, index, event, event_order, item)
        if om.get("depth_stencil"):
            for view_type in ("Depth", "Stencil"):
                item = dict(om["depth_stencil"], view_type=view_type, source="output_merger", shader_stage="OM", confidence=1.0)
                _append_event_bound_resource(rows, index, event, event_order, item)

        for root_binding in (event.get("root_constant_buffer_views") or {}).values():
            item = dict(
                root_binding,
                view_type="CBV",
                source="root_cbv",
                shader_stage=root_binding.get("stage"),
                confidence=0.75,
                diagnostics={"status": "partial", "reason": "Shader binding name is not available during index build."},
            )
            _append_event_bound_resource(rows, index, event, event_order, item)

        for root_binding in (event.get("root_descriptor_tables") or {}).values():
            start = _int_or_none(root_binding.get("descriptor_index"))
            if start is None:
                continue
            for descriptor_index in range(start, start + 32):
                write = _latest_descriptor_write(index, descriptor_index, root_binding.get("heap_id"), root_binding.get("line"))
                if write is None or write.get("view_type") not in {"SRV", "UAV"}:
                    continue
                item = {
                    "resource_id": write.get("resource_id"),
                    "resource_name": _resource_name(index, write.get("resource_id")),
                    "view_type": write.get("view_type"),
                    "shader_stage": root_binding.get("stage"),
                    "root_index": root_binding.get("root_index"),
                    "root_descriptor_index": root_binding.get("descriptor_index"),
                    "descriptor_index": str(descriptor_index),
                    "binding_slot": descriptor_index - start,
                    "source": "descriptor_table_scan",
                    "confidence": 0.5,
                    "resource_dimension": _resource_dimension_from_descriptor(write),
                    "descriptor_write": write,
                    "root_binding": root_binding,
                    "diagnostics": {"scan_count": 32, "status": "partial", "reason": "Shader declaration matching is resolved lazily by resource query tools."},
                }
                _append_event_bound_resource(rows, index, event, event_order, item)

    unique_rows: list[dict[str, Any]] = []
    for row in rows:
        key = _event_bound_resource_key(row)
        if key in seen:
            continue
        seen.add(key)
        unique_rows.append(row)
    return unique_rows


def insert_event_bound_resources(connection: sqlite3.Connection, resources: Iterable[dict[str, Any]]) -> None:
    rows = [
        (
            item.get("global_id"),
            _int_or_none(item.get("event_order")) or 0,
            item.get("resource_id"),
            item.get("resource_name"),
            item.get("view_type"),
            item.get("shader_stage"),
            item.get("binding_name"),
            item.get("root_index"),
            item.get("descriptor_index"),
            item.get("root_descriptor_index"),
            _int_or_none(item.get("binding_slot")),
            item.get("source") or "runtime",
            item.get("confidence"),
            _json_dumps(item.get("diagnostics") or _json_loads(item.get("diagnostics_json"), {})),
            _json_dumps(item.get("resource_json") if isinstance(item.get("resource_json"), dict) else item),
        )
        for item in resources
    ]
    connection.executemany(
        """
        INSERT INTO event_bound_resources(global_id, event_order, resource_id, resource_name, view_type, shader_stage,
                                          binding_name, root_index, descriptor_index, root_descriptor_index, binding_slot,
                                          source, confidence, diagnostics_json, resource_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def replace_event_bound_resources(path: str | Path, global_id: str | int, resources: list[dict[str, Any]]) -> None:
    with connect_database(path) as connection:
        connection.execute("DELETE FROM event_bound_resources WHERE global_id = ?", (str(global_id),))
        insert_event_bound_resources(connection, resources)


def _insert_event_bound_resources(connection: sqlite3.Connection, index: dict[str, Any]) -> None:
    insert_event_bound_resources(connection, _precompute_event_bound_resources(index))


def _insert_shader_metadata(connection: sqlite3.Connection, index: dict[str, Any]) -> None:
    rows = []
    for pso_id, pso in index.get("pso_index", {}).items():
        for stage in (pso or {}).get("stages", []):
            blob_path = stage.get("blob_path")
            blob_size = None
            if blob_path:
                try:
                    blob_size = Path(blob_path).stat().st_size
                except OSError:
                    blob_size = None
            rows.append(
                (
                    str(pso_id),
                    stage.get("stage"),
                    blob_path,
                    blob_size,
                    stage.get("format"),
                    stage.get("debug_name"),
                    "indexed",
                    "unresolved",
                    None,
                    None,
                    _json_dumps(stage),
                )
            )
    connection.executemany(
        """
        INSERT OR IGNORE INTO shader_metadata(pso_id, stage, blob_path, blob_size, format, debug_name,
                                              extraction_status, source_status, source_text, source_summary, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def _populate_database(connection: sqlite3.Connection, index: dict[str, Any]) -> None:
    _execute_schema(connection)
    _insert_events(connection, index)
    _insert_resources(connection, index)
    _insert_resource_references(connection, index)
    _insert_descriptor_writes(connection, index)
    _insert_root_bindings(connection, index)
    _insert_event_bound_resources(connection, index)
    _insert_shader_metadata(connection, index)
    _set_metadata(
        connection,
        {
            "schema_version": str(DATABASE_SCHEMA_VERSION),
            "export_dir": str(index.get("export_dir") or ""),
            "fingerprints": index.get("fingerprints") or [],
            "index_version": index.get("version"),
            "built_utc": datetime.now(timezone.utc).isoformat(),
        },
    )


def build_capture_database(export_dir: str | Path, index: dict[str, Any], refresh: bool = False) -> dict[str, Any]:
    root = Path(export_dir).resolve()
    if not root.exists():
        raise PixToolError(code="export_dir_not_found", message=f"Export directory does not exist: {root}", stage="capture_database", paths=[str(root)])
    db_path = database_path(root)
    fingerprints = list(index.get("fingerprints") or [])
    if not refresh and is_database_current(db_path, fingerprints):
        return {
            "database_path": str(db_path),
            "cache_hit": True,
            "schema_version": DATABASE_SCHEMA_VERSION,
            "table_counts": table_counts(db_path),
            "diagnostics": {"database_hit": True, "query_mode": "sqlite", "reason": None},
        }

    db_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = db_path.with_suffix(".sqlite.tmp")
    if temp_path.exists():
        temp_path.unlink()
    try:
        connection = connect_database(temp_path)
        try:
            _populate_database(connection, index)
            connection.commit()
        finally:
            connection.close()
        os.replace(temp_path, db_path)
    except Exception as exc:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise PixToolError(
            code="capture_database_build_failed",
            message=f"Failed to build capture database: {exc}",
            stage="capture_database",
            paths=[str(db_path)],
            details={"error": str(exc)},
        ) from exc

    return {
        "database_path": str(db_path),
        "cache_hit": False,
        "schema_version": DATABASE_SCHEMA_VERSION,
        "table_counts": table_counts(db_path),
        "diagnostics": {"database_hit": True, "query_mode": "sqlite", "reason": None},
    }


def load_event(path: str | Path, global_id: str | int) -> dict[str, Any] | None:
    with connect_database(path) as connection:
        row = connection.execute("SELECT event_json FROM events WHERE global_id = ?", (str(global_id),)).fetchone()
    if row is None:
        return None
    return _json_loads(row["event_json"], {})


def load_event_bound_resources(path: str | Path, global_id: str | int) -> list[dict[str, Any]]:
    with connect_database(path) as connection:
        rows = connection.execute(
            "SELECT * FROM event_bound_resources WHERE global_id = ? ORDER BY id",
            (str(global_id),),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        payload = _json_loads(row["resource_json"], {}) or {}
        if not isinstance(payload, dict):
            payload = {}
        payload.setdefault("resource_id", row["resource_id"])
        payload.setdefault("resource_name", row["resource_name"])
        payload.setdefault("view_type", row["view_type"])
        payload.setdefault("stage", row["shader_stage"])
        payload.setdefault("shader_binding_name", row["binding_name"])
        payload.setdefault("root_index", row["root_index"])
        payload.setdefault("descriptor_index", row["descriptor_index"])
        payload.setdefault("root_descriptor_index", row["root_descriptor_index"])
        payload.setdefault("shader_binding_slot", row["binding_slot"])
        payload.setdefault("database_source", row["source"])
        result.append(payload)
    return result


def load_same_named_resource_ids(path: str | Path, resource_name: str | None, resource_id: str | int | None = None) -> set[str]:
    ids: set[str] = {str(resource_id)} if resource_id is not None and str(resource_id) else set()
    if not resource_name:
        return ids
    with connect_database(path) as connection:
        rows = connection.execute("SELECT resource_id FROM resource_aliases WHERE name = ?", (str(resource_name),)).fetchall()
    ids.update(str(row["resource_id"]) for row in rows)
    return ids


def load_resource_references(path: str | Path, resource_ids: Iterable[str | int]) -> list[dict[str, Any]]:
    ids = [str(resource_id) for resource_id in resource_ids]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    with connect_database(path) as connection:
        rows = connection.execute(
            f"""
            SELECT rr.*, e.event_json
            FROM resource_references rr
            LEFT JOIN events e ON e.global_id = rr.global_id
            WHERE rr.resource_id IN ({placeholders})
            ORDER BY rr.event_order, rr.line
            """,
            ids,
        ).fetchall()
    return [
        {
            "resource_id": str(row["resource_id"]),
            "global_id": str(row["global_id"]),
            "event_order": int(row["event_order"]),
            "file": row["file"],
            "line": row["line"],
            "text": row["text"],
            "event": _json_loads(row["event_json"], {}) or {},
        }
        for row in rows
    ]


def load_resource_shader_accesses(path: str | Path, resource_ids: Iterable[str | int], view_types: set[str] | None = None) -> list[dict[str, Any]]:
    ids = [str(resource_id) for resource_id in resource_ids]
    if not ids:
        return []
    view_types = view_types or {"SRV", "UAV"}
    placeholders = ",".join("?" for _ in ids)
    view_placeholders = ",".join("?" for _ in view_types)
    with connect_database(path) as connection:
        rows = connection.execute(
            f"""
            SELECT ebr.*, e.event_json
            FROM event_bound_resources ebr
            LEFT JOIN events e ON e.global_id = ebr.global_id
            WHERE ebr.resource_id IN ({placeholders}) AND ebr.view_type IN ({view_placeholders})
            ORDER BY ebr.event_order, ebr.id
            """,
            [*ids, *sorted(view_types)],
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        resource = _json_loads(row["resource_json"], {}) or {}
        if not isinstance(resource, dict):
            resource = {}
        resource.setdefault("resource_id", row["resource_id"])
        resource.setdefault("resource_name", row["resource_name"])
        resource.setdefault("view_type", row["view_type"])
        resource.setdefault("stage", row["shader_stage"])
        resource.setdefault("shader_binding_name", row["binding_name"])
        resource.setdefault("shader_binding_slot", row["binding_slot"])
        resource.setdefault("descriptor_index", row["descriptor_index"])
        resource.setdefault("root_descriptor_index", row["root_descriptor_index"])
        result.append({"event": _json_loads(row["event_json"], {}) or {}, "resource": resource})
    return result


def load_shader_source_cache(path: str | Path, pso_id: str | int | None) -> list[dict[str, Any]]:
    if pso_id is None:
        return []
    with connect_database(path) as connection:
        rows = connection.execute(
            """
            SELECT * FROM shader_metadata
            WHERE pso_id = ? AND source_status = 'resolved' AND source_text IS NOT NULL
            ORDER BY stage, id
            """,
            (str(pso_id),),
        ).fetchall()
    stages: list[dict[str, Any]] = []
    for row in rows:
        metadata = _json_loads(row["metadata_json"], {}) or {}
        if not isinstance(metadata, dict):
            metadata = {}
        source_text = row["source_text"]
        stage = dict(metadata)
        stage.update(
            {
                "stage": row["stage"],
                "blob_path": row["blob_path"],
                "blob_size": row["blob_size"],
                "format": row["format"],
                "debug_name": row["debug_name"],
                "resolver_result": {
                    "status": "cached",
                    "result": {"sources": [{"content": source_text}]},
                },
            }
        )
        stages.append(stage)
    return stages


def store_shader_source_cache(path: str | Path, pso_id: str | int | None, stages: list[dict[str, Any]]) -> None:
    if pso_id is None:
        return
    rows = []
    for stage in stages:
        resolver_result = stage.get("resolver_result", {}) or {}
        result = resolver_result.get("result") or {}
        source_chunks = []
        for source in result.get("sources", []) if isinstance(result, dict) else []:
            content = source.get("content") if isinstance(source, dict) else None
            if content:
                source_chunks.append(str(content))
        if not source_chunks:
            continue
        source_text = "\n".join(source_chunks)
        rows.append(
            (
                str(pso_id),
                stage.get("stage"),
                stage.get("blob_path"),
                stage.get("blob_size"),
                stage.get("format"),
                stage.get("debug_name"),
                stage.get("extraction_status") or "indexed",
                "resolved",
                source_text,
                source_text[:512],
                _json_dumps(stage),
            )
        )
    if not rows:
        return
    with connect_database(path) as connection:
        connection.executemany(
            """
            INSERT INTO shader_metadata(pso_id, stage, blob_path, blob_size, format, debug_name,
                                        extraction_status, source_status, source_text, source_summary, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(pso_id, stage, blob_path) DO UPDATE SET
                blob_size = excluded.blob_size,
                format = excluded.format,
                debug_name = excluded.debug_name,
                extraction_status = excluded.extraction_status,
                source_status = excluded.source_status,
                source_text = excluded.source_text,
                source_summary = excluded.source_summary,
                metadata_json = excluded.metadata_json
            """,
            rows,
        )
