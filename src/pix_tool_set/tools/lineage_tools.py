"""Requirement section: cross-pass resource contract (gap two).

``trace-resource-lineage`` joins what the existing tools report separately --
write events, read events, state transitions -- into one chain with verdicts.
The default payload is the *suspicious-points list* (assertions whose verdict is
not ``pass``), not the full event set: an agent's context budget is the hard
constraint this tool is designed around.
"""

from __future__ import annotations

from typing import Any

from ..context import ToolContext
from ..engine import lineage
from ..errors import invalid_argument, not_found
from ..results import ToolResult
from ._common import PAGE_PARAMS, page_args, page_envelope, resolve_pass, tool, with_session

_BINDING_KINDS = {
    "srv": "srv_read",
    "uav": "uav_write",
    "cbv": "cbv_read",
    "rtv": "rtv_write",
    "dsv": "dsv_write",
}

_NOTE = (
    "This tool synthesises producers, consumers and state transitions into one "
    "assertion-carrying chain; it parses nothing new. Two traps are built in rather "
    "than left to the caller: (1) every next_action that reads a pass output names "
    "the first event AFTER the write (replay sampling reads pre-event state), and "
    "(2) value-reading actions are labelled resources_bin vs gpu_replay, with "
    "depth-class resources flagged as analytic-gradient risk that find-depth-content "
    "must resolve first. By default only assertions whose verdict is not 'pass' are "
    "returned; pass --include-passing for the full set. The resource history is "
    "paged; the assertions are the primary output, not the event list."
)


def _resolve_resource(capture, args: dict[str, Any]) -> int:
    """Locate the resource id, either directly or via pass + binding."""
    resource_id = args.get("resource_id")
    if resource_id is not None:
        return int(resource_id)

    pass_name = args.get("pass_name")
    binding = str(args.get("binding") or "").strip().lower()
    if pass_name is None or binding not in _BINDING_KINDS:
        raise invalid_argument(
            "resource_id/pass_name+binding",
            "provide resource_id, or pass_name together with binding "
            f"(one of {sorted(_BINDING_KINDS)})",
        )

    entry = resolve_pass(capture, {"pass_name": pass_name})
    marker_path = tuple(entry["marker_path"])
    draws = [d for d in capture.draw_calls if d.marker_path == marker_path]
    if not draws:
        raise not_found(
            "resource",
            f"pass {pass_name!r}",
            "The pass has no draw calls, so no binding can be read off it.",
        )
    wanted = _BINDING_KINDS[binding]
    for draw in draws:
        if wanted == "rtv_write" and draw.render_target_resource_ids:
            return draw.render_target_resource_ids[0]
        if wanted == "dsv_write" and draw.depth_stencil_resource_id is not None:
            return draw.depth_stencil_resource_id
        if wanted == "srv_read":
            for view in draw.srvs:
                if view.resource_id is not None:
                    return view.resource_id
        if wanted == "uav_write":
            for view in draw.uavs:
                if view.resource_id is not None:
                    return view.resource_id
        if wanted == "cbv_read":
            for view in draw.cbvs:
                if view.resource_id is not None:
                    return view.resource_id
    raise not_found(
        "resource",
        f"binding {binding!r} in pass {pass_name!r}",
        "No draw in the pass binds a resource of that kind.",
    )


@tool(
    name="trace-resource-lineage",
    summary=(
        "Production-consumption chain of one resource across the frame: every write "
        "event (RTV/DSV/UAV/clear/copy-dest), every read event (SRV/CBV/copy-source), "
        "the state timeline, and an assertion list with verdicts -- read-before-write, "
        "missing transitions or UAV barriers, subresource mismatches, format "
        "reinterpretation, cross-queue hazards and state gaps."
    ),
    category="resources",
    parameters=with_session(
        PAGE_PARAMS,
        resource_id={"type": "integer", "description": "Resource id to trace."},
        pass_name={
            "type": "string",
            "description": (
                "Alternative to resource_id: resolve the resource from this pass's "
                "bindings. Use together with --binding."
            ),
        },
        binding={
            "type": "string",
            "enum": sorted(_BINDING_KINDS),
            "description": (
                "Which binding of the pass names the resource: srv/uav/cbv read from "
                "the draw bindings, rtv/dsv from the output merger."
            ),
        },
        include_passing={
            "type": "boolean",
            "description": (
                "Return passing assertions too. Default false: only verdicts other "
                "than 'pass' are returned, because the assertions are the primary "
                "output, not the event list."
            ),
        },
    ),
    returns=(
        "resource facts, paged producers/consumers/state edges, and the assertion "
        "list. Each assertion carries id, verdict (pass/fail/unknown), evidence and "
        "a ready-to-run next_action with its data source and, for readbacks, the "
        "corrected sampling point."
    ),
    examples=[
        "pix-tool-set trace-resource-lineage --resource-id 1985",
        "pix-tool-set trace-resource-lineage --pass-name BasePass --binding srv",
        "pix-tool-set trace-resource-lineage --resource-id 1985 --include-passing",
    ],
    notes=_NOTE,
)
def trace_resource_lineage(args: dict[str, Any], context: ToolContext) -> ToolResult:
    capture = context.capture(args)
    resource_id = _resolve_resource(capture, args)
    if capture.resource(resource_id) is None:
        raise not_found("resource", resource_id, "Run list-resources to find valid ids.")

    result = lineage.build_lineage(capture, resource_id)
    offset, limit = page_args(args)

    def page(rows: list[dict[str, Any]]) -> dict[str, Any]:
        window = rows[offset : offset + limit] if limit else rows[offset:]
        return {
            "entries": window,
            **page_envelope(len(rows), offset, limit, len(window)),
        }

    include_passing = bool(args.get("include_passing"))
    assertions = result["assertions"]
    suspicious = [
        a for a in assertions if a["verdict"] != "pass" or include_passing
    ]

    payload = {
        "resource": result["resource"],
        "resource_id": resource_id,
        "depth_class": result["depth_class"],
        "producers": page(result["producers"]),
        "consumers": page(result["consumers"]),
        "state_edges": page(result["state_edges"]),
        "assertions": suspicious,
        "assertion_total": len(assertions),
        "assertions_returned": len(suspicious),
        "verdict_summary": result["verdict_summary"],
        "note": (
            "The assertions are the primary output; producers/consumers/state_edges "
            "are paged context for them. Default mode returns only verdicts other "
            "than 'pass'."
        ),
    }

    response = ToolResult.success(payload)
    if result["verdict_summary"].get("fail"):
        response.degrade(
            f"{result['verdict_summary']['fail']} assertion(s) failed for resource "
            f"{resource_id}; inspect each assertion's next_action for the follow-up "
            "command.",
            reason="lineage assertions failed",
        )
    if result["depth_class"]:
        response.add_diagnostic(
            "warning",
            "This is a depth-class resource: a resources.bin readback decodes as an "
            "analytic gradient, not the stored depth. Run find-depth-content before "
            "trusting any readback of it.",
        )
    return response
