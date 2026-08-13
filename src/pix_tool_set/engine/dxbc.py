"""DXBC/DXIL container parsing, reflection and disassembly.

A shader blob emitted by PIX is a standard DXBC container::

    'DXBC' | md5[16] | version u32 | totalSize u32 | chunkCount u32
    chunkOffsets u32[chunkCount]
    chunks: tag[4] | size u32 | payload

Chunks we care about:
    DXIL / SHEX / SHDR  the bytecode itself
    ISG1 / OSG1         input / output signature (DXIL era, 32-byte stride)
    RDEF                resource definitions (SM5 style)
    PSV0                pipeline state validation info
    ILDN                debug (PDB) name
    HASH                shader hash
    SPDB / ILDB         embedded debug info, may carry HLSL source

Disassembly uses the ``dxcompiler.dll`` shipped inside the PIX installation,
driven through raw COM vtable calls, so there is no third-party dependency.
"""

from __future__ import annotations

import ctypes
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import PixToolError

DXBC_MAGIC = b"DXBC"
BYTECODE_TAGS = ("DXIL", "SHEX", "SHDR")
SOURCE_TAGS = ("SPDB", "ILDB")


# --------------------------------------------------------------------------
# container
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DxbcChunk:
    tag: str
    offset: int
    size: int


@dataclass(slots=True)
class DxbcContainer:
    data: bytes
    total_size: int
    hash_md5: str
    chunks: list[DxbcChunk] = field(default_factory=list)

    @classmethod
    def parse(cls, blob: bytes) -> "DxbcContainer":
        if len(blob) < 32 or blob[:4] != DXBC_MAGIC:
            raise ValueError("not a DXBC container")
        md5 = blob[4:20].hex()
        total = struct.unpack_from("<I", blob, 24)[0]
        count = struct.unpack_from("<I", blob, 28)[0]
        if count > 64:
            raise ValueError(f"implausible chunk count {count}")
        offsets = struct.unpack_from(f"<{count}I", blob, 32)
        chunks: list[DxbcChunk] = []
        for offset in offsets:
            if offset + 8 > len(blob):
                continue
            tag = bytes(blob[offset : offset + 4]).decode("ascii", "replace")
            size = struct.unpack_from("<I", blob, offset + 4)[0]
            chunks.append(DxbcChunk(tag=tag, offset=offset + 8, size=size))
        return cls(
            data=blob[:total] if 0 < total <= len(blob) else blob,
            total_size=total,
            hash_md5=md5,
            chunks=chunks,
        )

    def chunk(self, tag: str) -> bytes | None:
        for entry in self.chunks:
            if entry.tag == tag:
                return self.data[entry.offset : entry.offset + entry.size]
        return None

    @property
    def tags(self) -> list[str]:
        return [c.tag for c in self.chunks]

    @property
    def is_dxil(self) -> bool:
        return any(c.tag == "DXIL" for c in self.chunks)

    @property
    def debug_name(self) -> str | None:
        raw = self.chunk("ILDN")
        if not raw or len(raw) < 4:
            return None
        name = raw[4:].split(b"\x00")[0]
        return name.decode("utf-8", "replace") or None

    @property
    def shader_hash(self) -> str | None:
        raw = self.chunk("HASH")
        if not raw or len(raw) < 20:
            return None
        return raw[4:20].hex()

    def has_embedded_source(self) -> bool:
        return any(c.tag in SOURCE_TAGS for c in self.chunks)


def split_packed_shaders(blob: bytes) -> list[bytes]:
    """Split a buffer holding several concatenated DXBC containers.

    PIX packs every stage of one PSO into a single resources.bin blob, so
    ``VS|PS`` arrive glued together.  DXBC carries its own ``totalSize`` which
    lets us walk them safely.
    """
    out: list[bytes] = []
    position = 0
    size = len(blob)
    while position + 32 <= size:
        if blob[position : position + 4] != DXBC_MAGIC:
            nxt = blob.find(DXBC_MAGIC, position + 1)
            if nxt < 0:
                break
            position = nxt
            continue
        total = struct.unpack_from("<I", blob, position + 24)[0]
        if total <= 0 or position + total > size:
            out.append(blob[position:])
            break
        out.append(blob[position : position + total])
        position += total
    return out


# --------------------------------------------------------------------------
# signatures
# --------------------------------------------------------------------------
_COMPONENT_TYPE = {
    0: "unknown",
    1: "uint",
    2: "int",
    3: "float",
    4: "uint16",
    5: "int16",
    6: "float16",
    7: "uint64",
    8: "int64",
    9: "float64",
}

_SYSTEM_VALUE = {
    0: "NONE",
    1: "POS",
    2: "CLIPDST",
    3: "CULLDST",
    4: "RTINDEX",
    5: "VPINDEX",
    6: "VERTID",
    7: "PRIMID",
    8: "INSTID",
    9: "FFACE",
    10: "SAMPLE",
    11: "QUADEDGE",
    12: "QUADINT",
    13: "TRIEDGE",
    14: "TRIINT",
    15: "LINEDET",
    16: "LINEDEN",
    64: "TARGET",
    65: "DEPTH",
    66: "COVERAGE",
    67: "DEPTHGE",
    68: "DEPTHLE",
}


@dataclass(frozen=True, slots=True)
class SignatureElement:
    semantic_name: str
    semantic_index: int
    register: int
    mask: int
    component_type: str
    system_value: str

    @property
    def mask_str(self) -> str:
        return "".join(c for c, bit in zip("xyzw", (1, 2, 4, 8)) if self.mask & bit)

    def to_dict(self) -> dict[str, Any]:
        return {
            "semantic": self.semantic_name,
            "semantic_index": self.semantic_index,
            "register": self.register,
            "mask": self.mask_str,
            "component_type": self.component_type,
            "system_value": self.system_value,
        }


def parse_signature(chunk: bytes | None) -> list[SignatureElement]:
    if not chunk or len(chunk) < 8:
        return []
    count, _offset = struct.unpack_from("<II", chunk, 0)
    if count > 64:
        return []
    elements: list[SignatureElement] = []
    stride = 32
    base = 8
    for index in range(count):
        pos = base + index * stride
        if pos + stride > len(chunk):
            break
        (
            _stream,
            name_offset,
            semantic_index,
            system_value,
            component_type,
            register,
            mask,
            _rw_mask,
            _pad,
        ) = struct.unpack_from("<IIIIIIBBH", chunk, pos)
        name = ""
        if name_offset < len(chunk):
            name = chunk[name_offset:].split(b"\x00")[0].decode("utf-8", "replace")
        elements.append(
            SignatureElement(
                semantic_name=name,
                semantic_index=semantic_index,
                register=register,
                mask=mask,
                component_type=_COMPONENT_TYPE.get(component_type, "?"),
                system_value=_SYSTEM_VALUE.get(system_value, str(system_value)),
            )
        )
    return elements


# --------------------------------------------------------------------------
# disassembly through the PIX-bundled dxcompiler.dll
# --------------------------------------------------------------------------
class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def parse(cls, text: str) -> "_GUID":
        text = text.strip("{}")
        parts = text.split("-")
        tail = bytes.fromhex(parts[3] + parts[4])
        return cls(
            int(parts[0], 16),
            int(parts[1], 16),
            int(parts[2], 16),
            (ctypes.c_ubyte * 8)(*tail),
        )


class _DxcBuffer(ctypes.Structure):
    _fields_ = [
        ("Ptr", ctypes.c_void_p),
        ("Size", ctypes.c_size_t),
        ("Encoding", ctypes.c_uint32),
    ]


_CLSID_DxcCompiler = _GUID.parse("73e22d93-e6ce-47f3-b5bf-f0664f39c1b0")
_IID_IDxcCompiler3 = _GUID.parse("228B4687-5A6A-4730-900C-9702B2203F54")
_IID_IDxcResult = _GUID.parse("58346CDA-DDE7-4497-9461-6F87AF5E0659")


def _vcall(pointer, index, restype, *argtypes):
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p))[0]
    slot = ctypes.cast(vtable, ctypes.POINTER(ctypes.c_void_p))[index]
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return lambda *a: proto(slot)(pointer, *a)


class ShaderDisassembler:
    """Disassembles DXBC/DXIL blobs with PIX's own dxcompiler.dll."""

    def __init__(self, dxc_dir: str | Path | None = None) -> None:
        self._dxc_dir = Path(dxc_dir) if dxc_dir else None
        self._dll = None
        self._compiler = None
        self._reason: str | None = None

    def _ensure(self) -> None:
        if self._compiler is not None or self._reason is not None:
            return
        candidates: list[Path] = []
        if self._dxc_dir:
            candidates.append(self._dxc_dir / "dxcompiler.dll")
        from ..pixtool import find_pix_install

        install = find_pix_install()
        if install is not None:
            candidates.append(install / "dxcompiler.dll")

        dll_path = next((c for c in candidates if c.exists()), None)
        if dll_path is None:
            self._reason = "dxcompiler.dll not found next to pixtool.exe"
            return
        try:
            try:
                ctypes.WinDLL(str(dll_path.parent / "dxil.dll"))
            except OSError:
                pass
            dll = ctypes.WinDLL(str(dll_path))
        except OSError as exc:
            self._reason = f"cannot load {dll_path}: {exc}"
            return

        dll.DxcCreateInstance.argtypes = [
            ctypes.POINTER(_GUID),
            ctypes.POINTER(_GUID),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        dll.DxcCreateInstance.restype = ctypes.c_int32
        compiler = ctypes.c_void_p()
        hr = dll.DxcCreateInstance(
            ctypes.byref(_CLSID_DxcCompiler),
            ctypes.byref(_IID_IDxcCompiler3),
            ctypes.byref(compiler),
        )
        if hr != 0 or not compiler:
            self._reason = f"DxcCreateInstance failed (hr=0x{hr & 0xFFFFFFFF:08x})"
            return
        self._dll = dll
        self._compiler = compiler

    @property
    def available(self) -> bool:
        self._ensure()
        return self._compiler is not None

    @property
    def unavailable_reason(self) -> str | None:
        self._ensure()
        return self._reason

    def disassemble(self, blob: bytes) -> str:
        self._ensure()
        if self._compiler is None:
            raise PixToolError(
                code="disassembly_unavailable",
                message=self._reason or "dxcompiler.dll is unavailable",
                stage="shader",
                suggestion="Install Microsoft PIX so dxcompiler.dll ships alongside pixtool.exe.",
            )
        source = ctypes.create_string_buffer(blob, len(blob))
        buffer = _DxcBuffer(ctypes.addressof(source), len(blob), 0)
        result = ctypes.c_void_p()
        hr = _vcall(
            self._compiler,
            4,
            ctypes.c_int32,
            ctypes.POINTER(_DxcBuffer),
            ctypes.POINTER(_GUID),
            ctypes.POINTER(ctypes.c_void_p),
        )(ctypes.byref(buffer), ctypes.byref(_IID_IDxcResult), ctypes.byref(result))
        if hr != 0 or not result:
            raise PixToolError(
                code="disassembly_failed",
                message=f"IDxcCompiler3::Disassemble failed (hr=0x{hr & 0xFFFFFFFF:08x})",
                stage="shader",
            )
        try:
            out = ctypes.c_void_p()
            hr2 = _vcall(result, 4, ctypes.c_int32, ctypes.POINTER(ctypes.c_void_p))(
                ctypes.byref(out)
            )
            if hr2 != 0 or not out:
                raise PixToolError(
                    code="disassembly_failed",
                    message=f"IDxcResult::GetResult failed (hr=0x{hr2 & 0xFFFFFFFF:08x})",
                    stage="shader",
                )
            try:
                pointer = _vcall(out, 3, ctypes.c_void_p)()
                size = _vcall(out, 4, ctypes.c_size_t)()
                return ctypes.string_at(pointer, size).decode("utf-8", "replace")
            finally:
                _vcall(out, 2, ctypes.c_uint32)()
        finally:
            _vcall(result, 2, ctypes.c_uint32)()


# --------------------------------------------------------------------------
# reflection helpers driven off the disassembly text
# --------------------------------------------------------------------------
_BIND_ID = re.compile(r"(?:CB|T|U|S)\d+")

# The Type column of dxc's binding table draws from a small fixed vocabulary. Listed
# longest-first so prefix matching cannot stop early on a shorter word that happens to
# be a prefix of a longer one.
_BIND_TYPE_WORDS = (
    "SamplerComparison",
    "structbuf",
    "byteaddr",
    "cbuffer",
    "tbuffer",
    "texture",
    "sampler",
    "UAV",
    "ROV",
)


def _split_glued_type(token: str) -> list[str]:
    """Separate a Type cell from a Format cell that overflowed into it.

    dxc renders this table with fixed column widths and pads with a *minimum* of one
    space, so a value wider than its column swallows the separator and arrives glued to
    its neighbour. ``unorm_f32`` is 9 characters in a 7-character Format column, which
    is why ``RWDownsampledWorldNormal2x2`` reaches us as::

        ; RWDownsampledWorldNormal2x2           UAVunorm_f32          2d      U7 ...

    Splitting on whitespace alone then reports ``type="UAVunorm_f32"`` and shifts every
    later cell one column left, so the register was described as ``format="2d"`` with an
    empty dimension.

    Slicing by the widths in the table's rule line is not an alternative: the Name column
    overflows too (``ForwardLightStruct_NumCulledLightsGrid`` is 38 characters in a
    30-character column), which shifts every following column on that row. Anchoring on
    the unambiguous ID cell and ungluing by vocabulary is stable against both.
    """
    for word in _BIND_TYPE_WORDS:
        if token == word:
            return [token]
        if token.startswith(word):
            return [word, token[len(word) :]]
    return [token]


def parse_resource_bindings(disassembly: str) -> list[dict[str, Any]]:
    """Read the ``; Resource Bindings:`` table emitted by dxc."""
    if not disassembly:
        return []
    rows: list[dict[str, Any]] = []
    in_table = False
    seen_rule = False
    for line in disassembly.splitlines():
        if "Resource Bindings:" in line:
            in_table = True
            continue
        if not in_table:
            continue
        if not line.startswith(";"):
            break
        body = line[1:].strip()
        if not body:
            if rows:
                break
            continue
        if set(body) <= set("- "):
            seen_rule = True
            continue
        if body.startswith("Name"):
            continue
        if not seen_rule:
            continue
        parts = body.split()
        if len(parts) < 5:
            break
        bind_id = next((p for p in parts if _BIND_ID.fullmatch(p)), None)
        if bind_id is None:
            break
        position = parts.index(bind_id)

        # Cells left of the ID are Name, Type, Format and Dim. ID, HLSL Bind and Count
        # are narrow enough never to overflow, so only this side can be glued. A head
        # shorter than four cells is the signal that it happened; attempting the split
        # unconditionally could damage a well-formed row whose name ends in "UAV".
        head = parts[:position]
        if 2 <= len(head) < 4:
            head = [head[0]] + _split_glued_type(head[1]) + head[2:]

        rows.append(
            {
                "name": head[0],
                "type": head[1] if len(head) > 1 else "",
                "format": head[2] if len(head) > 2 else "",
                "dimension": head[3] if len(head) > 3 else "",
                "id": bind_id,
                "hlsl_bind": parts[position + 1] if len(parts) > position + 1 else "",
                "count": parts[position + 2] if len(parts) > position + 2 else "",
            }
        )
    return rows


def parse_shader_metadata(disassembly: str) -> dict[str, Any]:
    """Entry point, thread group size, target triple, hash."""
    meta: dict[str, Any] = {}
    if not disassembly:
        return meta
    match = re.search(r"EntryFunctionName:\s*(\S+)", disassembly)
    if match:
        meta["entry_point"] = match.group(1)
    match = re.search(r"NumThreads=\((\d+)\s*,\s*(\d+)\s*,\s*(\d+)\)", disassembly)
    if match:
        meta["num_threads"] = [int(g) for g in match.groups()]
    match = re.search(r"target\s+triple\s*=\s*\"([^\"]+)\"", disassembly)
    if match:
        meta["target_triple"] = match.group(1)
    match = re.search(r";\s*shader hash:\s*([0-9a-f]+)", disassembly)
    if match:
        meta["shader_hash"] = match.group(1)
    for key, pattern in (
        ("uses_view_id", r"UsesViewID:\s*(\w+)"),
        ("output_position_present", r"OutputPositionPresent=(\d+)"),
        ("min_wave_lane_count", r"MinimumExpectedWaveLaneCount:\s*(\d+)"),
        ("max_wave_lane_count", r"MaximumExpectedWaveLaneCount:\s*(\d+)"),
    ):
        match = re.search(pattern, disassembly)
        if match:
            raw = match.group(1)
            meta[key] = raw if not raw.isdigit() else int(raw)
    return meta


def parse_export_names(disassembly: str) -> list[str]:
    """The entry-point symbols a DXIL library export set contains, in order.

    A raytracing DXIL library (``lib_6_*``) exports one entry point per HLSL
    function carrying a ``[shader(...)]`` attribute.  DXC prints each as a
    ``define`` line whose ``@`` symbol is the *MSVC-mangled* entry point (e.g.
    ``@"\\01?LumenHardwareRayTracingMaterialCHS@@YAX..."``).  The mangled name is
    deterministic — the same function signature always decorates identically — so
    comparing the symbol sets of the captured and recompiled libraries is exactly
    the check that catches a rename (which changes the decoration) or a dropped
    entry point (which removes a symbol).

    The human-readable DXR export name (``CHS_b5acc26ab7153489``) is *not* in the
    disassembly at all: it is a PIX-side ``D3D12_EXPORT_DESC`` rename that lives in
    ``CreatePSOs.cpp``, keyed to the entry point via ``original_name``.  That is
    why this returns the entry-point symbols and the export-name invariant is
    enforced against ``DxilExport.original_name`` on the capture side (see
    ``_dxr_edit_apply``), not against the disassembly text.

    ``parse_shader_metadata`` only reads the single ``EntryFunctionName``, which is
    wrong for a library container holding several shaders.
    """
    if not disassembly:
        return []
    names: list[str] = []
    for line in disassembly.splitlines():
        stripped = line.strip()
        if not stripped.startswith("define "):
            continue
        # The symbol sits between '@' and '('; it may be quoted (a decorated C++
        # name) or a bare identifier. Skip intrinsics (dx.*), which are not exports.
        at = stripped.find("@")
        if at < 0:
            continue
        rest = stripped[at + 1 :]
        # Strip a leading quote that wraps a decorated symbol.
        if rest.startswith('"'):
            rest = rest.lstrip('"').lstrip("\\")
            # The quote ends right before the '(' after the symbol.
            name = rest.split('"', 1)[0]
        else:
            name = rest.split("(", 1)[0].strip()
        # Reject empty / intrinsic symbols and the call to the shader's own
        # attributes block, keeping only real entry points.
        if not name or name.startswith("dx.") or name.startswith("llvm."):
            continue
        if name not in names:
            names.append(name)
    return names


def parse_constant_buffers(disassembly: str) -> list[dict[str, Any]]:
    """Read the ``; Buffer Definitions:`` block (cbuffer layouts).

    A field line carries its byte offset as a trailing comment on the *same* line::

        ;   uint MaxNumInstances;   ; Offset:  128

    Offsets are not derivable from field order because HLSL packing inserts
    padding (this shader jumps 24 -> 128), so they must be read, not computed.

    DXC wraps a cbuffer's members in a struct and closes it with a line that
    carries the total size::

        ;   } _RootShaderParameters;   ; Offset:    0 Size:   244

    That line is the only place the size appears, and it must not be mistaken for
    a member. When the last member ends before that size, PIX shows the remainder
    as a trailing ``pad`` entry, so it is reproduced here to match.
    """
    if not disassembly:
        return []
    buffers: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    in_block = False

    def close_buffer(entry: dict[str, Any]) -> None:
        _add_trailing_pad(entry)
        buffers.append(entry)

    for line in disassembly.splitlines():
        if "Buffer Definitions:" in line:
            in_block = True
            continue
        if not in_block:
            continue
        if not line.startswith(";"):
            break
        body = line[1:].strip()
        if body.startswith("cbuffer "):
            if current:
                close_buffer(current)
            current = {"name": body[len("cbuffer ") :].strip(), "fields": []}
            continue
        if current is None:
            continue

        # Closing line of the wrapper struct: "} Name;  ; Offset: 0 Size: 244"
        if body.startswith("}"):
            size_match = re.search(r"Size:\s*(\d+)", body)
            if size_match:
                current["size"] = int(size_match.group(1))
                continue
            close_buffer(current)
            current = None
            continue

        offset_match = re.search(r";\s*Offset:\s*(\d+)", body)
        declaration = body.split(";")[0].strip() + ";"
        match = re.match(
            r"([A-Za-z_][\w:<>, ]*?)\s+([A-Za-z_]\w*)(\[[^\]]*\])?\s*;", declaration
        )
        if match:
            type_name = match.group(1).strip()
            # "struct Name" opens the wrapper; it is not a member.
            if type_name == "struct" or type_name.endswith(" struct"):
                continue
            field: dict[str, Any] = {
                "type": type_name,
                "name": match.group(2),
                "array": match.group(3) or "",
            }
            if offset_match:
                field["offset"] = int(offset_match.group(1))
            current["fields"].append(field)
        elif offset_match and current["fields"]:
            # Offset reported on its own line (older DXC layouts).
            current["fields"][-1]["offset"] = int(offset_match.group(1))
    if current:
        close_buffer(current)
    return buffers


_SCALAR_WIDTH = {
    "bool": 4,
    "int": 4,
    "uint": 4,
    "dword": 4,
    "float": 4,
    "half": 2,
    "double": 8,
}


def _field_size(field: dict[str, Any]) -> int | None:
    """Byte size of a plain scalar/vector/matrix member, else None."""
    name = (field.get("type") or "").strip().lower()
    for modifier in ("row_major ", "column_major ", "const ", "unorm ", "snorm "):
        if name.startswith(modifier):
            name = name[len(modifier) :]
    for scalar, width in _SCALAR_WIDTH.items():
        if not name.startswith(scalar):
            continue
        suffix = name[len(scalar) :]
        if not suffix:
            count = 1
        elif "x" in suffix:
            left, _, right = suffix.partition("x")
            if not (left.isdigit() and right.isdigit()):
                return None
            count = int(left) * int(right)
        elif suffix.isdigit():
            count = int(suffix)
        else:
            return None
        return count * width
    return None


def _add_trailing_pad(entry: dict[str, Any]) -> None:
    """Append the trailing pad member PIX displays, when one exists.

    Two cases produce padding at the end of a cbuffer:
      * members stop short of the declared struct size
      * the struct size itself is not a multiple of 16, since a cbuffer is
        allocated in 16-byte registers

    PIX shows either as a final ``pad`` row with no value, so both are covered.
    """
    size = entry.get("size")
    fields = entry.get("fields") or []
    if not size or not fields:
        return
    last = fields[-1]
    offset = last.get("offset")
    width = _field_size(last)
    if offset is None or width is None:
        return
    members_end = offset + width
    # A cbuffer occupies whole 16-byte registers.
    allocated = ((size + 15) // 16) * 16
    pad_start = max(members_end, min(size, allocated))
    if pad_start >= allocated:
        return
    fields.append(
        {
            "type": "pad",
            "name": "pad",
            "array": "",
            "offset": pad_start,
            "size": allocated - pad_start,
            "is_padding": True,
        }
    )


_HLSL_HINT = re.compile(
    rb"(#include|cbuffer\s|struct\s+\w+|float[234]?\s+\w+\s*\(|"
    rb"SV_Position|Texture2D|SamplerState|RWTexture|numthreads)"
)


def scrape_embedded_hlsl(raw: bytes) -> str:
    """Best-effort recovery of readable HLSL from an SPDB/ILDB chunk."""
    if not raw or not _HLSL_HINT.search(raw):
        return ""
    chunks: list[str] = []
    current = bytearray()
    for byte in raw:
        if 9 <= byte <= 13 or 32 <= byte < 127:
            current.append(byte)
        else:
            if len(current) > 40:
                chunks.append(current.decode("ascii", "replace"))
            current = bytearray()
    if len(current) > 40:
        chunks.append(current.decode("ascii", "replace"))
    text = "\n".join(chunks)
    return text if _HLSL_HINT.search(text.encode("ascii", "replace")) else ""
