# Vendored WinPixEventRuntime

These files are vendored so that building the exported C++ replay project works on
a fresh checkout of this repository, on any machine, with no network access and
nothing else installed.

## Why vendor at all

The replay project that `pixtool export-to-cpp` generates guards every
`PIXBeginEvent` with `#ifdef WIN_PIX_EVENT_RUNTIME`, and its `CMakeLists.txt`
satisfies that dependency by downloading
`https://www.nuget.org/api/v2/package/WinPixEventRuntime` at configure time.

That download is the most fragile step in the whole replay path, and it fails
misleadingly. CMake's `file(DOWNLOAD)` creates the destination file before it
knows whether the transfer worked, so an SSL or proxy failure leaves a 0-byte
`.nupkg` behind. Every later configure then sees `EXISTS <nupkg>`, treats it as
success, extracts nothing, and still defines `WIN_PIX_EVENT_RUNTIME` — so the real
error only surfaces hundreds of translation units later as `cannot open include
file: pix3.h` or an unresolved `PIXBeginEvent...`.

Vendoring is cheap here: a ~46 KB DLL plus a ~12 KB import library.

## Contents

| Path | What it is |
| --- | --- |
| `bin/x64/WinPixEventRuntime.dll` | The runtime the replay executable loads |
| `bin/x64/WinPixEventRuntime.lib` | Import library the replay links against |
| `include/*.h` | The `pix3.h` header set that `pch.h` includes |
| `LICENSE.txt` | Microsoft's MIT licence, which covers all of the above |

## Provenance

Built from Microsoft's open-source
[PixEvents](https://github.com/microsoft/PixEvents) repository, which is MIT
licensed. The prebuilt binaries from the
[WinPixEventRuntime nuget package](https://www.nuget.org/packages/WinPixEventRuntime)
are interchangeable with these and also MIT licensed as of March 2024.

The DLL is used rather than the static library on purpose: upstream warns that
statically linking the full runtime into several binaries within one process
causes ETW provider conflicts.

## Refreshing

From a PixEvents checkout:

```
msbuild runtime\dll\desktop\WinPixEventRuntime.vcxproj ^
    /p:Configuration=Release /p:Platform=x64 /p:SolutionDir=<repo root>\
```

`SolutionDir` is not optional. `Directory.Build.props` derives
`XeSharedIntermediatePath` from it, and without it the `mc`-generated `PIXETW.h`
lands off the include path — the static library builds fine and then the DLL fails
with `cannot open include file: 'PIXETW.h'`, which is a confusing half-failure.

Then copy `output\Release\x64\WinPixEventRuntime\WinPixEventRuntime.{dll,lib}` and
`include\*.h` over the files here.

To add ARM64 support, drop the equivalent binaries into `bin/ARM64/`;
`engine/winpixruntime.py` already understands that layout.

## Consumers

`engine/winpixruntime.py` copies these into an export as the extracted-nuget layout
that the generated CMake expects:

```
<export>/WinPixEventRuntime/bin/x64/WinPixEventRuntime.dll
<export>/WinPixEventRuntime/bin/x64/WinPixEventRuntime.lib
<export>/WinPixEventRuntime/Include/WinPixEventRuntime/pix3.h
```

`replay-render` does this automatically. Pass `--no-vendored-winpixruntime` to skip
it and let the export download from nuget instead.

Note for packaging: these files are listed in `pyproject.toml` under
`[tool.setuptools.package-data]`. If you move or rename anything here, update that
list too, or a wheel install will silently lose the runtime.
