"""Regression: reading buffer values, 2D texture values, and Texture3D z slices.

The z case is the one that needed fixing. A Texture3D keeps every depth slice inside a
single subresource, `row_pitch * height` bytes apart, while a Tex2DArray gets one
subresource per layer. The footprint reader previously walked `height` rows from the
subresource offset, so it could only ever return z=0 no matter what was asked for.

Checks here are deliberately behavioural: distinct z values must yield distinct bytes,
out-of-range z must be refused with the real bound rather than clamped, and a capture
that is a few bytes short of the declared volume must say so instead of presenting a
truncated slice as complete.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

SESSION = "Tiled"
VOLUME_RID = 1896          # 468x468x450 R8_UNORM, one subresource, 450 z slices
DEPTH_QUEUE_ID = 17765     # a pass with a depth target, for the 2D path

PASSED: list[str] = []
FAILED: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    if condition:
        PASSED.append(label)
        print(f"  PASS  {label}")
    else:
        FAILED.append(f"{label}: {detail}")
        print(f"  FAIL  {label} :: {detail}")
    return condition


def run(tool: str, *args: str) -> dict:
    proc = subprocess.run(
        ["pix-tool-set", tool, "--session", SESSION, *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=dict(os.environ, PIX_TOOL_SET_NO_LOG="1"), shell=True,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"status": "error", "error": {"message": (proc.stdout or proc.stderr)[-400:]}}


# ----------------------------------------------------------------------
def stage_buffer() -> None:
    print("[1] buffer values are readable and decodable")
    from pix_tool_set.context import ToolContext

    cap = ToolContext.from_cwd().capture({"session": SESSION})
    target = None
    for resource in sorted((r for r in cap.resources.values() if r.is_buffer),
                           key=lambda r: -r.size_bytes):
        try:
            blob = cap.read_resource_bytes(resource.api_id)
        except Exception:
            continue
        if blob and len(blob) >= 64:
            target = resource.api_id
            break
    if not check("a buffer with recorded bytes exists", target is not None):
        return

    payload = run("read-buffer", "--resource-id", str(target),
                  "--length-bytes", "64", "--format", "R32_FLOAT")
    check("read-buffer succeeds", payload.get("status") == "success",
          str(payload.get("error")))
    data = payload.get("data", {})
    check("raw hex returned", bool(data.get("hex")))
    check("bytes actually available", data.get("bytes_available") is True)
    check("decoded as floats", len(data.get("elements") or []) == 16,
          str(len(data.get("elements") or [])))
    check("stride matches the format", data.get("stride") == 4, str(data.get("stride")))

    windowed = run("read-buffer", "--resource-id", str(target),
                   "--offset-bytes", "32", "--length-bytes", "32")
    check("offset window is honoured",
          windowed.get("data", {}).get("length_returned") == 32,
          str(windowed.get("data", {}).get("length_returned")))
    check("offset changes the bytes returned",
          windowed.get("data", {}).get("hex") != data.get("hex"))

    bad = run("read-buffer", "--resource-id", str(target), "--format", "NOT_A_FORMAT")
    check("unknown decode format is refused", bad.get("status") == "error",
          str(bad.get("status")))


def stage_texture2d() -> None:
    print("[2] 2D texture values are readable, including a single pixel")
    payload = run("read-resource-texture", "--queue-id", str(DEPTH_QUEUE_ID),
                  "--target", "depth", "--at-x", "766", "--at-y", "382")
    check("read succeeds", payload.get("status") in {"success", "partial"},
          str(payload.get("error")))
    planes = payload.get("data", {}).get("planes") or []
    check("planes reported", len(planes) >= 1, str(len(planes)))
    decoded = [p for p in planes if p.get("decoded")]
    check("at least one plane decoded", bool(decoded))
    if decoded:
        first = decoded[0]
        check("row pitch honoured",
              first.get("packed_bytes") == first["width"] * first["rows_recovered"]
              * (first.get("bytes_per_pixel") or 0),
              f"{first.get('packed_bytes')} vs "
              f"{first['width']}*{first['rows_recovered']}*{first.get('bytes_per_pixel')}")
        check("single pixel returned", "pixel" in first, str(sorted(first)))
        pixel = first.get("pixel") or {}
        check("pixel carries a value", "value" in pixel or "hex" in pixel, str(pixel))
        check("a 2D plane reports no z", "z" not in first, str(first.get("z")))


def stage_volume_slices(work: Path) -> None:
    print("[3] Texture3D z slices are addressable and distinct")
    signatures: dict[int, tuple] = {}
    for z in (0, 1, 100, 225):
        payload = run("read-resource-texture", "--resource-id", str(VOLUME_RID),
                      "--z", str(z), "--output", str(work), "--png", str(work))
        if not check(f"z={z} read succeeds", payload.get("status") in {"success", "partial"},
                     str(payload.get("error"))):
            continue
        data = payload["data"]
        volume = data.get("volume") or {}
        check(f"z={z} echoed back", volume.get("z_selected") == z, str(volume))
        plane = data["planes"][0]
        check(f"z={z} plane tagged with z", plane.get("z") == z, str(plane.get("z")))
        raw = Path(plane["output"]).read_bytes()
        signatures[z] = (len(raw), sum(raw))
        check(f"z={z} filename carries the slice", f"_z{z}_" in Path(plane["output"]).name,
              Path(plane["output"]).name)

    check("every slice has the same byte count",
          len({sig[0] for sig in signatures.values()}) == 1, str(signatures))
    check("slices differ from one another",
          len({sig[1] for sig in signatures.values()}) == len(signatures),
          str({z: sig[1] for z, sig in signatures.items()}))

    # A 2D read of the same resource must default to z=0, not silently to something else.
    default = run("read-resource-texture", "--resource-id", str(VOLUME_RID),
                  "--output", str(work))
    plane = default["data"]["planes"][0]
    check("omitting --z defaults to slice 0", plane.get("z") == 0, str(plane.get("z")))


def stage_volume_bounds() -> None:
    print("[4] out-of-range z is refused, and short captures are declared")
    payload = run("read-resource-texture", "--resource-id", str(VOLUME_RID), "--z", "450")
    check("z past the end is an error", payload.get("status") == "error",
          str(payload.get("status")))
    error = payload.get("error") or {}
    check("error names the real bound", "0..449" in (error.get("message") or ""),
          str(error.get("message"))[:160])
    check("error is an argument error", error.get("code") == "invalid_argument",
          str(error.get("code")))

    ok = run("read-resource-texture", "--resource-id", str(VOLUME_RID), "--z", "0")
    plane = ok["data"]["planes"][0]
    availability = plane.get("z_availability") or {}
    check("slice availability reported", bool(availability), str(availability))
    check("declared depth matches the footprint", availability.get("declared") == 450,
          str(availability.get("declared")))
    check("a short capture is admitted rather than hidden",
          availability.get("complete") < availability.get("declared")
          or availability.get("partial") is False,
          str(availability))
    check("layout is explained", "z slices" in (plane.get("layout") or ""),
          str(plane.get("layout")))


def stage_engine_level() -> None:
    print("[5] the footprint reader addresses slices correctly")
    from pix_tool_set.context import ToolContext
    from pix_tool_set.engine import footprint as fp

    cap = ToolContext.from_cwd().capture({"session": SESSION})
    entry = cap.resource_footprints(VOLUME_RID)[0]
    blob = cap.read_resource_bytes(VOLUME_RID)

    check("volume flagged as such", entry.is_volume is True)
    check("slice_bytes = pitch * height",
          entry.slice_bytes == entry.row_pitch * entry.height, str(entry.slice_bytes))
    check("size_bytes = slice_bytes * depth",
          entry.size_bytes == entry.slice_bytes * entry.depth, str(entry.size_bytes))
    check("slice_offset scales with z",
          entry.slice_offset(3) - entry.slice_offset(2) == entry.slice_bytes)

    rows0 = fp.extract_rows(blob, entry, 0)
    rows7 = fp.extract_rows(blob, entry, 7)
    check("both slices return full height",
          len(rows0) == entry.height and len(rows7) == entry.height,
          f"{len(rows0)}, {len(rows7)}")
    check("slice content differs at engine level", b"".join(rows0) != b"".join(rows7))
    check("negative z refused", fp.extract_rows(blob, entry, -1) is None)
    check("z past depth refused", fp.extract_rows(blob, entry, entry.depth) is None)

    # A plain 2D texture must be unaffected by the new parameter.
    flat = next(
        (r for r in cap.resources.values()
         if r.kind.value == "texture2d" and cap.resource_footprints(r.api_id)),
        None,
    )
    if flat is not None:
        flat_entry = cap.resource_footprints(flat.api_id)[0]
        check("2D footprint not flagged as a volume", flat_entry.is_volume is False)
        check("2D footprint omits volume fields", "z_slices" not in flat_entry.to_dict())


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="pixts-zslice-"))
    print(f"work dir: {work}\n")
    try:
        stage_buffer()
        print()
        stage_texture2d()
        print()
        stage_volume_slices(work)
        print()
        stage_volume_bounds()
        print()
        stage_engine_level()
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print()
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    for entry in FAILED:
        print("  -", entry)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
