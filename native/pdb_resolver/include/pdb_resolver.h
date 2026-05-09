#pragma once

#include <string>
#include <vector>

struct ShaderSourceFile {
    std::string name;
    std::string content;
};

struct PdbResolveResult {
    bool success = false;
    std::string error;

    // Shader metadata
    std::string entryPoint;
    std::string targetProfile;
    std::string mainFileName;
    std::string name;
    bool isFullPdb = false;

    // Sources embedded in PDB
    std::vector<ShaderSourceFile> sources;

    // Compile flags / args / defines
    std::vector<std::string> flags;
    std::vector<std::string> args;
    std::vector<std::pair<std::string, std::string>> argPairs;
    std::vector<std::string> defines;

    // Hash (hex string)
    std::string hashHex;
};

// Initialize dxcompiler.dll — must be called before other functions.
// dxcPath: optional explicit path to dxcompiler.dll (empty = auto-discover)
bool initDxCompiler(const std::string& dxcPath = "");

// Resolve a shader PDB file and extract HLSL sources + metadata.
PdbResolveResult resolvePdb(const std::string& pdbFilePath);

// Resolve a shader binary blob: extract debug name, then search for PDB in given dirs.
PdbResolveResult resolveShaderBlob(const std::string& blobFilePath,
                                    const std::vector<std::string>& pdbSearchPaths);

// Get the currently loaded dxcompiler.dll path
std::string getDxCompilerPath();
