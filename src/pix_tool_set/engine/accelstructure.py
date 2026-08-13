"""Raytracing acceleration structures: TLAS builds, instances, and replayed blobs.

Three separate things in the export describe acceleration structures, and only the
first two carry information a user can act on:

* ``CommandLists_*.cpp`` -- the ``BuildRaytracingAccelerationStructure`` calls,
  with their inputs (type, build flags, descriptor count) and destination;
* ``RaytracingInstanceDescs_*.cpp`` -- the TLAS instance array: transform,
  InstanceID, mask, ``InstanceContributionToHitGroupIndex``, and the BLAS address;
* ``AccelStructureRecreation_*.cpp`` -- how PIX rebuilds the structures for
  replay, which is a *driver-private serialized blob* fed through
  ``CopyRaytracingAccelerationStructure(..., DESERIALIZE)``.

The third one sets a hard boundary that this module refuses to cross. A serialized
BLAS contains no ``D3D12_RAYTRACING_GEOMETRY_DESC``, so the export simply does not
say how many triangles or vertices a BLAS holds. The blob size is available and
correlates loosely with geometry volume, which makes "estimate triangles from blob
size" a tempting and completely unfounded step. Every payload here reports the
blob size and reports geometry counts as unavailable, with the reason attached.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .cppparse import iter_lines, sorted_group
from .model import (
    AccelerationStructureBuild,
    AccelerationStructureInstance,
    AccelerationStructurePostbuildInfo,
    SerializedAccelerationStructure,
)

_RE_CL_FUNC = re.compile(r"^void\s+PopulateCommandList_(\d+)\s*\(")
_RE_GLOBAL_ID = re.compile(r"//\s*GlobalId\s*=\s*(\d+)")
_RE_AS_TYPE = re.compile(
    r"inputs\.Type\s*=\s*D3D12_RAYTRACING_ACCELERATION_STRUCTURE_TYPE_(\w+)"
)
_RE_AS_FLAGS = re.compile(r"inputs\.Flags\s*=\s*(.+?);")
_RE_AS_NUM_DESCS = re.compile(r"inputs\.NumDescs\s*=\s*(\d+)")
_RE_AS_LAYOUT = re.compile(r"inputs\.DescsLayout\s*=\s*D3D12_ELEMENTS_LAYOUT_(\w+)")
_RE_AS_POPULATE = re.compile(r"(PopulateRaytracingInstanceDescs_\d+)\s*\(")
_RE_AS_DESC = re.compile(
    r"D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_DESC\s+\w+\s*=\s*\{\s*"
    r"GetGpuva\((\d+),\s*(\d+)\)\s*,\s*\w+\s*,\s*GetGpuva\((\d+),\s*(\d+)\)\s*,\s*"
    r"GetGpuva\((\d+),\s*(\d+)\)"
)
_RE_AS_BUILD_CALL = re.compile(
    r"GetCommandList\((\d+)\)->BuildRaytracingAccelerationStructure"
)
_RE_PIX_BEGIN = re.compile(r"PIXBeginEvent\(")
_RE_PIX_END = re.compile(r"PIXEndEvent\(")

_RE_INSTANCE_FUNC = re.compile(r"^void\s+(PopulateRaytracingInstanceDescs_\d+)\s*\(")
_RE_INSTANCE_ENTRY = re.compile(r"instanceDescs\[(\d+)\]\s*=\s*\{\s*\{(.*?)\}\s*,(.*?)\}\s*;")

_RE_RECREATE_FUNC = re.compile(
    r"^void\s+(RecreateAccelStructure_(\d+)_(\d+)_(\d+))\s*\("
)
_RE_SERIALIZED_HEADER = re.compile(
    r"D3D12_SERIALIZED_RAYTRACING_ACCELERATION_STRUCTURE_HEADER\s+\w+\s*=\s*"
    r"\{\s*\w+\s*,\s*(\d+)\s*,\s*(\d+)"
)


def _normalise_as_flags(text: str) -> list[str]:
    flags: list[str] = []
    for token in re.split(r"[|]", text):
        token = token.strip()
        if not token or token.endswith("_NONE"):
            continue
        flags.append(
            token.replace("D3D12_RAYTRACING_ACCELERATION_STRUCTURE_BUILD_FLAG_", "").lower()
        )
    return flags


def parse_instance_descs(root: Path) -> dict[str, list[AccelerationStructureInstance]]:
    """``PopulateRaytracingInstanceDescs_<n>`` -> its instance array.

    Field order is fixed by D3D12_RAYTRACING_INSTANCE_DESC and is read positionally
    after the 12-float transform: ``InstanceID, InstanceMask,
    InstanceContributionToHitGroupIndex, Flags, AccelerationStructure``. The third
    integer is the one that links an instance to a hit-group record, so a shifted
    read here would silently attribute the wrong raytracing material to every
    object in the scene.
    """
    out: dict[str, list[AccelerationStructureInstance]] = {}
    for path in sorted_group(root, "RaytracingInstanceDescs"):
        if not path.exists():
            continue
        current: Optional[str] = None
        for lineno, line in iter_lines(path):
            match = _RE_INSTANCE_FUNC.match(line)
            if match:
                current = match.group(1)
                out.setdefault(current, [])
                continue
            if current is None:
                continue
            match = _RE_INSTANCE_ENTRY.search(line)
            if not match:
                continue
            transform = [float(token) for token in re.findall(r"-?\d+\.?\d*(?:e[-+]?\d+)?f?", match.group(2).replace("f", ""))]
            tail = match.group(3)
            gpuva = re.search(r"GetGpuva\((\d+),\s*(\d+)\)", tail)
            numbers = [int(token) for token in re.findall(r"(?<![\d.])(\d+)(?![\d.f])", tail.split("GetGpuva")[0])]
            numbers = (numbers + [0, 0, 0, 0])[:4]
            out[current].append(
                AccelerationStructureInstance(
                    index=int(match.group(1)),
                    transform=transform[:12],
                    instance_id=numbers[0],
                    instance_mask=numbers[1],
                    contribution_to_hit_group_index=numbers[2],
                    flags=numbers[3],
                    blas_resource_id=int(gpuva.group(1)) if gpuva else None,
                    blas_byte_offset=int(gpuva.group(2)) if gpuva else 0,
                    source_file=path.name,
                    source_line=lineno,
                )
            )
    return out


def parse_acceleration_structure_builds(root: Path) -> list[AccelerationStructureBuild]:
    """Every BuildRaytracingAccelerationStructure call, in submission order.

    Marker paths are tracked with a plain stack rather than reconciled against the
    event list, because these builds are non-draw commands the draw parser does
    not produce and so have no reconciled path to borrow. The path is therefore
    best-effort context, and the Global ID above each block is the authoritative
    identifier.
    """
    builds: list[AccelerationStructureBuild] = []
    instances = parse_instance_descs(root)
    for path in sorted_group(root, "CommandLists"):
        if not path.exists():
            continue
        markers: list[str] = []
        pending_gid: Optional[int] = None
        current: Optional[AccelerationStructureBuild] = None
        for lineno, line in iter_lines(path):
            if _RE_PIX_BEGIN.search(line):
                names = re.findall(r'LR"\((.*?)\)"', line)
                if names:
                    markers.append(names[-1])
                continue
            if _RE_PIX_END.search(line):
                if markers:
                    markers.pop()
                continue

            match = _RE_GLOBAL_ID.search(line)
            if match:
                pending_gid = int(match.group(1))
                continue

            match = _RE_AS_TYPE.search(line)
            if match:
                current = AccelerationStructureBuild(
                    global_id=pending_gid,
                    command_list_id=None,
                    type=match.group(1).lower(),
                    marker_path=tuple(markers),
                    source_file=path.name,
                    source_line=lineno,
                )
                continue
            if current is None:
                continue

            match = _RE_AS_FLAGS.search(line)
            if match:
                current.flags = _normalise_as_flags(match.group(1))
                continue
            match = _RE_AS_NUM_DESCS.search(line)
            if match:
                current.num_descs = int(match.group(1))
                continue
            match = _RE_AS_LAYOUT.search(line)
            if match:
                current.descs_layout = match.group(1).lower()
                continue
            match = _RE_AS_POPULATE.search(line)
            if match:
                current.instances_function = match.group(1)
                current.instances = list(instances.get(match.group(1), []))
                continue
            match = _RE_AS_DESC.search(line)
            if match:
                current.dest_resource_id = int(match.group(1))
                current.dest_byte_offset = int(match.group(2))
                source_id = int(match.group(3))
                # GetGpuva(0, 0) is the null source address of a fresh build, not
                # resource 0; reporting it as a resource would invent an update-in-place.
                current.source_resource_id = source_id or None
                current.scratch_resource_id = int(match.group(5))
                current.scratch_byte_offset = int(match.group(6))
                continue
            match = _RE_AS_BUILD_CALL.search(line)
            if match:
                current.command_list_id = int(match.group(1))
                builds.append(current)
                current = None
                pending_gid = None
    return builds


def parse_serialized_structures(root: Path) -> list[SerializedAccelerationStructure]:
    """The RecreateAccelStructure_* blocks, one per serialized blob.

    Only the function signature and the serialized/deserialized sizes are read.
    The blob body is intentionally not touched: it is driver-private, carries no
    geometry description, and the file is around a megabyte of it.
    """
    out: list[SerializedAccelerationStructure] = []
    for path in sorted_group(root, "AccelStructureRecreation"):
        if not path.exists():
            continue
        current: Optional[SerializedAccelerationStructure] = None
        for lineno, line in iter_lines(path):
            match = _RE_RECREATE_FUNC.match(line)
            if match:
                current = SerializedAccelerationStructure(
                    resource_id=int(match.group(2)),
                    byte_offset=int(match.group(3)),
                    sequence=int(match.group(4)),
                    function=match.group(1),
                    source_file=path.name,
                    source_line=lineno,
                )
                out.append(current)
                continue
            if current is None:
                continue
            match = _RE_SERIALIZED_HEADER.search(line)
            if match:
                current.serialized_size = int(match.group(1))
                current.deserialized_size = int(match.group(2))
                current = None
    return out


_RE_POSTBUILD_CALL = re.compile(
    r"GetCommandList\((\d+)\)->EmitRaytracingAccelerationStructurePostbuildInfo"
)
_RE_POSTBUILD_INFO_TYPE = re.compile(
    r"D3D12_RAYTRACING_ACCELERATION_STRUCTURE_POSTBUILD_INFO_(\w+)"
)


def parse_postbuild_info(root: Path) -> list[AccelerationStructurePostbuildInfo]:
    """EmitRaytracingAccelerationStructurePostbuildInfo calls, when the frame made any.

    These are the only place a driver reports the actual (current / compacted /
    serialized) size of an acceleration structure. They exist conditionally: an
    application that sizes its scratch conservatively and never compacts never
    emits one, so an empty result is a fact about the capture, not a parse gap.
    The D3D12 API speaks in ``D3D12_RAYTRACING_ACCELERATION_STRUCTURE_POSTBUILD_INFO_TYPE``
    tokens (COMPACTED_SIZE / CURRENT_SIZE / SERIALIZATION / TOOLS_VISUALIZATION); the
    concrete byte values land in a GPU-visible buffer the application reads back,
    so only the requested info types -- not the resulting numbers -- are reported.
    """
    out: list[AccelerationStructurePostbuildInfo] = []
    for path in sorted_group(root, "CommandLists"):
        if not path.exists():
            continue
        pending_gid: Optional[int] = None
        current: Optional[AccelerationStructurePostbuildInfo] = None
        for lineno, line in iter_lines(path):
            match = _RE_GLOBAL_ID.search(line)
            if match:
                pending_gid = int(match.group(1))
                continue
            if current is None:
                # The acceleration-structure argument and the info-type array both
                # precede the call itself, so start a record on the first info-type
                # token seen.
                if _RE_POSTBUILD_INFO_TYPE.search(line):
                    current = AccelerationStructurePostbuildInfo(
                        global_id=pending_gid,
                        acceleration_structure_resource_id=None,
                        command_list_id=None,
                        source_file=path.name,
                        source_line=lineno,
                    )
                continue
            match = _RE_POSTBUILD_INFO_TYPE.search(line)
            if match:
                info_type = match.group(1).lower()
                if info_type not in current.info_types:
                    current.info_types.append(info_type)
            match = _RE_POSTBUILD_CALL.search(line)
            if match:
                current.command_list_id = int(match.group(1))
                out.append(current)
                current = None
                pending_gid = None
    return out
