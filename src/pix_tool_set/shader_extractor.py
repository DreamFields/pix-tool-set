from __future__ import annotations

import ctypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import PixToolError

COMPRESS_ALGORITHM_XPRESS = 3


@dataclass(frozen=True)
class ShaderReadInfo:
    pso_id: int
    compressed_size: int
    shader_stage: str
    blob_offset: int
    blob_size: int


@dataclass(frozen=True)
class ResourceReadEntry:
    pso_id: int
    compressed_size: int
    context: str


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _read_u32(data: bytes, offset: int = 0) -> int:
    return int.from_bytes(data[offset : offset + 4], "little")


def _read_u16(data: bytes, offset: int = 0) -> int:
    return int.from_bytes(data[offset : offset + 2], "little")


def extract_debug_name_from_shader_blob(data: bytes) -> str:
    if len(data) < 32 or _read_u32(data) != 0x43425844:
        return ""
    total_size = _read_u32(data, 24)
    part_count = _read_u32(data, 28)
    if total_size > len(data) or part_count > 100:
        return ""

    shader_debug_name_fourcc = 0x4E424453
    for index in range(part_count):
        offset_pos = 32 + index * 4
        if offset_pos + 4 > len(data):
            break
        part_offset = _read_u32(data, offset_pos)
        if part_offset + 8 > len(data):
            continue
        fourcc = _read_u32(data, part_offset)
        part_size = _read_u32(data, part_offset + 4)
        part_data_offset = part_offset + 8
        if part_data_offset + part_size > len(data):
            continue
        if fourcc == shader_debug_name_fourcc and part_size >= 4:
            name_len = _read_u16(data, part_data_offset + 2)
            if 0 < name_len <= part_size - 4:
                name = data[part_data_offset + 4 : part_data_offset + 4 + name_len]
                return name.rstrip(b"\0").decode("utf-8", errors="replace")

    pdb_pos = data.find(b".pdb")
    if pdb_pos > 0:
        start = pdb_pos
        while start > 0 and data[start - 1] != 0 and data[start - 1] >= 0x20:
            start -= 1
        if start < pdb_pos:
            return data[start : pdb_pos + 4].decode("utf-8", errors="replace")
    return ""


def _shader_format(data: bytes) -> str:
    if len(data) < 4:
        return "unknown"
    magic = _read_u32(data)
    if magic == 0x43425844:
        return "DXBC"
    return f"0x{magic:08X}"


def _hex_preview(data: bytes) -> str:
    return data[:16].hex()


def parse_create_psos(create_psos_path: Path) -> list[ShaderReadInfo]:
    content = _read_text(create_psos_path)
    func_re = re.compile(r"void\s+CreatePipelineState_(\d+)\s*\(\)")
    read_re = re.compile(r"g_resourceReader->Read\(data,\s*(\d+)\)")
    shader_re = re.compile(
        r"pssDesc\.(VS|PS|CS|GS|HS|DS)\s*=\s*\{\s*reinterpret_cast<BYTE\*>\(&data\[(\d+)\]\)\s*,\s*(\d+)\s*\}"
    )
    fallback_shader_re = re.compile(r"pssDesc\.(VS|PS|CS|GS|HS|DS)\s*=\s*\{[^,]*,\s*(\d+)\s*\}")

    functions = [(int(match.group(1)), match.start()) for match in func_re.finditer(content)]
    results: list[ShaderReadInfo] = []
    for index, (pso_id, start) in enumerate(functions):
        end = functions[index + 1][1] if index + 1 < len(functions) else len(content)
        block = content[start:end]
        reads = [int(match.group(1)) for match in read_re.finditer(block)]
        if not reads:
            continue
        compressed_size = reads[0]
        shaders = [
            (match.group(1), int(match.group(2)), int(match.group(3)))
            for match in shader_re.finditer(block)
        ]
        if not shaders:
            offset = 0
            for match in fallback_shader_re.finditer(block):
                blob_size = int(match.group(2))
                shaders.append((match.group(1), offset, blob_size))
                offset += blob_size
        for stage, blob_offset, blob_size in shaders:
            results.append(
                ShaderReadInfo(
                    pso_id=pso_id,
                    compressed_size=compressed_size,
                    shader_stage=stage,
                    blob_offset=blob_offset,
                    blob_size=blob_size,
                )
            )
    return results


def _collect_read_calls(path: Path) -> list[int]:
    if not path.exists():
        return []
    read_re = re.compile(r"g_resourceReader->Read\(\w+,\s*(\d+)\)")
    return [int(match.group(1)) for match in read_re.finditer(_read_text(path))]


def _parse_pso_read_sizes(create_psos_path: Path) -> dict[int, list[int]]:
    if not create_psos_path.exists():
        return {}
    content = _read_text(create_psos_path)
    func_re = re.compile(r"void\s+CreatePipelineState_(\d+)\s*\(\)")
    read_re = re.compile(r"g_resourceReader->Read\(data,\s*(\d+)\)")
    functions = [(int(match.group(1)), match.start()) for match in func_re.finditer(content)]
    result: dict[int, list[int]] = {}
    for index, (pso_id, start) in enumerate(functions):
        end = functions[index + 1][1] if index + 1 < len(functions) else len(content)
        block = content[start:end]
        sizes = [int(match.group(1)) for match in read_re.finditer(block)]
        if sizes:
            result[pso_id] = sizes
    return result


def build_global_read_order(export_dir: Path) -> list[ResourceReadEntry]:
    frame_resources = export_dir / "FrameResources_000.cpp"
    if not frame_resources.exists():
        return []

    init_reads = _collect_read_calls(export_dir / "CreateAndInitResources_000.cpp")
    pso_read_sizes = _parse_pso_read_sizes(export_dir / "CreatePSOs.cpp")
    init_call_re = re.compile(r"CreateAndInitResources_000\(\)")
    pso_call_re = re.compile(r"CreatePipelineState_(\d+)\(\)")
    direct_read_re = re.compile(r"g_resourceReader->Read\(\w+,\s*(\d+)\)")

    entries: list[ResourceReadEntry] = []
    init_inlined = False
    for line in _read_text(frame_resources).splitlines():
        if not init_inlined and init_call_re.search(line):
            entries.extend(ResourceReadEntry(-1, size, "CreateAndInitResources") for size in init_reads)
            init_inlined = True
            continue
        pso_call = pso_call_re.search(line)
        if pso_call:
            pso_id = int(pso_call.group(1))
            entries.extend(ResourceReadEntry(pso_id, size, f"PSO_{pso_id}") for size in pso_read_sizes.get(pso_id, []))
            continue
        direct_read = direct_read_re.search(line)
        if direct_read:
            entries.append(ResourceReadEntry(-1, int(direct_read.group(1)), "FrameResources_000"))
    return entries


class XpressDecompressor:
    def __init__(self) -> None:
        self._cabinet = ctypes.WinDLL("cabinet")
        self._handle = ctypes.c_void_p()
        if not self._cabinet.CreateDecompressor(COMPRESS_ALGORITHM_XPRESS, None, ctypes.byref(self._handle)):
            raise OSError("CreateDecompressor(COMPRESS_ALGORITHM_XPRESS) failed")

    def close(self) -> None:
        if self._handle:
            self._cabinet.CloseDecompressor(self._handle)
            self._handle = ctypes.c_void_p()

    def decompress(self, compressed: bytes) -> bytes:
        needed = ctypes.c_size_t(0)
        src = ctypes.create_string_buffer(compressed)
        self._cabinet.Decompress(self._handle, src, len(compressed), None, 0, ctypes.byref(needed))
        if needed.value <= 0:
            return b""
        dst = ctypes.create_string_buffer(needed.value)
        actual = ctypes.c_size_t(0)
        ok = self._cabinet.Decompress(self._handle, src, len(compressed), dst, needed.value, ctypes.byref(actual))
        if not ok:
            return b""
        return dst.raw[: actual.value]

    def __enter__(self) -> "XpressDecompressor":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


def extract_shader_blobs(
    export_dir: str | Path,
    output_dir: str | Path | None = None,
    pso_id: int | str | None = None,
) -> dict[str, Any]:
    root = Path(export_dir).resolve()
    create_psos_path = root / "CreatePSOs.cpp"
    resources_bin_path = root / "resources.bin"
    if not create_psos_path.exists():
        raise PixToolError(code="create_psos_not_found", message=f"CreatePSOs.cpp not found: {create_psos_path}", stage="shader_extractor")
    if not resources_bin_path.exists():
        raise PixToolError(code="resources_bin_not_found", message=f"resources.bin not found: {resources_bin_path}", stage="shader_extractor")

    target_pso_id = int(pso_id) if pso_id is not None else None
    shader_infos = parse_create_psos(create_psos_path)
    read_entries = build_global_read_order(root)
    if not read_entries:
        raise PixToolError(
            code="resource_read_order_not_found",
            message="No resource read entries found. Check FrameResources_000.cpp call order.",
            stage="shader_extractor",
        )

    by_pso: dict[int, list[ShaderReadInfo]] = {}
    for info in shader_infos:
        by_pso.setdefault(info.pso_id, []).append(info)

    output_root = Path(output_dir).resolve() if output_dir else root / "extracted_shaders"
    output_root.mkdir(parents=True, exist_ok=True)
    shaders: list[dict[str, Any]] = []

    with resources_bin_path.open("rb") as resource_file, XpressDecompressor() as decompressor:
        for entry in read_entries:
            compressed = resource_file.read(entry.compressed_size)
            if len(compressed) != entry.compressed_size:
                break
            if entry.pso_id < 0:
                continue
            if target_pso_id is not None and entry.pso_id != target_pso_id:
                continue
            decompressed = decompressor.decompress(compressed)
            if not decompressed:
                continue
            for info in by_pso.get(entry.pso_id, []):
                if target_pso_id is not None and info.pso_id != target_pso_id:
                    continue
                end = info.blob_offset + info.blob_size
                if end > len(decompressed):
                    continue
                blob = decompressed[info.blob_offset:end]
                out_path = output_root / f"pso_{info.pso_id}_{info.shader_stage}.cso"
                out_path.write_bytes(blob)
                debug_name = extract_debug_name_from_shader_blob(blob)
                shaders.append(
                    {
                        "pso_id": info.pso_id,
                        "stage": info.shader_stage,
                        "blob_size": info.blob_size,
                        "compressed_size": entry.compressed_size,
                        "format": _shader_format(blob),
                        "debug_name": debug_name,
                        "has_debug_name": bool(debug_name),
                        "hex_preview": _hex_preview(blob),
                        "output_file": str(out_path),
                    }
                )

    return {
        "export_dir": str(root),
        "total_read_entries": len(read_entries),
        "shaders": shaders,
        "extracted_count": len(shaders),
        "output_dir": str(output_root),
    }
