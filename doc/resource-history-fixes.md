# Resource history fixes and troubleshooting notes

This document records the recent fixes and pitfalls around PIX resource binding and access-history analysis.

## Scope

The fixes focus on `get-event-resource` and `get-resource-access-history` for exported PIX C++ projects:

- Resolve shader-declared `CBV`, `SRV`, `UAV`, and static sampler bindings from recovered shader source.
- Match descriptor table entries back to shader declarations instead of returning only raw descriptor resources.
- Handle compute and graphics pipeline layouts, including input assembler and output merger resources.
- Keep CLI and MCP tool names unified through the shared registry.

## Fixes

### Nullable CLI/MCP parameters

Some client calls pass optional numeric arguments as `null` instead of omitting them.

- `analyze-events` accepts nullable `top_limit` and `sample_limit` and falls back to defaults.
- `get-resource-access-history` accepts nullable `descriptor_scan_count` and falls back to the default scan count.

Pitfall: treat `None` from JSON clients as a caller asking for the default value, not as an invalid integer.

### Shader declaration parsing

Resource binding resolution now parses declarations from recovered shader source:

- Constant buffers with explicit registers, such as `cbuffer View : register(b0)`.
- Constant buffers without explicit registers.
- `Texture*`, `Buffer`, `StructuredBuffer`, `ByteAddressBuffer`, and `RW*` resources.
- Static samplers with `register(sN, spaceM)`.
- Static samplers without explicit registers.

Pitfall: resource declarations inside `cbuffer` bodies must be ignored. The parser removes constant-buffer bodies before scanning resource declarations so member variables are not mistaken for resources.

### Multiple CBVs and fallback names

Some captures bind more root `CBV`s than the shader source clearly declares.

- Missing `CBV` declarations are filled with stable fallback names such as `_RootShaderParameters`, `View`, and `ReflectionCaptureSM5`.
- Slot numbers are kept deterministic.
- Constant-buffer usage statistics help select likely `CBV`s when there are more declarations than root bindings.

Pitfall: generic buffer resource names like `Resource Allocator Underlying Buffer` do not identify the shader binding. Display names must combine the resource name with the shader declaration name.

### Descriptor table matching

Descriptor entries are matched to shader declarations by view type, resource dimension, name tokens, and descriptor format hints.

- `SRV` and `UAV` bindings are matched independently.
- Texture descriptors are not matched to buffer declarations, and buffer descriptors are not matched to texture declarations.
- Name-token scoring handles cases such as normal/tangent/color/position buffers.
- Descriptor tables can overlap; the resolver keeps the table with the most specific root descriptor range.

Pitfall: scanning a fixed small number of descriptors is not enough when shader declarations exceed the default scan window. The scan window must be at least the number of declared `SRV` plus `UAV` resources.

### Static sampler filtering

Static samplers are included as resources with `view_type` set to `Static Sampler`.

- Register space is preserved when present.
- Unregistered samplers receive deterministic slots.
- In graphics shaders, sampler noise is reduced by keeping samplers related to texture declarations when possible.

Pitfall: static samplers do not have resource IDs or descriptor writes, so they need a separate resolution path.

### Graphics pipeline support

Graphics events are resolved across pipeline stages instead of being treated as compute-only events.

- Input assembler `VB` and `IB` resources are included.
- Vertex and pixel shader resources are resolved from stage-specific shader source.
- Output merger `RTV`, depth, and stencil resources are included.
- Root `CBV`s and descriptor tables are partitioned between stages by binding order and table scores.

Pitfall: graphics root bindings may appear in stage runs rather than in simple numeric root-index order. Use line/order metadata when available, then score candidate tables by declared resources.

### Descriptor heap disambiguation

Descriptor indices can repeat across different heaps.

- Descriptor lookup filters writes by `heap_id` when the root binding provides one.
- This avoids matching resources from another heap that happens to use the same descriptor index.

Pitfall: descriptor index alone is not always globally unique in exported PIX code.

### Access history rows

`get-resource-access-history` builds access rows from resource references and adds the target event shader binding row.

- Copy operations classify source and destination as read/write correctly.
- Barriers and transitions are treated as read/write state changes.
- `UAV` shader bindings default to `Read/Write` and `STATE_UNORDERED_ACCESS`.
- Duplicate rows are removed by event, binding, state, line, and text.

Pitfall: the event itself may not contain a direct API reference to the resource even though the shader binding uses it. Add a shader-binding row for the selected event.

## Validation checklist

Run the focused tests after editing resource binding logic:

```powershell
python -m pytest tests/test_registry_and_export.py
```

Important cases covered by the tests:

- Built-in tools are registered once.
- CLI and MCP names stay identical.
- Minimal C++ export validation accepts the required PIX files.
- Nullable optional parameters use defaults.
- Compute shader resources resolve declared `CBV`, `SRV`, `UAV`, and static sampler bindings.
- Overlapping descriptor tables resolve to the expected shader declarations.
- Graphics events include `IA`, shader-stage, `OM`, depth, and stencil resources.

## Implementation guardrails

- Keep user-facing outputs structured with `status`, `data`, `output_paths`, `diagnostics`, and optional `error`.
- Do not write regular diagnostics to stdout in stdio MCP mode.
- Prefer absolute paths in command examples.
- Keep examples generic and avoid machine- or project-specific export directory names.
- Add regression tests for every new capture layout before changing matching heuristics.

## 2026-05-18 ManyLights GlobalID 2864 绑定资源修复

- 修复图形管线根描述符表评分逻辑：当同一描述符表扫描窗口中包含 shader 未声明的 `UAV` 描述符时，不再因为这些额外候选资源否决该表，从而保留 `VS` 阶段真实声明的 `SRV Buffer` 绑定。
- `db-get-event-resource` 新增 `pdb_search_paths` 参数：当调用方提供 PDB 路径时，工具会删除该事件旧的 `runtime_resolved` 资源缓存，复用现有 shader 解析流程重新解析绑定并回写数据库，然后再从 SQLite 读取最终结果。
- 针对 `GlobalID=2864` 的截图布局补充回归测试，覆盖 `CBV 0/1`、连续 `SRV Buffer 0/1/2` 后跟额外 `UAV` 描述符的场景。
- 验证命令：`python -m pytest tests/test_registry_and_export.py -k "graphics_pipeline_stage_1632_layout or keeps_graphics_srv_table_with_trailing_uavs"`。

## 2026-05-18 仅保留数据库工具并移除非 DB 资源回退

- 内建工具加载入口现在只加载 `database_query_tools`，注册层也会跳过所有非 `db-` 前缀的工具，避免旧工具模块被直接导入时重新暴露非 DB 工具。
- `db-get-event-resource` 不再调用旧的 `get_event_resource` 非数据库查询路径；它会从 SQLite 的 `events`、`resources`、`descriptor_writes`、`shader_metadata` 等表读取数据，基于数据库缓存的 shader source 解析声明，重算并回写 `event_bound_resources`，最后仍从 SQLite 返回结果。
- `db-get-event-shader-source` 新增 `pdb_search_paths` 与 `resolver_path` 参数，用作唯一的 shader source 数据库缓存刷新入口；刷新后返回内容仍来自 SQLite 的 `shader_metadata` 表。
- 验证 `list-tools` 只剩 5 个 `db-*` 工具；验证 `db-get-event-resource --global-id 2864` 返回 10 个资源，且诊断为 `query_mode=sqlite`。
- 验证命令：`python -m pytest tests/test_registry_and_export.py::test_builtin_tools_are_registered tests/test_registry_and_export.py::test_cli_and_mcp_names_are_unified tests/test_registry_and_export.py::test_get_event_resource_keeps_graphics_srv_table_with_trailing_uavs tests/test_capture_db.py`。

## 2026-05-18 ManyLights GlobalID 3854 数据库资源刷新修复

- 修复 `db-get-event-resource` 只读取旧的数据库预计算绑定导致 `GlobalID=3854` 返回 80 项 descriptor 扫描结果的问题。
- `db-get-event-resource` 现在支持直接传入 `pdb_search_paths` 与 `resolver_path`：查询资源前会先刷新该事件 PSO 的 shader source cache，再基于 SQLite 中的 shader source、event、descriptor 和 resource 数据重算 `event_bound_resources`。
- 该改动让 `GlobalID=3854` 的绑定结果从错误的 80 项收敛为真实的 26 项：`IA` 4 项、`VS` 7 项、`PS` 7 项、`OM` 8 项，与 PIX 截图一致。
- 补充 `test_db_get_event_resource_refreshes_shader_source_before_resources` 回归测试，确保资源重算发生在 shader source cache 刷新之后。
- 验证命令：`python -m pytest tests/test_registry_and_export.py::test_db_get_event_resource_refreshes_shader_source_before_resources tests/test_registry_and_export.py::test_builtin_tools_are_registered tests/test_registry_and_export.py::test_get_event_resource_supports_graphics_pipeline_stage_1632_layout tests/test_registry_and_export.py::test_get_event_resource_keeps_graphics_srv_table_with_trailing_uavs`。
- 真实验证：使用当前工作区代码查询 `GlobalID=3854` 返回 26 项，查询 `GlobalID=2864` 仍返回 10 项。

## 2026-05-19 ManyLights 训练用例数据库查询修复

- 修复 `db-get-event-resource` 针对 compute dispatch 事件仍走图形管线重算路径的问题，避免 `GlobalID=3553` 和 `GlobalID=3968` 混入 `IA/OM` 资源。
- 数据库资源重算现在会根据事件类型选择图形或计算路径，并在计算路径中按 `CBV`、`SRV`、`UAV`、静态采样器分别解析 shader 声明与根绑定。
- 修复 descriptor table 扫描过早使用根绑定源码行号截断的问题，保证 `GlobalID=3968` 能从 SQLite 的 `descriptor_writes` 中补齐连续 `SRV/UAV` 绑定。
- `db-get-event-resource` 输出层新增确定性资源展示名称归一化，例如 `CBV 0 : View`、`SRV Buffer 0 : ...`、`Static Sampler [1, space=1000] : ...`，使 MCP 与 CLI 输出保持一致。
- `db-get-event-shader-source` 保留原有 `resolver_result` 的同时，在每个 stage 上补充扁平化的 `source_text` 与 `source_summary`，便于测试和调用方直接检查 HLSL 内容。
- 修正训练数据 `scenario_04` 的资源预期计数：该文件资源数组实际为 15 项，`resource_count` 同步更新为 15。
- 真实验证：`GlobalID=3854` 返回 26 项且名称完全匹配，`GlobalID=3553` 返回 15 项且满足 `resource_count >= 15` 与 shader source 断言，`GlobalID=3968` 返回 15 项且名称完全匹配。

## 2026-05-19 恢复导出、索引、数据库和 PDB 解析工具

- 恢复 `export-to-cpp`、`check-cpp-export`、`build-index`、`get-event-shader-source` 的内建加载和注册。
- `build-index` 继续作为生成索引和生成 SQLite 数据库的统一入口，返回 `index_path` 与 `database_path`。
- 注册层保留显式白名单，只恢复上述必要非 DB 工具；其余旧非 DB 分析工具仍不会注册，避免重新引入旧资源查询回退路径。
- 注册测试同步更新为 9 个可用工具，并额外断言 `analyze-events`、`get-event-resource`、`get-resource-access-history` 仍未注册。

## 2026-05-19 数据库绑定资源确定性修复

- 修复 `db-get-event-resource` 在 shader source 资源重算失败时继续返回旧 `precomputed` 或 `descriptor_table_scan` 缓存的问题；现在只把 `database_resolved` 结果作为精确绑定返回。
- 当数据库中只有旧的预计算 descriptor 扫描缓存时，工具返回 `partial`，`resource_count` 为 0，并在诊断中给出 `discarded_precomputed_resource_count` 与原因，避免异常多的粗略资源伪装成成功结果。
- 修复计算管线 descriptor table 连续计数使用全局最新 descriptor write 的问题；现在会传入当前 root binding 的 `line`，只选择绑定行之前的 descriptor 写入，避免后续事件复用 descriptor heap 污染当前事件。
- 补充回归测试覆盖旧预计算缓存丢弃和 descriptor 连续计数行号过滤。

## 2026-05-19 跨导出文件 descriptor 行号过滤修复

- 修复 `CommandLists_*.cpp` 中的 root descriptor table 与 `Descriptors_*.cpp` / `ModifyDescriptors_*.cpp` 中的 descriptor write 直接比较源码行号导致 `SRV/UAV` 全部丢失的问题。
- descriptor write 可见性现在只在 root binding 与 descriptor write 来自同一个源码文件时使用 `line` 过滤；跨文件时保留数据库中按 `write_order` 排序的最新 descriptor 写入。
- 数据库重算路径会把事件文件上下文注入 root descriptor table，并同步到底层 descriptor 解析，确保 table 候选选择和实际资源解析使用相同规则。
- 补充回归测试覆盖跨文件 descriptor write 不被行号误过滤、同文件后续 descriptor write 仍会被过滤。

## 2026-05-19 Root Signature 元数据数据库化

- 索引器现在会记录 `SetComputeRootSignature` / `SetGraphicsRootSignature` 的当前 root signature id，并把它写入事件与 root binding。
- 新增对 `FrameResources_*.cpp` 中 root signature 创建代码的轻量解析，提取 root parameter 类型、CBV register，以及 descriptor table 的 range 类型、数量、base register、space 和 offset。
- 事件的 root descriptor table 与 root CBV 会携带 `root_signature_layout`，数据库事件表新增 `root_signature_id` 字段，便于后续 SQL 查询基于真实 root layout 判断 `SRV/UAV/CBV` 绑定。
- 提升索引版本和数据库 schema 版本，确保旧缓存会在下次构建时刷新。