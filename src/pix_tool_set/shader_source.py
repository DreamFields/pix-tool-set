import json
import subprocess
from pathlib import Path
from typing import Any

from .errors import PixToolError
from .indexer import build_index
from .shader_extractor import extract_debug_name_from_shader_blob, extract_shader_blobs


def _blob_metadata(blob_path: Path) -> dict[str, Any]:
    data = blob_path.read_bytes()
    magic = int.from_bytes(data[:4], "little") if len(data) >= 4 else None
    return {
        "blob_size": len(data),
        "format": "DXBC" if magic == 0x43425844 else f"0x{magic:08X}" if magic is not None else "unknown",
        "debug_name": extract_debug_name_from_shader_blob(data),
    }


def _in_repo_resolver_path() -> Path | None:
    project_root = Path(__file__).resolve().parents[2]
    candidates = [
        project_root / "native" / "pdb_resolver" / "bin" / "pdb-resolver.exe",
        project_root / "native" / "pdb_resolver" / "build" / "pdb-resolver.exe",
        project_root / "native" / "pdb_resolver" / "build" / "Release" / "pdb-resolver.exe",
        project_root / "native" / "pdb_resolver" / "build_vs" / "Release" / "pdb-resolver.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return None


def _find_stage_blobs(index: dict[str, Any], event: dict[str, Any]) -> list[dict[str, Any]]:
    pso_id = event.get("pso_id")
    if not pso_id:
        return []
    pso = index.get("pso_index", {}).get(str(pso_id), {})
    stages: list[dict[str, Any]] = []
    for stage in pso.get("stages", []):
        item = dict(stage)
        blob_path = item.get("blob_path")
        if blob_path and Path(blob_path).exists():
            item.update(_blob_metadata(Path(blob_path)))
            item["has_debug_name"] = bool(item["debug_name"])
        stages.append(item)
    return stages


def _scan_extracted_stage_blobs(export_dir: Path, pso_id: str | int | None) -> list[dict[str, Any]]:
    if not pso_id:
        return []
    shader_dir = export_dir / "extracted_shaders"
    if not shader_dir.exists():
        return []
    stages: list[dict[str, Any]] = []
    for blob in sorted(shader_dir.glob(f"pso_{pso_id}_*.cso")):
        stage = blob.stem.rsplit("_", 1)[-1]
        item = {"stage": stage, "blob_path": str(blob.resolve())}
        item.update(_blob_metadata(blob))
        item["has_debug_name"] = bool(item["debug_name"])
        stages.append(item)
    return stages


def _run_shader_extractor(export_dir: Path, pso_id: str | int) -> dict[str, Any]:
    try:
        result = extract_shader_blobs(export_dir, pso_id=pso_id)
    except Exception as exc:
        return {"status": "failed", "reason": str(exc), "result": None}
    return {"status": "success", "result": result}


def _ensure_stage_blobs(export_dir: Path, index: dict[str, Any], event: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    pso_id = event.get("pso_id")
    extraction: dict[str, Any] = {"status": "not_needed", "reason": "stage blobs were already indexed"}
    stages = _find_stage_blobs(index, event)
    if stages:
        return stages, extraction

    stages = _scan_extracted_stage_blobs(export_dir, pso_id)
    if stages:
        return stages, {"status": "not_needed", "reason": "stage blobs were found in extracted_shaders"}

    if not pso_id:
        return [], {"status": "not_run", "reason": "event has no pso_id"}

    extraction = _run_shader_extractor(export_dir, pso_id)
    stages = _scan_extracted_stage_blobs(export_dir, pso_id)
    if extraction.get("result"):
        extracted = []
        for shader in extraction["result"].get("shaders", []):
            output_file = shader.get("output_file")
            if output_file:
                extracted.append(
                    {
                        "stage": shader.get("stage"),
                        "blob_path": str(Path(output_file).resolve()),
                        "blob_size": shader.get("blob_size"),
                        "format": shader.get("format"),
                        "debug_name": shader.get("debug_name"),
                        "has_debug_name": shader.get("has_debug_name"),
                    }
                )
        if extracted:
            stages = extracted
    return stages, extraction


def _find_pdb_by_debug_name(debug_name: str, pdb_search_paths: list[str]) -> str | None:
    if not debug_name:
        return None
    pdb_name = debug_name if debug_name.lower().endswith(".pdb") else debug_name + ".pdb"
    for search_path in pdb_search_paths:
        path = Path(search_path)
        if path.is_file() and path.name == pdb_name:
            return str(path.resolve())
        if not path.is_dir():
            continue
        direct = path / pdb_name
        if direct.exists():
            return str(direct.resolve())
        try:
            for candidate in path.rglob(pdb_name):
                if candidate.is_file():
                    return str(candidate.resolve())
        except OSError:
            continue
    return None


def _inspect_blob(blob_path: str, pdb_search_paths: list[str]) -> dict[str, Any]:
    data = Path(blob_path).read_bytes()
    debug_name = extract_debug_name_from_shader_blob(data)
    pdb_path = _find_pdb_by_debug_name(debug_name, pdb_search_paths)
    return {
        "status": "pdb_found" if pdb_path else "pdb_not_found",
        "debug_name": debug_name,
        "pdb_path": pdb_path,
        "reason": None if pdb_path else "PDB resolver is not bundled yet; found no matching PDB in search paths." if debug_name else "No debug name found in shader blob.",
        "pdb_search_paths": pdb_search_paths,
    }


def _run_resolver(resolver_path: Path, blob_path: str, pdb_search_paths: list[str]) -> dict[str, Any]:
    cmd = [str(resolver_path), "resolve-blob", blob_path, "--pdb-paths=" + ";".join(pdb_search_paths)]
    completed = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    parsed: dict[str, Any] | None = None
    if completed.stdout.strip():
        try:
            parsed = json.loads(completed.stdout)
        except json.JSONDecodeError:
            parsed = None
    return {
        "command": cmd,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "result": parsed,
    }


def get_event_shader_source(
    export_dir: str | Path,
    global_id: int | str,
    pdb_search_paths: list[str] | None = None,
    resolver_path: str | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    index = build_index(export_dir, refresh=refresh)
    event = index["events_by_global_id"].get(str(global_id))
    if event is None:
        raise PixToolError(
            code="event_not_found",
            message=f"Global ID was not found: {global_id}",
            stage="shader_source",
            suggestion="Run extract-shader-events-tree and choose a shader event global id.",
        )
    if not event.get("is_shader_event"):
        raise PixToolError(
            code="event_is_not_shader_event",
            message=f"Global ID does not execute shader work: {global_id}",
            stage="shader_source",
            suggestion="Use a global id marked is_shader_event=true.",
            details={"event": event},
        )

    search_paths = pdb_search_paths or [str(Path(export_dir).resolve())]
    export_root = Path(export_dir).resolve()
    stages, extraction = _ensure_stage_blobs(export_root, index, event)
    resolved: list[dict[str, Any]] = []
    resolver = Path(resolver_path).resolve() if resolver_path else _in_repo_resolver_path()

    for stage in stages:
        item = dict(stage)
        blob_path = item.get("blob_path")
        if resolver and resolver.exists() and blob_path:
            item["resolver_result"] = _run_resolver(resolver, blob_path, search_paths)
        elif blob_path:
            item["resolver_result"] = _inspect_blob(blob_path, search_paths)
        else:
            item["resolver_result"] = {
                "status": "not_run",
                "reason": "shader blob path is missing",
                "pdb_search_paths": search_paths,
            }
        resolved.append(item)

    return {
        "event": event,
        "pso_id": event.get("pso_id"),
        "stages": resolved,
        "diagnostics": {
            "cache_hit": index.get("cache_hit", False),
            "pdb_search_paths": search_paths,
            "resolver_path": str(resolver) if resolver else None,
            "shader_extraction": extraction,
        },
    }
