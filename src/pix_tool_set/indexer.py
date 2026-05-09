from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import PixToolError

INDEX_VERSION = 4
SHADER_CALLS = {
    "Dispatch": "->Dispatch(",
    "DispatchIndirect": "->DispatchIndirect(",
    "DispatchMesh": "->DispatchMesh(",
    "DispatchRays": "->DispatchRays(",
    "DrawInstanced": "->DrawInstanced(",
    "DrawIndexedInstanced": "->DrawIndexedInstanced(",
    "Draw": "->Draw(",
    "DrawIndexed": "->DrawIndexed(",
    "ExecuteIndirect": "->ExecuteIndirect(",
}
GLOBAL_RE = re.compile(r"//\s*GlobalId\s*=\s*(\d+)")
PSO_RE = re.compile(r"SetPipelineState\(GetPipelineState\((\d+)\)\)")
BEGIN_RE = re.compile(r"PIXBeginEvent\([^\n]*?LR?\"\((.*?)\)\"\)")
END_TOKEN = "PIXEndEvent("
RESOURCE_RE = re.compile(r"(GetResource\((\d+)\)|GetDescriptor\((\d+)\)|ResourceBarrier|CopyResource|CopyBufferRegion|CopyTextureRegion|Set(?:Graphics|Compute)Root.*)")
DESCRIPTOR_WRITE_RE = re.compile(
    r"(?P<call>Create(?:ShaderResourceView|UnorderedAccessView|RenderTargetView|DepthStencilView)[A-Za-z0-9_]*)\(.*?"
    r"GetResource\((?P<resource_id>\d+)\).*?"
    r"GetCpuDescriptor\(g_descriptorHeap_(?P<heap_id>\d+)\.Get\(\),\s*(?P<descriptor_index>\d+)\)",
)
SET_NAME_RE = re.compile(r"GetObject\((?P<resource_id>\d+)\)->SetName\(LR?\"\((?P<name>.*?)\)\"\)")
ROOT_DESCRIPTOR_TABLE_RE = re.compile(
    r"Set(?P<stage>Compute|Graphics)RootDescriptorTable\((?P<root_index>\d+),\s*"
    r"GetGpuDescriptor\(g_descriptorHeap_(?P<heap_id>\d+)\.Get\(\),\s*(?P<descriptor_index>\d+)\)\)"
)
ROOT_CBV_RE = re.compile(
    r"Set(?P<stage>Compute|Graphics)RootConstantBufferView\((?P<root_index>\d+),\s*"
    r"GetGpuva\((?P<resource_id>\d+),\s*(?P<offset>\d+)\)\)"
)
ROOT_SIGNATURE_RE = re.compile(r"Set(?P<stage>Compute|Graphics)RootSignature\(GetRootSignature\((?P<root_signature_id>\d+)\)\)")
IBV_DESC_RE = re.compile(
    r"D3D12_INDEX_BUFFER_VIEW\s+\w+\s*\{\s*GetGpuva\((?P<resource_id>\d+),\s*(?P<offset>\d+)\),\s*"
    r"(?P<size>\d+),\s*(?P<format>[^}\s]+)"
)
VBV_DESC_RE = re.compile(
    r"vertexBufferViews\[(?P<slot>\d+)\]\s*=\s*\{\s*GetGpuva\((?P<resource_id>\d+),\s*(?P<offset>\d+)\),\s*"
    r"(?P<size>\d+),\s*(?P<stride>\d+)\s*\}"
)
IA_SET_INDEX_RE = re.compile(r"IASetIndexBuffer\(")
IA_SET_VERTEX_RE = re.compile(r"IASetVertexBuffers\((?P<start_slot>\d+),\s*(?P<count>\d+),")
CREATE_RTV_RE = re.compile(r"CreateRenderTargetView\(GetResource\((?P<resource_id>\d+)\)\.Get\(")
CREATE_DSV_RE = re.compile(r"CreateDepthStencilView\(GetResource\((?P<resource_id>\d+)\)\.Get\(")
OM_SET_RE = re.compile(r"OMSetRenderTargets\((?P<rtv_count>\d+),")


def index_path(export_dir: Path) -> Path:
    return export_dir / ".cache" / "pix-tool-set" / "index.json"


def _source_files(export_dir: Path) -> list[Path]:
    patterns = [
        "CommandLists*.cpp",
        "CreatePSOs.cpp",
        "CreateAndInitResources*.cpp",
        "Descriptors*.cpp",
        "ResourceModifications*.cpp",
        "ModifyDescriptors*.cpp",
        "FrameResources*.cpp",
    ]
    files: list[Path] = []
    for pattern in patterns:
        files.extend(sorted(export_dir.glob(pattern)))
    return files


def _fingerprint(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _fingerprints(files: list[Path]) -> list[dict[str, Any]]:
    return [_fingerprint(path) for path in files if path.exists()]


def _load_cached(export_dir: Path, fingerprints: list[dict[str, Any]]) -> dict[str, Any] | None:
    path = index_path(export_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if payload.get("version") != INDEX_VERSION:
        return None
    if payload.get("fingerprints") != fingerprints:
        return None
    return payload


def _write_cached(export_dir: Path, payload: dict[str, Any]) -> None:
    path = index_path(export_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _stage_group(event_type: str | None) -> str | None:
    if event_type in {"Dispatch", "DispatchIndirect"}:
        return "compute"
    if event_type == "DispatchRays":
        return "raytracing"
    if event_type:
        return "graphics_or_indirect"
    return None


def _parse_command_lists(files: list[Path]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    by_gid: dict[str, dict[str, Any]] = {}
    marker_stack: list[dict[str, str]] = []
    current_pso: str | None = None
    current_root_tables: dict[str, dict[str, str]] = {}
    current_root_cbvs: dict[str, dict[str, str]] = {}
    current_ia: dict[str, Any] = {"vertex_buffers": [], "index_buffer": None}
    current_om: dict[str, Any] = {"render_targets": [], "depth_stencil": None}
    pending_ibv: dict[str, Any] | None = None
    pending_vbvs: dict[int, dict[str, Any]] = {}
    pending_rtvs: list[dict[str, Any]] = []
    pending_dsv: dict[str, Any] | None = None
    current: dict[str, Any] | None = None

    def finish_current() -> None:
        nonlocal current
        if current is None:
            return
        current.setdefault("name", current.get("event_type") or "Unknown")
        current.setdefault("is_shader_event", current.get("event_type") in SHADER_CALLS)
        current.setdefault("shader_stage_group", _stage_group(current.get("event_type")))
        events.append(current)
        by_gid[str(current["global_id"])] = current
        current = None

    for path in files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for index, line in enumerate(lines, start=1):
            match = GLOBAL_RE.search(line)
            if match:
                finish_current()
                current = {
                    "global_id": match.group(1),
                    "file": str(path),
                    "line": index,
                    "parent_global_id": marker_stack[-1]["global_id"] if marker_stack else None,
                    "marker_path": [item["name"] for item in marker_stack],
                    "pso_id": current_pso,
                    "root_descriptor_tables": dict(current_root_tables),
                    "root_constant_buffer_views": dict(current_root_cbvs),
                    "input_assembler": dict(current_ia),
                    "output_merger": dict(current_om),
                    "resource_refs": [],
                    "calls": [],
                }
                continue

            ibv_desc = IBV_DESC_RE.search(line)
            if ibv_desc:
                pending_ibv = {
                    "stage": "IA",
                    "slot": None,
                    "resource_id": ibv_desc.group("resource_id"),
                    "offset": ibv_desc.group("offset"),
                    "size": ibv_desc.group("size"),
                    "format": ibv_desc.group("format"),
                    "line": index,
                    "text": line.strip(),
                }

            vbv_desc = VBV_DESC_RE.search(line)
            if vbv_desc:
                pending_vbvs[int(vbv_desc.group("slot"))] = {
                    "stage": "IA",
                    "slot": int(vbv_desc.group("slot")),
                    "resource_id": vbv_desc.group("resource_id"),
                    "offset": vbv_desc.group("offset"),
                    "size": vbv_desc.group("size"),
                    "stride": vbv_desc.group("stride"),
                    "line": index,
                    "text": line.strip(),
                }

            if IA_SET_INDEX_RE.search(line) and pending_ibv is not None:
                current_ia["index_buffer"] = dict(pending_ibv, line=index, text=line.strip())
                if current is not None:
                    current["input_assembler"] = dict(current_ia)
                    current["calls"].append({"line": index, "text": line.strip(), "kind": "IASetIndexBuffer"})

            ia_vertices = IA_SET_VERTEX_RE.search(line)
            if ia_vertices:
                start_slot = int(ia_vertices.group("start_slot"))
                count = int(ia_vertices.group("count"))
                current_ia["vertex_buffers"] = [
                    dict(pending_vbvs[slot], slot=slot - start_slot)
                    for slot in range(start_slot, start_slot + count)
                    if slot in pending_vbvs and pending_vbvs[slot].get("resource_id") != "0"
                ]
                if current is not None:
                    current["input_assembler"] = dict(current_ia)
                    current["calls"].append({"line": index, "text": line.strip(), "kind": "IASetVertexBuffers"})

            rtv = CREATE_RTV_RE.search(line)
            if rtv:
                pending_rtvs.append({"stage": "OM", "resource_id": rtv.group("resource_id"), "line": index, "text": line.strip()})

            dsv = CREATE_DSV_RE.search(line)
            if dsv:
                pending_dsv = {"stage": "OM", "resource_id": dsv.group("resource_id"), "line": index, "text": line.strip()}

            om = OM_SET_RE.search(line)
            if om:
                rtv_count = int(om.group("rtv_count"))
                current_om = {
                    "render_targets": [dict(item, slot=slot) for slot, item in enumerate(pending_rtvs[-rtv_count:])] if rtv_count > 0 else [],
                    "depth_stencil": dict(pending_dsv) if pending_dsv is not None else None,
                }
                if current is not None:
                    current["output_merger"] = dict(current_om)
                    current["calls"].append({"line": index, "text": line.strip(), "kind": "OMSetRenderTargets"})

            root_table = ROOT_DESCRIPTOR_TABLE_RE.search(line)
            if root_table:
                root_binding = {
                    "stage": root_table.group("stage"),
                    "root_index": root_table.group("root_index"),
                    "heap_id": root_table.group("heap_id"),
                    "descriptor_index": root_table.group("descriptor_index"),
                    "line": index,
                    "text": line.strip(),
                }
                current_root_tables[root_binding["root_index"]] = root_binding
                if current is not None:
                    current["root_descriptor_tables"] = dict(current_root_tables)
                    current["calls"].append({"line": index, "text": line.strip(), "kind": "SetRootDescriptorTable"})

            root_cbv = ROOT_CBV_RE.search(line)
            if root_cbv:
                root_binding = {
                    "stage": root_cbv.group("stage"),
                    "root_index": root_cbv.group("root_index"),
                    "resource_id": root_cbv.group("resource_id"),
                    "offset": root_cbv.group("offset"),
                    "line": index,
                    "text": line.strip(),
                }
                current_root_cbvs[root_binding["root_index"]] = root_binding
                if current is not None:
                    current["root_constant_buffer_views"] = dict(current_root_cbvs)
                    current["calls"].append({"line": index, "text": line.strip(), "kind": "SetRootConstantBufferView"})

            root_signature = ROOT_SIGNATURE_RE.search(line)
            if root_signature:
                current_root_tables.clear()
                current_root_cbvs.clear()
                pending_vbvs.clear()
                if current is not None:
                    current["root_descriptor_tables"] = {}
                    current["root_constant_buffer_views"] = {}
                    current["calls"].append(
                        {
                            "line": index,
                            "text": line.strip(),
                            "kind": "SetRootSignature",
                            "root_signature_id": root_signature.group("root_signature_id"),
                            "stage": root_signature.group("stage"),
                        }
                    )

            pso = PSO_RE.search(line)
            if pso:
                current_pso = pso.group(1)
                if current is not None:
                    current["pso_id"] = current_pso
                    current["calls"].append({"line": index, "text": line.strip(), "kind": "SetPipelineState"})

            begin = BEGIN_RE.search(line)
            if begin:
                name = begin.group(1)
                marker_gid = str(current["global_id"]) if current is not None else f"marker:{path.name}:{index}"
                if current is not None:
                    current["name"] = name
                    current["event_type"] = "PIXBeginEvent"
                    current["is_shader_event"] = False
                marker_stack.append({"global_id": marker_gid, "name": name})

            if END_TOKEN in line and marker_stack:
                marker_stack.pop()

            if current is not None:
                for event_type, token in SHADER_CALLS.items():
                    if token in line:
                        current["event_type"] = event_type
                        current["name"] = event_type
                        current["is_shader_event"] = True
                        current["shader_stage_group"] = _stage_group(event_type)
                        current["calls"].append({"line": index, "text": line.strip(), "kind": event_type})
                        break
                resource = RESOURCE_RE.search(line)
                if resource:
                    current["resource_refs"].append({"line": index, "text": line.strip()})
        finish_current()
    return events, by_gid


def _parse_resource_names(files: list[Path]) -> dict[str, dict[str, Any]]:
    resources: dict[str, dict[str, Any]] = {}
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for index, line in enumerate(lines, start=1):
            match = SET_NAME_RE.search(line)
            if not match:
                continue
            resource_id = match.group("resource_id")
            resources[resource_id] = {
                "resource_id": resource_id,
                "name": match.group("name"),
                "file": str(path),
                "line": index,
            }
    return resources


def _descriptor_kind(call: str) -> str:
    if "UnorderedAccessView" in call:
        return "UAV"
    if "ShaderResourceView" in call:
        return "SRV"
    if "RenderTargetView" in call:
        return "RTV"
    if "DepthStencilView" in call:
        return "DSV"
    return "UNKNOWN"


def _parse_descriptors(files: list[Path]) -> dict[str, list[dict[str, Any]]]:
    descriptors: dict[str, list[dict[str, Any]]] = {}
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for index, line in enumerate(lines, start=1):
            match = DESCRIPTOR_WRITE_RE.search(line)
            if not match:
                continue
            descriptor_index = match.group("descriptor_index")
            call = match.group("call")
            descriptors.setdefault(descriptor_index, []).append(
                {
                    "descriptor_index": descriptor_index,
                    "heap_id": match.group("heap_id"),
                    "resource_id": match.group("resource_id"),
                    "view_type": _descriptor_kind(call),
                    "call": call,
                    "file": str(path),
                    "line": index,
                    "text": line.strip(),
                }
            )
    return descriptors


def _parse_pso_files(export_dir: Path) -> dict[str, Any]:
    pso_file = export_dir / "CreatePSOs.cpp"
    pso_index: dict[str, Any] = {}
    shader_dir = export_dir / "extracted_shaders"
    if shader_dir.exists():
        for blob in sorted(shader_dir.glob("pso_*_*.cso")):
            match = re.match(r"pso_(\d+)_([A-Za-z0-9]+)\.cso", blob.name)
            if not match:
                continue
            pso_id, stage = match.groups()
            entry = pso_index.setdefault(pso_id, {"pso_id": pso_id, "source_file": str(pso_file), "stages": []})
            entry["stages"].append({"stage": stage, "blob_path": str(blob)})
    return pso_index


def build_index(export_dir: str | Path, refresh: bool = False) -> dict[str, Any]:
    root = Path(export_dir).resolve()
    if not root.exists():
        raise PixToolError(code="export_dir_not_found", message=f"Export directory does not exist: {root}", stage="index", paths=[str(root)])
    files = _source_files(root)
    fingerprints = _fingerprints(files)
    if not refresh:
        cached = _load_cached(root, fingerprints)
        if cached is not None:
            cached["cache_hit"] = True
            return cached
    command_files = [path for path in files if path.name.startswith("CommandLists")]
    descriptor_files = [path for path in files if path.name.startswith("Descriptors") or path.name.startswith("ModifyDescriptors")]
    resource_name_files = [path for path in files if path.name.startswith("FrameResources")]
    events, by_gid = _parse_command_lists(command_files)
    shader_events = [event for event in events if event.get("is_shader_event")]
    payload = {
        "version": INDEX_VERSION,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "export_dir": str(root),
        "fingerprints": fingerprints,
        "events": events,
        "events_by_global_id": by_gid,
        "shader_event_global_ids": [event["global_id"] for event in shader_events],
        "pso_index": _parse_pso_files(root),
        "descriptor_index": _parse_descriptors(descriptor_files),
        "resource_names": _parse_resource_names(resource_name_files),
        "diagnostics": {"source_file_count": len(files), "event_count": len(events), "shader_event_count": len(shader_events)},
        "cache_hit": False,
    }
    _write_cached(root, payload)
    return payload
