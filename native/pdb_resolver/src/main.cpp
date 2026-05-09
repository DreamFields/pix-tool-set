// pdb-resolver: Extract HLSL source code from shader PDB files.
//
// Usage:
//   pdb-resolver.exe resolve-pdb <pdb-file>
//   pdb-resolver.exe resolve-blob <shader-blob> --pdb-paths=<dir1;dir2;...>
//   pdb-resolver.exe batch-resolve-pdb <dir-with-pdbs>
//   pdb-resolver.exe extract-debug-name <shader-blob>
//   pdb-resolver.exe info
//
// Output: JSON to stdout.

#include "pdb_resolver.h"
#include "dxbc_parser.h"
#include "json_writer.h"

#include <windows.h>
#include <iostream>
#include <fstream>
#include <filesystem>
#include <string>
#include <vector>
#include <algorithm>

namespace fs = std::filesystem;

// --------------------------------------------------------------------------
// Output result as JSON
// --------------------------------------------------------------------------
static void outputResult(const PdbResolveResult& r, const std::string& filePath = "") {
    JsonWriter jw;
    jw.beginObject();

    jw.kv("success", r.success);

    if (!filePath.empty()) {
        jw.kv("file", filePath);
    }

    if (!r.error.empty()) {
        jw.kv("error", r.error);
    }

    if (r.success) {
        if (!r.entryPoint.empty()) jw.kv("entry_point", r.entryPoint);
        if (!r.targetProfile.empty()) jw.kv("target_profile", r.targetProfile);
        if (!r.mainFileName.empty()) jw.kv("main_file_name", r.mainFileName);
        if (!r.name.empty()) jw.kv("name", r.name);
        jw.kv("is_full_pdb", r.isFullPdb);
        if (!r.hashHex.empty()) jw.kv("hash", r.hashHex);

        // Sources
        if (!r.sources.empty()) {
            jw.key("source_count");
            jw.valueInt(r.sources.size());

            jw.key("sources");
            jw.beginArray();
            for (const auto& src : r.sources) {
                jw.beginObject();
                jw.kv("name", src.name);
                jw.kv("content_length", (int64_t)src.content.size());
                jw.kv("content", src.content);
                jw.endObject();
            }
            jw.endArray();
        }

        // Compile args
        if (!r.args.empty()) {
            jw.key("compile_args");
            jw.beginArray();
            for (const auto& a : r.args) jw.valueString(a);
            jw.endArray();
        }

        // Flags
        if (!r.flags.empty()) {
            jw.key("compile_flags");
            jw.beginArray();
            for (const auto& f : r.flags) jw.valueString(f);
            jw.endArray();
        }

        // Defines
        if (!r.defines.empty()) {
            jw.key("defines");
            jw.beginArray();
            for (const auto& d : r.defines) jw.valueString(d);
            jw.endArray();
        }

        // Arg pairs
        if (!r.argPairs.empty()) {
            jw.key("arg_pairs");
            jw.beginArray();
            for (const auto& [name, value] : r.argPairs) {
                jw.beginObject();
                jw.kv("name", name);
                jw.kv("value", value);
                jw.endObject();
            }
            jw.endArray();
        }
    }

    jw.endObject();
    std::cout << jw.str() << std::endl;
}

// --------------------------------------------------------------------------
// Commands
// --------------------------------------------------------------------------
static int cmdResolvePdb(const std::string& pdbFile, const std::string& dxcPath) {
    if (!initDxCompiler(dxcPath)) {
        JsonWriter jw;
        jw.beginObject();
        jw.kv("success", false);
        jw.kv("error", "Failed to load dxcompiler.dll. Ensure it is installed (Windows SDK, Vulkan SDK, or set DXC_DLL_PATH env var).");
        jw.kv("dxc_path_tried", getDxCompilerPath());
        jw.endObject();
        std::cout << jw.str() << std::endl;
        return 1;
    }

    auto result = resolvePdb(pdbFile);
    outputResult(result, pdbFile);
    return result.success ? 0 : 1;
}

static int cmdResolveBlob(const std::string& blobFile,
                           const std::vector<std::string>& pdbPaths,
                           const std::string& dxcPath) {
    if (!initDxCompiler(dxcPath)) {
        JsonWriter jw;
        jw.beginObject();
        jw.kv("success", false);
        jw.kv("error", "Failed to load dxcompiler.dll.");
        jw.endObject();
        std::cout << jw.str() << std::endl;
        return 1;
    }

    auto result = resolveShaderBlob(blobFile, pdbPaths);
    outputResult(result, blobFile);
    return result.success ? 0 : 1;
}

static int cmdBatchResolvePdb(const std::string& pdbDir, const std::string& dxcPath) {
    if (!initDxCompiler(dxcPath)) {
        JsonWriter jw;
        jw.beginObject();
        jw.kv("success", false);
        jw.kv("error", "Failed to load dxcompiler.dll.");
        jw.endObject();
        std::cout << jw.str() << std::endl;
        return 1;
    }

    // Find all .pdb files in the directory
    std::vector<std::string> pdbFiles;
    try {
        for (const auto& entry : fs::recursive_directory_iterator(pdbDir)) {
            if (entry.is_regular_file()) {
                auto ext = entry.path().extension().string();
                std::transform(ext.begin(), ext.end(), ext.begin(), ::tolower);
                if (ext == ".pdb") {
                    pdbFiles.push_back(entry.path().string());
                }
            }
        }
    } catch (const std::exception& e) {
        JsonWriter jw;
        jw.beginObject();
        jw.kv("success", false);
        jw.kv("error", std::string("Failed to scan directory: ") + e.what());
        jw.endObject();
        std::cout << jw.str() << std::endl;
        return 1;
    }

    // Output results
    JsonWriter jw;
    jw.beginObject();
    jw.kv("success", true);
    jw.kv("directory", pdbDir);
    jw.kv("total_pdbs", (int64_t)pdbFiles.size());

    jw.key("results");
    jw.beginArray();

    int successCount = 0;
    for (const auto& pdbFile : pdbFiles) {
        auto result = resolvePdb(pdbFile);
        if (result.success) successCount++;

        jw.beginObject();
        jw.kv("file", pdbFile);
        jw.kv("success", result.success);
        if (!result.error.empty()) jw.kv("error", result.error);
        if (result.success) {
            if (!result.entryPoint.empty()) jw.kv("entry_point", result.entryPoint);
            if (!result.targetProfile.empty()) jw.kv("target_profile", result.targetProfile);
            if (!result.mainFileName.empty()) jw.kv("main_file_name", result.mainFileName);
            jw.kv("source_count", (int64_t)result.sources.size());
            jw.kv("is_full_pdb", result.isFullPdb);

            // Only include source names (not full content) for batch mode
            if (!result.sources.empty()) {
                jw.key("source_names");
                jw.beginArray();
                for (const auto& src : result.sources) {
                    jw.valueString(src.name);
                }
                jw.endArray();
            }
        }
        jw.endObject();
    }

    jw.endArray();
    jw.kv("resolved_count", (int64_t)successCount);
    jw.endObject();

    std::cout << jw.str() << std::endl;
    return 0;
}

static int cmdExtractDebugName(const std::string& blobFile) {
    std::ifstream f(blobFile, std::ios::binary | std::ios::ate);
    if (!f.is_open()) {
        JsonWriter jw;
        jw.beginObject();
        jw.kv("success", false);
        jw.kv("error", "Failed to read file: " + blobFile);
        jw.endObject();
        std::cout << jw.str() << std::endl;
        return 1;
    }

    auto size = f.tellg();
    f.seekg(0, std::ios::beg);
    std::vector<uint8_t> data(size);
    f.read(reinterpret_cast<char*>(data.data()), size);
    f.close();

    std::string debugName = extractDebugNameFromShaderBlob(data.data(), data.size());

    JsonWriter jw;
    jw.beginObject();
    jw.kv("success", !debugName.empty());
    jw.kv("file", blobFile);
    jw.kv("file_size", (int64_t)data.size());

    // Check magic
    if (data.size() >= 4) {
        uint32_t magic = data[0] | (data[1] << 8) | (data[2] << 16) | (data[3] << 24);
        if (magic == 0x43425844) {
            jw.kv("format", std::string("DXBC"));
        } else {
            char hexBuf[12];
            snprintf(hexBuf, sizeof(hexBuf), "0x%08X", magic);
            jw.kv("magic", std::string(hexBuf));
        }
    }

    if (!debugName.empty()) {
        jw.kv("debug_name", debugName);
    } else {
        jw.kv("error", std::string("No debug name found in shader blob. "
                       "The shader was likely compiled without debug info (/Zi flag)."));
    }

    jw.endObject();
    std::cout << jw.str() << std::endl;
    return debugName.empty() ? 1 : 0;
}

static int cmdInfo(const std::string& dxcPath) {
    bool dxcOk = initDxCompiler(dxcPath);

    JsonWriter jw;
    jw.beginObject();
    jw.kv("tool", std::string("pdb-resolver"));
    jw.kv("version", std::string("1.0.0"));
    jw.kv("dxcompiler_loaded", dxcOk);
    jw.kv("dxcompiler_path", getDxCompilerPath());
    jw.endObject();
    std::cout << jw.str() << std::endl;
    return 0;
}

// --------------------------------------------------------------------------
// Parse args
// --------------------------------------------------------------------------
static std::vector<std::string> parseSemicolonList(const std::string& s) {
    std::vector<std::string> result;
    std::string current;
    for (char c : s) {
        if (c == ';') {
            if (!current.empty()) {
                result.push_back(current);
                current.clear();
            }
        } else {
            current += c;
        }
    }
    if (!current.empty()) result.push_back(current);
    return result;
}

static void printUsage() {
    std::cerr << R"(
pdb-resolver: Extract HLSL source code from shader PDB files.

Usage:
  pdb-resolver resolve-pdb <pdb-file>
      Parse a shader PDB and extract all HLSL sources + metadata.

  pdb-resolver resolve-blob <shader-blob> --pdb-paths=<dir1;dir2;...>
      Extract debug name from a shader binary, find its PDB, and extract sources.

  pdb-resolver batch-resolve-pdb <directory>
      Scan a directory for .pdb files and resolve all of them.

  pdb-resolver extract-debug-name <shader-blob>
      Extract the debug name (hash) from a compiled shader binary.

  pdb-resolver info
      Show tool info and dxcompiler.dll status.

Options:
  --dxc-path=<path>        Explicit path to dxcompiler.dll
  --pdb-paths=<dir1;dir2>  Semicolon-separated PDB search paths

Output: JSON to stdout. Errors/logs to stderr.
)" << std::endl;
}

int main(int argc, char* argv[]) {
    // Force UTF-8 output
    SetConsoleOutputCP(CP_UTF8);

    if (argc < 2) {
        printUsage();
        return 1;
    }

    std::string command = argv[1];
    std::string dxcPath;
    std::vector<std::string> pdbPaths;
    std::string targetFile;

    // Parse remaining args
    for (int i = 2; i < argc; i++) {
        std::string arg = argv[i];
        if (arg.rfind("--dxc-path=", 0) == 0) {
            dxcPath = arg.substr(11);
        } else if (arg.rfind("--pdb-paths=", 0) == 0) {
            pdbPaths = parseSemicolonList(arg.substr(12));
        } else if (arg[0] != '-') {
            targetFile = arg;
        }
    }

    // Also check environment variable
    if (dxcPath.empty()) {
        const char* envDxc = getenv("DXC_DLL_PATH");
        if (envDxc) dxcPath = envDxc;
    }

    if (command == "resolve-pdb") {
        if (targetFile.empty()) {
            std::cerr << "Error: resolve-pdb requires a PDB file path." << std::endl;
            return 1;
        }
        return cmdResolvePdb(targetFile, dxcPath);
    }
    else if (command == "resolve-blob") {
        if (targetFile.empty()) {
            std::cerr << "Error: resolve-blob requires a shader blob file path." << std::endl;
            return 1;
        }
        if (pdbPaths.empty()) {
            std::cerr << "Error: resolve-blob requires --pdb-paths=<dir1;dir2;...>" << std::endl;
            return 1;
        }
        return cmdResolveBlob(targetFile, pdbPaths, dxcPath);
    }
    else if (command == "batch-resolve-pdb") {
        if (targetFile.empty()) {
            std::cerr << "Error: batch-resolve-pdb requires a directory path." << std::endl;
            return 1;
        }
        return cmdBatchResolvePdb(targetFile, dxcPath);
    }
    else if (command == "extract-debug-name") {
        if (targetFile.empty()) {
            std::cerr << "Error: extract-debug-name requires a shader blob file path." << std::endl;
            return 1;
        }
        return cmdExtractDebugName(targetFile);
    }
    else if (command == "info") {
        return cmdInfo(dxcPath);
    }
    else {
        std::cerr << "Unknown command: " << command << std::endl;
        printUsage();
        return 1;
    }
}
