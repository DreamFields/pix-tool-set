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
            "selector: it addresses every action in the capture, and it is the only one "
            "that works for actions on a queue the exported event list does not cover, "
            "since those have no Queue ID at all."
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
    # Queue qualifiers narrow an id, they do not locate on their own -- a queue
    # holds hundreds of draws. They exist because a capture can span several
    # queues while the exported event list covers only one, so a Queue ID read off
    # such a capture is only unique within that queue; passing the queue alongside
    # turns a wrong-queue hit into a clean not-found instead of a plausible-looking
    # wrong answer. Omitting both keeps the pre-existing behaviour exactly.
    "queue_name": {
        "type": "string",
        "description": (
            "Optional queue restriction, substring match on the queue name as PIX shows "
            "it, e.g. 'Compute' for 'Compute Queue (GPU 0)'. Narrows the selectors above; "
            "it cannot select a draw on its own."
        ),
    },
    "queue_object_id": {
        "type": "integer",
        "description": (
            "Optional queue restriction by ID3D12CommandQueue ApiObjectId (see "
            "queue-attribution). This is an object id, not a Queue ID."
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


def note_missing_queue_id(result, draw, *, level: str = "warning") -> bool:
    """Explain a null queue_id in terms of the queue that action actually ran on.

    Lives here rather than in the one tool that needs it because a null queue_id is
    not specific to draw-state: any payload quoting an id can hit it, and the
    explanation has to stay identical everywhere or the two failure modes it
    separates will get confused again.

    The distinction worth preserving: a missing Queue ID means the exported event
    list has no row for the action, not that the action or its bindings are
    missing. Bindings come from the C++ export and are unaffected. Naming the
    queue turns "we have no idea what this is" into "it ran on the compute queue,
    whose event list was not exported", which is a fact the caller can act on --
    and it names the selector that does work instead of leaving them to guess.

    ``level`` exists because the same fact is a warning where the caller asked for
    an id and an aside where they only asked "where am I": locate-event already
    answers with the draw index, so flagging its own reply as degraded would be
    noise. Returns whether a diagnostic was added, so a caller can decide what
    else to say without re-testing the condition.
    """
    if draw.queue_id is not None:
        return False

    attribution = draw.queue_attribution
    queue_name = attribution.get("queue_name") or "an unidentified queue"
    queue_object_id = attribution.get("queue_object_id")
    where = (
        f"{queue_name} (queue object {queue_object_id})"
        if queue_object_id is not None
        else queue_name
    )
    result.add_diagnostic(
        level,
        f"No Queue ID for this action (draw_index={draw.index}, "
        f"global_id={draw.global_id}). It ran on {where}, "
        "and the exported event list does not cover that queue, so PIX never wrote a "
        "Queue ID for it. The id cannot be derived: Queue ID is not a per-queue call "
        "count, and a synthesised one would address a different row. Bindings and "
        "counts read from the C++ export are unaffected -- select this action with "
        f"draw_index={draw.index}.",
        draw_index=draw.index,
    )
    return True


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


def draw_selector_args(args: dict[str, Any]) -> dict[str, Any]:
    """Extract the DRAW_SELECTOR keys for a direct ``capture.resolve_draw`` call.

    A handful of tools cannot use ``resolve_draw`` below because they fall back to
    a marker or answer with an event when no draw matches. Without this helper they
    would hand-pick draw_index/queue_id and silently drop the queue qualifiers that
    DRAW_SELECTOR advertises -- a parameter accepted and ignored is worse than one
    rejected, because the caller believes the restriction was applied.
    """
    return {
        "draw_index": args.get("draw_index"),
        "queue_id": args.get("queue_id"),
        "queue_name": args.get("queue_name"),
        "queue_object_id": args.get("queue_object_id"),
    }


def resolve_draw(capture, args: dict[str, Any], *, what: str = "draw call"):
    """Resolve a draw call from a draw index or an exported-event-list Queue ID.

    Centralised so the "which event?" question has exactly one answer across the
    toolkit, and so the not-found error names the selector the caller actually used.

    ``queue_name`` / ``queue_object_id`` are optional qualifiers on top of that id.
    They are forwarded verbatim; when absent this behaves exactly as before, which
    is why every existing caller needed no change.

    The Queue ID failure is split into several messages because they call for opposite
    reactions, and one of them is a trap. An id beyond the row count simply does not
    exist. An id *within* the row count always names a row -- the column is row order in
    this export -- so a miss means the row is a marker or a state-setting call rather
    than an action, and, worse, a hit proves nothing about intent: an id lifted from the
    PIX GUI of a multi-queue capture lands on an unrelated row and returns confident
    data for the wrong event. Naming the row we landed on is the only way the caller can
    notice, so the message quotes it. A queue qualifier is the one mechanism that can
    reject such a cross-queue id instead of answering it, which is why the qualified
    failure gets its own hint.
    """
    from ..errors import invalid_argument, not_found

    draw_index = args.get("draw_index")
    queue_id = args.get("queue_id")
    queue_name = args.get("queue_name")
    queue_object_id = args.get("queue_object_id")
    if draw_index is None and queue_id is None:
        raise invalid_argument(
            "draw_index/queue_id",
            "provide draw_index (addresses every action) or queue_id (exported queue only)",
        )

    draw = capture.resolve_draw(
        draw_index=draw_index,
        queue_id=queue_id,
        queue_name=queue_name,
        queue_object_id=queue_object_id,
    )
    if draw is not None:
        return draw

    qualifiers = [
        f"{key}={value!r}"
        for key, value in (
            ("queue_name", queue_name),
            ("queue_object_id", queue_object_id),
        )
        if value is not None
    ]

    if queue_id is not None:
        selector = f"queue_id={queue_id}"
        if qualifiers:
            # The id may well name a real action -- just not on the queue asked for.
            # Say that plainly, because the alternative is answering with the wrong
            # queue's data, which is exactly what the qualifier exists to prevent.
            raise not_found(
                what,
                f"{selector} on {', '.join(qualifiers)}",
                "The id resolved to a draw on a different queue, or to nothing at all. "
                "Run queue-attribution to see the queues and drop the queue restriction "
                "to find out which one the id belongs to.",
            )
        row = capture.event_by_queue_id(int(queue_id))
        if row is None:
            raise not_found(
                what,
                selector,
                f"The exported event list has {len(capture.events)} rows and none carries "
                f"this id. " + QUEUE_ID_IS_ROW_ORDER,
            )
        raise not_found(
            what,
            selector,
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

