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
        "description": "Zero-based index into the draw call list (see list-draw-calls).",
    },
    "global_id": {
        "type": "integer",
        "description": "PIX GUI 'Global ID' of the event. Preferred when you have it.",
    },
    "queue_id": {
        "type": "integer",
        "description": (
            "PIX GUI 'Queue ID' of the event. Present on every row of the PIX event "
            "list, unlike Global ID, so it also addresses pass markers."
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
