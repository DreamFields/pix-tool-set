"""Replay the per-frame CPU writes that PIX records for mapped resources.

A UE5 frame updates its big upload buffers from the CPU between command lists.
The exporter emits that as, in RenderFrameWorker_*.cpp::

    std::vector<BYTE> data;
    size_t offset = 0;
    g_resourceReader->Read(data, ResourceModificationSize_000);
    ModifyResource_000_000(data, offset);

and in ResourceModifications_*.cpp::

    Map(resource 2955)
    for (i = 0; i < 4; ++i)
        memcpy(mappedData + 4096 * PagesIndex_2955_8[i], &data[offset], 4096);
        offset += 4096;
    memcpy(mappedData + 4096 * PagesIndex_2955_8[4], &data[offset], 4096);

So the true contents a shader sees are the initial upload *plus* these page
writes. Reading only the initial blob returns stale bytes, which is what made
cbuffer values look like garbage (a uint field reading 1065353216, i.e. float
1.0f, was the giveaway).

This module reconstructs, per resource, the list of (page, blob, byte range)
writes so a caller can materialise the exact bytes bound at a draw.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

PAGE_SIZE = 4096

_RE_MOD_FUNC = re.compile(r"^void\s+(ModifyResource_\w+)\s*\(")
_RE_MAP = re.compile(r"GetResource\((\d+)\)\.Get\(\)->Map\(")
_RE_LOOP = re.compile(r"for\s*\(auto\s+i\s*=\s*0u;\s*i\s*<\s*(\d+);")
_RE_COPY_LOOP = re.compile(r"memcpy\(mappedData \+ 4096 \* (\w+)\[i\]")
_RE_COPY_ONE = re.compile(r"memcpy\(mappedData \+ 4096 \* (\w+)\[(\d+)\]")
_RE_PAGES_DECL = re.compile(r"static size_t (PagesIndex_\w+)\[(\d+)\]\s*=\s*\{")
_RE_SIZE_DECL = re.compile(r"static size_t (ResourceModificationSize_\w+)\s*=\s*(\d+)")
_RE_CALL = re.compile(r"(ModifyResource_\w+)\s*\(data,\s*offset\)")
_RE_READ_SIZE = re.compile(r"g_resourceReader->Read\(\s*data\s*,\s*(\w+)\s*\)")


@dataclass(slots=True)
class PageWrite:
    """One 4 KB page written into a resource from a modification blob."""

    resource_id: int
    page: int
    blob_index: int
    blob_offset: int
    size: int = PAGE_SIZE

    @property
    def resource_offset(self) -> int:
        return self.page * PAGE_SIZE


@dataclass(slots=True)
class ModificationPlan:
    """All recorded CPU page writes, grouped per resource."""

    writes: dict[int, list[PageWrite]] = field(default_factory=dict)
    blob_sizes: dict[int, int] = field(default_factory=dict)

    def for_resource(self, resource_id: int) -> list[PageWrite]:
        return self.writes.get(resource_id, [])

    @property
    def resource_count(self) -> int:
        return len(self.writes)

    @property
    def write_count(self) -> int:
        return sum(len(items) for items in self.writes.values())


def _read_page_tables(root: Path) -> tuple[dict[str, list[int]], dict[str, int]]:
    """Parse PagesIndex_* arrays and ResourceModificationSize_* constants."""
    header = root / "ResourceModifications.h"
    pages: dict[str, list[int]] = {}
    sizes: dict[str, int] = {}
    if not header.exists():
        return pages, sizes
    text = header.read_text(encoding="utf-8", errors="replace")

    for match in _RE_SIZE_DECL.finditer(text):
        sizes[match.group(1)] = int(match.group(2))

    for match in _RE_PAGES_DECL.finditer(text):
        name = match.group(1)
        start = text.find("{", match.end() - 1)
        end = text.find("}", start)
        if start < 0 or end < 0:
            continue
        body = text[start + 1 : end]
        pages[name] = [int(token) for token in re.findall(r"\d+", body)]
    return pages, sizes


def _read_call_order(root: Path) -> list[tuple[str, str]]:
    """Return [(modify_function, size_constant)] in frame execution order."""
    order: list[tuple[str, str]] = []
    for path in sorted(root.glob("RenderFrameWorker_*.cpp")):
        pending_size: Optional[str] = None
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                match = _RE_READ_SIZE.search(line)
                if match:
                    pending_size = match.group(1)
                    continue
                match = _RE_CALL.search(line)
                if match:
                    order.append((match.group(1), pending_size or ""))
                    pending_size = None
    return order


def _read_functions(root: Path) -> dict[str, list[tuple[int, str, Optional[int], int]]]:
    """Parse each ModifyResource_* body.

    Returns {function: [(resource_id, pages_array, explicit_index, loop_count)]}
    where explicit_index is set for a single-page copy and loop_count for a loop.
    """
    out: dict[str, list[tuple[int, str, Optional[int], int]]] = {}
    for path in sorted(root.glob("ResourceModifications_*.cpp")):
        current: Optional[str] = None
        resource: Optional[int] = None
        loop_count = 0
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                match = _RE_MOD_FUNC.match(line)
                if match:
                    current = match.group(1)
                    out.setdefault(current, [])
                    resource = None
                    loop_count = 0
                    continue
                if current is None:
                    continue
                match = _RE_MAP.search(line)
                if match:
                    resource = int(match.group(1))
                    continue
                match = _RE_LOOP.search(line)
                if match:
                    loop_count = int(match.group(1))
                    continue
                match = _RE_COPY_LOOP.search(line)
                if match and resource is not None:
                    out[current].append((resource, match.group(1), None, loop_count))
                    loop_count = 0
                    continue
                match = _RE_COPY_ONE.search(line)
                if match and resource is not None:
                    out[current].append(
                        (resource, match.group(1), int(match.group(2)), 0)
                    )
    return out


def build_plan(root: Path, blob_index_of_size: dict[str, int]) -> ModificationPlan:
    """Reconstruct every recorded page write.

    `blob_index_of_size` maps a ResourceModificationSize_* constant name to the
    index of the blob that the matching Read() consumed, which the caller derives
    while numbering the whole resource stream.
    """
    pages, sizes = _read_page_tables(root)
    functions = _read_functions(root)
    plan = ModificationPlan()

    for function, size_name in _read_call_order(root):
        steps = functions.get(function)
        if not steps:
            continue
        blob_index = blob_index_of_size.get(size_name)
        if blob_index is None:
            continue
        plan.blob_sizes[blob_index] = sizes.get(size_name, 0)
        cursor = 0
        for resource_id, array_name, explicit, loop_count in steps:
            table = pages.get(array_name) or []
            if explicit is None:
                for position in range(loop_count):
                    if position >= len(table):
                        break
                    plan.writes.setdefault(resource_id, []).append(
                        PageWrite(resource_id, table[position], blob_index, cursor)
                    )
                    cursor += PAGE_SIZE
            else:
                if explicit < len(table):
                    plan.writes.setdefault(resource_id, []).append(
                        PageWrite(resource_id, table[explicit], blob_index, cursor)
                    )
                cursor += PAGE_SIZE
    return plan
