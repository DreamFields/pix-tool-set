"""Can queue ownership be derived locally from the C++ export?

If yes, the multi-queue Queue ID problem is fixable inside pix-tool-set without
depending on pixtool exporting a per-queue event list (which this PIX build
cannot do: --queue-name rejects any value containing a space).

RenderFrameWorker_*.cpp records every submission as:

    ID3D12CommandList* commandLists[2];
    commandLists[0] = GetCommandList(2971).Get();
    commandLists[1] = GetCommandList(3058).Get();
    GetCommandQueue(1)->ExecuteCommandLists(_countof(commandLists), commandLists);

Each DrawCall already carries command_list_id, so command list -> queue gives
draw -> queue. This probe checks the mapping is actually usable:

  1. How many queues exist and which command lists go where.
  2. Whether any command list is submitted to more than one queue (which would
     make the mapping ambiguous and sink the whole approach).
  3. Whether the 90 draws currently missing a queue_id all live on the queues
     absent from the event list.
"""

from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set.context import ToolContext  # noqa: E402

EXPORT = Path(r"C:\Users\vinmeng\Desktop\ManyLights\debug\Tiled.pixcache\cpp")

RE_CL_ASSIGN = re.compile(r"commandLists\[(\d+)\]\s*=\s*GetCommandList\((\d+)\)")
RE_CL_UTILITY = re.compile(r"commandLists\[(\d+)\]\s*=\s*(g_\w*CommandList)")
RE_SUBMIT = re.compile(r"GetCommandQueue\((\d+)\)->ExecuteCommandLists")
RE_QUEUE_NAME = re.compile(r"GetObject\((\d+)\)->SetName\(LR\"\((.*?)\)\"\)")


def parse_submissions(root: Path) -> list[tuple[int, list[int]]]:
    """Return [(queue_id, [command_list_id, ...]), ...] in submission order."""
    out: list[tuple[int, list[int]]] = []
    for path in sorted(root.glob("RenderFrameWorker*.cpp")):
        pending: list[int] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = RE_CL_ASSIGN.search(line)
            if match:
                pending.append(int(match.group(2)))
                continue
            if RE_CL_UTILITY.search(line):
                pending.append(-1)  # utility list, not an ApiObjectId
                continue
            match = RE_SUBMIT.search(line)
            if match:
                out.append((int(match.group(1)), pending))
                pending = []
    return out


def queue_names(root: Path) -> dict[int, str]:
    names: dict[int, str] = {}
    for path in sorted(root.glob("FrameResources*.cpp")):
        for match in RE_QUEUE_NAME.finditer(
            path.read_text(encoding="utf-8", errors="replace")
        ):
            names[int(match.group(1))] = match.group(2)
    return names


def main() -> int:
    submissions = parse_submissions(EXPORT)
    print(f"1. submissions parsed: {len(submissions)}")
    per_queue = Counter(queue for queue, _ in submissions)
    names = queue_names(EXPORT)
    for queue, count in sorted(per_queue.items()):
        print(f"   queue {queue:>5}  submissions={count:>4}  name={names.get(queue, '?')!r}")

    print("\n2. command list -> queue mapping")
    cl_to_queues: dict[int, set[int]] = defaultdict(set)
    for queue, lists in submissions:
        for cl in lists:
            if cl >= 0:
                cl_to_queues[cl].add(queue)
    print(f"   distinct command lists submitted: {len(cl_to_queues)}")
    ambiguous = {cl: qs for cl, qs in cl_to_queues.items() if len(qs) > 1}
    print(f"   command lists submitted to >1 queue: {len(ambiguous)}")
    for cl, qs in list(ambiguous.items())[:10]:
        print(f"     cl {cl} -> queues {sorted(qs)}")

    print("\n3. do draws map cleanly onto that?")
    capture = ToolContext.from_cwd().capture({"session": "Tiled"})
    draws = capture.draw_calls
    unmapped = [d for d in draws if d.command_list_id not in cl_to_queues]
    print(f"   draws total={len(draws)}  with unmapped command list={len(unmapped)}")
    if unmapped:
        print(f"   unmapped command list ids: {sorted({d.command_list_id for d in unmapped})[:12]}")

    def queue_of(draw) -> int | None:
        qs = cl_to_queues.get(draw.command_list_id)
        if not qs:
            return None
        return sorted(qs)[0] if len(qs) == 1 else -2

    dist = Counter(queue_of(d) for d in draws)
    print(f"   draw distribution by derived queue: {dict(sorted(dist.items(), key=lambda kv: str(kv[0])))}")

    print("\n4. the 90 draws with no queue_id -- which queue are they on?")
    missing = [d for d in draws if d.queue_id is None]
    print(f"   draws lacking queue_id: {len(missing)}")
    print(f"   their derived queues: {dict(Counter(queue_of(d) for d in missing))}")
    have = [d for d in draws if d.queue_id is not None]
    print(f"   draws having queue_id, derived queues: {dict(Counter(queue_of(d) for d in have))}")

    print("\n5. verdict")
    clean = not ambiguous and not unmapped
    only_one_queue_in_csv = len({queue_of(d) for d in have}) == 1
    print(f"   mapping unambiguous and complete : {clean}")
    print(f"   event list covers exactly 1 queue: {only_one_queue_in_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
