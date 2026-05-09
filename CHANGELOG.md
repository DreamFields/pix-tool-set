# Changelog
本项目的所有重要变更都记录在此文件中。
格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。
## [2.0.0] - 2026-08-17
AI 客户端友好化重构后的首个稳定版本：94 个 CLI 工具、统一 JSON 输出信封、命名会话、GPU 回放与 shader 热替换全链路。
### Added
- **会话管理**：`session-open` 一次性导出并登记命名会话，后续查询毫秒级复用、跨进程有效。
- **自描述能力**：`list-tools` / `describe` 输出机器可读的工具目录与 JSON Schema 契约。
- **环境自检**：`env-check` 只读探测 PIX、dxcompiler、CMake、VS 生成器、Windows SDK、D3D12 设备、vendored WinPixEventRuntime 与 Agility SDK，每条失败自带修复动作。
- **跨队列选择器**：`Global ID` 提升为跨队列主选择器，14 个只接受 `--queue-id` 的工具全部支持 `--global-id`；`queue-attribution` 回答帧的队列归属。
- **实测 GPU 耗时**：`export-timing` / `event-timing`，`pass-cost` 可从工作量模型切换为实测毫秒。
- **Shader 源码恢复**：`pass-shader-source` 经 `IDxcPdbUtils`（ctypes 裸 COM）从 UE5 shader PDB 恢复真实 HLSL 与原始编译参数。
- **绑定资源取值**：`pass-values` / `constant-buffer` / `read-buffer`；解决 resources.bin 尾部段顺序问题，2,625 个带 root CBV 的 draw 全部可信解码。
- **深度/纹理读取**：`read-resource-texture`（截帧字节路径）与 `read-replay-target`（GPU 回放 DDS 无损路径）；`find-depth-content` 自动定位含真实几何的深度事件。
- **UAV 数组切片导出**：`export-uav-slice`（如 RWLightGrid），Texture3D 支持 `--z` 选层。
- **Shader 热替换**：`shader-edit-begin` / `shader-edit-apply` / `shader-edit-diff`，PIX Debug 面板 Apply 的可脚本化等价物，含绑定签名安全检查与 DXR 支持。
- **状态干预与二分**：`replay-override` / `bisect-render-state` / `replay-reset`，固定功能状态改写、逐字节可回滚。
- **光追工具链**：`analyze-raytracing`、`analyze-acceleration-structures`、`describe-state-object`、`describe-shader-table`、`list-raytracing-work`。
- **像素级调试**：`pixel-value-history`、`trace-downstream`、`pixel-history-replay`。
- **资源谱系**：`trace-resource-lineage` 跨 pass 资源生产-消费契约分析。
- **调用活动面板**：`activity-viewer` 本地网页实时跟随调用流、历史逐步回放；`replay-render` 抓取重建渲染画面并支持基线对比；`--export` 产出单文件离线 HTML。
- **UAV 探针**：`read-uav` 查看 dispatch 实际写入 UAV 的内容。
- Vendor WinPixEventRuntime，回放构建离线可用。
### Fixed
- 多 command list 交错导致的 marker 路径串味：draw 的 pass 路径以事件列表 CSV 的 `Parent` 显式父链为准。
- `ExecuteIndirect` 按 command signature 的 argument type 判定图形/计算管线，修复间接调用绑定读空。
- 描述符表按真实 root signature 声明范围展开，修复 UE5 SRV table（64 项）被旧默认值（16 项）截断。
- cbuffer 解析：外层 struct 大小收尾行不再泄漏为假字段；补齐 16 字节对齐的尾部 `pad`。
- 多 CBV 场景按 `(shader_register, visibility)` 配对，修复同寄存器号跨阶段重复声明的误配。
- 事件名含逗号时被截断的问题。
### Known boundaries
- `partial` 语义制度化：数据本就不存在（PIX 截帧的客观边界）时返回 `partial` 并在 `diagnostics` 说明，不伪装成功。详见 README「`partial` 的含义」。
## [1.0.0] - 2026-08-03
初始版本：事件树查询、事件资源/shader 关联、资源历史追踪，55 个工具的 AI 友好 CLI 雏形。
[2.0.0]: https://github.com/DreamFields/pix-tool-set/compare/v1.0.0...v2.0.0
[1.0.0]: https://github.com/DreamFields/pix-tool-set/releases/tag/v1.0.0
