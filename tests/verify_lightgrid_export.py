"""Export RWLightGrid slices, and check that an out-of-range slice is refused."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set import call_tool  # noqa: E402
from pix_tool_set.context import clear_capture_cache  # noqa: E402

OUT = Path("G:/pix-tool-set/lightgrid-out")
QUEUE_ID = 18461


def report(label: str, payload: dict) -> None:
    print("\n" + "=" * 80)
    print(f"{label}   status={payload['status']}")
    print("=" * 80)
    if payload["status"] == "error":
        error = payload["error"]
        print(f"   {error['code']}: {error['message']}")
        if error.get("suggestion"):
            print(f"   suggestion: {error['suggestion']}")
        return
    data = payload["data"]
    resource = data["resource"]
    print(f"   resource {data['resource_id']}  {resource['format']}  "
          f"{resource['width']}x{resource['height']}x{resource['depth_or_array_size']}")
    print(f"   resolved_by={data['resolved_by']}  "
          f"slice {data['slice']} of {data['slice_count']}")
    entry = data.get("footprint") or {}
    if entry:
        print(f"   footprint: {entry['width']}x{entry['height']} "
              f"pitch={entry['row_pitch']} offset={entry['offset']:,}")
    print(f"   rows={data.get('rows_recovered')} pixels={data.get('pixels'):,} "
          f"packed={data.get('packed_bytes'):,} B")
    if data.get("min") is not None:
        print(f"   values: min={data['min']} max={data['max']} "
              f"nonzero={data.get('nonzero'):,} distinct={data.get('distinct_values')}")
    if data.get("value_histogram"):
        print(f"   histogram: {data['value_histogram']}")
    if data.get("values"):
        print(f"   first values: {data['values'][:12]}")
    for entry in data.get("files") or []:
        print(f"   file: {Path(entry['path']).name}  ({entry['bytes']:,} B)  "
              f"{entry['layout']}")
    for entry in payload.get("diagnostics", [])[:2]:
        print(f"   [{entry['level']}] {entry['message'][:130]}")


def main() -> int:
    clear_capture_cache()
    OUT.mkdir(parents=True, exist_ok=True)

    # The request as given: slice 4.
    report(
        "requested slice 4",
        call_tool(
            "export-uav-slice",
            {
                "session": "Tiled",
                "queue_id": QUEUE_ID,
                "name": "RWLightGrid",
                "slice": 4,
                "output": str(OUT),
            },
        ),
    )

    # Every slice that does exist.
    for index in (0, 1, 2):
        report(
            f"slice {index}",
            call_tool(
                "export-uav-slice",
                {
                    "session": "Tiled",
                    "queue_id": QUEUE_ID,
                    "name": "RWLightGrid",
                    "slice": index,
                    "pixels": 8,
                    "output": str(OUT),
                },
            ),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
