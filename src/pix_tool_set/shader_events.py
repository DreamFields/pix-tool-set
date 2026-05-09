from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .indexer import build_index


def _node_payload(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "global_id": event.get("global_id"),
        "parent_global_id": event.get("parent_global_id"),
        "name": event.get("name"),
        "event_type": event.get("event_type"),
        "is_shader_event": bool(event.get("is_shader_event")),
        "shader_stage_group": event.get("shader_stage_group"),
        "pso_id": event.get("pso_id"),
        "file": event.get("file"),
        "line": event.get("line"),
        "marker_path": event.get("marker_path", []),
        "children": [],
    }


def build_shader_event_tree(export_dir: str | Path, refresh: bool = False) -> dict[str, Any]:
    index = build_index(export_dir, refresh=refresh)
    events_by_gid = index["events_by_global_id"]
    retained: set[str] = set()

    for global_id in index["shader_event_global_ids"]:
        current = events_by_gid.get(str(global_id))
        visited: set[str] = set()
        while current is not None and str(current.get("global_id")) not in visited:
            gid = str(current.get("global_id"))
            retained.add(gid)
            visited.add(gid)
            parent = current.get("parent_global_id")
            current = events_by_gid.get(str(parent)) if parent else None

    nodes = {gid: _node_payload(events_by_gid[gid]) for gid in retained if gid in events_by_gid}
    roots: list[dict[str, Any]] = []
    for gid, node in nodes.items():
        parent = node.get("parent_global_id")
        if parent and str(parent) in nodes:
            nodes[str(parent)]["children"].append(node)
        else:
            roots.append(node)

    def sort_tree(items: list[dict[str, Any]]) -> None:
        items.sort(key=lambda item: int(item.get("global_id") or 0))
        for item in items:
            sort_tree(item["children"])

    sort_tree(roots)
    return {
        "tree": roots,
        "metadata": {
            "export_dir": index["export_dir"],
            "total_events": index["diagnostics"]["event_count"],
            "shader_event_count": index["diagnostics"]["shader_event_count"],
            "retained_tree_node_count": len(nodes),
            "cache_hit": index.get("cache_hit", False),
        },
    }


def write_shader_event_tree(export_dir: str | Path, output_path: str | Path | None = None, refresh: bool = False) -> dict[str, Any]:
    payload = build_shader_event_tree(export_dir, refresh=refresh)
    out = Path(output_path) if output_path else Path(export_dir) / "shader_events_tree.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"tree": payload["tree"]}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    payload["output_path"] = str(out.resolve())
    return payload
