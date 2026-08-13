"""Shader binding tables: the D3D12_DISPATCH_RAYS_DESC behind each raytracing action.

There is no ``DispatchRays`` call anywhere in this export. Every raytracing
dispatch is an ``ExecuteIndirect`` whose argument buffer was filled at init time
by ``CreateIndirectArgumentBuffer_<key>()``, and it is that function -- not the
command list -- that holds the dispatch dimensions and the four shader-table
regions. So the chain from an action to its shaders is::

    SetPipelineState1(GetStateObject(3891))
    ExecuteIndirect(..., g_indirectArgumentBuffers["1415_1"], ...)
        -> CreateIndirectArgumentBuffer_1415_1()
             GetStateObject(3891)->QueryInterface(...)      # which pipeline
             D3D12_DISPATCH_RAYS_DESC { ...regions..., 232, 1, 1 }
             CreateShaderTable_00/01(...)                   # record contents

Each link is stated verbatim in the export, so nothing here is inferred.

Two traps that this module handles explicitly, because getting either wrong
produces a confident wrong answer rather than an error:

* the region sizes in the desc are *not* the sizes of the buffers holding them
  (raygen: a 64-byte region inside a 2,715,136-byte allocation);
* the hit-group and miss regions live in one buffer, so a single
  ``CreateShaderTable_*`` function writes records belonging to both, and a record
  must be classified by offset against the desc rather than by which function
  wrote it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .cppparse import iter_lines, sorted_group
from .model import ShaderBindingTable, ShaderRecord, ShaderTableRegion
from .stateobject import parse_raw_strings, split_top_level

_RE_INDIRECT_FUNC = re.compile(r"^void\s+CreateIndirectArgumentBuffer_([0-9_]+)\s*\(")
_RE_TABLE_FUNC = re.compile(r"^void\s+CreateShaderTable_(\d+)\s*\(")
_RE_ANY_FUNC = re.compile(r"^void\s+\w+\s*\(")
_RE_BUFFER_KEY = re.compile(r'g_indirectArgumentBuffers\["([^"]+)"\]\s*=')
_RE_STATE_OBJECT_QUERY = re.compile(r"GetStateObject\((\d+)\)->QueryInterface")
_RE_SHADER_IDENTIFIER = re.compile(r"GetShaderIdentifier\(\s*LR\"\((.*?)\)\"\s*\)")
_RE_TABLE_CALL = re.compile(r"CreateShaderTable_(\d+)\(\s*stateObjectProperties")
_RE_TABLE_VARIABLE = re.compile(r"(\w+)->GetGPUVirtualAddress\(\)")
_RE_TABLE_ASSIGN = re.compile(
    r"(\w+)\s*=\s*CreateGenericReadUploadBufferFromBytes\(\s*\w+[^,]*,\s*(\d+)"
)
_RE_DISPATCH_RAYS = re.compile(r"D3D12_DISPATCH_RAYS_DESC\s+\w+\s*=\s*\{(.*)\}\s*;")
_RE_OUTPUT_VECTOR = re.compile(r"std::vector<byte>\s+output\(\s*(\d+)\s*\)")
_RE_RECORD_OFFSET = re.compile(r"&output\[\s*(\d+)\s*\]")
_RE_ROOT_CONSTANTS = re.compile(r"AddRootConstants\(\s*\{([^}]*)\}\s*\)")
_RE_ADD_GPUVA = re.compile(r"AddGpuva\(\s*GetGpuva\(\s*(\d+)\s*,\s*(\d+)\s*\)")

# Which local variable each table region is built into, so the buffer allocation
# size can be reported next to the region size. Purely for the "region size is
# not buffer size" distinction; nothing depends on the names for correctness.
_REGION_ORDER = ("raygen", "miss", "hit_group", "callable")


def _parse_dispatch_rays_desc(
    text: str,
) -> Optional[tuple[dict[str, Optional[ShaderTableRegion]], dict[str, str], int, int, int]]:
    """Parse ``{ {a,b}, {c,d,e}, {f,g,h}, {i,j,k}, W, H, D }``.

    Brace-aware on purpose: the four groups have different element counts (raygen
    is an address/size pair, the others are address/size/stride triples), so
    flattening the numbers and reading them positionally shifts every field after
    the first group.

    Also returns which buffer variable each region's address came from. That name
    is what ties a region to the ``CreateShaderTable_*`` call that filled it, and
    it is stated in the export -- so the association needs no guessing about
    argument order.
    """
    fields = split_top_level(text)
    if len(fields) < 7:
        return None
    regions: dict[str, Optional[ShaderTableRegion]] = {}
    buffers: dict[str, str] = {}
    for name, field_text in zip(_REGION_ORDER, fields[:4]):
        numbers = [
            int(token)
            for token in re.findall(r"\b(\d+)(?:ull|ULL|u|U)?\b", field_text)
        ]
        # The address expression contributes no literal for a live table
        # (``x->GetGPUVirtualAddress()``) but does contribute ``0`` for an empty
        # one (``{ 0ull, 0, 0 }``), so trailing values are read from the right.
        if name == "raygen":
            size = numbers[-1] if numbers else 0
            stride = 0
        else:
            size = numbers[-2] if len(numbers) >= 2 else 0
            stride = numbers[-1] if len(numbers) >= 2 else 0
        variable = _RE_TABLE_VARIABLE.search(field_text)
        if variable:
            buffers[name] = variable.group(1)
        if not size:
            # An all-zero region means the pipeline has no such shader class. That
            # is different from a region with zero records, so it stays None.
            regions[name] = None
            continue
        regions[name] = ShaderTableRegion(size_in_bytes=size, stride_in_bytes=stride)
    tail = [int(token) for token in re.findall(r"\b(\d+)\b", ",".join(fields[4:7]))]
    width, height, depth = (tail + [0, 0, 0])[:3]
    return regions, buffers, width, height, depth


def _parse_shader_tables(root: Path) -> dict[str, list[ShaderRecord]]:
    """``CreateShaderTable_<n>`` -> its records, offsets verbatim, table unset.

    ``table`` is deliberately left blank here: it can only be decided once the
    dispatch desc that consumes the function is known, and this function is
    reachable from more than one desc in principle.
    """
    tables: dict[str, list[ShaderRecord]] = {}
    for path in sorted_group(root, "ShaderTableReconstruction"):
        if not path.exists():
            continue
        current: Optional[str] = None
        pending: Optional[ShaderRecord] = None
        for lineno, line in iter_lines(path):
            match = _RE_TABLE_FUNC.match(line)
            if match:
                current = f"CreateShaderTable_{match.group(1)}"
                tables.setdefault(current, [])
                pending = None
                continue
            if _RE_ANY_FUNC.match(line):
                current = None
                pending = None
                continue
            if current is None:
                continue

            identifier = _RE_SHADER_IDENTIFIER.search(line)
            if identifier:
                offset_match = _RE_RECORD_OFFSET.search(line)
                names = parse_raw_strings(line)
                pending = ShaderRecord(
                    offset=int(offset_match.group(1)) if offset_match else 0,
                    shader_identifier=names[0] if names else identifier.group(1),
                    reconstruction_function=current,
                    source_line=lineno,
                )
                tables[current].append(pending)
                continue
            if pending is None:
                continue

            constants = _RE_ROOT_CONSTANTS.search(line)
            if constants:
                pending.root_constants = [
                    int(token)
                    for token in re.findall(r"-?\d+", constants.group(1))
                ]
                continue
            gpuva = _RE_ADD_GPUVA.search(line)
            if gpuva:
                pending.root_gpuvas.append((int(gpuva.group(1)), int(gpuva.group(2))))
    return tables


def parse_shader_binding_tables(root: Path) -> dict[str, ShaderBindingTable]:
    """Every dispatch-rays desc in the export, keyed by indirect buffer name.

    The key is the same string ``DrawCall.indirect_argument_buffer`` already
    stores, so associating an action with its shader tables is a dict lookup and
    involves no matching heuristic.

    Empty for a capture with no raytracing.
    """
    record_source = _parse_shader_tables(root)
    tables: dict[str, ShaderBindingTable] = {}

    for path in sorted_group(root, "CreateAndInitResources"):
        if not path.exists():
            continue
        key: Optional[str] = None
        state_object_id: Optional[int] = None
        raygen_identifier = ""
        functions: list[str] = []
        # buffer variable -> the CreateShaderTable_* that filled it, and its size.
        # Both are stated in the export within a few lines of each other, which is
        # what makes region attribution exact instead of positional.
        filled_by: dict[str, str] = {}
        buffer_size: dict[str, int] = {}
        pending_table: Optional[str] = None
        pending_size = 0
        start_line = 0
        for lineno, line in iter_lines(path):
            match = _RE_INDIRECT_FUNC.match(line)
            if match:
                key = None
                state_object_id = None
                raygen_identifier = ""
                functions = []
                filled_by = {}
                buffer_size = {}
                pending_table = None
                pending_size = 0
                start_line = lineno
                continue
            if _RE_ANY_FUNC.match(line):
                key = None
                continue

            match = _RE_BUFFER_KEY.search(line)
            if match:
                key = match.group(1)
                continue
            if key is None:
                continue

            match = _RE_STATE_OBJECT_QUERY.search(line)
            if match:
                state_object_id = int(match.group(1))
                continue

            match = _RE_OUTPUT_VECTOR.search(line)
            if match:
                pending_size = int(match.group(1))
                continue

            if not raygen_identifier and "GetShaderIdentifier" in line:
                # The raygen record is copied inline here rather than through a
                # CreateShaderTable_* call, so this is the only place its
                # identifier appears.
                names = parse_raw_strings(line)
                if names:
                    raygen_identifier = names[0]

            match = _RE_TABLE_CALL.search(line)
            if match:
                pending_table = f"CreateShaderTable_{match.group(1)}"
                functions.append(pending_table)
                continue

            match = _RE_TABLE_ASSIGN.search(line)
            if match:
                variable = match.group(1)
                buffer_size[variable] = pending_size or int(match.group(2))
                if pending_table is not None:
                    filled_by[variable] = pending_table
                pending_table = None
                pending_size = 0
                continue

            match = _RE_DISPATCH_RAYS.search(line)
            if not match:
                continue
            parsed = _parse_dispatch_rays_desc(match.group(1))
            if parsed is None:
                continue
            regions, buffers, width, height, depth = parsed
            for name, variable in buffers.items():
                region = regions.get(name)
                if region is not None:
                    region.buffer_size_in_bytes = buffer_size.get(variable, 0)
            sbt = ShaderBindingTable(
                indirect_buffer_key=key,
                state_object_id=state_object_id,
                raygen=regions.get("raygen"),
                miss=regions.get("miss"),
                hit_group=regions.get("hit_group"),
                callable_table=regions.get("callable"),
                width=width,
                height=height,
                depth=depth,
                raygen_identifier=raygen_identifier,
                reconstruction_functions=list(functions),
                source_file=path.name,
                source_line=start_line,
            )
            _attach_records(sbt, record_source, buffers, filled_by)
            tables[key] = sbt

    return tables


def _attach_records(
    sbt: ShaderBindingTable,
    source: dict[str, list[ShaderRecord]],
    buffers: dict[str, str],
    filled_by: dict[str, str],
) -> None:
    """Copy each reconstruction function's records onto one SBT, classified.

    A region's home buffer is known by name from the desc, and each buffer's
    filling function is known by name from the assignment right above it. So for
    any function we know exactly which regions share its buffer, and a record is
    assigned by comparing its offset against those regions' extents.

    The alternative -- reading the function's ordinal position -- puts the
    ``&output[131072]`` record of ``CreateShaderTable_01`` into the hit-group
    table, when the desc says the hit-group region is 131072 bytes long and the
    miss region begins there.
    """
    # function -> the regions living in the buffer it fills, in desc order.
    regions_of: dict[str, list[str]] = {}
    for name in _REGION_ORDER:
        variable = buffers.get(name)
        if variable is None or sbt.region(name) is None:
            continue
        function = filled_by.get(variable)
        if function is None:
            continue
        regions_of.setdefault(function, []).append(name)

    for function in sbt.reconstruction_functions:
        names = regions_of.get(function, [])
        for template in source.get(function, []):
            record = ShaderRecord(
                offset=template.offset,
                shader_identifier=template.shader_identifier,
                root_constants=list(template.root_constants),
                root_gpuvas=list(template.root_gpuvas),
                reconstruction_function=function,
                source_line=template.source_line,
            )
            record.table, record.in_declared_region = _classify(
                sbt, names, record.offset
            )
            sbt.records.append(record)

    if sbt.raygen is not None and sbt.raygen_identifier:
        # Added explicitly because it is written inline rather than by a
        # CreateShaderTable_* call; omitting it would report zero raygen records
        # for a dispatch that plainly has one.
        sbt.records.insert(
            0,
            ShaderRecord(
                offset=0,
                shader_identifier=sbt.raygen_identifier,
                table="raygen",
                reconstruction_function="inline",
                source_line=sbt.source_line,
            ),
        )


def _classify(
    sbt: ShaderBindingTable, names: list[str], offset: int
) -> tuple[str, bool]:
    """Which region a record at ``offset`` belongs to, and whether it is read.

    Returns ``(table, in_declared_region)``. A record inside the declared region
    of the buffer it was written into belongs to that region and is read by the
    dispatch. A record past the end is *not* read: it belongs to the buffer, not
    to any region the desc names.

    This is where the trap sits. ``CreateShaderTable_01`` fills the 147,456-byte
    hit-group buffer and writes a miss identifier at offset 131,072 -- exactly the
    end of the 131,072-byte hit-group region -- because the application originally
    packed both tables into one allocation. But this dispatch's miss region points
    at a different buffer, filled by ``CreateShaderTable_00``. Reporting that
    trailing record as a hit group would misattribute a shader; reporting it as
    the dispatch's miss record would double-count one that is already accounted
    for. It is reported as its own layout region with ``in_declared_region``
    false, which is what the export actually supports.
    """
    if not names:
        return "", True
    cursor = 0
    for name in names:
        region = sbt.region(name)
        if region is None:
            continue
        region.start_offset = cursor
        if offset < cursor + region.size_in_bytes:
            return name, True
        cursor += region.size_in_bytes
    return f"{names[-1]}_buffer_tail", False
