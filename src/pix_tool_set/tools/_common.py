"""Shared helpers for tool handlers: schema fragments and pagination."""

from __future__ import annotations

from typing import Any

from ..registry import get_registry

registry = get_registry()
tool = registry.tool

# --------------------------------------------------------------------------
# reusable schema fragments
# --------------------------------------------------------------------------
SESSION_PARAMS: dict[str, Any] = {
    "session": {
        "type": "string",
        "description": "Session name from session-open. Defaults to the most recently used session.",
    },
    "capture": {
        "type": "string",
        "description": "Path to a .wpix file. Use instead of --session to target a capture directly.",
    },
    "export_dir": {
        "type": "string",
        "description": "Existing pixtool C++ export directory. Advanced override.",
    },
}

PAGE_PARAMS: dict[str, Any] = {
    "offset": {"type": "integer", "description": "Number of items to skip. Default 0."},
    "limit": {"type": "integer", "description": "Maximum items to return. Default 50."},
}

DRAW_SELECTOR: dict[str, Any] = {
    "draw_index": {
        "type": "integer",
        "description": (
            "Zero-based index into the draw call list (see list-draw-calls). Primary "
            "selector: it addresses every action in the capture, including those on "
            "command queues whose event list was not exported."
        ),
    },
    "queue_id": {
        "type": "integer",
        "description": (
            "Row identifier from the exported event list, usable only for events whose "
            "own queue is covered by that export; it is absent for the rest. Not "
            "interchangeable with the 'Queue ID' column shown in the PIX GUI on a "
            "multi-queue capture. Global ID is reported in results but not accepted as "
            "input."
        ),
    },
}

PASS_SELECTOR: dict[str, Any] = {
    "pass_name": {"type": "string", "description": "Pass name (substring match)."},
    "pass_index": {"type": "integer", "description": "Pass index from list-passes."},
    "queue_id": {
        "type": "integer",
        "description": (
            "Exported event list row id of any event inside the pass, or of the pass "
            "marker itself. Available only for passes on the exported queue; use "
            "pass_index or pass_name to reach the others."
        ),
    },
}

# One sentence, reused wherever a Queue ID miss is reported, because the failure is
# counter-intuitive enough that repeating it is cheaper than a support round trip: the
# exported list numbers its rows sequentially, so any integer below the row count
# resolves to *some* row. A Queue ID copied from the PIX GUI of a multi-queue capture is
# therefore not rejected, it silently addresses an unrelated event.
QUEUE_ID_IS_ROW_ORDER = (
    "Note: in this export the Queue ID column is simply the row number of the event "
    "list, so an id taken from the PIX GUI of a multi-queue capture will resolve to a "
    "different event instead of failing. Prefer draw_index, which is unambiguous."
)


def queue_id_coverage(capture) -> dict[str, Any]:
    """How much of the capture the exported event list can actually address.

    Callers need this to phrase a missing Queue ID as a known gap in the export rather
    than as a parse failure. The two are indistinguishable from a null field alone, and
    reading a null as a bug sent one investigation down the wrong path entirely.

    ``event_list_is_single_queue`` is inferred from actions that carry no row at all: if
    even one exists, the export cannot be covering every queue the frame submitted to.
    It is deliberately not derived from queue names, since attributing an action to a
    queue is a separate problem this module does not attempt.
    """
    draws = capture.draw_calls
    missing = [draw for draw in draws if draw.queue_id is None]
    return {
        "draw_count": len(draws),
        "draws_without_queue_id": len(missing),
        "draws_with_queue_id": len(draws) - len(missing),
        "passes_without_queue_id": len({draw.pass_name for draw in missing}),
        "event_list_rows": len(capture.events),
        "event_list_is_single_queue": bool(missing),
    }


def note_missing_queue_id(result, draw, *, level: str = "warning"):
    """Attach the standard explanation for an action that has no Queue ID.

    Every tool reporting a queue_id needs to say the same thing when it is null, and
    saying it differently in each place made the gap look like several unrelated bugs.
    Kept as a helper so the wording stays identical, and so the distinction it protects
    survives: the bindings and counts around it come from the C++ export and remain
    complete; only the ability to name the action by an event list id is missing.
    """
    if draw.queue_id is not None:
        return result
    return result.add_diagnostic(
        level,
        f"No Queue ID for this action (draw_index={draw.index}, "
        f"global_id={draw.global_id}): the exported event list has no row for it, which "
        "happens when the capture spans several command queues and the export covers "
        "only one. Data read from the C++ export is unaffected; select this action by "
        f"draw_index={draw.index}.",
        draw_index=draw.index,
    )





def object_schema(
    *fragments: dict[str, Any],
    required: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Compose a JSON-Schema object from reusable fragments plus inline props."""
    properties: dict[str, Any] = {}
    for fragment in fragments:
        properties.update(fragment)
    properties.update(extra)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def with_session(*fragments: dict[str, Any], required: list[str] | None = None, **extra: Any):
    return object_schema(SESSION_PARAMS, *fragments, required=required, **extra)


# --------------------------------------------------------------------------
# pagination
# --------------------------------------------------------------------------
DEFAULT_LIMIT = 50


def page_args(args: dict[str, Any], default_limit: int = DEFAULT_LIMIT) -> tuple[int, int]:
    offset = max(int(args.get("offset", 0) or 0), 0)
    raw_limit = args.get("limit")
    limit = default_limit if raw_limit is None else int(raw_limit)
    return offset, max(limit, 0)


def page_envelope(total: int, offset: int, limit: int, returned: int) -> dict[str, Any]:
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "returned": returned,
        "has_more": offset + returned < total,
        "next_offset": offset + returned if offset + returned < total else None,
    }


def top_n(items: list[dict[str, Any]], key: str, count: int = 10) -> list[dict[str, Any]]:
    return sorted(items, key=lambda entry: -(entry.get(key) or 0))[:count]


def percent(part: float, whole: float) -> float:
    return round(100.0 * part / whole, 2) if whole else 0.0


# --------------------------------------------------------------------------
# PIX identifiers
# --------------------------------------------------------------------------
def resolve_pass(capture, args: dict[str, Any]) -> dict[str, Any]:
    """Resolve a pass from a name, a pass index, or an exported-event-list Queue ID.

    Queue ID was originally the toolkit's single event identifier: it appears on every
    row of the exported list, whereas Global ID appears only on actions and so cannot
    name a pass marker. That reasoning still holds *within* one queue and is why Global
    ID remains output-only.

    What it missed is that the export covers a single command queue. Passes submitted to
    another queue have no row at all, so an identifier defined by that list cannot be the
    only way in -- on Tiled.wpix it leaves 74 passes unreachable. ``pass_index`` and
    ``pass_name`` come from marker grouping in the C++ export, which sees every pass, so
    they are the selectors that always work; Queue ID is now a convenience for the
    exported queue.

    An id still wins over a name, because it is unambiguous while a name is a substring
    match that can hit several passes.
    """
    from ..errors import invalid_argument, not_found

    queue_id = args.get("queue_id")
    if queue_id is not None:
        entry = capture.find_pass_by_event(queue_id=queue_id)
        if entry is None:
            row = capture.event_by_queue_id(int(queue_id))
            if row is None:
                raise not_found(
                    "pass",
                    f"queue_id={queue_id}",
                    f"The exported event list has {len(capture.events)} rows and none "
                    f"carries this id. Use list-passes or find-pass --name instead. "
                    + QUEUE_ID_IS_ROW_ORDER,
                )
            raise not_found(
                "pass",
                f"queue_id={queue_id}",
                f"Row {queue_id} of the exported event list is {row.name!r} and no marker "
                f"encloses it. Use list-passes or find-pass --name instead. "
                + QUEUE_ID_IS_ROW_ORDER,
            )
        return entry

    key = args.get("pass_index")
    if key is None:
        key = args.get("pass_name")
    if key is None:
        raise invalid_argument(
            "pass_name/pass_index/queue_id", "provide one of them"
        )
    entry = capture.find_pass(key)
    if entry is None:
        raise not_found("pass", key, "Run list-passes to see valid names and indices.")
    return entry


    key = args.get("pass_index")
    if key is None:
        key = args.get("pass_name")
    if key is None:
        raise invalid_argument(
            "pass_name/pass_index/queue_id", "provide one of them"
        )
    entry = capture.find_pass(key)
    if entry is None:
        raise not_found("pass", key, "Run list-passes to see valid names and indices.")
    return entry


def resolve_draw(capture, args: dict[str, Any], *, what: str = "draw call"):
    """Resolve a draw call from a draw index or an exported-event-list Queue ID.

    Centralised so the "which event?" question has exactly one answer across the
    toolkit, and so the not-found error names the selector the caller actually used.

    The Queue ID failure is split into two messages because they call for opposite
    reactions, and one of them is a trap. An id beyond the row count simply does not
    exist. An id *within* the row count always names a row -- the column is row order in
    this export -- so a miss means the row is a marker or a state-setting call rather
    than an action, and, worse, a hit proves nothing about intent: an id lifted from the
    PIX GUI of a multi-queue capture lands on an unrelated row and returns confident
    data for the wrong event. Naming the row we landed on is the only way the caller can
    notice, so the message quotes it.
    """
    from ..errors import invalid_argument, not_found

    draw_index = args.get("draw_index")
    queue_id = args.get("queue_id")
    if draw_index is None and queue_id is None:
        raise invalid_argument(
            "draw_index/queue_id",
            "provide draw_index (addresses every action) or queue_id (exported queue only)",
        )

    draw = capture.resolve_draw(draw_index=draw_index, queue_id=queue_id)
    if draw is not None:
        return draw

    if queue_id is not None:
        row = capture.event_by_queue_id(int(queue_id))
        if row is None:
            raise not_found(
                what,
                f"queue_id={queue_id}",
                f"The exported event list has {len(capture.events)} rows and none carries "
                f"this id. " + QUEUE_ID_IS_ROW_ORDER,
            )
        raise not_found(
            what,
            f"queue_id={queue_id}",
            f"Row {queue_id} of the exported event list is {row.name!r}, which is not an "
            f"action, so no {what} corresponds to it. " + QUEUE_ID_IS_ROW_ORDER,
        )

    raise not_found(
        what,
        f"draw_index={draw_index}",
        f"Valid draw indices are 0..{len(capture.draw_calls) - 1}. Run list-draw-calls "
        "or find-draw-calls to locate one.",
    )




def pass_identity(entry: dict[str, Any]) -> dict[str, Any]:
    """The PIX identifiers that address a pass, for splicing into any payload.

    Every payload naming a pass should carry these, because `pass_index` is ours
    alone: it is derived from marker grouping and means nothing in the PIX UI. Queue ID
    is what the user can see and type, so omitting it forces them to run another tool
    just to translate our answer back into something they can act on.

    Two Queue IDs are reported because they answer different questions:
      * ``queue_id`` is the pass's first action, which is what other tools accept as a
        selector for reading bindings, values or shaders.
      * ``marker_queue_id`` is the marker row that opens the pass. Markers carry no
        Global ID, so this is the only id addressing the pass row itself.

    Both are null for a pass on a queue the export missed. A bare null there is
    indistinguishable from a parsing failure, so ``queue_id_unavailable`` states the
    reason and ``draw_index`` carries the selector that still works -- a caller should
    never have to guess which of the two situations it is looking at.
    """
    identity = {
        "queue_id": entry.get("first_queue_id"),
        "marker_queue_id": entry.get("marker_queue_id"),
        "first_queue_id": entry.get("first_queue_id"),
        "last_queue_id": entry.get("last_queue_id"),
        "first_global_id": entry.get("first_global_id"),
        "draw_index": entry.get("first_draw_index"),
    }
    if identity["queue_id"] is None and identity["marker_queue_id"] is None:
        identity["queue_id_unavailable"] = (
            "This pass has no row in the exported event list, which covers a single "
            "command queue. Address it by pass_index or draw_index."
        )
    return identity

