from __future__ import annotations

from pathlib import Path
from typing import Any
import re

from .capture_db import (
    load_event_bound_resources,
    load_resource_references,
    load_resource_shader_accesses,
    load_same_named_resource_ids,
    replace_event_bound_resources,
)
from .errors import PixToolError
from .indexer import build_index
from .shader_source import get_event_shader_source


DEFAULT_DESCRIPTOR_SCAN_COUNT = 8
RESOURCE_ID_RE = re.compile(r"GetResource\((\d+)\)")
TRANSITION_RE = re.compile(
    r"Transition\(GetResource\((?P<resource_id>\d+)\)\.Get\(\),\s*"
    r"(?P<before>.*?),\s*"
    r"(?P<after>.*?),\s*"
    r"(?P<subresource>\d+)\s*,"
)
CBUFFER_DECL_RE = re.compile(r"\bcbuffer\s+(?P<name>\w+)(?:\s*:\s*register\(b(?P<slot>\d+)\))?")
RESOURCE_DECL_RE = re.compile(
    r"\b(?P<type>RW(?:StructuredBuffer|Texture\w*|Buffer)|(?:ByteAddressBuffer|StructuredBuffer|Texture\w*|Buffer))(?:\s+|\s*(?=<))"
    r"(?:<[^;]+>\s*)?(?P<name>\w+)\s*(?:\[[^;]+\])?\s*"
    r"(?::\s*register\((?P<register_type>[tu])(?P<register_slot>\d+)(?:\s*,\s*space(?P<register_space>\d+))?\))?\s*;"
)
SAMPLER_DECL_RE = re.compile(
    r"\b(?P<type>Sampler(?:State|ComparisonState))\s+(?P<name>\w+)\s*"
    r":\s*register\(s(?P<slot>\d+)(?:\s*,\s*space(?P<space>\d+))?\)\s*;"
)
SAMPLER_NO_REGISTER_DECL_RE = re.compile(r"\b(?P<type>Sampler(?:State|ComparisonState))\s+(?P<name>\w+)\s*;")
IDENTIFIER_RE = re.compile(r"\b[_A-Za-z]\w*\b")
GENERIC_RESOURCE_NAME_PARTS = {"resource", "allocator", "pool", "underlying", "buffer", "dummy", "default"}


def _resource_dimension(resource_type: str) -> str:
    return "Texture" if "Texture" in resource_type else "Buffer"


def _descriptor_dimension(write: dict[str, Any] | None) -> str | None:
    if not write:
        return None
    text = f"{write.get('call') or ''} {write.get('text') or ''}"
    if "Tex" in text or "TEXTURE" in text:
        return "Texture"
    if "Buffer" in text or "BUFFER" in text:
        return "Buffer"
    return None


def _source_without_cbuffer_bodies(source: str) -> str:
    result: list[str] = []
    position = 0
    for match in re.finditer(r"\bcbuffer\s+\w+(?:\s*:\s*register\(b\d+\))?\s*\{", source):
        result.append(source[position : match.end()])
        depth = 1
        cursor = match.end()
        while cursor < len(source) and depth > 0:
            char = source[cursor]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            cursor += 1
        result.append(" }")
        if cursor < len(source) and source[cursor] == ";":
            cursor += 1
        position = cursor
    result.append(source[position:])
    return "".join(result)


def _identifier_tokens(value: str | None) -> set[str]:
    if not value:
        return set()
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    words = re.split(r"[^A-Za-z0-9]+", normalized)
    return {word.lower() for word in words if word and word.lower() not in GENERIC_RESOURCE_NAME_PARTS}


def _cbuffer_usage_stats(source: str) -> dict[str, dict[str, float]]:
    stats: dict[str, dict[str, float]] = {}
    for match in re.finditer(r"\bcbuffer\s+(?P<name>\w+)(?:\s*:\s*register\(b\d+\))?\s*\{", source):
        name = match.group("name")
        depth = 1
        cursor = match.end()
        while cursor < len(source) and depth > 0:
            if source[cursor] == "{":
                depth += 1
            elif source[cursor] == "}":
                depth -= 1
            cursor += 1
        body = source[match.end() : max(match.end(), cursor - 1)]
        outside = f"{source[:match.start()]} {source[cursor:]}"
        members = [identifier for identifier in IDENTIFIER_RE.findall(body) if identifier.startswith(f"{name}_")]
        unique_members = sorted(set(members))
        used_count = sum(1 for member in unique_members if re.search(rf"\b{re.escape(member)}\b", outside))
        total_count = len(unique_members)
        stats[name] = {
            "used_count": float(used_count),
            "usage_ratio": float(used_count) / float(total_count or 1),
        }
    return stats


def _shader_source_text(shader_info: dict[str, Any]) -> str:
    chunks: list[str] = []
    for stage in shader_info.get("stages", []):
        resolver_result = stage.get("resolver_result", {}).get("result") or {}
        for source in resolver_result.get("sources", []):
            content = source.get("content")
            if content:
                chunks.append(str(content))
    return "\n".join(chunks)


def _shader_bindings_from_source(source: str) -> dict[str, list[dict[str, Any]]]:
    bindings: dict[str, list[dict[str, Any]]] = {"CBV": [], "SRV": [], "UAV": [], "Sampler": []}
    if not source:
        return bindings

    cbuffer_usage = _cbuffer_usage_stats(source)
    next_cbv_slot = 0
    for match in CBUFFER_DECL_RE.finditer(source):
        register_slot = match.group("slot")
        slot = int(register_slot) if register_slot is not None else next_cbv_slot
        usage = cbuffer_usage.get(match.group("name"), {})
        bindings["CBV"].append(
            {
                "view_type": "CBV",
                "slot": slot,
                "shader_binding_name": match.group("name"),
                "declaration_type": "cbuffer",
                "resource_dimension": "Buffer",
                "usage_count": usage.get("used_count", 0.0),
                "usage_ratio": usage.get("usage_ratio", 0.0),
            }
        )
        next_cbv_slot = max(next_cbv_slot + 1, slot + 1)

    srv_slot = 0
    uav_slot = 0
    resource_source = _source_without_cbuffer_bodies(source)
    for match in RESOURCE_DECL_RE.finditer(resource_source):
        resource_type = match.group("type")
        name = match.group("name")
        register_slot = match.group("register_slot")
        if resource_type.startswith("RW"):
            slot = int(register_slot) if register_slot is not None else uav_slot
            bindings["UAV"].append(
                {
                    "view_type": "UAV",
                    "slot": slot,
                    "shader_binding_name": name,
                    "declaration_type": resource_type,
                    "resource_dimension": _resource_dimension(resource_type),
                    "register_space": int(match.group("register_space")) if match.group("register_space") is not None else None,
                }
            )
            uav_slot = max(uav_slot + 1, slot + 1)
        else:
            slot = int(register_slot) if register_slot is not None else srv_slot
            bindings["SRV"].append(
                {
                    "view_type": "SRV",
                    "slot": slot,
                    "shader_binding_name": name,
                    "declaration_type": resource_type,
                    "resource_dimension": _resource_dimension(resource_type),
                    "register_space": int(match.group("register_space")) if match.group("register_space") is not None else None,
                }
            )
            srv_slot = max(srv_slot + 1, slot + 1)

    sampler_names: set[str] = set()
    for match in SAMPLER_DECL_RE.finditer(source):
        sampler_names.add(match.group("name"))
        bindings["Sampler"].append(
            {
                "view_type": "Static Sampler",
                "slot": int(match.group("slot")),
                "shader_binding_name": match.group("name"),
                "declaration_type": match.group("type"),
                "resource_dimension": "Sampler",
                "register_space": int(match.group("space")) if match.group("space") is not None else None,
            }
        )
    next_sampler_slot = max((int(item["slot"]) for item in bindings["Sampler"]), default=-1) + 1
    for match in SAMPLER_NO_REGISTER_DECL_RE.finditer(source):
        name = match.group("name")
        if name in sampler_names:
            continue
        sampler_names.add(name)
        bindings["Sampler"].append(
            {
                "view_type": "Static Sampler",
                "slot": next_sampler_slot,
                "shader_binding_name": name,
                "declaration_type": match.group("type"),
                "resource_dimension": "Sampler",
                "register_space": None,
            }
        )
        next_sampler_slot += 1
    return bindings


def _get_event(index: dict[str, Any], global_id: int | str, stage: str) -> dict[str, Any]:
    event = index["events_by_global_id"].get(str(global_id))
    if event is None:
        raise PixToolError(
            code="event_not_found",
            message=f"Global ID was not found: {global_id}",
            stage=stage,
            suggestion="Run extract-shader-events-tree and choose an event global id.",
        )
    return event


def _latest_descriptor_write(index: dict[str, Any], descriptor_index: int) -> dict[str, Any] | None:
    writes = index.get("descriptor_index", {}).get(str(descriptor_index), [])
    return writes[-1] if writes else None


def _latest_matching_descriptor_write(index: dict[str, Any], descriptor_index: int, root_binding: dict[str, Any]) -> dict[str, Any] | None:
    writes = index.get("descriptor_index", {}).get(str(descriptor_index), [])
    heap_id = root_binding.get("heap_id")
    if heap_id is not None:
        writes = [write for write in writes if str(write.get("heap_id")) == str(heap_id)]
    root_line = root_binding.get("line")
    if root_line is not None:
        writes_before_binding = [write for write in writes if int(write.get("line") or 0) <= int(root_line)]
        if writes_before_binding:
            writes = writes_before_binding
    return writes[-1] if writes else None


def _resource_name(index: dict[str, Any], resource_id: str | None) -> str | None:
    if resource_id is None:
        return None
    resource = index.get("resource_names", {}).get(str(resource_id), {})
    return resource.get("name")


def _infer_shader_binding_name(resource_name: str | None, view_type: str | None) -> str | None:
    if not resource_name:
        return None
    leaf_name = resource_name.rsplit(".", 1)[-1]
    if view_type == "UAV":
        return "RW" + leaf_name
    if view_type == "SRV":
        return leaf_name
    return None


def _apply_shader_binding(resolved: dict[str, Any], binding: dict[str, Any]) -> dict[str, Any]:
    item = dict(resolved)
    shader_binding_name = binding.get("shader_binding_name")
    item.update(
        {
            "shader_binding_name": shader_binding_name,
            "shader_binding_slot": binding.get("slot"),
            "shader_declaration_type": binding.get("declaration_type"),
            "resource_dimension": binding.get("resource_dimension"),
            "register_space": binding.get("register_space"),
            "view_type": binding.get("view_type") or item.get("view_type"),
        }
    )
    resource_name = item.get("resource_name")
    fallback_resource_name = False
    if not resource_name and shader_binding_name:
        resource_name = shader_binding_name
        item["resource_name"] = resource_name
        fallback_resource_name = True
    if resource_name and shader_binding_name and not fallback_resource_name:
        item["display_name"] = f"{resource_name}:{shader_binding_name}"
    else:
        item["display_name"] = resource_name or shader_binding_name
    return item


def _resolved_root_cbv(index: dict[str, Any], root_binding: dict[str, Any], binding: dict[str, Any]) -> dict[str, Any] | None:
    resource_id = str(root_binding.get("resource_id")) if root_binding.get("resource_id") is not None else None
    resource_name = _resource_name(index, resource_id)
    if not resource_id and not resource_name:
        return None
    return _apply_shader_binding(
        {
            "root_index": root_binding.get("root_index"),
            "stage": root_binding.get("stage"),
            "root_descriptor_index": None,
            "descriptor_index": None,
            "resource_id": resource_id,
            "resource_name": resource_name,
            "view_type": "CBV",
            "shader_binding_name": binding.get("shader_binding_name"),
            "display_name": f"{resource_name}:{binding.get('shader_binding_name')}" if resource_name and binding.get("shader_binding_name") else resource_name,
            "descriptor_write": None,
            "root_binding": root_binding,
        },
        binding,
    )


def _resolved_static_sampler(binding: dict[str, Any]) -> dict[str, Any]:
    shader_binding_name = binding.get("shader_binding_name")
    return {
        "root_index": None,
        "stage": binding.get("stage") or "Compute",
        "root_descriptor_index": None,
        "descriptor_index": None,
        "resource_id": None,
        "resource_name": shader_binding_name,
        "view_type": "Static Sampler",
        "shader_binding_name": shader_binding_name,
        "shader_binding_slot": binding.get("slot"),
        "shader_declaration_type": binding.get("declaration_type"),
        "resource_dimension": binding.get("resource_dimension"),
        "register_space": binding.get("register_space"),
        "display_name": shader_binding_name,
        "descriptor_write": None,
        "root_binding": None,
    }


def _resolved_descriptor(index: dict[str, Any], descriptor_index: int, root_binding: dict[str, Any]) -> dict[str, Any] | None:
    write = _latest_matching_descriptor_write(index, descriptor_index, root_binding)
    if write is None:
        return None
    resource_id = str(write.get("resource_id")) if write.get("resource_id") is not None else None
    resource_name = _resource_name(index, resource_id)
    shader_binding_name = _infer_shader_binding_name(resource_name, write.get("view_type"))
    return {
        "root_index": root_binding.get("root_index"),
        "stage": root_binding.get("stage"),
        "root_descriptor_index": root_binding.get("descriptor_index"),
        "descriptor_index": str(descriptor_index),
        "resource_id": resource_id,
        "resource_name": resource_name,
        "view_type": write.get("view_type"),
        "resource_dimension": _descriptor_dimension(write),
        "shader_binding_name": shader_binding_name,
        "display_name": f"{resource_name}:{shader_binding_name}" if resource_name and shader_binding_name else resource_name,
        "descriptor_write": write,
        "root_binding": root_binding,
    }


def _fill_missing_cbv_bindings(bindings: list[dict[str, Any]], root_count: int) -> list[dict[str, Any]]:
    filled = list(bindings)
    fallback_names = ["_RootShaderParameters", "View", "ReflectionCaptureSM5"]
    existing_names = {str(binding.get("shader_binding_name")) for binding in filled}
    next_slot = max((int(binding.get("slot", -1)) for binding in filled), default=-1) + 1
    while len(filled) < root_count:
        name = next((candidate for candidate in fallback_names if candidate not in existing_names), f"CBV{len(filled)}")
        existing_names.add(name)
        filled.append(
            {
                "view_type": "CBV",
                "slot": next_slot,
                "shader_binding_name": name,
                "declaration_type": "cbuffer",
                "resource_dimension": "Buffer",
            }
        )
        next_slot += 1
    return filled


def _binding_matches_descriptor(binding: dict[str, Any], resolved: dict[str, Any]) -> bool:
    binding_dimension = binding.get("resource_dimension")
    descriptor_dimension = resolved.get("resource_dimension")
    if binding_dimension and descriptor_dimension and binding_dimension != descriptor_dimension:
        return False
    return True


def _descriptor_binding_score(binding: dict[str, Any], resolved: dict[str, Any]) -> float:
    if not _binding_matches_descriptor(binding, resolved):
        return -1.0
    score = 0.0
    binding_name = str(binding.get("shader_binding_name") or "")
    resource_name = str(resolved.get("resource_name") or "")
    binding_normalized = re.sub(r"[^a-z0-9]", "", binding_name.lower())
    resource_leaf = resource_name.rsplit(".", 1)[-1]
    resource_normalized = re.sub(r"[^a-z0-9]", "", resource_leaf.lower())
    if resource_normalized and resource_normalized in binding_normalized:
        score += 100.0
    resource_tokens = _identifier_tokens(resource_name)
    binding_tokens = _identifier_tokens(binding_name)
    if resolved.get("resource_dimension") == "Buffer" and not resource_tokens:
        if binding.get("selected_cbv_prefix"):
            score += 35.0
        elif binding.get("unselected_cbv_prefix"):
            score -= 15.0
    if resource_tokens and binding_tokens:
        score += 10.0 * len(resource_tokens & binding_tokens)
    descriptor_text = str((resolved.get("descriptor_write") or {}).get("text") or "").lower()
    if "snorm" in descriptor_text and ({"tangent", "tangents", "normal", "normals"} & binding_tokens):
        score += 25.0
    if "float" in descriptor_text and ({"position", "positions", "pos"} & binding_tokens):
        score += 20.0
    if "unorm" in descriptor_text and ({"color", "colors", "colour", "colours"} & binding_tokens):
        score += 20.0
    if "uint" in descriptor_text and ({"index", "indices", "id", "ids"} & binding_tokens):
        score += 8.0
    return score


def _select_descriptor_binding(bindings: list[dict[str, Any]], resolved: dict[str, Any], start_index: int) -> tuple[int, dict[str, Any]] | None:
    best: tuple[float, int, dict[str, Any]] | None = None
    first_matching: tuple[int, dict[str, Any]] | None = None
    for index in range(start_index, len(bindings)):
        binding = bindings[index]
        score = _descriptor_binding_score(binding, resolved)
        if score < 0:
            continue
        if first_matching is None:
            first_matching = (index, binding)
        ranked = (score, -float(index - start_index), binding)
        if best is None or ranked > (best[0], -float(best[1] - start_index), best[2]):
            best = (score, index, binding)
    if best is None:
        return None
    if best[0] < 25 and first_matching is not None:
        return first_matching
    return best[1], best[2]


def _descriptor_scan_window(bindings: dict[str, list[dict[str, Any]]], descriptor_scan_count: int) -> int:
    return max(1, descriptor_scan_count, len(bindings.get("SRV", [])) + len(bindings.get("UAV", [])))


def _descriptor_resources_by_view_type(
    index: dict[str, Any],
    event: dict[str, Any],
    descriptor_scan_count: int,
    root_tables: dict[str, dict[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    resources_by_type: dict[str, dict[str, dict[str, Any]]] = {"SRV": {}, "UAV": {}}
    root_tables = root_tables if root_tables is not None else event.get("root_descriptor_tables", {})
    sorted_roots = [root_tables[key] for key in sorted(root_tables, key=lambda value: int(value))]
    for root_binding in sorted_roots:
        start = int(root_binding["descriptor_index"])
        local_scan_count = int(root_binding.get("descriptor_count") or descriptor_scan_count)
        for descriptor_index in range(start, start + local_scan_count):
            resolved = _resolved_descriptor(index, descriptor_index, root_binding)
            if resolved is None:
                continue
            view_type = resolved.get("view_type")
            if view_type not in resources_by_type:
                continue
            existing = resources_by_type[view_type].get(str(descriptor_index))
            if existing is None or int(root_binding["descriptor_index"]) > int(existing["root_descriptor_index"]):
                resources_by_type[view_type][str(descriptor_index)] = resolved
    return {
        view_type: [resources[str(descriptor_index)] for descriptor_index in sorted((int(key) for key in resources))]
        for view_type, resources in resources_by_type.items()
    }


def _resolve_bound_resources(index: dict[str, Any], event: dict[str, Any], descriptor_scan_count: int) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    seen: set[tuple[str | None, str | None, str | None]] = set()
    root_tables = event.get("root_descriptor_tables", {})
    for root_index in sorted(root_tables, key=lambda value: int(value)):
        root_binding = root_tables[root_index]
        start = int(root_binding["descriptor_index"])
        for descriptor_index in range(start, start + descriptor_scan_count):
            resolved = _resolved_descriptor(index, descriptor_index, root_binding)
            if resolved is None:
                continue
            key = (resolved.get("root_index"), resolved.get("descriptor_index"), resolved.get("resource_id"))
            if key in seen:
                continue
            seen.add(key)
            resources.append(resolved)
    return resources


def _resolve_shader_declared_resources(
    index: dict[str, Any],
    event: dict[str, Any],
    bindings: dict[str, list[dict[str, Any]]],
    descriptor_scan_count: int,
    *,
    stage: str | None = None,
    root_cbvs: dict[str, dict[str, Any]] | None = None,
    root_tables: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    root_cbvs = root_cbvs if root_cbvs is not None else event.get("root_constant_buffer_views", {})
    # root_tables = event.get("root_descriptor_tables", {})

    sorted_cbv_roots = [root_cbvs[key] for key in sorted(root_cbvs, key=lambda value: int(value))]
    cbv_bindings = _fill_missing_cbv_bindings(sorted(bindings.get("CBV", []), key=lambda item: int(item.get("slot", 0))), len(sorted_cbv_roots))
    for binding, root_binding in zip(cbv_bindings, sorted_cbv_roots):
        resolved = _resolved_root_cbv(index, root_binding, binding)
        if resolved is not None:
            resources.append(resolved)

    resources_by_view_type = _descriptor_resources_by_view_type(index, event, _descriptor_scan_window(bindings, descriptor_scan_count), root_tables=root_tables)

    for view_type in ("SRV", "UAV"):
        sorted_bindings = sorted(bindings.get(view_type, []), key=lambda item: int(item.get("slot", 0)))
        binding_index = 0
        descriptor_offset = 0
        for resolved in resources_by_view_type[view_type]:
            selected = _select_descriptor_binding(sorted_bindings, resolved, binding_index)
            if selected is None:
                continue
            selected_index, binding = selected
            binding_for_descriptor = dict(binding)
            binding_for_descriptor["slot"] = descriptor_offset
            resources.append(_apply_shader_binding(resolved, binding_for_descriptor))
            binding_index = selected_index + 1
            descriptor_offset += 1

    for binding in sorted(bindings.get("Sampler", []), key=lambda item: (int(item.get("register_space") or 0), int(item.get("slot", 0)))):
        binding_for_stage = dict(binding)
        if stage is not None:
            binding_for_stage["stage"] = stage
        resources.append(_resolved_static_sampler(binding_for_stage))

    return resources


def _filter_static_samplers(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sampler_indexes = [index for index, resource in enumerate(resources) if resource.get("view_type") == "Static Sampler"]
    if len(sampler_indexes) <= 1:
        return resources
    texture_binding_names = [
        str(resource.get("shader_binding_name"))
        for resource in resources
        if resource.get("view_type") == "SRV" and resource.get("resource_dimension") == "Texture" and resource.get("shader_binding_name")
    ]
    if not texture_binding_names:
        return resources
    keep_sampler_indexes: set[int] = {sampler_indexes[0]}
    for sampler_index in sampler_indexes:
        sampler_name = str(resources[sampler_index].get("shader_binding_name") or "")
        if any(texture_name and texture_name in sampler_name for texture_name in texture_binding_names):
            keep_sampler_indexes.add(sampler_index)
    if len(keep_sampler_indexes) > 3:
        keep_sampler_indexes.remove(max(keep_sampler_indexes))
    return [resource for index, resource in enumerate(resources) if index not in sampler_indexes or index in keep_sampler_indexes]


def _resolve_shader_bindings(export_dir: str | Path, global_id: int | str, pdb_search_paths: list[str] | None, refresh: bool) -> dict[str, list[dict[str, Any]]]:
    if not pdb_search_paths:
        return {"CBV": [], "SRV": [], "UAV": [], "Sampler": []}
    try:
        shader_info = get_event_shader_source(export_dir, global_id, pdb_search_paths=pdb_search_paths, refresh=refresh)
    except Exception:
        return {"CBV": [], "SRV": [], "UAV": [], "Sampler": []}
    return _shader_bindings_from_source(_shader_source_text(shader_info))


def _stage_source_text(stage: dict[str, Any]) -> str:
    resolver_result = stage.get("resolver_result", {}).get("result") or {}
    chunks: list[str] = []
    for source in resolver_result.get("sources", []):
        content = source.get("content")
        if content:
            chunks.append(str(content))
    return "\n".join(chunks)


def _resolve_shader_bindings_by_stage(export_dir: str | Path, global_id: int | str, pdb_search_paths: list[str] | None, refresh: bool) -> dict[str, dict[str, list[dict[str, Any]]]]:
    if not pdb_search_paths:
        return {}
    try:
        shader_info = get_event_shader_source(export_dir, global_id, pdb_search_paths=pdb_search_paths, refresh=refresh)
    except Exception:
        return {}
    result: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for stage in shader_info.get("stages", []):
        stage_name = str(stage.get("stage") or "").upper()
        if not stage_name:
            continue
        result[stage_name] = _shader_bindings_from_source(_stage_source_text(stage))
    return result


def _graphics_stage_roots(event: dict[str, Any], stage: str) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    return event.get("root_constant_buffer_views", {}), event.get("root_descriptor_tables", {})


def _root_binding_runs(bindings: dict[str, dict[str, Any]]) -> list[list[dict[str, Any]]]:
    values = list(bindings.values())
    ordered = sorted(values, key=lambda item: int(item.get("line") or 0)) if any(item.get("line") for item in values) else values
    runs: list[list[dict[str, Any]]] = []
    for binding in ordered:
        root_index = int(binding.get("root_index") or 0)
        if runs and root_index < int(runs[-1][-1].get("root_index") or 0):
            runs.append([])
        if not runs:
            runs.append([])
        runs[-1].append(binding)
    return runs


def _stage_order(bindings_by_stage: dict[str, dict[str, list[dict[str, Any]]]]) -> list[str]:
    preferred = ["VS", "PS"]
    return [stage for stage in preferred if bindings_by_stage.get(stage)] + [stage for stage in bindings_by_stage if stage not in preferred]


def _root_dict(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(item.get("root_index")): item for item in items}


def _select_cbv_bindings(bindings: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    ordered = sorted(bindings, key=lambda item: int(item.get("slot", 0)))
    if count >= len(ordered):
        return ordered
    usage_values = {float(binding.get("usage_count") or 0.0) for binding in ordered}
    ratio_values = {float(binding.get("usage_ratio") or 0.0) for binding in ordered}
    if len(usage_values) == 1 and len(ratio_values) == 1 and count > 1:
        selected_indexes = {0, *range(max(1, len(ordered) - (count - 1)), len(ordered))}
    else:
        ranked = sorted(
            enumerate(ordered),
            key=lambda pair: (float(pair[1].get("usage_count") or 0.0), float(pair[1].get("usage_ratio") or 0.0), -pair[0]),
            reverse=True,
        )[:count]
        selected_indexes = {index for index, _binding in ranked}
    return [binding for index, binding in enumerate(ordered) if index in selected_indexes]


def _table_has_declared_resources(index: dict[str, Any], event: dict[str, Any], table: dict[str, Any], bindings: dict[str, list[dict[str, Any]]], descriptor_scan_count: int) -> bool:
    resources_by_view_type = _descriptor_resources_by_view_type(index, event, _descriptor_scan_window(bindings, descriptor_scan_count), root_tables={str(table.get("root_index")): table})
    return any(resources_by_view_type.get(view_type) for view_type in ("SRV", "UAV"))


def _table_stage_score(index: dict[str, Any], event: dict[str, Any], table: dict[str, Any], bindings: dict[str, list[dict[str, Any]]], descriptor_scan_count: int) -> float:
    resources_by_view_type = _descriptor_resources_by_view_type(index, event, _descriptor_scan_window(bindings, descriptor_scan_count), root_tables={str(table.get("root_index")): table})
    score = 0.0
    for view_type in ("SRV", "UAV"):
        declared = bindings.get(view_type, [])
        if not declared:
            if resources_by_view_type.get(view_type):
                score -= 100.0
            continue
        declared_dimensions = {binding.get("resource_dimension") for binding in declared}
        for resolved in resources_by_view_type.get(view_type, []):
            score += 20.0
            if resolved.get("resource_dimension") in declared_dimensions:
                score += 5.0
    return score


def _partition_graphics_roots(
    index: dict[str, Any],
    event: dict[str, Any],
    bindings_by_stage: dict[str, dict[str, list[dict[str, Any]]]],
    descriptor_scan_count: int,
) -> dict[str, tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]]:
    stages = _stage_order(bindings_by_stage)
    result: dict[str, tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]] = {stage: ({}, {}) for stage in stages}

    cbv_runs = _root_binding_runs(event.get("root_constant_buffer_views", {}))
    for stage, run in zip(stages, cbv_runs):
        cbvs, tables = result[stage]
        result[stage] = (_root_dict(run), tables)

    candidate_tables = [
        table
        for run in _root_binding_runs(event.get("root_descriptor_tables", {}))
        for table in run
        if _table_has_declared_resources(index, event, table, {"CBV": [], "SRV": [], "UAV": [], "Sampler": []}, descriptor_scan_count)
    ]
    sorted_candidate_tables = sorted(candidate_tables, key=lambda item: int(item.get("descriptor_index") or 0))
    descriptor_counts: dict[str, int] = {}
    for table_index, table in enumerate(sorted_candidate_tables):
        start = int(table.get("descriptor_index") or 0)
        next_start = int(sorted_candidate_tables[table_index + 1].get("descriptor_index") or 0) if table_index + 1 < len(sorted_candidate_tables) else None
        if next_start is not None and next_start > start:
            descriptor_counts[str(table.get("root_index"))] = next_start - start
    resource_tables = [dict(table, descriptor_count=descriptor_counts.get(str(table.get("root_index")), descriptor_scan_count)) for table in candidate_tables]
    ordered_resource_tables = list(resource_tables)
    has_table_order = any(table.get("line") for table in ordered_resource_tables)
    remaining_tables = list(resource_tables)
    for stage_index, stage in enumerate(stages):
        if not remaining_tables:
            break
        if has_table_order and stage_index < len(ordered_resource_tables):
            table = ordered_resource_tables[stage_index]
            if table not in remaining_tables or _table_stage_score(index, event, table, bindings_by_stage.get(stage, {}), descriptor_scan_count) <= 0:
                continue
            remaining_tables.remove(table)
        else:
            best_table = max(
                remaining_tables,
                key=lambda candidate: _table_stage_score(index, event, candidate, bindings_by_stage.get(stage, {}), descriptor_scan_count),
            )
            if _table_stage_score(index, event, best_table, bindings_by_stage.get(stage, {}), descriptor_scan_count) <= 0:
                continue
            table = best_table
            remaining_tables.remove(best_table)
        cbvs, tables = result[stage]
        tables = dict(tables)
        tables[str(table.get("root_index"))] = table
        result[stage] = (cbvs, tables)
    return result


def _filtered_graphics_stage_bindings(stage: str, bindings: dict[str, list[dict[str, Any]]], cbv_count: int | None = None) -> dict[str, list[dict[str, Any]]]:
    filtered = {key: list(value) for key, value in bindings.items()}
    declared_cbv_names = {str(binding.get("shader_binding_name")) for binding in filtered.get("CBV", [])}
    if cbv_count is not None:
        filtered["CBV"] = _select_cbv_bindings(filtered.get("CBV", []), cbv_count)
    selected_cbv_names = {str(binding.get("shader_binding_name")) for binding in filtered.get("CBV", [])}
    unselected_cbv_names = declared_cbv_names - selected_cbv_names
    for view_type in ("SRV", "UAV"):
        updated_bindings = []
        for binding in filtered.get(view_type, []):
            prefix = str(binding.get("shader_binding_name") or "").split("_", 1)[0]
            updated_bindings.append(
                dict(
                    binding,
                    selected_cbv_prefix=prefix in selected_cbv_names,
                    unselected_cbv_prefix=prefix in unselected_cbv_names,
                )
            )
        filtered[view_type] = updated_bindings
    filtered["CBV"] = [dict(binding, slot=slot) for slot, binding in enumerate(filtered.get("CBV", []))]
    return filtered


def _resolve_graphics_shader_resources(
    index: dict[str, Any],
    event: dict[str, Any],
    bindings_by_stage: dict[str, dict[str, list[dict[str, Any]]]],
    descriptor_scan_count: int,
) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    roots_by_stage = _partition_graphics_roots(index, event, bindings_by_stage, descriptor_scan_count)
    for stage in _stage_order(bindings_by_stage):
        bindings = bindings_by_stage.get(stage)
        if not bindings:
            continue
        root_cbvs, root_tables = roots_by_stage.get(stage, ({}, {}))
        bindings = _filtered_graphics_stage_bindings(stage, bindings, cbv_count=len(root_cbvs))
        stage_resources = _resolve_shader_declared_resources(
            index,
            event,
            bindings,
            descriptor_scan_count,
            stage=stage,
            root_cbvs=root_cbvs,
            root_tables=root_tables,
        )
        for resource in stage_resources:
            resource["stage"] = stage
        resources.extend(_filter_static_samplers(stage_resources))
    return resources


def _make_pipeline_resource(index: dict[str, Any], binding: dict[str, Any], view_type: str, display_name: str | None = None) -> dict[str, Any] | None:
    resource_id = str(binding.get("resource_id")) if binding.get("resource_id") is not None else None
    resource_name = _resource_name(index, resource_id)
    if not resource_id and not resource_name:
        return None
    return {
        "root_index": None,
        "stage": binding.get("stage"),
        "root_descriptor_index": None,
        "descriptor_index": None,
        "resource_id": resource_id,
        "resource_name": resource_name,
        "view_type": view_type,
        "shader_binding_name": None,
        "shader_binding_slot": binding.get("slot"),
        "shader_declaration_type": None,
        "resource_dimension": "Buffer" if view_type in {"VB", "IB"} else "Texture",
        "register_space": None,
        "display_name": display_name or resource_name,
        "descriptor_write": None,
        "root_binding": binding,
    }


def _resolve_input_assembler_resources(index: dict[str, Any], event: dict[str, Any]) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    ia = event.get("input_assembler") or {}
    for vertex_buffer in ia.get("vertex_buffers") or []:
        if int(vertex_buffer.get("slot") or 0) > len(resources):
            break
        resolved = _make_pipeline_resource(index, vertex_buffer, "VB")
        if resolved is not None:
            resources.append(resolved)
    index_buffer = ia.get("index_buffer")
    if index_buffer:
        resolved = _make_pipeline_resource(index, index_buffer, "IB")
        if resolved is not None:
            resources.append(resolved)
    return resources


def _resolve_output_merger_resources(index: dict[str, Any], event: dict[str, Any]) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    om = event.get("output_merger") or {}
    for target in om.get("render_targets") or []:
        resolved = _make_pipeline_resource(index, target, "RTV")
        if resolved is not None:
            resources.append(resolved)
    depth_stencil = om.get("depth_stencil")
    if depth_stencil:
        depth = _make_pipeline_resource(index, dict(depth_stencil, slot=None), "Depth", display_name=_resource_name(index, str(depth_stencil.get("resource_id"))))
        stencil = _make_pipeline_resource(index, dict(depth_stencil, slot=None), "Stencil", display_name=_resource_name(index, str(depth_stencil.get("resource_id"))))
        resources.extend(item for item in (depth, stencil) if item is not None)
    return resources


def _normalize_resource_selector(value: str | int | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text.lower() if text else None


def _resource_matches(resource: dict[str, Any], selector: str) -> bool:
    candidates = {
        str(resource.get("resource_id") or ""),
        str(resource.get("resource_name") or ""),
        str(resource.get("shader_binding_name") or ""),
        str(resource.get("display_name") or ""),
    }
    selector_lower = selector.lower()
    return any(candidate.lower() == selector_lower for candidate in candidates if candidate)


def _select_target_resource(bound_resources: list[dict[str, Any]], selector: str | int | None) -> dict[str, Any]:
    normalized = _normalize_resource_selector(selector)
    if normalized is None:
        raise PixToolError(
            code="resource_selector_missing",
            message="A resource selector is required for access history.",
            stage="resource_access_history",
            suggestion="Pass resource as a resource id, resource name, shader binding name, or display name such as 'RayTracing.LightGrid:RWLightGrid'.",
        )
    matches = [resource for resource in bound_resources if _resource_matches(resource, normalized)]
    if not matches:
        available = [resource.get("display_name") or resource.get("resource_name") or resource.get("resource_id") for resource in bound_resources]
        raise PixToolError(
            code="resource_not_bound",
            message=f"Resource was not found in the event bindings: {selector}",
            stage="resource_access_history",
            suggestion="Choose one of the resources returned by get-event-resource for the same global id.",
            details={"available_resources": available},
        )
    return matches[0]


def _event_display_name(event: dict[str, Any]) -> str | None:
    name = event.get("name")
    marker_path = event.get("marker_path") or []
    if name in {"Unknown", "Dispatch", "DispatchIndirect", "DispatchMesh", "DispatchRays"} and marker_path:
        return str(marker_path[-1])
    if name:
        return str(name)
    return str(marker_path[-1]) if marker_path else name


def _queue_name(_event: dict[str, Any]) -> str:
    return "Graphics Queue 0 (3D Queue (GPU 0))"


def _api_parameter_binding(text: str, resource_id: str) -> str | None:
    for match in re.finditer(r"\(([^()]|GetResource\(\d+\)\.Get\(\))*\)", text):
        params = match.group(0)[1:-1]
        if f"GetResource({resource_id})" not in params:
            continue
        parts = [part.strip() for part in params.split(",")]
        for index, part in enumerate(parts):
            if f"GetResource({resource_id})" in part:
                return f"API Parameters [{index}]"
        return "API Parameters [None]"
    return None


def _read_write_for_text(text: str, resource_id: str, view_type: str | None = None) -> str:
    if "ClearUnorderedAccessView" in text or "ClearRenderTargetView" in text or "ClearDepthStencilView" in text:
        return "Write"
    if "DiscardResource" in text:
        return "Write"
    if "CopyBufferRegion" in text or "CopyTextureRegion" in text:
        match = re.search(r"Copy(?:Buffer|Texture)Region\((.*?)\)", text)
        if match:
            params = [part.strip() for part in match.group(1).split(",")]
            for index, part in enumerate(params):
                if f"GetResource({resource_id})" in part:
                    return "Write" if index == 0 else "Read"
    if "CopyResource" in text:
        return "Read/Write"
    if "Transition" in text or "ResourceBarrier" in text:
        return "Read/Write"
    if view_type == "UAV":
        return "Read/Write"
    if view_type == "SRV":
        return "Read"
    return "Read/Write"


def _states_for_text(text: str, resource_id: str, fallback_state: str | None = None) -> str | None:
    transition = TRANSITION_RE.search(text)
    if transition and transition.group("resource_id") == str(resource_id):
        return transition.group("after").replace("D3D12_RESOURCE_STATE_", "STATE_")
    if "DiscardResource" in text:
        return "STATE_UNORDERED_ACCESS"
    if fallback_state:
        return fallback_state
    return None


def _binding_for_resource_ref(text: str, resource_id: str) -> str:
    if "ClearUnorderedAccessView" in text or "ClearRenderTargetView" in text or "ClearDepthStencilView" in text:
        return "OM [None]"
    return _api_parameter_binding(text, resource_id) or "API Parameters [None]"


def _shader_binding(target: dict[str, Any]) -> str:
    view_type = target.get("view_type")
    if view_type in {"SRV", "UAV"}:
        stage = str(target.get("stage") or "Compute")
        prefix = "CS" if stage == "Compute" else stage
        if prefix == "Graphics":
            prefix = "Shader"
        slot = target.get("shader_binding_slot")
        if target.get("descriptor_index") is not None and target.get("root_descriptor_index") is not None:
            slot = int(target["descriptor_index"]) - int(target["root_descriptor_index"])
        return f"{prefix} {view_type} {int(slot or 0)}"
    return str(target.get("shader_binding_name") or target.get("display_name") or "Shader Binding")


def _shader_state(target: dict[str, Any]) -> str | None:
    if target.get("view_type") == "UAV":
        return "STATE_COMMON"
    if target.get("view_type") == "SRV":
        return "STATE_NON_PIXEL_SHADER_RESOURCE | STATE_PIXEL_SHADER_RESOURCE"
    return None


def _same_named_resource_ids(index: dict[str, Any], target: dict[str, Any]) -> set[str]:
    target_name = str(target.get("resource_name") or "")
    target_id = str(target.get("resource_id") or "")
    if not target_name:
        return {target_id} if target_id else set()
    return {
        str(resource_id)
        for resource_id, resource in index.get("resource_names", {}).items()
        if str((resource or {}).get("name") or "") == target_name
    } | ({target_id} if target_id else set())


def _access_row_target_for_resource_id(index: dict[str, Any], target: dict[str, Any], resource_id: str) -> dict[str, Any]:
    row_target = dict(target)
    row_target["resource_id"] = str(resource_id)
    row_target["resource_name"] = _resource_name(index, str(resource_id)) or target.get("resource_name")
    return row_target


def _first_matching_resource_id(resource_ids: set[str], ids_in_text: set[str]) -> str | None:
    for resource_id in sorted(resource_ids, key=int):
        if resource_id in ids_in_text:
            return resource_id
    return None


def _make_access_row(
    event: dict[str, Any],
    target: dict[str, Any],
    *,
    binding: str,
    read_write: str,
    states: str | None,
    resource_ref: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "event": _event_display_name(event),
        "queue": _queue_name(event),
        "global_id": event.get("global_id"),
        "resource_id": target.get("resource_id"),
        "name": target.get("resource_name"),
        "binding": binding,
        "read_write": read_write,
        "states": states,
        "file": event.get("file"),
        "line": resource_ref.get("line") if resource_ref else event.get("line"),
        "text": resource_ref.get("text") if resource_ref else None,
    }


def _dedupe_access_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any, Any, Any, Any]] = set()
    for row in rows:
        key = (row.get("global_id"), row.get("binding"), row.get("states"), row.get("line"), row.get("text"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _database_path_from_index(index: dict[str, Any]) -> str | None:
    database_path = index.get("database_path")
    return str(database_path) if database_path else None


def _database_event_resources(index: dict[str, Any], global_id: int | str, pdb_search_paths: list[str] | None, refresh: bool) -> tuple[list[dict[str, Any]], str | None]:
    database_path = _database_path_from_index(index)
    if refresh or not database_path:
        return [], "database unavailable" if not database_path else "refresh requested"
    try:
        resources = load_event_bound_resources(database_path, global_id)
    except Exception as exc:
        return [], str(exc)
    if not resources:
        return [], "no event-bound resources in database"
    has_runtime_resolved = any(str(resource.get("database_source") or resource.get("source") or "") == "runtime_resolved" for resource in resources)
    if pdb_search_paths and not has_runtime_resolved:
        return [], "database only has precomputed bindings without shader declaration names"
    return resources, None


def _event_order(index: dict[str, Any], global_id: int | str) -> int:
    for position, event in enumerate(index.get("events", [])):
        if str(event.get("global_id")) == str(global_id):
            return position
    return -1


def _cache_event_resources(index: dict[str, Any], global_id: int | str, resources: list[dict[str, Any]]) -> None:
    database_path = _database_path_from_index(index)
    if not database_path or not resources:
        return
    event_order = _event_order(index, global_id)
    cached_resources = [dict(resource, global_id=str(global_id), event_order=event_order, source="runtime_resolved", confidence=1.0) for resource in resources]
    try:
        replace_event_bound_resources(database_path, global_id, cached_resources)
    except Exception:
        return


def _resource_diagnostics(
    index: dict[str, Any],
    event: dict[str, Any],
    descriptor_scan_count: int,
    bound_resources: list[dict[str, Any]],
    shader_binding_counts: dict[str, Any],
    *,
    database_hit: bool,
    query_mode: str,
    fallback_reason: str | None = None,
) -> dict[str, Any]:
    database_path = _database_path_from_index(index)
    return {
        "cache_hit": index.get("cache_hit", False),
        "descriptor_scan_count": max(1, descriptor_scan_count),
        "root_descriptor_table_count": len(event.get("root_descriptor_tables", {})),
        "shader_binding_counts": shader_binding_counts,
        "database_hit": database_hit,
        "database_path": database_path,
        "query_mode": query_mode,
        "fallback_reason": fallback_reason,
        "reason": None if bound_resources else "No bound descriptor resources were resolved for the event.",
    }


def _get_event_resource_from_index(
    index: dict[str, Any],
    export_dir: str | Path,
    global_id: int | str,
    descriptor_scan_count: int,
    pdb_search_paths: list[str] | None,
    refresh: bool,
    *,
    stage: str,
) -> dict[str, Any]:
    event = _get_event(index, global_id, stage)
    database_resources, database_miss_reason = _database_event_resources(index, global_id, pdb_search_paths, refresh)
    if database_resources:
        return {
            "status": "success",
            "event": event,
            "resources": database_resources,
            "diagnostics": _resource_diagnostics(
                index,
                event,
                descriptor_scan_count,
                database_resources,
                {},
                database_hit=True,
                query_mode="sqlite",
            ),
        }

    if event.get("shader_stage_group") == "graphics_or_indirect":
        shader_bindings_by_stage = _resolve_shader_bindings_by_stage(export_dir, global_id, pdb_search_paths, refresh)
    else:
        shader_bindings_by_stage = {}
    if shader_bindings_by_stage:
        shader_bindings = {"CBV": [], "SRV": [], "UAV": [], "Sampler": []}
        bound_resources = [
            *_resolve_input_assembler_resources(index, event),
            *_resolve_graphics_shader_resources(index, event, shader_bindings_by_stage, descriptor_scan_count=max(1, descriptor_scan_count)),
            *_resolve_output_merger_resources(index, event),
        ]
        shader_binding_counts: dict[str, Any] = {stage: {key: len(value) for key, value in bindings.items()} for stage, bindings in shader_bindings_by_stage.items()}
    else:
        shader_bindings = _resolve_shader_bindings(export_dir, global_id, pdb_search_paths, refresh)
        bound_resources = _resolve_shader_declared_resources(index, event, shader_bindings, descriptor_scan_count=max(1, descriptor_scan_count))
        shader_binding_counts = {key: len(value) for key, value in shader_bindings.items()}
    if not bound_resources:
        bound_resources = _resolve_bound_resources(index, event, descriptor_scan_count=max(1, descriptor_scan_count))
    _cache_event_resources(index, global_id, bound_resources)
    return {
        "status": "success" if bound_resources else "partial",
        "event": event,
        "resources": bound_resources,
        "diagnostics": _resource_diagnostics(
            index,
            event,
            descriptor_scan_count,
            bound_resources,
            shader_binding_counts,
            database_hit=False,
            query_mode="fallback_scan",
            fallback_reason=database_miss_reason,
        ),
    }


def get_event_resource(
    export_dir: str | Path,
    global_id: int | str,
    descriptor_scan_count: int = DEFAULT_DESCRIPTOR_SCAN_COUNT,
    pdb_search_paths: list[str] | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    index = build_index(export_dir, refresh=refresh)
    return _get_event_resource_from_index(
        index,
        export_dir,
        global_id,
        descriptor_scan_count,
        pdb_search_paths,
        refresh,
        stage="resource",
    )


def _resource_access_history_from_index(
    index: dict[str, Any],
    export_dir: str | Path,
    global_id: int | str,
    resource: str | int,
    descriptor_scan_count: int,
    pdb_search_paths: list[str] | None,
    refresh: bool,
) -> dict[str, Any]:
    events = index["events"]
    events_by_global_id = index.get("events_by_global_id", {})
    event = _get_event(index, global_id, "resource_access_history")
    descriptor_scan_count = max(32, descriptor_scan_count)
    event_resources = _resolve_bound_resources(index, event, descriptor_scan_count=descriptor_scan_count)
    try:
        target = _select_target_resource(event_resources, resource)
    except PixToolError as coarse_error:
        resolved = _get_event_resource_from_index(
            index,
            export_dir,
            global_id,
            descriptor_scan_count,
            pdb_search_paths,
            refresh,
            stage="resource_access_history",
        )
        event_resources = resolved["resources"]
        try:
            target = _select_target_resource(event_resources, resource)
        except PixToolError as resolved_error:
            if coarse_error.code == "resource_not_bound":
                raise resolved_error from coarse_error
            raise

    database_path = _database_path_from_index(index)
    if database_path and not refresh:
        history_resource_ids = load_same_named_resource_ids(database_path, target.get("resource_name"), target.get("resource_id"))
    else:
        history_resource_ids = _same_named_resource_ids(index, target)

    rows: list[dict[str, Any]] = []
    event_positions = {str(item.get("global_id")): position for position, item in enumerate(events)}
    database_hit = False
    database_fallback_reason: str | None = None
    resource_ref_index_hit = False
    shader_event_candidates: list[dict[str, Any]] = []
    shader_events_scanned = 0

    if database_path and not refresh:
        try:
            database_refs = load_resource_references(database_path, history_resource_ids)
            database_shader_accesses = load_resource_shader_accesses(database_path, history_resource_ids)
            database_hit = True
            resource_ref_index_hit = True
            for ref in database_refs:
                item = ref.get("event") or events_by_global_id.get(str(ref.get("global_id")))
                if item is None:
                    continue
                resource_id = str(ref.get("resource_id"))
                text = str(ref.get("text") or "")
                row_target = _access_row_target_for_resource_id(index, target, resource_id)
                rows.append(
                    _make_access_row(
                        item,
                        row_target,
                        binding=_binding_for_resource_ref(text, resource_id),
                        read_write=_read_write_for_text(text, resource_id),
                        states=_states_for_text(text, resource_id, "STATE_COMMON"),
                        resource_ref=ref,
                    )
                )
            for access in database_shader_accesses:
                item = access.get("event") or {}
                bound_resource = access.get("resource") or {}
                if str(bound_resource.get("resource_id") or "") not in history_resource_ids:
                    continue
                if bound_resource.get("view_type") not in {"SRV", "UAV"}:
                    continue
                shader_event_candidates.append(item)
                rows.append(
                    _make_access_row(
                        item,
                        bound_resource,
                        binding=_shader_binding(bound_resource),
                        read_write=_read_write_for_text("", str(bound_resource.get("resource_id")), bound_resource.get("view_type")),
                        states=_shader_state(bound_resource),
                    )
                )
        except Exception as exc:
            database_hit = False
            database_fallback_reason = str(exc)
            rows = []
            shader_event_candidates = []

    if not database_hit:
        refs_by_resource_id = index.get("resource_refs_by_resource_id", {})
        resource_ref_index_hit = bool(refs_by_resource_id)
        if refs_by_resource_id:
            indexed_refs: list[tuple[int, int, str, dict[str, Any]]] = []
            for resource_id in history_resource_ids:
                for ref in refs_by_resource_id.get(str(resource_id), []):
                    event_position = event_positions.get(str(ref.get("global_id")))
                    if event_position is None:
                        continue
                    line = int(ref.get("line") or 0)
                    indexed_refs.append((event_position, line, str(resource_id), ref))
            for _, _, resource_id, ref in sorted(indexed_refs, key=lambda item: (item[0], item[1])):
                item = events_by_global_id.get(str(ref.get("global_id")))
                if item is None:
                    continue
                text = str(ref.get("text") or "")
                row_target = _access_row_target_for_resource_id(index, target, resource_id)
                rows.append(
                    _make_access_row(
                        item,
                        row_target,
                        binding=_binding_for_resource_ref(text, resource_id),
                        read_write=_read_write_for_text(text, resource_id),
                        states=_states_for_text(text, resource_id, "STATE_COMMON"),
                        resource_ref=ref,
                    )
                )
        else:
            for item in events:
                for ref in item.get("resource_refs", []):
                    text = str(ref.get("text") or "")
                    ids = {match.group(1) for match in RESOURCE_ID_RE.finditer(text)}
                    matched_resource_id = _first_matching_resource_id(history_resource_ids, ids)
                    if matched_resource_id is None:
                        continue
                    row_target = _access_row_target_for_resource_id(index, target, matched_resource_id)
                    rows.append(
                        _make_access_row(
                            item,
                            row_target,
                            binding=_binding_for_resource_ref(text, matched_resource_id),
                            read_write=_read_write_for_text(text, matched_resource_id),
                            states=_states_for_text(text, matched_resource_id, "STATE_COMMON"),
                            resource_ref=ref,
                        )
                    )

        coarse_resources_by_global_id: dict[str, list[dict[str, Any]]] = {}
        for item in events:
            if not item.get("is_shader_event"):
                continue
            if str(item.get("global_id")) == str(global_id):
                shader_event_candidates.append(item)
                coarse_resources_by_global_id[str(item.get("global_id"))] = event_resources
                continue
            coarse_resources = _resolve_bound_resources(index, item, descriptor_scan_count=descriptor_scan_count)
            matching_resources = [
                bound_resource
                for bound_resource in coarse_resources
                if str(bound_resource.get("resource_id") or "") in history_resource_ids
                and bound_resource.get("view_type") in {"SRV", "UAV"}
            ]
            if matching_resources:
                shader_event_candidates.append(item)
                coarse_resources_by_global_id[str(item.get("global_id"))] = matching_resources

        for item in shader_event_candidates:
            shader_events_scanned += 1
            item_resources = coarse_resources_by_global_id.get(str(item.get("global_id")), [])
            for bound_resource in item_resources:
                if str(bound_resource.get("resource_id") or "") not in history_resource_ids:
                    continue
                if bound_resource.get("view_type") not in {"SRV", "UAV"}:
                    continue
                rows.append(
                    _make_access_row(
                        item,
                        bound_resource,
                        binding=_shader_binding(bound_resource),
                        read_write=_read_write_for_text("", str(bound_resource.get("resource_id")), bound_resource.get("view_type")),
                        states=_shader_state(bound_resource),
                    )
                )

    unique_shader_candidates = {str(item.get("global_id")) for item in shader_event_candidates if item}
    rows.sort(key=lambda row: (event_positions.get(str(row.get("global_id")), len(events)), int(row.get("line") or 0)))
    rows = _dedupe_access_rows(rows)
    return {
        "status": "success" if rows else "partial",
        "event": event,
        "resource": target,
        "access_history": rows,
        "diagnostics": {
            "cache_hit": index.get("cache_hit", False),
            "descriptor_scan_count": descriptor_scan_count,
            "access_count": len(rows),
            "shader_event_candidate_count": len(unique_shader_candidates),
            "shader_event_scan_count": shader_events_scanned,
            "resource_ref_index_hit": resource_ref_index_hit,
            "database_hit": database_hit,
            "database_path": database_path,
            "query_mode": "sqlite" if database_hit else "fallback_scan",
            "fallback_reason": database_fallback_reason,
            "reason": None if rows else "No access history rows were resolved for the selected resource.",
        },
    }


def get_resource_access_history(
    export_dir: str | Path,
    global_id: int | str,
    resource: str | int,
    descriptor_scan_count: int = DEFAULT_DESCRIPTOR_SCAN_COUNT,
    pdb_search_paths: list[str] | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    index = build_index(export_dir, refresh=refresh)
    return _resource_access_history_from_index(index, export_dir, global_id, resource, descriptor_scan_count, pdb_search_paths, refresh)




def get_event_resource_history(
    export_dir: str | Path,
    global_id: int | str,
    window: int = 25,
    refresh: bool = False,
) -> dict[str, Any]:
    index = build_index(export_dir, refresh=refresh)
    events = index["events"]
    event = _get_event(index, global_id, "resource_history")

    position = next((i for i, item in enumerate(events) if str(item.get("global_id")) == str(global_id)), -1)
    start = max(0, position - window) if position >= 0 else 0
    end = min(len(events), position + window + 1) if position >= 0 else len(events)
    nearby = events[start:end]

    direct_refs = event.get("resource_refs", [])
    pso_id = event.get("pso_id")
    related = []
    for item in nearby:
        refs = item.get("resource_refs", [])
        if refs or (pso_id and item.get("pso_id") == pso_id):
            related.append(
                {
                    "global_id": item.get("global_id"),
                    "name": item.get("name"),
                    "event_type": item.get("event_type"),
                    "pso_id": item.get("pso_id"),
                    "file": item.get("file"),
                    "line": item.get("line"),
                    "resource_refs": refs,
                    "operation_summary": "resource reference or same PSO context",
                }
            )

    return {
        "status": "partial" if not direct_refs else "success",
        "event": event,
        "resource_history": {
            "direct_refs": direct_refs,
            "nearby_related_events": related,
            "window": window,
        },
        "diagnostics": {
            "cache_hit": index.get("cache_hit", False),
            "reason": None if direct_refs else "No direct resource references were found inside the event block; nearby events are returned as context.",
        },
    }
