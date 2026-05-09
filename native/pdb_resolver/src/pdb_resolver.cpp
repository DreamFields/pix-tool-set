#include "pdb_resolver.h"
#include "dxbc_parser.h"
#include "dxcapi.h"

#include <windows.h>
#include <fstream>
#include <sstream>
#include <algorithm>
#include <filesystem>
#include <iostream>
#include <iomanip>

namespace fs = std::filesystem;

// --------------------------------------------------------------------------
// Globals
// --------------------------------------------------------------------------
static HMODULE g_dxcModule = nullptr;
static DxcCreateInstanceProc g_DxcCreateInstance = nullptr;
static std::string g_dxcPath;

// --------------------------------------------------------------------------
// Helpers
// --------------------------------------------------------------------------
static std::string wideToUtf8(const wchar_t* wstr) {
    if (!wstr || !*wstr) return "";
    int len = WideCharToMultiByte(CP_UTF8, 0, wstr, -1, nullptr, 0, nullptr, nullptr);
    if (len <= 0) return "";
    std::string result(len - 1, '\0');
    WideCharToMultiByte(CP_UTF8, 0, wstr, -1, &result[0], len, nullptr, nullptr);
    return result;
}

static std::wstring utf8ToWide(const std::string& str) {
    if (str.empty()) return L"";
    int len = MultiByteToWideChar(CP_UTF8, 0, str.c_str(), -1, nullptr, 0);
    if (len <= 0) return L"";
    std::wstring result(len - 1, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, str.c_str(), -1, &result[0], len);
    return result;
}

static std::string bstrToUtf8(BSTR bstr) {
    if (!bstr) return "";
    return wideToUtf8(bstr);
}

static std::string bytesToHex(const uint8_t* data, size_t len) {
    std::ostringstream oss;
    for (size_t i = 0; i < len; i++) {
        oss << std::hex << std::setfill('0') << std::setw(2) << (int)data[i];
    }
    return oss.str();
}

static std::vector<uint8_t> readFileBytes(const std::string& path) {
    std::ifstream f(path, std::ios::binary | std::ios::ate);
    if (!f.is_open()) return {};
    auto size = f.tellg();
    f.seekg(0, std::ios::beg);
    std::vector<uint8_t> data(size);
    f.read(reinterpret_cast<char*>(data.data()), size);
    return data;
}

// --------------------------------------------------------------------------
// Auto-discover dxcompiler.dll
// --------------------------------------------------------------------------
static std::string findDxCompilerDll() {
    // 1. Check Windows SDK
    const char* sdkRoots[] = {
        "C:\\Program Files (x86)\\Windows Kits\\10\\bin",
        "C:\\Program Files\\Windows Kits\\10\\bin",
    };
    for (const auto& root : sdkRoots) {
        if (fs::exists(root)) {
            std::vector<std::string> versions;
            for (const auto& entry : fs::directory_iterator(root)) {
                if (entry.is_directory()) {
                    std::string name = entry.path().filename().string();
                    if (!name.empty() && name[0] >= '0' && name[0] <= '9') {
                        versions.push_back(name);
                    }
                }
            }
            std::sort(versions.begin(), versions.end());
            for (int i = (int)versions.size() - 1; i >= 0; i--) {
                auto candidate = fs::path(root) / versions[i] / "x64" / "dxcompiler.dll";
                if (fs::exists(candidate)) return candidate.string();
            }
        }
    }

    // 2. Check Vulkan SDK
    const char* vulkanSdk = getenv("VULKAN_SDK");
    if (vulkanSdk) {
        auto candidate = fs::path(vulkanSdk) / "Bin" / "dxcompiler.dll";
        if (fs::exists(candidate)) return candidate.string();
    }

    // 3. Check alongside pdb-resolver.exe
    {
        wchar_t exePath[MAX_PATH];
        GetModuleFileNameW(nullptr, exePath, MAX_PATH);
        auto candidate = fs::path(exePath).parent_path() / "dxcompiler.dll";
        if (fs::exists(candidate)) return candidate.string();
    }

    // 4. Check PIX installation (PIX ships its own dxcompiler.dll)
    {
        auto pixRoot = fs::path("C:\\Program Files\\Microsoft PIX");
        if (fs::exists(pixRoot)) {
            std::vector<std::string> versions;
            for (const auto& entry : fs::directory_iterator(pixRoot)) {
                if (entry.is_directory()) {
                    versions.push_back(entry.path().filename().string());
                }
            }
            std::sort(versions.begin(), versions.end());
            for (int i = (int)versions.size() - 1; i >= 0; i--) {
                auto candidate = pixRoot / versions[i] / "dxcompiler.dll";
                if (fs::exists(candidate)) return candidate.string();
            }
        }
    }

    // 5. Fallback: rely on PATH
    return "dxcompiler.dll";
}

// --------------------------------------------------------------------------
// Init
// --------------------------------------------------------------------------
bool initDxCompiler(const std::string& dxcPath) {
    if (g_dxcModule) return true; // already initialized

    std::string path = dxcPath.empty() ? findDxCompilerDll() : dxcPath;
    g_dxcPath = path;

    std::wstring wpath = utf8ToWide(path);
    g_dxcModule = LoadLibraryW(wpath.c_str());
    if (!g_dxcModule) {
        // Try without path (system search)
        g_dxcModule = LoadLibraryW(L"dxcompiler.dll");
        if (!g_dxcModule) return false;
        g_dxcPath = "dxcompiler.dll (system)";
    }

    g_DxcCreateInstance = (DxcCreateInstanceProc)GetProcAddress(g_dxcModule, "DxcCreateInstance");
    return g_DxcCreateInstance != nullptr;
}

std::string getDxCompilerPath() {
    return g_dxcPath;
}

// --------------------------------------------------------------------------
// Resolve PDB
// --------------------------------------------------------------------------
PdbResolveResult resolvePdb(const std::string& pdbFilePath) {
    PdbResolveResult result;

    if (!g_DxcCreateInstance) {
        result.error = "dxcompiler.dll not initialized. Call initDxCompiler() first.";
        return result;
    }

    // Read the PDB file
    auto fileData = readFileBytes(pdbFilePath);
    if (fileData.empty()) {
        result.error = "Failed to read PDB file: " + pdbFilePath;
        return result;
    }

    // Create IDxcUtils to make a blob
    IDxcUtils* pUtils = nullptr;
    HRESULT hr = g_DxcCreateInstance(CLSID_DxcUtils, __uuidof(IDxcUtils), (void**)&pUtils);
    if (FAILED(hr) || !pUtils) {
        result.error = "Failed to create IDxcUtils. HRESULT: " + std::to_string(hr);
        return result;
    }

    // Create blob from the file data
    IDxcBlobEncoding* pBlobEncoding = nullptr;
    hr = pUtils->CreateBlobFromPinned(fileData.data(), (UINT32)fileData.size(), 0, &pBlobEncoding);
    if (FAILED(hr) || !pBlobEncoding) {
        pUtils->Release();
        result.error = "Failed to create blob from PDB data. HRESULT: " + std::to_string(hr);
        return result;
    }

    // Create IDxcPdbUtils
    IDxcPdbUtils* pPdbUtils = nullptr;
    hr = g_DxcCreateInstance(CLSID_DxcPdbUtils, __uuidof(IDxcPdbUtils), (void**)&pPdbUtils);
    if (FAILED(hr) || !pPdbUtils) {
        pBlobEncoding->Release();
        pUtils->Release();
        result.error = "Failed to create IDxcPdbUtils. HRESULT: " + std::to_string(hr);
        return result;
    }

    // Load the PDB
    hr = pPdbUtils->Load(pBlobEncoding);
    if (FAILED(hr)) {
        pPdbUtils->Release();
        pBlobEncoding->Release();
        pUtils->Release();
        result.error = "IDxcPdbUtils::Load failed. The file may not be a valid shader PDB. HRESULT: " + std::to_string(hr);
        return result;
    }

    result.success = true;

    // --- Extract metadata ---
    BSTR bstr = nullptr;

    if (SUCCEEDED(pPdbUtils->GetEntryPoint(&bstr)) && bstr) {
        result.entryPoint = bstrToUtf8(bstr);
        SysFreeString(bstr); bstr = nullptr;
    }
    if (SUCCEEDED(pPdbUtils->GetTargetProfile(&bstr)) && bstr) {
        result.targetProfile = bstrToUtf8(bstr);
        SysFreeString(bstr); bstr = nullptr;
    }
    if (SUCCEEDED(pPdbUtils->GetMainFileName(&bstr)) && bstr) {
        result.mainFileName = bstrToUtf8(bstr);
        SysFreeString(bstr); bstr = nullptr;
    }
    if (SUCCEEDED(pPdbUtils->GetName(&bstr)) && bstr) {
        result.name = bstrToUtf8(bstr);
        SysFreeString(bstr); bstr = nullptr;
    }

    result.isFullPdb = pPdbUtils->IsFullPDB() ? true : false;

    // --- Extract hash ---
    IDxcBlob* pHash = nullptr;
    if (SUCCEEDED(pPdbUtils->GetHash(&pHash)) && pHash && pHash->GetBufferSize() > 0) {
        result.hashHex = bytesToHex(
            static_cast<const uint8_t*>(pHash->GetBufferPointer()),
            pHash->GetBufferSize());
        pHash->Release();
    }

    // --- Extract sources ---
    UINT32 sourceCount = 0;
    if (SUCCEEDED(pPdbUtils->GetSourceCount(&sourceCount))) {
        for (UINT32 i = 0; i < sourceCount; i++) {
            ShaderSourceFile src;

            BSTR srcName = nullptr;
            if (SUCCEEDED(pPdbUtils->GetSourceName(i, &srcName)) && srcName) {
                src.name = bstrToUtf8(srcName);
                SysFreeString(srcName);
            }

            IDxcBlobEncoding* pSrcBlob = nullptr;
            if (SUCCEEDED(pPdbUtils->GetSource(i, &pSrcBlob)) && pSrcBlob) {
                // Try to get as UTF-8 first
                IDxcBlobUtf8* pUtf8 = nullptr;
                hr = pUtils->GetBlobAsUtf8(pSrcBlob, &pUtf8);
                if (SUCCEEDED(hr) && pUtf8 && pUtf8->GetStringLength() > 0) {
                    src.content = std::string(pUtf8->GetStringPointer(), pUtf8->GetStringLength());
                    pUtf8->Release();
                } else {
                    // Fallback: raw data as string
                    if (pSrcBlob->GetBufferSize() > 0) {
                        src.content = std::string(
                            static_cast<const char*>(pSrcBlob->GetBufferPointer()),
                            pSrcBlob->GetBufferSize());
                        // Remove null terminators
                        while (!src.content.empty() && src.content.back() == '\0') {
                            src.content.pop_back();
                        }
                    }
                    if (pUtf8) pUtf8->Release();
                }
                pSrcBlob->Release();
            }

            result.sources.push_back(std::move(src));
        }
    }

    // --- Extract flags ---
    UINT32 flagCount = 0;
    if (SUCCEEDED(pPdbUtils->GetFlagCount(&flagCount))) {
        for (UINT32 i = 0; i < flagCount; i++) {
            BSTR flag = nullptr;
            if (SUCCEEDED(pPdbUtils->GetFlag(i, &flag)) && flag) {
                result.flags.push_back(bstrToUtf8(flag));
                SysFreeString(flag);
            }
        }
    }

    // --- Extract args ---
    UINT32 argCount = 0;
    if (SUCCEEDED(pPdbUtils->GetArgCount(&argCount))) {
        for (UINT32 i = 0; i < argCount; i++) {
            BSTR arg = nullptr;
            if (SUCCEEDED(pPdbUtils->GetArg(i, &arg)) && arg) {
                result.args.push_back(bstrToUtf8(arg));
                SysFreeString(arg);
            }
        }
    }

    // --- Extract arg pairs ---
    UINT32 argPairCount = 0;
    if (SUCCEEDED(pPdbUtils->GetArgPairCount(&argPairCount))) {
        for (UINT32 i = 0; i < argPairCount; i++) {
            BSTR name = nullptr, value = nullptr;
            if (SUCCEEDED(pPdbUtils->GetArgPair(i, &name, &value))) {
                result.argPairs.push_back({
                    name ? bstrToUtf8(name) : "",
                    value ? bstrToUtf8(value) : ""
                });
                if (name) SysFreeString(name);
                if (value) SysFreeString(value);
            }
        }
    }

    // --- Extract defines ---
    UINT32 defineCount = 0;
    if (SUCCEEDED(pPdbUtils->GetDefineCount(&defineCount))) {
        for (UINT32 i = 0; i < defineCount; i++) {
            BSTR def = nullptr;
            if (SUCCEEDED(pPdbUtils->GetDefine(i, &def)) && def) {
                result.defines.push_back(bstrToUtf8(def));
                SysFreeString(def);
            }
        }
    }

    // Cleanup
    pPdbUtils->Release();
    pBlobEncoding->Release();
    pUtils->Release();

    return result;
}

// --------------------------------------------------------------------------
// Resolve shader blob → find PDB → extract sources
// --------------------------------------------------------------------------
PdbResolveResult resolveShaderBlob(const std::string& blobFilePath,
                                    const std::vector<std::string>& pdbSearchPaths) {
    PdbResolveResult result;

    // Read the shader blob
    auto blobData = readFileBytes(blobFilePath);
    if (blobData.empty()) {
        result.error = "Failed to read shader blob: " + blobFilePath;
        return result;
    }

    // Extract debug name
    std::string debugName = extractDebugNameFromShaderBlob(blobData.data(), blobData.size());
    if (debugName.empty()) {
        result.error = "No debug name found in shader blob. "
                       "The shader was likely compiled without /Zi (debug info).";
        return result;
    }

    // Ensure .pdb extension
    std::string pdbFileName = debugName;
    if (pdbFileName.size() < 4 ||
        pdbFileName.substr(pdbFileName.size() - 4) != ".pdb") {
        pdbFileName += ".pdb";
    }

    // Search for the PDB in the given paths
    for (const auto& searchPath : pdbSearchPaths) {
        fs::path candidate;

        // Check if searchPath is a .zip file (future enhancement)
        // For now, just search directories
        if (fs::is_directory(searchPath)) {
            // Direct match
            candidate = fs::path(searchPath) / pdbFileName;
            if (fs::exists(candidate)) {
                return resolvePdb(candidate.string());
            }

            // Recursive search
            try {
                for (const auto& entry : fs::recursive_directory_iterator(searchPath)) {
                    if (entry.is_regular_file() &&
                        entry.path().filename().string() == pdbFileName) {
                        return resolvePdb(entry.path().string());
                    }
                }
            } catch (...) {}
        } else if (fs::is_regular_file(searchPath)) {
            // Maybe it's a direct path to the PDB
            if (searchPath.size() >= 4 &&
                searchPath.substr(searchPath.size() - 4) == ".pdb") {
                return resolvePdb(searchPath);
            }
        }
    }

    result.error = "PDB file '" + pdbFileName + "' not found in any search path. "
                   "Debug name extracted from blob: '" + debugName + "'. "
                   "Search paths checked: ";
    for (size_t i = 0; i < pdbSearchPaths.size(); i++) {
        if (i > 0) result.error += ", ";
        result.error += pdbSearchPaths[i];
    }

    return result;
}
