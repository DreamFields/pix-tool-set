"""Parsers for the C++ project that ``pixtool export-to-cpp`` produces.

Passes:
  1. parse_resources         CreateAndInitResources_*.cpp  -> Resource
  2. parse_descriptors       Descriptors_*.cpp             -> View
  3. parse_pipeline_states   CreatePSOs.cpp                -> PipelineState + Shader
  4. parse_root_signatures   FrameResources_*.cpp          -> RootSignature
  5. CommandListParser       CommandLists_*.cpp            -> DrawCall

The command-list pass is a state machine: it replays the emitted D3D12 calls in
order, tracking the currently bound PSO / root signature / descriptor heaps /
render targets / vertex+index buffers / root arguments, and snapshots that state
at every draw or dispatch.  That snapshot is what PIX shows for a selected draw.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from .model import (
    BindingSlot,
    DrawCall,
    EventKind,
    IndexBufferBinding,
    PipelineState,
    Resource,
    ResourceKind,
    RootParameter,
    RootParameterKind,
    Shader,
    ShaderStage,
    VertexBufferBinding,
    View,
    ViewKind,
)

_NUM = r"(-?[\d.]+)f?"


def _ints(text: str) -> list[int]:
    return [int(x) for x in re.findall(r"-?\d+", text)]


def split_args(text: str) -> list[str]:
    """Split a C++ argument list on top-level commas."""
    out: list[str] = []
    depth = 0
    current: list[str] = []
    for char in text:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        if char == "," and depth == 0:
            out.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if current:
        out.append("".join(current).strip())
    return out


def iter_lines(path: Path) -> Iterator[tuple[int, str]]:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle, 1):
            yield number, line


def sorted_group(root: Path, prefix: str) -> list[Path]:
    exact = root / f"{prefix}.cpp"
    files = sorted(root.glob(f"{prefix}_*.cpp"), key=lambda p: _ints(p.stem) or [0])
    if exact.exists():
        files.insert(0, exact)
    return files


# --------------------------------------------------------------------------
# 1. resources
# --------------------------------------------------------------------------
_RE_RES_FUNC = re.compile(r"^void\s+CreateAndInitResource(?:_Reserved)?_(\d+)\s*\(")
_RE_RES_DESC = re.compile(r"D3D12_RESOURCE_DESC1?\s+\w+\s*=\s*\{(.*)")
_RE_TRACK = re.compile(r"CreateAndTrack(Placed|Committed|Reserved)Resource\d?\s*\((.*)")
_RE_READ = re.compile(r"g_resourceReader->Read\(\s*\w+\s*,\s*(\d+)\s*\)")

_DIMENSION_MAP = {
    "D3D12_RESOURCE_DIMENSION_BUFFER": ResourceKind.BUFFER,
    "D3D12_RESOURCE_DIMENSION_TEXTURE1D": ResourceKind.TEXTURE1D,
    "D3D12_RESOURCE_DIMENSION_TEXTURE2D": ResourceKind.TEXTURE2D,
    "D3D12_RESOURCE_DIMENSION_TEXTURE3D": ResourceKind.TEXTURE3D,
}


def parse_resources(root: Path) -> dict[int, Resource]:
    resources: dict[int, Resource] = {}
    for path in sorted_group(root, "CreateAndInitResources"):
        current: Resource | None = None
        for lineno, line in iter_lines(path):
            match = _RE_RES_FUNC.match(line)
            if match:
                current = Resource(
                    api_id=int(match.group(1)),
                    source_file=path.name,
                    source_line=lineno,
                )
                resources[current.api_id] = current
                continue
            if current is None:
                continue

            match = _RE_RES_DESC.search(line)
            if match:
                _apply_resource_desc(current, match.group(1))
                continue

            match = _RE_TRACK.search(line)
            if match:
                args = split_args(match.group(2).rstrip(");\n "))
                if len(args) >= 4:
                    current.initial_state = args[3].strip()
                if len(args) >= 7:
                    try:
                        current.heap_id = int(args[5])
                        current.heap_offset = int(args[6])
                    except ValueError:
                        pass
                continue

            match = _RE_READ.search(line)
            if match and current.data_blob_index is None:
                # Placeholder: the true index is assigned later, once every
                # Read() across the whole export has been numbered in program
                # order (see collect_resource_reads).
                current.data_blob_index = -1
    return resources


# resources.bin is one sequential XPRESS stream with no index table, so a blob can
# only be addressed by replaying the Read() calls in execution order.
#
# Layout established empirically against Tiled.wpix by chain-decompressing from
# candidate offsets (a wrong order fails on the first blob):
#
#   1. CreatePSOs.cpp                  376 blobs,     8,762,741 bytes
#   2. CreateAndInitResources_00{0,1,2} 3,127 blobs
#   3. FrameResources_000.cpp          1 blob,      236,950,490 bytes
#   4. tail, 231 blobs, 281,342 bytes  -- NOT in file order, see below
#
# The tail is produced by two interleaved sources: ModifyResource_* calls in
# RenderFrameWorker (each preceded by a Read of a named size constant) and Read
# calls sitting *inside* PopulateCommandList_* bodies, which live in
# CommandLists_*.cpp but execute whenever RenderFrameWorker invokes them. Walking
# RenderFrameWorker and splicing in each callee's own reads reproduces the stream
# exactly: 231/231 blobs decompress and the byte total matches the file size.
_INIT_ORDER = (
    "CreatePSOs.cpp",
    "CreateAndInitResources_000.cpp",
    "CreateAndInitResources_001.cpp",
    "CreateAndInitResources_002.cpp",
    "FrameResources_000.cpp",
)

_RE_INIT_FUNC = re.compile(r"^void\s+CreateAndInitResource_(\d+)\s*\(")
_RE_PSO_DEF = re.compile(r"^void\s+CreatePipelineState_(\d+)\s*\(")
_RE_ANY_FUNC = re.compile(r"^void\s+(\w+)\s*\(")
_RE_READ_NAMED = re.compile(r"g_resourceReader->Read\(\s*\w+\s*,\s*([A-Za-z_]\w*)\s*\)")
_RE_CALLEE = re.compile(r"^\s*(PopulateCommandList_\w+|ModifyResource_\w+)\s*\(")
_RE_SIZE_CONST = re.compile(r"static size_t (\w+)\s*=\s*(\d+)")


@dataclass(slots=True)
class ResourceRead:
    """One g_resourceReader->Read call, in global stream order."""

    index: int
    compressed_size: int
    source_file: str
    source_line: int
    owner_id: Optional[int]
    owner_kind: str
    size_symbol: str = ""
    callee: str = ""


def _size_constants(root: Path) -> dict[str, int]:
    """Named blob sizes declared in the generated headers."""
    out: dict[str, int] = {}
    for name in ("ResourceModifications.h", "CapturedAssets.h", "FrameResources.h"):
        path = root / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _RE_SIZE_CONST.finditer(text):
            out[match.group(1)] = int(match.group(2))
    return out


def _reads_per_function(
    root: Path, patterns: tuple[str, ...], sizes: dict[str, int]
) -> dict[str, list[tuple[int, str, int]]]:
    """function name -> [(size, file, line)] for Reads inside its body."""
    out: dict[str, list[tuple[int, str, int]]] = {}
    for pattern in patterns:
        for path in sorted(root.glob(pattern)):
            current: Optional[str] = None
            for lineno, line in iter_lines(path):
                match = _RE_ANY_FUNC.match(line)
                if match:
                    current = match.group(1)
                    continue
                if current is None:
                    continue
                match = _RE_READ.search(line)
                if match:
                    out.setdefault(current, []).append(
                        (int(match.group(1)), path.name, lineno)
                    )
                    continue
                match = _RE_READ_NAMED.search(line)
                if match:
                    value = sizes.get(match.group(1))
                    if value is not None:
                        out.setdefault(current, []).append((value, path.name, lineno))
    return out


def collect_resource_reads(root: Path) -> list[ResourceRead]:
    """Number every Read() call across the export in true stream order.

    Init functions are named after the object they fill
    (`CreateAndInitResource_<resourceId>`), which is what attributes a blob to a
    resource. The frame tail is reconstructed by following RenderFrameWorker's
    call order rather than file order, because its callees consume the same
    cursor.
    """
    reads: list[ResourceRead] = []
    index = 0
    sizes = _size_constants(root)

    for name in _INIT_ORDER:
        path = root / name
        if not path.exists():
            continue
        owner: Optional[int] = None
        if name == "CreatePSOs.cpp":
            kind = "pso"
        elif name.startswith("CreateAndInitResources"):
            kind = "resource"
        else:
            # FrameResources' single large blob is not owned by one resource.
            kind = "frame"
        for lineno, line in iter_lines(path):
            match = _RE_INIT_FUNC.match(line) or _RE_PSO_DEF.match(line)
            if match:
                owner = int(match.group(1))
                continue
            match = _RE_READ.search(line)
            if match:
                reads.append(
                    ResourceRead(
                        index=index,
                        compressed_size=int(match.group(1)),
                        source_file=name,
                        source_line=lineno,
                        owner_id=owner if kind == "resource" else owner,
                        owner_kind=kind,
                    )
                )
                index += 1
                continue
            match = _RE_READ_NAMED.search(line)
            if match:
                value = sizes.get(match.group(1))
                if value is not None:
                    reads.append(
                        ResourceRead(
                            index=index,
                            compressed_size=value,
                            source_file=name,
                            source_line=lineno,
                            owner_id=owner,
                            owner_kind=kind,
                            size_symbol=match.group(1),
                        )
                    )
                    index += 1

    bodies = _reads_per_function(
        root, ("CommandLists_*.cpp", "ResourceModifications_*.cpp"), sizes
    )
    for path in sorted(root.glob("RenderFrameWorker_*.cpp")):
        pending: Optional[str] = None
        for lineno, line in iter_lines(path):
            match = _RE_READ_NAMED.search(line)
            if match and match.group(1) in sizes:
                pending = match.group(1)
                continue
            match = _RE_READ.search(line)
            if match:
                reads.append(
                    ResourceRead(
                        index=index,
                        compressed_size=int(match.group(1)),
                        source_file=path.name,
                        source_line=lineno,
                        owner_id=None,
                        owner_kind="frame",
                    )
                )
                index += 1
                continue
            match = _RE_CALLEE.match(line)
            if not match:
                continue
            callee = match.group(1)
            # The Read that feeds ModifyResource sits just above its call.
            if callee.startswith("ModifyResource_") and pending:
                reads.append(
                    ResourceRead(
                        index=index,
                        compressed_size=sizes[pending],
                        source_file=path.name,
                        source_line=lineno,
                        owner_id=None,
                        owner_kind="modification",
                        size_symbol=pending,
                        callee=callee,
                    )
                )
                index += 1
                pending = None
            for size, file_name, body_line in bodies.get(callee, []):
                reads.append(
                    ResourceRead(
                        index=index,
                        compressed_size=size,
                        source_file=file_name,
                        source_line=body_line,
                        owner_id=None,
                        owner_kind="command_list",
                        callee=callee,
                    )
                )
                index += 1
    return reads


def _apply_resource_desc(resource: Resource, body: str) -> None:
    args = split_args(body.split("};")[0])
    if len(args) < 9:
        return
    resource.kind = _DIMENSION_MAP.get(args[0].strip(), ResourceKind.UNKNOWN)
    try:
        resource.width = int(args[2])
        resource.height = int(args[3])
        resource.depth_or_array_size = int(args[4])
        resource.mip_levels = int(args[5])
        resource.format = args[6].strip()
    except (ValueError, IndexError):
        pass
    sample = re.search(r"\{\s*(\d+)\s*,\s*(\d+)\s*\}", body)
    if sample:
        try:
            resource.sample_count = int(sample.group(1))
        except ValueError:
            pass
    flags = " ".join(a for a in args if "RESOURCE_FLAG" in a)
    resource.flags = flags
    resource.is_render_target = "ALLOW_RENDER_TARGET" in flags
    resource.is_depth_stencil = "ALLOW_DEPTH_STENCIL" in flags
    resource.is_uav = "ALLOW_UNORDERED_ACCESS" in flags


# --------------------------------------------------------------------------
# 2. descriptors
# --------------------------------------------------------------------------
_RE_CPU_DESC = re.compile(
    r"GetCpuDescriptor\(\s*g_descriptorHeap_(\d+)\.Get\(\)\s*,\s*(\d+)\s*\)"
)
_RE_CREATE_VIEW = re.compile(
    r"\b(CreateShaderResourceView|CreateUnorderedAccessView|CreateConstantBufferView"
    r"|CreateRenderTargetView|CreateDepthStencilView|CreateSampler)(_[A-Za-z0-9]+)?\s*\("
)
_RE_GET_RESOURCE = re.compile(r"GetResource\(\s*(\d+)\s*\)")
_RE_GPUVA = re.compile(r"GetGpuva\(\s*(\d+)\s*,\s*(\d+)\s*\)")
_RE_FORMAT = re.compile(r"(DXGI_FORMAT_[A-Z0-9_]+)")
_RE_VIEW_DIM = re.compile(r"(D3D12_(?:SRV|UAV|RTV|DSV)_DIMENSION_[A-Z0-9_]+)")

_VIEW_KIND = {
    "CreateShaderResourceView": ViewKind.SRV,
    "CreateUnorderedAccessView": ViewKind.UAV,
    "CreateConstantBufferView": ViewKind.CBV,
    "CreateRenderTargetView": ViewKind.RTV,
    "CreateDepthStencilView": ViewKind.DSV,
    "CreateSampler": ViewKind.SAMPLER,
}


def parse_descriptors(
    root: Path, extra_files: Iterable[Path] = ()
) -> dict[tuple[int, int], View]:
    views: dict[tuple[int, int], View] = {}
    files = list(sorted_group(root, "Descriptors")) + list(extra_files)
    for path in files:
        if not path.exists():
            continue
        for lineno, line in iter_lines(path):
            match = _RE_CREATE_VIEW.search(line)
            if not match:
                continue
            kind = _VIEW_KIND[match.group(1)]
            suffix = (match.group(2) or "").lstrip("_")

            heap = _RE_CPU_DESC.search(line)
            heap_id = int(heap.group(1)) if heap else None
            heap_index = int(heap.group(2)) if heap else None

            resource = _RE_GET_RESOURCE.search(line)
            resource_id = int(resource.group(1)) if resource else None

            gpuva = _RE_GPUVA.search(line)
            va_resource = int(gpuva.group(1)) if gpuva else None
            va_offset = int(gpuva.group(2)) if gpuva else 0

            fmt = _RE_FORMAT.search(line)
            dim = _RE_VIEW_DIM.search(line)

            view = View(
                kind=kind,
                heap_id=heap_id,
                heap_index=heap_index,
                resource_id=resource_id if resource_id is not None else va_resource,
                format=fmt.group(1) if fmt else "",
                dimension=dim.group(1) if dim else suffix,
                va_resource_id=va_resource,
                va_offset=va_offset,
                detail=line.strip()[:400],
                source_file=path.name,
                source_line=lineno,
            )
            if heap_id is not None and heap_index is not None:
                views[(heap_id, heap_index)] = view
    return views


# --------------------------------------------------------------------------
# 3. pipeline states
# --------------------------------------------------------------------------
_RE_PSO_FUNC = re.compile(r"^void\s+CreatePipelineState_(\d+)\s*\(")
_RE_STAGE = re.compile(r"pssDesc\.(VS|PS|CS|GS|HS|DS|AS|MS)\s*=\s*\{[^}]*?,\s*(\d+)\s*\}")
_RE_PSO_ROOTSIG = re.compile(r"pssDesc\.pRootSignature\s*=\s*GetRootSignature\((\d+)\)")
_RE_RT_FORMAT = re.compile(r"rtFormatArray\.RTFormats\[(\d+)\]\s*=\s*(DXGI_FORMAT_\w+)")
_RE_DSV_FORMAT = re.compile(r"pssDesc\.DSVFormat\s*=\s*(DXGI_FORMAT_\w+)")
_RE_TOPOLOGY_TYPE = re.compile(r"pssDesc\.PrimitiveTopologyType\s*=\s*(\w+)")
_RE_SAMPLE_DESC = re.compile(r"pssDesc\.SampleDesc\s*=\s*\{\s*(\d+)\s*,\s*(\d+)\s*\}")
_RE_SAMPLE_MASK = re.compile(r"pssDesc\.SampleMask\s*=\s*(\d+)")
_RE_INPUT_ELEMENT = re.compile(
    r'\{\s*"([^"]+)"\s*,\s*(\d+)\s*,\s*(DXGI_FORMAT_\w+)\s*,\s*(\d+)\s*,\s*(\d+)'
)
_RE_RASTERIZER = re.compile(
    r"CD3DX12_RASTERIZER_DESC\d?\(\s*(D3D12_FILL_MODE_\w+)\s*,\s*(D3D12_CULL_MODE_\w+)"
)
_RE_DEPTH_STENCIL = re.compile(
    r"CD3DX12_DEPTH_STENCIL_DESC\d?\(\s*(TRUE|FALSE)\s*,\s*(D3D12_DEPTH_WRITE_MASK_\w+)"
    r"\s*,\s*(D3D12_COMPARISON_FUNC_\w+)"
)
_RE_BLEND_RT = re.compile(
    r"blendDesc\.RenderTarget\[(\d+)\]\s*=\s*\{\s*(TRUE|FALSE)\s*,\s*(TRUE|FALSE)\s*,\s*(.*)"
)


@dataclass(slots=True)
class PsoParseResult:
    pipeline_states: dict[int, PipelineState] = field(default_factory=dict)
    read_sizes: list[int] = field(default_factory=list)


def parse_pipeline_states(root: Path) -> PsoParseResult:
    path = root / "CreatePSOs.cpp"
    result = PsoParseResult()
    if not path.exists():
        return result

    current: PipelineState | None = None
    read_counter = 0
    pending_read: int | None = None
    stage_cursor = 0

    for lineno, line in iter_lines(path):
        match = _RE_PSO_FUNC.match(line)
        if match:
            current = PipelineState(
                api_id=int(match.group(1)), source_file=path.name, source_line=lineno
            )
            result.pipeline_states[current.api_id] = current
            pending_read = None
            stage_cursor = 0
            continue
        if current is None:
            continue

        match = _RE_READ.search(line)
        if match:
            result.read_sizes.append(int(match.group(1)))
            pending_read = read_counter
            current.blob_index = read_counter
            read_counter += 1
            stage_cursor = 0
            continue

        match = _RE_STAGE.search(line)
        if match:
            stage = ShaderStage(match.group(1))
            size = int(match.group(2))
            current.shaders.append(
                Shader(
                    stage=stage,
                    pso_id=current.api_id,
                    byte_size=size,
                    blob_index=pending_read,
                    blob_stage_offset=stage_cursor,
                )
            )
            stage_cursor += size
            if stage is ShaderStage.CS:
                current.kind = "compute"
            continue

        match = _RE_PSO_ROOTSIG.search(line)
        if match:
            current.root_signature_id = int(match.group(1))
            continue

        match = _RE_RT_FORMAT.search(line)
        if match:
            index = int(match.group(1))
            while len(current.rtv_formats) <= index:
                current.rtv_formats.append("")
            current.rtv_formats[index] = match.group(2)
            continue

        match = _RE_DSV_FORMAT.search(line)
        if match:
            current.dsv_format = match.group(1)
            continue

        match = _RE_TOPOLOGY_TYPE.search(line)
        if match:
            current.primitive_topology_type = match.group(1)
            continue

        match = _RE_SAMPLE_DESC.search(line)
        if match:
            current.sample_count = int(match.group(1))
            continue

        match = _RE_SAMPLE_MASK.search(line)
        if match:
            current.sample_mask = int(match.group(1))
            continue

        match = _RE_RASTERIZER.search(line)
        if match:
            current.fill_mode = match.group(1)
            current.cull_mode = match.group(2)
            continue

        match = _RE_DEPTH_STENCIL.search(line)
        if match:
            current.depth_enabled = match.group(1) == "TRUE"
            current.depth_write = "ALL" in match.group(2)
            current.depth_func = match.group(3)
            continue

        match = _RE_BLEND_RT.search(line)
        if match:
            enabled = match.group(2) == "TRUE"
            current.blend_states.append(
                {
                    "index": int(match.group(1)),
                    "blend_enable": enabled,
                    "logic_op_enable": match.group(3) == "TRUE",
                }
            )
            if enabled:
                current.blend_enabled = True
            continue

        if "inputElementDescs[]" in line:
            for element in _RE_INPUT_ELEMENT.finditer(line):
                current.input_layout.append(
                    {
                        "semantic": element.group(1),
                        "semantic_index": int(element.group(2)),
                        "format": element.group(3),
                        "input_slot": int(element.group(4)),
                        "aligned_byte_offset": int(element.group(5)),
                    }
                )

    for pso in result.pipeline_states.values():
        pso.rtv_formats = [fmt for fmt in pso.rtv_formats if fmt]
        if pso.is_compute:
            pso.kind = "compute"
    return result


# --------------------------------------------------------------------------
# 4. root signatures
# --------------------------------------------------------------------------
_RE_API_ID = re.compile(r"//\s*ApiObjectId\s*=\s*(\d+)")
_RE_PARAM_TYPE = re.compile(
    r"rootParameters\[(\d+)\]\.ParameterType\s*=\s*D3D12_ROOT_PARAMETER_TYPE_(\w+)"
)
_RE_VISIBILITY = re.compile(
    r"rootParameters\[(\d+)\]\.ShaderVisibility\s*=\s*D3D12_SHADER_VISIBILITY_(\w+)"
)
_RE_RANGE = re.compile(
    r"descriptorRanges\[(\d+)\]\s*=\s*\{\s*D3D12_DESCRIPTOR_RANGE_TYPE_(\w+)\s*,"
    r"\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)"
)
_RE_TABLE_ASSIGN = re.compile(
    r"rootParameters\[(\d+)\]\.DescriptorTable\s*=\s*\{\s*(\d+)"
)
_RE_DESCRIPTOR_ASSIGN = re.compile(
    r"rootParameters\[(\d+)\]\.Descriptor\s*=\s*\{\s*(\d+)\s*,\s*(\d+)"
)
_RE_CONSTANTS_ASSIGN = re.compile(
    r"rootParameters\[(\d+)\]\.Constants\s*=\s*\{\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)"
)
_RE_CREATE_ROOTSIG = re.compile(r"CreateAndTrackRootSignature\(\s*(\d+)")

_ROOT_KIND_MAP = {
    "DESCRIPTOR_TABLE": RootParameterKind.DESCRIPTOR_TABLE,
    "CBV": RootParameterKind.CBV,
    "SRV": RootParameterKind.SRV,
    "UAV": RootParameterKind.UAV,
    "32BIT_CONSTANTS": RootParameterKind.CONSTANTS,
}

UNBOUNDED = 0xFFFFFFFF

# D3D12_DESCRIPTOR_RANGE_TYPE_* -> the view kind a slot in that range must hold.
_RANGE_TYPE_TO_VIEW: dict[str, ViewKind] = {
    "SRV": ViewKind.SRV,
    "UAV": ViewKind.UAV,
    "CBV": ViewKind.CBV,
    "SAMPLER": ViewKind.SAMPLER,
}


@dataclass(slots=True)
class RootSignature:
    api_id: int
    parameters: list[RootParameter] = field(default_factory=list)
    static_sampler_count: int = 0
    source_file: str = ""
    source_line: int = 0

    def parameter(self, index: int) -> RootParameter | None:
        return next((p for p in self.parameters if p.index == index), None)

    def table_size(self, root_index: int) -> int:
        parameter = self.parameter(root_index)
        if parameter is None or parameter.kind is not RootParameterKind.DESCRIPTOR_TABLE:
            return 0
        total = 0
        for entry in parameter.ranges:
            count = entry.get("count", 0)
            if count in (UNBOUNDED, -1):
                return -1
            total += count
        return total

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_signature_id": self.api_id,
            "parameter_count": len(self.parameters),
            "static_sampler_count": self.static_sampler_count,
            "parameters": [p.to_dict() for p in self.parameters],
            "source": f"{self.source_file}:{self.source_line}",
        }


def parse_root_signatures(root: Path) -> dict[int, RootSignature]:
    signatures: dict[int, RootSignature] = {}
    files = sorted_group(root, "FrameResources")
    for path in files:
        if not path.exists():
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        parameters: dict[int, RootParameter] = {}
        pending_ranges: list[dict] = []
        block_start = 0

        for lineno, line in enumerate(lines, 1):
            if _RE_API_ID.search(line):
                parameters = {}
                pending_ranges = []
                block_start = lineno
                continue

            match = _RE_PARAM_TYPE.search(line)
            if match:
                index = int(match.group(1))
                parameters[index] = RootParameter(
                    index=index,
                    kind=_ROOT_KIND_MAP.get(
                        match.group(2), RootParameterKind.DESCRIPTOR_TABLE
                    ),
                )
                continue

            match = _RE_VISIBILITY.search(line)
            if match:
                index = int(match.group(1))
                if index in parameters:
                    parameters[index].visibility = match.group(2)
                continue

            match = _RE_RANGE.search(line)
            if match:
                pending_ranges.append(
                    {
                        "slot": int(match.group(1)),
                        "range_type": match.group(2),
                        "count": int(match.group(3)),
                        "base_shader_register": int(match.group(4)),
                        "register_space": int(match.group(5)),
                    }
                )
                continue

            match = _RE_TABLE_ASSIGN.search(line)
            if match:
                index = int(match.group(1))
                count = int(match.group(2))
                if index in parameters:
                    parameters[index].ranges = pending_ranges[-count:] if count else []
                    parameters[index].num_descriptors = sum(
                        entry["count"]
                        for entry in parameters[index].ranges
                        if entry["count"] != UNBOUNDED
                    )
                pending_ranges = []
                continue

            match = _RE_DESCRIPTOR_ASSIGN.search(line)
            if match:
                index = int(match.group(1))
                if index in parameters:
                    parameters[index].shader_register = int(match.group(2))
                    parameters[index].register_space = int(match.group(3))
                continue

            match = _RE_CONSTANTS_ASSIGN.search(line)
            if match:
                index = int(match.group(1))
                if index in parameters:
                    parameters[index].shader_register = int(match.group(2))
                    parameters[index].register_space = int(match.group(3))
                    parameters[index].num_descriptors = int(match.group(4))
                continue

            match = _RE_CREATE_ROOTSIG.search(line)
            if match:
                signature = RootSignature(
                    api_id=int(match.group(1)),
                    parameters=[parameters[k] for k in sorted(parameters)],
                    source_file=path.name,
                    source_line=block_start,
                )
                window = "\n".join(lines[max(0, lineno - 12) : lineno])
                sampler = re.search(
                    r"D3D12_ROOT_SIGNATURE_DESC1?\s+\w+\s*=\s*\{\s*\d+\s*,\s*\w+\s*,\s*(\d+)",
                    window,
                )
                if sampler:
                    signature.static_sampler_count = int(sampler.group(1))
                signatures[signature.api_id] = signature
                parameters = {}
                pending_ranges = []
    return signatures


# --------------------------------------------------------------------------
# 5. command lists -> draw calls
# --------------------------------------------------------------------------
_RE_CL_FUNC = re.compile(r"^void\s+PopulateCommandList_([\d_]+)\s*\(")
_RE_CL_CALL = re.compile(r"GetCommandList\((\d+)\)->(\w+)\((.*)$")
_RE_GLOBAL_ID = re.compile(r"//\s*GlobalId\s*=\s*(\d+)")
_RE_PIX_BEGIN = re.compile(r"PIXBeginEvent\([^,]+,\s*\d+\s*,\s*LR?\"\((.*?)\)\"")
_RE_PIX_BEGIN_ALT = re.compile(r"PIXBeginEvent\([^,]+,\s*\d+\s*,\s*L?\"(.*?)\"")
_RE_PIX_END = re.compile(r"PIXEndEvent\(")
_RE_GPU_DESC = re.compile(
    r"GetGpuDescriptor\(\s*g_descriptorHeap_(\d+)\.Get\(\)\s*,\s*(\d+)\s*\)"
)
_RE_HEAP_REF = re.compile(r"g_descriptorHeap_(\d+)\.Get\(\)")
_RE_VBV_ENTRY = re.compile(
    r"vertexBufferViews\[(\d+)\]\s*=\s*\{\s*GetGpuva\(\s*(\d+)\s*,\s*(\d+)\s*\)"
    r"\s*,\s*(\d+)\s*,\s*(\d+)\s*\}"
)
_RE_IBV_AGGREGATE = re.compile(
    r"D3D12_INDEX_BUFFER_VIEW\s+\w+\s*\{?\s*=?\s*\{?\s*"
    r"GetGpuva\(\s*(\d+)\s*,\s*(\d+)\s*\)\s*,\s*(\d+)\s*,\s*(DXGI_FORMAT_\w+)"
)
_RE_IBV_FIELD_RES = re.compile(r"ibvDesc\.BufferLocation\s*=\s*GetGpuva\((\d+),\s*(\d+)\)")
_RE_IBV_FIELD_SIZE = re.compile(r"ibvDesc\.SizeInBytes\s*=\s*(\d+)")
_RE_IBV_FIELD_FMT = re.compile(r"ibvDesc\.Format\s*=\s*(DXGI_FORMAT_\w+)")
_RE_RTV_HANDLE = re.compile(
    r"rtvHandle\w*\s*=\s*GetCpuDescriptor\(\s*g_descriptorHeap_(\d+)\.Get\(\)\s*,\s*(\d+)\s*\)"
)
_RE_DSV_HANDLE = re.compile(
    r"dsvHandle\w*\s*=\s*GetCpuDescriptor\(\s*g_descriptorHeap_(\d+)\.Get\(\)\s*,\s*(\d+)\s*\)"
)
_RE_RTV_INLINE = re.compile(
    r"CreateRenderTargetView(?:_[A-Za-z0-9]+)?\s*\(\s*GetResource\((\d+)\)"
)
_RE_DSV_INLINE = re.compile(
    r"CreateDepthStencilView(?:_[A-Za-z0-9]+)?\s*\(\s*GetResource\((\d+)\)"
)
_RE_INDIRECT_BUFFER = re.compile(r'g_indirectArgumentBuffers\["([^"]+)"\]')
_RE_VIEWPORT = re.compile(
    r"\{\s*" + _NUM + r"\s*,\s*" + _NUM + r"\s*,\s*" + _NUM + r"\s*,\s*" + _NUM
    + r"\s*,\s*" + _NUM + r"\s*,\s*" + _NUM + r"\s*\}"
)
_RE_SCISSOR = re.compile(r"\{\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\}")

DRAW_APIS = {
    "DrawInstanced": EventKind.DRAW,
    "DrawIndexedInstanced": EventKind.DRAW,
    "Dispatch": EventKind.DISPATCH,
    "DispatchRays": EventKind.DISPATCH_RAYS,
    "DispatchMesh": EventKind.DISPATCH,
    "ExecuteIndirect": EventKind.EXECUTE_INDIRECT,
}


class CommandListParser:
    """Replays emitted D3D12 calls, snapshotting state at each draw."""

    def __init__(
        self,
        root: Path,
        views: dict[tuple[int, int], View] | None = None,
        root_signatures: dict[int, RootSignature] | None = None,
        default_table_span: int = 8,
    ) -> None:
        self.root = Path(root)
        self.views = views or {}
        self.root_signatures = root_signatures or {}
        self.default_table_span = default_table_span
        self.draw_calls: list[DrawCall] = []
        self._marker_stack: list[str] = []
        # Every descriptor-table base that any draw binds, per heap. A table can
        # never legitimately extend into the next bound base, so these act as
        # hard upper bounds when expanding a table (see _expand_table).
        self._table_bases: dict[int, set[int]] = {}

    def prescan_table_bases(self) -> dict[int, set[int]]:
        """Collect every descriptor-table base bound anywhere in the frame.

        UE5 sub-allocates a small window per dispatch out of one huge heap, so
        consecutive draws bind bases only a few slots apart while the root
        signature declares a much larger range (e.g. 64 SRVs). Expanding by the
        declared count alone would read straight into the next draw's table (or
        into PIX's initialisation filler). Knowing all bases lets us stop early.
        """
        bases: dict[int, set[int]] = {}
        for path in sorted_group(self.root, "CommandLists"):
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if "RootDescriptorTable" not in line:
                        continue
                    match = _RE_GPU_DESC.search(line)
                    if match:
                        heap = int(match.group(1))
                        bases.setdefault(heap, set()).add(int(match.group(2)))
        self._table_bases = bases
        return bases

    def _fresh_state(self) -> dict:
        return {
            "pso": None,
            "gfx_rootsig": None,
            "compute_rootsig": None,
            "heaps": [],
            "gfx_bindings": {},
            "compute_bindings": {},
            "vbs": [],
            "ib": None,
            "rtv_heap": [],
            "dsv": None,
            "rt_res": [],
            "ds_res": None,
            "topology": "",
            "viewports": [],
            "scissors": [],
            "pending_ibv": {},
            "pending_vbv": {},
            "pending_rtv": [],
            "pending_dsv": None,
            "inline_rtv_res": [],
            "inline_dsv_res": None,
        }

    def parse(self) -> list[DrawCall]:
        self.prescan_table_bases()
        for path in sorted_group(self.root, "CommandLists"):
            self._parse_file(path)
        return self.draw_calls

    def _parse_file(self, path: Path) -> None:
        # PIX splits one command list across several PopulateCommandList_* funcs;
        # state must persist across them and reset only on Reset().
        states: dict[int, dict] = {}
        current_cl: int | None = None
        pending_global_id: int | None = None

        for lineno, line in iter_lines(path):
            match = _RE_CL_FUNC.match(line)
            if match:
                parts = match.group(1).split("_")
                try:
                    current_cl = int(parts[0])
                except (ValueError, IndexError):
                    current_cl = None
                pending_global_id = None
                continue

            match = _RE_GLOBAL_ID.search(line)
            if match:
                pending_global_id = int(match.group(1))
                continue

            match = _RE_PIX_BEGIN.search(line) or _RE_PIX_BEGIN_ALT.search(line)
            if match:
                if len(self._marker_stack) < 64:
                    self._marker_stack.append(match.group(1))
                continue
            if _RE_PIX_END.search(line):
                if self._marker_stack:
                    self._marker_stack.pop()
                continue

            match = _RE_CL_CALL.search(line)
            if match:
                cl_id, api, argtext = int(match.group(1)), match.group(2), match.group(3)
                state = states.setdefault(cl_id, self._fresh_state())
                self._scan_pending(line, state)
                args = split_args(argtext.rstrip("); \n"))

                if api == "Reset":
                    fresh = self._fresh_state()
                    pso = re.search(r"GetPipelineState\((\d+)\)", argtext)
                    if pso:
                        fresh["pso"] = int(pso.group(1))
                    states[cl_id] = fresh
                    continue

                if api in DRAW_APIS:
                    self._emit_draw(
                        path, lineno, cl_id, api, args, state, pending_global_id, argtext
                    )
                    pending_global_id = None
                    continue

                self._apply_state_call(api, args, argtext, state, lineno)
                continue

            if current_cl is not None:
                state = states.setdefault(current_cl, self._fresh_state())
                self._scan_pending(line, state)

    # -- helpers --------------------------------------------------------
    def _scan_pending(self, line: str, state: dict) -> None:
        if "D3D12_VERTEX_BUFFER_VIEW" in line:
            state["pending_vbv"] = {}
        for entry in _RE_VBV_ENTRY.finditer(line):
            state["pending_vbv"][int(entry.group(1))] = {
                "res": int(entry.group(2)),
                "off": int(entry.group(3)),
                "size": int(entry.group(4)),
                "stride": int(entry.group(5)),
            }

        match = _RE_IBV_AGGREGATE.search(line)
        if match:
            state["pending_ibv"] = {
                "res": int(match.group(1)),
                "off": int(match.group(2)),
                "size": int(match.group(3)),
                "fmt": match.group(4),
            }
        else:
            match = _RE_IBV_FIELD_RES.search(line)
            if match:
                state["pending_ibv"] = {
                    "res": int(match.group(1)),
                    "off": int(match.group(2)),
                }
            match = _RE_IBV_FIELD_SIZE.search(line)
            if match and state["pending_ibv"]:
                state["pending_ibv"]["size"] = int(match.group(1))
            match = _RE_IBV_FIELD_FMT.search(line)
            if match and state["pending_ibv"]:
                state["pending_ibv"]["fmt"] = match.group(1)

        for handle in _RE_RTV_HANDLE.finditer(line):
            state["pending_rtv"].append((int(handle.group(1)), int(handle.group(2))))
        match = _RE_DSV_HANDLE.search(line)
        if match:
            state["pending_dsv"] = (int(match.group(1)), int(match.group(2)))

        for inline in _RE_RTV_INLINE.finditer(line):
            state["inline_rtv_res"].append(int(inline.group(1)))
        match = _RE_DSV_INLINE.search(line)
        if match:
            state["inline_dsv_res"] = int(match.group(1))

    def _apply_state_call(
        self, api: str, args: list[str], argtext: str, state: dict, lineno: int
    ) -> None:
        if api == "SetPipelineState":
            ids = _ints(argtext)
            state["pso"] = ids[0] if ids else None
        elif api == "SetGraphicsRootSignature":
            ids = _ints(argtext)
            state["gfx_rootsig"] = ids[0] if ids else None
            state["gfx_bindings"] = {}
        elif api == "SetComputeRootSignature":
            ids = _ints(argtext)
            state["compute_rootsig"] = ids[0] if ids else None
            state["compute_bindings"] = {}
        elif api == "SetDescriptorHeaps":
            heaps = [int(h) for h in _RE_HEAP_REF.findall(argtext)]
            if heaps:
                state["heaps"] = heaps
        elif api in ("SetGraphicsRootDescriptorTable", "SetComputeRootDescriptorTable"):
            slot = "gfx_bindings" if api.startswith("SetGraphics") else "compute_bindings"
            descriptor = _RE_GPU_DESC.search(argtext)
            try:
                root_index = int(args[0])
            except (ValueError, IndexError):
                return
            if descriptor:
                state[slot][root_index] = BindingSlot(
                    root_index=root_index,
                    kind=RootParameterKind.DESCRIPTOR_TABLE,
                    heap_id=int(descriptor.group(1)),
                    heap_index=int(descriptor.group(2)),
                    source_line=lineno,
                )
        elif api in (
            "SetGraphicsRootConstantBufferView",
            "SetComputeRootConstantBufferView",
            "SetGraphicsRootShaderResourceView",
            "SetComputeRootShaderResourceView",
            "SetGraphicsRootUnorderedAccessView",
            "SetComputeRootUnorderedAccessView",
        ):
            slot = "gfx_bindings" if api.startswith("SetGraphics") else "compute_bindings"
            kind = (
                RootParameterKind.CBV
                if "ConstantBuffer" in api
                else RootParameterKind.SRV
                if "ShaderResource" in api
                else RootParameterKind.UAV
            )
            gpuva = _RE_GPUVA.search(argtext)
            try:
                root_index = int(args[0])
            except (ValueError, IndexError):
                return
            state[slot][root_index] = BindingSlot(
                root_index=root_index,
                kind=kind,
                resource_id=int(gpuva.group(1)) if gpuva else None,
                va_offset=int(gpuva.group(2)) if gpuva else 0,
                source_line=lineno,
            )
        elif api in (
            "SetGraphicsRoot32BitConstants",
            "SetComputeRoot32BitConstants",
            "SetGraphicsRoot32BitConstant",
            "SetComputeRoot32BitConstant",
        ):
            slot = "gfx_bindings" if api.startswith("SetGraphics") else "compute_bindings"
            numbers = _ints(argtext)
            if numbers:
                state[slot][numbers[0]] = BindingSlot(
                    root_index=numbers[0],
                    kind=RootParameterKind.CONSTANTS,
                    num_constants=numbers[1] if len(numbers) > 1 else 0,
                    source_line=lineno,
                )
        elif api == "IASetVertexBuffers":
            numbers = _ints(argtext)
            start = numbers[0] if numbers else 0
            pending = state["pending_vbv"] or {}
            buffers: list[VertexBufferBinding] = []
            for slot_index in sorted(pending):
                entry = pending[slot_index]
                if entry["res"] == 0 and entry["size"] == 0:
                    continue
                buffers.append(
                    VertexBufferBinding(
                        slot=start + slot_index,
                        resource_id=entry["res"],
                        offset=entry["off"],
                        size_bytes=entry["size"],
                        stride=entry["stride"],
                    )
                )
            state["vbs"] = buffers
            state["pending_vbv"] = {}
        elif api == "IASetIndexBuffer":
            pending = state["pending_ibv"]
            if pending:
                state["ib"] = IndexBufferBinding(
                    resource_id=pending.get("res"),
                    offset=pending.get("off", 0),
                    size_bytes=pending.get("size", 0),
                    format=pending.get("fmt", ""),
                )
            state["pending_ibv"] = {}
        elif api == "IASetPrimitiveTopology":
            state["topology"] = argtext.strip(") ;\n")
        elif api == "OMSetRenderTargets":
            numbers = _ints(argtext)
            count = numbers[0] if numbers else 0
            rtvs = state["pending_rtv"][-count:] if count else []
            state["rtv_heap"] = list(rtvs)
            state["dsv"] = state["pending_dsv"]
            state["rt_res"] = self._resolve_render_targets(rtvs, state)
            state["ds_res"] = self._resolve_depth(state["dsv"], state)
            state["pending_rtv"] = []
            state["pending_dsv"] = None
        elif api == "RSSetViewports":
            state["viewports"] = [
                {
                    "top_left_x": float(m.group(1)),
                    "top_left_y": float(m.group(2)),
                    "width": float(m.group(3)),
                    "height": float(m.group(4)),
                    "min_depth": float(m.group(5)),
                    "max_depth": float(m.group(6)),
                }
                for m in _RE_VIEWPORT.finditer(argtext)
            ]
        elif api == "RSSetScissorRects":
            state["scissors"] = [
                {
                    "left": int(m.group(1)),
                    "top": int(m.group(2)),
                    "right": int(m.group(3)),
                    "bottom": int(m.group(4)),
                }
                for m in _RE_SCISSOR.finditer(argtext)
            ]

    def _resolve_render_targets(self, rtvs, state) -> list[int]:
        out: list[int] = []
        for heap_id, index in rtvs:
            view = self.views.get((heap_id, index))
            if view is not None and view.resource_id is not None:
                out.append(view.resource_id)
        if not out and state["inline_rtv_res"]:
            out = list(dict.fromkeys(state["inline_rtv_res"]))
        return out

    def _resolve_depth(self, dsv, state):
        if dsv is not None:
            view = self.views.get(dsv)
            if view is not None and view.resource_id is not None:
                return view.resource_id
        return state["inline_dsv_res"]

    def _emit_draw(
        self,
        path: Path,
        lineno: int,
        cl_id: int,
        api: str,
        args: list[str],
        state: dict,
        global_id: int | None,
        argtext: str,
    ) -> None:
        kind = DRAW_APIS[api]
        is_compute = kind in (EventKind.DISPATCH, EventKind.DISPATCH_RAYS)
        active_rootsig = state["compute_rootsig"] if is_compute else state["gfx_rootsig"]
        source = state["compute_bindings"] if is_compute else state["gfx_bindings"]

        snapshot: list[BindingSlot] = []
        for binding in source.values():
            copy = BindingSlot(
                root_index=binding.root_index,
                kind=binding.kind,
                heap_id=binding.heap_id,
                heap_index=binding.heap_index,
                resource_id=binding.resource_id,
                gpu_va=binding.gpu_va,
                va_offset=binding.va_offset,
                num_constants=binding.num_constants,
                source_line=binding.source_line,
            )
            if copy.heap_id is not None and copy.heap_index is not None:
                copy.resolved_views, copy.table_confidence = self._expand_table(
                    copy.heap_id, copy.heap_index, active_rootsig, copy.root_index
                )
            snapshot.append(copy)
        snapshot.sort(key=lambda b: b.root_index)

        numbers = _ints(argtext)
        draw = DrawCall(
            index=len(self.draw_calls),
            kind=kind,
            api=api,
            command_list_id=cl_id,
            global_id=global_id,
            marker_path=tuple(self._marker_stack),
            source_file=path.name,
            source_line=lineno,
            pso_id=state["pso"],
            root_signature_id=active_rootsig,
            primitive_topology=state["topology"],
            bindings=snapshot,
            descriptor_heap_ids=list(state["heaps"]),
            vertex_buffers=list(state["vbs"]),
            index_buffer=state["ib"],
            render_target_resource_ids=list(state["rt_res"]),
            depth_stencil_resource_id=state["ds_res"],
            viewports=list(state["viewports"]),
            scissor_rects=list(state["scissors"]),
        )
        if api == "DrawIndexedInstanced" and len(numbers) >= 5:
            (
                draw.vertex_or_index_count,
                draw.instance_count,
                draw.start_index,
                draw.base_vertex,
                draw.start_instance,
            ) = numbers[:5]
        elif api == "DrawInstanced" and len(numbers) >= 4:
            (
                draw.vertex_or_index_count,
                draw.instance_count,
                draw.base_vertex,
                draw.start_instance,
            ) = numbers[:4]
        elif api in ("Dispatch", "DispatchMesh") and len(numbers) >= 3:
            draw.thread_group_x, draw.thread_group_y, draw.thread_group_z = numbers[:3]
        elif api == "ExecuteIndirect":
            indirect = _RE_INDIRECT_BUFFER.search(argtext)
            if indirect:
                draw.indirect_argument_buffer = indirect.group(1)

        self.draw_calls.append(draw)

    def _expand_table(
        self,
        heap_id: int,
        base_index: int,
        root_signature_id: int | None = None,
        root_index: int | None = None,
    ) -> tuple[list[View], str]:
        """Expand a root descriptor table into the views the shader can see.

        Returns (views, confidence). Confidence is:
          exact    the table is fully bounded by the next bound base and every
                   slot matches the range type the root signature declares
          bounded  a hard bound applied, but the window was cut short
          loose    no neighbouring base was known, so the declared count was used
        """
        signature = (
            self.root_signatures.get(root_signature_id)
            if root_signature_id is not None
            else None
        )
        parameter = (
            signature.parameter(root_index)
            if signature is not None and root_index is not None
            else None
        )

        declared = 0
        allowed_kinds: set[ViewKind] = set()
        if parameter is not None and parameter.kind is RootParameterKind.DESCRIPTOR_TABLE:
            declared = signature.table_size(root_index) if signature else 0
            for entry in parameter.ranges:
                kind = _RANGE_TYPE_TO_VIEW.get(entry.get("range_type", ""))
                if kind is not None:
                    allowed_kinds.add(kind)

        span = declared if declared > 0 else self.default_table_span
        if declared == -1:
            span = 4096

        # A table cannot run into the next base bound out of the same heap.
        confidence = "loose"
        bases = self._table_bases.get(heap_id)
        if bases:
            following = [value for value in bases if value > base_index]
            if following:
                span = min(span, min(following) - base_index)
                confidence = "exact"

        out: list[View] = []
        misses = 0
        for offset in range(max(span, 0)):
            view = self.views.get((heap_id, base_index + offset))
            if view is None:
                misses += 1
                if misses > 4:
                    break
                continue
            # Slots whose view type contradicts the declared range belong to a
            # different table (or to PIX's filler), so stop rather than report them.
            if allowed_kinds and view.kind not in allowed_kinds:
                if confidence == "exact":
                    confidence = "bounded"
                break
            misses = 0
            out.append(view)

        if declared > 0 and len(out) < declared and confidence == "exact":
            confidence = "bounded"
        return out, confidence
