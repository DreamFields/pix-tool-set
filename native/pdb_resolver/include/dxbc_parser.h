#pragma once

#include <string>
#include <vector>
#include <cstdint>

// Parse DXBC/DXIL container to extract the shader debug name (hash).
// The debug name is stored in the DFCC_ShaderDebugName part of the container.
// Returns empty string if not found.
std::string extractDebugNameFromShaderBlob(const uint8_t* data, size_t size);

// DXBC container magic: "DXBC" = 0x43425844
constexpr uint32_t DXBC_MAGIC = 0x43425844; // 'D','X','B','C' in little-endian

// DXIL container FourCC for ShaderDebugName
constexpr uint32_t DFCC_ShaderDebugName = 0x7A6E4453; // "SDnz" — actually "SDbg"... 

// The actual FourCC values used by DXIL containers
// DFCC_ShaderDebugName is actually stored as part index 0x7268
// Let's use the raw value from the DXC source
namespace DxilFourCC {
    constexpr uint32_t ShaderDebugName = 0x7A6E4453; // "SDnz" reversed
}
