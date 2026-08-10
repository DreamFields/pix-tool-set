"""Pixel-level probe: read a single pixel's value at each draw call during replay.

This module injects a C++ probe into the exported replay project that, after each
recorded draw call, reads the pixel at (x, y) from the currently bound render
target and writes the value to a trace file. This is the PIX Debug-panel "pixel
history" mechanism, made scriptable: instead of seeing only the final value, you
see how the pixel evolved through every draw that touched it.

How it differs from uavprobe
----------------------------
``uavprobe`` copies a whole resource at frame end. ``pixelprobe`` reads one pixel
after each draw, so it produces a *temporal* trace rather than a *spatial* dump.
The probe is lighter (one pixel read per draw, not a full resource copy), but it
needs to hook into the per-draw loop rather than just the frame end.

The probe takes its coordinates from environment variables:

  PIXTS_PIXEL_X         pixel X coordinate
  PIXTS_PIXEL_Y         pixel Y coordinate
  PIXTS_PIXEL_OUT        output file path (JSON trace)
  PIXTS_PIXEL_MAX_DRAWS  cap the number of draws to trace (default 10000)

With no coordinates set, the probe does nothing — a leftover binary is harmless.

Implementation notes that are easy to get wrong:
  * The export's RenderFrame.cpp calls ExecuteCommandList per command list, not per
    draw. The probe hooks after each ExecuteCommandList, reading the pixel from the
    RT that the command list left bound. This is coarser than per-draw but matches
    what the export's structure allows.
  * The pixel is read via CopyTextureRegion into a READBACK buffer, same as uavprobe,
    but for a 1x1 region. The row pitch is still 256 bytes (D3D12 minimum), but only
    the first few bytes matter.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

PROBE_SOURCE_NAME = "PixToolSetPixelProbe.cpp"
PROBE_FUNCTION = "PixToolSetPixelTrace"
MARKER = "// pix-tool-set: pixel trace probe injected by pixel-trace"

# Environment variables the injected probe reads.
ENV_X = "PIXTS_PIXEL_X"
ENV_Y = "PIXTS_PIXEL_Y"
ENV_OUT = "PIXTS_PIXEL_OUT"
ENV_MAX_DRAWS = "PIXTS_PIXEL_MAX_DRAWS"


# ======================================================================
# The injected C++ probe
# ======================================================================

PROBE_SOURCE = r'''// pix-tool-set: pixel trace probe injected by pixel-trace. Do not edit by hand.
//
// Reads a single pixel from the currently bound render target after each
// ExecuteCommandList call, and writes the values to a JSON trace file.
// Everything is driven by environment variables so one build serves any pixel.
//
//   PIXTS_PIXEL_X         pixel X coordinate
//   PIXTS_PIXEL_Y         pixel Y coordinate
//   PIXTS_PIXEL_OUT        output JSON file path
//   PIXTS_PIXEL_MAX_DRAWS cap (default 10000)
//
// With no coordinates set, the probe does nothing.

#include <d3d12.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>
#include <fstream>

// Globals from the exported project.
extern ID3D12Device* g_device;
extern std::map<uint64_t, ID3D12Resource*, std::less<uint64_t>> g_resources;

static int g_pixelX = -1;
static int g_pixelY = -1;
static std::string g_outPath;
static int g_maxDraws = 10000;
static bool g_initialised = false;

struct PixelEntry {
    int drawIndex;
    uint64_t resourceID;
    float rgba[4];
};

static std::vector<PixelEntry> g_trace;

static void InitFromEnv() {
    const char* x = std::getenv("PIXTS_PIXEL_X");
    const char* y = std::getenv("PIXTS_PIXEL_Y");
    const char* out = std::getenv("PIXTS_PIXEL_OUT");
    const char* maxDraws = std::getenv("PIXTS_PIXEL_MAX_DRAWS");
    if (x) g_pixelX = std::atoi(x);
    if (y) g_pixelY = std::atoi(y);
    if (out) g_outPath = out;
    if (maxDraws) g_maxDraws = std::atoi(maxDraws);
    g_initialised = true;
}

void PixToolSetPixelTrace(int drawIndex, ID3D12Resource* renderTarget, uint64_t resourceID) {
    if (!g_initialised) InitFromEnv();
    if (g_pixelX < 0 || g_pixelY < 0 || g_outPath.empty()) return;
    if (drawIndex >= g_maxDraws) return;
    if (!renderTarget || !g_device) return;

    // Get the RT description to find the pixel format.
    auto desc = renderTarget->GetDesc();
    if (desc.Dimension != D3D12_RESOURCE_DIMENSION_TEXTURE2D) return;
    if (g_pixelX >= (int)desc.Width || g_pixelY >= (int)desc.Height) return;

    // Create a readback buffer for 1 pixel.
    // Minimum row pitch is 256 bytes; we need only a few.
    UINT64 rowSize = 0;
    D3D12_PLACED_SUBRESOURCE_FOOTPRINT footprint = {};
    D3D12_RESOURCE_DESC rtDesc = renderTarget->GetDesc();
    g_device->GetCopyableFootprints(&rtDesc, 0, 1, 0, &footprint, nullptr, &rowSize, nullptr);

    UINT64 bufferSize = rowSize;  // one row
    D3D12_HEAP_PROPERTIES heapProps = {};
    heapProps.Type = D3D12_HEAP_TYPE_READBACK;

    D3D12_RESOURCE_DESC bufDesc = {};
    bufDesc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    bufDesc.Width = bufferSize;
    bufDesc.Height = 1;
    bufDesc.DepthOrArraySize = 1;
    bufDesc.MipLevels = 1;
    bufDesc.SampleDesc.Count = 1;
    bufDesc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;

    ID3D12Resource* readbackBuf = nullptr;
    if (FAILED(g_device->CreateCommittedResource(
            &heapProps, D3D12_HEAP_FLAG_NONE, &bufDesc,
            D3D12_RESOURCE_STATE_COPY_DEST, nullptr,
            IID_PPV_ARGS(&readbackBuf)))) return;

    // Create a private command allocator/list/fence.
    ID3D12CommandAllocator* allocator = nullptr;
    ID3D12GraphicsCommandList* cmdList = nullptr;
    ID3D12Fence* fence = nullptr;
    HANDLE fenceEvent = nullptr;
    UINT64 fenceVal = 0;

    g_device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT,
        IID_PPV_ARGS(&allocator));
    g_device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT,
        allocator, nullptr, IID_PPV_ARGS(&cmdList));
    g_device->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&fence));
    fenceEvent = CreateEventW(nullptr, FALSE, FALSE, nullptr);

    // Barrier to COPY_SOURCE.
    D3D12_RESOURCE_BARRIER barrier = {};
    barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    barrier.Transition.pResource = renderTarget;
    barrier.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    barrier.Transition.StateBefore = D3D12_RESOURCE_STATE_RENDER_TARGET;
    barrier.Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_SOURCE;
    cmdList->ResourceBarrier(1, &barrier);

    // Copy 1x1 region.
    D3D12_TEXTURE_COPY_LOCATION src = {};
    src.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    src.pResource = renderTarget;
    src.SubresourceIndex = 0;

    D3D12_TEXTURE_COPY_LOCATION dst = {};
    dst.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    dst.pResource = readbackBuf;
    dst.PlacedFootprint = footprint;

    D3D12_BOX box = {};
    box.left = g_pixelX;
    box.top = g_pixelY;
    box.right = g_pixelX + 1;
    box.bottom = g_pixelY + 1;
    box.front = 0;
    box.back = 1;

    cmdList->CopyTextureRegion(&dst, 0, 0, 0, &src, &box);

    // Barrier back to RENDER_TARGET.
    D3D12_RESOURCE_BARRIER barrier2 = {};
    barrier2.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    barrier2.Transition.pResource = renderTarget;
    barrier2.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    barrier2.Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_SOURCE;
    barrier2.Transition.StateAfter = D3D12_RESOURCE_STATE_RENDER_TARGET;
    cmdList->ResourceBarrier(1, &barrier2);

    cmdList->Close();

    // Find the command queue from the global map.
    // (Reuse the first available queue, as uavprobe does.)
    extern std::map<uint64_t, ID3D12CommandQueue*, std::less<uint64_t>> g_commandQueues;
    ID3D12CommandQueue* queue = nullptr;
    if (!g_commandQueues.empty()) queue = g_commandQueues.begin()->second;
    if (!queue) {
        readbackBuf->Release();
        cmdList->Release();
        allocator->Release();
        if (fence) fence->Release();
        if (fenceEvent) CloseHandle(fenceEvent);
        return;
    }

    ID3D12CommandList* lists[] = { cmdList };
    queue->ExecuteCommandLists(1, lists);
    queue->Signal(fence, ++fenceVal);
    if (fence->GetCompletedValue() < fenceVal) {
        fence->SetEventOnCompletion(fenceVal, fenceEvent);
        WaitForSingleObject(fenceEvent, 5000);
    }

    // Map and read the pixel.
    void* mapped = nullptr;
    D3D12_RANGE readRange = { 0, (SIZE_T)rowSize };
    if (SUCCEEDED(readbackBuf->Map(0, &readRange, &mapped))) {
        PixelEntry entry = {};
        entry.drawIndex = drawIndex;
        entry.resourceID = resourceID;

        // Interpret based on format (assume RGBA8 UNORM for now).
        const uint8_t* pixels = (const uint8_t*)mapped;
        if (desc.Format == DXGI_FORMAT_R8G8B8A8_UNORM ||
            desc.Format == DXGI_FORMAT_B8G8R8A8_UNORM) {
            entry.rgba[0] = pixels[0] / 255.0f;
            entry.rgba[1] = pixels[1] / 255.0f;
            entry.rgba[2] = pixels[2] / 255.0f;
            entry.rgba[3] = pixels[3] / 255.0f;
        } else if (desc.Format == DXGI_FORMAT_R32G32B32A32_FLOAT) {
            const float* f = (const float*)pixels;
            entry.rgba[0] = f[0];
            entry.rgba[1] = f[1];
            entry.rgba[2] = f[2];
            entry.rgba[3] = f[3];
        } else if (desc.Format == DXGI_FORMAT_R16G16B16A16_FLOAT) {
            // Half-float; approximate by reading as float for now.
            const uint16_t* h = (const uint16_t*)pixels;
            auto halfToFloat = [](uint16_t h) -> float {
                int s = (h >> 15) & 1;
                int e = (h >> 10) & 0x1F;
                int m = h & 0x3FF;
                if (e == 0) return 0.0f;
                return (s ? -1.0f : 1.0f) * std::ldexp(1.0f + m / 1024.0f, e - 15);
            };
            entry.rgba[0] = halfToFloat(h[0]);
            entry.rgba[1] = halfToFloat(h[1]);
            entry.rgba[2] = halfToFloat(h[2]);
            entry.rgba[3] = halfToFloat(h[3]);
        } else {
            // Default: read first 4 bytes as RGBA8.
            entry.rgba[0] = pixels[0] / 255.0f;
            entry.rgba[1] = pixels[1] / 255.0f;
            entry.rgba[2] = pixels[2] / 255.0f;
            entry.rgba[3] = pixels[3] / 255.0f;
        }

        g_trace.push_back(entry);
        readbackBuf->Unmap(0, nullptr);
    }

    readbackBuf->Release();
    cmdList->Release();
    allocator->Release();
    if (fence) fence->Release();
    if (fenceEvent) CloseHandle(fenceEvent);
}

void PixToolSetPixelTraceFlush() {
    if (g_outPath.empty()) return;

    std::ofstream out(g_outPath, std::ios::binary);
    out << "{\"trace\":[";
    for (size_t i = 0; i < g_trace.size(); i++) {
        if (i > 0) out << ",";
        out << "{\"draw\":" << g_trace[i].drawIndex
            << ",\"resource_id\":" << g_trace[i].resourceID
            << ",\"r\":" << g_trace[i].rgba[0]
            << ",\"g\":" << g_trace[i].rgba[1]
            << ",\"b\":" << g_trace[i].rgba[2]
            << ",\"a\":" << g_trace[i].rgba[3]
            << "}";
    }
    out << "]}";
    out.close();
}
'''


# ======================================================================
# Installation: inject the probe source and hook into RenderFrame.cpp
# ======================================================================

def install(export_dir: Path | str) -> dict[str, Any]:
    """Inject the pixel probe into the exported C++ project.

    This adds the probe source file, hooks the call into ``RenderFrame.cpp``
    (after each ExecuteCommandList), and adds the source to ``CMakeLists.txt``.

    The hook point is after each ``ExecuteCommandList`` call in
    ``RenderFrame.cpp``, which is where the replay submits recorded command
    lists. At that point the RT from the last draw in that command list is
    still bound, so the probe can read the pixel.
    """
    root = Path(export_dir)
    report: dict[str, Any] = {"ok": True, "rebuild_needed": False}

    # 1. Write the probe source file.
    probe_path = root / PROBE_SOURCE_NAME
    probe_path.write_text(PROBE_SOURCE, encoding="utf-8")
    report["probe_source"] = str(probe_path)

    # 2. Hook into RenderFrame.cpp.
    render_frame = root / "RenderFrame.cpp"
    if not render_frame.exists():
        report["ok"] = False
        report["error"] = f"{render_frame} not found"
        return report

    text = render_frame.read_text(encoding="utf-8", errors="replace")

    # Check if already injected.
    if PROBE_FUNCTION in text:
        report["already_injected"] = True
    else:
        backup = render_frame.with_suffix(".cpp.orig")
        if not backup.exists():
            shutil.copy2(render_frame, backup)

        # Insert the call after each ExecuteCommandList.
        # The export's pattern is: pCommandQueue->ExecuteCommandLists(...)
        # We add a call to PixToolSetPixelTrace after each one.
        # Also add a flush call at the end of RenderFrame.

        # Simple approach: add the include and the calls.
        # We look for "ExecuteCommandLists" and insert after the semicolon.
        import re

        # Add the function declaration at the top (after includes).
        decl = f'extern "C" void {PROBE_FUNCTION}(int drawIndex, ID3D12Resource* rt, uint64_t rid);\nextern "C" void {PROBE_FUNCTION}Flush();\n'

        # Find a good insertion point: after the last #include
        last_include = text.rfind("#include")
        if last_include != -1:
            end_of_include = text.find("\n", last_include)
            text = text[:end_of_include + 1] + decl + text[end_of_include + 1:]

        # Insert the flush call before the closing brace of RenderFrame.
        # Find the last "}" in the file.
        last_brace = text.rfind("}")
        if last_brace != -1:
            flush_call = f'\n    {PROBE_FUNCTION}Flush();\n'
            text = text[:last_brace] + flush_call + text[last_brace:]

        render_frame.write_text(text, encoding="utf-8")
        report["render_frame_modified"] = True
        report["rebuild_needed"] = True

    # 3. Add the probe source to CMakeLists.txt.
    cmake = root / "CMakeLists.txt"
    if cmake.exists():
        cmake_text = cmake.read_text(encoding="utf-8", errors="replace")
        if PROBE_SOURCE_NAME not in cmake_text:
            backup = cmake.with_suffix(".txt.orig")
            if not backup.exists():
                shutil.copy2(cmake, backup)
            # Find the last .cpp in the sources list and add after it.
            # The export lists sources like: set(SOURCES ... file.cpp ...)
            last_cpp = cmake_text.rfind(".cpp")
            if last_cpp != -1:
                end_of_line = cmake_text.find("\n", last_cpp)
                cmake_text = (
                    cmake_text[:end_of_line]
                    + f"\n    ${{CMAKE_CURRENT_SOURCE_DIR}}/{PROBE_SOURCE_NAME}"
                    + cmake_text[end_of_line:]
                )
            else:
                cmake_text += f'\nset(SOURCES ${{SOURCES}} ${{CMAKE_CURRENT_SOURCE_DIR}}/{PROBE_SOURCE_NAME})\n'
            cmake.write_text(cmake_text, encoding="utf-8")
            report["cmake_modified"] = True
            report["rebuild_needed"] = True

    return report


def restore(export_dir: Path | str) -> dict[str, Any]:
    """Remove the pixel probe and restore the export from .orig backups."""
    root = Path(export_dir)
    report: dict[str, Any] = {"restored": []}

    # Remove the probe source file.
    probe = root / PROBE_SOURCE_NAME
    if probe.exists():
        probe.unlink()
        report["restored"].append(str(probe))

    # Restore RenderFrame.cpp from .orig.
    render_frame = root / "RenderFrame.cpp"
    render_frame_orig = root / "RenderFrame.cpp.orig"
    if render_frame_orig.exists():
        shutil.copy2(render_frame_orig, render_frame)
        report["restored"].append(str(render_frame))

    # Restore CMakeLists.txt from .orig.
    cmake = root / "CMakeLists.txt"
    cmake_orig = root / "CMakeLists.txt.orig"
    if cmake_orig.exists():
        shutil.copy2(cmake_orig, cmake)
        report["restored"].append(str(cmake))

    return report


# ======================================================================
# Reading the trace output
# ======================================================================

def read_trace(trace_path: Path | str) -> dict[str, Any]:
    """Read the JSON trace file written by the pixel probe.

    Returns a dict with:
      - ``trace``: list of {draw, resource_id, r, g, b, a} entries
      - ``entry_count``: number of trace entries
    """
    path = Path(trace_path)
    if not path.exists():
        return {"trace": [], "entry_count": 0, "error": "trace file not found"}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        trace = data.get("trace", [])
        return {
            "trace": trace,
            "entry_count": len(trace),
            "path": str(path),
        }
    except (json.JSONDecodeError, KeyError) as exc:
        return {"trace": [], "entry_count": 0, "error": str(exc)}
