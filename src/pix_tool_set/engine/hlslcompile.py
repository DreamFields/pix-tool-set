"""Compile HLSL back into a signed DXIL container, so an edited shader can replace
the one recorded in a capture.

PIX's GUI "Apply" button lives inside its own replay engine and is not exposed by
``pixtool``; there is no ``--replace-shader`` anywhere in its command list.  The
equivalent has to be assembled from the two halves the capture does give us:

  * the exact preprocessed HLSL, recovered from the engine's shader PDB
  * the exact compile arguments, also recorded in that PDB

Feeding those back through DXC reproduces a container with the same bindings as
the captured one, which is the property that makes substitution safe.

Two routes, same as ``shaderpdb``:

  1. ``IDxcCompiler3::Compile`` through raw COM vtables with ctypes.  Primary,
     because it needs no temporary files and returns errors as text.
  2. ``dxc.exe`` from the Windows SDK.  Fallback, for a ``dxcompiler.dll`` that
     refuses to load in-process.

Signing matters: D3D12 rejects an unsigned container.  ``dxil.dll`` sits beside
``dxcompiler.dll`` in the PIX install and is loaded first, so the blob that comes
back already carries a real hash in its header.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ..errors import PixToolError
from .dxbc import _GUID, _DxcBuffer, _vcall

# {73E22D93-E6CE-47F3-B5BF-F0664F39C1B0} IDxcCompiler3 lives behind this CLSID
_CLSID_DxcCompiler = _GUID.parse("73e22d93-e6ce-47f3-b5bf-f0664f39c1b0")
_IID_IDxcCompiler3 = _GUID.parse("228B4687-5A6A-4730-900C-9702B2203F54")
_IID_IDxcResult = _GUID.parse("58346CDA-DDE7-4497-9461-6F87AF5E0659")

DXC_CP_UTF8 = 65001

# IDxcCompiler3
_COMPILE = 3
# IDxcOperationResult, which IDxcResult extends
_GET_STATUS = 3
_GET_RESULT = 4
_GET_ERRORS = 5
# IDxcBlob
_BLOB_PTR = 3
_BLOB_SIZE = 4

# Arguments that only make sense for the command line tool. Passing -Fo to the
# API is harmless but pointless, and -Qstrip_debug interacts with -Zi in a way we
# want to preserve exactly, so it is deliberately kept.
_DROP_ARGS = {"-Fo", "/Fo", "-Fd", "/Fd", "-Fh", "/Fh"}

SDK_BIN_ROOTS = (
    r"C:\Program Files (x86)\Windows Kits\10\bin",
    r"C:\Program Files\Windows Kits\10\bin",
)


@dataclass(slots=True)
class CompileResult:
    ok: bool
    blob: bytes = b""
    method: str = ""
    errors: str = ""
    arguments: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "method": self.method,
            "byte_size": len(self.blob),
            "arguments": list(self.arguments),
            "errors": self.errors,
        }


def strip_output_args(args: Sequence[str]) -> list[str]:
    """Remove file-output flags and their values from a recorded argument list."""
    cleaned: list[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in _DROP_ARGS:
            skip_next = True
            continue
        cleaned.append(arg)
    return cleaned


def find_dxc_exe() -> Path | None:
    """Newest ``dxc.exe`` from an installed Windows SDK, or None."""
    best: tuple[tuple[int, ...], Path] | None = None
    for root in SDK_BIN_ROOTS:
        base = Path(root)
        if not base.is_dir():
            continue
        for version in base.iterdir():
            candidate = version / "x64" / "dxc.exe"
            if not candidate.exists():
                continue
            key = tuple(int(x) for x in version.name.split(".") if x.isdigit()) or (0,)
            if best is None or key > best[0]:
                best = (key, candidate)
    if best is not None:
        return best[1]
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry and (Path(entry) / "dxc.exe").exists():
            return Path(entry) / "dxc.exe"
    return None


class ShaderCompiler:
    """Compiles HLSL text with a recorded argument list."""

    def __init__(self, dxc_dir: str | Path | None = None) -> None:
        self._dxc_dir = Path(dxc_dir) if dxc_dir else None
        self._dll = None
        self._compiler = None
        self._reason: str | None = None

    # ------------------------------------------------------------------
    def _ensure(self) -> None:
        if self._compiler is not None or self._reason is not None:
            return
        candidates: list[Path] = []
        if self._dxc_dir:
            candidates.append(self._dxc_dir / "dxcompiler.dll")
        from ..pixtool import find_pix_install

        install = find_pix_install()
        if install is not None:
            candidates.append(install / "dxcompiler.dll")
        sdk = find_dxc_exe()
        if sdk is not None:
            candidates.append(sdk.parent / "dxcompiler.dll")

        dll_path = next((c for c in candidates if c.exists()), None)
        if dll_path is None:
            self._reason = "dxcompiler.dll was not found next to pixtool.exe or dxc.exe"
            return
        try:
            # dxil.dll must be resident before compiling, otherwise the container
            # comes back unsigned and D3D12 refuses to create the PSO.
            try:
                ctypes.WinDLL(str(dll_path.parent / "dxil.dll"))
            except OSError:
                pass
            dll = ctypes.WinDLL(str(dll_path))
        except OSError as exc:
            self._reason = f"cannot load {dll_path}: {exc}"
            return

        dll.DxcCreateInstance.argtypes = [
            ctypes.POINTER(_GUID),
            ctypes.POINTER(_GUID),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        dll.DxcCreateInstance.restype = ctypes.c_int32
        compiler = ctypes.c_void_p()
        hr = dll.DxcCreateInstance(
            ctypes.byref(_CLSID_DxcCompiler),
            ctypes.byref(_IID_IDxcCompiler3),
            ctypes.byref(compiler),
        )
        if hr != 0 or not compiler:
            self._reason = f"DxcCreateInstance failed (hr=0x{hr & 0xFFFFFFFF:08x})"
            return
        self._dll = dll
        self._compiler = compiler
        self._dll_path = dll_path

    @property
    def available(self) -> bool:
        self._ensure()
        return self._compiler is not None

    @property
    def unavailable_reason(self) -> str | None:
        self._ensure()
        return self._reason

    # ------------------------------------------------------------------
    def compile(self, source: str, arguments: Sequence[str]) -> CompileResult:
        """Compile ``source``; falls back to dxc.exe when the DLL route fails."""
        args = strip_output_args(arguments)
        via_api = self._compile_via_api(source, args)
        if via_api.ok:
            return via_api
        via_exe = self._compile_via_exe(source, args)
        if via_exe.ok:
            return via_exe
        # Report whichever failure carries real compiler diagnostics.
        return via_api if via_api.errors else via_exe

    # ------------------------------------------------------------------
    def _compile_via_api(self, source: str, args: list[str]) -> CompileResult:
        self._ensure()
        if self._compiler is None:
            return CompileResult(
                ok=False,
                method="IDxcCompiler3 (unavailable)",
                errors=self._reason or "dxcompiler.dll is unavailable",
                arguments=args,
            )

        raw = source.encode("utf-8")
        holder = ctypes.create_string_buffer(raw, len(raw))
        buffer = _DxcBuffer(ctypes.addressof(holder), len(raw), DXC_CP_UTF8)
        argv = (ctypes.c_wchar_p * len(args))(*args)

        result = ctypes.c_void_p()
        hr = _vcall(
            self._compiler,
            _COMPILE,
            ctypes.c_int32,
            ctypes.POINTER(_DxcBuffer),
            ctypes.POINTER(ctypes.c_wchar_p),
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(_GUID),
            ctypes.POINTER(ctypes.c_void_p),
        )(
            ctypes.byref(buffer),
            argv,
            len(args),
            None,
            ctypes.byref(_IID_IDxcResult),
            ctypes.byref(result),
        )
        if hr != 0 or not result:
            return CompileResult(
                ok=False,
                method="IDxcCompiler3::Compile",
                errors=f"Compile call failed (hr=0x{hr & 0xFFFFFFFF:08x})",
                arguments=args,
            )
        try:
            errors = self._blob_text(result, _GET_ERRORS)
            status = ctypes.c_int32(0)
            _vcall(result, _GET_STATUS, ctypes.c_int32, ctypes.POINTER(ctypes.c_int32))(
                ctypes.byref(status)
            )
            if status.value != 0:
                return CompileResult(
                    ok=False,
                    method="IDxcCompiler3::Compile",
                    errors=errors or f"compilation failed (0x{status.value & 0xFFFFFFFF:08x})",
                    arguments=args,
                )
            blob = self._blob_bytes(result, _GET_RESULT)
            if not blob:
                return CompileResult(
                    ok=False,
                    method="IDxcCompiler3::Compile",
                    errors=errors or "compiler produced no object blob",
                    arguments=args,
                )
            return CompileResult(
                ok=True,
                blob=blob,
                method="IDxcCompiler3::Compile (dxcompiler.dll)",
                errors=errors,
                arguments=args,
            )
        finally:
            _vcall(result, 2, ctypes.c_uint32)()

    @staticmethod
    def _blob_bytes(result: ctypes.c_void_p, slot: int) -> bytes:
        out = ctypes.c_void_p()
        hr = _vcall(result, slot, ctypes.c_int32, ctypes.POINTER(ctypes.c_void_p))(
            ctypes.byref(out)
        )
        if hr != 0 or not out:
            return b""
        try:
            pointer = _vcall(out, _BLOB_PTR, ctypes.c_void_p)()
            size = _vcall(out, _BLOB_SIZE, ctypes.c_size_t)()
            if not pointer or not size:
                return b""
            return ctypes.string_at(pointer, size)
        finally:
            _vcall(out, 2, ctypes.c_uint32)()

    def _blob_text(self, result: ctypes.c_void_p, slot: int) -> str:
        raw = self._blob_bytes(result, slot)
        if not raw:
            return ""
        # DXC returns a NUL-terminated buffer; keeping the terminator would leak a
        # \u0000 into the JSON report.
        return raw.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()

    # ------------------------------------------------------------------
    def _compile_via_exe(self, source: str, args: list[str]) -> CompileResult:
        exe = find_dxc_exe()
        if exe is None:
            return CompileResult(
                ok=False,
                method="dxc.exe (not found)",
                errors="No dxc.exe in any installed Windows SDK or on PATH.",
                arguments=args,
            )
        with tempfile.TemporaryDirectory(prefix="pixts-hlsl-") as tmp:
            src = Path(tmp) / "edited.hlsl"
            out = Path(tmp) / "edited.dxil"
            src.write_text(source, encoding="utf-8")
            command = [str(exe), *args, "-Fo", str(out), str(src)]
            proc = subprocess.run(
                command, capture_output=True, text=True, encoding="utf-8", errors="replace"
            )
            errors = "\n".join(p for p in (proc.stdout, proc.stderr) if p and p.strip()).strip()
            if proc.returncode != 0 or not out.exists():
                return CompileResult(
                    ok=False,
                    method=f"dxc.exe ({exe.parent.name})",
                    errors=errors or f"dxc.exe exited {proc.returncode}",
                    arguments=args,
                )
            return CompileResult(
                ok=True,
                blob=out.read_bytes(),
                method=f"dxc.exe ({exe})",
                errors=errors,
                arguments=args,
            )


def require_compiler(dxc_dir: str | Path | None = None) -> ShaderCompiler:
    compiler = ShaderCompiler(dxc_dir)
    if not compiler.available and find_dxc_exe() is None:
        raise PixToolError(
            code="compiler_unavailable",
            message=compiler.unavailable_reason or "no HLSL compiler is available",
            stage="shader",
            suggestion=(
                "Install Microsoft PIX so dxcompiler.dll is present, or install the "
                "Windows SDK so dxc.exe is available."
            ),
        )
    return compiler
