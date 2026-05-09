"""Unit-check the fixed-function state parser (gap one).

Covers the four export shapes the parser must handle: single-line constructors,
constructors spanning lines, D3D12_DEFAULT forms, and independent blend
aggregates. Also drives parse_pipeline_states end-to-end over a minimal
CreatePSOs.cpp so the block absorber and the legacy flat fields are exercised
together, not just the pure helpers.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pix_tool_set.engine import cppparse  # noqa: E402


def check(label: str, ok: bool) -> int:
    print(f"   {label:<44} {'ok' if ok else 'MISMATCH'}")
    return 0 if ok else 1


def main() -> int:
    failures = 0
    print("single-line rasterizer (7 of 11 arguments)")
    rs = cppparse.parse_rasterizer_block(
        "pssDesc.RasterizerState = CD3DX12_RASTERIZER_DESC("
        "D3D12_FILL_MODE_SOLID, D3D12_CULL_MODE_BACK, TRUE, 0, 0.f, 2.0f, TRUE);"
    )
    failures += check("parsed=partial (fewer args than fields)", rs["parsed"] == "partial")
    failures += check("fill_mode", rs["fill_mode"] == "D3D12_FILL_MODE_SOLID")
    failures += check("cull_mode", rs["cull_mode"] == "D3D12_CULL_MODE_BACK")
    failures += check("front_counter_clockwise=True", rs["front_counter_clockwise"] is True)
    failures += check("slope_scaled_depth_bias=2.0", rs["slope_scaled_depth_bias"] == 2.0)
    failures += check("depth_clip_enable=True", rs["depth_clip_enable"] is True)
    failures += check("tail fields absent, not guessed",
                      "conservative_raster" not in rs)

    print("multi-line rasterizer (full 11 arguments)")
    rs = cppparse.parse_rasterizer_block(
        "pssDesc.RasterizerState = CD3DX12_RASTERIZER_DESC(\n"
        "    D3D12_FILL_MODE_WIREFRAME, D3D12_CULL_MODE_NONE, FALSE, -2, 1.5f, 0.f,\n"
        "    FALSE, FALSE, FALSE, 0, D3D12_CONSERVATIVE_RASTERIZATION_MODE_ON);"
    )
    failures += check("parsed=full", rs["parsed"] == "full")
    failures += check("wireframe", rs["fill_mode"] == "D3D12_FILL_MODE_WIREFRAME")
    failures += check("cull none", rs["cull_mode"] == "D3D12_CULL_MODE_NONE")
    failures += check("depth_bias=-2", rs["depth_bias"] == -2)
    failures += check("depth_bias_clamp=1.5", rs["depth_bias_clamp"] == 1.5)
    failures += check("conservative on",
                      rs["conservative_raster"] == "D3D12_CONSERVATIVE_RASTERIZATION_MODE_ON")

    print("D3D12_DEFAULT rasterizer")
    rs = cppparse.parse_rasterizer_block(
        "pssDesc.RasterizerState = CD3DX12_RASTERIZER_DESC(D3D12_DEFAULT);"
    )
    failures += check("parsed=default", rs["parsed"] == "default")
    failures += check("default fill mode", rs["fill_mode"] == "D3D12_FILL_MODE_SOLID")
    failures += check("default cull mode", rs["cull_mode"] == "D3D12_CULL_MODE_BACK")
    failures += check("default depth clip on", rs["depth_clip_enable"] is True)

    print("depth-stencil, full 14 arguments with nested args")
    ds = cppparse.parse_depth_stencil_block(
        "pssDesc.DepthStencilState = CD3DX12_DEPTH_STENCIL_DESC(\n"
        "    TRUE, D3D12_DEPTH_WRITE_MASK_ZERO, D3D12_COMPARISON_FUNC_LESS_EQUAL,\n"
        "    TRUE, 254, 253,\n"
        "    D3D12_STENCIL_OP_REPLACE, D3D12_STENCIL_OP_DECR_SAT, D3D12_STENCIL_OP_KEEP,\n"
        "    D3D12_COMPARISON_FUNC_GREATER,\n"
        "    D3D12_STENCIL_OP_INVERT, D3D12_STENCIL_OP_INCR_SAT, D3D12_STENCIL_OP_ZERO,\n"
        "    D3D12_COMPARISON_FUNC_NEVER);"
    )
    failures += check("parsed=full", ds["parsed"] == "full")
    failures += check("depth write mask ZERO kept verbatim",
                      ds["depth_write_mask"] == "D3D12_DEPTH_WRITE_MASK_ZERO")
    failures += check("depth func", ds["depth_func"] == "D3D12_COMPARISON_FUNC_LESS_EQUAL")
    failures += check("stencil enabled", ds["stencil_enable"] is True)
    failures += check("stencil read mask 254", ds["stencil_read_mask"] == 254)
    failures += check("front fail op", ds["front_face"]["fail_op"] == "D3D12_STENCIL_OP_REPLACE")
    failures += check("front func", ds["front_face"]["func"] == "D3D12_COMPARISON_FUNC_GREATER")
    failures += check("back pass op", ds["back_face"]["pass_op"] == "D3D12_STENCIL_OP_ZERO")
    failures += check("back func", ds["back_face"]["func"] == "D3D12_COMPARISON_FUNC_NEVER")

    print("depth-stencil D3D12_DEFAULT")
    ds = cppparse.parse_depth_stencil_block(
        "pssDesc.DepthStencilState = CD3DX12_DEPTH_STENCIL_DESC(D3D12_DEFAULT);"
    )
    failures += check("parsed=default", ds["parsed"] == "default")
    failures += check("default write mask", ds["depth_write_mask"] == "D3D12_DEPTH_WRITE_MASK_ALL")
    failures += check("default stencil off", ds["stencil_enable"] is False)

    print("blend render target aggregate (independent blend)")
    rt = cppparse.parse_blend_rt_block(
        "blendDesc.RenderTarget[1] = {\n"
        "    TRUE, FALSE,\n"
        "    D3D12_BLEND_SRC_ALPHA, D3D12_BLEND_INV_SRC_ALPHA, D3D12_BLEND_OP_ADD,\n"
        "    D3D12_BLEND_ONE, D3D12_BLEND_ZERO, D3D12_BLEND_OP_ADD,\n"
        "    D3D12_LOGIC_OP_NOOP, D3D12_COLOR_WRITE_ENABLE_RED | D3D12_COLOR_WRITE_ENABLE_GREEN\n"
        "};",
        1,
    )
    failures += check("parsed=full", rt["parsed"] == "full")
    failures += check("index preserved", rt["index"] == 1)
    failures += check("blend_enable", rt["blend_enable"] is True)
    failures += check("logic_op_enable", rt["logic_op_enable"] is False)
    failures += check("src_blend", rt["src_blend"] == "D3D12_BLEND_SRC_ALPHA")
    failures += check("dest_blend", rt["dest_blend"] == "D3D12_BLEND_INV_SRC_ALPHA")
    failures += check("blend_op", rt["blend_op"] == "D3D12_BLEND_OP_ADD")
    failures += check("write mask keeps both channels",
                      "RED" in rt["render_target_write_mask"]
                      and "GREEN" in rt["render_target_write_mask"])

    print("blend render target D3D12_DEFAULT")
    rt = cppparse.parse_blend_rt_block(
        "blendDesc.RenderTarget[0] = { D3D12_DEFAULT };", 0
    )
    failures += check("parsed=default", rt["parsed"] == "default")
    failures += check("default blend off", rt["blend_enable"] is False)
    failures += check("default write mask all",
                      rt["render_target_write_mask"] == "D3D12_COLOR_WRITE_ENABLE_ALL")

    print("end-to-end parse_pipeline_states on a minimal CreatePSOs.cpp")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "CreatePSOs.cpp").write_text(
            "\n".join(
                [
                    "void CreatePipelineState_10()",
                    "{",
                    "    g_resourceReader->Read(data, 128);",
                    "    pssDesc.RasterizerState = CD3DX12_RASTERIZER_DESC(",
                    "        D3D12_FILL_MODE_SOLID, D3D12_CULL_MODE_BACK, FALSE, 0,",
                    "        0.f, 0.f, TRUE, FALSE, FALSE, 0,",
                    "        D3D12_CONSERVATIVE_RASTERIZATION_MODE_OFF);",
                    "    pssDesc.DepthStencilState = CD3DX12_DEPTH_STENCIL_DESC(",
                    "        TRUE, D3D12_DEPTH_WRITE_MASK_ALL, D3D12_COMPARISON_FUNC_LESS,",
                    "        FALSE, 255, 255, D3D12_STENCIL_OP_KEEP, D3D12_STENCIL_OP_KEEP,",
                    "        D3D12_STENCIL_OP_KEEP, D3D12_COMPARISON_FUNC_ALWAYS,",
                    "        D3D12_STENCIL_OP_KEEP, D3D12_STENCIL_OP_KEEP, D3D12_STENCIL_OP_KEEP,",
                    "        D3D12_COMPARISON_FUNC_ALWAYS);",
                    "    blendDesc.RenderTarget[0] = { TRUE, FALSE, D3D12_BLEND_SRC_ALPHA,",
                    "        D3D12_BLEND_INV_SRC_ALPHA, D3D12_BLEND_OP_ADD, D3D12_BLEND_ONE,",
                    "        D3D12_BLEND_ZERO, D3D12_BLEND_OP_ADD, D3D12_LOGIC_OP_NOOP,",
                    "        D3D12_COLOR_WRITE_ENABLE_ALL };",
                    "    pssDesc.BlendState.AlphaToCoverageEnable = TRUE;",
                    "}",
                    "void CreatePipelineState_11()",
                    "{",
                    "    g_resourceReader->Read(data, 64);",
                    "    pssDesc.RasterizerState = CD3DX12_RASTERIZER_DESC(D3D12_DEFAULT);",
                    "}",
                ]
            ),
            encoding="utf-8",
        )
        result = cppparse.parse_pipeline_states(root)
        pso = result.pipeline_states[10]
        failures += check("pso 10 parsed", pso is not None)
        failures += check("legacy fill_mode kept", pso.fill_mode == "D3D12_FILL_MODE_SOLID")
        failures += check("legacy cull_mode kept", pso.cull_mode == "D3D12_CULL_MODE_BACK")
        failures += check("legacy depth_write kept", pso.depth_write is True)
        failures += check("legacy depth_enabled kept", pso.depth_enabled is True)
        failures += check("legacy blend_enabled kept", pso.blend_enabled is True)
        failures += check("new rasterizer dict present",
                          pso.rasterizer.get("parsed") == "full")
        failures += check("new depth_stencil dict present",
                          pso.depth_stencil.get("parsed") == "full")
        failures += check("new blend dict with rt0",
                          pso.blend["render_targets"][0]["index"] == 0)
        failures += check("alpha_to_coverage flag",
                          pso.blend.get("alpha_to_coverage") is True)
        failures += check("legacy blend_states gained src_blend",
                          pso.blend_states[0].get("src_blend") == "D3D12_BLEND_SRC_ALPHA")
        pso11 = result.pipeline_states[11]
        failures += check("pso 11 D3D12_DEFAULT tagged",
                          pso11.rasterizer.get("parsed") == "default")
        failures += check("read_sizes kept for both PSOs",
                          result.read_sizes == [128, 64])

    print(f"\nRESULT: {'PASS' if failures == 0 else f'FAIL ({failures})'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
