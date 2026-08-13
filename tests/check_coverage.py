"""Final acceptance check: coverage of Doc/requirement.md by registered tools."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pix_tool_set import __version__, list_tools  # noqa: E402
from pix_tool_set.registry import CATEGORY_TITLES, get_registry  # noqa: E402
from pix_tool_set.tools import load_builtin_tools  # noqa: E402

REQUIREMENT = ROOT / "Doc" / "requirement.md"

# Raytracing D3D12 entry points, and the tool that answers for each. A frame need
# not exercise every one; "implemented but no sample in this capture" is a distinct
# and legitimate state from "not covered", and conflating the two would either hide
# a real gap or raise a false alarm on every non-raytracing capture.
RAYTRACING_API_COVERAGE: dict[str, str] = {
    "CreateStateObject": "describe-state-object",
    "AddToStateObject": "describe-state-object",
    "SetPipelineState1": "list-raytracing-work",
    "DispatchRays": "describe-shader-table",
    "BuildRaytracingAccelerationStructure": "analyze-acceleration-structures",
    "CopyRaytracingAccelerationStructure": "analyze-acceleration-structures",
    "EmitRaytracingAccelerationStructurePostbuildInfo": "analyze-acceleration-structures",
}

# requirement heading -> tool that implements it
MAPPING: dict[str, str] = {
    "打开捕获文件": "session-open",
    "关闭捕获": "session-close",
    "查询捕获基本信息": "capture-info",
    "列出 Action": "list-actions",
    "查询单个 Action 详情": "action-info",
    "搜索 Action": "search-actions",
    "查找 DrawCall": "find-draw-calls",
    "定位当前事件": "locate-event",
    "查询整体帧统计": "frame-stats",
    "列出渲染 Pass": "list-passes",
    "查询 Pass 详情": "pass-info",
    "查询 Pass 耗时": "pass-cost",
    "列出纹理": "list-textures",
    "查询纹理统计": "texture-stats",
    "查询纹理详情": "texture-info",
    "导出纹理": "export-texture",
    "导出 DrawCall 相关纹理": "export-draw-textures",
    "读取纹理像素": "read-texture-pixels",
    "读取纹理统计": "texture-pixel-stats",
    "拾取像素": "pick-pixel",
    "查询 Shader 统计": "shader-stats",
    "查询 Shader 详情": "shader-info",
    "反汇编 Shader": "disassemble-shader",
    "查询 Shader 反射信息": "shader-reflection",
    "查询 Shader 绑定": "shader-bindings",
    "查询常量缓冲区内容": "constant-buffer",
    "查询模型统计": "model-stats",
    "查询 DrawCall 统计": "draw-call-stats",
    "列出 DrawCall 事件": "list-draw-calls",
    "比对 DrawCall 差异": "diff-draw-calls",
    "查询管线状态": "pipeline-state",
    "查询 DrawCall 状态": "draw-state",
    "查询顶点输入": "vertex-input",
    "查询 PostVS 数据": "post-vs-data",
    "列出资源": "list-resources",
    "列出缓冲区": "list-buffers",
    "查询资源使用": "resource-usage",
    "读取缓冲区数据": "read-buffer",
    "导出网格": "export-mesh",
    "保存渲染目标": "save-render-target",
    "查询像素历史": "pixel-history",
    "分析渲染 Pass": "analyze-pass",
    "采样像素区域": "sample-pixel-region",
    "调试像素处 Shader": "debug-pixel-shader",
    "分析过度绘制": "analyze-overdraw",
    "分析带宽": "analyze-bandwidth",
    "分析状态切换": "analyze-state-changes",
    "诊断负向值问题": "diagnose-negative-values",
    "诊断精度问题": "diagnose-precision",
    "诊断反射不一致": "diagnose-reflection-mismatch",
    "诊断移动端风险": "diagnose-mobile-risks",
}


def parse_requirements() -> list[str]:
    text = REQUIREMENT.read_text(encoding="utf-8")
    return [line[4:].strip() for line in text.splitlines() if line.startswith("### ")]


def main() -> int:
    load_builtin_tools()
    registry = get_registry()

    print("=" * 72)
    print(f"pix-tool-set {__version__} acceptance check")
    print("=" * 72)

    requirements = parse_requirements()
    print(f"\nrequirement items: {len(requirements)}")
    print(f"registered tools : {len(registry.list_tools())}")

    missing: list[str] = []
    unmapped: list[str] = []
    print("\n--- requirement coverage ---")
    for item in requirements:
        tool_name = MAPPING.get(item)
        if tool_name is None:
            unmapped.append(item)
            print(f"  [?]  {item}  -> (no mapping recorded)")
            continue
        if not registry.has(tool_name):
            missing.append(item)
            print(f"  [X]  {item}  -> {tool_name} MISSING")
        else:
            print(f"  [ok] {item:26s} -> {tool_name}")

    extras = sorted(
        {definition.name for definition in registry.list_tools()} - set(MAPPING.values())
    )
    print(f"\n--- additional tools beyond the requirement ({len(extras)}) ---")
    for name in extras:
        print(f"  {name}: {registry.get(name).summary[:70]}")

    print("\n--- raytracing API coverage ---")
    problems: list[str] = []
    uncovered: list[str] = []
    for api, tool_name in RAYTRACING_API_COVERAGE.items():
        if not tool_name:
            # Recorded deliberately rather than omitted: an API with no tool is a
            # known gap, and leaving it out of the table would let it be forgotten.
            print(f"  [--] {api:48s} -> no tool yet (known gap)")
            uncovered.append(api)
            continue
        if not registry.has(tool_name):
            print(f"  [X]  {api:48s} -> {tool_name} MISSING")
            problems.append(f"raytracing api {api}: {tool_name} not registered")
            continue
        print(f"  [ok] {api:48s} -> {tool_name}")
    covered = len(RAYTRACING_API_COVERAGE) - len(uncovered)
    print(
        f"  {covered}/{len(RAYTRACING_API_COVERAGE)} raytracing entry points have a tool"
    )
    if uncovered:
        print(f"  known gaps: {', '.join(uncovered)}")

    print("\n--- schema sanity ---")
    for definition in registry.list_tools():
        if not definition.summary:
            problems.append(f"{definition.name}: empty summary")
        if definition.parameters.get("type") != "object":
            problems.append(f"{definition.name}: parameters is not an object schema")
        for param, schema in definition.parameters.get("properties", {}).items():
            if not schema.get("description"):
                problems.append(f"{definition.name}.{param}: missing description")
            if not schema.get("type"):
                problems.append(f"{definition.name}.{param}: missing type")
        if not definition.returns:
            problems.append(f"{definition.name}: missing returns description")
    if problems:
        for entry in problems[:20]:
            print(f"  [X] {entry}")
        print(f"  total schema problems: {len(problems)}")
    else:
        print("  all tools expose complete, described schemas")

    print("\n--- CLI smoke ---")
    for argv in (["list-tools", "--brief"], ["describe", "frame-stats"]):
        proc = subprocess.run(
            [sys.executable, "-m", "pix_tool_set.cli", *argv],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(ROOT),
            env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")},
        )
        try:
            payload = json.loads(proc.stdout)
            print(f"  {' '.join(argv):26s} -> {payload.get('status')} (valid JSON)")
        except json.JSONDecodeError:
            print(f"  {' '.join(argv):26s} -> INVALID JSON")
            problems.append(f"cli {argv}: invalid json")

    print("\n" + "=" * 72)
    ok = not missing and not unmapped and not problems
    print(
        f"coverage: {len(requirements) - len(missing) - len(unmapped)}/{len(requirements)} "
        f"requirement items | schema problems: {len(problems)}"
    )
    print("RESULT:", "PASS" if ok else "ATTENTION NEEDED")
    print("=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
