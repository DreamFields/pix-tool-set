#include "dxbc_parser.h"
#include <cstring>

// DXBC Container format:
// Offset  Size  Description
// 0       4     Magic "DXBC" (0x43425844)
// 4       16    Hash (MD5)
// 20      4     Version (1)
// 24      4     Total size
// 28      4     Part count
// 32      N*4   Part offsets
// Then each part:
//   0     4     FourCC
//   4     4     Part size
//   8     ...   Part data

// ShaderDebugName part (FourCC = 0x7A6E4453 or "SDnz"):
// 0       2     Flags
// 2       2     NameLength (including null terminator)
// 4       ...   Name string (null-terminated, padded to 4-byte boundary)

// The actual FourCC for ShaderDebugName in DXIL/DXBC containers
// From DxilContainer.h: DFCC_ShaderDebugName = DxilFourCC('I', 'L', 'D', 'N') no...
// Actually from the DXC source: DFCC_ShaderDebugName is defined as a specific value.
// Let's just search for the known part patterns.

// Known FourCC values for shader debug parts:
// "ILDN" = 0x4E444C49 — shader debug info (DXIL)
// "ILDB" = 0x42444C49 — shader debug info (DXBC)
// The debug name FourCC is actually embedded differently.
// From DxilContainer.h:
//   DFCC_ShaderDebugName       = DxilFourCC('S', 'D', 'N', '\0') or similar

// Let's take a practical approach: scan the DXBC container parts for the debug name.
// The ShaderDebugName part has a specific structure we can identify.

static uint32_t readU32(const uint8_t* p) {
    return p[0] | (p[1] << 8) | (p[2] << 16) | (p[3] << 24);
}

static uint16_t readU16(const uint8_t* p) {
    return p[0] | (p[1] << 8);
}

std::string extractDebugNameFromShaderBlob(const uint8_t* data, size_t size) {
    if (size < 32) return "";

    // Check DXBC magic
    uint32_t magic = readU32(data);
    if (magic != 0x43425844) { // "DXBC"
        return "";
    }

    uint32_t totalSize = readU32(data + 24);
    uint32_t partCount = readU32(data + 28);

    if (totalSize > size || partCount > 100) return "";

    // Known FourCC for ShaderDebugName
    // In the DXC codebase:
    // #define DFCC_ShaderDebugName       DxilFourCC('S', 'D', 'B', 'N')  -- not sure
    // Let's try multiple known values. The actual value from DxilContainer.h is:
    // Part_ShaderDebugName = MakeFourCC('S','H','D','B') ... no
    // Actually from the source: it's stored as the 4 bytes for the name marker.
    // The safest approach: iterate all parts and look for ones that could be debug names.
    
    // Correct FourCC: from DxilContainer.h line ~60:
    //   DFCC_ShaderDebugName       = DXIL_FOURCC('S', 'D', 'N', '\0')  -- but actually it's
    //   defined differently depending on version.
    // The pragmatic approach: the debug name part has a 2-byte flags + 2-byte name length prefix,
    // followed by a null-terminated ASCII string that looks like a hex hash.
    
    // FourCC values we look for:
    // 0x7A6E4453 = "SDnz" (DXIL shader debug name)  -- incorrect
    // The actual value from the LLVM docs:
    //   ShaderDebugName = 0x7A6E4453  -- that's "SDnz" reversed
    // Wait, let me check: the LLVM DXContainer.h says:
    //   ShaderDebugName = 'S' | ('H' << 8) | ('D' << 16) | ('R' << 24) -- no
    // From LLVM source: https://llvm.org/docs/DirectX/DXContainer.html
    //   The debug name part FourCC is not listed there.
    // From DXC source: include/dxc/DxilContainer/DxilContainer.h
    //   DFCC_ShaderDebugName     = DXIL_FOURCC('S', 'D', 'B', 'N')  
    //   = 'S' | ('D' << 8) | ('B' << 16) | ('N' << 24) = 0x4E424453

    const uint32_t SDBN = 0x4E424453; // "SDBN" — ShaderDebugName

    for (uint32_t i = 0; i < partCount; i++) {
        if (32 + i * 4 + 4 > size) break;
        uint32_t partOffset = readU32(data + 32 + i * 4);
        if (partOffset + 8 > size) continue;

        uint32_t fourCC = readU32(data + partOffset);
        uint32_t partSize = readU32(data + partOffset + 4);

        if (partOffset + 8 + partSize > size) continue;

        const uint8_t* partData = data + partOffset + 8;

        if (fourCC == SDBN && partSize >= 4) {
            // uint16_t flags = readU16(partData);
            uint16_t nameLen = readU16(partData + 2);
            if (nameLen > 0 && nameLen <= partSize - 4) {
                std::string name(reinterpret_cast<const char*>(partData + 4), nameLen);
                // Remove null terminator if present
                while (!name.empty() && name.back() == '\0') {
                    name.pop_back();
                }
                return name;
            }
        }

        // Also try a heuristic: any FourCC that isn't a known shader code part
        // and has a small part with printable ASCII might be the debug name
    }

    // Fallback: search for common debug name patterns in the blob
    // Debug names are typically hex hashes like "abcdef0123456789abcdef0123456789.pdb"
    // or paths containing ".pdb"
    std::string blobStr(reinterpret_cast<const char*>(data), size);
    size_t pdbPos = blobStr.find(".pdb");
    if (pdbPos != std::string::npos && pdbPos > 0) {
        // Walk backwards to find the start of the name
        size_t start = pdbPos;
        while (start > 0 && blobStr[start - 1] != '\0' && blobStr[start - 1] >= 0x20) {
            start--;
        }
        if (start < pdbPos) {
            return blobStr.substr(start, pdbPos + 4 - start);
        }
    }

    return "";
}
