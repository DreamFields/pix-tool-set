"""Recover original HLSL from a DXC shader PDB via IDxcPdbUtils.

Approach chosen after comparing two routes:

  1. Hand-parse the PDB: the MSF container yields a DXBC stream whose SRCI chunk
     holds a zlib-compressed copy of the preprocessed HLSL. This works and is
     implemented in `msf.py`, but relies on the chunk layout of one DXC build.
  2. Ask DXC itself through `IDxcPdbUtils` (CLSID 54621dfb-...). This is the
     documented path, returns the per-file split for free, and also exposes the
     compile arguments, defines, entry point and target profile.

Route 2 is used as primary and route 1 as fallback, so a PDB that a newer or
older dxcompiler.dll refuses to load can still be read.

No compilation step and no third-party package: `dxcompiler.dll` is called
through raw COM vtables with ctypes, the same technique already used for
disassembly.
"""

from __future__ import annotations

import ctypes
import re
import struct
import zlib
from ctypes import POINTER, byref, c_void_p, c_wchar_p
from ctypes.wintypes import BOOL, DWORD, LPVOID, UINT
from pathlib import Path
from typing import Any, Optional

_LINE_DIRECTIVE = re.compile(r'^#line\s+(\d+)\s+"([^"]*)"\s*$', re.MULTILINE)
_SENTINEL = "__UE_FILENAME_SENTINEL"

# {54621dfb-f2ce-457e-ae8c-ec355faeec7c}
_CLSID_DxcPdbUtils = (
    b"\xfb\x1db\x54"          # 0x54621dfb LE
    b"\xce\xf2"                # 0xf2ce LE
    b"\x7eE"                   # 0x457e LE
    b"\xae\x8c\xec\x35\x5f\xae\xec\x7c"
)
# IDxcPdbUtils {E6C9647E-9D6A-4C3B-B94C-524B5A6C343D}
_IID_IDxcPdbUtils = (
    b"\x7ed\xc9\xe6"
    b"j\x9d"
    b";L"
    b"\xb9L\x52\x4b\x5a\x6c\x34\x3d"
)
# {6245D6AF-66E0-48FD-80B4-4D271796748C}
_CLSID_DxcUtils = (
    b"\xaf\xd6E\x62"
    b"\xe0f"
    b"\xfdH"
    b"\x80\xb4M'\x17\x96t\x8c"
)
# IDxcUtils {4605C4CB-2019-492A-ADA4-65F20BB7D67F}
_IID_IDxcUtils = (
    b"\xcb\xc4\x05F"
    b"\x19 "
    b"*I"
    b"\xad\xa4e\xf2\x0b\xb7\xd6\x7f"
)

# vtable slots (IUnknown occupies 0-2)
_PDB_LOAD = 3
_PDB_GET_SOURCE_COUNT = 4
_PDB_GET_SOURCE = 5
_PDB_GET_SOURCE_NAME = 6
_PDB_GET_FLAG_COUNT = 7
_PDB_GET_FLAG = 8
_PDB_GET_ARG_COUNT = 9
_PDB_GET_ARG = 10

# IDxcBlob
_BLOB_GET_PTR = 3
_BLOB_GET_SIZE = 4
# IDxcUtils::CreateBlob is slot 5 (after CreateBlobFromBlob at 3, CreateBlobFromPinned at 4)
_UTILS_CREATE_BLOB_FROM_PINNED = 4


class _Vtbl:
    """Call a COM method by vtable slot without a generated interface."""

    def __init__(self, pointer: c_void_p) -> None:
        self.ptr = pointer
        self._table = ctypes.cast(
            ctypes.cast(pointer, POINTER(c_void_p))[0], POINTER(c_void_p)
        )

    def call(self, slot: int, restype, *argtypes_and_args):
        argtypes = argtypes_and_args[0]
        args = argtypes_and_args[1:]
        proto = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
        fn = proto(self._table[slot])
        return fn(self.ptr, *args)

    def release(self) -> None:
        if self.ptr:
            proto = ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)
            proto(self._table[2])(self.ptr)
            self.ptr = None


def _load_dxc() -> Optional[ctypes.CDLL]:
    """Find and load dxcompiler.dll, preferring the copy PIX ships."""
    candidates: list[Path] = []
    program_files = Path("C:/Program Files")
    pix_root = program_files / "Microsoft PIX"
    if pix_root.exists():
        for version in sorted(pix_root.iterdir(), reverse=True):
            dll = version / "dxcompiler.dll"
            if dll.exists():
                candidates.append(dll)
    kits = program_files / "Windows Kits" / "10" / "bin"
    if kits.exists():
        for version in sorted(kits.iterdir(), reverse=True):
            dll = version / "x64" / "dxcompiler.dll"
            if dll.exists():
                candidates.append(dll)
    candidates.append(Path("dxcompiler.dll"))
    for candidate in candidates:
        try:
            return ctypes.WinDLL(str(candidate))
        except OSError:
            continue
    return None


def _bstr_to_str(bstr: c_void_p) -> str:
    if not bstr:
        return ""
    text = ctypes.cast(bstr, c_wchar_p).value or ""
    ctypes.windll.oleaut32.SysFreeString(bstr)
    return text


def _extract_via_dxc(pdb_bytes: bytes) -> Optional[dict[str, Any]]:
    """Use IDxcPdbUtils to enumerate the PDB's source files."""
    dll = _load_dxc()
    if dll is None:
        return None
    try:
        create = dll.DxcCreateInstance
    except AttributeError:
        return None
    create.restype = ctypes.c_long
    create.argtypes = [c_void_p, c_void_p, POINTER(c_void_p)]

    utils_ptr = c_void_p()
    if create(
        ctypes.cast(ctypes.c_char_p(_CLSID_DxcUtils), c_void_p),
        ctypes.cast(ctypes.c_char_p(_IID_IDxcUtils), c_void_p),
        byref(utils_ptr),
    ) < 0:
        return None
    utils = _Vtbl(utils_ptr)

    buffer = ctypes.create_string_buffer(pdb_bytes, len(pdb_bytes))
    blob_ptr = c_void_p()
    hr = utils.call(
        _UTILS_CREATE_BLOB_FROM_PINNED,
        ctypes.c_long,
        [LPVOID, UINT, DWORD, POINTER(c_void_p)],
        ctypes.cast(buffer, LPVOID),
        UINT(len(pdb_bytes)),
        DWORD(0),
        byref(blob_ptr),
    )
    if hr < 0 or not blob_ptr:
        utils.release()
        return None

    pdb_ptr = c_void_p()
    if create(
        ctypes.cast(ctypes.c_char_p(_CLSID_DxcPdbUtils), c_void_p),
        ctypes.cast(ctypes.c_char_p(_IID_IDxcPdbUtils), c_void_p),
        byref(pdb_ptr),
    ) < 0:
        utils.release()
        return None
    pdb = _Vtbl(pdb_ptr)

    try:
        if pdb.call(_PDB_LOAD, ctypes.c_long, [c_void_p], blob_ptr) < 0:
            return None
        count = UINT(0)
        if pdb.call(
            _PDB_GET_SOURCE_COUNT, ctypes.c_long, [POINTER(UINT)], byref(count)
        ) < 0:
            return None

        files: dict[str, str] = {}
        for index in range(count.value):
            name_bstr = c_void_p()
            pdb.call(
                _PDB_GET_SOURCE_NAME,
                ctypes.c_long,
                [UINT, POINTER(c_void_p)],
                UINT(index),
                byref(name_bstr),
            )
            name = _bstr_to_str(name_bstr) or f"source_{index}"

            src_ptr = c_void_p()
            if pdb.call(
                _PDB_GET_SOURCE,
                ctypes.c_long,
                [UINT, POINTER(c_void_p)],
                UINT(index),
                byref(src_ptr),
            ) < 0 or not src_ptr:
                continue
            blob = _Vtbl(src_ptr)
            pointer = blob.call(_BLOB_GET_PTR, LPVOID, [])
            size = blob.call(_BLOB_GET_SIZE, ctypes.c_size_t, [])
            if pointer and size:
                raw = ctypes.string_at(pointer, size)
                files[name] = raw.decode("utf-8", "replace")
            blob.release()

        args: list[str] = []
        arg_count = UINT(0)
        if pdb.call(
            _PDB_GET_ARG_COUNT, ctypes.c_long, [POINTER(UINT)], byref(arg_count)
        ) >= 0:
            for index in range(arg_count.value):
                bstr = c_void_p()
                pdb.call(
                    _PDB_GET_ARG,
                    ctypes.c_long,
                    [UINT, POINTER(c_void_p)],
                    UINT(index),
                    byref(bstr),
                )
                text = _bstr_to_str(bstr)
                if text:
                    args.append(text)

        if not files:
            return None
        return {"files": files, "compile_args": args}
    finally:
        pdb.release()
        utils.release()


# ----------------------------------------------------------------------
# Fallback: parse the PDB container directly
# ----------------------------------------------------------------------
def dxbc_chunk(blob: bytes, want: str) -> Optional[bytes]:
    if len(blob) < 32 or blob[:4] != b"DXBC":
        return None
    count = struct.unpack_from("<I", blob, 28)[0]
    try:
        offsets = struct.unpack_from(f"<{count}I", blob, 32)
    except struct.error:
        return None
    for offset in offsets:
        if offset + 8 > len(blob):
            continue
        name = blob[offset : offset + 4].decode("ascii", "replace")
        size = struct.unpack_from("<I", blob, offset + 4)[0]
        if name == want:
            return blob[offset + 8 : offset + 8 + size]
    return None


def dxbc_chunk_names(blob: bytes) -> list[str]:
    if len(blob) < 32 or blob[:4] != b"DXBC":
        return []
    count = struct.unpack_from("<I", blob, 28)[0]
    try:
        offsets = struct.unpack_from(f"<{count}I", blob, 32)
    except struct.error:
        return []
    return [
        blob[offset : offset + 4].decode("ascii", "replace")
        for offset in offsets
        if offset + 8 <= len(blob)
    ]


def _inflate_srci(srci: bytes) -> str:
    """Locate and inflate the compressed source blob inside a SRCI chunk."""
    marker = srci.find(b"PK")
    start = marker if marker >= 0 else 0
    for offset in range(start, min(start + 64, len(srci))):
        for wbits in (15, -15):
            try:
                data = zlib.decompressobj(wbits).decompress(srci[offset:])
            except zlib.error:
                continue
            if len(data) > 200:
                return data.decode("utf-8", "replace")
    return ""


def split_line_sections(text: str) -> dict[str, str]:
    """Split preprocessed HLSL into {logical file: text} using #line directives."""
    sections: dict[str, list[str]] = {}
    matches = list(_LINE_DIRECTIVE.finditer(text))
    if not matches:
        return {}
    for index, match in enumerate(matches):
        name = match.group(2) or "(unnamed)"
        body_start = match.end()
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip("\n")
        if body.strip():
            sections.setdefault(name, []).append(body)
    return {name: "\n".join(parts) for name, parts in sections.items()}


def slice_entry_function(text: str, entry_point: str) -> str:
    """Return the entry function plus its attributes, or '' if not found.

    UE5's preprocessed output starts with a few hundred lines of generated
    helpers (select_internal overloads and similar), so locating the authored
    function is what makes the recovered source readable.
    """
    if not text or not entry_point:
        return ""
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        # The definition line, not a call site or a forward declaration.
        if entry_point in stripped and "(" in stripped and not stripped.endswith(";"):
            start = index
            break
    if start is None:
        return ""
    # Walk backwards over attributes and comments attached to the definition.
    while start > 0:
        previous = lines[start - 1].strip()
        if previous.startswith("[") or previous.startswith("//"):
            start -= 1
            continue
        break
    # Walk forwards to the matching closing brace.
    depth = 0
    seen_open = False
    end = len(lines)
    for index in range(start, len(lines)):
        depth += lines[index].count("{")
        if depth:
            seen_open = True
        depth -= lines[index].count("}")
        if seen_open and depth <= 0:
            end = index + 1
            break
    return "\n".join(lines[start:end])


def _extract_via_container(pdb_bytes: bytes) -> Optional[dict[str, Any]]:
    from .msf import MsfError, MsfFile

    try:
        msf = MsfFile(pdb_bytes)
    except (MsfError, struct.error):
        return None
    for index in range(msf.stream_count):
        blob = msf.stream(index)
        if len(blob) < 32 or blob[:4] != b"DXBC":
            continue
        srci = dxbc_chunk(blob, "SRCI")
        if not srci:
            continue
        text = _inflate_srci(srci)
        if not text:
            continue
        return {
            "full_text": text,
            "files": split_line_sections(text),
            "chunks": dxbc_chunk_names(blob),
            "stream": index,
        }
    return None


def extract_sources(pdb_path: Path) -> dict[str, Any]:
    """Recover the original HLSL for one shader PDB.

    The report always states which route produced the answer, so a caller can
    tell real source from a fallback rather than having to trust the tool.
    """
    report: dict[str, Any] = {
        "pdb": str(pdb_path),
        "ok": False,
        "files": {},
        "full_text": "",
        "entry_file": None,
        "compile_args": [],
        "method": None,
        "detail": None,
    }
    try:
        data = Path(pdb_path).read_bytes()
    except OSError as exc:
        report["detail"] = f"cannot read PDB: {exc}"
        return report

    via_dxc = _extract_via_dxc(data)
    if via_dxc:
        report.update(
            {
                "ok": True,
                "files": via_dxc["files"],
                "compile_args": via_dxc.get("compile_args", []),
                "method": "IDxcPdbUtils (dxcompiler.dll)",
            }
        )
    else:
        via_container = _extract_via_container(data)
        if via_container:
            report.update(
                {
                    "ok": True,
                    "files": via_container["files"],
                    "full_text": via_container["full_text"],
                    "method": (
                        f"SRCI chunk of DXBC in PDB stream {via_container['stream']}, "
                        "zlib inflated"
                    ),
                }
            )
        else:
            report["detail"] = (
                "IDxcPdbUtils could not load the PDB and no inflatable SRCI chunk was found"
            )
            return report

    files = report["files"]
    if files:
        if _SENTINEL in files:
            report["entry_file"] = _SENTINEL
        else:
            usf = [name for name in files if name.lower().endswith((".usf", ".hlsl"))]
            pool = usf or list(files)
            report["entry_file"] = max(pool, key=lambda name: len(files[name]))
        if not report["full_text"]:
            report["full_text"] = files.get(report["entry_file"], "")

    # DXC hands back one preprocessed translation unit per source, and UE5 packs
    # a large prologue of generated helpers ahead of the real shader. Splitting on
    # #line lets a caller ask for the authored body alone.
    combined = report["full_text"] or "\n".join(files.values())
    sections = split_line_sections(combined) if combined else {}
    if sections:
        report["sections"] = sections
        report["section_names"] = list(sections)
        body = sections.get(_SENTINEL)
        if body:
            report["shader_body"] = body
            report["shader_body_source"] = _SENTINEL
        else:
            largest = max(sections, key=lambda name: len(sections[name]))
            report["shader_body"] = sections[largest]
            report["shader_body_source"] = largest
    else:
        report["sections"] = {}
        report["section_names"] = []
        report["shader_body"] = report["full_text"]
        report["shader_body_source"] = report["entry_file"]
    return report


def find_pdb(
    search_dirs: list[Path], shader_hash: str, debug_name: str = ""
) -> Optional[Path]:
    """Locate a shader's PDB. UE5 writes <hash>.pdb into ShaderSymbols/<platform>."""
    names = [name for name in (debug_name, f"{shader_hash}.pdb" if shader_hash else "") if name]
    for directory in search_dirs:
        if not directory.exists():
            continue
        for name in names:
            candidate = directory / name
            if candidate.exists():
                return candidate
    # These directories hold hundreds of thousands of files, so only recurse when
    # the direct lookup missed.
    for directory in search_dirs:
        if not directory.exists():
            continue
        for name in names:
            for hit in directory.rglob(name):
                return hit
    return None
