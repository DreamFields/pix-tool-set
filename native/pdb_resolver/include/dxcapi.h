// Minimal dxcapi.h for pdb-resolver
// Based on https://github.com/microsoft/DirectXShaderCompiler/blob/main/include/dxc/dxcapi.h
// Only includes the interfaces needed for PDB resolution.

#pragma once

#ifndef __dxcapi_h__
#define __dxcapi_h__

#include <windows.h>
#include <unknwn.h>
#include <stdint.h>

// Forward declarations
struct IDxcBlob;
struct IDxcBlobEncoding;
struct IDxcBlobUtf8;
struct IDxcBlobUtf16;
struct IDxcPdbUtils;
struct IDxcUtils;
struct IDxcVersionInfo;
struct IDxcCompiler3;
struct IDxcResult;

// --------------------------------------------------------------------------
// GUIDs
// --------------------------------------------------------------------------

// {54621dfb-f2ce-457e-ae8c-ec355faeec7c}
static const GUID CLSID_DxcPdbUtils = {
    0x54621dfb, 0xf2ce, 0x457e, {0xae, 0x8c, 0xec, 0x35, 0x5f, 0xae, 0xec, 0x7c}};

// {6245D6AF-66E0-48FD-80B4-4D271796748C}
static const GUID CLSID_DxcUtils = {
    0x6245d6af, 0x66e0, 0x48fd, {0x80, 0xb4, 0x4d, 0x27, 0x17, 0x96, 0x74, 0x8c}};

// {73e22d93-e6ce-47f3-b5bf-f0664f39c1b0}
static const GUID CLSID_DxcLibrary = {
    0x73e22d93, 0xe6ce, 0x47f3, {0xb5, 0xbf, 0xf0, 0x66, 0x4f, 0x39, 0xc1, 0xb0}};

// --------------------------------------------------------------------------
// IDxcBlob
// --------------------------------------------------------------------------
MIDL_INTERFACE("8BA5FB08-5195-40e2-AC58-0D989C3A0102")
IDxcBlob : public IUnknown {
    virtual LPVOID STDMETHODCALLTYPE GetBufferPointer() = 0;
    virtual SIZE_T STDMETHODCALLTYPE GetBufferSize() = 0;
};

// --------------------------------------------------------------------------
// IDxcBlobEncoding
// --------------------------------------------------------------------------
MIDL_INTERFACE("7241d424-2646-4191-97c0-98e96e42fc68")
IDxcBlobEncoding : public IDxcBlob {
    virtual HRESULT STDMETHODCALLTYPE GetEncoding(
        _Out_ BOOL *pKnown,
        _Out_ UINT32 *pCodePage) = 0;
};

// --------------------------------------------------------------------------
// IDxcBlobUtf8
// --------------------------------------------------------------------------
MIDL_INTERFACE("3DA636C9-BA71-4024-A301-30CBF125305B")
IDxcBlobUtf8 : public IDxcBlobEncoding {
    virtual LPCSTR STDMETHODCALLTYPE GetStringPointer() = 0;
    virtual SIZE_T STDMETHODCALLTYPE GetStringLength() = 0;
};

// --------------------------------------------------------------------------
// IDxcBlobUtf16
// --------------------------------------------------------------------------
MIDL_INTERFACE("A3F84EAB-0FAA-497E-A39C-EE6ED60B2D84")
IDxcBlobUtf16 : public IDxcBlobEncoding {
    virtual LPCWSTR STDMETHODCALLTYPE GetStringPointer() = 0;
    virtual SIZE_T STDMETHODCALLTYPE GetStringLength() = 0;
};

// --------------------------------------------------------------------------
// IDxcVersionInfo
// --------------------------------------------------------------------------
MIDL_INTERFACE("b04f5b50-2059-4f12-a8ff-a1e0cde1cc7e")
IDxcVersionInfo : public IUnknown {
    virtual HRESULT STDMETHODCALLTYPE GetVersion(
        _Out_ UINT32 *pMajor,
        _Out_ UINT32 *pMinor) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetFlags(
        _Out_ UINT32 *pFlags) = 0;
};

// --------------------------------------------------------------------------
// DxcArgPair (for IDxcPdbUtils::OverrideArgs)
// --------------------------------------------------------------------------
struct DxcArgPair {
    const WCHAR *pName;
    const WCHAR *pValue;
};

// --------------------------------------------------------------------------
// IDxcPdbUtils
// --------------------------------------------------------------------------
MIDL_INTERFACE("E6C9647E-9D6A-4C3B-B94C-524B5A6C343D")
IDxcPdbUtils : public IUnknown {
    virtual HRESULT STDMETHODCALLTYPE Load(
        _In_ IDxcBlob *pPdbOrDxil) = 0;

    virtual HRESULT STDMETHODCALLTYPE GetSourceCount(
        _Out_ UINT32 *pCount) = 0;

    virtual HRESULT STDMETHODCALLTYPE GetSource(
        _In_ UINT32 uIndex,
        _Out_ IDxcBlobEncoding **ppResult) = 0;

    virtual HRESULT STDMETHODCALLTYPE GetSourceName(
        _In_ UINT32 uIndex,
        _Out_ BSTR *pResult) = 0;

    virtual HRESULT STDMETHODCALLTYPE GetFlagCount(
        _Out_ UINT32 *pCount) = 0;

    virtual HRESULT STDMETHODCALLTYPE GetFlag(
        _In_ UINT32 uIndex,
        _Out_ BSTR *pResult) = 0;

    virtual HRESULT STDMETHODCALLTYPE GetArgCount(
        _Out_ UINT32 *pCount) = 0;

    virtual HRESULT STDMETHODCALLTYPE GetArg(
        _In_ UINT32 uIndex,
        _Out_ BSTR *pResult) = 0;

    virtual HRESULT STDMETHODCALLTYPE GetArgPairCount(
        _Out_ UINT32 *pCount) = 0;

    virtual HRESULT STDMETHODCALLTYPE GetArgPair(
        _In_ UINT32 uIndex,
        _Out_ BSTR *pName,
        _Out_ BSTR *pValue) = 0;

    virtual HRESULT STDMETHODCALLTYPE GetDefineCount(
        _Out_ UINT32 *pCount) = 0;

    virtual HRESULT STDMETHODCALLTYPE GetDefine(
        _In_ UINT32 uIndex,
        _Out_ BSTR *pResult) = 0;

    virtual HRESULT STDMETHODCALLTYPE GetTargetProfile(
        _Out_ BSTR *pResult) = 0;

    virtual HRESULT STDMETHODCALLTYPE GetEntryPoint(
        _Out_ BSTR *pResult) = 0;

    virtual HRESULT STDMETHODCALLTYPE GetMainFileName(
        _Out_ BSTR *pResult) = 0;

    virtual HRESULT STDMETHODCALLTYPE GetHash(
        _Out_ IDxcBlob **ppResult) = 0;

    virtual HRESULT STDMETHODCALLTYPE GetName(
        _Out_ BSTR *pResult) = 0;

    virtual BOOL STDMETHODCALLTYPE IsFullPDB() = 0;

    virtual HRESULT STDMETHODCALLTYPE GetFullPDB(
        _Out_ IDxcBlob **ppFullPDB) = 0;

    virtual HRESULT STDMETHODCALLTYPE GetVersionInfo(
        _Out_ IDxcVersionInfo **ppVersionInfo) = 0;

    virtual HRESULT STDMETHODCALLTYPE SetCompiler(
        _In_ IDxcCompiler3 *pCompiler) = 0;

    virtual HRESULT STDMETHODCALLTYPE CompileForFullPDB(
        _Out_ IDxcResult **ppResult) = 0;

    virtual HRESULT STDMETHODCALLTYPE OverrideArgs(
        _In_ DxcArgPair *pArgPairs,
        UINT32 uNumArgPairs) = 0;

    virtual HRESULT STDMETHODCALLTYPE OverrideRootSignature(
        _In_ const WCHAR *pRootSignature) = 0;
};

// --------------------------------------------------------------------------
// IDxcUtils (minimal — only what we need)
// --------------------------------------------------------------------------
MIDL_INTERFACE("4605C4CB-2019-492A-ADA4-65F20BB7D67F")
IDxcUtils : public IUnknown {
    // We only use CreateBlobFromPinned and LoadFile
    // vtable slots before our methods — we pad with placeholders
    virtual HRESULT STDMETHODCALLTYPE CreateBlobFromBlob(
        _In_ IDxcBlob *pBlob,
        UINT32 offset,
        UINT32 length,
        _Out_ IDxcBlob **ppResult) = 0;

    virtual HRESULT STDMETHODCALLTYPE CreateBlobFromPinned(
        _In_ LPCVOID pData,
        UINT32 size,
        UINT32 codePage,
        _Out_ IDxcBlobEncoding **ppBlobEncoding) = 0;

    virtual HRESULT STDMETHODCALLTYPE MoveToBlob(
        _In_ LPCVOID pData,
        _In_opt_ IMalloc *pIMalloc,
        UINT32 size,
        UINT32 codePage,
        _Out_ IDxcBlobEncoding **ppBlobEncoding) = 0;

    virtual HRESULT STDMETHODCALLTYPE CreateBlob(
        _In_ LPCVOID pData,
        UINT32 size,
        UINT32 codePage,
        _Out_ IDxcBlobEncoding **ppBlobEncoding) = 0;

    virtual HRESULT STDMETHODCALLTYPE LoadFile(
        _In_ LPCWSTR pFileName,
        _In_opt_ UINT32 *pCodePage,
        _Out_ IDxcBlobEncoding **ppBlobEncoding) = 0;

    // Remaining methods not needed, but included to preserve vtable layout
    virtual HRESULT STDMETHODCALLTYPE CreateReadOnlyStreamFromBlob(
        _In_ IDxcBlob *pBlob,
        _Out_ IStream **ppStream) = 0;

    virtual HRESULT STDMETHODCALLTYPE CreateDefaultIncludeHandler(
        _Out_ void **ppResult) = 0;

    virtual HRESULT STDMETHODCALLTYPE GetBlobAsUtf8(
        _In_ IDxcBlob *pBlob,
        _Out_ IDxcBlobUtf8 **ppBlobEncoding) = 0;

    virtual HRESULT STDMETHODCALLTYPE GetBlobAsUtf16(
        _In_ IDxcBlob *pBlob,
        _Out_ IDxcBlobUtf16 **ppBlobEncoding) = 0;

    virtual HRESULT STDMETHODCALLTYPE GetDxilContainerPart(
        _In_ const void *pShader,
        UINT32 shaderSize,
        UINT32 partFourCC,
        _Out_ LPCVOID *ppPartData,
        _Out_ UINT32 *pPartSizeInBytes) = 0;

    virtual HRESULT STDMETHODCALLTYPE CreateReflection(
        _In_ const void *pData,
        REFIID iid,
        _Out_ void **ppvReflection) = 0;

    virtual HRESULT STDMETHODCALLTYPE BuildArguments(
        _In_ LPCWSTR pSourceName,
        _In_ LPCWSTR pEntryPoint,
        _In_ LPCWSTR pTargetProfile,
        _In_opt_ LPCWSTR *pArguments,
        UINT32 argCount,
        _In_opt_ const void *pDefines,
        UINT32 defineCount,
        _Out_ void **ppArgs) = 0;

    virtual HRESULT STDMETHODCALLTYPE GetPDBContents(
        _In_ IDxcBlob *pPDBBlob,
        _Out_ IDxcBlob **ppHash,
        _Out_ IDxcBlob **ppContainer) = 0;
};

// --------------------------------------------------------------------------
// DxcCreateInstance function typedef (loaded dynamically)
// --------------------------------------------------------------------------
typedef HRESULT(__stdcall *DxcCreateInstanceProc)(
    _In_ REFCLSID rclsid,
    _In_ REFIID riid,
    _Out_ LPVOID *ppv);

#endif // __dxcapi_h__
