"""Parsing the CSV event list produced by ``pixtool save-event-list``.

Columns are ``Queue ID, Parent, Name, Global ID`` plus any requested counters.
``Parent`` points at another row's Queue ID, which lets us rebuild the marker
hierarchy that PIX shows in its event tree.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

from .model import Event, EventKind

_DRAW = re.compile(r"^(DrawIndexedInstanced|DrawInstanced|DrawIndexed|Draw)\b", re.I)
_DISPATCH = re.compile(r"^(Dispatch|DispatchMesh)\b", re.I)
_RAYS = re.compile(r"^DispatchRays\b", re.I)
_INDIRECT = re.compile(r"^ExecuteIndirect\b", re.I)
_COPY = re.compile(r"^(Copy\w*|Resolve\w*|AtomicCopy\w*)\b", re.I)
_CLEAR = re.compile(r"^(Clear\w*|Discard\w*)\b", re.I)
_BARRIER = re.compile(r"^(ResourceBarrier|Barrier)\b", re.I)
_QUERY = re.compile(
    r"^(BeginQuery|EndQuery|ResolveQueryData|SetPredication|WriteBufferImmediate)\b", re.I
)
_SYNC = re.compile(r"^(Signal|Wait|Close|Reset|ExecuteCommandLists|Present)\b", re.I)
_STATE = re.compile(
    r"^(Set\w+|IASet\w+|OMSet\w+|RSSet\w+|SOSet\w+|BeginRenderPass|EndRenderPass)\b", re.I
)
_RAYTRACING = re.compile(
    r"^(BuildRaytracingAccelerationStructure"
    r"|EmitRaytracingAccelerationStructurePostbuildInfo"
    r"|CopyRaytracingAccelerationStructure)\b",
    re.I,
)


def classify(name: str) -> EventKind:
    text = name.strip()
    if _DRAW.match(text):
        return EventKind.DRAW
    if _RAYS.match(text):
        return EventKind.DISPATCH_RAYS
    if _DISPATCH.match(text):
        return EventKind.DISPATCH
    if _INDIRECT.match(text):
        return EventKind.EXECUTE_INDIRECT
    if _RAYTRACING.match(text):
        return EventKind.RAYTRACING
    if _COPY.match(text):
        return EventKind.COPY
    if _CLEAR.match(text):
        return EventKind.CLEAR
    if _BARRIER.match(text):
        return EventKind.BARRIER
    if _QUERY.match(text):
        return EventKind.QUERY
    if _SYNC.match(text):
        return EventKind.SYNC
    if _STATE.match(text):
        return EventKind.STATE
    return EventKind.MARKER


def _to_int(text: str | None) -> int | None:
    if not text:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return int(stripped)
    except ValueError:
        return None


def _clean_name(row: list[str], name_col: int, global_col: int) -> str:
    """Recover the Name cell even when it was split on an embedded comma.

    pixtool writes ``Queue ID, Parent, Name, Global ID`` with a space after each
    comma, and quotes a Name that itself contains commas. Because the quote then
    starts one character late (after that space), a strict CSV reader does not
    treat it as a quoting character and splits the name into several cells, e.g.
    ``"AccessModePass[Graphics] (Textures: 0`` + ``Buffers: 2)"``. That silently
    corrupted 28 rows in Tiled.events.csv: the visible name was truncated and
    the Global ID column shifted, so an event could look like it had no id.

    ``skipinitialspace=True`` fixes the parse going forward. This stays as the
    repair path for a row that still arrives over-split (the quote convention is
    pixtool's, not ours, so it may vary by version): everything from the name
    column up to the trailing Global ID column is rejoined with ", ".
    """
    # A well-formed row has exactly one Name cell, so this is a no-op for it.
    # An over-split row has extra cells between Name and the trailing columns
    # (Global ID plus any counters), and all of them belong to the name.
    extra = max(len(row) - global_col - 1, 0)
    end = name_col + 1 + extra
    parts = [cell.strip() for cell in row[name_col:end]]
    joined = ", ".join(part for part in parts if part)
    return joined.strip().strip('"').strip()


def parse_event_list(path: str | Path) -> list[Event]:
    """Read the CSV and return the flat event list with tree links set."""
    csv_path = Path(path)
    events: list[Event] = []
    by_queue: dict[int, Event] = {}

    with open(csv_path, "r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.reader(handle, skipinitialspace=True)
        header = next(reader, None)
        if not header:
            return []
        columns = [item.strip() for item in header]
        index = {name: position for position, name in enumerate(columns)}
        queue_col = index.get("Queue ID", 0)
        parent_col = index.get("Parent", 1)
        name_col = index.get("Name", 2)
        global_col = index.get("Global ID", 3)
        counter_columns = [
            (position, name)
            for position, name in enumerate(columns)
            if position not in (queue_col, parent_col, name_col, global_col)
        ]

        for row in reader:
            if not row or len(row) <= name_col:
                continue
            queue_id = _to_int(row[queue_col])
            if queue_id is None:
                continue
            name = _clean_name(row, name_col, global_col)
            # An over-split name pushes every later column right by the same
            # amount, so the trailing columns must be read from the end.
            shift = max(len(row) - len(columns), 0)
            event = Event(
                queue_id=queue_id,
                parent_queue_id=_to_int(row[parent_col]) if len(row) > parent_col else -1,
                name=name,
                global_id=(
                    _to_int(row[global_col + shift])
                    if len(row) > global_col + shift
                    else None
                ),
                kind=classify(name),
            )
            for position, counter_name in counter_columns:
                shifted = position + shift
                if shifted < len(row) and row[shifted].strip():
                    event.counters[counter_name] = row[shifted].strip()
            events.append(event)
            by_queue[queue_id] = event

    for event in events:
        parent_id = event.parent_queue_id
        if parent_id is not None and parent_id >= 0:
            parent = by_queue.get(parent_id)
            if parent is not None and parent is not event:
                event.parent = parent
                parent.children.append(event)

    for event in events:
        depth = 0
        node = event.parent
        while node is not None and depth < 512:
            depth += 1
            node = node.parent
        event.depth = depth
    return events


def roots(events: list[Event]) -> list[Event]:
    return [event for event in events if event.parent is None]
