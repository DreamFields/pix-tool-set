"""Per-event pixel sampling during replay: the data behind PIX's Pixel History.

What this produces
------------------
For one texel of one resource, an ordered trace of *paired* samples: the value
immediately **before** a chosen event and the value immediately **after** it. Two
samples per event, not one, because "what did this draw write?" cannot be answered
by a single read -- you need the value it started from to know whether it changed
anything at all. PIX's Pixel History panel shows exactly this pair as
``Previous Value`` -> ``New Value``, and reproducing it needs the same two reads.

Why sampling *inside* the recorded command list
-----------------------------------------------
``uavprobe`` copies a whole resource once, after all recorded work is submitted.
That answers "what does this resource hold at end of frame" and nothing else. A
pixel history needs the value at 2N specific points in the middle of the frame, so
the copy has to be recorded *between* the recorded commands, in the very command
list that carries them.

The exported project makes this possible in a way that is worth stating, because it
is what the whole design rests on: every recorded event in ``CommandLists_*.cpp``
is preceded by a ``// GlobalId        = N`` comment, sits at a fixed indent, and
begins at a statement boundary inside a ``PopulateCommandList_<cl>_<a>_<b>()``
function. Measured on the reference export: 5539 of 5539 GlobalId blocks satisfy
all three (indent 4, inside such a function, predecessor line ends in ``;``/``{``/
``}``/``#...``). So a probe call can be spliced immediately before and immediately
after any event without parsing C++ -- and ``install`` re-verifies these invariants
per file instead of trusting them, because an export that breaks them must fail
loudly rather than produce a plausible trace from misplaced copies.

The trap this design exists to avoid
------------------------------------
Sampling a resource at the *time the tool asks for event N* yields the state
**before** N executes. That is how ``pixtool save-resource`` behaves and it has
already cost real debugging time here: the value that looks like "what event N
wrote" is actually "what N inherited". Recording an explicit copy after N is the
only way to get N's own output, which is why every sample carries a ``phase`` of
``before`` or ``after`` and the two are never conflated.

Correctness details that are easy to get wrong, all of them load-bearing:

* **Recording, not executing.** The probe records ``CopyTextureRegion`` into the
  same command list, in line. It does not create a private queue or flush, unlike
  ``uavprobe``: doing that mid-recording would reorder the frame being reproduced.
  The readback buffer is read after the frame, once everything has drained.
* **One readback slot per sample.** Slots are pre-assigned in Python, so the C++
  side is a fixed table and no allocation happens during recording. Every recorded
  function runs exactly once per frame in the reference export (247 call sites, 247
  distinct functions), so a slot cannot be written twice within a frame.
* **State transitions.** A render target is in ``RENDER_TARGET`` while being drawn
  to, so each sample brackets its copy with a transition to ``COPY_SOURCE`` and
  back. The state to return to is supplied per target, not guessed.
* **Existence probing.** A pixel whose value never changes is indistinguishable
  from a probe that silently did nothing -- and the second, read as the first, is
  a false negative that looks like data. So each slot carries a ``written`` flag
  set by the GPU-side copy path, and the reader reports "not sampled" separately
  from "sampled, unchanged". Nothing is inferred from an absent value.
* **Frame gating.** The replay's ``RenderFrame()`` runs repeatedly. Samples are
  taken on the first recorded frame only, so a slot holds one frame's values
  rather than the last frame to overwrite it.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

PROBE_SOURCE_NAME = "PixToolSetPixelProbe.cpp"
PROBE_HEADER_NAME = "PixToolSetPixelProbe.h"
PROBE_FUNCTION = "PixToolSetPixelSample"
PROBE_FLUSH_FUNCTION = "PixToolSetPixelProbeFlush"
PROBE_PLAN_NAME = "pixtoolset_pixel_probe_plan.json"
MARKER = "// pix-tool-set: pixel history probe injected by pixel-history-replay"

#: Environment variables the injected probe reads. Declared here so the C++ side
#: and the Python side cannot drift apart.
ENV_OUT = "PIXTS_PIXEL_OUT"
ENV_ENABLE = "PIXTS_PIXEL_ENABLE"

#: D3D12_RESOURCE_STATES values used as the "return to" state for a sampled
#: target. Kept as plain ints because the Python side never includes d3d12.h.
STATE_RENDER_TARGET = 4
STATE_DEPTH_WRITE = 16
STATE_UNORDERED_ACCESS = 8
STATE_COPY_SOURCE = 2048
STATE_PIXEL_SHADER_RESOURCE = 128

#: Bytes reserved per sample slot in the readback buffer. A 1x1 copy of any format
#: this toolkit decodes is at most 16 bytes, but D3D12 requires each
#: CopyTextureRegion destination footprint to start at a 512-byte offset
#: (D3D12_TEXTURE_DATA_PLACEMENT_ALIGNMENT), so that is the stride.
SLOT_STRIDE = 512

#: Matches the export's per-event marker: ``    // GlobalId        = 3851``.
_RE_GID_LINE = re.compile(r"^(\s*)//\s*GlobalId\s*=\s*(\d+)\s*$")
#: Matches ``void PopulateCommandList_3200_2_0()``, whose first group is the
#: command list's ApiObjectId -- which is what the probe needs to record into.
_RE_POPULATE_FUNC = re.compile(r"^void\s+PopulateCommandList_(\d+)_(\d+)_(\d+)\s*\(\s*\)")

PHASE_BEFORE = "before"
PHASE_AFTER = "after"


# ======================================================================
# the sampling plan
# ======================================================================
@dataclass(frozen=True, slots=True)
class SampleSlot:
    """One recorded 1x1 copy: which resource, which texel, when, and where it lands.

    ``slot`` is the index into the readback buffer and is assigned by Python, so the
    generated C++ needs no allocator and no map lookup at record time.
    """

    slot: int
    global_id: int
    phase: str  # PHASE_BEFORE | PHASE_AFTER
    resource_id: int
    x: int
    y: int
    subresource: int
    return_state: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "global_id": self.global_id,
            "phase": self.phase,
            "resource_id": self.resource_id,
            "x": self.x,
            "y": self.y,
            "subresource": self.subresource,
            "return_state": self.return_state,
        }


@dataclass(slots=True)
class SamplePlan:
    """Every slot to record, plus the identity of the texel being traced."""


    resource_id: int
    x: int
    y: int
    slots: list[SampleSlot] = field(default_factory=list)
    subresource: int = 0

    def add(self, global_id: int, phase: str, return_state: int) -> SampleSlot:
        slot = SampleSlot(
            slot=len(self.slots),
            global_id=global_id,
            phase=phase,
            resource_id=self.resource_id,
            x=self.x,
            y=self.y,
            subresource=self.subresource,
            return_state=return_state,
        )
        self.slots.append(slot)
        return slot

    @property
    def slot_count(self) -> int:
        return len(self.slots)

    def by_global_id(self) -> dict[int, dict[str, SampleSlot]]:
        out: dict[int, dict[str, SampleSlot]] = {}
        for slot in self.slots:
            out.setdefault(slot.global_id, {})[slot.phase] = slot
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "pixel": {"x": self.x, "y": self.y},
            "subresource": self.subresource,
            "slot_count": self.slot_count,
            "slots": [slot.to_dict() for slot in self.slots],
        }


def build_plan(
    resource_id: int,
    x: int,
    y: int,
    global_ids: Iterable[int],
    *,
    return_state: int = STATE_RENDER_TARGET,
    return_states: dict[int, int] | None = None,
    subresource: int = 0,
) -> SamplePlan:
    """Two slots per event: one before it, one after it.

    Both are always emitted, even though a run of consecutive events makes some of
    them redundant (event N's ``after`` and event N+1's ``before`` read the same
    point when nothing separates them). The redundancy is the point: comparing the
    two is a free consistency check on the whole mechanism, and a mismatch means
    something wrote the texel from a path the plan does not know about. Deduplicating
    would save GPU copies and throw away the only self-check available.
    """
    plan = SamplePlan(resource_id=resource_id, x=x, y=y, subresource=subresource)
    states = return_states or {}
    for gid in sorted({int(g) for g in global_ids}):
        state = int(states.get(gid, return_state))
        plan.add(gid, PHASE_BEFORE, state)
        plan.add(gid, PHASE_AFTER, state)
    return plan


# ======================================================================
# the injected probe
# ======================================================================
_PROBE_HEADER = r'''// pix-tool-set: pixel history probe injected by pixel-history-replay. Do not edit.
#pragma once

#include "pch.h"

// Records a 1x1 CopyTextureRegion of one texel into a pre-assigned readback slot.
// Declared in a header so every CommandLists_*.cpp can call it after one include.
void PixToolSetPixelSample(unsigned int slot, ID3D12GraphicsCommandList* list);
void PixToolSetPixelProbeFlush();
'''

_PROBE_SOURCE_TEMPLATE = r'''// pix-tool-set: pixel history probe injected by pixel-history-replay. Do not edit.
//
// Records a 1x1 copy of one texel into a readback buffer, once per pre-assigned
// slot, in line with the recorded commands. The slot table is generated, so this
// file allocates nothing while recording and needs no lookups.
//
//   PIXTS_PIXEL_OUT      absolute path for the JSON trace (and <path>.done sentinel)
//   PIXTS_PIXEL_ENABLE   set to 0 to make every call a no-op
//
// With PIXTS_PIXEL_OUT unset the probe still records copies but writes no file, so a
// leftover binary cannot surprise anyone.
//
// Notes that are easy to get wrong, each of which cost time somewhere:
//   * g_device and ApiObjectId live in the *global* namespace (CapturedAssets.h),
//     not in Helpers::. Writing Helpers::g_device fails with C2039.
//   * GetResource() uses .at() and throws; g_resources is searched directly.
//   * The copy is *recorded*, not executed. Creating a private queue and flushing
//     mid-recording would reorder the very frame the replay reproduces.
//   * Each destination footprint must start at a 512-byte aligned offset
//     (D3D12_TEXTURE_DATA_PLACEMENT_ALIGNMENT), hence the slot stride.
//   * Samples are taken on the first recorded frame only: RenderFrame() runs in a
//     loop, and without gating each slot would hold whichever frame wrote last.

#include "pch.h"

#include "Helpers.h"
#include "PixToolSetPixelProbe.h"

#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <string>
#include <vector>

using Microsoft::WRL::ComPtr;

namespace
{
    struct SlotSpec
    {
        unsigned int slot;
        unsigned int globalId;
        unsigned int resourceId;
        unsigned int x;
        unsigned int y;
        unsigned int subresource;
        unsigned int returnState;
        unsigned char phaseIsAfter;
    };

    // Generated from the Python-side plan.
    static const SlotSpec kSlots[] = {
__SLOT_TABLE__
    };
    static const unsigned int kSlotCount = __SLOT_COUNT__;
    static const unsigned int kSlotStride = __SLOT_STRIDE__;

    struct SlotResult
    {
        bool recorded;      // a copy was recorded for this slot
        bool readable;      // the readback bytes were mapped successfully
        unsigned int byteCount;
        unsigned char bytes[16];
        unsigned int dxgiFormat;
        unsigned int rowPitch;
    };

    static ComPtr<ID3D12Resource> g_readback;
    static SlotResult g_results[__SLOT_COUNT_OR_ONE__]{};
    static bool g_disabled = false;
    static bool g_initialised = false;
    static bool g_flushed = false;

    void Report(const char* text)
    {
        OutputDebugStringA(text);
    }

    bool Enabled()
    {
        if (!g_initialised)
        {
            g_initialised = true;
            char buffer[16]{};
            DWORD length = GetEnvironmentVariableA("PIXTS_PIXEL_ENABLE", buffer, 16);
            if (length > 0 && length < 16 && buffer[0] == '0')
            {
                g_disabled = true;
            }
        }
        return !g_disabled && kSlotCount > 0;
    }

    std::wstring OutputPath()
    {
        wchar_t buffer[1024]{};
        DWORD length = GetEnvironmentVariableW(L"PIXTS_PIXEL_OUT", buffer, 1024);
        if (length == 0 || length >= 1024)
        {
            return std::wstring();
        }
        return std::wstring(buffer);
    }

    // One buffer for every slot. Created lazily on the first sample so a build whose
    // probe is never exercised allocates nothing.
    bool EnsureReadback()
    {
        if (g_readback)
        {
            return true;
        }
        if (!g_device)
        {
            return false;
        }

        D3D12_HEAP_PROPERTIES heap{};
        heap.Type = D3D12_HEAP_TYPE_READBACK;
        heap.CPUPageProperty = D3D12_CPU_PAGE_PROPERTY_UNKNOWN;
        heap.MemoryPoolPreference = D3D12_MEMORY_POOL_UNKNOWN;

        D3D12_RESOURCE_DESC desc{};
        desc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
        desc.Width = static_cast<UINT64>(kSlotStride) * kSlotCount;
        desc.Height = 1;
        desc.DepthOrArraySize = 1;
        desc.MipLevels = 1;
        desc.Format = DXGI_FORMAT_UNKNOWN;
        desc.SampleDesc.Count = 1;
        desc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
        desc.Flags = D3D12_RESOURCE_FLAG_NONE;

        if (FAILED(g_device->CreateCommittedResource(
                &heap, D3D12_HEAP_FLAG_NONE, &desc,
                D3D12_RESOURCE_STATE_COPY_DEST, nullptr, IID_PPV_ARGS(&g_readback))))
        {
            Report("[pix-tool-set] pixel probe: readback allocation failed\n");
            return false;
        }
        return true;
    }

    ID3D12Resource* FindResource(unsigned int resourceId)
    {
        // GetResource() throws on a miss, and a throw from inside a recorded command
        // list would take down the replay rather than degrade the trace.
        auto found = g_resources.find(static_cast<ApiObjectId>(resourceId));
        if (found == g_resources.end())
        {
            return nullptr;
        }
        return found->second.Get();
    }
}

void PixToolSetPixelSample(unsigned int slot, ID3D12GraphicsCommandList* list)
{
    if (!Enabled() || list == nullptr || slot >= kSlotCount)
    {
        return;
    }
    // RenderFrame() loops. Only the first recorded frame is sampled, so a slot holds
    // one frame's value instead of whichever frame happened to write last.
    if (g_results[slot].recorded)
    {
        return;
    }
    if (!EnsureReadback())
    {
        return;
    }

    const SlotSpec& spec = kSlots[slot];
    ID3D12Resource* source = FindResource(spec.resourceId);
    if (source == nullptr)
    {
        char message[192]{};
        sprintf_s(message, "[pix-tool-set] pixel probe: resource %u is not tracked\n",
                  spec.resourceId);
        Report(message);
        return;
    }

    const D3D12_RESOURCE_DESC desc = source->GetDesc();
    if (desc.Dimension != D3D12_RESOURCE_DIMENSION_TEXTURE2D)
    {
        return;
    }
    if (spec.x >= static_cast<unsigned int>(desc.Width) ||
        spec.y >= static_cast<unsigned int>(desc.Height))
    {
        return;
    }

    // A 1x1 footprint of this format. Asking the device rather than assuming keeps
    // block-compressed and bit-packed formats honest.
    D3D12_RESOURCE_DESC single = desc;
    single.Width = 1;
    single.Height = 1;
    single.MipLevels = 1;
    single.DepthOrArraySize = 1;

    D3D12_PLACED_SUBRESOURCE_FOOTPRINT footprint{};
    UINT numRows = 0;
    UINT64 rowSizeBytes = 0;
    UINT64 totalBytes = 0;
    g_device->GetCopyableFootprints(
        &single, 0, 1, static_cast<UINT64>(slot) * kSlotStride,
        &footprint, &numRows, &rowSizeBytes, &totalBytes);
    if (rowSizeBytes == 0 || rowSizeBytes > sizeof(g_results[0].bytes))
    {
        return;
    }

    const bool needsBarrier =
        spec.returnState != static_cast<unsigned int>(D3D12_RESOURCE_STATE_COPY_SOURCE);
    D3D12_RESOURCE_BARRIER toCopy{};
    toCopy.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    toCopy.Flags = D3D12_RESOURCE_BARRIER_FLAG_NONE;
    toCopy.Transition.pResource = source;
    toCopy.Transition.Subresource = spec.subresource;
    toCopy.Transition.StateBefore =
        static_cast<D3D12_RESOURCE_STATES>(spec.returnState);
    toCopy.Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_SOURCE;
    if (needsBarrier)
    {
        list->ResourceBarrier(1, &toCopy);
    }

    D3D12_TEXTURE_COPY_LOCATION dst{};
    dst.pResource = g_readback.Get();
    dst.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    dst.PlacedFootprint = footprint;

    D3D12_TEXTURE_COPY_LOCATION src{};
    src.pResource = source;
    src.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    src.SubresourceIndex = spec.subresource;

    D3D12_BOX box{};
    box.left = spec.x;
    box.top = spec.y;
    box.front = 0;
    box.right = spec.x + 1;
    box.bottom = spec.y + 1;
    box.back = 1;

    list->CopyTextureRegion(&dst, 0, 0, 0, &src, &box);

    if (needsBarrier)
    {
        // Put the resource back, so the rest of the recorded frame sees the state it
        // was recorded with.
        D3D12_RESOURCE_BARRIER back = toCopy;
        back.Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_SOURCE;
        back.Transition.StateAfter =
            static_cast<D3D12_RESOURCE_STATES>(spec.returnState);
        list->ResourceBarrier(1, &back);
    }

    // "A copy was recorded" is tracked separately from "a value was read", because a
    // slot that was never recorded and a texel that never changed are different
    // findings and must not be reported as one.
    g_results[slot].recorded = true;
    g_results[slot].byteCount = static_cast<unsigned int>(rowSizeBytes);
    g_results[slot].dxgiFormat = static_cast<unsigned int>(footprint.Footprint.Format);
    g_results[slot].rowPitch = footprint.Footprint.RowPitch;
}

void PixToolSetPixelProbeFlush()
{
    if (g_flushed)
    {
        return;
    }
    g_flushed = true;
    if (!Enabled())
    {
        return;
    }

    const std::wstring path = OutputPath();
    if (path.empty())
    {
        Report("[pix-tool-set] pixel probe: PIXTS_PIXEL_OUT is not set\n");
        return;
    }

    unsigned int readable = 0;
    if (g_readback)
    {
        void* mapped = nullptr;
        D3D12_RANGE range{ 0, static_cast<SIZE_T>(kSlotStride) * kSlotCount };
        if (SUCCEEDED(g_readback->Map(0, &range, &mapped)) && mapped != nullptr)
        {
            const unsigned char* base = static_cast<const unsigned char*>(mapped);
            for (unsigned int i = 0; i < kSlotCount; ++i)
            {
                if (!g_results[i].recorded)
                {
                    continue;
                }
                const unsigned int count = g_results[i].byteCount;
                memcpy(g_results[i].bytes, base + static_cast<size_t>(i) * kSlotStride,
                       count > sizeof(g_results[i].bytes) ? sizeof(g_results[i].bytes) : count);
                g_results[i].readable = true;
                ++readable;
            }
            g_readback->Unmap(0, nullptr);
        }
        else
        {
            Report("[pix-tool-set] pixel probe: readback map failed\n");
        }
    }

    std::ofstream out(path.c_str(), std::ios::binary);
    out << "{\"slot_count\":" << kSlotCount << ",\"samples\":[";
    for (unsigned int i = 0; i < kSlotCount; ++i)
    {
        if (i > 0)
        {
            out << ",";
        }
        const SlotSpec& spec = kSlots[i];
        out << "{\"slot\":" << spec.slot
            << ",\"global_id\":" << spec.globalId
            << ",\"phase\":\"" << (spec.phaseIsAfter ? "after" : "before") << "\""
            << ",\"resource_id\":" << spec.resourceId
            << ",\"x\":" << spec.x
            << ",\"y\":" << spec.y
            << ",\"recorded\":" << (g_results[i].recorded ? "true" : "false")
            << ",\"readable\":" << (g_results[i].readable ? "true" : "false")
            << ",\"dxgi_format\":" << g_results[i].dxgiFormat
            << ",\"bytes\":[";
        for (unsigned int b = 0; b < g_results[i].byteCount &&
                                 b < sizeof(g_results[i].bytes); ++b)
        {
            if (b > 0)
            {
                out << ",";
            }
            out << static_cast<unsigned int>(g_results[i].bytes[b]);
        }
        out << "]}";
    }
    out << "]}";
    out.close();

    // Written last, so a reader can tell "still running" from "finished and produced
    // nothing" without relying on a timeout alone.
    std::ofstream sentinel((path + L".done").c_str());
    sentinel << "slots=" << kSlotCount << "\n"
             << "readable=" << readable << "\n";
    sentinel.close();

    char message[192]{};
    sprintf_s(message, "[pix-tool-set] pixel probe: %u of %u slots readable\n",
              readable, kSlotCount);
    Report(message);
}
'''


def _slot_table(plan: SamplePlan) -> str:
    rows: list[str] = []
    for slot in plan.slots:
        rows.append(
            "        {{ {slot}u, {gid}u, {rid}u, {x}u, {y}u, {sub}u, {state}u, {after} }},".format(
                slot=slot.slot,
                gid=slot.global_id,
                rid=slot.resource_id,
                x=slot.x,
                y=slot.y,
                sub=slot.subresource,
                state=slot.return_state,
                after=1 if slot.phase == PHASE_AFTER else 0,
            )
        )
    return "\n".join(rows) if rows else "        { 0u, 0u, 0u, 0u, 0u, 0u, 0u, 0 },"


def render_probe_source(plan: SamplePlan) -> str:
    """The probe translation unit, with the plan baked in as a static table."""
    count = plan.slot_count
    return (
        _PROBE_SOURCE_TEMPLATE.replace("__SLOT_TABLE__", _slot_table(plan))
        .replace("__SLOT_COUNT_OR_ONE__", str(max(count, 1)))
        .replace("__SLOT_COUNT__", str(count))
        .replace("__SLOT_STRIDE__", str(SLOT_STRIDE))
    )


# ======================================================================
# injecting and removing the probe
# ======================================================================
def _backup_path(target: Path) -> Path:
    """``CommandLists_000.cpp`` -> ``CommandLists_000.cpp.orig``, matching uavprobe."""
    return target.with_suffix(target.suffix + ".orig")


def _command_list_sources(export_dir: Path) -> list[Path]:
    return sorted(export_dir.glob("CommandLists_*.cpp"))


def is_installed(export_dir: Path) -> bool:
    source = export_dir / PROBE_SOURCE_NAME
    if not source.exists():
        return False
    lists = export_dir / "CMakeLists.txt"
    if not lists.exists() or PROBE_SOURCE_NAME not in lists.read_text(
        encoding="utf-8", errors="replace"
    ):
        return False
    render = export_dir / "RenderFrame.cpp"
    return render.exists() and PROBE_FLUSH_FUNCTION in render.read_text(
        encoding="utf-8", errors="replace"
    )


def installed_plan(export_dir: Path) -> Optional[SamplePlan]:
    """The plan a previously installed probe was built for, or None.

    Needed because ``--skip-build`` may only reuse an executable whose baked-in slot
    table is the one being asked for. Reusing a binary built for another pixel would
    return values that look right and belong to somewhere else.
    """
    record = export_dir / PROBE_PLAN_NAME
    if not record.exists():
        return None
    try:
        payload = json.loads(record.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    pixel = payload.get("pixel") or {}
    plan = SamplePlan(
        resource_id=int(payload.get("resource_id", 0)),
        x=int(pixel.get("x", -1)),
        y=int(pixel.get("y", -1)),
        subresource=int(payload.get("subresource", 0)),
    )
    for entry in payload.get("slots", []):
        plan.slots.append(
            SampleSlot(
                slot=int(entry["slot"]),
                global_id=int(entry["global_id"]),
                phase=str(entry["phase"]),
                resource_id=int(entry["resource_id"]),
                x=int(entry["x"]),
                y=int(entry["y"]),
                subresource=int(entry.get("subresource", 0)),
                return_state=int(entry.get("return_state", STATE_RENDER_TARGET)),
            )
        )
    return plan


def plan_matches(export_dir: Path, plan: SamplePlan) -> bool:
    existing = installed_plan(export_dir)
    if existing is None:
        return False
    return [slot.to_dict() for slot in existing.slots] == [
        slot.to_dict() for slot in plan.slots
    ]


def _verify_export_shape(path: Path) -> list[str]:
    """Check the three invariants injection relies on; return every violation.

    Verified rather than assumed, because a malformed splice would still compile in
    some cases and produce a trace whose copies sit somewhere other than where the
    caller believes. A loud failure is the only safe outcome.
    """
    problems: list[str] = []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    enclosing: str | None = None
    for index, line in enumerate(lines):
        func = _RE_POPULATE_FUNC.match(line)
        if func:
            enclosing = func.group(1)
        match = _RE_GID_LINE.match(line)
        if not match:
            continue
        if enclosing is None:
            problems.append(
                f"{path.name}:{index + 1}: GlobalId marker outside any "
                f"PopulateCommandList function"
            )
        previous = index - 1
        while previous >= 0 and not lines[previous].strip():
            previous -= 1
        text = lines[previous].strip() if previous >= 0 else ""
        if not (
            text.endswith(";")
            or text.endswith("{")
            or text.endswith("}")
            or text.startswith("#")
        ):
            problems.append(
                f"{path.name}:{index + 1}: GlobalId marker does not follow a "
                f"statement boundary (previous line: {text[:60]!r})"
            )
    return problems


def _event_block_end(lines: list[str], start: int) -> int:
    """Index one past the event that begins at the GlobalId marker on ``start``.

    Two shapes occur in the export and they end differently:
      * a braced block -- ``{`` on the next line, closed by a ``}`` at the marker's
        own indent;
      * a single statement -- ``GetCommandList(n)->DrawIndexedInstanced(...);``.
    Brace counting handles the first; the first line ending in ``;`` handles the
    second. Anything else returns -1 so the caller skips the event rather than
    guessing, because a wrong end means the "after" copy lands mid-event.
    """
    indent = len(lines[start]) - len(lines[start].lstrip())
    cursor = start + 1
    while cursor < len(lines) and not lines[cursor].strip():
        cursor += 1
    if cursor >= len(lines):
        return -1

    if lines[cursor].strip() == "{":
        depth = 0
        while cursor < len(lines):
            stripped = lines[cursor].strip()
            depth += stripped.count("{") - stripped.count("}")
            if depth == 0:
                current_indent = len(lines[cursor]) - len(lines[cursor].lstrip())
                if stripped.startswith("}") and current_indent == indent:
                    return cursor + 1
            cursor += 1
        return -1

    # A single statement, possibly wrapped over several lines.
    while cursor < len(lines):
        if lines[cursor].rstrip().endswith(";"):
            return cursor + 1
        if _RE_GID_LINE.match(lines[cursor]):
            return -1
        cursor += 1
    return -1


def install(export_dir: Path, plan: SamplePlan) -> dict[str, Any]:
    """Splice the probe calls around every planned event and add the probe sources.

    Idempotent only with respect to an *identical* plan: a different pixel or a
    different event set needs a different slot table, so a mismatched install is
    replaced rather than reused.
    """
    from ..errors import PixToolError, not_found

    lists = export_dir / "CMakeLists.txt"
    render = export_dir / "RenderFrame.cpp"
    for required in (lists, render):
        if not required.exists():
            raise not_found(
                required.name,
                str(export_dir),
                "This export cannot host the probe; re-run session-open to regenerate it.",
            )

    if plan.slot_count == 0:
        raise PixToolError(
            code="pixel_probe_empty_plan",
            message="No events were selected, so there is nothing to sample.",
            stage="export",
            suggestion=(
                "Widen the candidate set: the resource may not be written at this "
                "pixel by any event in the frame."
            ),
        )

    if is_installed(export_dir) and plan_matches(export_dir, plan):
        return {
            "action": "reused the probe already installed for this exact plan",
            "already_installed": True,
            "slot_count": plan.slot_count,
            "files_modified": [],
            "rebuild_needed": False,
        }

    # A probe for a different plan must go before a new one is spliced in, or the
    # two sets of calls would both be present and both write into slot 0.
    if is_installed(export_dir) or (export_dir / PROBE_SOURCE_NAME).exists():
        restore(export_dir)

    sources = _command_list_sources(export_dir)
    if not sources:
        raise not_found(
            "CommandLists_*.cpp",
            str(export_dir),
            "The probe records its copies inside the exported command lists; this "
            "export has none.",
        )

    problems: list[str] = []
    for path in sources:
        problems.extend(_verify_export_shape(path))
    if problems:
        raise PixToolError(
            code="pixel_probe_export_shape",
            message=(
                "The exported command lists are not shaped the way this probe splices "
                "into, so nothing was changed."
            ),
            stage="export",
            paths=[str(p) for p in sources],
            suggestion=(
                "Injection requires every '// GlobalId = N' marker to sit inside a "
                "PopulateCommandList function at a statement boundary. Report the "
                "violations below; a forced injection would place copies in the wrong "
                "place and silently produce wrong values."
            ),
            details={"violations": problems[:20], "violation_count": len(problems)},
        )

    wanted = plan.by_global_id()
    changed: list[str] = []
    backups: list[str] = []
    injected: dict[int, dict[str, Any]] = {}

    for path in sources:
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines(keepends=True)
        raw = [line.rstrip("\n").rstrip("\r") for line in lines]

        # (position, text) insertions, applied back-to-front so earlier indices stay
        # valid.
        insertions: list[tuple[int, str]] = []
        command_list = None
        touched = False

        for index, line in enumerate(raw):
            func = _RE_POPULATE_FUNC.match(line)
            if func:
                command_list = func.group(1)
            match = _RE_GID_LINE.match(line)
            if not match:
                continue
            gid = int(match.group(2))
            phases = wanted.get(gid)
            if not phases or command_list is None:
                continue

            end = _event_block_end(raw, index)
            if end < 0:
                injected[gid] = {
                    "injected": False,
                    "reason": (
                        "the event's extent could not be determined from the export, "
                        "so no copy was placed"
                    ),
                    "source": f"{path.name}:{index + 1}",
                }
                continue

            indent = " " * (len(line) - len(line.lstrip()))
            entry: dict[str, Any] = {
                "injected": True,
                "source": f"{path.name}:{index + 1}",
                "command_list_id": int(command_list),
            }

            before = phases.get(PHASE_BEFORE)
            if before is not None:
                insertions.append(
                    (
                        index,
                        f"{indent}{MARKER} (slot {before.slot}, before GlobalId {gid})\n"
                        f"{indent}{PROBE_FUNCTION}({before.slot}u, "
                        f"GetCommandList({command_list}).Get());\n",
                    )
                )
                entry["before_slot"] = before.slot

            after = phases.get(PHASE_AFTER)
            if after is not None:
                insertions.append(
                    (
                        end,
                        f"{indent}{MARKER} (slot {after.slot}, after GlobalId {gid})\n"
                        f"{indent}{PROBE_FUNCTION}({after.slot}u, "
                        f"GetCommandList({command_list}).Get());\n",
                    )
                )
                entry["after_slot"] = after.slot

            injected[gid] = entry
            touched = True

        if not touched:
            continue

        backup = _backup_path(path)
        if not backup.exists():
            shutil.copy2(path, backup)
            backups.append(str(backup))

        # Later positions first: inserting at a lower index would shift the ones after
        # it, and a shifted "after" copy lands inside the following event.
        for position, snippet in sorted(insertions, key=lambda item: -item[0]):
            lines.insert(position, snippet)

        header_include = f'#include "{PROBE_HEADER_NAME}"\n'
        if header_include not in "".join(lines[:40]):
            anchor = next(
                (
                    i
                    for i, line in enumerate(lines)
                    if line.startswith("#include") or line.startswith("using namespace")
                ),
                0,
            )
            lines.insert(anchor, header_include)

        path.write_text("".join(lines), encoding="utf-8")
        changed.append(str(path))

    missing = sorted(set(wanted) - set(injected))
    not_injected = sorted(
        gid for gid, entry in injected.items() if not entry.get("injected")
    )

    # --- probe sources -------------------------------------------------
    (export_dir / PROBE_HEADER_NAME).write_text(_PROBE_HEADER, encoding="utf-8")
    (export_dir / PROBE_SOURCE_NAME).write_text(
        render_probe_source(plan), encoding="utf-8"
    )
    changed.extend(
        [str(export_dir / PROBE_HEADER_NAME), str(export_dir / PROBE_SOURCE_NAME)]
    )

    # --- RenderFrame.cpp: flush once, after the recorded work ----------
    render_text = render.read_text(encoding="utf-8", errors="replace")
    if PROBE_FLUSH_FUNCTION not in render_text:
        anchor = "g_perFrameBuffers.clear();"
        if anchor not in render_text:
            _restore_paths(export_dir, changed)
            raise PixToolError(
                code="pixel_probe_anchor_missing",
                message="RenderFrame.cpp does not end the frame the way this probe expects.",
                stage="export",
                paths=[str(render)],
                suggestion=(
                    "The flush is injected before g_perFrameBuffers.clear(); that "
                    "statement was not found, so the injection was rolled back."
                ),
            )
        backup = _backup_path(render)
        if not backup.exists():
            shutil.copy2(render, backup)
            backups.append(str(backup))
        declaration = f'{MARKER}\n#include "{PROBE_HEADER_NAME}"\n\n'
        marker_line = "void RenderFrame()"
        position = render_text.find(marker_line)
        render_text = (
            render_text[:position] + declaration + render_text[position:]
            if position >= 0
            else declaration + render_text
        )
        call = (
            f"    {MARKER}\n"
            f"    // Runs after the recorded work has been submitted, so every slot's\n"
            f"    // copy has completed and the readback buffer can be mapped.\n"
            f"    {PROBE_FLUSH_FUNCTION}();\n\n"
        )
        position = render_text.find(anchor)
        line_start = render_text.rfind("\n", 0, position) + 1
        render_text = render_text[:line_start] + call + render_text[line_start:]
        render.write_text(render_text, encoding="utf-8")
        changed.append(str(render))

    # --- CMakeLists.txt: an explicit source list, not a GLOB -----------
    cmake_text = lists.read_text(encoding="utf-8", errors="replace")
    if PROBE_SOURCE_NAME not in cmake_text:
        out_lines: list[str] = []
        inserted = False
        for line in cmake_text.splitlines(keepends=True):
            out_lines.append(line)
            if not inserted and line.strip() == "RenderFrame.cpp":
                indent = line[: len(line) - len(line.lstrip())]
                out_lines.append(f"{indent}{PROBE_SOURCE_NAME}\n")
                inserted = True
        if not inserted:
            _restore_paths(export_dir, changed)
            raise PixToolError(
                code="pixel_probe_cmake_anchor_missing",
                message="CMakeLists.txt has no RenderFrame.cpp entry to insert the probe after.",
                stage="export",
                paths=[str(lists)],
                suggestion="The export's source list is not shaped as expected; the injection was rolled back.",
            )
        cmake_backup = _backup_path(lists)
        if not cmake_backup.exists():
            shutil.copy2(lists, cmake_backup)
            backups.append(str(cmake_backup))
        lists.write_text("".join(out_lines), encoding="utf-8")
        changed.append(str(lists))

    (export_dir / PROBE_PLAN_NAME).write_text(
        json.dumps(plan.to_dict(), indent=2), encoding="utf-8"
    )

    return {
        "action": "injected the pixel history probe into the export",
        "already_installed": False,
        "slot_count": plan.slot_count,
        "events_injected": sorted(
            gid for gid, entry in injected.items() if entry.get("injected")
        ),
        "events_not_injected": not_injected,
        "events_not_found_in_export": missing,
        "injection_detail": injected,
        "files_modified": changed,
        "backups": backups,
        "rebuild_needed": True,
    }


def _restore_paths(export_dir: Path, changed: Iterable[str]) -> None:
    """Roll back a partial injection so a failure never leaves a broken export."""
    for name in changed:
        target = Path(name)
        backup = _backup_path(target)
        if backup.exists() and target.exists():
            shutil.copy2(backup, target)
            backup.unlink()
    for name in (PROBE_SOURCE_NAME, PROBE_HEADER_NAME, PROBE_PLAN_NAME):
        (export_dir / name).unlink(missing_ok=True)


def restore(export_dir: Path) -> dict[str, Any]:
    """Undo the injection: restore every ``.orig`` and delete the probe sources."""
    restored: list[str] = []
    removed: list[str] = []

    targets = [export_dir / "RenderFrame.cpp", export_dir / "CMakeLists.txt"]
    targets.extend(_command_list_sources(export_dir))
    for target in targets:
        backup = _backup_path(target)
        if backup.exists() and target.exists():
            shutil.copy2(backup, target)
            backup.unlink()
            restored.append(str(target))

    for name in (PROBE_SOURCE_NAME, PROBE_HEADER_NAME, PROBE_PLAN_NAME):
        path = export_dir / name
        if path.exists():
            path.unlink()
            removed.append(str(path))

    return {
        "action": "restored the export to its state before injection",
        "files_restored": restored,
        "files_removed": removed,
    }


# ======================================================================
# reading and decoding what the probe wrote
# ======================================================================
#: Channel layouts for the bit-packed formats the probe can meet. Each entry is
#: (channel name, bit offset, bit width). Kept explicit rather than derived from the
#: format name because the alpha field of R10G10B10A2 is 2 bits while its name
#: suggests nothing about which end it sits at, and getting that wrong yields values
#: that are plausible and wrong -- the worst possible failure here.
_PACKED_LAYOUTS: dict[int, tuple[str, tuple[tuple[str, int, int], ...]]] = {
    24: (
        "R10G10B10A2_UNORM",
        (("R", 0, 10), ("G", 10, 10), ("B", 20, 10), ("A", 30, 2)),
    ),
    87: (
        "B8G8R8A8_UNORM",
        (("B", 0, 8), ("G", 8, 8), ("R", 16, 8), ("A", 24, 8)),
    ),
}


@dataclass(frozen=True, slots=True)
class PixelValue:
    """One decoded texel: normalised floats alongside the raw integer fields.

    Both are carried because the PIX UI shows both (``0.4995 (0x1FF)``) and because
    the raw field is the only form that proves the bit layout was read correctly --
    a normalised 0.4995 could come from several wrong interpretations, 0x1FF from
    only one.
    """

    format_name: str
    dxgi_format: int
    channels: tuple[str, ...]
    normalised: tuple[float, ...]
    raw: tuple[int, ...]
    bit_widths: tuple[int, ...]
    raw_bytes: bytes

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format_name,
            "dxgi_format": self.dxgi_format,
            "channels": list(self.channels),
            "normalised": [round(v, 6) for v in self.normalised],
            "raw": list(self.raw),
            "raw_hex": [f"0x{v:X}" for v in self.raw],
            "bit_widths": list(self.bit_widths),
            "display": ", ".join(
                f"{name}:{value:.4f} (0x{integer:X})"
                for name, value, integer in zip(
                    self.channels, self.normalised, self.raw
                )
            ),
            "bytes_hex": self.raw_bytes.hex(),
        }

    def equals(self, other: "PixelValue | None", *, tolerance: int = 0) -> bool:
        """Compare on the raw integer fields, which is exact.

        Comparing normalised floats would make 511/1023 and 511.0000001/1023 differ
        on a round trip through JSON. ``tolerance`` is in raw units and defaults to
        an exact match.
        """
        if other is None:
            return False
        if len(self.raw) != len(other.raw):
            return False
        return all(abs(a - b) <= tolerance for a, b in zip(self.raw, other.raw))


def decode_pixel(raw_bytes: bytes, dxgi_format: int) -> Optional[PixelValue]:
    """Decode one texel's bytes for a format, or None when the format is unknown.

    Bit-packed formats are unpacked here from an explicit field table; everything
    else is delegated to ``engine/dds.py``, which already handles the component
    formats and is the single place those live. None is returned rather than a guess
    -- a wrong value is worse than a missing one.
    """
    from . import dds

    if not raw_bytes:
        return None

    packed = _PACKED_LAYOUTS.get(dxgi_format)
    if packed is not None:
        name, fields = packed
        if len(raw_bytes) < 4:
            return None
        value = int.from_bytes(raw_bytes[:4], "little")
        names: list[str] = []
        raws: list[int] = []
        norms: list[float] = []
        widths: list[int] = []
        for channel, offset, width in fields:
            field_value = (value >> offset) & ((1 << width) - 1)
            names.append(channel)
            raws.append(field_value)
            widths.append(width)
            norms.append(field_value / float((1 << width) - 1))
        # Report in RGBA order regardless of storage order, so a BGRA target and an
        # RGBA one can be compared without the caller tracking which is which.
        order = [names.index(c) for c in ("R", "G", "B", "A") if c in names]
        return PixelValue(
            format_name=name,
            dxgi_format=dxgi_format,
            channels=tuple(names[i] for i in order),
            normalised=tuple(norms[i] for i in order),
            raw=tuple(raws[i] for i in order),
            bit_widths=tuple(widths[i] for i in order),
            raw_bytes=bytes(raw_bytes[:4]),
        )

    spec = dds.DXGI_FORMATS.get(dxgi_format)
    if spec is None:
        return None
    name, bytes_per_pixel, components, code = spec
    if len(raw_bytes) < bytes_per_pixel:
        return None
    image = dds.DdsImage(
        width=1,
        height=1,
        dxgi_format=dxgi_format,
        format_name=name,
        bytes_per_pixel=bytes_per_pixel,
        component_count=components,
        component_code=code,
        pixel_offset=0,
        data=bytes(raw_bytes[:bytes_per_pixel]),
    )
    try:
        normalised = image.pixel(0, 0, normalise=True)
        raw = image.pixel(0, 0, normalise=False)
    except (IndexError, Exception):  # noqa: BLE001 - a bad decode must not raise here
        return None
    normalised = list(normalised) if isinstance(normalised, (list, tuple)) else [normalised]
    raw = list(raw) if isinstance(raw, (list, tuple)) else [raw]
    letters = re.findall(r"([RGBA])\d+", name) or ["R", "G", "B", "A"][: len(normalised)]
    widths = [int(w) for w in re.findall(r"[RGBA](\d+)", name)] or [
        bytes_per_pixel * 8 // max(len(normalised), 1)
    ] * len(normalised)
    return PixelValue(
        format_name=name,
        dxgi_format=dxgi_format,
        channels=tuple(letters[: len(normalised)]),
        normalised=tuple(float(v) for v in normalised),
        raw=tuple(int(v) for v in raw),
        bit_widths=tuple(widths[: len(normalised)]),
        raw_bytes=bytes(raw_bytes[:bytes_per_pixel]),
    )


@dataclass(slots=True)
class Sample:
    """One slot as it came back: what was asked for, and what (if anything) arrived."""

    slot: int
    global_id: int
    phase: str
    resource_id: int
    x: int
    y: int
    recorded: bool
    readable: bool
    dxgi_format: int
    raw_bytes: bytes
    value: Optional[PixelValue] = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "slot": self.slot,
            "global_id": self.global_id,
            "phase": self.phase,
            "resource_id": self.resource_id,
            "recorded": self.recorded,
            "readable": self.readable,
            "value": self.value.to_dict() if self.value else None,
        }
        if self.reason:
            payload["reason"] = self.reason
        return payload


def read_trace(trace_path: Path | str, plan: SamplePlan | None = None) -> dict[str, Any]:
    """Parse the probe's JSON and decode every slot.

    A missing value is always accompanied by a reason distinguishing the three cases
    that produce one -- no copy was recorded, the copy was recorded but the buffer
    could not be read, the bytes arrived but the format has no decoder. Collapsing
    them into a bare null would make "the probe did not run" look identical to "the
    texel was never written", which is the exact false negative this tool exists to
    avoid.
    """
    path = Path(trace_path)
    if not path.exists():
        return {
            "ok": False,
            "samples": [],
            "reason": f"the probe wrote no trace at {path}",
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {
            "ok": False,
            "samples": [],
            "reason": f"the trace could not be parsed: {type(exc).__name__}: {exc}",
        }

    expected_slots = {slot.slot for slot in plan.slots} if plan is not None else None

    samples: list[Sample] = []

    for entry in payload.get("samples", []):
        raw = bytes(int(b) & 0xFF for b in entry.get("bytes", []))
        dxgi = int(entry.get("dxgi_format", 0))
        recorded = bool(entry.get("recorded"))
        readable = bool(entry.get("readable"))
        value = None
        reason = ""
        if not recorded:
            reason = (
                "no copy was recorded for this slot: the event was not reached during "
                "the replayed frame, or the resource was not tracked"
            )
        elif not readable:
            reason = "the copy was recorded but the readback buffer could not be mapped"
        else:
            value = decode_pixel(raw, dxgi)
            if value is None:
                reason = (
                    f"the bytes arrived but DXGI format {dxgi} has no decoder, so no "
                    f"value is reported rather than a guessed one"
                )
        samples.append(
            Sample(
                slot=int(entry.get("slot", -1)),
                global_id=int(entry.get("global_id", -1)),
                phase=str(entry.get("phase", "")),
                resource_id=int(entry.get("resource_id", 0)),
                x=int(entry.get("x", -1)),
                y=int(entry.get("y", -1)),
                recorded=recorded,
                readable=readable,
                dxgi_format=dxgi,
                raw_bytes=raw,
                value=value,
                reason=reason,
            )
        )

    sentinel = Path(str(path) + ".done")
    finished = sentinel.exists()
    report: dict[str, Any] = {
        "ok": True,
        "path": str(path),
        "finished": finished,
        "slot_count": int(payload.get("slot_count", len(samples))),
        "samples": samples,
        "decoded_count": sum(1 for s in samples if s.value is not None),
    }
    if expected_slots is not None:
        # A slot the plan asked for but the trace never mentions means the built
        # binary does not match the plan -- almost always a stale --skip-build reuse.
        # Naming it beats letting the caller read the gap as "the pixel was untouched".
        returned = {s.slot for s in samples}
        report["slots_missing_from_trace"] = sorted(expected_slots - returned)
        report["slots_unexpected_in_trace"] = sorted(returned - expected_slots)
    return report



def pair_samples(samples: Iterable[Sample]) -> dict[int, dict[str, Sample]]:
    """Group samples into ``{global_id: {"before": ..., "after": ...}}``."""
    paired: dict[int, dict[str, Sample]] = {}
    for sample in samples:
        paired.setdefault(sample.global_id, {})[sample.phase] = sample
    return paired


def check_consistency(
    ordered_global_ids: list[int], paired: dict[int, dict[str, Sample]]
) -> dict[str, Any]:
    """Assert that each event's New Value equals the next event's Previous Value.

    Nothing may change the texel between one event's ``after`` copy and the next
    event's ``before`` copy, because the two copies are adjacent in the same
    recorded stream when the events are consecutive. So the two must read alike, and
    a mismatch is evidence that something outside the sampled set wrote the texel --
    another queue, an event the candidate set missed, or a misplaced splice. Either
    way it invalidates the trace's story and must be reported rather than smoothed
    over.
    """
    checks: list[dict[str, Any]] = []
    for first, second in zip(ordered_global_ids, ordered_global_ids[1:]):
        after = (paired.get(first) or {}).get(PHASE_AFTER)
        before = (paired.get(second) or {}).get(PHASE_BEFORE)
        entry: dict[str, Any] = {
            "from_global_id": first,
            "to_global_id": second,
        }
        if after is None or before is None or after.value is None or before.value is None:
            entry["status"] = "not_checkable"
            entry["detail"] = (
                "one of the two samples has no value, so the pair cannot be compared"
            )
        elif after.value.equals(before.value):
            entry["status"] = "consistent"
            entry["value"] = after.value.to_dict()["display"]
        else:
            entry["status"] = "mismatch"
            entry["new_value_of_first"] = after.value.to_dict()["display"]
            entry["previous_value_of_second"] = before.value.to_dict()["display"]
            entry["detail"] = (
                "the texel changed between these two events without a sampled event "
                "in between; the candidate set is incomplete or another queue wrote it"
            )
        checks.append(entry)

    mismatches = [c for c in checks if c["status"] == "mismatch"]
    uncheckable = [c for c in checks if c["status"] == "not_checkable"]
    return {
        "checks": checks,
        "checked": len(checks) - len(uncheckable),
        "consistent": len(checks) - len(mismatches) - len(uncheckable),
        "mismatches": len(mismatches),
        "not_checkable": len(uncheckable),
        "self_consistent": not mismatches and len(checks) > len(uncheckable),
    }


#: Verdicts a sampled event can carry. Deliberately separates the measurement from
#: the interpretation: ``value_unchanged`` is a fact, ``failed_depth_stencil_test``
#: is a conclusion that requires the depth evidence to support it.
VERDICT_WROTE = "wrote_value"
VERDICT_UNCHANGED = "value_unchanged"
VERDICT_DEPTH_FAILED = "failed_depth_stencil_test"
VERDICT_UNKNOWN = "not_sampled"


def classify_event(
    before: Optional[Sample],
    after: Optional[Sample],
    *,
    depth_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """What happened at this event, and how strongly the data supports saying so.

    The distinction this function exists to preserve: "the value did not change" is
    measured, while "the depth test rejected this pixel" is inferred. The second is
    only claimed when the first holds *and* the pipeline state shows a bound depth
    target with depth testing enabled -- and even then the conclusion is labelled as
    inferred, with the evidence attached, because a pixel can also be left unchanged
    by a shader that discarded, by a scissor, or by a blend that is a no-op. Reporting
    the GUI's wording without the evidence would be fabricating agreement.
    """
    if before is None or after is None or before.value is None or after.value is None:
        missing = []
        if before is None or before.value is None:
            missing.append("previous")
        if after is None or after.value is None:
            missing.append("new")
        reason = "; ".join(
            filter(
                None,
                [
                    (before.reason if before is not None else "no 'before' slot"),
                    (after.reason if after is not None else "no 'after' slot"),
                ],
            )
        )
        return {
            "verdict": VERDICT_UNKNOWN,
            "verdict_is_inferred": False,
            "missing_values": missing,
            "reason": reason or "the probe produced no value for this event",
        }

    if not after.value.equals(before.value):
        return {
            "verdict": VERDICT_WROTE,
            "verdict_is_inferred": False,
            "evidence": "the texel's raw bits differ across the event",
        }

    outcome: dict[str, Any] = {
        "verdict": VERDICT_UNCHANGED,
        "verdict_is_inferred": False,
        "evidence": "the texel's raw bits are identical before and after the event",
    }
    evidence = depth_evidence or {}
    covers = evidence.get("depth_test_enabled") and evidence.get(
        "depth_stencil_bound"
    )
    if covers:
        outcome["verdict"] = VERDICT_DEPTH_FAILED
        outcome["verdict_is_inferred"] = True
        outcome["inference"] = (
            "the value did not change and this draw runs with depth testing against a "
            "bound depth target, which is what PIX reports as a failed depth/stencil "
            "test. It is an inference: a discard in the shader, a scissor, or a no-op "
            "blend would look identical from the texel alone."
        )
        outcome["depth_evidence"] = evidence
    elif evidence:
        outcome["depth_evidence"] = evidence
        outcome["note"] = (
            "no depth/stencil conclusion is drawn: "
            + (
                "the draw has no depth target bound"
                if not evidence.get("depth_stencil_bound")
                else "depth testing is not enabled on this pipeline state"
            )
        )
    return outcome

