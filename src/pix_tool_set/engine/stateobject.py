"""DXR state objects: CreatePSOs.cpp's CreateStateObject_* functions.

A raytracing pipeline reaches the GPU through a shape nothing else in this export
uses. ``CreateStateObject_3892`` builds a COLLECTION that owns one DXIL library
and one hit group; ``CreateStateObject_3930`` builds a RAYTRACING_PIPELINE that
owns almost nothing and instead references dozens of those collections, growing
itself across three ``AddToStateObject`` segments. Reading a single function body
and reporting its subobjects is therefore not a partial answer, it is a wrong
one: RTPSO 3930's own body declares zero exports, so a caller asking "which
shaders can this dispatch run" would be told "none".

This module is a standalone file-level sweep -- it only ever reads
``CreatePSOs.cpp`` and shares no state with the command-list replay in
``cppparse`` -- which is why it lives next to ``bindinglabel`` and
``resourceevents`` rather than inside the 2000-line parser.

Two things here are easy to get wrong and are called out at their sites:

* the number in ``g_resourceReader->Read(dxilData_0_0, 6896)`` is a *compressed
  byte count*, not a blob index; the index is the call's ordinal in the global
  Read() stream (see ``_assign_blob_indices``);
* ``D3D12_SUBOBJECT_TO_EXPORTS_ASSOCIATION`` points at ``&subobjects_0[7]`` by
  array index and may point forward, so associations need a second pass.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator, Optional

from .cppparse import iter_lines
from .model import DxilExport, HitGroup, ShaderStage, StateObject, StateObjectType

_RE_SO_FUNC = re.compile(r"^void\s+CreateStateObject_(\d+)\s*\(")
_RE_ANY_FUNC = re.compile(r"^void\s+\w+\s*\(")
_RE_READ = re.compile(r"g_resourceReader->Read\(\s*\w+\s*,\s*(\d+)\s*\)")
_RE_SUBOBJECT_ARRAY = re.compile(r"D3D12_STATE_SUBOBJECT\s+subobjects_(\d+)\s*\[(\d+)\]")
_RE_SUBOBJECT_ASSIGN = re.compile(
    r"subobjects_(\d+)\[(\d+)\]\s*=\s*\{\s*D3D12_STATE_SUBOBJECT_TYPE_(\w+)\s*,\s*&(\w+)"
)
_RE_DESC_ASSIGN = re.compile(
    r"stateObjectDescs\[(\d+)\]\s*=\s*\{\s*D3D12_STATE_OBJECT_TYPE_(\w+)\s*,\s*(\d+)"
)
_RE_TRACK_SINGLE = re.compile(r"CreateAndTrackStateObject\(\s*(\d+)\s*,\s*stateObjectDescs\s*\)")
_RE_TRACK_SEGMENT = re.compile(
    r"CreateAndTrackStateObject\(\s*(\d+)\s*,\s*&stateObjectDescs\[(\d+)\]"
)
_RE_ADD_TO = re.compile(r"AddToStateObject\(\s*&stateObjectDescs\[(\d+)\]")
_RE_CREATE_SO = re.compile(r"->CreateStateObject\(\s*&stateObjectDescs\[(\d+)\]")
_RE_SHADER_CONFIG = re.compile(
    r"D3D12_RAYTRACING_SHADER_CONFIG\s+(\w+)\s*=\s*\{\s*(\d+)\s*,\s*(\d+)\s*\}"
)
_RE_PIPELINE_CONFIG = re.compile(
    r"D3D12_RAYTRACING_PIPELINE_CONFIG\s+(\w+)\s*=\s*\{\s*(\d+)\s*\}"
)
_RE_SO_CONFIG = re.compile(r"D3D12_STATE_OBJECT_CONFIG\s+(\w+)\s*=\s*\{([^}]*)\}")
_RE_GLOBAL_RS = re.compile(
    r"D3D12_GLOBAL_ROOT_SIGNATURE\s+(\w+)\s*=\s*\{\s*GetRootSignature\((\d+)\)"
)
_RE_LOCAL_RS = re.compile(
    r"D3D12_LOCAL_ROOT_SIGNATURE\s+(\w+)\s*=\s*\{\s*GetRootSignature\((\d+)\)"
)
_RE_DXIL_LIB_DESC = re.compile(
    r"D3D12_DXIL_LIBRARY_DESC\s+(\w+)\s*=\s*\{\s*(\w+)\s*,\s*(\d+)\s*,\s*(\w+|nullptr)"
)
_RE_EXPORT_ARRAY = re.compile(r"D3D12_EXPORT_DESC\s+(\w+)\s*\[\s*\]\s*=\s*\{(.*)\}\s*;")
_RE_HIT_GROUP = re.compile(r"D3D12_HIT_GROUP_DESC\s+(\w+)\s*=\s*\{(.*)\}\s*;")
_RE_EXISTING_COLLECTION = re.compile(
    r"D3D12_EXISTING_COLLECTION_DESC\s+(\w+)\s*=\s*\{\s*GetStateObject\((\d+)\)"
)
_RE_LPCWSTR_ARRAY = re.compile(r"LPCWSTR\s+(\w+)\s*\[\s*\]\s*=\s*\{(.*)\}\s*;")
_RE_ASSOCIATION = re.compile(
    r"D3D12_SUBOBJECT_TO_EXPORTS_ASSOCIATION\s+(\w+)\s*=\s*\{\s*&subobjects_(\d+)\[(\d+)\]"
    r"\s*,\s*(\d+)\s*,\s*(\w+|nullptr)"
)

# Prefix -> stage, the weakest of the four inference sources. UE5 is consistent
# about these, but a prefix is a naming convention and nothing more, so anything
# derived here is tagged ``name_prefix`` and must be reported as such.
_STAGE_PREFIXES: tuple[tuple[str, ShaderStage], ...] = (
    ("RayGen", ShaderStage.RAYGEN),
    ("RGS", ShaderStage.RAYGEN),
    ("CHS", ShaderStage.CLOSESTHIT),
    ("ClosestHit", ShaderStage.CLOSESTHIT),
    ("AHS", ShaderStage.ANYHIT),
    ("AnyHit", ShaderStage.ANYHIT),
    ("Miss", ShaderStage.MISS),
    ("MS", ShaderStage.MISS),
    ("IS", ShaderStage.INTERSECTION),
    ("Intersection", ShaderStage.INTERSECTION),
    ("Callable", ShaderStage.CALLABLE),
)

_HIT_GROUP_TYPES = {
    "D3D12_HIT_GROUP_TYPE_TRIANGLES": "triangles",
    "D3D12_HIT_GROUP_TYPE_PROCEDURAL_PRIMITIVE": "procedural_primitive",
}


def parse_raw_strings(text: str) -> list[str]:
    """Every ``LR"delim(...)delim"`` literal in ``text``, in order.

    Written as a scanner rather than a regex because the obvious
    ``LR"\\((.*?)\\)"`` truncates any literal containing ``)"`` -- the bug already
    on record against ``_RE_PIX_BEGIN``. DXR export names are hash suffixes and
    safe, but ``original_name`` comes from the engine and is not under our
    control, and a silently truncated shader name would break every
    cross-reference that keys on it. Handles the general C++ raw-string form so
    it can be reused to fix the marker parser.
    """
    out: list[str] = []
    index = 0
    length = len(text)
    while True:
        start = text.find('R"', index)
        if start < 0:
            return out
        # Accept R"..." / LR"..." / u8R"..." etc: the prefix is [LuU8]* before R.
        open_paren = text.find("(", start + 2)
        if open_paren < 0:
            return out
        delim = text[start + 2 : open_paren]
        if not all(char.isalnum() or char in "_{}[]#<>%:;.?*+-/^&|~!=,'" for char in delim):
            index = start + 2
            continue
        terminator = ")" + delim + '"'
        end = text.find(terminator, open_paren + 1)
        if end < 0:
            return out
        out.append(text[open_paren + 1 : end])
        index = end + len(terminator)


def split_top_level(text: str) -> list[str]:
    """Split on commas that are not nested in braces, parens or raw strings."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "R" and text.startswith('R"', index):
            open_paren = text.find("(", index + 2)
            if open_paren >= 0:
                delim = text[index + 2 : open_paren]
                terminator = ")" + delim + '"'
                end = text.find(terminator, open_paren + 1)
                if end >= 0:
                    current.append(text[index : end + len(terminator)])
                    index = end + len(terminator)
                    continue
        if char in "{(":
            depth += 1
        elif char in "})":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    if current:
        parts.append("".join(current).strip())
    return [part for part in parts if part]


def _normalise_flags(text: str) -> list[str]:
    """``D3D12_STATE_OBJECT_FLAG_ALLOW_X`` -> ``allow_x``."""
    flags: list[str] = []
    for token in re.split(r"[|,]", text):
        token = token.strip()
        if not token or token in ("0", "D3D12_STATE_OBJECT_FLAG_NONE"):
            continue
        flags.append(token.replace("D3D12_STATE_OBJECT_FLAG_", "").lower())
    return flags


def stage_from_name(name: str) -> Optional[ShaderStage]:
    """Guess a stage from an export name prefix. Weakest inference source."""
    for prefix, stage in _STAGE_PREFIXES:
        if name.startswith(prefix + "_") or name == prefix:
            return stage
    lowered = name.lower()
    for prefix, stage in _STAGE_PREFIXES:
        if lowered.endswith(prefix.lower()):
            return stage
    return None


class _Subobject:
    """One ``subobjects_<segment>[<index>]`` slot, before associations resolve."""

    __slots__ = ("segment", "index", "type", "variable")

    def __init__(self, segment: int, index: int, type_name: str, variable: str) -> None:
        self.segment = segment
        self.index = index
        self.type = type_name
        self.variable = variable


class _FunctionBody:
    """Accumulated state of one CreateStateObject_* function while scanning."""

    def __init__(self, api_id: int, source_file: str, source_line: int) -> None:
        self.api_id = api_id
        self.source_file = source_file
        self.source_line = source_line
        self.subobjects: dict[tuple[int, int], _Subobject] = {}
        self.segment_types: dict[int, str] = {}
        self.segment_counts: dict[int, int] = {}
        self.segment_origin: dict[int, str] = {}
        self.shader_configs: dict[str, tuple[int, int]] = {}
        self.pipeline_configs: dict[str, int] = {}
        self.state_object_configs: dict[str, list[str]] = {}
        self.global_root_signatures: dict[str, int] = {}
        self.local_root_signatures: dict[str, int] = {}
        self.dxil_libs: dict[str, tuple[str, int, str]] = {}
        self.export_arrays: dict[str, list[tuple[str, str, str]]] = {}
        self.hit_groups: dict[str, HitGroup] = {}
        self.existing_collections: dict[str, int] = {}
        self.name_arrays: dict[str, list[str]] = {}
        self.associations: dict[str, tuple[int, int, list[str]]] = {}
        # DXIL Read() calls in this body, in order; index 0 pairs with the first
        # dxil library declared, which is how the compressed size is attached.
        self.reads: list[int] = []
        self.read_cursor = 0
        self.tracked_id: Optional[int] = None
        self.final_segment: Optional[int] = None


def _flush(body: _FunctionBody) -> StateObject:
    """Turn a scanned function body into one StateObject.

    Every ``stateObjectDescs[n]`` segment contributes; the last one is what the
    tracked object ends up being, but an AddToStateObject chain means the earlier
    segments' subobjects are still part of the final pipeline. Taking only the
    final segment would reduce RTPSO 3930 to seven subobjects and drop the 63
    collections that carry its shaders.
    """
    api_id = body.tracked_id if body.tracked_id is not None else body.api_id
    final_segment = body.final_segment
    if final_segment is None and body.segment_types:
        final_segment = max(body.segment_types)
    type_name = body.segment_types.get(final_segment or 0, "COLLECTION")
    state_object = StateObject(
        api_id=api_id,
        type=(
            StateObjectType.RAYTRACING_PIPELINE
            if type_name == "RAYTRACING_PIPELINE"
            else StateObjectType.COLLECTION
        ),
        desc_segment_count=len(body.segment_types) or 1,
        source_file=body.source_file,
        source_line=body.source_line,
    )

    # Resolve each association's target subobject into the export names it binds.
    # Two passes are unavoidable: an association may reference a slot declared
    # later in the body.
    local_rs_for_export: dict[str, int] = {}
    for segment, index, names in body.associations.values():
        target = body.subobjects.get((segment, index))
        if target is None:
            continue
        if target.type == "LOCAL_ROOT_SIGNATURE":
            rs_id = body.local_root_signatures.get(target.variable)
            if rs_id is None:
                continue
            for name in names:
                local_rs_for_export[name] = rs_id

    for key in sorted(body.subobjects):
        subobject = body.subobjects[key]
        kind = subobject.type
        variable = subobject.variable
        if kind == "RAYTRACING_SHADER_CONFIG":
            config = body.shader_configs.get(variable)
            if config:
                state_object.max_payload_size = config[0]
                state_object.max_attribute_size = config[1]
        elif kind == "RAYTRACING_PIPELINE_CONFIG":
            depth = body.pipeline_configs.get(variable)
            if depth is not None:
                state_object.max_recursion_depth = depth
        elif kind == "STATE_OBJECT_CONFIG":
            for flag in body.state_object_configs.get(variable, []):
                if flag not in state_object.flags:
                    state_object.flags.append(flag)
        elif kind == "GLOBAL_ROOT_SIGNATURE":
            rs_id = body.global_root_signatures.get(variable)
            if rs_id is not None:
                state_object.global_root_signature_id = rs_id
        elif kind == "LOCAL_ROOT_SIGNATURE":
            rs_id = body.local_root_signatures.get(variable)
            if rs_id is not None and rs_id not in state_object.local_root_signature_ids:
                state_object.local_root_signature_ids.append(rs_id)
        elif kind == "DXIL_LIBRARY":
            lib = body.dxil_libs.get(variable)
            if lib is None:
                continue
            _bytecode, _count, export_array = lib
            for name, original, flags in body.export_arrays.get(export_array, []):
                state_object.exports.append(
                    DxilExport(
                        name=name,
                        original_name=original,
                        flags=flags,
                        defining_state_object_id=api_id,
                    )
                )
        elif kind == "HIT_GROUP":
            group = body.hit_groups.get(variable)
            if group is not None:
                group.defining_state_object_id = api_id
                state_object.hit_groups.append(group)
        elif kind == "EXISTING_COLLECTION":
            collection_id = body.existing_collections.get(variable)
            if (
                collection_id is not None
                and collection_id not in state_object.existing_collection_ids
            ):
                state_object.existing_collection_ids.append(collection_id)

    for export in state_object.exports:
        export.local_root_signature_id = local_rs_for_export.get(export.name)
    for group in state_object.hit_groups:
        # A hit group's local root signature is declared against its member
        # exports, not against the group, so it has to be lifted from them.
        for member in group.member_exports:
            rs_id = local_rs_for_export.get(member)
            if rs_id is not None:
                group.local_root_signature_id = rs_id
                break

    _infer_stages(state_object)
    return state_object


def _infer_stages(state_object: StateObject) -> None:
    """Fill ``stage``/``stage_source`` on every export of one object.

    Hit-group membership is stated by the export and wins; a name prefix is a
    convention and is recorded as such. Nothing is ever left looking like a fact
    when it is a guess -- that distinction is the whole reason ``stage_source``
    exists.
    """
    by_name = {export.name: export for export in state_object.exports}
    for group in state_object.hit_groups:
        for member, stage in (
            (group.closest_hit, ShaderStage.CLOSESTHIT),
            (group.any_hit, ShaderStage.ANYHIT),
            (group.intersection, ShaderStage.INTERSECTION),
        ):
            export = by_name.get(member)
            if member and export is not None:
                export.stage = stage
                export.stage_source = "hit_group"
    for export in state_object.exports:
        if export.stage is not None:
            continue
        guess = stage_from_name(export.name) or stage_from_name(export.original_name)
        if guess is not None:
            export.stage = guess
            export.stage_source = "name_prefix"


def _assign_blob_indices(root: Path, objects: dict[int, StateObject]) -> None:
    """Attach the resources.bin blob ordinal to each state object's DXIL reads.

    The literal in ``Read(dxilData_0_0, 6896)`` is the *compressed size*, not an
    index -- resources.bin is one sequential stream with no index table, so the
    only address a blob has is its position in the global Read() order. This
    walks CreatePSOs.cpp counting every Read (the PSO ones first, then the state
    object ones) so the numbering matches what ``collect_resource_reads`` and
    ``Capture._load_blob`` already use.
    """
    path = root / "CreatePSOs.cpp"
    if not path.exists():
        return
    counter = 0
    current: Optional[int] = None
    for _lineno, line in iter_lines(path):
        match = _RE_SO_FUNC.match(line)
        if match:
            current = int(match.group(1))
            continue
        if _RE_ANY_FUNC.match(line):
            current = None
        match = _RE_READ.search(line)
        if not match:
            continue
        index = counter
        counter += 1
        if current is None:
            continue
        state_object = objects.get(current)
        if state_object is None:
            continue
        state_object.dxil_blob_indices.append(index)
        size = int(match.group(1))
        for export in state_object.exports:
            if export.dxil_blob_index is None:
                export.dxil_blob_index = index
                export.dxil_compressed_size = size


def _iter_function_bodies(path: Path) -> Iterator[_FunctionBody]:
    body: Optional[_FunctionBody] = None
    for lineno, line in iter_lines(path):
        match = _RE_SO_FUNC.match(line)
        if match:
            if body is not None:
                yield body
            body = _FunctionBody(int(match.group(1)), path.name, lineno)
            continue
        if body is None:
            continue
        if _RE_ANY_FUNC.match(line):
            # Any other top-level function closes the current one. Without this
            # the next CreatePipelineState_* body would leak into the last state
            # object, which is exactly the class of bug the PSO parser has.
            yield body
            body = None
            continue
        _scan_line(body, line)
    if body is not None:
        yield body


def _scan_line(body: _FunctionBody, line: str) -> None:
    match = _RE_SUBOBJECT_ARRAY.search(line)
    if match:
        body.segment_counts[int(match.group(1))] = int(match.group(2))
        return

    match = _RE_SUBOBJECT_ASSIGN.search(line)
    if match:
        segment = int(match.group(1))
        index = int(match.group(2))
        body.subobjects[(segment, index)] = _Subobject(
            segment, index, match.group(3), match.group(4)
        )
        return

    match = _RE_DESC_ASSIGN.search(line)
    if match:
        body.segment_types[int(match.group(1))] = match.group(2)
        return

    match = _RE_TRACK_SEGMENT.search(line)
    if match:
        body.tracked_id = int(match.group(1))
        body.final_segment = int(match.group(2))
        return

    match = _RE_TRACK_SINGLE.search(line)
    if match:
        body.tracked_id = int(match.group(1))
        body.final_segment = 0
        return

    match = _RE_ADD_TO.search(line)
    if match:
        body.segment_origin[int(match.group(1))] = "add_to_state_object"
        return

    match = _RE_CREATE_SO.search(line)
    if match:
        body.segment_origin[int(match.group(1))] = "create_state_object"
        return

    match = _RE_SHADER_CONFIG.search(line)
    if match:
        body.shader_configs[match.group(1)] = (int(match.group(2)), int(match.group(3)))
        return

    match = _RE_PIPELINE_CONFIG.search(line)
    if match:
        body.pipeline_configs[match.group(1)] = int(match.group(2))
        return

    match = _RE_SO_CONFIG.search(line)
    if match:
        body.state_object_configs[match.group(1)] = _normalise_flags(match.group(2))
        return

    match = _RE_GLOBAL_RS.search(line)
    if match:
        body.global_root_signatures[match.group(1)] = int(match.group(2))
        return

    match = _RE_LOCAL_RS.search(line)
    if match:
        body.local_root_signatures[match.group(1)] = int(match.group(2))
        return

    match = _RE_DXIL_LIB_DESC.search(line)
    if match:
        body.dxil_libs[match.group(1)] = (
            match.group(2),
            int(match.group(3)),
            match.group(4),
        )
        return

    match = _RE_EXPORT_ARRAY.search(line)
    if match:
        body.export_arrays[match.group(1)] = _parse_export_descs(match.group(2))
        return

    match = _RE_HIT_GROUP.search(line)
    if match:
        group = _parse_hit_group(match.group(2))
        if group is not None:
            body.hit_groups[match.group(1)] = group
        return

    match = _RE_EXISTING_COLLECTION.search(line)
    if match:
        body.existing_collections[match.group(1)] = int(match.group(2))
        return

    match = _RE_LPCWSTR_ARRAY.search(line)
    if match:
        body.name_arrays[match.group(1)] = parse_raw_strings(match.group(2))
        return

    match = _RE_ASSOCIATION.search(line)
    if match:
        array_name = match.group(5)
        names = body.name_arrays.get(array_name, [])
        body.associations[match.group(1)] = (
            int(match.group(2)),
            int(match.group(3)),
            names,
        )
        return

    if _RE_READ.search(line):
        body.reads.append(0)


def _parse_export_descs(text: str) -> list[tuple[str, str, str]]:
    """Parse ``{ LR"(a)", LR"(b)", FLAG }, { ... }`` into triples."""
    out: list[tuple[str, str, str]] = []
    for entry in re.finditer(r"\{([^{}]*)\}", text):
        fields = split_top_level(entry.group(1))
        if not fields:
            continue
        names = parse_raw_strings(fields[0])
        name = names[0] if names else ""
        if not name:
            continue
        original = ""
        if len(fields) > 1 and fields[1].strip() != "nullptr":
            originals = parse_raw_strings(fields[1])
            original = originals[0] if originals else ""
        flags = fields[2].strip() if len(fields) > 2 else ""
        if flags == "D3D12_EXPORT_FLAG_NONE":
            flags = ""
        out.append((name, original, flags))
    return out


def _parse_hit_group(text: str) -> Optional[HitGroup]:
    """Parse ``{ name, TYPE, anyHit, closestHit, intersection }``.

    Field order is fixed by D3D12_HIT_GROUP_DESC and is *not* the order a reader
    expects: AnyHit precedes ClosestHit. Swapping them would silently report the
    wrong shader for every hit group in the frame.
    """
    fields = split_top_level(text)
    if not fields:
        return None

    def literal(index: int) -> str:
        if index >= len(fields):
            return ""
        raw = fields[index].strip()
        if raw in ("nullptr", "NULL", "0"):
            return ""
        names = parse_raw_strings(raw)
        return names[0] if names else ""

    name = literal(0)
    if not name:
        return None
    type_token = fields[1].strip() if len(fields) > 1 else ""
    return HitGroup(
        name=name,
        type=_HIT_GROUP_TYPES.get(type_token, type_token.lower() or "triangles"),
        any_hit=literal(2),
        closest_hit=literal(3),
        intersection=literal(4),
    )


def parse_state_objects(root: Path) -> dict[int, StateObject]:
    """Every raytracing state object declared in CreatePSOs.cpp, keyed by id.

    Returns an empty dict for a capture with no raytracing, which is a fact and
    not a failure; callers should not treat it as a parse error.
    """
    path = root / "CreatePSOs.cpp"
    objects: dict[int, StateObject] = {}
    if not path.exists():
        return objects
    for body in _iter_function_bodies(path):
        state_object = _flush(body)
        # AddToStateObject grows one object across segments, so the tracked id is
        # the only identity that matters; the function name can differ from it.
        objects[state_object.api_id] = state_object
    _assign_blob_indices(root, objects)
    return objects
