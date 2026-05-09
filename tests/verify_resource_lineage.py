"""Unit-check the resource lineage engine (gap two).

Drives ``lineage.build_lineage`` against a fake capture, because the assertions
are pure synthesis over parsed data -- the test only needs to prove the joins
and verdicts, not to re-parse an export. Verifies: producers/consumers
attribution, read-before-write, missing UAV barrier, state gaps are not
smoothed, depth-class resources are flagged analytic-gradient, and next_action
sampling points land after the write event.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set.engine import lineage  # noqa: E402
from pix_tool_set.engine.cppparse import QueueOwnership  # noqa: E402
from pix_tool_set.engine.model import (  # noqa: E402
    DrawCall,
    EventKind,
    Resource,
    ResourceKind,
    View,
    ViewKind,
)
from pix_tool_set.engine.resourceevents import ResourceEvent, ResourceTouch  # noqa: E402


class FakeCapture:
    def __init__(self) -> None:
        self.draw_calls: list[DrawCall] = []
        self.resource_events: list[ResourceEvent] = []
        self.resources: dict[int, Resource] = {}
        self.views: dict[tuple[int, int], View] = {}
        self.command_queues = QueueOwnership()

    def resource(self, resource_id: int):
        return self.resources.get(resource_id)


def check(label: str, ok: bool) -> int:
    print(f"   {label:<46} {'ok' if ok else 'MISMATCH'}")
    return 0 if ok else 1


def make_draw(
    capture: FakeCapture,
    index: int,
    global_id: int,
    *,
    rt: list[int] | None = None,
    uav: list[tuple[int, int | None]] | None = None,
    srv: list[tuple[int, int | None]] | None = None,
) -> DrawCall:
    views: list[View] = []
    for rid, mip in uav or []:
        views.append(View(kind=ViewKind.UAV, resource_id=rid, mip_slice=mip))
    for rid, mip in srv or []:
        views.append(View(kind=ViewKind.SRV, resource_id=rid, mip_slice=mip))
    # Store the views on the draw via bindings so draw.uavs / draw.srvs work.
    from pix_tool_set.engine.model import BindingSlot, RootParameterKind

    bindings = []
    if views:
        bindings.append(
            BindingSlot(
                root_index=0,
                kind=RootParameterKind.DESCRIPTOR_TABLE,
                resolved_views=views,
            )
        )
    draw = DrawCall(
        index=index,
        kind=EventKind.DRAW if not (uav and not rt) else EventKind.DISPATCH,
        api="DrawIndexedInstanced" if not (uav and not rt) else "Dispatch",
        command_list_id=1,
        global_id=global_id,
        marker_path=("Frame 1", f"Pass{index}"),
        render_target_resource_ids=rt or [],
        bindings=bindings,
    )
    draw._capture = capture
    capture.draw_calls.append(draw)
    return draw


def main() -> int:
    failures = 0
    capture = FakeCapture()
    capture.resources[1985] = Resource(
        api_id=1985,
        kind=ResourceKind.TEXTURE2D,
        width=1532,
        height=764,
        format="DXGI_FORMAT_R32_TYPELESS",
        mip_levels=2,
    )
    capture.resources[777] = Resource(
        api_id=777,
        kind=ResourceKind.TEXTURE2D,
        width=1532,
        height=764,
        format="DXGI_FORMAT_D32_FLOAT",
    )

    # draw 0 writes 1985 as RTV (gid 100), draw 1 reads as SRV (gid 110).
    make_draw(capture, 0, 100, rt=[1985])
    make_draw(capture, 1, 110, srv=[(1985, 0)])
    capture.resource_events.append(
        ResourceEvent(
            global_id=105,
            api="ResourceBarrier",
            event_type="barrier",
            command_list_id=1,
            source_file="CommandLists_000.cpp",
            source_line=10,
            touches=[
                ResourceTouch(
                    resource_id=1985,
                    parameter_index=0,
                    parameter_count=1,
                    access="state_transition",
                    state_before="D3D12_RESOURCE_STATE_RENDER_TARGET",
                    state_after="D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE",
                    subresource=0xFFFFFFFF,
                )
            ],
        )
    )

    result = lineage.build_lineage(capture, 1985)

    print("attribution")
    failures += check(
        "producer is draw 0 (rtv_write)",
        any(t["draw_index"] == 0 and t["kind"] == "rtv_write" for t in result["producers"]),
    )
    failures += check(
        "consumer is draw 1 (srv_read)",
        any(t["draw_index"] == 1 and t["kind"] == "srv_read" for t in result["consumers"]),
    )
    failures += check(
        "state edge recorded",
        len(result["state_edges"]) == 1,
    )

    print("read_before_write")
    capture2 = FakeCapture()
    capture2.resources[1985] = capture.resources[1985]
    make_draw(capture2, 0, 100, srv=[(1985, 0)])   # reader at gid 100
    make_draw(capture2, 1, 110, rt=[1985])        # writer at gid 110
    make_draw(capture2, 2, 120, rt=[1985])        # later event, sampleable
    result2 = lineage.build_lineage(capture2, 1985)
    verdicts = {a["id"]: a["verdict"] for a in result2["assertions"]}
    failures += check("read before any write fails", verdicts.get("read_before_write") == "fail")
    write_touch = next(t for t in result2["producers"] if t["kind"] == "rtv_write")
    read_touch = next(t for t in result2["consumers"] if t["kind"] == "srv_read")
    failures += check(
        "next_action samples after the write",
        lineage._sampling_action(capture2, write_touch)["draw_index"] == write_touch["draw_index"] + 1,
    )
    failures += check(
        "sampling_point says after_write",
        lineage._sampling_action(capture2, write_touch)["sampling_point"] == "after_write",
    )

    print("missing_uav_barrier")
    capture3 = FakeCapture()
    capture3.resources[1985] = capture.resources[1985]
    make_draw(capture3, 0, 100, uav=[(1985, 0)])
    make_draw(capture3, 1, 110, uav=[(1985, 0)])
    result3 = lineage.build_lineage(capture3, 1985)
    verdicts3 = {a["id"]: a["verdict"] for a in result3["assertions"]}
    failures += check("two UAV writes without barrier fail",
                      verdicts3.get("missing_uav_barrier") == "fail")

    print("state_gap not smoothed")
    capture4 = FakeCapture()
    capture4.resources[1985] = capture.resources[1985]
    make_draw(capture4, 0, 100, rt=[1985])
    capture4.resource_events = [
        ResourceEvent(
            global_id=105,
            api="ResourceBarrier",
            event_type="barrier",
            command_list_id=1,
            source_file="CommandLists_000.cpp",
            source_line=10,
            touches=[
                ResourceTouch(
                    resource_id=1985,
                    parameter_index=0,
                    parameter_count=1,
                    access="state_transition",
                    state_before="D3D12_RESOURCE_STATE_RENDER_TARGET",
                    state_after="D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE",
                    subresource=0xFFFFFFFF,
                )
            ],
        ),
        ResourceEvent(
            global_id=120,
            api="ResourceBarrier",
            event_type="barrier",
            command_list_id=1,
            source_file="CommandLists_000.cpp",
            source_line=20,
            touches=[
                ResourceTouch(
                    resource_id=1985,
                    parameter_index=0,
                    parameter_count=1,
                    access="state_transition",
                    state_before="D3D12_RESOURCE_STATE_RENDER_TARGET",
                    state_after="D3D12_RESOURCE_STATE_UNORDERED_ACCESS",
                    subresource=0xFFFFFFFF,
                )
            ],
        ),
    ]
    result4 = lineage.build_lineage(capture4, 1985)
    verdicts4 = {a["id"]: a["verdict"] for a in result4["assertions"]}
    failures += check("inconsistent timeline fails state_gap",
                      verdicts4.get("state_gap") == "fail")

    print("depth-class analytic-gradient risk")
    make_draw(capture4, 1, 130, rt=[777], uav=[(777, 0)])
    capture4.resources[777] = capture.resources[777]
    result5 = lineage.build_lineage(capture4, 777)
    failures += check("depth_class flag set", result5["depth_class"] is True)
    fmt_assertion = next(a for a in result5["assertions"] if a["id"] == "format_reinterpret")
    failures += check(
        "analytic_gradient risk recorded",
        any(e.get("risk") == "analytic_gradient" for e in fmt_assertion["evidence"]),
    )

    print(f"\nRESULT: {'PASS' if failures == 0 else f'FAIL ({failures})'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
