from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_json_file(path: str | Path, payload: dict[str, Any]) -> str:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return str(output_path.resolve())


def default_output_path(export_dir: str | Path, name: str) -> str:
    return str((Path(export_dir) / name).resolve())
