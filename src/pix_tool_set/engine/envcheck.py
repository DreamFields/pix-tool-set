"""Environment probing: what this machine has, and what it still needs.

Why this exists
---------------
Everything this toolkit does rests on dependencies it cannot ship. Reading a
capture at all needs ``pixtool.exe`` from a Microsoft PIX install; rebuilding the
exported replay project additionally needs CMake, a Visual Studio C++ toolchain,
a Windows SDK, a working D3D12 device, and the D3D12 Agility SDK package.

Before this module the only way to learn that something was missing was to run a
real tool and read its failure, which is a poor way to set up a new machine: the
failures arrive one at a time, in whatever order the work happens to reach them,
and some of them surface far from their cause (a missing Agility SDK shows up as
"cannot open include file: d3d12.h" hundreds of translation units later).

So the probes live here, in one place, and report every finding at once. They are
deliberately *read-only*: nothing is installed, downloaded or configured. The
worst a probe does is load a DLL, ask Windows whether a D3D12 device could be
created, or run ``--version`` on a tool that is already on PATH.

Two tiers, because the requirements are not the same
----------------------------------------------------
``core``   what static analysis of a ``.wpix`` needs. Without these nothing works.
``replay`` what rebuilding and running the exported C++ project needs. A machine
           that only answers questions about a capture can skip all of it.

Each probe returns the same shape so the caller can render or diff them without
per-probe special casing::

    {"name": ..., "tier": "core"|"replay", "ok": bool, "required": bool,
     "detail": "...", "found": {...}, "fix": "..." | None}

``required`` is False for probes that describe a fallback rather than a hard
dependency (a Windows SDK ``dxc.exe`` is only needed when PIX's own
``dxcompiler.dll`` cannot be loaded), so a caller can tell "missing, and that
matters" from "missing, and something else covers it".
"""

from __future__ import annotations

import ctypes
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import hlslcompile, winpixruntime

#: Minimum interpreter this package is written against (see pyproject.toml).
MIN_PYTHON = (3, 11)

#: The generator every replay tool passes to CMake unless told otherwise. Kept
#: here so a machine with a different Visual Studio can be told exactly which
#: ``--generator`` value to use instead of discovering it through a build failure.
DEFAULT_GENERATOR = "Visual Studio 18 2026"

#: Visual Studio major version -> the CMake generator that drives it.
_VS_GENERATORS = {
    "18": "Visual Studio 18 2026",
    "17": "Visual Studio 17 2022",
    "16": "Visual Studio 16 2019",
    "15": "Visual Studio 15 2017",
}

_VSWHERE = (
    r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe",
    r"C:\Program Files\Microsoft Visual Studio\Installer\vswhere.exe",
)

_SDK_INCLUDE_ROOTS = (
    r"C:\Program Files (x86)\Windows Kits\10\Include",
    r"C:\Program Files\Windows Kits\10\Include",
)

#: The nuget package the export's CMake downloads and this repository does not
#: vendor. Named here so the probe can say what to fetch, and where to put it.
AGILITY_NUPKG = "D3D12AgilitySdk.nupkg"
AGILITY_URL = "https://www.nuget.org/api/v2/package/Microsoft.Direct3D.D3D12"


def _check(
    name: str,
    tier: str,
    ok: bool,
    detail: str,
    *,
    required: bool = True,
    found: dict[str, Any] | None = None,
    fix: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": name,
        "tier": tier,
        "ok": bool(ok),
        "required": bool(required),
        "detail": detail,
    }
    if found:
        entry["found"] = found
    if not ok and fix:
        entry["fix"] = fix
    return entry


# ----------------------------------------------------------------------
# core tier
# ----------------------------------------------------------------------
def probe_platform() -> dict[str, Any]:
    """Windows on x64: pixtool, the D3D12 replay and every ctypes call assume it."""
    system = platform.system()
    machine = platform.machine()
    ok = system == "Windows" and machine.upper() in {"AMD64", "X86_64"}
    return _check(
        "windows_x64",
        "core",
        ok,
        f"{system} {platform.release()} on {machine}",
        found={"system": system, "release": platform.release(), "machine": machine},
        fix="This toolkit drives pixtool.exe and D3D12; it only runs on 64-bit Windows.",
    )


def probe_python() -> dict[str, Any]:
    version = sys.version_info
    ok = (version.major, version.minor) >= MIN_PYTHON
    return _check(
        "python",
        "core",
        ok,
        f"Python {version.major}.{version.minor}.{version.micro} at {sys.executable}",
        found={
            "version": f"{version.major}.{version.minor}.{version.micro}",
            "executable": sys.executable,
            "bits": 64 if sys.maxsize > 2**32 else 32,
        },
        fix=f"Install Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ (64-bit) and reinstall with `pip install -e .`.",
    )


def probe_console_script() -> dict[str, Any]:
    """``pip install -e .`` puts ``pix-tool-set`` / ``pixts`` on PATH.

    Not required: ``python -m pix_tool_set.cli`` works without it. Worth
    reporting because a missing entry point is the usual reason a copied command
    line comes back as "not recognized".
    """
    found = {
        name: shutil.which(name)
        for name in ("pix-tool-set", "pixts")
    }
    ok = any(found.values())
    return _check(
        "console_script",
        "core",
        ok,
        "on PATH: " + ", ".join(n for n, p in found.items() if p) if ok else "neither entry point is on PATH",
        required=False,
        found={k: v for k, v in found.items() if v},
        fix="Run `pip install -e .` in the repository root, or invoke `python -m pix_tool_set.cli`.",
    )


def probe_pix(explicit: str | Path | None = None) -> dict[str, Any]:
    """``pixtool.exe``: the one dependency nothing here can work around."""
    from ..pixtool import PIX_ROOTS, find_pix_install

    install = find_pix_install(explicit)
    if install is None:
        return _check(
            "pix_install",
            "core",
            False,
            "pixtool.exe was not found",
            found={"searched": list(PIX_ROOTS), "PIXTOOL_PATH": os.environ.get("PIXTOOL_PATH")},
            fix=(
                "Install Microsoft PIX for Windows, or set PIXTOOL_PATH, or pass "
                "--pixtool <path to pixtool.exe>. A .wpix cannot be read without it."
            ),
        )
    return _check(
        "pix_install",
        "core",
        True,
        f"pixtool.exe in {install}",
        found={"install_dir": str(install), "pixtool": str(install / "pixtool.exe")},
    )


def probe_dxcompiler(explicit: str | Path | None = None) -> dict[str, Any]:
    """The DXC DLL behind disassembly, HLSL compilation and PDB reading.

    Loading it is the only honest test: the file existing next to pixtool.exe
    says nothing about whether ``DxcCreateInstance`` will succeed on this
    machine, and that is what every shader tool actually depends on.
    """
    from .dxbc import ShaderDisassembler

    probe = ShaderDisassembler(explicit)
    ok = probe.available
    from ..pixtool import find_pix_install

    install = find_pix_install(explicit)
    found: dict[str, Any] = {}
    if install is not None:
        dll = install / "dxcompiler.dll"
        dxil = install / "dxil.dll"
        found["dxcompiler"] = str(dll) if dll.exists() else None
        # dxil.dll is what signs a freshly compiled container; without it an
        # edited shader compiles but D3D12 refuses to create the PSO.
        found["dxil"] = str(dxil) if dxil.exists() else None
    return _check(
        "dxcompiler",
        "core",
        ok,
        "IDxcCompiler3 created successfully" if ok else (probe.unavailable_reason or "unavailable"),
        # Not a hard blocker: without it disassembly, shader-edit compilation and PDB
        # source lookup go dark, but every structural query still answers. Marking it
        # required would make a usable machine report read_only_analysis=false.
        required=False,
        found=found,
        fix=(
            "Install Microsoft PIX so dxcompiler.dll and dxil.dll sit beside pixtool.exe. "
            "Without it disassemble-shader, shader-edit compilation and PDB source lookup "
            "are unavailable; structural queries are unaffected."
        ),
    )


def probe_windows_sdk_dxc() -> dict[str, Any]:
    """``dxc.exe`` from a Windows SDK: the fallback compilation route."""
    exe = hlslcompile.find_dxc_exe()
    return _check(
        "windows_sdk_dxc",
        "core",
        exe is not None,
        f"dxc.exe at {exe}" if exe else "no dxc.exe in any installed Windows SDK or on PATH",
        required=False,
        found={"dxc": str(exe)} if exe else None,
        fix=(
            "Optional. Install the Windows SDK to gain a second compilation route for "
            "when PIX's dxcompiler.dll rejects a shader."
        ),
    )


# ----------------------------------------------------------------------
# replay tier
# ----------------------------------------------------------------------
def probe_cmake() -> dict[str, Any]:
    exe = shutil.which("cmake")
    if exe is None:
        return _check(
            "cmake",
            "replay",
            False,
            "cmake is not on PATH",
            fix="Install CMake 3.20+ and make sure it is on PATH. Needed only by replay tools.",
        )
    version = ""
    try:
        proc = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60,
        )
        version = (proc.stdout or "").splitlines()[0].strip() if proc.stdout else ""
    except (OSError, subprocess.SubprocessError):
        version = ""
    return _check(
        "cmake",
        "replay",
        True,
        version or f"cmake at {exe}",
        found={"cmake": exe, "version": version},
    )


def _installed_visual_studios() -> list[dict[str, Any]]:
    """Ask vswhere for VS installs that actually carry the C++ toolset."""
    vswhere = next((p for p in _VSWHERE if Path(p).exists()), None)
    if vswhere is None:
        return []
    try:
        proc = subprocess.run(
            [
                vswhere, "-products", "*", "-format", "json", "-utf8",
                "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        entries = json.loads(proc.stdout or "[]")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []
    installs: list[dict[str, Any]] = []
    for entry in entries if isinstance(entries, list) else []:
        version = str(entry.get("installationVersion", ""))
        major = version.split(".")[0]
        installs.append(
            {
                "display_name": entry.get("displayName"),
                "installation_version": version,
                "installation_path": entry.get("installationPath"),
                "cmake_generator": _VS_GENERATORS.get(major),
            }
        )
    return installs


def _cmake_known_generators() -> list[str]:
    """Generator names ``cmake --help`` advertises on this machine."""
    exe = shutil.which("cmake")
    if exe is None:
        return []
    try:
        proc = subprocess.run(
            [exe, "--help"], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=90,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    names: list[str] = []
    for line in (proc.stdout or "").splitlines():
        text = line.strip().lstrip("* ").strip()
        if text.startswith("Visual Studio "):
            names.append(text.split("=")[0].strip().rstrip("[arch]").strip())
    return names


def probe_toolchain() -> dict[str, Any]:
    """A Visual Studio C++ toolset, and whether the default generator names it.

    The replay tools default to ``Visual Studio 18 2026``. On a machine with only
    VS 2022 that default fails at configure time with an unhelpful message, so the
    probe resolves the generator this machine can actually use and hands it back
    ready to paste after ``--generator``.
    """
    installs = _installed_visual_studios()
    known = _cmake_known_generators()
    generators = sorted({i["cmake_generator"] for i in installs if i.get("cmake_generator")})
    default_usable = DEFAULT_GENERATOR in generators and (
        not known or DEFAULT_GENERATOR in known
    )
    found: dict[str, Any] = {
        "visual_studio": installs,
        "default_generator": DEFAULT_GENERATOR,
        "default_generator_usable": default_usable,
        "cmake_known_generators": known,
    }
    if generators and not default_usable:
        recommended = generators[-1]
        found["recommended_generator"] = recommended
        return _check(
            "vs_toolchain",
            "replay",
            False,
            (
                f"Visual Studio is installed but the default generator {DEFAULT_GENERATOR!r} "
                f"does not match it"
            ),
            found=found,
            fix=f'Pass --generator "{recommended}" to every replay/build tool.',
        )
    if not installs:
        return _check(
            "vs_toolchain",
            "replay",
            False,
            "no Visual Studio install with the x64 C++ toolset was found",
            found=found,
            fix=(
                "Install Visual Studio (Desktop development with C++) or the Build Tools. "
                "Needed only by tools that rebuild the exported replay project."
            ),
        )
    # Name the install the default generator actually drives. Reporting installs[0]
    # was misleading on a machine with several: it printed "Visual Studio Community
    # 2022 -> generator 'Visual Studio 18 2026'", which reads like a mismatch.
    matching = next(
        (i for i in installs if i.get("cmake_generator") == DEFAULT_GENERATOR), installs[0]
    )
    return _check(
        "vs_toolchain",
        "replay",
        True,
        f"{matching.get('display_name') or 'Visual Studio'} -> generator {DEFAULT_GENERATOR!r}",
        found=found,
    )


def probe_windows_sdk() -> dict[str, Any]:
    """A Windows SDK with ``d3d12.h``: the export includes it directly."""
    best: tuple[tuple[int, ...], Path] | None = None
    for root in _SDK_INCLUDE_ROOTS:
        base = Path(root)
        if not base.is_dir():
            continue
        for version in base.iterdir():
            header = version / "um" / "d3d12.h"
            if not header.exists():
                continue
            key = tuple(int(x) for x in version.name.split(".") if x.isdigit()) or (0,)
            if best is None or key > best[0]:
                best = (key, version)
    if best is None:
        return _check(
            "windows_sdk",
            "replay",
            False,
            "no Windows SDK with um/d3d12.h was found",
            fix="Install the Windows 10/11 SDK; the exported replay project includes d3d12.h.",
        )
    return _check(
        "windows_sdk",
        "replay",
        True,
        f"Windows SDK {best[1].name}",
        found={"include_dir": str(best[1])},
    )


def probe_d3d12_device() -> dict[str, Any]:
    """Ask Windows whether a D3D12 device *could* be created, without creating one.

    ``D3D12CreateDevice`` with a null ``ppDevice`` is the documented capability
    check: it returns S_FALSE when the adapter and driver would support the
    requested feature level. That keeps the probe read-only - no device, no
    command queue, nothing to leak.
    """
    if platform.system() != "Windows":
        return _check(
            "d3d12_device",
            "replay",
            False,
            "not Windows",
            fix="A D3D12-capable GPU and driver are required to run the replay.",
        )
    try:
        d3d12 = ctypes.WinDLL("d3d12.dll")
    except OSError as exc:
        return _check(
            "d3d12_device",
            "replay",
            False,
            f"cannot load d3d12.dll: {exc}",
            fix="Install a GPU driver with D3D12 support.",
        )

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_uint32),
            ("Data2", ctypes.c_uint16),
            ("Data3", ctypes.c_uint16),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    iid_device = _GUID(
        0x189819F1, 0x1DB6, 0x4B57,
        (ctypes.c_ubyte * 8)(0xBE, 0x54, 0x18, 0x21, 0x33, 0x9B, 0x85, 0xF7),
    )
    try:
        d3d12.D3D12CreateDevice.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_GUID), ctypes.c_void_p
        ]
        d3d12.D3D12CreateDevice.restype = ctypes.c_int32
        hr = d3d12.D3D12CreateDevice(None, 0xB000, ctypes.byref(iid_device), None)
    except (AttributeError, OSError) as exc:
        return _check(
            "d3d12_device",
            "replay",
            False,
            f"D3D12CreateDevice is unavailable: {exc}",
            fix="Install a GPU driver with D3D12 feature level 11_0 support.",
        )
    ok = hr in (0, 1)  # S_OK / S_FALSE both mean "this would work"
    return _check(
        "d3d12_device",
        "replay",
        ok,
        "feature level 11_0 is supported on the default adapter"
        if ok
        else f"D3D12CreateDevice returned 0x{hr & 0xFFFFFFFF:08x}",
        found={"hresult": f"0x{hr & 0xFFFFFFFF:08x}"},
        fix="Update the GPU driver, or run on a machine with a D3D12 feature level 11_0 GPU.",
    )


def probe_vendored_winpixruntime() -> dict[str, Any]:
    """WinPixEventRuntime travels with this repository, so this should never fail."""
    info = winpixruntime.describe_vendored("x64")
    ok = bool(info.get("available"))
    return _check(
        "vendored_winpixeventruntime",
        "replay",
        ok,
        f"vendored in this repository ({info.get('dll_bytes', 0)} byte DLL)"
        if ok
        else f"missing under {info.get('vendor_root')}",
        found=info,
        fix=(
            "The files should be in src/pix_tool_set/vendor/winpixeventruntime; restore them "
            "from the repository, or pass --no-vendored-winpixruntime to download the nuget "
            "package instead (needs network)."
        ),
    )


def probe_agility_sdk(export_dir: Path | None, *, check_network: bool) -> dict[str, Any]:
    """The one build dependency this repository does not carry.

    Unlike WinPixEventRuntime, the Agility SDK is not vendored here, so the
    export's CMake downloads it from nuget.org on first configure. This probe
    reports whichever of the two ways out is available: a package already sitting
    in the export, or reachable network to fetch one.
    """
    found: dict[str, Any] = {"package": AGILITY_NUPKG, "url": AGILITY_URL}
    cached = False
    if export_dir is not None:
        target = export_dir / AGILITY_NUPKG
        found["export_dir"] = str(export_dir)
        if target.exists():
            size = target.stat().st_size
            found["cached_bytes"] = size
            # replay-render treats anything under 1 KB as a truncated download and
            # replaces it, so apply the same threshold rather than inventing one.
            cached = size > 1024
            if not cached:
                found["note"] = "present but truncated (a failed download); it will be re-fetched"

    reachable: bool | None = None
    if check_network and not cached:
        import urllib.request

        try:
            request = urllib.request.Request(AGILITY_URL, method="HEAD")
            with urllib.request.urlopen(request, timeout=30) as response:
                reachable = 200 <= getattr(response, "status", 200) < 400
        except Exception as exc:  # noqa: BLE001
            reachable = False
            found["network_error"] = f"{type(exc).__name__}: {exc}"
        found["nuget_reachable"] = reachable

    # Only a positive "not cached AND nuget unreachable" is a real blocker. With
    # --check-network omitted we cannot know, so the probe stays green and says so
    # rather than inventing a failure the user cannot act on.
    if cached:
        ok, detail = True, f"already cached in the export ({found.get('cached_bytes')} bytes)"
    elif reachable is True:
        ok, detail = True, "nuget.org is reachable, so CMake can download it"
    elif reachable is False:
        ok, detail = False, "not cached in the export and nuget.org is unreachable"
    else:
        ok, detail = True, (
            "not cached in the export; CMake downloads it on first configure "
            "(pass --check-network to test that nuget.org is reachable)"
        )
    return _check(
        "agility_sdk",
        "replay",
        ok,
        detail,
        found=found,
        fix=(
            "Download it once on a networked machine and drop it in the export directory:\n"
            f"  Invoke-WebRequest '{AGILITY_URL}' -OutFile <export>\\{AGILITY_NUPKG}\n"
            "then delete <export>\\build so CMake unpacks it."
        ),
    )


# ----------------------------------------------------------------------
def run_checks(
    *,
    scope: str = "all",
    pixtool: str | Path | None = None,
    export_dir: Path | None = None,
    check_network: bool = False,
) -> dict[str, Any]:
    """Run every probe in ``scope`` and summarise what the machine can do."""
    checks: list[dict[str, Any]] = [
        probe_platform(),
        probe_python(),
        probe_console_script(),
        probe_pix(pixtool),
        probe_dxcompiler(pixtool),
        probe_windows_sdk_dxc(),
    ]
    if scope in {"all", "replay"}:
        checks += [
            probe_cmake(),
            probe_toolchain(),
            probe_windows_sdk(),
            probe_d3d12_device(),
            probe_vendored_winpixruntime(),
            probe_agility_sdk(export_dir, check_network=check_network),
        ]
    if scope == "replay":
        checks = [c for c in checks if c["tier"] == "replay" or c["name"] in {"windows_x64", "python"}]

    def _blocking(tier: str) -> list[str]:
        return [c["name"] for c in checks if c["tier"] == tier and c["required"] and not c["ok"]]

    core_missing = _blocking("core")
    replay_missing = _blocking("replay")
    # Only claim what was actually probed. With scope="core" no replay probe ran, so
    # reporting gpu_replay=true would be a fabricated pass - worse than saying nothing.
    ran_replay = any(c["tier"] == "replay" for c in checks)
    ran_core = any(c["tier"] == "core" and c["required"] for c in checks)
    ready: dict[str, Any] = {}
    if scope != "replay":
        ready["read_only_analysis"] = not core_missing
    if ran_replay:
        ready["gpu_replay"] = not core_missing and not replay_missing
    if scope == "replay" and ran_core:
        # The two core probes kept in replay scope are prerequisites, not a full core
        # verdict, so say so instead of implying analysis was checked.
        ready["core_fully_checked"] = False
    return {
        "scope": scope,
        "checks": checks,
        "ready": ready,
        "missing": {"core": core_missing, "replay": replay_missing},
        "optional_missing": [
            c["name"] for c in checks if not c["required"] and not c["ok"]
        ],
    }
