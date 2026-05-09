"""Read back a UAV the GPU actually wrote, by probing the exported replay project.

Why this exists
---------------
A compute shader's UAV is unreadable through every other path in this toolkit, and
each of the three failures has a different cause:

  * ``pixtool save-resource`` exports *bound render targets* only, so a compute-only
    UAV fails with "PIXTOOL9 - Requested Render Target with specified index does not
    exist". ``export-texture`` inherits that limit.
  * ``export-uav-slice`` and ``read-resource-texture`` read ``resources.bin``, which
    holds uploads and CPU writes. A UAV filled on the GPU is never re-uploaded, so
    those tools honestly report the resource's *initial* bytes - not the dispatch's
    output.
  * ``replay-render`` captures the replay window. An intermediate G-Buffer UAV never
    reaches the backbuffer, so the picture does not change even when the UAV does.

What is left is to ask the GPU during a replay. This module injects a small,
self-contained C++ probe into the exported project which copies the target texture
into a ``D3D12_HEAP_TYPE_READBACK`` buffer after the frame's recorded work has been
submitted, and writes the bytes plus a layout sidecar to disk. Python then decodes
them. The bytes therefore describe what the GPU wrote, which is a different question
from the one ``export-uav-slice`` answers, and the tool says so in its diagnostics.

Design decisions worth stating
------------------------------
  * The probe takes its target and its output path from *environment variables*, not
    from generated constants. One build therefore serves any resource and any number
    of runs, which is what makes "dump, swap the shader patch, dump again" cheap.
  * With no environment set, the probe does nothing. A leftover binary cannot surprise
    anyone by writing files.
  * It creates its own allocator, command list and fence. Reusing the recorded ones
    would perturb the very submission order the replay exists to reproduce.
  * Every file it touches in the user's export is backed up as ``.orig`` before the
    first edit, matching the convention ``shader-edit-apply`` established with
    ``CreatePSOs.cpp.orig``.

Traps that cost real debugging time, encoded here so they stay fixed:
  * ``g_cmdQueue`` in ``Helpers.h`` is a *local* inside ``CreateAndTrackCommandQueue``,
    not a global. The supported way to a queue is the global ``g_commandQueues`` map.
  * ``g_device`` and ``ApiObjectId`` live in the *global* namespace (``CapturedAssets.h``),
    not in ``Helpers::``. Writing ``Helpers::g_device`` fails with C2039.
  * The target texture sits in a UAV state at end of frame, so it needs a barrier to
    ``COPY_SOURCE`` and back.
  * The export's ``CMakeLists.txt`` lists sources explicitly; it is not a GLOB, so a new
    file must be added to it by hand.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import dds

PROBE_SOURCE_NAME = "PixToolSetProbe.cpp"
PROBE_FUNCTION = "PixToolSetProbeReadback"
MARKER = "// pix-tool-set: UAV readback probe injected by read-uav"

#: Environment variables the injected probe reads. Kept here so the C++ side and the
#: Python side cannot drift apart.
ENV_TARGETS = "PIXTS_PROBE_TARGETS"
ENV_OUT = "PIXTS_PROBE_OUT"
ENV_STATE = "PIXTS_PROBE_STATE"
#: Which mip the probe copies. A mip-chain pass such as UE5's ReduceHZB binds the
#: same texture at several mips at once (mips 4..7 in one dispatch), and the readback
#: previously hard-coded subresource 0, so every requested mip returned the top
#: level's bytes -- identical output that silently looked like a successful export.
ENV_MIP = "PIXTS_PROBE_MIP"

#: D3D12_RESOURCE_STATE_UNORDERED_ACCESS. The state a compute UAV is left in at the
#: end of the recorded frame, and so the default StateBefore for the copy barrier.
STATE_UNORDERED_ACCESS = 8


# ======================================================================
# the injected probe
# ======================================================================
PROBE_SOURCE = r'''// pix-tool-set: UAV readback probe injected by read-uav. Do not edit by hand.
//
// Copies one or more captured resources into a READBACK heap after the frame's
// recorded work has been submitted, and writes each one to disk with a sidecar
// describing its layout. This is the only way to observe what a compute dispatch
// wrote: pixtool's save-resource exports bound render targets only, and
// resources.bin holds uploads rather than GPU writes.
//
// Everything is driven by environment variables, so one build serves any resource
// and any number of runs:
//
//   PIXTS_PROBE_TARGETS  comma-separated resource ids, e.g. "3032" or "3032,3033"
//   PIXTS_PROBE_OUT      absolute path prefix; each dump becomes <prefix>_<id>.bin
//                        plus <prefix>_<id>.bin.txt, and <prefix>.done when finished
//   PIXTS_PROBE_STATE    optional D3D12_RESOURCE_STATES value the resource is in
//                        (default 8 = UNORDERED_ACCESS)
//   PIXTS_PROBE_MIP      optional mip level to copy (default 0). A mip-chain pass
//                        binds one texture at several mips in a single dispatch, so
//                        the subresource must be selectable; hard-coding 0 returned
//                        the top level for every request.
//
// With no targets set it does nothing at all, so a leftover binary is harmless.
//
// Implementation notes that are easy to get wrong:
//   * g_cmdQueue in Helpers.h is a local, not a global; g_commandQueues is the map.
//   * g_device and ApiObjectId are in the global namespace, not in Helpers::.
//   * GetResource() uses .at() and throws, so g_resources is searched directly.
//   * A private allocator/list/fence is used so the recorded submission order is
//     never disturbed.

#include "pch.h"

#include "Helpers.h"

#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <string>
#include <vector>

using Microsoft::WRL::ComPtr;

namespace
{
    std::wstring ProbeOutputPrefix()
    {
        wchar_t buffer[1024]{};
        DWORD length = GetEnvironmentVariableW(L"PIXTS_PROBE_OUT", buffer, 1024);
        if (length == 0 || length >= 1024)
        {
            return std::wstring();
        }
        return std::wstring(buffer);
    }

    std::vector<ApiObjectId> ProbeTargets()
    {
        std::vector<ApiObjectId> targets;
        char buffer[1024]{};
        DWORD length = GetEnvironmentVariableA("PIXTS_PROBE_TARGETS", buffer, 1024);
        if (length == 0 || length >= 1024)
        {
            return targets;
        }
        const char* cursor = buffer;
        while (*cursor != '\0')
        {
            char* end = nullptr;
            unsigned long value = strtoul(cursor, &end, 10);
            if (end == cursor)
            {
                ++cursor;
                continue;
            }
            targets.push_back(static_cast<ApiObjectId>(value));
            cursor = end;
        }
        return targets;
    }

    D3D12_RESOURCE_STATES ProbeSourceState()
    {
        char buffer[64]{};
        DWORD length = GetEnvironmentVariableA("PIXTS_PROBE_STATE", buffer, 64);
        if (length == 0 || length >= 64)
        {
            return D3D12_RESOURCE_STATE_UNORDERED_ACCESS;
        }
        return static_cast<D3D12_RESOURCE_STATES>(strtoul(buffer, nullptr, 10));
    }

    UINT ProbeMipLevel()
    {
        char buffer[64]{};
        DWORD length = GetEnvironmentVariableA("PIXTS_PROBE_MIP", buffer, 64);
        if (length == 0 || length >= 64)
        {
            return 0;
        }
        return static_cast<UINT>(strtoul(buffer, nullptr, 10));
    }

    ID3D12CommandQueue* FindDirectQueue()
    {
        // g_cmdQueue in Helpers.h is a function-local, so the tracked map is the only
        // way to a queue the replay itself created.
        for (const auto& entry : g_commandQueues)
        {
            if (!entry.second)
            {
                continue;
            }
            D3D12_COMMAND_QUEUE_DESC queueDesc = entry.second->GetDesc();
            if (queueDesc.Type == D3D12_COMMAND_LIST_TYPE_DIRECT)
            {
                return entry.second.Get();
            }
        }
        return nullptr;
    }

    void Report(const char* text)
    {
        OutputDebugStringA(text);
    }

    bool DumpOne(ApiObjectId resourceId, const std::wstring& prefix,
                 D3D12_RESOURCE_STATES sourceState, ID3D12CommandQueue* queue,
                 UINT mipLevel)
    {
        auto found = g_resources.find(resourceId);
        if (found == g_resources.end() || !found->second)
        {
            char message[256]{};
            sprintf_s(message, "[pix-tool-set] probe: resource %u is not tracked\n",
                      static_cast<unsigned>(resourceId));
            Report(message);
            return false;
        }
        ComPtr<ID3D12Resource> source = found->second;
        const D3D12_RESOURCE_DESC desc = source->GetDesc();

        // Refuse a mip the resource does not have rather than clamping to 0. Clamping
        // would hand back the top level's bytes under the requested mip's filename,
        // which is indistinguishable from a correct export.
        if (mipLevel >= desc.MipLevels)
        {
            char message[256]{};
            sprintf_s(message,
                      "[pix-tool-set] probe: resource %u has %u mip(s), mip %u requested\n",
                      static_cast<unsigned>(resourceId),
                      static_cast<unsigned>(desc.MipLevels),
                      static_cast<unsigned>(mipLevel));
            Report(message);
            return false;
        }

        // For a 2D non-array texture the subresource index equals the mip level.
        // Arrays would need mip + arraySlice * mipLevels; this probe copies plane 0
        // of slice 0, which is what the HZB-style cases need.
        const UINT subresource = mipLevel;

        D3D12_PLACED_SUBRESOURCE_FOOTPRINT footprint{};
        UINT numRows = 0;
        UINT64 rowSizeBytes = 0;
        UINT64 totalBytes = 0;
        g_device->GetCopyableFootprints(
            &desc, subresource, 1, 0, &footprint, &numRows, &rowSizeBytes, &totalBytes);
        if (totalBytes == 0)
        {
            Report("[pix-tool-set] probe: footprint is empty\n");
            return false;
        }

        D3D12_HEAP_PROPERTIES readbackHeap{};
        readbackHeap.Type = D3D12_HEAP_TYPE_READBACK;
        readbackHeap.CPUPageProperty = D3D12_CPU_PAGE_PROPERTY_UNKNOWN;
        readbackHeap.MemoryPoolPreference = D3D12_MEMORY_POOL_UNKNOWN;

        D3D12_RESOURCE_DESC bufferDesc{};
        bufferDesc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
        bufferDesc.Alignment = 0;
        bufferDesc.Width = totalBytes;
        bufferDesc.Height = 1;
        bufferDesc.DepthOrArraySize = 1;
        bufferDesc.MipLevels = 1;
        bufferDesc.Format = DXGI_FORMAT_UNKNOWN;
        bufferDesc.SampleDesc.Count = 1;
        bufferDesc.SampleDesc.Quality = 0;
        bufferDesc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
        bufferDesc.Flags = D3D12_RESOURCE_FLAG_NONE;

        ComPtr<ID3D12Resource> readback;
        if (FAILED(g_device->CreateCommittedResource(
                &readbackHeap, D3D12_HEAP_FLAG_NONE, &bufferDesc,
                D3D12_RESOURCE_STATE_COPY_DEST, nullptr, IID_PPV_ARGS(&readback))))
        {
            Report("[pix-tool-set] probe: readback allocation failed\n");
            return false;
        }

        // A private allocator and list: reusing the recorded ones would disturb the
        // submission order the replay exists to reproduce.
        ComPtr<ID3D12CommandAllocator> allocator;
        if (FAILED(g_device->CreateCommandAllocator(
                D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&allocator))))
        {
            Report("[pix-tool-set] probe: allocator creation failed\n");
            return false;
        }
        ComPtr<ID3D12GraphicsCommandList> list;
        if (FAILED(g_device->CreateCommandList(
                0, D3D12_COMMAND_LIST_TYPE_DIRECT, allocator.Get(), nullptr,
                IID_PPV_ARGS(&list))))
        {
            Report("[pix-tool-set] probe: command list creation failed\n");
            return false;
        }

        const bool needsBarrier = sourceState != D3D12_RESOURCE_STATE_COPY_SOURCE;
        D3D12_RESOURCE_BARRIER toCopy{};
        toCopy.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
        toCopy.Flags = D3D12_RESOURCE_BARRIER_FLAG_NONE;
        toCopy.Transition.pResource = source.Get();
        // Must match the subresource being copied. Transitioning subresource 0 while
        // copying mip N leaves mip N in UNORDERED_ACCESS during a COPY_SOURCE read,
        // which is a silent barrier mismatch the debug layer would flag.
        toCopy.Transition.Subresource = subresource;
        toCopy.Transition.StateBefore = sourceState;
        toCopy.Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_SOURCE;
        if (needsBarrier)
        {
            list->ResourceBarrier(1, &toCopy);
        }

        D3D12_TEXTURE_COPY_LOCATION dst{};
        dst.pResource = readback.Get();
        dst.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
        dst.PlacedFootprint = footprint;

        D3D12_TEXTURE_COPY_LOCATION src{};
        src.pResource = source.Get();
        src.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
        src.SubresourceIndex = subresource;

        list->CopyTextureRegion(&dst, 0, 0, 0, &src, nullptr);

        if (needsBarrier)
        {
            // Put it back, so the rest of the replay sees the state it recorded.
            D3D12_RESOURCE_BARRIER back = toCopy;
            back.Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_SOURCE;
            back.Transition.StateAfter = sourceState;
            list->ResourceBarrier(1, &back);
        }
        list->Close();

        ID3D12CommandList* lists[] = { list.Get() };
        queue->ExecuteCommandLists(1, lists);

        ComPtr<ID3D12Fence> fence;
        if (FAILED(g_device->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&fence))))
        {
            Report("[pix-tool-set] probe: fence creation failed\n");
            return false;
        }
        HANDLE event = CreateEventW(nullptr, FALSE, FALSE, nullptr);
        queue->Signal(fence.Get(), 1);
        fence->SetEventOnCompletion(1, event);
        WaitForSingleObject(event, 30000);
        CloseHandle(event);

        void* mapped = nullptr;
        D3D12_RANGE range{ 0, static_cast<SIZE_T>(totalBytes) };
        if (FAILED(readback->Map(0, &range, &mapped)) || mapped == nullptr)
        {
            Report("[pix-tool-set] probe: map failed\n");
            return false;
        }

        wchar_t suffix[64]{};
        // The mip is part of the name: two mips of one resource are two different
        // images, and a shared filename made the second overwrite the first.
        if (mipLevel == 0)
        {
            swprintf_s(suffix, L"_%u.bin", static_cast<unsigned>(resourceId));
        }
        else
        {
            swprintf_s(suffix, L"_%u_mip%u.bin", static_cast<unsigned>(resourceId),
                       static_cast<unsigned>(mipLevel));
        }
        const std::wstring binPath = prefix + suffix;

        std::ofstream out(binPath.c_str(), std::ios::binary);
        out.write(static_cast<const char*>(mapped), static_cast<std::streamsize>(totalBytes));
        out.close();
        readback->Unmap(0, nullptr);

        // The sidecar exists so the reader never has to guess the layout: row pitch is
        // aligned and is not width * bytes-per-pixel. width/height are the *mip's*
        // dimensions from the footprint, not the mip-0 dimensions in desc, or the
        // reader would slice a 64x32 mip as if it were 1024x512.
        std::ofstream meta((binPath + L".txt").c_str());
        meta << "resource_id=" << static_cast<unsigned>(resourceId) << "\n"
             << "width=" << footprint.Footprint.Width << "\n"
             << "height=" << footprint.Footprint.Height << "\n"
             << "resource_width=" << desc.Width << "\n"
             << "resource_height=" << desc.Height << "\n"
             << "depth_or_array_size=" << desc.DepthOrArraySize << "\n"
             << "mip_levels=" << desc.MipLevels << "\n"
             << "mip=" << mipLevel << "\n"
             << "subresource=" << subresource << "\n"
             << "format=" << static_cast<int>(desc.Format) << "\n"
             << "footprint_format=" << static_cast<int>(footprint.Footprint.Format) << "\n"
             << "row_pitch=" << footprint.Footprint.RowPitch << "\n"
             << "rows=" << numRows << "\n"
             << "row_size_bytes=" << rowSizeBytes << "\n"
             << "total_bytes=" << totalBytes << "\n"
             << "state_before=" << static_cast<unsigned>(sourceState) << "\n";
        meta.close();

        char message[512]{};
        sprintf_s(message,
                  "[pix-tool-set] probe: resource %u wrote %llu bytes, %ux%u fmt=%d pitch=%u\n",
                  static_cast<unsigned>(resourceId),
                  static_cast<unsigned long long>(totalBytes),
                  static_cast<unsigned>(desc.Width),
                  static_cast<unsigned>(desc.Height),
                  static_cast<int>(desc.Format),
                  static_cast<unsigned>(footprint.Footprint.RowPitch));
        Report(message);
        return true;
    }
}

// Called once from RenderFrame(), after the recorded work has been submitted.
void PixToolSetProbeReadback()
{
    static bool done = false;
    if (done)
    {
        return;
    }
    done = true;

    const std::vector<ApiObjectId> targets = ProbeTargets();
    if (targets.empty())
    {
        return;
    }
    const std::wstring prefix = ProbeOutputPrefix();
    if (prefix.empty())
    {
        Report("[pix-tool-set] probe: PIXTS_PROBE_OUT is not set\n");
        return;
    }

    ID3D12CommandQueue* queue = FindDirectQueue();
    if (queue == nullptr)
    {
        Report("[pix-tool-set] probe: no DIRECT queue is tracked\n");
        return;
    }

    const D3D12_RESOURCE_STATES sourceState = ProbeSourceState();
    const UINT mipLevel = ProbeMipLevel();
    unsigned succeeded = 0;
    for (ApiObjectId resourceId : targets)
    {
        if (DumpOne(resourceId, prefix, sourceState, queue, mipLevel))
        {
            ++succeeded;
        }
    }

    // A sentinel written last, so the reader can tell "still running" from "finished
    // and produced nothing" without polling on a timeout alone.
    std::ofstream sentinel((prefix + L".done").c_str());
    sentinel << "dumped=" << succeeded << "\n"
             << "requested=" << targets.size() << "\n"
             << "mip=" << mipLevel << "\n";
    sentinel.close();
}
'''


# ======================================================================
# injecting and removing the probe
# ======================================================================
def _backup_path(target: Path) -> Path:
    """``RenderFrame.cpp`` -> ``RenderFrame.cpp.orig``, matching shader-edit-apply."""
    return target.with_suffix(target.suffix + ".orig")


def is_installed(export_dir: Path) -> bool:
    source = export_dir / PROBE_SOURCE_NAME
    if not source.exists():
        return False
    render = export_dir / "RenderFrame.cpp"
    lists = export_dir / "CMakeLists.txt"
    return (
        render.exists()
        and PROBE_FUNCTION in render.read_text(encoding="utf-8", errors="replace")
        and lists.exists()
        and PROBE_SOURCE_NAME in lists.read_text(encoding="utf-8", errors="replace")
    )


def install(export_dir: Path) -> dict[str, Any]:
    """Add the probe to the export, backing up every file first touched.

    Idempotent: an export that already carries the probe is left alone, which is what
    makes a second run skip the rebuild entirely.
    """
    from ..errors import PixToolError, not_found

    render = export_dir / "RenderFrame.cpp"
    lists = export_dir / "CMakeLists.txt"
    for required in (render, lists):
        if not required.exists():
            raise not_found(
                required.name,
                str(export_dir),
                "This export cannot host the probe; re-run session-open to regenerate it.",
            )

    if is_installed(export_dir):
        return {
            "action": "reused the probe already installed in this export",
            "already_installed": True,
            "probe_source": str(export_dir / PROBE_SOURCE_NAME),
            "files_modified": [],
            "rebuild_needed": False,
        }

    changed: list[str] = []
    backups: list[str] = []

    (export_dir / PROBE_SOURCE_NAME).write_text(PROBE_SOURCE, encoding="utf-8")
    changed.append(str(export_dir / PROBE_SOURCE_NAME))

    # --- RenderFrame.cpp: declare the probe and call it once per frame -------
    text = render.read_text(encoding="utf-8", errors="replace")
    anchor = "g_perFrameBuffers.clear();"
    if anchor not in text:
        raise PixToolError(
            code="probe_anchor_missing",
            message="RenderFrame.cpp does not end the frame the way this probe expects.",
            stage="export",
            paths=[str(render)],
            suggestion=(
                "The probe is injected before g_perFrameBuffers.clear(); that statement "
                "was not found, so nothing was changed."
            ),
        )
    backup = _backup_path(render)
    if not backup.exists():
        shutil.copy2(render, backup)
        backups.append(str(backup))

    declaration = f"{MARKER}\nvoid {PROBE_FUNCTION}();\n\n"
    body = text
    marker_line = f"void RenderFrame()"
    index = body.find(marker_line)
    body = body[:index] + declaration + body[index:] if index >= 0 else declaration + body

    call = (
        f"    {MARKER}\n"
        f"    // Runs after the recorded work has been submitted, so the UAV holds what\n"
        f"    // the dispatch wrote rather than what was uploaded.\n"
        f"    {PROBE_FUNCTION}();\n\n"
    )
    position = body.find(anchor)
    line_start = body.rfind("\n", 0, position) + 1
    body = body[:line_start] + call + body[line_start:]
    render.write_text(body, encoding="utf-8")
    changed.append(str(render))

    # --- CMakeLists.txt: an explicit source list, not a GLOB ----------------
    cmake_text = lists.read_text(encoding="utf-8", errors="replace")
    inserted = False
    out_lines: list[str] = []
    for line in cmake_text.splitlines(keepends=True):
        out_lines.append(line)
        if not inserted and line.strip() == "RenderFrame.cpp":
            indent = line[: len(line) - len(line.lstrip())]
            out_lines.append(f"{indent}{PROBE_SOURCE_NAME}\n")
            inserted = True
    if not inserted:
        # Undo the RenderFrame.cpp edit rather than leaving a source that cannot build.
        shutil.copy2(backup, render)
        (export_dir / PROBE_SOURCE_NAME).unlink(missing_ok=True)
        raise PixToolError(
            code="probe_cmake_anchor_missing",
            message="CMakeLists.txt has no RenderFrame.cpp entry to insert the probe after.",
            stage="export",
            paths=[str(lists)],
            suggestion="The export's source list is not shaped as expected; nothing was left changed.",
        )
    cmake_backup = _backup_path(lists)
    if not cmake_backup.exists():
        shutil.copy2(lists, cmake_backup)
        backups.append(str(cmake_backup))
    lists.write_text("".join(out_lines), encoding="utf-8")
    changed.append(str(lists))

    return {
        "action": "injected the readback probe into the export",
        "already_installed": False,
        "probe_source": str(export_dir / PROBE_SOURCE_NAME),
        "files_modified": changed,
        "backups": backups,
        "rebuild_needed": True,
    }


def restore(export_dir: Path) -> dict[str, Any]:
    """Undo the injection: restore each ``.orig`` and delete the probe source.

    Called on the way out unless the caller asked to keep the probe, because an
    injected file left behind silently is a change to the user's project that they
    did not make.
    """
    restored: list[str] = []
    removed: list[str] = []

    for name in ("RenderFrame.cpp", "CMakeLists.txt"):
        target = export_dir / name
        backup = _backup_path(target)
        if backup.exists() and target.exists():
            shutil.copy2(backup, target)
            backup.unlink()
            restored.append(str(target))

    probe = export_dir / PROBE_SOURCE_NAME
    if probe.exists():
        probe.unlink()
        removed.append(str(probe))

    return {
        "action": "restored the export to its state before injection",
        "files_restored": restored,
        "files_removed": removed,
        "left_behind": [],
    }


# ======================================================================
# reading back what the probe wrote
# ======================================================================
@dataclass(frozen=True, slots=True)
class ProbeDump:
    """One resource dumped by the probe, with the layout it was written in."""

    resource_id: int
    width: int
    height: int
    dxgi_format: int
    row_pitch: int
    rows: int
    row_size_bytes: int
    total_bytes: int
    subresource: int
    state_before: int
    bin_path: Path
    sidecar_path: Path
    mip: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "width": self.width,
            "height": self.height,
            "mip": self.mip,
            "dxgi_format": self.dxgi_format,
            "format": format_name(self.dxgi_format),
            "row_pitch": self.row_pitch,
            "rows": self.rows,
            "row_size_bytes": self.row_size_bytes,
            "total_bytes": self.total_bytes,
            "subresource": self.subresource,
            "row_padding_bytes": max(self.row_pitch - self.row_size_bytes, 0),
            "source_state": self.state_before,
            "bin_path": str(self.bin_path),
            "sidecar_path": str(self.sidecar_path),
        }


_KEY_VALUE = re.compile(r"^(\w+)=(-?\d+)\s*$")


def read_sidecar(bin_path: Path) -> ProbeDump:
    """Parse the layout sidecar the probe writes beside each dump.

    The sidecar exists so nothing here has to be inferred: row pitch is aligned to a
    hardware boundary and is not ``width * bytes_per_pixel`` (1532 pixels of
    R10G10B10A2 measure 6128 bytes but ship with a pitch of 6144).
    """
    from ..errors import not_found

    sidecar = Path(str(bin_path) + ".txt")
    if not sidecar.exists():
        raise not_found(
            "probe sidecar",
            str(sidecar),
            "The probe writes <dump>.bin.txt beside every dump; without it the layout "
            "is unknown and will not be guessed.",
        )
    fields: dict[str, int] = {}
    for line in sidecar.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _KEY_VALUE.match(line.strip())
        if match:
            fields[match.group(1)] = int(match.group(2))

    return ProbeDump(
        resource_id=fields.get("resource_id", 0),
        width=fields.get("width", 0),
        height=fields.get("height", 0),
        dxgi_format=fields.get("footprint_format") or fields.get("format", 0),
        row_pitch=fields.get("row_pitch", 0),
        rows=fields.get("rows", 0),
        row_size_bytes=fields.get("row_size_bytes", 0),
        total_bytes=fields.get("total_bytes", 0),
        subresource=fields.get("subresource", 0),
        state_before=fields.get("state_before", STATE_UNORDERED_ACCESS),
        mip=fields.get("mip", 0),
        bin_path=Path(bin_path),
        sidecar_path=sidecar,
    )


def format_name(dxgi_format: int) -> str:
    spec = dds.DXGI_FORMATS.get(dxgi_format)
    return spec[0] if spec else f"DXGI_FORMAT_{dxgi_format}"


def format_spec(dxgi_format: int) -> tuple[str, int, int, str] | None:
    """(name, bytes per pixel, component count, struct code), or None if unsupported."""
    return dds.DXGI_FORMATS.get(dxgi_format)


def depad(blob: bytes, dump: ProbeDump) -> bytes:
    """Drop the alignment padding at the end of every row.

    A short read yields the rows that are present rather than nothing, so a truncated
    dump can be reported as truncated instead of failing opaquely.
    """
    if dump.row_pitch <= 0 or dump.row_size_bytes <= 0:
        return b""
    packed = bytearray()
    for y in range(dump.rows):
        start = y * dump.row_pitch
        end = start + dump.row_size_bytes
        if end > len(blob):
            break
        packed += blob[start:end]
    return bytes(packed)


def as_image(packed: bytes, dump: ProbeDump) -> dds.DdsImage:
    """Wrap de-padded bytes in a DdsImage so its decoders can be reused.

    ``engine/dds.py`` already unpacks every format that matters here, including the
    bit-packed ones (R10G10B10A2_UNORM's channels are fields inside one integer, and
    R11G11B10_FLOAT's are three unsigned floats with no sign bit). Duplicating that
    would be two implementations of the same fiddly bit twiddling.
    """
    from ..errors import unsupported

    spec = format_spec(dump.dxgi_format)
    if spec is None:
        raise unsupported(
            f"decoding DXGI format {dump.dxgi_format}",
            "this format has no decoder in engine/dds.py",
            "The raw .bin and its layout sidecar are still written, so it can be "
            "decoded by hand.",
        )
    name, bpp, components, code = spec
    rows = len(packed) // dump.row_size_bytes if dump.row_size_bytes else 0
    return dds.DdsImage(
        width=dump.width,
        height=min(rows, dump.height),
        dxgi_format=dump.dxgi_format,
        format_name=name,
        bytes_per_pixel=bpp,
        component_count=components,
        component_code=code,
        pixel_offset=0,
        data=packed,
    )


def channel_names(image: dds.DdsImage) -> list[str]:
    """R/G/B/A labels for however many channels the decoded pixels carry."""
    letters = re.findall(r"([RGBA])\d+", image.format_name)
    if letters:
        return letters
    return ["R", "G", "B", "A"][: max(image.component_count, 1)]


def _as_list(value: Any) -> list[float]:
    return list(value) if isinstance(value, (list, tuple)) else [float(value)]


def statistics(
    image: dds.DdsImage, *, max_samples: int = 250_000
) -> dict[str, Any]:
    """Per-channel min/max/mean over the decoded pixels.

    Sampled on a stride when the surface is large, because a 1.17 M pixel texture
    decoded one pixel at a time in Python is slow enough to matter and a strided
    sample answers "what did the dispatch write" just as well. The stride is reported
    so the numbers are never mistaken for an exhaustive scan.
    """
    total = image.width * image.height
    if total <= 0:
        return {"sampled": 0}
    step = max(total // max_samples, 1)
    names = channel_names(image)

    count = 0
    sums: list[float] = []
    lows: list[float] = []
    highs: list[float] = []
    nonzero = 0

    for index in range(0, total, step):
        values = _as_list(image.pixel(index % image.width, index // image.width))
        if not sums:
            sums = [0.0] * len(values)
            lows = [float("inf")] * len(values)
            highs = [float("-inf")] * len(values)
        for channel, value in enumerate(values):
            if value != value:  # NaN
                continue
            sums[channel] += value
            lows[channel] = min(lows[channel], value)
            highs[channel] = max(highs[channel], value)
        if any(value for value in values):
            nonzero += 1
        count += 1

    if not count or not sums:
        return {"sampled": 0}

    normalised = image.format_name.endswith(("UNORM", "UNORM_SRGB"))
    channels = []
    for index, name in enumerate(names[: len(sums)]):
        mean = sums[index] / count
        entry: dict[str, Any] = {
            "channel": name,
            "min": round(lows[index], 6),
            "max": round(highs[index], 6),
            "mean": round(mean, 6),
        }
        if normalised:
            # 0..255 is how these values are read and compared by eye, and it is the
            # scale the PNG carries, so both are reported rather than one.
            entry["mean_8bit"] = round(mean * 255.0, 2)
            entry["min_8bit"] = round(lows[index] * 255.0, 2)
            entry["max_8bit"] = round(highs[index] * 255.0, 2)
        channels.append(entry)

    return {
        "sampled": count,
        "sample_step": step,
        "exhaustive": step == 1,
        "pixel_count": total,
        "nonzero_pixels_sampled": nonzero,
        "nonzero_share_percent": round(100.0 * nonzero / count, 2),
        "values_are": (
            "normalised 0..1 (UNORM)" if normalised else "raw decoded values"
        ),
        "channels": channels,
    }


def sample_pixels(
    image: dds.DdsImage, count: int, *, stride: int | None = None
) -> list[dict[str, Any]]:
    """A handful of pixel values with their coordinates, for eyeballing."""
    total = image.width * image.height
    if total <= 0 or count <= 0:
        return []
    step = stride if stride and stride > 0 else max(total // count, 1)
    out: list[dict[str, Any]] = []
    for index in range(0, total, step):
        if len(out) >= count:
            break
        x, y = index % image.width, index // image.width
        out.append(
            {
                "x": x,
                "y": y,
                "value": [round(v, 6) for v in _as_list(image.pixel(x, y))],
            }
        )
    return out


def to_rgb_png(image: dds.DdsImage) -> tuple[bytes, dict[str, Any]] | None:
    """Encode the decoded surface as an 8-bit RGB PNG, plus how it was mapped.

    UNORM data is already 0..1 so it maps straight onto 0..255; anything else is
    contrast stretched over the values actually present, because a float or uint
    surface has no inherent display range. Which of the two happened is returned so
    the picture can be read quantitatively rather than admired.
    """
    from . import screencap

    width, height = image.width, image.height
    if width <= 0 or height <= 0:
        return None

    normalised = image.format_name.endswith(("UNORM", "UNORM_SRGB"))
    rows: list[list[list[float]]] = []
    low, high = float("inf"), float("-inf")
    for y in range(height):
        row = [_as_list(image.pixel(x, y)) for x in range(width)]
        rows.append(row)
        if not normalised:
            for values in row:
                for value in values[:3]:
                    if value == value and abs(value) != float("inf"):
                        low = min(low, value)
                        high = max(high, value)

    if normalised:
        low, high = 0.0, 1.0
    if low > high:
        low, high = 0.0, 1.0
    span = (high - low) or 1.0

    bgra = bytearray(width * height * 4)
    offset = 0
    for row in rows:
        for values in row:
            if len(values) >= 3:
                triple = values[:3]
            else:
                triple = [values[0]] * 3  # a single channel shown as grey
            scaled = []
            for value in triple:
                if value != value:
                    scaled.append(0)
                    continue
                level = int((value - low) / span * 255.0)
                scaled.append(0 if level < 0 else (255 if level > 255 else level))
            bgra[offset] = scaled[2]
            bgra[offset + 1] = scaled[1]
            bgra[offset + 2] = scaled[0]
            bgra[offset + 3] = 255
            offset += 4

    blob = screencap.encode_png_rgb(bgra, width, height)
    # `component_count` counts storage units, not colour channels: a bit-packed format
    # like R10G10B10A2_UNORM is read as one integer and only becomes four values after
    # `_unpack_r10g10b10a2`, so it is 1 here while the image really is RGBA. Reporting it
    # directly labelled a colour PNG "single channel as grey". Ask the decoded rows how
    # many channels they actually carry, which is what the pixel loop above keys off too.
    decoded_channels = 0
    for row in rows:
        if row:
            decoded_channels = len(row[0])
            break
    mapping = {
        "width": width,
        "height": height,
        "decoded_channels": decoded_channels,
        "channels_shown": (
            "RGB" if decoded_channels >= 3 else "single channel as grey"
        ),
        "mapping": (
            "UNORM 0..1 scaled to 0..255"
            if normalised
            else f"contrast stretched from {round(low, 6)} to {round(high, 6)}"
        ),
    }
    return blob, mapping


def summarise_probe_log(prefix: Path) -> dict[str, Any]:
    """Read the sentinel the probe writes last, so 'finished' is a fact not a timeout."""
    sentinel = Path(str(prefix) + ".done")
    if not sentinel.exists():
        return {"finished": False}
    out: dict[str, Any] = {"finished": True, "sentinel": str(sentinel)}
    for line in sentinel.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _KEY_VALUE.match(line.strip())
        if match:
            out[match.group(1)] = int(match.group(2))
    return out
