"""WinPixEventRuntime supplied from files vendored into this repository.

Why this exists
---------------
The exported replay project links WinPixEventRuntime because every
``PIXBeginEvent`` in the generated command lists is guarded by
``#ifdef WIN_PIX_EVENT_RUNTIME``. pixtool's own ``CMakeLists.txt`` obtains that
dependency by curl-probing the internet and then ``file(DOWNLOAD ...)``-ing
``https://www.nuget.org/api/v2/package/WinPixEventRuntime``.

That download is the most fragile step in the whole replay path, and it fails in
a way that actively misleads:

  * CMake's ``file(DOWNLOAD)`` creates the target file *before* it knows whether
    the transfer succeeded. An SSL or proxy failure therefore leaves a 0-byte
    ``WinPixEventRuntime.nupkg`` on disk.
  * Every later configure sees ``EXISTS <nupkg>`` and treats that as success, so
    the untar silently produces nothing, ``WIN_PIX_EVENT_RUNTIME`` is defined
    anyway, and the error finally surfaces hundreds of translation units later as
    "cannot open include file: pix3.h" or an unresolved ``PIXBeginEvent...``.

So the binaries and headers are vendored into this package instead. They are
tiny (a ~46 KB DLL and a ~12 KB import lib), they come from Microsoft's
open-source PixEvents repository under the MIT licence, and shipping them means a
replay build works on a fresh checkout of this repo with no network at all and
nothing else installed.

Deliberately *not* building from source
---------------------------------------
An earlier version of this module invoked MSBuild against a local PixEvents
checkout. That was wrong: it made a working replay build depend on a second
repository happening to be present on the machine, which does not survive moving
to another device. Vendored files travel with this repository, so they do.

What "package layout" means
---------------------------
The export's CMake expects the *extracted nuget* shape, not the .nupkg:

    <export>/WinPixEventRuntime/bin/x64/WinPixEventRuntime.dll
    <export>/WinPixEventRuntime/bin/x64/WinPixEventRuntime.lib
    <export>/WinPixEventRuntime/Include/WinPixEventRuntime/pix3.h

``pch.h`` includes that last path verbatim. Populating this tree directly is
better than synthesising a .nupkg: it skips the untar step entirely, and the only
remaining obstacle is the ``EXISTS <nupkg>`` gate, which a stamp file satisfies.

Provenance and refreshing
-------------------------
``vendor/winpixeventruntime/`` holds the DLL, the import lib, the pix3 header set
and Microsoft's MIT licence text; its README records how to refresh them. In short:
build ``runtime/dll/desktop/WinPixEventRuntime.vcxproj`` from the PixEvents
repository with ``/p:Configuration=Release /p:Platform=x64`` and, importantly, a
trailing-separator ``SolutionDir`` property - without it the ``mc``-generated
``PIXETW.h`` lands off the include path and the build fails late with a confusing
"cannot open include file: 'PIXETW.h'". Prebuilt binaries from the
WinPixEventRuntime nuget package work equally well.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

# Relative layout inside an extracted WinPixEventRuntime nuget package.
_BIN_SUBDIR = {"x64": "bin/x64", "ARM64": "bin/ARM64"}
_INCLUDE_SUBDIR = "Include/WinPixEventRuntime"

# Header closure required by pch.h's `#include "WinPixEventRuntime/Include/..."`.
# pix3.h pulls in pix3_win.h (or pix3_xbox.h on console), PIXEventsCommon.h and
# PIXEvents.h; PIXEventsCommon.h pulls in PIXEventsLegacy.h. Everything else comes
# from the Windows SDK. Every header in the vendored directory is copied rather
# than just this list, so adding one upstream cannot silently break the build;
# the list is kept to verify the closure is actually present.
_REQUIRED_HEADERS = (
    "pix3.h",
    "pix3_win.h",
    "PIXEvents.h",
    "PIXEventsCommon.h",
    "PIXEventsLegacy.h",
)

_STAMP_NAME = ".pix-tool-set-vendored.json"


def vendor_root() -> Path:
    """The vendored package inside this installed package.

    Resolved relative to this file so it works from a source checkout, an
    editable install and a wheel alike.
    """
    # src/pix_tool_set/engine/winpixruntime.py -> src/pix_tool_set/vendor/...
    return Path(__file__).resolve().parents[1] / "vendor" / "winpixeventruntime"


def available(architecture: str = "x64") -> bool:
    """True when this repository carries a usable runtime for `architecture`."""
    root = vendor_root()
    bin_dir = root / _BIN_SUBDIR.get(architecture, "")
    return (
        architecture in _BIN_SUBDIR
        and (bin_dir / "WinPixEventRuntime.dll").is_file()
        and (bin_dir / "WinPixEventRuntime.lib").is_file()
        and (root / "include" / "pix3.h").is_file()
    )


def describe_vendored(architecture: str = "x64") -> dict[str, Any]:
    """What this repository ships, for reporting in tool output."""
    root = vendor_root()
    info: dict[str, Any] = {
        "vendor_root": str(root),
        "architecture": architecture,
        "available": available(architecture),
    }
    dll = root / _BIN_SUBDIR.get(architecture, "") / "WinPixEventRuntime.dll"
    if dll.is_file():
        info["dll_bytes"] = dll.stat().st_size
    return info


def install_into_export(
    export_root: Path | str,
    *,
    architecture: str = "x64",
    force: bool = False,
) -> dict[str, Any]:
    """Populate ``<export>/WinPixEventRuntime`` from the vendored files.

    Returns a report dict rather than raising, so the caller can fall back to the
    nuget download on any failure. ``ok`` distinguishes the two cases.
    """
    export = Path(export_root)
    root = vendor_root()
    report: dict[str, Any] = {"architecture": architecture, "vendor_root": str(root)}

    if architecture not in _BIN_SUBDIR:
        report["ok"] = False
        report["error"] = (
            f"unsupported architecture {architecture!r}; expected one of "
            + ", ".join(sorted(_BIN_SUBDIR))
        )
        return report

    if not available(architecture):
        report["ok"] = False
        report["error"] = (
            f"this build carries no vendored WinPixEventRuntime for {architecture}; "
            f"expected it under {root}"
        )
        return report

    if not force and is_installed(export, architecture):
        report["ok"] = True
        report["reused_existing"] = True
        report.update(describe_installation(export, architecture))
        return report

    source_bin = root / _BIN_SUBDIR[architecture]
    target_bin = export / "WinPixEventRuntime" / _BIN_SUBDIR[architecture]
    target_include = export / "WinPixEventRuntime" / _INCLUDE_SUBDIR

    try:
        target_bin.mkdir(parents=True, exist_ok=True)
        target_include.mkdir(parents=True, exist_ok=True)

        binaries: list[str] = []
        for origin in sorted(source_bin.iterdir()):
            if origin.is_file():
                shutil.copy2(origin, target_bin / origin.name)
                binaries.append(origin.name)

        headers: list[str] = []
        for origin in sorted((root / "include").glob("*.h")):
            shutil.copy2(origin, target_include / origin.name)
            headers.append(origin.name)

        missing = [n for n in _REQUIRED_HEADERS if not (target_include / n).is_file()]
        if missing:
            report["ok"] = False
            report["error"] = (
                "the vendored header set is incomplete; pch.h also needs: "
                + ", ".join(missing)
            )
            return report

        # Carry the licence alongside the binaries it covers.
        licence = root / "LICENSE.txt"
        if licence.is_file():
            shutil.copy2(licence, export / "WinPixEventRuntime" / "LICENSE.txt")

        stamp = {
            "supplied_by": "pix-tool-set",
            "origin": "vendored in this repository (see vendor/winpixeventruntime)",
            "architecture": architecture,
            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "binaries": binaries,
            "headers": headers,
        }
        (export / "WinPixEventRuntime" / _STAMP_NAME).write_text(
            json.dumps(stamp, indent=2), encoding="utf-8"
        )
        _write_gate_stamp(export, stamp)
    except OSError as exc:
        report["ok"] = False
        report["error"] = f"could not install into the export: {exc}"
        return report

    report["ok"] = True
    report["binaries"] = binaries
    report["headers"] = len(headers)
    report["bin_dir"] = str(target_bin)
    report["include_dir"] = str(target_include)
    return report


def _write_gate_stamp(export: Path, stamp: dict[str, Any]) -> None:
    """Satisfy the export CMake's ``EXISTS <nupkg>`` gate without a download.

    ``_repair_nupkgs`` in the replay-render tool deletes anything under 1 KB as a
    truncated transfer, so the stamp is padded past that threshold. That is belt
    and braces alongside the caller's skip list: if either guard is bypassed, this
    file still survives rather than triggering a pointless re-download.
    """
    text = (
        "This is not a nuget package.\n"
        "WinPixEventRuntime was supplied from files vendored in pix-tool-set; the "
        "extracted layout is already in ./WinPixEventRuntime, so the export's "
        "CMake needs to download and untar nothing.\n"
        "Delete this file and the WinPixEventRuntime directory beside it to fall "
        "back to the nuget package.\n"
        f"{json.dumps(stamp, indent=2)}\n"
    )
    padding = max(0, 1200 - len(text.encode("utf-8")))
    (export / "WinPixEventRuntime.nupkg").write_text(
        text + ("#" * padding + "\n" if padding else ""), encoding="utf-8"
    )


def is_installed(export_root: Path | str, architecture: str = "x64") -> bool:
    """True when the export already has a usable WinPixEventRuntime layout."""
    base = Path(export_root) / "WinPixEventRuntime"
    bin_dir = base / _BIN_SUBDIR.get(architecture, "")
    return (
        architecture in _BIN_SUBDIR
        and (bin_dir / "WinPixEventRuntime.dll").is_file()
        and (bin_dir / "WinPixEventRuntime.lib").is_file()
        and (base / _INCLUDE_SUBDIR / "pix3.h").is_file()
    )


def describe_installation(export_root: Path | str, architecture: str = "x64") -> dict[str, Any]:
    """What is currently installed in an export, and where it came from."""
    base = Path(export_root) / "WinPixEventRuntime"
    info: dict[str, Any] = {"present": is_installed(export_root, architecture)}
    stamp = base / _STAMP_NAME
    if stamp.is_file():
        try:
            info["stamp"] = json.loads(stamp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    dll = base / _BIN_SUBDIR.get(architecture, "") / "WinPixEventRuntime.dll"
    if dll.is_file():
        info["dll"] = str(dll)
        info["dll_bytes"] = dll.stat().st_size
    return info
