# pix-tool-set

![version](https://img.shields.io/badge/version-2.0.0-blue)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![platform](https://img.shields.io/badge/platform-Windows%20x64-lightgrey)
![license](https://img.shields.io/badge/license-MIT-green)
![deps](https://img.shields.io/badge/python%20deps-0-brightgreen)

面向 AI 客户端的 PIX 截帧（`.wpix`）脚本化分析工具集。94 个 CLI 工具，
每个都自带 JSON Schema、输出统一的 JSON 信封，无需读文档即可被程序驱动：
从「这个 pass 绑了什么资源」到「把 shader 改掉、重编译、看新渲染结果」，全链路可脚本化。

```powershell
pip install -e .
pix-tool-set env-check                                   # 只读体检，缺什么直接说
pix-tool-set session-open --capture D:\caps\frame.wpix   # 一次性导出并建会话
pix-tool-set pass-bindings --pass-name TileClassificationBuildLists --stage CS
```

## 安装

```powershell
cd pix-tool-set
pip install -e .
```

装完先跑一次环境自检，缺什么它会直接告诉你怎么补：

```powershell
pix-tool-set env-check
```

依赖分两层，**只读分析**与**GPU 回放**的要求不同：

| 层 | 需要什么 | 谁提供 |
| --- | --- | --- |
| 只读分析（读 `.wpix`、查 pass/绑定/资源） | Windows x64、Python 3.11+、**Microsoft PIX for Windows** | PIX 需自行安装 |
| Shader 反汇编 / 改源码编译 / PDB 取源码 | `dxcompiler.dll` + `dxil.dll` | 随 PIX 安装；缺失则回退 Windows SDK 的 `dxc.exe` |
| 重建并运行导出工程（`replay-render` `read-uav` `pixel-history-replay` 等） | CMake、Visual Studio C++ 工具链、Windows SDK、D3D12 GPU、D3D12 Agility SDK | 前四项需自行安装；Agility SDK 由导出工程的 CMake 从 nuget 下载 |
| 导出工程链接 WinPixEventRuntime | `WinPixEventRuntime.dll` / `.lib` / `pix3.h` | **本仓库自带**（`src/pix_tool_set/vendor/winpixeventruntime`），离线可用 |

Python 侧**无第三方依赖**。PIX 自动探测 `C:\Program Files\Microsoft PIX\<版本>`，
也可用 `PIXTOOL_PATH` 环境变量或 `--pixtool` 指定。**没有 PIX 就一行也读不了**：
`.wpix` 是闭源容器格式，全部信息来自 `pixtool export-to-cpp` 的产物再解析。

安装后可用 `pix-tool-set` 或简写 `pixts`；未安装时用
`python -m pix_tool_set.cli`（需设置 `PYTHONPATH=<repo>\src`）。

## 三种调用方式

```powershell
# 1) 自描述：列出全部工具及其 schema
pix-tool-set list-tools
pix-tool-set list-tools --category shaders --brief
pix-tool-set describe draw-state

# 2) JSON 调用（推荐给 AI 客户端，参数结构化）
pix-tool-set run list-passes --json-args '{"limit": 10, "sort_by": "triangles"}'

# 3) 直接子命令（人手输入更顺）
pix-tool-set list-passes --limit 10 --sort-by triangles
```

退出码：成功 `0`，工具级错误 `1`，参数错误 `2`。

## 典型工作流

```powershell
pix-tool-set env-check                                    # 新机器先体检（只读，不装东西）
pix-tool-set session-open --capture D:\caps\frame.wpix    # 一次性导出并建会话
pix-tool-set frame-stats                                  # 全帧概览
pix-tool-set list-passes --sort-by triangles --limit 10   # 找最重的 Pass
pix-tool-set analyze-pass --pass-index 12                 # 深挖某个 Pass
pix-tool-set draw-state --draw-index 2461                 # 看某次 draw 的全部绑定
pix-tool-set disassemble-shader --draw-index 2461 --stage PS -o ps.txt
pix-tool-set diagnose-mobile-risks                        # 移动端风险体检
```

## 工具总览（94 个）

以下按功能归类，与 `list-tools` 的 `category` 字段并非一一对应（后者是机器可读的
14 个分类）。数量以 `list-tools` 为准。

**会话管理（5）** `session-open` `session-close` `session-list` `capture-info`
`session-set-pdb-dirs`

**事件与 Action 导航（8）** `list-actions` `action-info` `search-actions`
`find-draw-calls` `locate-event` `find-pass` `queue-attribution`
`list-raytracing-work`

**实测耗时（2）** `export-timing` `event-timing`

**帧统计（4）** `frame-stats` `list-passes` `pass-info` `pass-cost`

**纹理分析（8）** `list-textures` `texture-stats` `texture-info` `export-texture`
`export-draw-textures` `read-texture-pixels` `texture-pixel-stats` `pick-pixel`

**Shader 分析（13）** `shader-stats` `list-shaders` `shader-info` `disassemble-shader`
`shader-reflection` `shader-bindings` `constant-buffer` `pass-bindings`
`pass-shader-source` `pass-values` `shader-edit-begin` `shader-edit-apply`
`shader-edit-diff`

**Shader 源码与编辑（3）** `session-set-pdb-dirs` `shader-edit-begin` `shader-edit-apply`
—— 从引擎 shader PDB 恢复真实 HLSL，改完重编译并校验绑定签名后打补丁到导出工程，
是 PIX Debug 面板 Apply 按钮的可脚本化等价物。

**纹理数值读取（5）** `read-resource-texture` `read-replay-target` `find-depth-content`
`export-uav-slice` `read-uav`

**模型与 DrawCall（4）** `model-stats` `draw-call-stats` `list-draw-calls` `diff-draw-calls`

**管线状态（7）** `list-pipeline-states` `pipeline-state` `draw-state` `vertex-input`
`post-vs-data` `describe-state-object` `describe-shader-table`

**资源管理（4）** `list-resources` `list-buffers` `resource-usage`
`trace-resource-lineage`

**数据导出（4）** `read-buffer` `export-mesh` `save-render-target` `export-report`

**高级分析（6）** `pixel-history` `analyze-pass` `sample-pixel-region` `debug-pixel-shader`
`analyze-acceleration-structures` `analyze-raytracing`

**像素级调试（3）** `pixel-value-history` `trace-downstream` `pixel-history-replay`

**性能分析（3）** `analyze-overdraw` `analyze-bandwidth` `analyze-state-changes`

**诊断（6）** `diagnose-negative-values` `diagnose-precision`
`diagnose-reflection-mismatch` `diagnose-mobile-risks` `diagnose-fixed-function`
`env-check` —— 前五个诊断截帧内容，`env-check` 诊断的是**这台机器**，无需会话。

**状态干预与二分（2）** `replay-override` `bisect-render-state` —— 不改 shader 直接
改写导出工程的固定功能状态，并以像素区域判据自动二分最小复现子集；
`replay-reset` 一键逐字节回滚。

**回放会话与快照（7）** `replay-baseline-check` `replay-edits` `replay-reset`
`frame-replay-dump` `snapshot-list` `snapshot-compare` `snapshot-remove`

**调用活动（3）** `activity-viewer` `activity-log` `replay-render` —— 本地网页实时显示
每次调用与结果、逐步回放调用历史，并把重建后的渲染画面抓成 PNG 显示在面板里。

## 输出信封

```json
{
  "status": "success",
  "tool": "list-passes",
  "data": { "passes": [ ... ], "total": 416, "has_more": true, "next_offset": 10 },
  "output_paths": [],
  "diagnostics": []
}
```

`status` 只有 `success` / `partial` / `error` 三种：`partial` 表示答案可用但某处被降级，
原因写在 `diagnostics` 里；`error` 时 `error.code` 决定恢复路径、`error.suggestion`
给出下一步动作。列表类工具统一分页：`total` / `offset` / `limit` / `returned` /
`has_more` / `next_offset`。

## 更多文档

- 深度使用指南（绑定查询、ID 定位、GPU 耗时、shader 源码/热改、深度纹理、活动面板等）：[Doc/guides/](Doc/guides/)
- 设计理念、`partial` 语义、Python API、架构、验证与已知边界：[Doc/development.md](Doc/development.md)
- 变更记录 [CHANGELOG.md](CHANGELOG.md) ｜ 贡献指南 [CONTRIBUTING.md](CONTRIBUTING.md) ｜ 安全策略 [SECURITY.md](SECURITY.md) ｜ 许可证 [MIT](LICENSE)
