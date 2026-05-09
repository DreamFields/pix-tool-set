"""Resource lineage: the production-consumption contract of one resource.

Everything here is synthesis, not parsing. The producers and consumers are read
off ``capture.draw_calls`` (RTV/DSV/UAV writes, SRV/CBV reads) and
``capture.resource_events`` (clears, copies, barriers); the state timeline is
reused verbatim from ``resourceevents.state_timeline``. What this module adds is
the *assertions* that join them into one chain, and the two built-in traps that
the README previously left to the agent's memory:

  1. Sampling-point correction: any ``next_action`` that reads a pass output
     names the first event AFTER the write, never the write itself -- replay
     sampling reads the state before an event executes, so sampling the writer
     yields the pre-write content, which is the correct answer to the wrong
     question.
  2. Data-source labelling: every value-reading action is tagged
     ``resources_bin`` (frame-init contents) or ``gpu_replay`` (true post-write
     values), and depth-class resources are flagged as ``analytic_gradient``
     risk, steering the caller to ``find-depth-content`` first.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from . import resourceevents
from .model import EventKind


# Access classes: which side of the contract an event is on.
_WRITE_ACCESSES = {"rtv_write", "dsv_write", "uav_write", "clear", "copy_dest"}
_READ_ACCESSES = {"srv_read", "cbv_read", "copy_source"}

# Depth-class resources decode to an analytic gradient, not the pixel value, when
# read back from resources.bin -- see find-depth-content.
_DEPTH_FORMAT_MARKERS = ("D16", "D24", "D32")


def _is_depth_class(resource: Any) -> bool:
    """Whether a readback of this resource is an analytic-gradient risk.

    The resource's own ALLOW_DEPTH_STENCIL flag is the primary signal, because
    typeless depth formats (R32G8X24_TYPELESS, R24G8_TYPELESS) carry no D marker
    in their name. The format markers are the fallback for captures whose
    resource flags were not exported.
    """
    if resource is not None and getattr(resource, "is_depth_stencil", False):
        return True
    fmt = (resource.format if resource is not None else "") or ""
    return any(marker in fmt.upper() for marker in _DEPTH_FORMAT_MARKERS)


def _format_family(fmt: str) -> str:
    """Channel part of a DXGI format, for same-family reinterpret checks."""
    name = (fmt or "").replace("DXGI_FORMAT_", "").upper()
    base = name.split("_")[0]
    # R32G32B32A32 and R32G32 stay one family through their common prefix.
    if base == "R32G32B32A32":
        return "R32G32B32A32"
    return name


def _is_same_family(producer_fmt: str, consumer_fmt: str) -> bool:
    """Whether a typed view reinterpretation stays inside the typeless family.

    A TYPELESS resource carries no interpretation; the RTV/SRV formats must
    agree on the channel bit layout. Comparing the bit-width sequence covers
    the legal depth reinterpret (R32G8X24_TYPELESS read as
    R32_FLOAT_X8X24_TYPELESS) without special cases: both decode to
    [32, 8, 24], while R32G32B32A32 vs R32G32_FLOAT decode differently.
    """
    producer_bits = re.findall(r"[RGBAXD](\d+)", (producer_fmt or "").replace("_TYPELESS", ""))
    consumer_bits = re.findall(r"[RGBAXD](\d+)", (consumer_fmt or "").upper())
    return bool(producer_bits) and producer_bits == consumer_bits


def _order_key(entry: dict[str, Any]) -> tuple:
    """Frame order: Global ID first; events without one sort last as uncertain."""
    gid = entry.get("global_id")
    return (gid is None, gid if gid is not None else 1 << 62)


def _resource_for(capture, resource_id: int) -> Any:
    return capture.resource(resource_id)


def _draw_touches(capture, resource_id: int) -> list[dict[str, Any]]:
    """Every draw that reads or writes the resource, with the access spelled out."""
    touches: list[dict[str, Any]] = []
    for draw in capture.draw_calls:
        entries: list[dict[str, Any]] = []
        for slot, rid in enumerate(draw.render_target_resource_ids):
            if rid == resource_id:
                entries.append({"access": "rtv_write", "slot": slot})
        if draw.depth_stencil_resource_id == resource_id:
            entries.append({"access": "dsv_write"})
        for view in draw.uavs:
            if view.resource_id == resource_id:
                entries.append(
                    {
                        "access": "uav_write",
                        "mip_slice": view.mip_slice,
                        "array_slice": view.array_slice,
                    }
                )
        for view in draw.srvs:
            if view.resource_id == resource_id:
                entries.append(
                    {
                        "access": "srv_read",
                        "mip_slice": view.mip_slice,
                        "array_slice": view.array_slice,
                    }
                )
        if any(view.resource_id == resource_id for view in draw.cbvs):
            entries.append({"access": "cbv_read"})
        if not entries:
            continue
        for entry in entries:
            touches.append(
                {
                    "global_id": draw.global_id,
                    "draw_index": draw.index,
                    "kind": entry["access"],
                    "api": draw.api,
                    "pass_name": draw.pass_name,
                    "queue_object_id": draw.queue_object_id,
                    "queue_name": draw.queue_name,
                    "slot": entry.get("slot"),
                    "mip_slice": entry.get("mip_slice"),
                    "array_slice": entry.get("array_slice"),
                }
            )
    return touches


def _event_touches(capture, resource_id: int) -> list[dict[str, Any]]:
    """Non-draw events (clear / copy / barrier) that name the resource."""
    touches: list[dict[str, Any]] = []
    for event in capture.resource_events:
        touch = event.touch_for(resource_id)
        if touch is None:
            continue
        if touch.access not in (
            "write",
            "copy_dest",
            "copy_source",
            "uav_barrier",
            "state_transition",
            "discard",
        ):
            continue
        touches.append(
            {
                "global_id": event.global_id,
                "draw_index": None,
                "kind": touch.access,
                "api": event.api,
                "pass_name": event.marker_path[-1] if event.marker_path else "",
                "queue_object_id": None,
                "queue_name": "",
                "state_before": touch.state_before,
                "state_after": touch.state_after,
                "clear_value": event.clear_value,
                "source": f"{event.source_file}:{event.source_line}",
            }
        )
    return touches


def _sampling_action(capture, after_touch: dict[str, Any]) -> dict[str, Any]:
    """The replay command that reads the resource right AFTER a write.

    Trap one, built in: replay sampling captures the state before an event
    executes, so the draw that writes a render target is the wrong place to read
    it -- the read must address the next event. ``sampling_point: after_write``
    says why, so the caller does not re-learn the trap from the README. When the
    write is the frame's last event there is no later event to sample; that is
    reported honestly rather than clamped back onto the write itself.
    """
    draw_index = after_touch.get("draw_index")
    next_index = None
    if draw_index is not None and draw_index + 1 < len(capture.draw_calls):
        next_index = draw_index + 1
    if next_index is None:
        return {
            "tool": "read-replay-target",
            "draw_index": None,
            "sampling_point": "after_write",
            "reason": (
                "The write is the last event in the frame, so no later event exists "
                "to sample the written value; sample at frame end or against a "
                "replay dump instead."
            ),
        }
    return {
        "tool": "read-replay-target",
        "draw_index": next_index,
        "sampling_point": "after_write",
        "reason": (
            "Replay sampling reads the state before an event executes; sampling the "
            "write event itself returns the pre-write contents. The next event is "
            "the first one that sees the written value."
        ),
    }


def _assertion(
    ident: str,
    verdict: str,
    evidence: list[Any],
    next_action: dict[str, Any] | None = None,
    *,
    source: str,
    note: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": ident,
        "verdict": verdict,
        "evidence": evidence,
    }
    if note:
        payload["note"] = note
    if next_action is not None:
        payload["next_action"] = next_action
    payload["source"] = source
    return payload


def _is_present_target(capture, resource_id: int) -> bool:
    """True when the resource is a swapchain buffer handed to Present.

    A present target has no reader inside the frame by design, so the lineage
    assertions need to tell it apart from genuinely dead writes. The export names
    swapchain buffers explicitly, which is a statement rather than an inference.
    """
    resource = _resource_for(capture, resource_id)
    if resource is None:
        return False
    name = str(getattr(resource, "name", "") or "").lower()
    return "backbuffer" in name or "swapchain" in name


def build_lineage(capture, resource_id: int) -> dict[str, Any]:
    """Assemble producers, consumers, state edges and assertions for one resource."""
    resource = _resource_for(capture, resource_id)
    fmt = resource.format if resource is not None else ""
    depth_class = _is_depth_class(resource)

    draw_touches = _draw_touches(capture, resource_id)
    event_touches = _event_touches(capture, resource_id)
    timeline = resourceevents.state_timeline(capture.resource_events, resource_id)

    producers = [t for t in draw_touches if t["kind"] in _WRITE_ACCESSES]
    consumers = [t for t in draw_touches if t["kind"] in _READ_ACCESSES]
    event_rows = [t for t in event_touches if t["kind"] in _WRITE_ACCESSES | _READ_ACCESSES | {"uav_barrier", "discard"}]
    producers += [t for t in event_rows if t["kind"] in _WRITE_ACCESSES]
    consumers += [t for t in event_rows if t["kind"] in _READ_ACCESSES]
    producers.sort(key=_order_key)
    consumers.sort(key=_order_key)

    assertions: list[dict[str, Any]] = []
    ordered = any(t.get("global_id") is not None for t in producers + consumers)

    def between(a: dict[str, Any], b: dict[str, Any]) -> list[Any]:
        lo, hi = sorted([_order_key(a), _order_key(b)])
        return [
            t for t in event_touches
            if lo <= _order_key(t) <= hi and t is not a and t is not b
        ]

    # 1. read_before_write: a consumer before any producer reads garbage.
    if producers and consumers and ordered:
        first_producer = producers[0]
        early = [
            c for c in consumers
            if _order_key(c) < _order_key(first_producer)
        ]
        if early:
            assertions.append(
                _assertion(
                    "read_before_write",
                    "fail",
                    [
                        {
                            "draw_index": c.get("draw_index"),
                            "global_id": c.get("global_id"),
                            "pass_name": c.get("pass_name"),
                        }
                        for c in early[:5]
                    ],
                    _sampling_action(capture, first_producer),
                    source="resources_bin",
                )
            )
        else:
            assertions.append(
                _assertion("read_before_write", "pass", [], source="gpu_replay")
            )
    else:
        assertions.append(
            _assertion(
                "read_before_write",
                "unknown" if ordered else "pass",
                [] if ordered else ["No ordered events to compare."],
                source="gpu_replay",
            )
        )

    # 2. missing_transition: a write -> read pair needs a barrier in between.
    if producers and consumers and ordered:
        first_producer = producers[0]
        first_consumer = consumers[0]
        if _order_key(first_consumer) < _order_key(first_producer):
            # Already covered by read_before_write; no meaningful pair.
            assertions.append(
                _assertion(
                    "missing_transition",
                    "unknown",
                    ["First consumer precedes the first producer; see read_before_write."],
                    source="gpu_replay",
                )
            )
        else:
            transitions = [
                t for t in between(first_producer, first_consumer)
                if t["kind"] == "state_transition"
            ]
            needed = ""
            if first_consumer["kind"] == "srv_read":
                needed = "SHADER_RESOURCE"
            elif first_consumer["kind"] == "uav_write":
                needed = "UNORDERED_ACCESS"
            elif first_consumer["kind"] == "copy_source":
                needed = "COPY_SOURCE"
            if not needed:
                verdict, note = "pass", "Consumer needs no specific state transition."
            elif not transitions:
                verdict, note = (
                    "unknown",
                    "No transition barrier between the first write and first read; the "
                    "initial state may already be correct, which the export alone cannot prove.",
                )
            elif any(needed in t["state_after"] for t in transitions):
                verdict, note = "pass", f"Transition into {needed} found."
            else:
                verdict, note = (
                    "fail",
                    f"Transitions exist but none moves the resource into {needed}.",
                )
            assertions.append(
                _assertion(
                    "missing_transition",
                    verdict,
                    [
                        {"count": len(transitions)},
                        {"note": note},
                    ],
                    _sampling_action(capture, first_consumer),
                    source="gpu_replay",
                )
            )
    else:
        assertions.append(
            _assertion("missing_transition", "pass", [], source="gpu_replay")
        )

    # 3. missing_uav_barrier: two UAV writes without a barrier between them race.
    uav_writes = [t for t in producers if t["kind"] == "uav_write"]
    hazard_pairs = []
    for first, second in zip(uav_writes, uav_writes[1:]):
        if first.get("global_id") is None or second.get("global_id") is None:
            continue
        barriers = [
            t for t in event_touches
            if t["kind"] == "uav_barrier"
            and _order_key(first) < _order_key(t) < _order_key(second)
        ]
        if not barriers:
            hazard_pairs.append(
                {
                    "write_a": {"draw_index": first.get("draw_index"), "global_id": first.get("global_id")},
                    "write_b": {"draw_index": second.get("draw_index"), "global_id": second.get("global_id")},
                }
            )
    assertions.append(
        _assertion(
            "missing_uav_barrier",
            "fail" if hazard_pairs else "pass",
            hazard_pairs[:8],
            _sampling_action(capture, uav_writes[-1]) if hazard_pairs else None,
            source="gpu_replay",
        )
    )

    # 4. subresource_mismatch: producer wrote a different mip/slice than the
    #    consumer reads. Judged only where both sides recorded a selector.
    mismatches = []
    for producer in producers:
        for consumer in consumers:
            write_mip = producer.get("mip_slice")
            read_mip = consumer.get("mip_slice")
            if write_mip is None or read_mip is None:
                continue
            if write_mip != read_mip:
                mismatches.append(
                    {
                        "writer": {"draw_index": producer.get("draw_index"), "mip": write_mip},
                        "reader": {"draw_index": consumer.get("draw_index"), "mip": read_mip},
                    }
                )
    assertions.append(
        _assertion(
            "subresource_mismatch",
            "fail" if mismatches else "pass",
            mismatches[:8],
            _sampling_action(capture, mismatches[0].get("reader", producers[0])) if mismatches else None,
            source="gpu_replay",
        )
    )

    # 5. format_reinterpret: RTV/SRV formats must stay inside the typeless family.
    reinterpret = []
    if depth_class:
        reinterpret.append(
            {
                "format": fmt,
                "risk": "analytic_gradient",
                "reason": (
                    "Depth-class resources decode as an analytic gradient when read from "
                    "resources.bin, not as the stored depth. Run find-depth-content first; "
                    "do not trust a straight readback of this resource."
                ),
            }
        )
    if "TYPELESS" in fmt and producers and consumers:
        formats = {
            view.format
            for view in capture.views.values()
            if view.resource_id == resource_id and view.format
        }
        for view_format in sorted(formats):
            if not _is_same_family(fmt, view_format):
                reinterpret.append(
                    {
                        "view_format": view_format,
                        "resource_format": fmt,
                        "verdict": "outside_family",
                    }
                )
                break
    assertions.append(
        _assertion(
            "format_reinterpret",
            "fail" if any(r.get("verdict") == "outside_family" for r in reinterpret) else "pass",
            reinterpret[:8],
            None,
            source="resources_bin",
        )
    )

    # 6. write_never_read: produced but never consumed.
    #
    #    A present target is the one legitimate exception: the back buffer is
    #    written and then handed to Present, so nothing in the frame ever reads
    #    it. Reporting that as a failure would fire on every capture and train the
    #    caller to ignore the assertion, so a swapchain target is a pass with the
    #    reason stated instead.
    is_present_target = _is_present_target(capture, resource_id)
    unread = bool(producers) and not consumers
    if unread and is_present_target:
        verdict_unread = "pass"
        note_unread = (
            "Nothing reads this resource, which is correct: it is a swapchain / "
            "present target, so the frame writes it and Present consumes it."
        )
    elif unread:
        verdict_unread = "fail"
        note_unread = (
            "The resource is written but never read in this frame. Either the "
            "consumer is in another frame, or the work is dead."
        )
    else:
        verdict_unread = "pass"
        note_unread = "The resource is both written and read."
    assertions.append(
        _assertion(
            "write_never_read",
            verdict_unread,
            [
                {
                    "producer_count": len(producers),
                    "first_producer": {
                        "draw_index": producers[0].get("draw_index"),
                        "pass_name": producers[0].get("pass_name"),
                    },
                    "present_target": is_present_target,
                }
            ]
            if unread
            else [],
            None,
            source="gpu_replay",
            note=note_unread,
        )
    )

    # 7. cross_queue_hazard: producer and consumer on different queues. The
    #    export carries no fence/Wait evidence, so the verdict is unknown rather
    #    than fail -- cross-queue use with a fence is the UE5 norm for depth and
    #    HZB resources, and this toolkit never reports an inference as a fact.
    queues = {
        t.get("queue_object_id")
        for t in producers + consumers
        if t.get("queue_object_id") is not None
    }
    assertions.append(
        _assertion(
            "cross_queue_hazard",
            "unknown" if len(queues) > 1 else "pass",
            (
                [
                    {
                        "queue_object_ids": sorted(queues),
                        "note": (
                            "The export records no fence or Wait evidence, so whether "
                            "the queues synchronised cannot be decided from the C++ "
                            "alone. Cross-queue use with a fence is normal (UE5 depth/"
                            "HZB); confirm in the PIX GUI before acting."
                        ),
                    }
                ]
                if len(queues) > 1
                else []
            ),
            None,
            source="gpu_replay",
        )
    )

    # 8. state_gap: an inconsistent state timeline is a split barrier or an
    #    off-queue state change; never smoothed over.
    gaps = [edge for edge in timeline if edge.get("inconsistent")]
    assertions.append(
        _assertion(
            "state_gap",
            "fail" if gaps else "pass",
            gaps[:8],
            None,
            source="gpu_replay",
        )
    )

    return {
        "resource_id": resource_id,
        "resource": resource.to_dict() if resource is not None else None,
        "depth_class": depth_class,
        "producers": producers,
        "consumers": consumers,
        "state_edges": timeline,
        "assertions": assertions,
        "verdict_summary": {
            verdict: sum(1 for a in assertions if a["verdict"] == verdict)
            for verdict in ("pass", "fail", "unknown")
        },
    }
