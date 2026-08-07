"""Requirement section 2: event and action navigation."""

from __future__ import annotations

from typing import Any

from ..context import ToolContext
from ..engine.model import DRAW_KINDS, EventKind
from ..errors import invalid_argument, not_found
from ..results import ToolResult
from ._common import (
    DRAW_SELECTOR,
    PAGE_PARAMS,
    note_missing_queue_id,
    page_args,
    page_envelope,
    pass_identity,
    resolve_draw,
    resolve_pass,
    tool,
    with_session,
)


_KIND_VALUES = [kind.value for kind in EventKind]


@tool(
    name="list-actions",
    summary=(
        "List capture events (PIX 'actions') in submission order, optionally filtered by "
        "kind, marker path, or a global-id range."
    ),
    category="events",
    parameters=with_session(
        PAGE_PARAMS,
        kind={
            "type": "string",
            "enum": _KIND_VALUES,
            "description": "Keep only events of this kind.",
        },
        drawable_only={
            "type": "boolean",
            "description": "Keep only draw/dispatch/indirect events.",
        },
        marker={
            "type": "string",
            "description": "Substring match against the marker path of the event.",
        },
        min_queue_id={"type": "integer", "description": "Lowest PIX Queue ID to include."},
        max_queue_id={"type": "integer", "description": "Highest PIX Queue ID to include."},
        detail={"type": "boolean", "description": "Include marker path and counters."},
    ),
    returns="Paged list of events with kind, name, Queue ID, Global ID and depth.",
    examples=[
        "pix-tool-set list-actions --limit 20",
        "pix-tool-set list-actions --drawable-only --limit 50",
        "pix-tool-set list-actions --kind dispatch --detail",
    ],
)
def list_actions(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    offset, limit = page_args(args)
    detail = bool(args.get("detail"))

    events = capture.events
    if not events:
        return ToolResult.partial(
            {"events": [], **page_envelope(0, offset, limit, 0)}
        ).add_diagnostic(
            "warning",
            "This session has no event list; re-open the capture without --skip-events.",
        )

    kind = args.get("kind")
    marker = (args.get("marker") or "").lower()
    lo = args.get("min_queue_id")
    hi = args.get("max_queue_id")
    drawable_only = bool(args.get("drawable_only"))

    filtered = []
    for event in events:
        if kind and event.kind.value != kind:
            continue
        if drawable_only and event.kind not in DRAW_KINDS:
            continue
        # Range filtering on Queue ID rather than Global ID: every row has a Queue ID,
        # so a window expressed this way cannot silently drop markers.
        if lo is not None and event.queue_id < lo:
            continue
        if hi is not None and event.queue_id > hi:
            continue
        if marker and marker not in " / ".join(event.marker_path).lower():
            continue
        filtered.append(event)

    window = filtered[offset : offset + limit] if limit else filtered[offset:]
    return ToolResult.success(
        {
            "events": [event.to_dict(detail=detail) for event in window],
            **page_envelope(len(filtered), offset, limit, len(window)),
        }
    )


@tool(
    name="action-info",
    summary=(
        "Full detail for one event: kind, marker path, parent/child links, counters, and "
        "the associated draw call when the event is a draw or dispatch."
    ),
    category="events",
    parameters=with_session(
        queue_id={
            "type": "integer",
            "description": (
                "Row id from the exported event list; it addresses markers as well as "
                "actions, but only for the queue that export covers. For an action on "
                "another queue use locate-event --draw-index instead."
            ),
        },

        include_children={
            "type": "boolean",
            "description": "Include the immediate child events.",
        },
    ),
    returns="Event detail, ancestor chain, and linked draw call summary.",
    examples=["pix-tool-set action-info --queue-id 18704"],
)
def action_info(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    queue_id = args.get("queue_id")
    if queue_id is None:
        raise invalid_argument("queue_id", "provide the event's PIX Queue ID")

    event = capture.event_by_queue_id(int(queue_id))
    if event is None:
        raise not_found(
            "action",
            f"queue_id={queue_id}",
            "Use list-actions or search-actions to find a valid Queue ID.",
        )

    ancestors = []
    node = event.parent
    while node is not None:
        ancestors.append(node.to_dict())
        node = node.parent
    ancestors.reverse()

    data: dict[str, Any] = {
        "event": event.to_dict(detail=True),
        "ancestors": ancestors,
        "draw_call": None,
    }
    if bool(args.get("include_children")):
        data["children"] = [child.to_dict() for child in event.children]

    draw = event.draw_call
    if draw is not None:
        data["draw_call"] = draw.to_dict()
    return ToolResult.success(data)


@tool(
    name="search-actions",
    summary="Search events by name using a substring or a regular expression.",
    category="events",
    parameters=with_session(
        PAGE_PARAMS,
        query={"type": "string", "description": "Text or regex to match against event names."},
        regex={"type": "boolean", "description": "Treat query as a regular expression."},
        kind={"type": "string", "enum": _KIND_VALUES, "description": "Restrict to one kind."},
        detail={"type": "boolean", "description": "Include marker path and counters."},
        required=["query"],
    ),
    returns="Paged list of matching events.",
    examples=[
        "pix-tool-set search-actions --query Lumen",
        'pix-tool-set search-actions --query "Draw\\w+Instanced" --regex',
    ],
)
def search_actions(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    offset, limit = page_args(args)
    window, total = capture.find_events(
        args["query"],
        kind=args.get("kind"),
        regex=bool(args.get("regex")),
        offset=offset,
        limit=limit,
    )
    detail = bool(args.get("detail"))
    result = ToolResult.success(
        {
            "query": args["query"],
            "regex": bool(args.get("regex")),
            "events": [event.to_dict(detail=detail) for event in window],
            **page_envelope(total, offset, limit, len(window)),
        }
    )
    if not capture.events:
        result.degrade("This session has no event list, so the search had nothing to scan.")
    return result


@tool(
    name="find-draw-calls",
    summary=(
        "Find draw calls / dispatches by pass name, PSO, shader hash, resource usage or "
        "size thresholds. This is the main entry point for 'which draws do X'."
    ),
    category="events",
    parameters=with_session(
        PAGE_PARAMS,
        pass_name={"type": "string", "description": "Substring match on the innermost marker."},
        queue_id={
            "type": "integer",
            "description": (
                "Exported event list row id of any event inside a pass. Restricts the "
                "search to that one pass, which a name cannot do when passes share a "
                "label. Only works for the exported queue; pass_name or marker reach "
                "every pass."
            ),
        },

        marker={"type": "string", "description": "Substring match on the full marker path."},
        kind={
            "type": "string",
            "enum": ["draw", "dispatch", "dispatch_rays", "execute_indirect"],
            "description": "Restrict to one draw kind.",
        },
        pso_id={"type": "integer", "description": "Only draws using this pipeline state."},
        uses_resource={"type": "integer", "description": "Only draws touching this resource id."},
        shader_hash={"type": "string", "description": "Match a shader hash or PDB debug name."},
        min_instances={"type": "integer", "description": "Minimum instance count."},
        min_triangles={"type": "integer", "description": "Minimum triangle count."},
        detail={"type": "boolean", "description": "Include full bound state per draw."},
    ),
    returns="Paged list of draw calls with counts of bound resources.",
    examples=[
        "pix-tool-set find-draw-calls --pass-name Lumen --limit 10",
        "pix-tool-set find-draw-calls --queue-id 18704",
        "pix-tool-set find-draw-calls --kind dispatch --min-triangles 0",
        "pix-tool-set find-draw-calls --uses-resource 641 --detail",
    ],
    aliases=["search-draw-calls"],
)
def find_draw_calls(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    offset, limit = page_args(args)

    if args.get("queue_id") is not None:
        # Resolve the id to one pass, then apply the remaining filters within it.
        # Filtering by the pass's name would widen the result again, because names
        # are not unique across the frame.
        wanted = tuple(resolve_pass(capture, args)["marker_path"])
        candidates, _ = capture.find_draw_calls(
            kind=args.get("kind"),
            pso_id=args.get("pso_id"),
            uses_resource=args.get("uses_resource"),
            shader_hash=args.get("shader_hash"),
            min_instances=args.get("min_instances"),
            min_triangles=args.get("min_triangles"),
        )
        matched = [d for d in candidates if d.marker_path == wanted]
        total = len(matched)
        window = matched[offset : offset + limit] if limit else matched[offset:]
    else:
        window, total = capture.find_draw_calls(
            pass_name=args.get("pass_name"),
            marker=args.get("marker"),
            kind=args.get("kind"),
            pso_id=args.get("pso_id"),
            uses_resource=args.get("uses_resource"),
            shader_hash=args.get("shader_hash"),
            min_instances=args.get("min_instances"),
            min_triangles=args.get("min_triangles"),
            offset=offset,
            limit=limit,
        )
    detail = bool(args.get("detail"))
    return ToolResult.success(
        {
            "draw_calls": [draw.to_dict(detail=detail) for draw in window],
            **page_envelope(total, offset, limit, len(window)),
        }
    )


@tool(
    name="locate-event",
    summary=(
        "Locate an event in the frame: its ordinal position, marker path, containing pass, "
        "neighbouring draws, and the exact generated C++ source line."
    ),
    category="events",
    parameters=with_session(
        DRAW_SELECTOR,
        pass_name={
            "type": "string",
            "description": (
                "Pass name as shown in the PIX GUI marker tree (substring match). Resolves "
                "to the pass's first action, so a name read off the screen is enough to "
                "start from even when the pass has no event list row."
            ),
        },
        neighbours={
            "type": "integer",
            "description": "How many draws before and after to include. Default 3.",
        },
    ),
    returns="Position within the frame plus surrounding context.",
    examples=[
        "pix-tool-set locate-event --queue-id 18461",
        "pix-tool-set locate-event --draw-index 2461 --neighbours 5",
        'pix-tool-set locate-event --pass-name "CompactTraces WaveOps"',
    ],
)
def locate_event(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)

    # A pass name is what the user can actually read off the PIX GUI, and for a pass on
    # an unexported queue it is the only thing they have: there is no Queue ID to copy and
    # the draw index is precisely what they are trying to find out. Resolving it here
    # closes that loop in one call instead of sending them through find-pass first.
    matched_pass = None
    if args.get("pass_name") and args.get("draw_index") is None and args.get("queue_id") is None:
        needle = str(args["pass_name"]).lower()
        candidates = [p for p in capture.passes if needle in p["name"].lower()]
        if not candidates:
            raise not_found(
                "pass",
                args["pass_name"],
                "Run list-passes or find-pass to browse the marker tree.",
            )
        matched_pass = candidates[0]
        args = {**args, "draw_index": matched_pass["first_draw_index"]}

    draw = capture.resolve_draw(
        draw_index=args.get("draw_index"),
        queue_id=args.get("queue_id"),
    )
    if draw is None:
        # The id may name a marker rather than an action. Markers carry no Global ID at
        # all, so a marker's Queue ID is the only way to name the pass row itself;
        # answer with the marker and the pass it opens.
        event = capture.resolve_event(queue_id=args.get("queue_id"))
        if event is not None:
            pass_entry = capture.find_pass_by_event(queue_id=event.queue_id)
            result = ToolResult.success(
                {
                    "is_action": False,
                    "event": event.to_dict(),
                    "pass": (
                        {
                            "pass_index": pass_entry["pass_index"],
                            "name": pass_entry["name"],
                            **pass_identity(pass_entry),
                            "marker_path": pass_entry["marker_path"],
                            "first_draw_index": pass_entry["first_draw_index"],
                        }
                        if pass_entry
                        else None
                    ),
                }
            )
            result.add_diagnostic(
                "info",
                "This id names a marker, not a draw. Use the pass's first_draw_index "
                "for draw-level tools.",
            )
            return result
        # Delegate the miss so the two Queue ID failure modes are described in one place;
        # a bare "not found" here would hide the row-order trap that makes a wrong id look
        # like a right one elsewhere.
        resolve_draw(capture, args, what="event")

    span = int(args.get("neighbours") or 3)
    lo = max(draw.index - span, 0)
    hi = min(draw.index + span + 1, len(capture.draw_calls))
    pass_entry = next(
        (p for p in capture.passes if tuple(p["marker_path"]) == draw.marker_path), None
    )

    total_draws = len(capture.draw_calls)
    data = {
        "draw_index": draw.index,
        "global_id": draw.global_id,
        "api": draw.api,
        "kind": draw.kind.value,
        "queue_id": draw.queue_id,
        "position": {
            "draw_index": draw.index,
            "total_draw_calls": total_draws,
            "percent_through_frame": round(100.0 * draw.index / total_draws, 2)
            if total_draws
            else 0.0,
        },
        "marker_path": list(draw.marker_path),
        "pass": pass_entry,
        "source": {"file": draw.source_file, "line": draw.source_line},
        "command_list_id": draw.command_list_id,
        "neighbours": [
            {
                "draw_index": other.index,
                "global_id": other.global_id,
                "api": other.api,
                "pass_name": other.pass_name,
                "is_target": other.index == draw.index,
            }
            for other in capture.draw_calls[lo:hi]
        ],
    }
    event = draw.event
    if event is not None:
        data["event"] = event.to_dict(detail=True)
    result = ToolResult.success(data)
    if matched_pass is not None:
        result.add_diagnostic(
            "info",
            f"Matched pass {matched_pass['name']!r} (pass_index="
            f"{matched_pass['pass_index']}) and located its first action; the pass spans "
            f"draw indices {matched_pass['first_draw_index']}..{matched_pass['last_draw_index']}.",
            pass_index=matched_pass["pass_index"],
        )
    note_missing_queue_id(result, draw, level="info")
    return result

