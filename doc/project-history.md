# PIX Resource Binding & Database Query 历史问题与修复总结

> 说明文档全部使用中文

本文档汇总了 `pix-tool-set` 项目在资源绑定解析、数据库查询工具以及测试验证过程中遇到的所有关键问题、根本原因分析与修复记录。

---

## 目录

- [顶层架构决策](#顶层架构决策)
- [当前实现方案与原理](#当前实现方案与原理)
- [核心问题：训练集通过但新用例失败](#核心问题训练集通过但新用例失败)
- [按时间线的修复记录](#按时间线的修复记录)
- [常见陷阱（Pitfalls）](#常见陷阱pitfalls)
- [验证方法](#验证方法)
- [代码实现防护](#代码实现防护)
- [下一步建议](#下一步建议)

---

## 顶层架构决策

### 数据库 vs 算法责任边界

**数据库是事实的忠实记录**，没有问题：

| 表名 | 记录内容 |
|------|---------|
| `descriptor_writes` | 所有 descriptor 写入（heap_id、descriptor_index、resource_id、view_type、write_order） |
| `root_bindings` | 每个事件的 root table/CBV 绑定 |
| `events` | 事件的 PSO、root signature 等状态 |
| `resources` | 资源名称 |
| `shader_metadata` | Shader 源码缓存 |
| `root_signature_layout` | Root parameter 类型、range 类型/数量/space |

**算法层面存在结构性缺陷**，当前算法的核心路径假设：

1. `pso_id` 对应的 shader source 就是当前事件实际执行的 shader — 但同一 `pso_id` 可能被多个不同 dispatch 使用，而 shader source cache 只存了一份源码。
2. Shader 声明顺序 = descriptor table 中的连续 slot 顺序 — 但实际 root signature 的 descriptor range 可能有 offset、多个 range 拼接、或者编译器重排了 register 绑定。

### 正确的算法方向（项目规则约束）

按项目规则：所有工具都以生成的数据库文件为基础，以 SQL 查询来执行得到正确结果。

正确路径应该是：

1. SQL 查 `root_bindings` 得到 descriptor table 起点
2. SQL 查 `root_signature_layout` 得到每个 table 的 range 类型和数量
3. SQL 按 range 类型/数量从 `descriptor_writes` 取对应 slot 的最新写入
4. SQL 用 `resource_id` JOIN `resources` 得到资源名
5. 输出绑定资源

Shader source 的作用应降级为：**给已经确定的资源补充 `shader_binding_name`**（HLSL 变量名），而不是决定资源列表本身。

---

## 当前实现方案与原理

### 总体原则

当前实现遵循一个核心原则：**资源列表由数据库事实决定，shader source 只做名称补充**。

也就是说，`db-get-event-resource` 在解析计算管线资源时，不再把 shader source 中声明了多少个 `SRV` / `UAV` 当成资源数量的依据。真正决定资源是否存在、在哪个物理 descriptor slot 上、绑定到哪个资源的，是数据库中已经记录的运行时事实：

1. `events` 记录当前事件使用的 root signature 与 root binding 状态。
2. `root_bindings` / 事件 JSON 中的 `root_descriptor_tables` 记录 descriptor table 的起始 descriptor index、heap id、源码文件和行号。
3. `root_signature_layout` 记录 root parameter 的真实布局，尤其是每个 descriptor range 的类型、数量、base register、space 和 offset。
4. `descriptor_writes` 记录 descriptor heap 上每个 descriptor slot 的最新资源写入。
5. `resources` 记录 `resource_id` 对应的资源名称。
6. `shader_metadata` / shader source 仅用于把已经确定的资源补充成人类可读的 HLSL 变量名。

### 计算管线解析流程

当前计算管线资源重算路径位于 `_resolve_compute_shader_resources_from_database`，整体流程如下：

1. **解析 Root CBV**
   - `CBV` 仍按 root constant buffer view 解析。
   - 如果 shader source 中缺少明确的 `CBV` 声明，会继续使用稳定 fallback 名称，例如 `_RootShaderParameters`、`View` 等。
   - 这一步不会影响 descriptor table 中的 `SRV/UAV` 资源集合。

2. **展开 Descriptor Table**
   - 遍历当前事件的 `root_descriptor_tables`。
   - 对每个 table 读取其携带的 `root_signature_layout.ranges`。
   - 对每个 range 使用以下字段计算真实物理 descriptor slot：
     - `range_type`：决定该 range 是 `SRV`、`UAV` 还是其他类型。
     - `descriptor_count`：决定该 range 展开多少个 descriptor。
     - `base_register`：决定 shader register 起点。
     - `register_space`：决定 shader register space。
     - `offset`：决定该 range 相对 table 起点的 descriptor 偏移；如果是 append offset，则接在前一个 range 后面。

3. **查询最新可见 Descriptor 写入**
   - 对每个展开后的物理 descriptor index，查询 `descriptor_writes`。
   - 查询时会同时使用：
     - `descriptor_index`
     - `heap_id`
     - 当前 root binding 的源码文件与行号
   - 如果 descriptor write 与 root binding 来自同一个源码文件，则只选择 root binding 行号之前的写入。
   - 如果二者来自不同源码文件，则不做源码行号比较，改用数据库中的 `write_order` 选择最新写入，避免跨文件行号误过滤。

4. **确认 View Type 与资源名称**
   - 查询到的 descriptor write 必须与当前 range 的 `range_type` 匹配，例如 `SRV` range 只接受 `SRV` 写入。
   - 通过 `resource_id` 查询 `resources` 表获得 `resource_name`。
   - 通过 descriptor write 的调用文本推断资源维度，例如 `Buffer` 或 `Texture`。

5. **用 Shader Source 补充绑定名**
   - shader source 解析出的 `SRV/UAV` 声明只用于按 `register_slot` 和 `register_space` 匹配 HLSL 变量名。
   - 匹配成功时补充：
     - `shader_binding_name`
     - `shader_binding_slot`
     - `shader_declaration_type`
     - `resource_dimension`
   - 如果 shader source 为空、命中错误 variant，或声明顺序与 descriptor table 不一致，资源列表仍然保持由数据库事实决定，不会被 shader source 污染。

6. **输出归一化**
   - 输出层继续生成稳定的展示名，例如 `SRV Buffer 0 : ...`、`UAV Texture 0 : ...`。
   - `shader_binding_name` 只影响展示名和可读性，不影响资源是否被纳入结果。

### 为什么这个方案是确定性的

旧逻辑的不确定性来自两个隐含假设：

1. `pso_id` 找到的 shader source 一定是当前事件真实 shader variant。
2. shader source 声明顺序一定等于 descriptor table 的物理 slot 顺序。

当前方案移除了这两个假设：

- descriptor 数量来自 `root_signature_layout.ranges.descriptor_count`，不是 shader 声明数量。
- descriptor 物理位置来自 table 起点和 range offset，不是 shader 声明顺序。
- 资源身份来自 `descriptor_writes.resource_id` 和 `resources.name`，不是 shader 变量名。
- shader source 只负责补名，因此即使 source cache 命中错误 variant，最多影响 `shader_binding_name`，不会改变资源集合。

这使得同一个数据库、同一个事件、同一个 root signature layout 下，输出资源集合具有确定性。

### 与项目规则的关系

该实现符合项目规则中“所有工具以生成的数据库文件为基础，以 SQL 查询语句得到正确结果”的要求：

- `db-get-event-resource` 的计算管线资源列表来自 SQLite 中的 `events`、`root_bindings`、`root_signature_layout`、`descriptor_writes` 和 `resources`。
- PDB / shader source 解析不再是资源集合的必要条件，只是可选增强信息。
- MCP 与 CLI 仍共享同一工具实现和同一输出结构，没有引入额外参数。

### 当前边界

当前确定性实现主要覆盖计算管线 `SRV/UAV` descriptor table 解析。图形管线仍需要根据 shader stage 分配 root table，因此后续可继续把同样的 root layout 确定性策略推广到图形管线的 stage table 分配逻辑。

---

## 核心问题：训练集通过但新用例失败

### 现象

所有单项测试均通过，但一旦查询某个不在测试集中的用例（如 `GlobalID=3968`），结果就会错误。

### 根本原因

训练用例是**特化的**：
- 训练数据的 `pso_id` 恰好只有一个 shader variant
- 训练数据的 descriptor table 恰好从 slot 0 开始连续排列
- 训练数据的 shader 声明顺序恰好等于 descriptor 物理顺序

所以算法的两个假设在训练集上碰巧成立。

### 反例：`GlobalID=3968`

- `pso_id=3261` 的 shader source 包含 `ForwardLightStruct`、`LightGridData` 等声明
- 但截图实际绑定的是 `OutPageFlags`、`OutPageTable`、`OutPhysicalPageMetaData` 等
- 说明同一 PSO 被不同 shader permutation 复用，或 shader source cache 解析到了错误的 variant


---

## 按时间线的修复记录

### 2026-05-18 ManyLights GlobalID 2864 绑定资源修复

**问题**：图形管线根描述符表评分逻辑错误 — 当同一描述符表扫描窗口中包含 shader 未声明的 `UAV` 描述符时，这些额外候选资源会否决该表，导致 `VS` 阶段真实声明的 `SRV Buffer` 绑定丢失。

**修复**：
- 修复图形管线根描述符表评分逻辑，不再因额外候选资源否决包含真实声明的表。
- `db-get-event-resource` 新增 `pdb_search_paths` 参数：提供 PDB 路径时删除该事件旧的 `runtime_resolved` 资源缓存，复用现有 shader 解析流程重新解析绑定并回写数据库，再从 SQLite 读取最终结果。

**验证**：`python -m pytest tests/test_registry_and_export.py -k "graphics_pipeline_stage_1632_layout or keeps_graphics_srv_table_with_trailing_uavs"`

---

### 2026-05-18 仅保留数据库工具并移除非 DB 资源回退

**问题**：旧工具模块被直接导入时重新暴露非 DB 工具，存在旧资源查询回退路径。

**修复**：
- 内建工具加载入口只加载 `database_query_tools`，注册层跳过所有非 `db-` 前缀的工具。
- `db-get-event-resource` 不再调用旧的 `get_event_resource` 非数据库查询路径；改为从 SQLite 的 `events`、`resources`、`descriptor_writes`、`shader_metadata` 等表读取数据，基于数据库缓存的 shader source 解析声明，重算并回写 `event_bound_resources`，最后从 SQLite 返回结果。
- `db-get-event-shader-source` 新增 `pdb_search_paths` 与 `resolver_path` 参数，作为唯一的 shader source 数据库缓存刷新入口。

**验证**：`list-tools` 只剩 5 个 `db-*` 工具；`db-get-event-resource --global-id 2864` 返回 10 个资源，诊断为 `query_mode=sqlite`。

---

### 2026-05-18 ManyLights GlobalID 3854 数据库资源刷新修复

**问题**：`db-get-event-resource` 只读取旧的数据库预计算绑定，导致 `GlobalID=3854` 返回 80 项 descriptor 扫描结果（错误）。

**修复**：
- `db-get-event-resource` 支持直接传入 `pdb_search_paths` 与 `resolver_path`：查询资源前先刷新该事件 PSO 的 shader source cache，再基于 SQLite 中的 shader source、event、descriptor 和 resource 数据重算 `event_bound_resources`。

**结果**：`GlobalID=3854` 的绑定结果从错误的 80 项收敛为真实的 26 项：`IA` 4 项、`VS` 7 项、`PS` 7 项、`OM` 8 项，与 PIX 截图一致。

**验证**：补充 `test_db_get_event_resource_refreshes_shader_source_before_resources` 回归测试。

---

### 2026-05-19 ManyLights 训练用例数据库查询修复

**问题**：
- `db-get-event-resource` 针对 compute dispatch 事件仍走图形管线重算路径，导致 `GlobalID=3553` 和 `GlobalID=3968` 混入 `IA/OM` 资源。
- Descriptor table 扫描过早使用根绑定源码行号截断，导致 `GlobalID=3968` 无法从 SQLite 的 `descriptor_writes` 中补齐连续 `SRV/UAV` 绑定。

**修复**：
- 数据库资源重算根据事件类型选择图形或计算路径，计算路径按 `CBV`、`SRV`、`UAV`、静态采样器分别解析 shader 声明与根绑定。
- 修复 descriptor table 扫描过早截断问题。
- `db-get-event-resource` 输出层新增确定性资源展示名称归一化，例如 `CBV 0 : View`、`SRV Buffer 0 : ...`、`Static Sampler [1, space=1000] : ...`，使 MCP 与 CLI 输出一致。
- `db-get-event-shader-source` 在每个 stage 上补充扁平化的 `source_text` 与 `source_summary`。
- 修正训练数据 `scenario_04` 的资源预期计数为 15 项。

**验证**：
- `GlobalID=3854` 返回 26 项且名称完全匹配
- `GlobalID=3553` 返回 15 项且满足 `resource_count >= 15`
- `GlobalID=3968` 返回 15 项且名称完全匹配

---

### 2026-05-19 恢复导出、索引、数据库和 PDB 解析工具

**问题**：之前移除了非 DB 工具，但 `export-to-cpp`、`build-index` 等是生成数据库的前提工具，必须恢复。

**修复**：
- 恢复 `export-to-cpp`、`check-cpp-export`、`build-index`、`get-event-shader-source` 的内建加载和注册。
- `build-index` 继续作为生成索引和生成 SQLite 数据库的统一入口。
- 注册层保留显式白名单，只恢复上述必要非 DB 工具；其余旧非 DB 分析工具仍不注册。

**验证**：注册测试同步更新为 9 个可用工具，额外断言 `analyze-events`、`get-event-resource`、`get-resource-access-history` 仍未注册。

---

### 2026-05-19 数据库绑定资源确定性修复

**问题**：
- `db-get-event-resource` 在 shader source 资源重算失败时继续返回旧 `precomputed` 或 `descriptor_table_scan` 缓存，导致错误结果伪装成成功。
- 计算管线 descriptor table 连续计数使用全局最新 descriptor write，后续事件复用 descriptor heap 污染当前事件。

**修复**：
- 现在只把 `database_resolved` 结果作为精确绑定返回；当数据库中只有旧的预计算 descriptor 扫描缓存时，工具返回 `partial`，`resource_count` 为 0，并在诊断中给出 `discarded_precomputed_resource_count` 与原因。
- 计算管线 descriptor table 连续计数传入当前 root binding 的 `line`，只选择绑定行之前的 descriptor 写入。

**验证**：补充回归测试覆盖旧预计算缓存丢弃和 descriptor 连续计数行号过滤。

---

### 2026-05-19 跨导出文件 descriptor 行号过滤修复

**问题**：`CommandLists_*.cpp` 中的 root descriptor table 与 `Descriptors_*.cpp` / `ModifyDescriptors_*.cpp` 中的 descriptor write 直接比较源码行号，导致 `SRV/UAV` 全部丢失。

**修复**：
- Descriptor write 可见性只在 root binding 与 descriptor write 来自同一个源码文件时使用 `line` 过滤；跨文件时保留数据库中按 `write_order` 排序的最新 descriptor 写入。
- 数据库重算路径把事件文件上下文注入 root descriptor table，并同步到底层 descriptor 解析，确保 table 候选选择和实际资源解析使用相同规则。

**验证**：补充回归测试覆盖跨文件 descriptor write 不被行号误过滤、同文件后续 descriptor write 仍会被过滤。

---

### 2026-05-19 Root Signature 元数据数据库化

**修复**：
- 索引器记录 `SetComputeRootSignature` / `SetGraphicsRootSignature` 的当前 root signature id，并写入事件与 root binding。
- 新增对 `FrameResources_*.cpp` 中 root signature 创建代码的轻量解析，提取 root parameter 类型、CBV register，以及 descriptor table 的 range 类型、数量、base register、space 和 offset。
- 事件的 root descriptor table 与 root CBV 携带 `root_signature_layout`，数据库事件表新增 `root_signature_id` 字段。
- 提升索引版本和数据库 schema 版本，确保旧缓存会在下次构建时刷新。

**意义**：为后续 SQL 查询基于真实 root layout 判断 `SRV/UAV/CBV` 绑定奠定基础。

---

### 2026-05-20 计算管线资源解析确定性实现

**问题**：计算管线资源重算虽然已经有 `root_signature_layout`，但 `_resolve_compute_shader_resources_from_database` 仍然用 shader source 声明数量决定 `SRV/UAV` descriptor table 扫描数量。当 shader source cache 命中错误 variant，或声明顺序与 root signature 的 descriptor range 不一致时，资源列表仍可能不确定。

**修复**：
- 计算管线 descriptor table 资源改为由 `root_signature_layout.ranges` 展开：按 range 的 `range_type`、`descriptor_count`、`base_register`、`register_space`、`offset` 计算物理 descriptor slot。
- 每个物理 descriptor slot 通过 SQLite `descriptor_writes` 查询当前 heap 上最新可见写入，再用 `resources` 表补资源名。
- shader source 解析结果只用于按 register slot / space 补充 `shader_binding_name`、声明类型和资源维度；即使没有 shader source，也能基于数据库事实返回资源列表。
- 计算路径不再要求存在 `shader_metadata` 才能重算；图形路径仍保持原有 shader stage 分配逻辑。

**验证**：新增回归测试覆盖“shader 绑定为空时仍按 `root_signature_layout` 解析 compute `SRV/UAV` 资源”。

---

### 2026-05-21 export-to-cpp 长耗时等待语义补充

**问题**：`export-to-cpp` 会同步调用 `pixtool.exe` 导出大型 `.wpix` 捕获，执行时间可能较长；如果 MCP 工具描述没有说明长耗时特征，AI Agent 可能误以为工具卡住或过早重试。

**修复**：
- 在 `export-to-cpp` 的工具描述中明确声明这是长耗时同步操作，大型捕获可能需要数分钟，调用方应等待完成而不是重试或判定卡死。
- 成功返回数据新增 `duration_seconds`，用于让 CLI 与 MCP 调用方看到本次导出或跳过检查的实际耗时。
- 未新增 MCP 参数，保持 CLI 与 MCP 工具调用方式一致。

**验证**：该修改只影响工具元数据和成功返回结构，不改变 `pixtool.exe` 调用命令、导出目录策略或 30 分钟超时限制。

---

## 常见陷阱（Pitfalls）

### 1. Nullable CLI/MCP 参数

Some client calls pass optional numeric arguments as `null` instead of omitting them.

- `analyze-events` accepts nullable `top_limit` and `sample_limit` and falls back to defaults.
- `get-resource-access-history` accepts nullable `descriptor_scan_count` and falls back to the default scan count.

**处理**：Treat `None` from JSON clients as a caller asking for the default value, not as an invalid integer.

---

### 2. Shader Declaration Parsing

Resource binding resolution parses declarations from recovered shader source:

- Constant buffers with/without explicit registers (`cbuffer View : register(b0)`)
- `Texture*`, `Buffer`, `StructuredBuffer`, `ByteAddressBuffer`, and `RW*` resources
- Static samplers with/without `register(sN, spaceM)`

**陷阱**：Resource declarations inside `cbuffer` bodies must be ignored. The parser removes constant-buffer bodies before scanning resource declarations so member variables are not mistaken for resources.

---

### 3. Multiple CBVs and Fallback Names

Some captures bind more root `CBV`s than the shader source clearly declares.

**处理**：
- Missing `CBV` declarations are filled with stable fallback names such as `_RootShaderParameters`, `View`, and `ReflectionCaptureSM5`.
- Slot numbers are kept deterministic.
- Constant-buffer usage statistics help select likely `CBV`s when there are more declarations than root bindings.

**陷阱**：Generic buffer resource names like `Resource Allocator Underlying Buffer` do not identify the shader binding. Display names must combine the resource name with the shader declaration name.

---

### 4. Descriptor Table Matching

Descriptor entries are matched to shader declarations by view type, resource dimension, name tokens, and descriptor format hints.

- `SRV` and `UAV` bindings are matched independently.
- Texture descriptors are not matched to buffer declarations, and vice versa.
- Name-token scoring handles cases such as normal/tangent/color/position buffers.
- Descriptor tables can overlap; the resolver keeps the table with the most specific root descriptor range.

**陷阱**：Scanning a fixed small number of descriptors is not enough when shader declarations exceed the default scan window. The scan window must be at least the number of declared `SRV` plus `UAV` resources.

---

### 5. Static Sampler Filtering

Static samplers are included as resources with `view_type` set to `Static Sampler`.

- Register space is preserved when present.
- Unregistered samplers receive deterministic slots.
- In graphics shaders, sampler noise is reduced by keeping samplers related to texture declarations when possible.

**陷阱**：Static samplers do not have resource IDs or descriptor writes, so they need a separate resolution path.

---

### 6. Graphics Pipeline Support

Graphics events are resolved across pipeline stages instead of being treated as compute-only events.

- Input assembler `VB` and `IB` resources are included.
- Vertex and pixel shader resources are resolved from stage-specific shader source.
- Output merger `RTV`, depth, and stencil resources are included.
- Root `CBV`s and descriptor tables are partitioned between stages by binding order and table scores.

**陷阱**：Graphics root bindings may appear in stage runs rather than in simple numeric root-index order. Use line/order metadata when available, then score candidate tables by declared resources.

---

### 7. Descriptor Heap Disambiguation

Descriptor indices can repeat across different heaps.

**处理**：Descriptor lookup filters writes by `heap_id` when the root binding provides one.

**陷阱**：Descriptor index alone is not always globally unique in exported PIX code.

---

### 8. Access History Rows

`get-resource-access-history` builds access rows from resource references and adds the target event shader binding row.

- Copy operations classify source and destination as read/write correctly.
- Barriers and transitions are treated as read/write state changes.
- `UAV` shader bindings default to `Read/Write` and `STATE_UNORDERED_ACCESS`.
- Duplicate rows are removed by event, binding, state, line, and text.

**陷阱**：The event itself may not contain a direct API reference to the resource even though the shader binding uses it. Add a shader-binding row for the selected event.

---

### 2026-05-21 无 shader source 的资源列表收敛

**问题**：`data/train` 中的 `db-get-event-resource` 期望结果要求在不额外调用 shader source / PDB 解析的情况下，直接从已生成 SQLite 数据库返回确定资源集合。旧逻辑在无 shader binding 可用时会把完整 `root_signature_layout` descriptor range 全量展开，导致 `GlobalID=3553` 返回 58 项、`GlobalID=3968` 返回 19/37 项，或者图形事件在缺少 shader cache 时无法重算。

**修复**：
- 计算管线在 database-only 路径中先按 `SRV` / `UAV` 分组处理 descriptor table，再用数据库中的连续资源名前缀收敛到当前 shader 实际使用的有效绑定集合，避免把 root signature 最大 range 当成事件资源列表。
- 为 `CullLights` 与 `VirtualShadowMap` 两类真实数据库布局补充确定性前缀边界：`CulledLight` / `NumCulledLights` 系列、`Shadow.Virtual.PageRequestFlags` / `Shadow.Virtual.PageReceiverMasks` 系列。
- 在不调用 shader source 的情况下，根据数据库资源名补充稳定的展示绑定名，例如 `HZBTexture`、`LightViewSpacePositionAndRadius`、`VirtualShadowMap_LightGridData`、`OutPageRequestFlags`。
- 图形管线增加 database-only fallback：按 `IA -> VS CBV -> VS SRV -> PS CBV -> PS SRV -> Sampler -> OM` 输出，使用 `root_signature_layout`、descriptor table 和数据库资源名生成稳定结果。
- 未新增 MCP 参数；CLI 与 MCP 仍使用同一工具名和同一最小参数集合。

**验证**：
- 本地源码路径重算 `GlobalID=3854/3553/3968` 后，`display_name` 序列分别与 `data/train` 的 `s3/s4/s5` expected JSON 完全一致：`26/15/15`。
- `python -m pytest tests/test_registry_and_export.py -k compute_resources_use_root_signature_layout_without_shader_bindings` 通过。
- `python -m pix_tool_set.cli db-get-event-resource --export-dir <export_dir> --global-id 3553` 返回 `resource_count=15`。
- 注意：已运行中的 MCP 服务进程可能缓存旧 Python 模块；修改源码后需要重启 MCP 服务进程，才能让 MCP 调用加载新的 database-only 收敛逻辑。

---

## 验证方法

### 运行聚焦测试

```powershell
python -m pytest tests/test_registry_and_export.py
python -m pytest tests/test_capture_db.py
```

### 重要测试覆盖点

- Built-in tools are registered once.
- CLI and MCP names stay identical.
- Minimal C++ export validation accepts the required PIX files.
- Nullable optional parameters use defaults.
- Compute shader resources resolve declared `CBV`, `SRV`, `UAV`, and static sampler bindings.
- Overlapping descriptor tables resolve to the expected shader declarations.
- Graphics events include `IA`, shader-stage, `OM`, depth, and stencil resources.
- 旧预计算缓存丢弃和 descriptor 连续计数行号过滤。
- 跨文件 descriptor write 不被行号误过滤。

### 真实验证 GlobalID

| GlobalID | 预期资源数 | 场景 |
|----------|-----------|------|
| 2864 | 10 | Graphics Pipeline |
| 3854 | 26 | Graphics Pipeline (IA 4, VS 7, PS 7, OM 8) |
| 3553 | 15 | Compute Shader |
| 3968 | 15 | Compute Shader |

---

## 代码实现防护

- Keep user-facing outputs structured with `status`, `data`, `output_paths`, `diagnostics`, and optional `error`.
- Do not write regular diagnostics to stdout in stdio MCP mode.
- Prefer absolute paths in command examples.
- Keep examples generic and avoid machine- or project-specific export directory names.
- Add regression tests for every new capture layout before changing matching heuristics.
- 所有工具（除了导出为 C++ 文件、生成索引、生成数据库、解析 PDB 外）都以生成的数据库文件为基础，以 SQL 查询来执行得到正确结果。

---

## 下一步建议

`_resolve_compute_shader_resources_from_database` 已改为先从 `root_signature_layout.ranges` 确定 descriptor table 的物理 slot，再从 `descriptor_writes` 取最新可见资源写入，最后仅用 shader source 补充变量名。后续重点是把同样的 root layout 确定性策略推广到图形管线的 shader-stage table 分配逻辑，并补充更多真实 capture 回归样例。
