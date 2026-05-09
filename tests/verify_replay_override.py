"""Unit-check the state-level override engine (gap three).

Works on a synthetic export project: apply overrides, assert the pinned text
edits landed where they should, assert scope=draw clones the PSO and repoints
only the target draw, then assert restore_overrides returns every file
byte-for-byte. No GPU work involved -- these are the properties a replay round
would pay minutes to discover, so they are pinned cheaply here.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set.engine import override  # noqa: E402

CREATE_PSOS = """\
void CreatePipelineState_3184()
{
    pssDesc.RasterizerState = CD3DX12_RASTERIZER_DESC(
        D3D12_FILL_MODE_SOLID, D3D12_CULL_MODE_BACK, FALSE, 0, 0.f, 0.f, TRUE, FALSE,
        FALSE, 0, D3D12_CONSERVATIVE_RASTERIZATION_MODE_OFF);
    pssDesc.DepthStencilState = CD3DX12_DEPTH_STENCIL_DESC(
        TRUE, D3D12_DEPTH_WRITE_MASK_ALL, D3D12_COMPARISON_FUNC_LESS, FALSE, 255, 255,
        D3D12_STENCIL_OP_KEEP, D3D12_STENCIL_OP_KEEP, D3D12_STENCIL_OP_KEEP,
        D3D12_COMPARISON_FUNC_ALWAYS, D3D12_STENCIL_OP_KEEP, D3D12_STENCIL_OP_KEEP,
        D3D12_STENCIL_OP_KEEP, D3D12_COMPARISON_FUNC_ALWAYS);
    blendDesc.RenderTarget[0] = { TRUE, FALSE, D3D12_BLEND_SRC_ALPHA,
        D3D12_BLEND_INV_SRC_ALPHA, D3D12_BLEND_OP_ADD, D3D12_BLEND_ONE,
        D3D12_BLEND_ZERO, D3D12_BLEND_OP_ADD, D3D12_LOGIC_OP_NOOP,
        D3D12_COLOR_WRITE_ENABLE_ALL };
    CreateAndTrackPipelineState(3184, pssDesc);
}

void CreatePipelineState_3185()
{
    pssDesc.RasterizerState = CD3DX12_RASTERIZER_DESC(D3D12_DEFAULT);
}
"""

COMMAND_LISTS = """\
void PopulateCommandList_1()
{
    // GlobalId = 1001
    GetCommandList(1)->SetPipelineState(GetPipelineState(3184));
    // GlobalId = 1002
    GetCommandList(1)->DrawIndexedInstanced(36, 1, 0, 0, 0);
    // GlobalId = 1003
    GetCommandList(1)->DrawInstanced(3, 1, 0, 0);
    // GlobalId = 1004
    GetCommandList(1)->Dispatch(8, 8, 1);
}
"""


def make_export(root: Path) -> dict[Path, bytes]:
    files = {
        root / "CreatePSOs.cpp": CREATE_PSOS,
        root / "CommandLists_000.cpp": COMMAND_LISTS,
    }
    for path, content in files.items():
        path.write_text(content, encoding="utf-8")
    return {path: path.read_bytes() for path in files}


def check(label: str, ok: bool) -> int:
    print(f"   {label:<52} {'ok' if ok else 'MISMATCH'}")
    return 0 if ok else 1


def main() -> int:
    failures = 0

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        originals = make_export(root)

        print("scope=pso: blend_off + cull=none")
        report = override.apply_override(
            root,
            overrides=[{"kind": "blend_off"}, {"kind": "cull", "value": "none"}],
            pso_id=3184,
            target_global_ids=None,
            scope="pso",
            dry_run=False,
        )
        text = (root / "CreatePSOs.cpp").read_text(encoding="utf-8")
        failures += check(
            "blend aggregate flipped to FALSE",
            "RenderTarget[0] = { FALSE," in text,
        )
        failures += check(
            "cull mode replaced in constructor",
            "D3D12_CULL_MODE_NONE, FALSE, 0, 0.f, 0.f, TRUE" in text,
        )
        failures += check("backup created", (root / "CreatePSOs.cpp.override-backup").exists())

        actions = override.restore_overrides(root)
        failures += check(
            "restore reported the file",
            any(a["file"].endswith("CreatePSOs.cpp") for a in actions),
        )
        failures += check(
            "byte-for-byte restore after scope=pso",
            (root / "CreatePSOs.cpp").read_bytes() == originals[root / "CreatePSOs.cpp"],
        )

        print("scope=pso on D3D12_DEFAULT: cull=front expands the default")
        report = override.apply_override(
            root,
            overrides=[{"kind": "cull", "value": "front"}],
            pso_id=3185,
            target_global_ids=None,
            scope="pso",
            dry_run=False,
        )
        text = (root / "CreatePSOs.cpp").read_text(encoding="utf-8")
        failures += check(
            "D3D12_DEFAULT expanded with FRONT cull",
            "CD3DX12_RASTERIZER_DESC(D3D12_FILL_MODE_SOLID, D3D12_CULL_MODE_FRONT, FALSE,"
            in text,
        )
        override.restore_overrides(root)

        print("scope=draw: clone + repoint only the target draw")
        report = override.apply_override(
            root,
            overrides=[{"kind": "blend_off"}],
            pso_id=3184,
            target_global_ids={1001},
            scope="draw",
            dry_run=False,
        )
        text = (root / "CreatePSOs.cpp").read_text(encoding="utf-8")
        failures += check(
            "clone function created",
            "void CreatePipelineState_9003184(" in text,
        )
        failures += check(
            "clone carries the override",
            text.count("RenderTarget[0] = { FALSE,") == 1,
        )
        failures += check(
            "original PSO untouched",
            text.count("RenderTarget[0] = { TRUE,") == 1,
        )
        failures += check(
            "clone registered under its own id",
            "CreateAndTrackPipelineState(9003184" in text,
        )
        cl = (root / "CommandLists_000.cpp").read_text(encoding="utf-8")
        failures += check(
            "target draw repointed at the clone",
            "SetPipelineState(GetPipelineState(9003184))" in cl,
        )
        failures += check(
            "no other draw repointed",
            cl.count("9003184") == 1,
        )
        override.restore_overrides(root)
        failures += check(
            "byte-for-byte restore after scope=draw",
            (root / "CreatePSOs.cpp").read_bytes() == originals[root / "CreatePSOs.cpp"]
            and (root / "CommandLists_000.cpp").read_bytes() == originals[root / "CommandLists_000.cpp"],
        )

        print("skip_draw comments out exactly the target draw")
        report = override.apply_override(
            root,
            overrides=[{"kind": "skip_draw"}],
            pso_id=None,
            target_global_ids={1002},
            scope="draw",
            dry_run=False,
        )
        cl = (root / "CommandLists_000.cpp").read_text(encoding="utf-8")
        failures += check(
            "target draw commented",
            "// pix-tool-set override:     GetCommandList(1)->DrawIndexedInstanced" in cl,
        )
        failures += check(
            "non-target draws left running",
            "// pix-tool-set override:     GetCommandList(1)->DrawInstanced" not in cl
            and "// pix-tool-set override:     GetCommandList(1)->Dispatch" not in cl,
        )
        failures += check(
            "other call untouched",
            "GetCommandList(1)->SetPipelineState(GetPipelineState(3184));" in cl,
        )
        override.restore_overrides(root)
        failures += check(
            "byte-for-byte restore after skip_draw",
            (root / "CommandLists_000.cpp").read_bytes() == originals[root / "CommandLists_000.cpp"],
        )

        print("solo_draw keeps the target and comments out every other draw")
        report = override.apply_override(
            root,
            overrides=[{"kind": "solo_draw"}],
            pso_id=None,
            target_global_ids={1002},
            scope="draw",
            dry_run=False,
        )
        cl = (root / "CommandLists_000.cpp").read_text(encoding="utf-8")
        failures += check(
            "target draw still runs",
            "    GetCommandList(1)->DrawIndexedInstanced(36, 1, 0, 0, 0);" in cl,
        )
        failures += check(
            "the two other draws are commented",
            "// pix-tool-set override:     GetCommandList(1)->DrawInstanced" in cl
            and "// pix-tool-set override:     GetCommandList(1)->Dispatch" in cl,
        )
        solo_change = next(
            (c for c in report.changes if c.get("kind") == "solo_draw"), {}
        )
        failures += check("solo commented 2 draws", solo_change.get("count") == 2)
        failures += check("solo kept 1 draw", solo_change.get("draws_kept") == 1)
        failures += check(
            "state-setting calls untouched by solo",
            "GetCommandList(1)->SetPipelineState(GetPipelineState(3184));" in cl,
        )
        override.restore_overrides(root)
        failures += check(
            "byte-for-byte restore after solo_draw",
            (root / "CommandLists_000.cpp").read_bytes() == originals[root / "CommandLists_000.cpp"],
        )

        print("solo_draw without a target is refused, not applied")
        report = override.apply_override(
            root,
            overrides=[{"kind": "solo_draw"}],
            pso_id=None,
            target_global_ids=None,
            scope="draw",
            dry_run=False,
        )
        cl_bytes = (root / "CommandLists_000.cpp").read_bytes()
        failures += check(
            "no draw was commented out",
            cl_bytes == originals[root / "CommandLists_000.cpp"],
        )
        failures += check(
            "the refusal is reported, not swallowed",
            any(c.get("error") for c in report.changes),
        )

        print("write_mask parses every channel combination")
        cases = {
            "RGBA": "D3D12_COLOR_WRITE_ENABLE_ALL",
            "R": "D3D12_COLOR_WRITE_ENABLE_RED",
            "A": "D3D12_COLOR_WRITE_ENABLE_ALPHA",
            "NONE": "0",
            "RGB": (
                "D3D12_COLOR_WRITE_ENABLE_RED | D3D12_COLOR_WRITE_ENABLE_GREEN"
                " | D3D12_COLOR_WRITE_ENABLE_BLUE"
            ),
            "ar": (
                "D3D12_COLOR_WRITE_ENABLE_RED | D3D12_COLOR_WRITE_ENABLE_ALPHA"
            ),
        }
        for value, expected in cases.items():
            expr, error = override.parse_write_mask(value)
            failures += check(f"write_mask={value} -> expression", expr == expected and error is None)
        expr, error = override.parse_write_mask("RGBX")
        failures += check("write_mask=RGBX rejected", expr is None and "X" in (error or ""))

        print("write_mask=R isolates the red channel in the export")
        report = override.apply_override(
            root,
            overrides=[{"kind": "write_mask", "value": "R"}],
            pso_id=3184,
            target_global_ids=None,
            scope="pso",
            dry_run=False,
        )
        text = (root / "CreatePSOs.cpp").read_text(encoding="utf-8")
        failures += check(
            "aggregate mask replaced with RED only",
            "D3D12_COLOR_WRITE_ENABLE_RED }" in text
            and "D3D12_COLOR_WRITE_ENABLE_ALL" not in text,
        )
        failures += check(
            "the rest of the blend aggregate is intact",
            "D3D12_BLEND_SRC_ALPHA" in text and "D3D12_LOGIC_OP_NOOP" in text,
        )
        override.restore_overrides(root)
        failures += check(
            "byte-for-byte restore after write_mask",
            (root / "CreatePSOs.cpp").read_bytes() == originals[root / "CreatePSOs.cpp"],
        )

        print("stencil_off reports a no-op instead of staying silent")
        report = override.apply_override(
            root,
            overrides=[{"kind": "stencil_off"}],
            pso_id=3185,
            target_global_ids=None,
            scope="pso",
            dry_run=False,
        )
        failures += check(
            "no-op is reported with count 0",
            any(c.get("no_op") and c.get("count") == 0 for c in report.changes),
        )
        override.restore_overrides(root)

        print("dry_run writes nothing")
        before = (root / "CreatePSOs.cpp").read_bytes()
        override.apply_override(
            root,
            overrides=[{"kind": "blend_off"}],
            pso_id=3184,
            target_global_ids=None,
            scope="pso",
            dry_run=True,
        )
        failures += check(
            "dry_run left the file byte-identical",
            (root / "CreatePSOs.cpp").read_bytes() == before,
        )

    print(f"\nRESULT: {'PASS' if failures == 0 else f'FAIL ({failures})'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
