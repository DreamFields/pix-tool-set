"""XPRESS decompression for the PIX ``resources.bin`` blob store.

PIX packs every captured resource (shader bytecode, vertex/index data, texture
mips) into one ``resources.bin``.  The generated C++ reads it with a *sequential*
cursor::

    g_resourceReader->Read(data, <compressedSize>);

Each call consumes exactly ``compressedSize`` bytes from the current offset and
XPRESS-decompresses them.  There is no index table, so the only way to address a
blob is to replay the Read() calls in program order; ``ResourceStream`` does that
once and records absolute offsets so later access is random.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from ..errors import PixToolError

COMPRESS_ALGORITHM_XPRESS = 3
_ERROR_INSUFFICIENT_BUFFER = 122

try:
    _cabinet: ctypes.WinDLL | None = ctypes.WinDLL("Cabinet.dll")
except OSError:  # pragma: no cover - non Windows
    _cabinet = None

if _cabinet is not None:
    _cabinet.CreateDecompressor.argtypes = [
        wt.DWORD,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _cabinet.CreateDecompressor.restype = wt.BOOL
    _cabinet.Decompress.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    _cabinet.Decompress.restype = wt.BOOL
    _cabinet.CloseDecompressor.argtypes = [ctypes.c_void_p]
    _cabinet.CloseDecompressor.restype = wt.BOOL


def _error(message: str, **details) -> PixToolError:
    return PixToolError(
        code="decompression_failed",
        message=message,
        stage="resources.bin",
        suggestion="Re-run `session-open --force` to regenerate the export.",
        details=details,
    )


class XpressDecompressor:
    """Thin ctypes wrapper over the Windows Compression API (XPRESS)."""

    def __init__(self) -> None:
        if _cabinet is None:
            raise _error("Cabinet.dll is unavailable; XPRESS decompression needs Windows.")
        self._handle = ctypes.c_void_p()
        if not _cabinet.CreateDecompressor(
            COMPRESS_ALGORITHM_XPRESS, None, ctypes.byref(self._handle)
        ):
            raise _error(f"CreateDecompressor failed (err={ctypes.GetLastError()})")

    def decompress(self, src: bytes, expected: int | None = None) -> bytes:
        if not src:
            return b""
        assert _cabinet is not None
        if expected is not None:
            capacity = expected
        else:
            needed = ctypes.c_size_t(0)
            _cabinet.Decompress(self._handle, src, len(src), None, 0, ctypes.byref(needed))
            capacity = needed.value or max(1 << 16, len(src) * 64)

        for _ in range(6):
            buffer = ctypes.create_string_buffer(capacity)
            produced = ctypes.c_size_t(0)
            ok = _cabinet.Decompress(
                self._handle, src, len(src), buffer, capacity, ctypes.byref(produced)
            )
            if ok:
                return buffer.raw[: produced.value]
            if ctypes.GetLastError() == _ERROR_INSUFFICIENT_BUFFER:
                capacity = max(capacity * 4, produced.value or capacity * 4)
                continue
            raise _error(
                f"Decompress failed (err={ctypes.GetLastError()})",
                compressed_size=len(src),
            )
        raise _error("Decompress failed: output buffer kept growing")

    def close(self) -> None:
        if _cabinet is not None and self._handle:
            _cabinet.CloseDecompressor(self._handle)
            self._handle = ctypes.c_void_p()

    def __enter__(self) -> "XpressDecompressor":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover
        try:
            self.close()
        except Exception:
            pass


@dataclass(frozen=True, slots=True)
class BlobRef:
    index: int
    offset: int
    compressed_size: int


class ResourceStream:
    """Random-access reader over ``resources.bin``."""

    def __init__(self, path: str | Path, sizes: list[int] | None = None) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise _error(f"resources.bin not found: {self.path}")
        self._decompressor = XpressDecompressor()
        self._refs: list[BlobRef] = []
        if sizes:
            self.build_index(sizes)

    def build_index(self, sizes: list[int]) -> list[BlobRef]:
        refs: list[BlobRef] = []
        cursor = 0
        for index, size in enumerate(sizes):
            refs.append(BlobRef(index=index, offset=cursor, compressed_size=size))
            cursor += size
        self._refs = refs
        return refs

    @property
    def refs(self) -> list[BlobRef]:
        return list(self._refs)

    @property
    def file_size(self) -> int:
        return self.path.stat().st_size

    def read_at(self, offset: int, compressed_size: int) -> bytes:
        with open(self.path, "rb") as handle:
            handle.seek(offset)
            raw = handle.read(compressed_size)
        if len(raw) != compressed_size:
            raise _error(
                f"Short read at 0x{offset:x}: wanted {compressed_size}, got {len(raw)}"
            )
        return self._decompressor.decompress(raw)

    def read_index(self, index: int) -> bytes:
        if not self._refs:
            raise _error("Blob index has not been built for this stream.")
        if index < 0 or index >= len(self._refs):
            raise _error(f"Blob index {index} out of range (0..{len(self._refs) - 1})")
        ref = self._refs[index]
        return self.read_at(ref.offset, ref.compressed_size)

    def iter_blobs(self, limit: int | None = None) -> Iterator[tuple[BlobRef, bytes]]:
        with open(self.path, "rb") as handle:
            for ref in self._refs[:limit]:
                handle.seek(ref.offset)
                raw = handle.read(ref.compressed_size)
                if len(raw) != ref.compressed_size:
                    break
                try:
                    yield ref, self._decompressor.decompress(raw)
                except PixToolError:
                    continue

    def close(self) -> None:
        self._decompressor.close()
