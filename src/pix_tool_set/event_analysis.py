from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


def _sort_key(value: str) -> tuple[int, int | str]:
    if value.isdigit():
        return (0, int(value))
    return (1, value)


def _ordered_counts(counter: Counter[str], limit: int | None = None) -> list[dict[str, Any]]:
    items = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    if limit is not None and limit > 0:
        items = items[:limit]
    return [{"name": name, "count": count} for name, count in items]


def analyze_shader_event_tree_payload(
    tree: list[dict[str, Any]],
    *,
    metadata: dict[str, Any] | None = None,
    top_limit: int | None = 20,
    sample_limit: int | None = 20,
) -> dict[str, Any]:
    event_types: Counter[str] = Counter()
    shader_stages: Counter[str] = Counter()
    pso_ids: set[str] = set()
    marker_paths: set[str] = set()
    total_nodes = 0
    shader_events = 0
    max_depth = 0

    def traverse(node: dict[str, Any], depth: int) -> None:
        nonlocal total_nodes, shader_events, max_depth
        total_nodes += 1
        max_depth = max(max_depth, depth)

        event_type = str(node.get("event_type") or "Unknown")
        event_types[event_type] += 1

        marker_path_items = [str(item) for item in node.get("marker_path", [])]
        marker_path = " > ".join(marker_path_items)
        if marker_path:
            marker_paths.add(marker_path)

        if bool(node.get("is_shader_event")):
            shader_events += 1
            shader_stage = str(node.get("shader_stage_group") or "unknown")
            shader_stages[shader_stage] += 1
            pso_id = node.get("pso_id")
            if pso_id is not None:
                pso_ids.add(str(pso_id))

        for child in node.get("children", []):
            traverse(child, depth + 1)

    for root in tree:
        traverse(root, 1)

    pso_examples = sorted(pso_ids, key=_sort_key)
    marker_path_examples = sorted(marker_paths)
    if sample_limit is not None and sample_limit > 0:
        pso_examples = pso_examples[:sample_limit]
        marker_path_examples = marker_path_examples[:sample_limit]

    return {
        "metadata": metadata or {},
        "summary": {
            "root_count": len(tree),
            "total_nodes": total_nodes,
            "shader_event_count": shader_events,
            "unique_pso_count": len(pso_ids),
            "unique_marker_path_count": len(marker_paths),
            "max_depth": max_depth,
        },
        "event_types": _ordered_counts(event_types, top_limit),
        "shader_stage_groups": _ordered_counts(shader_stages, top_limit),
        "examples": {
            "pso_ids": pso_examples,
            "marker_paths": marker_path_examples,
        },
    }


def analyze_shader_event_tree_file(
    input_path: str | Path,
    *,
    top_limit: int | None = 20,
    sample_limit: int | None = 20,
) -> dict[str, Any]:
    path = Path(input_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    return analyze_shader_event_tree_payload(
        data.get("tree", []),
        metadata=data.get("metadata", {}),
        top_limit=top_limit,
        sample_limit=sample_limit,
    )


def write_event_analysis(analysis: dict[str, Any], output_path: str | Path) -> str:
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(analysis, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return str(out.resolve())
