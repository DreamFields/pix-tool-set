"""Non-draw events that touch a resource: barriers, clears, discards, copies.

The PIX resource-history view is not a list of draws. Alongside every draw and
dispatch it lists the barriers that changed the resource's state, the clear that
zeroed it, the discard that dropped its contents. Those rows carry the ``States``
column, and without them a history cannot explain *why* a resource was readable
at one point and a render target at another -- the transitions are exactly the
events that answer that.

``parse_resource_events`` walks the exported command lists a second time, picking
up the calls the draw parser deliberately ignores, and records for each one which
resources it named and in what position. Two things fall out of that:

* the ``API Parameters [n]`` binding label, where ``n`` is the resource's index in
  the call's argument array -- a barrier array in practice;
* a per-resource state timeline, because a transition barrier states both the
  before and after state verbatim.

Costs: this is a second full pass over CommandLists_*.cpp, which is why it is
opt-in at the tool level rather than folded into the draw parse.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from .cppparse import _RE_CL_FUNC, _RE_GLOBAL_ID, iter_lines, sorted_group

# One resource inside a barrier array, plus the states it moves between.
_RE_TRANSITION = re.compile(
    r"CD3DX12_RESOURCE_BARRIER::Transition\(\s*GetResource\((\d+)\)\.Get\(\)\s*,\s*"
    r"([^,]+?)\s*,\s*(.+?)\s*,\s*(\d+|4294967295)\s*,"
)
_RE_ALIASING = re.compile(
    r"CD3DX12_RESOURCE_BARRIER::Aliasing\(\s*([^,]+?)\s*,\s*(?:GetResource\((\d+)\)\.Get\(\)|nullptr)"
)
_RE_UAV_BARRIER = re.compile(
    r"CD3DX12_RESOURCE_BARRIER::UAV\(\s*(?:GetResource\((\d+)\)\.Get\(\)|nullptr)"
)
_RE_BARRIER_CALL = re.compile(r"->ResourceBarrier\(\s*(\d+)")
_RE_DISCARD = re.compile(r"->DiscardResource\(\s*GetResource\((\d+)\)\.Get\(\)")
_RE_CLEAR_RTV = re.compile(r"->ClearRenderTargetView\(")
_RE_CLEAR_DSV = re.compile(r"->ClearDepthStencilView\(")
_RE_CLEAR_UAV = re.compile(r"->ClearUnorderedAccessView(?:Float|Uint)\(")
_RE_CREATE_RTV_INLINE = re.compile(
    r"CreateRenderTargetView\(\s*GetResource\((\d+)\)\.Get\(\)"
)
_RE_CREATE_DSV_INLINE = re.compile(
    r"CreateDepthStencilView\(\s*GetResource\((\d+)\)\.Get\(\)"
)
_RE_COPY_BUFFER = re.compile(
    r"->CopyBufferRegion\(\s*GetResource\((\d+)\)\.Get\(\)\s*,\s*\d+\s*,\s*"
    r"GetResource\((\d+)\)\.Get\(\)"
)
_RE_CLEAR_FLOAT = re.compile(r"clearColor\[\]\s*=\s*\{([^}]*)\}")
_RE_CL_CALL_ANY = re.compile(r"GetCommandList\((\d+)\)->(\w+)\(")

# The subresource wildcard D3D12 uses for "all subresources".
ALL_SUBRESOURCES = 0xFFFFFFFF


@dataclass(slots=True)
class ResourceTouch:
    """One resource named by one non-draw event."""

    resource_id: int
    parameter_index: Optional[int]
    parameter_count: Optional[int]
    access: str  # "state_transition" | "write" | "discard" | "alias" | "copy_source" | "copy_dest"
    state_before: str = ""
    state_after: str = ""
    subresource: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "resource_id": self.resource_id,
            "access": self.access,
        }
        if self.parameter_index is not None:
            payload["parameter_index"] = self.parameter_index
        if self.parameter_count is not None:
            payload["parameter_count"] = self.parameter_count
        if self.state_before:
            payload["state_before"] = self.state_before
        if self.state_after:
            payload["state_after"] = self.state_after
        if self.subresource is not None:
            payload["subresource"] = (
                "all" if self.subresource == ALL_SUBRESOURCES else self.subresource
            )
        return payload


@dataclass(slots=True)
class ResourceEvent:
    """A non-draw command-list event, with every resource it names."""

    global_id: Optional[int]
    api: str
    event_type: str  # "barrier" | "clear" | "discard" | "copy"
    command_list_id: Optional[int]
    source_file: str
    source_line: int
    touches: list[ResourceTouch] = field(default_factory=list)
    clear_value: Optional[list[float]] = None
    marker_path: tuple[str, ...] = ()

    def touch_for(self, resource_id: int) -> Optional[ResourceTouch]:
        return next((t for t in self.touches if t.resource_id == resource_id), None)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "global_id": self.global_id,
            "api": self.api,
            "event_type": self.event_type,
            "command_list_id": self.command_list_id,
            "source": f"{self.source_file}:{self.source_line}",
            "resources": [t.to_dict() for t in self.touches],
        }
        if self.clear_value is not None:
            payload["clear_value"] = self.clear_value
        if self.marker_path:
            payload["pass_name"] = self.marker_path[-1]
        return payload


def _clean_state(text: str) -> str:
    """Normalise a state expression to what the PIX UI shows.

    The export writes the full enum names joined by ``|``. PIX drops the
    ``D3D12_RESOURCE_`` prefix, so ``D3D12_RESOURCE_STATE_RENDER_TARGET`` reads as
    ``STATE_RENDER_TARGET``. Keeping the pipe-joined form matters: a resource in
    ``NON_PIXEL | PIXEL_SHADER_RESOURCE`` is readable from both stages, and
    collapsing that to one would misreport what the next draw is allowed to do.
    """
    parts = [part.strip() for part in text.split("|")]
    cleaned = [
        part.replace("D3D12_RESOURCE_", "") for part in parts if part and part != "0"
    ]
    return " | ".join(cleaned)


def parse_resource_events(root: Path) -> list[ResourceEvent]:
    """Collect every barrier, clear, discard and copy in the export, in order.

    Events are emitted in the order they appear in the command lists, which is the
    order PIX numbers them, so the returned list is already a timeline.
    """
    events: list[ResourceEvent] = []

    for path in sorted_group(root, "CommandLists"):
        if not path.exists():
            continue
        current_cl: Optional[int] = None
        pending_global_id: Optional[int] = None
        pending_touches: list[ResourceTouch] = []
        pending_clear_target: Optional[int] = None
        pending_clear_value: Optional[list[float]] = None

        for lineno, line in iter_lines(path):
            match = _RE_CL_FUNC.match(line)
            if match:
                parts = match.group(1).split("_")
                try:
                    current_cl = int(parts[0])
                except (ValueError, IndexError):
                    current_cl = None
                pending_global_id = None
                pending_touches = []
                pending_clear_target = None
                continue

            match = _RE_GLOBAL_ID.search(line)
            if match:
                pending_global_id = int(match.group(1))
                # A new event starts here; anything still pending belonged to the
                # previous one and was either consumed or is not ours to keep.
                pending_touches = []
                pending_clear_target = None
                pending_clear_value = None
                continue

            # -- accumulate barrier entries ------------------------------
            for entry in _RE_TRANSITION.finditer(line):
                pending_touches.append(
                    ResourceTouch(
                        resource_id=int(entry.group(1)),
                        parameter_index=len(pending_touches),
                        parameter_count=None,
                        access="state_transition",
                        state_before=_clean_state(entry.group(2)),
                        state_after=_clean_state(entry.group(3)),
                        subresource=int(entry.group(4)),
                    )
                )
            for entry in _RE_ALIASING.finditer(line):
                # Aliasing names a before/after pair; the "after" resource is the
                # one gaining the memory, and that is the one PIX attributes.
                after = entry.group(2)
                pending_touches.append(
                    ResourceTouch(
                        resource_id=int(after) if after else -1,
                        parameter_index=len(pending_touches),
                        parameter_count=None,
                        access="alias",
                    )
                )
            for entry in _RE_UAV_BARRIER.finditer(line):
                rid = entry.group(1)
                pending_touches.append(
                    ResourceTouch(
                        resource_id=int(rid) if rid else -1,
                        parameter_index=len(pending_touches),
                        parameter_count=None,
                        access="uav_barrier",
                    )
                )

            # An inline RTV/DSV creation names the resource a following clear will
            # act on; the clear call itself only carries a descriptor handle, so
            # this is the only place the resource id appears.
            match = _RE_CREATE_RTV_INLINE.search(line) or _RE_CREATE_DSV_INLINE.search(
                line
            )
            if match:
                pending_clear_target = int(match.group(1))
            match = _RE_CLEAR_FLOAT.search(line)
            if match:
                try:
                    pending_clear_value = [
                        float(value.strip().rstrip("f"))
                        for value in match.group(1).split(",")
                        if value.strip()
                    ]
                except ValueError:
                    pending_clear_value = None

            call = _RE_CL_CALL_ANY.search(line)
            if call is None:
                continue
            cl_id, api = int(call.group(1)), call.group(2)

            if api == "ResourceBarrier":
                declared = _RE_BARRIER_CALL.search(line)
                count = int(declared.group(1)) if declared else len(pending_touches)
                # Only the last `count` entries belong to this call: the vector is
                # rebuilt per event, but a defensive tail-slice keeps a stray
                # earlier append from shifting every parameter index by one, which
                # would silently mislabel every API Parameters [n].
                touches = pending_touches[-count:] if count else []
                for position, touch in enumerate(touches):
                    touch.parameter_index = position
                    touch.parameter_count = len(touches)
                events.append(
                    ResourceEvent(
                        global_id=pending_global_id,
                        api="ResourceBarrier",
                        event_type="barrier",
                        command_list_id=cl_id,
                        source_file=path.name,
                        source_line=lineno,
                        touches=[t for t in touches if t.resource_id >= 0],
                    )
                )
                pending_touches = []
                continue

            if api == "DiscardResource":
                match = _RE_DISCARD.search(line)
                if match:
                    events.append(
                        ResourceEvent(
                            global_id=pending_global_id,
                            api="DiscardResource",
                            event_type="discard",
                            command_list_id=cl_id,
                            source_file=path.name,
                            source_line=lineno,
                            touches=[
                                ResourceTouch(
                                    resource_id=int(match.group(1)),
                                    parameter_index=0,
                                    parameter_count=1,
                                    access="discard",
                                )
                            ],
                        )
                    )
                continue

            if api in (
                "ClearRenderTargetView",
                "ClearDepthStencilView",
                "ClearUnorderedAccessViewFloat",
                "ClearUnorderedAccessViewUint",
            ):
                if pending_clear_target is not None:
                    events.append(
                        ResourceEvent(
                            global_id=pending_global_id,
                            api=api,
                            event_type="clear",
                            command_list_id=cl_id,
                            source_file=path.name,
                            source_line=lineno,
                            touches=[
                                ResourceTouch(
                                    resource_id=pending_clear_target,
                                    parameter_index=None,
                                    parameter_count=None,
                                    access="write",
                                )
                            ],
                            clear_value=pending_clear_value,
                        )
                    )
                pending_clear_target = None
                pending_clear_value = None
                continue

            if api == "CopyBufferRegion":
                match = _RE_COPY_BUFFER.search(line)
                if match:
                    events.append(
                        ResourceEvent(
                            global_id=pending_global_id,
                            api=api,
                            event_type="copy",
                            command_list_id=cl_id,
                            source_file=path.name,
                            source_line=lineno,
                            touches=[
                                ResourceTouch(
                                    resource_id=int(match.group(1)),
                                    parameter_index=0,
                                    parameter_count=2,
                                    access="copy_dest",
                                ),
                                ResourceTouch(
                                    resource_id=int(match.group(2)),
                                    parameter_index=2,
                                    parameter_count=2,
                                    access="copy_source",
                                ),
                            ],
                        )
                    )
                continue

    return events


def events_for_resource(
    events: Iterable[ResourceEvent], resource_id: int
) -> list[ResourceEvent]:
    """Filter a parsed event list down to those naming one resource."""
    return [event for event in events if event.touch_for(resource_id) is not None]


def state_timeline(
    events: Iterable[ResourceEvent], resource_id: int
) -> list[dict[str, Any]]:
    """Reconstruct the state a resource is in over the frame.

    Transition barriers are the only authoritative statements of resource state in
    the export, and they name both endpoints, so the timeline is read off directly
    rather than inferred. A gap between one transition's ``after`` and the next
    one's ``before`` means something changed state outside these barriers (a split
    barrier, or another queue), and is reported as ``inconsistent`` instead of
    being smoothed over -- a state timeline that quietly self-corrects would hide
    exactly the bug someone reads it to find.
    """
    timeline: list[dict[str, Any]] = []
    expected: Optional[str] = None
    for event in events:
        touch = event.touch_for(resource_id)
        if touch is None or touch.access != "state_transition":
            continue
        entry = {
            "global_id": event.global_id,
            "state_before": touch.state_before,
            "state_after": touch.state_after,
            "subresource": (
                "all" if touch.subresource == ALL_SUBRESOURCES else touch.subresource
            ),
            "source": f"{event.source_file}:{event.source_line}",
        }
        if expected is not None and touch.state_before != expected:
            entry["inconsistent"] = True
            entry["expected_state_before"] = expected
        timeline.append(entry)
        expected = touch.state_after
    return timeline
