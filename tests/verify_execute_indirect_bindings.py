"""Regression probe for ExecuteIndirect binding snapshots.

Why this exists
---------------
A command list holds the graphics and the compute root arguments in two fully
independent sets. ``ExecuteIndirect`` looks identical in the exported C++ no
matter which pipeline it drives, so the parser used to classify it by API name
and always read the *graphics* set. Every indirect dispatch therefore came back
with srv=0/uav=0/cbv=0 and root_signature_id=None, which is indistinguishable
from "this action really binds nothing" -- a silent false negative that hides
the resource read/write history of every indirect compute pass in the frame.

The fix resolves the command signature (FrameResources_*.cpp) and uses its
D3D12_INDIRECT_ARGUMENT_TYPE to pick the binding set.

Reference case (Tiled.wpix): ``CompactTraces WaveOps:1``, Global ID 5098, uses
command signature 3346 (TYPE_DISPATCH), PSO 3854, root signature 3005, and binds
SRV table @152871 + UAV table @152869 + root CBV. ScreenProbeSceneDepth (t0)
must appear among the SRVs.

Run:
    python tests/verify_execute_indirect_bindings.py [session-name]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set.context import ToolContext  # noqa: E402
from pix_tool_set.engine.model import EventKind, RootParameterKind  # noqa: E402
from pix_tool_set.session import SessionStore  # noqa: E402

SESSION = sys.argv[1] if len(sys.argv) > 1 else "Tiled"

failures: list[str] = []
notes: list[str] = []


def check(condition: bool, message: str) -> bool:
    if condition:
        print(f"  ok   {message}")
        return True
    print(f"  FAIL {message}")
    failures.append(message)
    return False


def main() -> int:
    store = SessionStore()
    if store.get(SESSION) is None:
        print(f"No session named {SESSION!r}; open one with `pixts session-open` first.")
        print(f"known: {[r.name for r in store.list()]}")
        return 2

    capture = ToolContext.from_cwd().capture({"session": SESSION})

    print("1. command signatures parsed")
    signatures = capture.command_signatures
    check(bool(signatures), f"parsed {len(signatures)} command signatures")
    for sig in signatures.values():
        check(
            bool(sig.command_type),
            f"signature {sig.api_id} has a command type ({sig.command_type or '?'})",
        )

    print("\n2. every ExecuteIndirect resolves its command signature")
    indirect = [d for d in capture.draw_calls if d.kind is EventKind.EXECUTE_INDIRECT]
    check(bool(indirect), f"found {len(indirect)} ExecuteIndirect calls")
    unresolved = [d.index for d in indirect if d.command_signature_id is None]
    check(not unresolved, f"all resolved a command signature (missing: {unresolved[:5]})")

    print("\n3. no indirect dispatch reports an empty binding set")
    compute_indirect = [d for d in indirect if d.launches_compute]
    check(bool(compute_indirect), f"{len(compute_indirect)} of them are dispatches")
    empty = [d.index for d in compute_indirect if not d.bindings]
    check(
        not empty,
        f"every indirect dispatch snapshotted bindings (empty: {empty[:8]})",
    )
    no_rootsig = [d.index for d in compute_indirect if d.root_signature_id is None]
    check(
        not no_rootsig,
        f"every indirect dispatch has a root signature (missing: {no_rootsig[:8]})",
    )

    print("\n4. graphics ExecuteIndirect still reads the graphics set")
    gfx_indirect = [d for d in indirect if not d.launches_compute]
    if gfx_indirect:
        notes.append(f"{len(gfx_indirect)} indirect calls drive the graphics pipeline")
        mismatched = [
            d.index
            for d in gfx_indirect
            if d.pipeline_state is not None and d.pipeline_state.is_compute
        ]
        check(
            not mismatched,
            f"none classified as graphics while bound to a compute PSO ({mismatched[:5]})",
        )
    else:
        notes.append("no graphics ExecuteIndirect in this capture")

    print("\n5. classification agrees with the bound PSO")
    disagree = [
        (d.index, d.indirect_command_type, d.pipeline_state.kind)
        for d in indirect
        if d.pipeline_state is not None
        and d.pipeline_state.is_compute != d.launches_compute
    ]
    check(not disagree, f"command signature and PSO agree everywhere ({disagree[:5]})")

    print("\n6. reference case: CompactTraces WaveOps:1 (Global ID 5098)")
    target = next((d for d in indirect if d.global_id == 5098), None)
    if target is None:
        notes.append("Global ID 5098 absent; capture is not Tiled.wpix, skipping")
    else:
        check(target.launches_compute, "classified as an indirect dispatch")
        check(
            target.indirect_command_type == "DISPATCH",
            f"command type is DISPATCH (got {target.indirect_command_type!r})",
        )
        check(
            target.command_signature_id == 3346,
            f"command signature is 3346 (got {target.command_signature_id})",
        )
        check(target.pso_id == 3854, f"PSO is 3854 (got {target.pso_id})")
        check(
            target.root_signature_id == 3005,
            f"root signature is 3005 (got {target.root_signature_id})",
        )

        tables = [
            b
            for b in target.bindings
            if b.kind is RootParameterKind.DESCRIPTOR_TABLE
        ]
        bases = sorted(b.heap_index for b in tables if b.heap_index is not None)
        check(
            bases == [152869, 152871],
            f"both descriptor tables bound at 152869 + 152871 (got {bases})",
        )
        check(
            any(
                b.kind is RootParameterKind.CBV and b.resource_id == 2956
                for b in target.bindings
            ),
            "root CBV on resource 2956 present",
        )
        check(len(target.srvs) >= 3, f"at least 3 SRVs resolved (got {len(target.srvs)})")
        check(len(target.uavs) >= 2, f"at least 2 UAVs resolved (got {len(target.uavs)})")

        print("\n7. ScreenProbeSceneDepth is back in the read history")
        srv_ids = {
            v.resource_id if v.resource_id is not None else v.va_resource_id
            for v in target.srvs
        }
        srv_ids.discard(None)
        check(bool(srv_ids), f"SRV resource ids resolved: {sorted(srv_ids)}")
        check(
            785 in srv_ids,
            f"TraceHit (res 785) read as an SRV here (srvs: {sorted(srv_ids)})",
        )
        usage = capture.resource_usage
        reads_recorded = [
            rid for rid in srv_ids if target.index in usage.get(rid, {}).get("read_draws", [])
        ]
        check(
            len(reads_recorded) == len(srv_ids),
            f"every SRV records this draw in read_draws ({len(reads_recorded)}/{len(srv_ids)})",
        )

    print("\n8. descriptor coverage did not regress")
    coverage = capture.descriptor_coverage
    print(
        f"  tables={coverage['descriptor_tables_bound']} "
        f"with_views={coverage['descriptor_tables_with_views']} "
        f"coverage={coverage['coverage_percent']}%"
    )
    check(
        coverage["coverage_percent"] >= 95.0,
        f"table resolution at least 95% (got {coverage['coverage_percent']}%)",
    )

    print("\n" + "=" * 68)
    for note in notes:
        print(f"note: {note}")
    if failures:
        print(f"FAILED: {len(failures)} check(s)")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("PASS: ExecuteIndirect binding snapshots are complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
