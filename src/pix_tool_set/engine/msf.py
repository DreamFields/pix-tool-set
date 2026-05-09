"""Minimal MSF (PDB) reader, just enough to pull DXC's source streams out.

Why hand-rolled: the goal is zero third-party dependencies (no pdbparse, no
llvm-pdbutil). DXC writes HLSL source into a PDB whose named streams include
per-file source text, so only the MSF layer plus the stream directory and the
name table are needed - not full CodeView parsing.

MSF 7.00 layout:
  page 0            superblock: magic, page size, directory size + page map
  directory         stream count, then each stream's size, then page lists
  stream 1          PDB info stream: version, signature, age, and the
                    name -> stream index table
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Optional

MSF_MAGIC = b"Microsoft C/C++ MSF 7.00\r\n\x1aDS\x00\x00\x00"


class MsfError(Exception):
    """Raised when the file is not a usable MSF container."""


class MsfFile:
    """Random access to the numbered streams of an MSF/PDB container."""

    def __init__(self, data: bytes) -> None:
        if not data.startswith(MSF_MAGIC):
            raise MsfError("not an MSF 7.00 container")
        self._data = data
        (
            self.page_size,
            _free_page_map,
            self.page_count,
            directory_size,
            _reserved,
            directory_page_map,
        ) = struct.unpack_from("<6I", data, len(MSF_MAGIC))
        if self.page_size == 0:
            raise MsfError("zero page size")
        self.streams: list[list[int]] = []
        self.stream_sizes: list[int] = []
        self._read_directory(directory_size, directory_page_map)

    # -- internals ----------------------------------------------------
    def _page(self, index: int) -> bytes:
        start = index * self.page_size
        return self._data[start : start + self.page_size]

    def _pages_needed(self, size: int) -> int:
        return (size + self.page_size - 1) // self.page_size

    def _gather(self, pages: list[int], size: int) -> bytes:
        out = bytearray()
        for page in pages:
            out += self._page(page)
        return bytes(out[:size])

    def _read_directory(self, directory_size: int, directory_page_map: int) -> None:
        # The page map itself is a list of page numbers holding the directory.
        map_pages = self._pages_needed(directory_size)
        raw_map = self._page(directory_page_map)
        try:
            pages = list(struct.unpack_from(f"<{map_pages}I", raw_map, 0))
        except struct.error as exc:
            raise MsfError(f"bad directory page map: {exc}") from exc

        directory = self._gather(pages, directory_size)
        if len(directory) < 4:
            raise MsfError("directory too small")
        count = struct.unpack_from("<I", directory, 0)[0]
        if count > 1_000_000:
            raise MsfError(f"implausible stream count {count}")

        sizes = []
        cursor = 4
        for _ in range(count):
            (size,) = struct.unpack_from("<i", directory, cursor)
            cursor += 4
            sizes.append(0 if size < 0 else size)

        for size in sizes:
            needed = self._pages_needed(size)
            page_list = list(struct.unpack_from(f"<{needed}I", directory, cursor))
            cursor += needed * 4
            self.streams.append(page_list)
            self.stream_sizes.append(size)

    # -- public -------------------------------------------------------
    @property
    def stream_count(self) -> int:
        return len(self.streams)

    def stream(self, index: int) -> bytes:
        if not 0 <= index < len(self.streams):
            return b""
        return self._gather(self.streams[index], self.stream_sizes[index])

    def named_streams(self) -> dict[str, int]:
        """Parse stream 1's name table: {stream name: stream index}."""
        info = self.stream(1)
        if len(info) < 28:
            return {}
        # version, signature, age, guid(16 bytes)
        cursor = 4 + 4 + 4 + 16
        if len(info) < cursor + 8:
            return {}
        names_size = struct.unpack_from("<I", info, cursor)[0]
        cursor += 4
        names_blob = info[cursor : cursor + names_size]
        cursor += names_size
        if len(info) < cursor + 8:
            return {}
        size, _capacity = struct.unpack_from("<2I", info, cursor)
        cursor += 8

        def read_bitset(offset: int) -> tuple[list[int], int]:
            (word_count,) = struct.unpack_from("<I", info, offset)
            offset += 4
            words = struct.unpack_from(f"<{word_count}I", info, offset)
            offset += word_count * 4
            bits = [
                index
                for index, word in enumerate(words)
                for bit in range(32)
                if word & (1 << bit)
                for index in (index * 32 + bit,)
            ]
            return bits, offset

        try:
            present, cursor = read_bitset(cursor)
            _deleted, cursor = read_bitset(cursor)
        except struct.error:
            return {}

        out: dict[str, int] = {}
        for _ in range(size):
            if len(info) < cursor + 8:
                break
            name_offset, stream_index = struct.unpack_from("<2I", info, cursor)
            cursor += 8
            end = names_blob.find(b"\x00", name_offset)
            if end < 0:
                continue
            name = names_blob[name_offset:end].decode("utf-8", "replace")
            if name:
                out[name] = stream_index
        return out


def open_msf(path: Path) -> Optional[MsfFile]:
    try:
        return MsfFile(Path(path).read_bytes())
    except (MsfError, OSError, struct.error):
        return None
