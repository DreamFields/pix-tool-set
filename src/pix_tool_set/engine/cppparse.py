"""Parsers for the C++ project that ``pixtool export-to-cpp`` produces.

Passes:
  1. parse_resources         CreateAndInitResources_*.cpp  -> Resource
  2. parse_descriptors       Descriptors_*.cpp             -> View
                             ModifyDescriptors_*.cpp       (override, see below)
  3. parse_pipeline_states   CreatePSOs.cpp                -> PipelineState + Shader
  4. parse_root_signatures     FrameResources_*.cpp          -> RootSignature
  4b. parse_command_signatures FrameResources_*.cpp        -> CommandSignature
  4c. parse_command_queues   RenderFrameWorker_*.cpp       -> CommandQueue
                             FrameResources_*.cpp          (queue names)
  5. CommandListParser       CommandLists_*.cpp            -> DrawCall

The command-list pass is a state machine: it replays the emitted D3D12 calls in
order, tracking the currently bound PSO / root signature / descriptor heaps /
render targets / vertex+index buffers / root arguments, and snapshots that state
at every draw or dispatch.  That snapshot is what PIX shows for a selected draw.

ExecuteIndirect needs the command-signature pass to be snapshotted correctly.
Its indirect argument buffer only supplies the thread-group / vertex counts; the
root signature and every descriptor table are still set with ordinary
Set*Root* calls right before it, exactly like a direct dispatch. Which of the
two binding sets applies (compute vs graphics) is decided by the command
signature's D3D12_INDIRECT_ARGUMENT_TYPE, hence parse_command_signatures.

parse_command_queues exists because the exported event list covers a single
command queue. On a capture that spans several queues every action on the other
queues has no row in the CSV and therefore no Queue ID, which used to surface as
a bare null. The submissions recorded in RenderFrameWorker_*.cpp let us say
*which* queue such an action ran on without inventing an identifier for it -- see
the warning in that function about why synthesising a Queue ID is not an option.
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
    _apply_resource_names(root, resources)
    return resources


def _apply_resource_names(root: Path, resources: dict[int, Resource]) -> None:
    """Attach the engine's debug name to each resource.

    PIX names objects through a generic ``GetObject(n)->SetName(...)`` that sits in
    FrameResources_*.cpp, thousands of lines away from the CreateAndInitResource_*
    block that declares the resource, which is why this is a second sweep keyed by
    object id rather than something the main loop can pick up.

    Without this, a resource can only be referred to by its numeric id, so a
    payload cannot say "GBufferA" where the PIX UI does -- the caller is left to
    map ids to names by hand. The same SetName statements also name queues and
    fences; ids that are not resources simply have no entry here and are skipped,
    so a name landing on a non-resource object cannot invent a resource.
    """
    for path in sorted_group(root, "FrameResources"):
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _RE_OBJECT_NAME.finditer(text):
            resource = resources.get(int(match.group(1)))
            # First name wins: re-naming an object mid-frame is legal in D3D12 but
            # the capture's own UI shows the name it was created with.
            if resource is not None and not resource.name:
                resource.name = match.group(2)



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

# Subresource selector positions, per Create*View_* helper in the export's Helpers.h.
#
# Read directly off those inline definitions rather than inferred from D3D12
# struct layout, because the helpers do not all take the same prefix: every SRV
# helper carries a shader4ComponentMapping argument that the UAV helpers lack,
# and every UAV helper takes an extra pCounterResource pointer that the SRV
# helpers lack. Guessing a shared offset would silently shift every field.
#
# Indices count the *comma-separated top-level arguments of the call as written
# in the export*, zero-based. GetCpuDescriptor(...) and GetResource(...) each
# occupy exactly one such argument.
#
#   CreateShaderResourceView_Tex2D(res, dest, format, dim, mapping,
#                                  mostDetailedMip, mipLevels, planeSlice, clamp)
#     ->  0     1     2       3    4        5              6         7          8
#
#   CreateUnorderedAccessView_Tex2D(res, counter, dest, format, dim,
#                                   mipSlice, planeSlice)
#     ->  0    1        2     3       4    5         6
#
# Only fields the helper genuinely exposes are listed; anything absent stays
# None so it reads as "not applicable" instead of a fabricated zero. Buffer and
# RaytracingAS variants are deliberately omitted -- they have no subresources.
_SUBRESOURCE_ARGS: dict[str, dict[str, int]] = {
    "CreateShaderResourceView_Tex2D": {
        "mip_slice": 5,
        "mip_levels": 6,
        "plane_slice": 7,
    },
    "CreateShaderResourceView_Tex2DArray": {
        "mip_slice": 5,
        "mip_levels": 6,
        "array_slice": 7,
        "array_size": 8,
        "plane_slice": 9,
    },
    "CreateShaderResourceView_Tex2DMSArray": {
        "array_slice": 5,
        "array_size": 6,
    },
    "CreateShaderResourceView_Tex3D": {
        "mip_slice": 5,
        "mip_levels": 6,
    },
    "CreateShaderResourceView_TexCube": {
        "mip_slice": 5,
        "mip_levels": 6,
    },
    "CreateShaderResourceView_TexCubeArray": {
        "mip_slice": 5,
        "mip_levels": 6,
        "array_slice": 7,
        "array_size": 8,
    },
    "CreateUnorderedAccessView_Tex2D": {
        "mip_slice": 5,
        "plane_slice": 6,
    },
    "CreateUnorderedAccessView_Tex2DArray": {
        "mip_slice": 5,
        "array_slice": 6,
        "array_size": 7,
        "plane_slice": 8,
    },
    "CreateUnorderedAccessView_Tex3D": {
        "mip_slice": 5,
        "array_slice": 6,  # firstWSlice -- the depth-slice analogue for a 3D UAV
        "array_size": 7,  # wSize
    },
    "CreateRenderTargetView_Tex2D": {
        "mip_slice": 4,
        "plane_slice": 5,
    },
    "CreateRenderTargetView_Tex2DArray": {
        "mip_slice": 4,
        "array_slice": 5,
        "array_size": 6,
        "plane_slice": 7,
    },
    "CreateDepthStencilView_Tex2D": {
        "mip_slice": 4,
    },
    "CreateDepthStencilView_Tex2DArray": {
        "mip_slice": 4,
        "array_slice": 5,
        "array_size": 6,
    },
}


def _split_call_args(line: str, call_start: int) -> list[str]:
    """Split one Create*View call into top-level argument strings.

    Nested calls such as ``GetCpuDescriptor(heap.Get(), 400404)`` contain commas
    that must NOT split an argument, so this tracks parenthesis depth instead of
    using ``str.split(',')``. Returns [] when the call is not closed on this
    line, since a positional lookup into a truncated argument list would quietly
    read the wrong field.
    """
    open_paren = line.find("(", call_start)
    if open_paren < 0:
        return []
    depth = 0
    args: list[str] = []
    current: list[str] = []
    for ch in line[open_paren:]:
        if ch == "(":
            depth += 1
            if depth == 1:
                continue
        elif ch == ")":
            depth -= 1
            if depth == 0:
                args.append("".join(current).strip())
                return args
        if depth == 1 and ch == ",":
            args.append("".join(current).strip())
            current = []
            continue
        current.append(ch)
    return []


def _parse_uint(token: str) -> int | None:
    """Read a UINT literal, tolerating casts and suffixes; None when not one.

    Returning None for a non-literal (an identifier, an expression) is
    deliberate: a fabricated 0 would make two different subresources compare
    equal, which is the very confusion these fields exist to prevent.
    """
    token = token.strip()
    match = re.fullmatch(r"(?:\(\s*UINT\s*\)\s*)?(\d+)[uUlL]*", token)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _subresource_fields(func_name: str, line: str, call_start: int) -> dict[str, int]:
    """Extract whichever subresource selectors this helper exposes."""
    spec = _SUBRESOURCE_ARGS.get(func_name)
    if not spec:
        return {}
    args = _split_call_args(line, call_start)
    if not args:
        return {}
    fields: dict[str, int] = {}
    for field_name, position in spec.items():
        if position >= len(args):
            continue
        value = _parse_uint(args[position])
        if value is not None:
            fields[field_name] = value
    return fields



_VIEW_KIND = {
    "CreateShaderResourceView": ViewKind.SRV,
    "CreateUnorderedAccessView": ViewKind.UAV,
    "CreateConstantBufferView": ViewKind.CBV,
    "CreateRenderTargetView": ViewKind.RTV,
    "CreateDepthStencilView": ViewKind.DSV,
    "CreateSampler": ViewKind.SAMPLER,
}


def descriptor_source_files(root: Path, extra_files: Iterable[Path] = ()) -> list[Path]:
    """Every file that writes descriptors, in the order the frame writes them.

    ORDER IS LOAD-BEARING -- do not sort this list or reshuffle the groups.
    parse_descriptors keys views by (heap_id, heap_index) and lets a later write
    replace an earlier one, so file order *is* the override policy:

      1. ``Descriptors_*.cpp``        PIX's initialisation filler. It populates
                                      the whole heap up front, so almost every
                                      slot has a value here -- usually a stale
                                      one that no draw ever reads.
      2. ``ModifyDescriptors_*.cpp``  the descriptors PIX rewrites so they are
                                      correct *at the draw/dispatch that uses
                                      them*. These MUST come after group 1, or
                                      the filler wins and tables decode to the
                                      wrong resources (or to nothing at all,
                                      reported as trust=unavailable/filler).
      3. ``extra_files``              caller-supplied, normally
                                      ``CommandLists_*.cpp``, whose inline
                                      Create*View calls happen last of all.

    Concretely, on Tiled.wpix heap 32: slots 416290 and 416292 are written by
    Descriptors_031.cpp (rid 786 / 631) and rewritten by ModifyDescriptors_000.cpp
    (rid 753 / 769). Getting the order backwards silently yields the 786/631
    pair, which looks plausible but is wrong -- so those two slots double as a
    regression probe.

    ``sorted_group(root, "Descriptors")`` globs ``Descriptors_*.cpp``, which does
    NOT match ``ModifyDescriptors_000.cpp`` (fnmatch anchors the whole basename),
    hence the separate group rather than a widened prefix. If you touch this,
    re-check that assumption directly, e.g.::

        [p.name for p in sorted_group(root, "Descriptors")
         if p.name.startswith("Modify")]   # must be []

    Widening the prefix instead would also change parse_resources
    (``CreateAndInitResources``), parse_root_signatures (``FrameResources``) and
    CommandListParser (``CommandLists``), which all share sorted_group.
    """
    files = list(sorted_group(root, "Descriptors"))
    files += list(sorted_group(root, "ModifyDescriptors"))
    files += list(extra_files)
    # De-duplicate on the last occurrence so a repeated path keeps its latest
    # (highest-priority) position instead of being pinned to the earliest one.
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in reversed(files):
        resolved = Path(path)
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    unique.reverse()
    return unique


def parse_descriptors(
    root: Path, extra_files: Iterable[Path] = ()
) -> dict[tuple[int, int], View]:
    """Build the (heap_id, heap_index) -> View map for the whole export.

    Later writes intentionally overwrite earlier ones (see the assignment at the
    bottom of the loop); descriptor_source_files defines that precedence.
    """
    views: dict[tuple[int, int], View] = {}
    files = descriptor_source_files(root, extra_files)
    # CreateSampler calls are preceded by local ``samplerDesc.Filter = ...``
    # assignments. This collects those per variable, so a sampler view can carry
    # the descriptor content instead of just "there is a sampler here". Reset at
    # every function boundary and file boundary so assignments cannot leak.
    pending_samplers: dict[str, dict[str, str]] = {}
    for path in files:
        if not path.exists():
            continue
        pending_samplers = {}
        for lineno, line in iter_lines(path):
            if _RE_ANY_FUNC.match(line):
                pending_samplers = {}
            decl = _RE_SAMPLER_DECL.search(line)
            if decl:
                pending_samplers[decl.group(1)] = {}
            for assign in _RE_SAMPLER_ASSIGN.finditer(line):
                variable = assign.group(1)
                if variable not in pending_samplers:
                    continue
                field = _SAMPLER_FIELD_MAP.get(assign.group(2))
                if field is not None:
                    pending_samplers[variable][field] = assign.group(3).strip()

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

            # Which mip / slice / plane this descriptor addresses. Two UAVs on
            # one texture at different mips are two distinct bindings, and
            # without this the pair is indistinguishable downstream.
            subresource = _subresource_fields(
                match.group(1) + (match.group(2) or ""), line, match.start()
            )

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
                **subresource,
            )
            if kind is ViewKind.SAMPLER:
                named = next(
                    (
                        fields
                        for variable, fields in pending_samplers.items()
                        if variable in line and fields
                    ),
                    None,
                )
                if named is None and len(pending_samplers) == 1:
                    named = next(iter(pending_samplers.values()))
                if named:
                    view.sampler_desc = dict(named)
                else:
                    # PIX's own helper takes the sampler state positionally:
                    # CreateSampler(dest, filter, addressU, addressV, addressW,
                    # mipLODBias, maxAnisotropy, comparisonFunc, borderColor[4],
                    # minLOD, maxLOD). Zip the arguments onto the field table.
                    positional = _split_call_args(line, match.start())
                    if len(positional) >= 2:
                        values: dict[str, Any] = {}
                        for index, field in enumerate(_SAMPLER_POSITIONAL_FIELDS):
                            if index + 1 < len(positional):
                                values[field] = _coerce_sampler_value(
                                    positional[index + 1]
                                )
                        border = [
                            values.pop(f"border_color_{index}", None)
                            for index in range(4)
                        ]
                        if any(value is not None for value in border):
                            values["border_color"] = border
                        values["parsed"] = (
                            "full"
                            if len(positional) >= len(_SAMPLER_POSITIONAL_FIELDS) + 1
                            else "partial"
                        )
                        view.sampler_desc = values
                # None deliberately stays None when nothing was recorded: a
                # sampler without descriptor content must read as "not in the
                # export", never as a fabricated default.
            if heap_id is not None and heap_index is not None:
                # Last write wins. A heap slot is legitimately written many
                # times across the export, and the *latest* write is the one
                # the shader sees, so this must stay an unconditional assign --
                # do not turn it into setdefault(). The file iteration order set
                # up by descriptor_source_files is what makes that correct.
                views[(heap_id, heap_index)] = view
    return views


# --------------------------------------------------------------------------
# 3. pipeline states
# --------------------------------------------------------------------------
_RE_PSO_FUNC = re.compile(r"^void\s+CreatePipelineState_(\d+)\s*\(")
# Any top-level function definition, used purely as a body terminator.
_RE_ANY_TOP_FUNC = re.compile(r"^void\s+\w+\s*\(")

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
_RE_BLEND_FLAG = re.compile(
    r"pssDesc\.BlendState\.(AlphaToCoverageEnable|IndependentBlendEnable)\s*=\s*(TRUE|FALSE)"
)
_BLEND_FLAG_KEYS = {
    "AlphaToCoverageEnable": "alpha_to_coverage",
    "IndependentBlendEnable": "independent_blend",
}

# --------------------------------------------------------------------------
# Fixed-function state parsing.
#
# CD3DX12_*_DESC constructors are POSITIONAL: the n-th argument is the n-th
# field of the D3D12 desc the constructor fills. The field order below is the
# constructor signature from d3dx12.h (stable since the D3D12 Agility SDK's
# earliest public version). The export may call the constructor with fewer
# arguments (the tail fields keep their D3D12 defaults), which is reported as
# ``parsed: partial`` rather than silently shifting the remaining fields.
# --------------------------------------------------------------------------
_RASTERIZER_FIELDS: tuple[str, ...] = (
    "fill_mode",
    "cull_mode",
    "front_counter_clockwise",
    "depth_bias",
    "depth_bias_clamp",
    "slope_scaled_depth_bias",
    "depth_clip_enable",
    "multisample_enable",
    "antialiased_line_enable",
    "forced_sample_count",
    "conservative_raster",
)

_DEPTH_STENCIL_FIELDS: tuple[str, ...] = (
    "depth_enable",
    "depth_write_mask",
    "depth_func",
    "stencil_enable",
    "stencil_read_mask",
    "stencil_write_mask",
    "front_stencil_fail_op",
    "front_stencil_depth_fail_op",
    "front_stencil_pass_op",
    "front_stencil_func",
    "back_stencil_fail_op",
    "back_stencil_depth_fail_op",
    "back_stencil_pass_op",
    "back_stencil_func",
)

# D3D12_RENDER_TARGET_BLEND_DESC, in struct declaration order. The export writes
# ``blendDesc.RenderTarget[i] = { ... }`` as aggregate initialisation, so the
# braces open with these fields positionally.
_RT_BLEND_FIELDS: tuple[str, ...] = (
    "blend_enable",
    "logic_op_enable",
    "src_blend",
    "dest_blend",
    "blend_op",
    "src_blend_alpha",
    "dest_blend_alpha",
    "blend_op_alpha",
    "logic_op",
    "render_target_write_mask",
)

# D3D12 defaults, used only when the export wrote D3D12_DEFAULT. Reported under
# ``parsed: default`` so a default is never mistaken for a measured value.
_DEFAULT_RASTERIZER: dict[str, Any] = {
    "fill_mode": "D3D12_FILL_MODE_SOLID",
    "cull_mode": "D3D12_CULL_MODE_BACK",
    "front_counter_clockwise": False,
    "depth_bias": 0,
    "depth_bias_clamp": 0.0,
    "slope_scaled_depth_bias": 0.0,
    "depth_clip_enable": True,
    "multisample_enable": False,
    "antialiased_line_enable": False,
    "forced_sample_count": 0,
    "conservative_raster": "D3D12_CONSERVATIVE_RASTERIZATION_MODE_OFF",
}

_DEFAULT_DEPTH_STENCIL: dict[str, Any] = {
    "depth_enable": True,
    "depth_write_mask": "D3D12_DEPTH_WRITE_MASK_ALL",
    "depth_func": "D3D12_COMPARISON_FUNC_LESS",
    "stencil_enable": False,
    "stencil_read_mask": 255,
    "stencil_write_mask": 255,
    "front_stencil_fail_op": "D3D12_STENCIL_OP_KEEP",
    "front_stencil_depth_fail_op": "D3D12_STENCIL_OP_KEEP",
    "front_stencil_pass_op": "D3D12_STENCIL_OP_KEEP",
    "front_stencil_func": "D3D12_COMPARISON_FUNC_ALWAYS",
    "back_stencil_fail_op": "D3D12_STENCIL_OP_KEEP",
    "back_stencil_depth_fail_op": "D3D12_STENCIL_OP_KEEP",
    "back_stencil_pass_op": "D3D12_STENCIL_OP_KEEP",
    "back_stencil_func": "D3D12_COMPARISON_FUNC_ALWAYS",
}

_DEFAULT_RT_BLEND: dict[str, Any] = {
    "blend_enable": False,
    "logic_op_enable": False,
    "src_blend": "D3D12_BLEND_ONE",
    "dest_blend": "D3D12_BLEND_ZERO",
    "blend_op": "D3D12_BLEND_OP_ADD",
    "src_blend_alpha": "D3D12_BLEND_ONE",
    "dest_blend_alpha": "D3D12_BLEND_ZERO",
    "blend_op_alpha": "D3D12_BLEND_OP_ADD",
    "logic_op": "D3D12_LOGIC_OP_NOOP",
    "render_target_write_mask": "D3D12_COLOR_WRITE_ENABLE_ALL",
}


def _balanced(text: str) -> bool:
    """True when every opening bracket in ``text`` is closed."""
    depth = 0
    for char in text:
        if char in "({":
            depth += 1
        elif char in ")}":
            depth -= 1
    return depth <= 0


def _ctor_args(text: str, ctor: str) -> list[str] | None:
    """Top-level arguments of one ``ctor(...)`` call; None when not present.

    Handles calls that span lines and arguments that contain nested braces or
    parens (e.g. a D3D12_STENCIL_OP_DESC{...} argument), by tracking depth
    instead of splitting on every comma.
    """
    start = text.find(ctor)
    if start < 0:
        return None
    open_paren = text.find("(", start)
    if open_paren < 0:
        return None
    args: list[str] = []
    current: list[str] = []
    depth = 0
    for char in text[open_paren:]:
        if char == "(":
            depth += 1
            if depth == 1:
                continue
        elif char == ")":
            depth -= 1
            if depth == 0:
                args.append("".join(current).strip())
                return args
        if depth == 1 and char == ",":
            args.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    return args


def _positional_dict(
    args: list[str] | None,
    fields: tuple[str, ...],
    defaults: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """Zip a constructor's arguments onto its field table.

    Returns (values, parsed_tag). ``D3D12_DEFAULT`` yields the D3D12 defaults
    tagged ``default``; fewer arguments than fields yields ``partial`` (the
    missing fields stay absent rather than being guessed); an exact or
    over-long list yields ``full`` (extra arguments are ignored).
    """
    if args and len(args) == 1 and args[0].strip() == "D3D12_DEFAULT":
        return dict(defaults), "default"
    if not args:
        return {}, "partial"
    values = {
        field: args[index].strip()
        for index, field in enumerate(fields)
        if index < len(args) and args[index].strip()
    }
    tag = "full" if len(args) >= len(fields) else "partial"
    return values, tag


def _coerce_typed(values: dict[str, Any]) -> dict[str, Any]:
    """Turn the raw string arguments into the types their names imply.

    BOOL fields read TRUE/FALSE, numeric fields read as int or float, everything
    else keeps its enum string verbatim (the enum names are what the PIX GUI
    shows and what cross-checks compare against).
    """
    out: dict[str, Any] = {}
    for key, value in values.items():
        text = str(value).strip().rstrip("f")
        upper = text.upper()
        if upper in ("TRUE", "FALSE"):
            out[key] = upper == "TRUE"
            continue
        try:
            out[key] = int(upper)
        except ValueError:
            try:
                out[key] = float(text)
            except ValueError:
                out[key] = text
    return out


def parse_rasterizer_block(text: str) -> dict[str, Any]:
    """Parse a CD3DX12_RASTERIZER_DESC(...) expression (single or multi line)."""
    args = _ctor_args(text, "CD3DX12_RASTERIZER_DESC")
    values, tag = _positional_dict(args, _RASTERIZER_FIELDS, _DEFAULT_RASTERIZER)
    return {"parsed": tag, **_coerce_typed(values)}


def parse_depth_stencil_block(text: str) -> dict[str, Any]:
    """Parse a CD3DX12_DEPTH_STENCIL_DESC(...) expression into the flat shape."""
    args = _ctor_args(text, "CD3DX12_DEPTH_STENCIL_DESC")
    values, tag = _positional_dict(args, _DEPTH_STENCIL_FIELDS, _DEFAULT_DEPTH_STENCIL)
    coerced = _coerce_typed(values)
    payload = {"parsed": tag, **coerced}
    # The stencil pair is split into front/back sub-dicts so a caller can quote
    # one face without unpacking the naming scheme.
    payload["stencil_enable"] = coerced.get("stencil_enable", False)
    payload["front_face"] = {
        key: coerced.get(f"front_stencil_{suffix}")
        for key, suffix in (
            ("fail_op", "fail_op"),
            ("depth_fail_op", "depth_fail_op"),
            ("pass_op", "pass_op"),
            ("func", "func"),
        )
    }
    payload["back_face"] = {
        key: coerced.get(f"back_stencil_{suffix}")
        for key, suffix in (
            ("fail_op", "fail_op"),
            ("depth_fail_op", "depth_fail_op"),
            ("pass_op", "pass_op"),
            ("func", "func"),
        )
    }
    return payload


def parse_blend_rt_block(text: str, index: int) -> dict[str, Any]:
    """Parse one ``blendDesc.RenderTarget[i] = {...}`` aggregate initialiser.

    The aggregate braces open with the D3D12_RENDER_TARGET_BLEND_DESC fields in
    declaration order. ``D3D12_DEFAULT`` inside the braces yields the defaults.
    """
    match = re.search(rf"RenderTarget\[{index}\]\s*=\s*\{{(.*?)\}}", text, re.DOTALL)
    if match is None:
        return {"parsed": "partial"}
    body = match.group(1)
    if "D3D12_DEFAULT" in body:
        values, tag = dict(_DEFAULT_RT_BLEND), "default"
    else:
        values, tag = _positional_dict(
            split_args(body), _RT_BLEND_FIELDS, _DEFAULT_RT_BLEND
        )
    payload = {"parsed": tag, "index": index, **_coerce_typed(values)}
    # The legacy key is a bool; keep it in sync for the existing consumers.
    payload["blend_enable"] = bool(payload.get("blend_enable"))
    payload["logic_op_enable"] = bool(payload.get("logic_op_enable"))
    return payload


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
    block = ""  # a CD3DX12_*_DESC / blend aggregate still being absorbed

    for lineno, line in iter_lines(path):
        if block:
            block += " " + line
            if _balanced(block):
                _apply_state_block(current, block)
                block = ""
            continue

        match = _RE_PSO_FUNC.match(line)
        if match:
            current = PipelineState(
                api_id=int(match.group(1)), source_file=path.name, source_line=lineno
            )
            result.pipeline_states[current.api_id] = current
            pending_read = None
            stage_cursor = 0
            continue
        if _RE_ANY_TOP_FUNC.match(line):
            # Any other top-level function ends the current PSO. CreatePSOs.cpp also
            # holds 81 CreateStateObject_* functions whose DXIL Read() calls would
            # otherwise keep overwriting the last PSO's blob_index, pointing its
            # shader bytecode at a raytracing library blob.
            current = None
            pending_read = None
            continue
        if current is None:
            # Reads outside a PSO body still belong to the resources.bin stream, so
            # they must keep both the counter and read_sizes in step -- read_sizes is
            # the fallback blob index, and dropping entries from it would shift every
            # later blob offset. They just do not belong to any PipelineState.
            match = _RE_READ.search(line)
            if match:
                result.read_sizes.append(int(match.group(1)))
                read_counter += 1
            continue

        if (
            "CD3DX12_RASTERIZER_DESC" in line
            or "CD3DX12_DEPTH_STENCIL_DESC" in line
            or "blendDesc.RenderTarget[" in line
        ):
            # A state block may span lines; absorb until the brackets balance so
            # the whole constructor / aggregate is parsed at once. A single-line
            # block falls through to _apply_state_block immediately.
            if _balanced(line):
                _apply_state_block(current, line)
                continue
            block = line
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

        match = _RE_BLEND_FLAG.search(line)
        if match:
            current.blend.setdefault("parsed", "full")
            current.blend[_BLEND_FLAG_KEYS[match.group(1)]] = match.group(2) == "TRUE"
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


def _apply_state_block(pso: PipelineState | None, text: str) -> None:
    """Feed one complete rasterizer / depth-stencil / blend block into a PSO.

    Updates both the new full dicts and the legacy flat fields, so every
    existing consumer keeps working while the new fields carry the detail.
    """
    if pso is None:
        return

    if "CD3DX12_RASTERIZER_DESC" in text:
        pso.rasterizer = parse_rasterizer_block(text)
        pso.fill_mode = pso.rasterizer.get("fill_mode", "")
        pso.cull_mode = pso.rasterizer.get("cull_mode", "")
        return

    if "CD3DX12_DEPTH_STENCIL_DESC" in text:
        pso.depth_stencil = parse_depth_stencil_block(text)
        pso.depth_enabled = bool(pso.depth_stencil.get("depth_enable"))
        pso.depth_write = "ALL" in str(pso.depth_stencil.get("depth_write_mask", ""))
        pso.depth_func = str(pso.depth_stencil.get("depth_func") or "")
        return

    if "blendDesc.RenderTarget[" in text:
        for match in re.finditer(r"RenderTarget\[(\d+)\]\s*=", text):
            rt = parse_blend_rt_block(text, int(match.group(1)))
            pso.blend.setdefault("parsed", "full")
            pso.blend.setdefault("render_targets", [])
            # One render target per index; a re-exported assignment replaces it.
            pso.blend["render_targets"] = [
                entry for entry in pso.blend["render_targets"] if entry["index"] != rt["index"]
            ]
            pso.blend["render_targets"].append(rt)
            if rt["blend_enable"]:
                pso.blend_enabled = True
            # The legacy blend_states list keeps its two original keys and gains
            # the new ones, so existing callers are untouched and new callers get
            # the full row in the same place they already read.
            legacy = {
                "index": rt["index"],
                "blend_enable": rt["blend_enable"],
                "logic_op_enable": rt["logic_op_enable"],
            }
            legacy.update(
                {key: value for key, value in rt.items() if key != "parsed"}
            )
            replaced = False
            for position, entry in enumerate(pso.blend_states):
                if entry.get("index") == rt["index"]:
                    pso.blend_states[position] = legacy
                    replaced = True
                    break
            if not replaced:
                pso.blend_states.append(legacy)
        return

    if "BlendState = CD3DX12_BLEND_DESC(D3D12_DEFAULT)" in text:
        pso.blend = {
            "parsed": "default",
            "render_targets": [{**_DEFAULT_RT_BLEND, "index": 0}],
        }


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

# D3D12_SAMPLER_DESC field order, used for positional parsing of sampler
# constructors (CD3DX12_STATIC_SAMPLER_DESC takes the shader register first,
# then these fields in this order).
_SAMPLER_FIELDS: tuple[str, ...] = (
    "filter",
    "address_u",
    "address_v",
    "address_w",
    "mip_lod_bias",
    "max_anisotropy",
    "comparison_func",
    "border_color",
    "min_lod",
    "max_lod",
)

# Struct-member name -> the short field key used in payloads.
_SAMPLER_FIELD_MAP: dict[str, str] = {
    "Filter": "filter",
    "AddressU": "address_u",
    "AddressV": "address_v",
    "AddressW": "address_w",
    "MipLODBias": "mip_lod_bias",
    "MaxAnisotropy": "max_anisotropy",
    "ComparisonFunc": "comparison_func",
    "BorderColor": "border_color",
    "MinLOD": "min_lod",
    "MaxLOD": "max_lod",
}

_RE_SAMPLER_ASSIGN = re.compile(
    r"(\w+)\.(Filter|AddressU|AddressV|AddressW|MipLODBias|MaxAnisotropy"
    r"|ComparisonFunc|BorderColor|MinLOD|MaxLOD)\s*=\s*([^;]+)"
)
_RE_SAMPLER_DECL = re.compile(r"D3D12_SAMPLER_DESC\s+(\w+)\s*[;=]")

# PIX's CreateSampler helper signature (Helpers.h): dest descriptor first, then
# the D3D12_SAMPLER_DESC fields in order, BorderColor expanded to four scalars.
_SAMPLER_POSITIONAL_FIELDS: tuple[str, ...] = (
    "filter",
    "address_u",
    "address_v",
    "address_w",
    "mip_lod_bias",
    "max_anisotropy",
    "comparison_func",
    "border_color_0",
    "border_color_1",
    "border_color_2",
    "border_color_3",
    "min_lod",
    "max_lod",
)


def _coerce_sampler_value(text: str) -> Any:
    """int when integral, float when numeric, enum/constant string otherwise."""
    cleaned = text.strip().rstrip("f")
    try:
        return int(cleaned)
    except ValueError:
        try:
            return float(cleaned)
        except ValueError:
            return text.strip()

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
    static_samplers: list[dict[str, Any]] = field(default_factory=list)
    is_local: bool = False
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
        payload: dict[str, Any] = {
            "root_signature_id": self.api_id,
            "parameter_count": len(self.parameters),
            "static_sampler_count": self.static_sampler_count,
            "static_samplers": self.static_samplers,
            "is_local": self.is_local,
            "parameters": [p.to_dict() for p in self.parameters],
            "source": f"{self.source_file}:{self.source_line}",
        }
        return payload


def _parse_static_samplers(text: str) -> list[dict[str, Any]]:
    """Static sampler descriptors around one CreateAndTrackRootSignature call.

    Two export shapes are covered: the positional
    ``CD3DX12_STATIC_SAMPLER_DESC(reg, filter, ...)`` constructor (field order
    from d3dx12.h, first argument is the shader register) and the struct
    field-assignment form (``samplerDesc.Filter = ...``). Anything else is
    simply not reported -- an empty list is honest, a fabricated sampler is not.
    """
    samplers: list[dict[str, Any]] = []
    for match in re.finditer(r"CD3DX12_STATIC_SAMPLER_DESC\d?\s*\(", text):
        args = _ctor_args(text[match.start():], "CD3DX12_STATIC_SAMPLER_DESC")
        if args is None or not args:
            continue
        values: dict[str, Any] = {"shader_register": args[0].strip()}
        for index, field in enumerate(_SAMPLER_FIELDS):
            if index + 1 < len(args) and args[index + 1].strip():
                values[field] = args[index + 1].strip()
        values["parsed"] = "full" if len(args) >= len(_SAMPLER_FIELDS) + 1 else "partial"
        samplers.append(values)

    by_variable: dict[str, dict[str, Any]] = {}
    for match in _RE_SAMPLER_ASSIGN.finditer(text):
        variable = match.group(1)
        field = _SAMPLER_FIELD_MAP.get(match.group(2))
        if field is None:
            continue
        by_variable.setdefault(variable, {"parsed": "partial"})[field] = match.group(3).strip()
    samplers.extend(by_variable.values())
    return samplers


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
                if "LOCAL_ROOT_SIGNATURE" in window:
                    signature.is_local = True
                # The whole block carries the static sampler descriptors; parse
                # what is there rather than just counting them.
                signature.static_samplers = _parse_static_samplers(
                    "\n".join(lines[max(0, block_start - 1) : lineno])
                )
                signatures[signature.api_id] = signature
                parameters = {}
                pending_ranges = []
    return signatures


# --------------------------------------------------------------------------
# 4b. command signatures
# --------------------------------------------------------------------------
# A command signature says what an ExecuteIndirect actually launches. Without it
# an ExecuteIndirect is just an opaque API call: the pipeline type it drives
# (graphics vs compute) is not visible at the call site, yet it decides which of
# the two independent root-binding sets on the command list the shader sees.
_RE_INDIRECT_ARG_TYPE = re.compile(
    r"argumentDescs\[(\d+)\]\.Type\s*=\s*D3D12_INDIRECT_ARGUMENT_TYPE_(\w+)"
)
_RE_CMDSIG_STRIDE = re.compile(r"commandSignatureDesc\.ByteStride\s*=\s*(\d+)")
_RE_CREATE_CMDSIG = re.compile(
    r"CreateAndTrackCommandSignature\(\s*(\d+)\s*,\s*GetRootSignature\((\d+)\)"
)

# The argument type that terminates the command, i.e. the one that decides
# whether the indirect call is a draw or a dispatch. Everything else in a
# signature (root constants, VB/IB views, ...) only patches state beforehand.
_INDIRECT_DISPATCH_TYPES = {"DISPATCH", "DISPATCH_RAYS", "DISPATCH_MESH"}
_INDIRECT_DRAW_TYPES = {"DRAW", "DRAW_INDEXED"}

_INDIRECT_TYPE_TO_KIND: dict[str, EventKind] = {
    "DISPATCH": EventKind.DISPATCH,
    "DISPATCH_MESH": EventKind.DISPATCH,
    "DISPATCH_RAYS": EventKind.DISPATCH_RAYS,
    "DRAW": EventKind.DRAW,
    "DRAW_INDEXED": EventKind.DRAW,
}


@dataclass(slots=True)
class CommandSignature:
    """One ``CreateAndTrackCommandSignature`` block from FrameResources_*.cpp."""

    api_id: int
    argument_types: list[str] = field(default_factory=list)
    byte_stride: int = 0
    root_signature_id: Optional[int] = None
    source_file: str = ""
    source_line: int = 0

    @property
    def command_type(self) -> str:
        """The terminating argument type, e.g. ``DISPATCH`` or ``DRAW_INDEXED``."""
        for name in reversed(self.argument_types):
            if name in _INDIRECT_DISPATCH_TYPES or name in _INDIRECT_DRAW_TYPES:
                return name
        return ""

    @property
    def is_compute(self) -> bool:
        """True when ExecuteIndirect with this signature consumes compute bindings.

        DISPATCH_MESH is deliberately excluded: mesh shaders are dispatched, but
        the amplification/mesh stages live on the *graphics* pipeline and read
        the graphics root arguments, so treating it as compute would snapshot the
        wrong binding set.
        """
        return self.command_type in ("DISPATCH", "DISPATCH_RAYS")

    @property
    def event_kind(self) -> Optional[EventKind]:
        return _INDIRECT_TYPE_TO_KIND.get(self.command_type)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_signature_id": self.api_id,
            "command_type": self.command_type,
            "argument_types": list(self.argument_types),
            "byte_stride": self.byte_stride,
            "root_signature_id": self.root_signature_id,
            "is_compute": self.is_compute,
            "source": f"{self.source_file}:{self.source_line}",
        }


def parse_command_signatures(root: Path) -> dict[int, CommandSignature]:
    """Map command-signature ApiObjectId -> CommandSignature.

    The blocks sit in the same FrameResources_*.cpp files as the root
    signatures, shaped like::

        // ApiObjectId     = 3346
        {
            static D3D12_INDIRECT_ARGUMENT_DESC argumentDescs[1] = {};
            argumentDescs[0].Type = D3D12_INDIRECT_ARGUMENT_TYPE_DISPATCH;
            ...
            CreateAndTrackCommandSignature(3346, GetRootSignature(0), ...);
        }

    Argument descs accumulate until the CreateAndTrack call closes the block, so
    a signature with several argument descs (root constants plus a draw, say) is
    captured in declaration order and ``command_type`` picks the terminator.
    """
    signatures: dict[int, CommandSignature] = {}
    for path in sorted_group(root, "FrameResources"):
        if not path.exists():
            continue
        pending: dict[int, str] = {}
        stride = 0
        block_start = 0
        for lineno, line in iter_lines(path):
            if _RE_API_ID.search(line):
                pending = {}
                stride = 0
                block_start = lineno
                continue

            match = _RE_INDIRECT_ARG_TYPE.search(line)
            if match:
                pending[int(match.group(1))] = match.group(2)
                continue

            match = _RE_CMDSIG_STRIDE.search(line)
            if match:
                stride = int(match.group(1))
                continue

            match = _RE_CREATE_CMDSIG.search(line)
            if match:
                api_id = int(match.group(1))
                signatures[api_id] = CommandSignature(
                    api_id=api_id,
                    argument_types=[pending[k] for k in sorted(pending)],
                    byte_stride=stride,
                    root_signature_id=int(match.group(2)),
                    source_file=path.name,
                    source_line=block_start or lineno,
                )
                pending = {}
                stride = 0
    return signatures


# --------------------------------------------------------------------------
# 4c. command queues -> which queue each command list was submitted to
# --------------------------------------------------------------------------
# WHY THIS PASS EXISTS
#
# ``pixtool export-event-list`` writes one CSV covering exactly one command
# queue. On a capture that spans several queues (Tiled.wpix has three) every
# action recorded on the other queues is simply absent from that CSV, so its
# Queue ID resolves to None. On Tiled.wpix that is 90 draws across 72 passes,
# nearly all of them Lumen async-compute -- reported as a bare null, which reads
# as "unknown" when in fact we know a great deal: exactly which queue ran it.
#
# DO NOT SYNTHESISE A QUEUE ID HERE. It was tried and it is wrong. The hypothesis
# was that Queue ID is a per-queue running index, so it could be recomputed by
# counting API calls on a queue. Counting queue 1 that way yields 102136 rows
# while the real exported event list for queue 1 has 22155 -- the numbering is
# not a call count and does not reconstruct. A fabricated id would look valid,
# would be accepted by every selector, and would silently address a *different*
# action than the caller meant. That is strictly worse than None. This pass
# therefore only attributes ownership; callers that need to address one of those
# draws must use draw_index.
#
# Submissions look like this in RenderFrameWorker_*.cpp:
#
#     ID3D12CommandList* commandLists[2];
#     commandLists[0] = GetCommandList(2971).Get();
#     commandLists[1] = GetCommandList(3058).Get();
#     GetCommandQueue(1)->ExecuteCommandLists(_countof(commandLists), commandLists);
#
# The assignments accumulate until an ExecuteCommandLists consumes them, exactly
# like the argument descs in parse_command_signatures. Some slots are filled with
# PIX's own helper lists (``commandLists[0] = g_utilityCommandList.Get();``, used
# for the Present blit) which have no ApiObjectId at all; those are recorded as
# utility entries and never enter the mapping, because attributing a draw to them
# is meaningless and crashing on them would lose the whole frame.
_RE_CL_ASSIGN = re.compile(r"commandLists\[(\d+)\]\s*=\s*GetCommandList\((\d+)\)")
_RE_CL_UTILITY = re.compile(r"commandLists\[(\d+)\]\s*=\s*(g_\w*CommandList)")
_RE_EXECUTE_LISTS = re.compile(r"GetCommandQueue\((\d+)\)->ExecuteCommandLists")
_RE_CREATE_QUEUE = re.compile(
    r"CreateAndTrackCommandQueue\(\s*(\d+)\s*,\s*g_device"
)
_RE_QUEUE_DESC = re.compile(
    r"D3D12_COMMAND_QUEUE_DESC\s+\w+\s*=\s*\{\s*D3D12_COMMAND_LIST_TYPE_(\w+)"
)
# PIX emits object names as wide raw string literals so a name may contain
# quotes and parentheses; `3D Queue (GPU 0)` does contain parentheses, so the
# body must be matched non-greedily up to the closing )".
_RE_OBJECT_NAME = re.compile(r"GetObject\((\d+)\)->SetName\(LR\"\((.*?)\)\"\)")

# D3D12_COMMAND_LIST_TYPE_* -> the short name used everywhere in the payloads.
_QUEUE_TYPE_MAP = {
    "DIRECT": "direct",
    "COMPUTE": "compute",
    "COPY": "copy",
    "BUNDLE": "bundle",
    "VIDEO_DECODE": "video_decode",
    "VIDEO_PROCESS": "video_process",
    "VIDEO_ENCODE": "video_encode",
}

# Fallback when the queue desc was not exported: UE5 names its queues after their
# role, and PIX passes that name straight through. Matched on the name only after
# the desc lookup fails, so a real D3D12_COMMAND_LIST_TYPE always wins.
_QUEUE_NAME_HINTS = (
    ("compute", "compute"),
    ("copy", "copy"),
    ("3d", "direct"),
    ("direct", "direct"),
    ("graphics", "direct"),
)


@dataclass(slots=True)
class CommandQueue:
    """One ID3D12CommandQueue from the export, with what was submitted to it."""

    api_id: int
    name: str = ""
    list_type: str = ""
    command_list_ids: list[int] = field(default_factory=list)
    submission_count: int = 0
    utility_submission_count: int = 0
    source_file: str = ""
    source_line: int = 0

    @property
    def queue_type(self) -> str:
        """direct / compute / copy, from the queue desc or failing that the name."""
        mapped = _QUEUE_TYPE_MAP.get(self.list_type.upper())
        if mapped:
            return mapped
        lowered = self.name.lower()
        for needle, kind in _QUEUE_NAME_HINTS:
            if needle in lowered:
                return kind
        return ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "queue_object_id": self.api_id,
            "queue_name": self.name,
            "queue_type": self.queue_type,
            "command_list_type": self.list_type,
            "submission_count": self.submission_count,
            "command_list_count": len(self.command_list_ids),
            "utility_submission_count": self.utility_submission_count,
            "source": f"{self.source_file}:{self.source_line}" if self.source_file else "",
        }


@dataclass(slots=True)
class QueueOwnership:
    """command list -> queue, plus the queues themselves.

    ``ambiguous_command_lists`` must stay empty for the mapping to mean anything.
    A command list submitted to two different queues would make "which queue did
    this draw run on" unanswerable from the C++ alone, and the honest response is
    to report no owner rather than pick one. It has never happened on any capture
    inspected so far (90/90 lists on Tiled.wpix resolve to exactly one queue), but
    if you extend this parser, keep checking: silently choosing the first queue
    would mislabel real work.
    """

    queues: dict[int, CommandQueue] = field(default_factory=dict)
    command_list_to_queue: dict[int, int] = field(default_factory=dict)
    ambiguous_command_lists: dict[int, list[int]] = field(default_factory=dict)
    submissions: list[tuple[int, list[int]]] = field(default_factory=list)

    def queue_for_command_list(self, command_list_id: int) -> Optional[CommandQueue]:
        queue_id = self.command_list_to_queue.get(command_list_id)
        if queue_id is None:
            return None
        return self.queues.get(queue_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "queues": [q.to_dict() for q in self.queues.values()],
            "queue_count": len(self.queues),
            "command_lists_attributed": len(self.command_list_to_queue),
            "ambiguous_command_lists": {
                str(k): v for k, v in self.ambiguous_command_lists.items()
            },
        }


def _parse_queue_objects(root: Path) -> dict[int, CommandQueue]:
    """Queue objects declared in FrameResources_*.cpp, with their names.

    The desc sits on the line *above* CreateAndTrackCommandQueue inside the same
    `if (g_constructionNeeded)` block, so the type is carried forward from the
    most recent desc seen rather than parsed out of the create call, which only
    takes a pointer. SetName calls live thousands of lines further down in the
    same file and are keyed by object id, hence the second sweep.
    """
    queues: dict[int, CommandQueue] = {}
    for path in sorted_group(root, "FrameResources"):
        if not path.exists():
            continue
        pending_type = ""
        for lineno, line in iter_lines(path):
            match = _RE_QUEUE_DESC.search(line)
            if match:
                pending_type = match.group(1)
                continue
            match = _RE_CREATE_QUEUE.search(line)
            if match:
                api_id = int(match.group(1))
                queues[api_id] = CommandQueue(
                    api_id=api_id,
                    list_type=pending_type,
                    source_file=path.name,
                    source_line=lineno,
                )
                pending_type = ""

    # Names are applied to whatever object ids exist; a SetName on a
    # non-queue object is simply ignored here.
    for path in sorted_group(root, "FrameResources"):
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _RE_OBJECT_NAME.finditer(text):
            api_id = int(match.group(1))
            queue = queues.get(api_id)
            if queue is not None and not queue.name:
                queue.name = match.group(2)
    return queues


def parse_command_queues(root: Path) -> QueueOwnership:
    """Attribute every submitted command list to the queue that executed it.

    This is the whole basis for reporting queue ownership on actions whose Queue
    ID is missing, and it is derived purely from the export -- pixtool is not
    involved and no identifier is invented. Read the block comment above before
    changing anything here.

    Queues seen only in ExecuteCommandLists but never in a
    CreateAndTrackCommandQueue block still get an entry, so an unusual export
    cannot make a draw's owner disappear; such a queue simply has no name and no
    type. If you touch the regexes, re-verify against the export directly, e.g.::

        parse_command_queues(root).ambiguous_command_lists   # must be {}

    and confirm the queue count still matches what FrameResources declares
    (3 on Tiled.wpix: obj 1 direct, obj 11 compute, obj 2988 copy).
    """
    ownership = QueueOwnership(queues=_parse_queue_objects(root))
    seen: dict[int, set[int]] = {}

    for path in sorted(root.glob("RenderFrameWorker_*.cpp")):
        pending: list[int] = []
        utility = 0
        for lineno, line in iter_lines(path):
            match = _RE_CL_ASSIGN.search(line)
            if match:
                pending.append(int(match.group(2)))
                continue
            if _RE_CL_UTILITY.search(line):
                # PIX's own helper list: no ApiObjectId, so it can never be the
                # command_list_id of a parsed draw. Counted, not mapped.
                utility += 1
                continue

            match = _RE_EXECUTE_LISTS.search(line)
            if not match:
                continue
            queue_id = int(match.group(1))
            queue = ownership.queues.get(queue_id)
            if queue is None:
                queue = CommandQueue(
                    api_id=queue_id, source_file=path.name, source_line=lineno
                )
                ownership.queues[queue_id] = queue
            queue.submission_count += 1
            queue.utility_submission_count += utility
            for command_list_id in pending:
                seen.setdefault(command_list_id, set()).add(queue_id)
                if command_list_id not in queue.command_list_ids:
                    queue.command_list_ids.append(command_list_id)
            ownership.submissions.append((queue_id, list(pending)))
            pending = []
            utility = 0

    for command_list_id, queue_ids in seen.items():
        if len(queue_ids) == 1:
            ownership.command_list_to_queue[command_list_id] = next(iter(queue_ids))
        else:
            # Deliberately left out of command_list_to_queue: see QueueOwnership.
            ownership.ambiguous_command_lists[command_list_id] = sorted(queue_ids)
    return ownership


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
_RE_STATE_OBJECT = re.compile(r"GetStateObject\((\d+)\)")
_RE_RT_PSO_IN_RESET = re.compile(r"GetStateObject\((\d+)\)")
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
        command_signatures: dict[int, CommandSignature] | None = None,
        pipeline_states: dict[int, PipelineState] | None = None,
    ) -> None:
        self.root = Path(root)
        self.views = views or {}
        self.root_signatures = root_signatures or {}
        self.command_signatures = command_signatures or {}
        self.pipeline_states = pipeline_states or {}
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
            "state_object": None,
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
            "blend_factor": None,
            "stencil_ref": None,
            "pending_blend_factor": None,
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
                    # A command list's inherited state can also be a raytracing
                    # state object; the same clearing rule as SetPipelineState1
                    # applies, so report it rather than letting a stale PSO leak.
                    so = _RE_RT_PSO_IN_RESET.search(argtext)
                    if so:
                        fresh["state_object"] = int(so.group(1))
                        fresh["pso"] = None
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
        blend = re.search(r"float\s+\w+\[\]\s*=\s*\{([^}]*)\}", line)
        if blend:
            state["pending_blend_factor"] = [
                float(value.strip().rstrip("f"))
                for value in blend.group(1).split(",")
                if value.strip()
            ]
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
            # A plain SetPipelineState binds a graphics/compute PSO. It clears any
            # raytracing state object bound by SetPipelineState1, because the two
            # pipeline types are mutually exclusive on a command list and reporting
            # both would let a payload quote a state object that is no longer bound.
            state["state_object"] = None
        elif api == "SetPipelineState1":
            # SetPipelineState1 binds a raytracing state object (ID3D12StateObject),
            # not a PSO. Keeping the previous ``pso`` here is the stale-PSO hazard:
            # a DispatchRays executed via ExecuteIndirect would be answered with an
            # unrelated compute shader bound 99 lines earlier. State objects are not
            # modelled yet, so the honest value is None -- the caller is told the
            # pipeline is a state object via ``state_object`` and can degrade.
            state["state_object"] = None
            so = _RE_STATE_OBJECT.search(argtext)
            if so:
                state["state_object"] = int(so.group(1))
            state["pso"] = None
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
        elif api == "OMSetBlendFactor":
            # Signature: OMSetBlendFactor(const FLOAT BlendFactor[4]). The export
            # declares the array one line above and passes the variable; inline
            # arrays are handled too. The values ride on the command list and
            # only matter when a PSO enables D3D12_BLEND_FACTOR.
            numbers = re.findall(r"-?[\d.]+", argtext)
            if len(numbers) >= 4:
                state["blend_factor"] = [float(value) for value in numbers[:4]]
            elif state["pending_blend_factor"]:
                state["blend_factor"] = list(state["pending_blend_factor"])
            state["pending_blend_factor"] = None
        elif api == "OMSetStencilRef":
            numbers = _ints(argtext)
            state["stencil_ref"] = numbers[0] if numbers else None
        elif api in ("ClearRenderTargetView", "ClearDepthStencilView"):
            # A clear builds its own descriptor into the scratch heap on the line
            # above and consumes it immediately. If it is left in the pending list
            # it leaks into the next OMSetRenderTargets fallback, which then
            # reports the cleared resource as a target of an unrelated draw. On
            # Tiled.wpix six consecutive clears did exactly that, and the following
            # single-target draw came out bound to all six.
            state["inline_rtv_res"] = []
            state["inline_dsv_res"] = None

        elif api == "OMSetRenderTargets":
            numbers = _ints(argtext)
            count = numbers[0] if numbers else 0
            rtvs = state["pending_rtv"][-count:] if count else []
            state["rtv_heap"] = list(rtvs)
            state["dsv"] = state["pending_dsv"]
            state["rt_res"] = self._resolve_render_targets(rtvs, state, count)
            state["ds_res"] = self._resolve_depth(state["dsv"], state)
            state["pending_rtv"] = []
            state["pending_dsv"] = None
            # The inline RTV/DSV descriptors are consumed by this call and must not
            # survive it. They used to accumulate for the whole command list, and
            # because the fallback below deduplicated the entire list, a draw that
            # bound a single render target was reported as binding every target the
            # command list had ever created -- a false positive that is
            # indistinguishable from a real multi-target bind. See
            # _resolve_render_targets for why the fallback exists at all.
            state["inline_rtv_res"] = []
            state["inline_dsv_res"] = None

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

    def _resolve_render_targets(self, rtvs, state, count: int | None = None) -> list[int]:
        """Resolve the bound render targets to resource ids.

        Two sources, in priority order. Descriptor-heap handles are authoritative
        when present. But PIX also emits an inline form, where the RTV is created
        straight into a scratch heap and bound via
        GetCPUDescriptorHandleForHeapStart(), which carries no heap index and so
        yields no ``pending_rtv`` entry at all -- hence the fallback.

        The fallback is bounded by ``count``: OMSetRenderTargets states how many
        targets it binds, and only the last ``count`` inline descriptors belong to
        this call. Without that bound the fallback reported every inline RTV the
        command list had created, turning a one-target bind into a six-target one.
        """
        out: list[int] = []
        for heap_id, index in rtvs:
            view = self.views.get((heap_id, index))
            if view is not None and view.resource_id is not None:
                out.append(view.resource_id)
        if not out and state["inline_rtv_res"]:
            inline = list(dict.fromkeys(state["inline_rtv_res"]))
            if count:
                inline = inline[-count:]
            out = inline
        return out


    def _resolve_depth(self, dsv, state):
        if dsv is not None:
            view = self.views.get(dsv)
            if view is not None and view.resource_id is not None:
                return view.resource_id
        return state["inline_dsv_res"]

    def _consumes_compute_bindings(
        self,
        kind: EventKind,
        api: str,
        state: dict,
        command_signature: "CommandSignature | None",
    ) -> bool:
        """Decide which of the two root-binding sets this action reads.

        A command list carries graphics and compute root arguments completely
        independently, so picking the wrong set does not degrade gracefully -- it
        reports an empty binding list, which reads as "this action binds nothing"
        and is indistinguishable from a real missing binding.

        Dispatch / DispatchRays are unambiguously compute and DispatchMesh is
        unambiguously graphics. ExecuteIndirect is the one case that cannot be
        decided at the call site: identical C++ drives either pipeline depending
        on the command signature's argument type, which lives in
        FrameResources_*.cpp. Resolving it there is the fix; the fallbacks below
        only cover exports where that block is missing.
        """
        if kind in (EventKind.DISPATCH, EventKind.DISPATCH_RAYS):
            return True
        if api != "ExecuteIndirect":
            return False

        if command_signature is not None:
            return command_signature.is_compute

        # No command signature parsed. Prefer the PSO, which knows its own
        # pipeline type, over guessing from the API name.
        pso = self.pipeline_states.get(state["pso"]) if state["pso"] is not None else None
        if pso is not None:
            return pso.is_compute
        # Last resort: only one of the two sets was ever bound on this list.
        if state["compute_rootsig"] is not None and state["gfx_rootsig"] is None:
            return True
        return False

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
        command_signature: CommandSignature | None = None
        if api == "ExecuteIndirect":
            ids = _ints(argtext)
            command_signature = self.command_signatures.get(ids[0]) if ids else None

        is_compute = self._consumes_compute_bindings(kind, api, state, command_signature)
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
            state_object_id=state["state_object"],
            root_signature_id=active_rootsig,
            primitive_topology=state["topology"],
            blend_factor=state["blend_factor"],
            stencil_ref=state["stencil_ref"],
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
            if len(numbers) >= 2:
                draw.indirect_max_command_count = numbers[1]
            if command_signature is not None:
                draw.command_signature_id = command_signature.api_id
                draw.indirect_command_type = command_signature.command_type
                draw.indirect_byte_stride = command_signature.byte_stride
                # The counts themselves live in GPU memory and are only known at
                # execution time; the type tells the caller which fields the
                # indirect argument buffer holds.
                draw.indirect_arguments_are_gpu_resident = True

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
