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
            "Zero-based index into the draw call list (see list-draw-calls). This is the "
            "only selector that works for actions on a queue the exported event list "
            "does not cover, since those have no Queue ID."
        ),
    },
    "queue_id": {
        "type": "integer",
        "description": (
            "PIX GUI 'Queue ID' of the event. This is the single event identifier the "
            "toolkit accepts: it is present on every row of the PIX event list, so it "
            "also addresses pass markers. Global ID is reported in results but is not "
            "accepted as input."
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
            "PIX GUI 'Queue ID' of any row inside the pass, or of the pass marker "
            "itself. This is the id visible on every PIX event list row."
        ),
    },
}




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
    """Resolve a pass from a name, a pass index, or a PIX GUI Queue ID.

    Queue ID is the toolkit's single event identifier. The PIX GUI also shows a Global
    ID, but only for actions, so it cannot name a pass marker and cannot address every
    row the user can see. Accepting both meant two ways to say the same thing, with one
    of them silently unable to express half the cases; results still report Global ID
    for cross-referencing.

    An id wins over a name, because it is unambiguous while a name is a substring
    match that can hit several passes.
    """
    from ..errors import invalid_argument, not_found

    queue_id = args.get("queue_id")
    if queue_id is not None:
        entry = capture.find_pass_by_event(queue_id=queue_id)
        if entry is None:
            raise not_found(
                "pass",
                f"queue_id={queue_id}",
                "Use locate-event to check the id, or list-passes to browse passes.",
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
    """Resolve a draw call from a Queue ID or a draw index, or raise.

    Centralised so the "which event?" question has exactly one answer across the
    toolkit, and so the not-found error names the selector the caller actually used.

    ``queue_name`` / ``queue_object_id`` are optional qualifiers on top of that id.
    They are forwarded verbatim; when absent this behaves exactly as before, which
    is why every existing caller needed no change.
    """
    from ..errors import not_found

    queue_name = args.get("queue_name")
    queue_object_id = args.get("queue_object_id")
    draw = capture.resolve_draw(
        draw_index=args.get("draw_index"),
        queue_id=args.get("queue_id"),
        queue_name=queue_name,
        queue_object_id=queue_object_id,
    )
    if draw is None:
        selector = (
            f"queue_id={args['queue_id']}"
            if args.get("queue_id") is not None
            else f"draw_index={args.get('draw_index')}"
        )
        qualifiers = [
            f"{key}={value!r}"
            for key, value in (
                ("queue_name", queue_name),
                ("queue_object_id", queue_object_id),
            )
            if value is not None
        ]
        if qualifiers:
            selector = f"{selector} on {', '.join(qualifiers)}"
            hint = (
                "The id resolved to a draw on a different queue, or to nothing at all. "
                "Run queue-attribution to see the queues and drop the queue restriction "
                "to find out which one the id belongs to."
            )
        else:
            hint = "Use list-draw-calls to find a valid Queue ID or draw index."
        raise not_found(what, selector, hint)
    return draw


def note_missing_queue_id(result, draw) -> bool:
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

    Returns whether a diagnostic was added, so a caller can decide what else to
    say without re-testing the condition.
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
        "warning",
        f"No Queue ID for this action (global_id={draw.global_id}). It ran on {where}, "
        "and the exported event list does not cover that queue, so PIX never wrote a "
        "Queue ID for it. The id cannot be derived: Queue ID is not a per-queue call "
        "count, and a synthesised one would address a different row. Bindings above "
        f"come from the C++ export and are unaffected -- select this action with "
        f"draw_index={draw.index}.",
    )
    return True


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
    """
    return {
        "queue_id": entry.get("first_queue_id"),
        "marker_queue_id": entry.get("marker_queue_id"),
        "first_queue_id": entry.get("first_queue_id"),
        "last_queue_id": entry.get("last_queue_id"),
        "first_global_id": entry.get("first_global_id"),
    }
