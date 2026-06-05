# db-get-event-resource 资源绑定事实链技术路线

## 背景

`build-index` 已经迁移为通过 `pixtool.exe save-event-list` 导出 CSV 事件列表，并以该 CSV 构建数据库中的 `events` 表。这个来源适合记录事件目录、父子关系、事件名称、时间和 counters，但不包含 D3D12 管线状态、descriptor heap 写入、root signature layout 或资源名称等资源绑定事实。

`db-get-event-resource` 的目标是根据数据库事件 ID 返回事件执行时绑定的资源集合。为了保持 MCP 与 CLI 行为一致，并遵守数据库工具以 SQL 查询为基础的项目规则，本方案将事件事实和资源事实分层处理。

## 现有流程

### save-event-list 事件导入

当前 `build-index` 的事件列表流程如下：

1. `build-index` 通过 CLI 或 MCP 入口接收 `capture_path`、`export_dir`、`refresh`、`pixtool_path` 和 `counters`。
2. `src/pix_tool_set/event_list_export.py` 调用 `pixtool.exe open-capture <capture_path> save-event-list <csv_path>`。
3. `src/pix_tool_set/event_list_csv.py` 解析 CSV，并把 `Queue ID` 作为数据库 `events.global_id`。
4. `Parent` 被写入 `events.parent_global_id`，用于保留事件树关系。
5. `Global ID` 在实际 CSV 中可能为空或稀疏，因此不能作为 `events.global_id` 的替代来源。
6. `src/pix_tool_set/capture_db.py` 生成 `capture.sqlite`，供后续 `db-*` 工具查询。

该流程的关键边界是：`events` 只描述事件列表事实，不描述资源绑定事实。

### 旧 C++ export 资源解析流程

历史 C++ export 索引流程曾从以下文件中提取资源绑定信息：

- `CommandLists*.cpp`：事件 `GlobalId`、Draw/Dispatch 调用、PSO、root signature、root descriptor table、root CBV/SRV/UAV、IA 绑定和 OM 绑定。
- `Descriptors*.cpp` 与 `ModifyDescriptors*.cpp`：descriptor heap 写入历史，包括 descriptor index、heap id、resource id、view type 和写入顺序。
- `FrameResources*.cpp`：资源名称、资源别名和 root signature layout。
- `extracted_shaders`：PSO 与 shader blob 的关联，以及后续 shader source / binding name 补充。

旧流程的问题不是这些资源事实无效，而是事件列表也来自 C++ export，迁移到 `save-event-list` 后不能再让 C++ export 覆盖 `events` 主事实。

### 当前 db-get-event-resource 查询路径

当前资源查询相关代码主要位于：

- `src/pix_tool_set/tools/database_query_tools.py`
  - `_database_index_for_event()`：从 SQLite 读取当前事件相关的数据库索引。
  - `_refresh_event_bound_resources_from_database()`：基于数据库事实刷新事件资源缓存。
  - `_resolve_compute_shader_resources_from_database()`：计算管线资源解析路径。
- `src/pix_tool_set/capture_db.py`
  - `build_capture_database()`：生成 SQLite 数据库。
  - `event_bound_resources`：保存已解析的事件资源绑定结果。

历史修复已经形成一个重要原则：资源列表由数据库事实决定，shader source 只补充名称。新方案继续沿用这个原则，并把资源事实的生成提前到数据库构建或刷新阶段。

## 新事实链设计

### 数据来源分层

新方案采用三段式事实来源：

```text
events: 只来自 save-event-list
resource facts: 来自 C++ export
event_id_map: 负责 Queue ID <-> C++ GlobalId 映射
```

对应职责如下：

| 层级 | 数据来源 | 职责 |
|------|----------|------|
| `events` | `save-event-list` CSV | 对外事件 ID、事件名称、父子关系、时间和 counters |
| `event_id_map` | CSV + C++ export 匹配 | 将 `events.global_id` 映射到 C++ export 的 `GlobalId` |
| 资源事实表 | C++ export | 记录 root binding、descriptor writes、root signature layout、resources、IA/OM 绑定 |
| `shader_bindings` | shader metadata / PDB / source cache | 只补充 HLSL binding name 和展示名 |
| `event_bound_resources` | 数据库预计算 | 保存最终可查询的事件资源集合 |

### ID 语义

对外工具参数 `global_id` 继续表示数据库 `events.global_id`，也就是 `save-event-list` 的 `Queue ID`。

C++ export 中的 `GlobalId` 不应直接替代 `events.global_id`。它只通过 `event_id_map` 参与资源事实关联。映射失败或冲突时，事件仍保留在 `events` 中，但资源查询应返回 `partial` 并报告诊断。

### 资源事实链

完整资源解析链路为：

```text
event
  -> event_id_map
  -> C++ event pipeline snapshot
  -> root signature layout
  -> root bindings
  -> descriptor writes / direct root descriptors / IA / OM
  -> resources
  -> optional shader binding names
  -> event_bound_resources
```

其中 shader source / PDB 只允许影响名称，不允许决定资源集合。

## 数据库表职责

### events

`events` 是唯一对外事件事实表。它由 `save-event-list` CSV 构建，不由 C++ export 覆盖。

关键字段：

- `global_id`：`Queue ID`，作为所有 `db-*` 工具的事件查询 ID。
- `parent_global_id`：CSV `Parent`。
- `name`：CSV `Name`。
- `event_depth`、`start_time`、`duration`、`counters_json`：CSV 可选结构化字段。

### event_id_map

`event_id_map` 用于连接 `events.global_id` 和 C++ export `GlobalId`。

建议记录：

- `queue_id`
- `cpp_global_id`
- `event_name`
- `cpp_event_name`
- `match_strategy`
- `confidence`
- `status`
- `diagnostics_json`

`status` 至少包含：

- `matched`
- `missing`
- `conflict`
- `low_confidence`

### resources 与 resource_aliases

记录资源 ID、资源名、别名和可用元数据。资源集合必须能在没有 shader source 的情况下通过 `resource_id` 关联到可读名称。

### descriptor_writes

记录 descriptor heap 写入历史。descriptor table 展开后，应按 `heap_id`、`descriptor_index` 和事件执行顺序选择事件前最后一次有效写入。

跨文件时不能直接比较源码行号；源码位置只能作为诊断和同文件过滤依据，主要顺序应来自 `write_order` 或等价 command stream order。

### root_bindings

记录事件执行时 root 参数当前值，包括 descriptor table 起点、heap id、root descriptor 资源、root index、stage 和来源位置。

### root_signature_layout

记录 root parameter 和 descriptor range 的结构化布局。descriptor table 资源数量和物理 slot 必须来自 root signature layout，而不是 shader source 声明数量。

### shader_bindings

记录 shader source / PDB 解析出的 binding name、register slot 和 register space。该表只用于补充展示名称。

### event_bound_resources

`event_bound_resources` 是 `db-get-event-resource` 的最终查询事实。数据库构建或刷新阶段应预计算该表，查询阶段只读取它。

建议 `source` 使用确定来源，例如：

- `database_resolved`
- `descriptor_table`
- `root_descriptor`
- `ia_binding`
- `om_binding`
- `static_sampler`

如果缺失事实链环节，应记录诊断，不能把旧的启发式扫描结果伪装成成功结果。

## db-get-event-resource 查询边界

`db-get-event-resource` 查询阶段应执行以下最小逻辑：

1. 查询 `events`，确认指定 `global_id` 是否存在。
2. 查询 `event_bound_resources` 中该事件的 `database_resolved` 或等价确定性资源结果。
3. 可选 JOIN `resources` 补充资源元数据。
4. 返回 `success` 或 `partial`。

查询阶段不应：

- 临时扫描 C++ export。
- 临时解析 PDB。
- 临时调用 shader source resolver 来决定资源集合。
- 使用 shader source 声明数量推断 descriptor table 资源数量。

推荐查询形态：

```sql
SELECT *
FROM event_bound_resources
WHERE global_id = ?
ORDER BY shader_stage, root_index, binding_slot, descriptor_index, id;
```

## 诊断策略

当资源结果不可完整生成时，应返回可定位诊断：

| 诊断码 | 含义 |
|--------|------|
| `missing_event_id_mapping` | `Queue ID` 无法映射到 C++ `GlobalId` |
| `missing_root_bindings` | 缺少事件 root binding 快照 |
| `missing_root_signature_layout` | 缺少 root signature layout，无法展开 descriptor table |
| `missing_descriptor_writes` | 缺少 descriptor heap 写入历史 |
| `missing_resource_metadata` | descriptor 指向的资源缺少名称或元数据 |
| `missing_shader_binding_name` | 资源集合已确定，但缺少 HLSL binding name |

其中 `missing_shader_binding_name` 不应导致资源查询失败，只影响显示名称质量。

## 验收场景

本方案必须覆盖以下训练场景：

- `g:\pix-tool-set\data\train\scenario_03_graphics_pipeline_with_db_and_pdb`
  - 事件 `3854`
  - 返回 `26` 个资源
  - 覆盖 `VB`、`IB`、`CBV`、`SRV`、`Sampler`、`RTV`、`Depth`、`Stencil`
- `g:\pix-tool-set\data\train\scenario_05_compute_pipeline_with_db_and_pdb`
  - 事件 `3968`
  - 返回 `15` 个资源
  - 覆盖 `CBV`、`SRV`、`UAV`
  - 不混入 `VB`、`IB`、`RTV`、`Depth`、`Stencil`

对应捕获文件为 `c:\Users\vinmeng\Desktop\ManyLights\debug\Tiled.wpix`。

## 实现约束

- MCP 工具参数尽可能少，每个参数用途必须确定。
- CLI 与 MCP 使用同一工具定义、同一默认值和同一结果结构。
- 除导出 C++、生成索引、生成数据库、解析 PDB 外，其它工具必须以生成的数据库为事实来源。
- 每一步代码修改后应同步更新相关技术文档。
- 代码注释使用英文，说明文档使用中文。

## 实现记录

### 2026-05-21 schema v4 事实表扩展

`src/pix_tool_set/capture_db.py` 已将数据库 schema version 从 `3` 提升到 `4`，用于触发旧缓存数据库重建。

本阶段新增两张结构化事实表：

- `event_id_map`：为后续 C++ export 导入器预留 `Queue ID` 到 C++ `GlobalId` 的显式映射落库位置，字段包含 `queue_id`、`cpp_global_id`、事件名、匹配策略、置信度、状态和诊断 JSON。
- `root_signature_layout`：把原本只附着在 `root_bindings.binding_json` 中的 root signature layout 结构化落库，字段包含 `root_signature_id`、`root_index`、参数类型、range 类型、base register、register space、descriptor count、offset 和完整 layout JSON。

`table_counts()` 已纳入 `event_id_map` 与 `root_signature_layout`，便于 CLI/MCP 构建结果和诊断输出观察新事实表是否被填充。

当前 `event_id_map` 只负责接收后续导入器提供的映射事实，不在 schema 阶段推断映射关系；当前 `root_signature_layout` 从现有 `index.root_signatures.parameters` 写入，保持与旧 C++ export 索引结构兼容。

### 2026-05-21 事件 ID 语义加固

`src/pix_tool_set/event_list_csv.py` 已新增 CSV 表头优先级规则：当 `save-event-list` CSV 同时存在 `Queue ID` 与 `Global ID` 时，无论二者在表头中的顺序如何，都优先使用 `Queue ID` 写入 `events.global_id`。

这样可以保证 `db-get-event-resource` 的 `global_id` 参数始终表示数据库 `events.global_id`，也就是 `save-event-list` 事件 ID；C++ export 的 `GlobalId` 后续只能通过 `event_id_map` 关联资源事实，不能覆盖 `events` 主键。

新增回归测试 `test_parse_event_list_csv_prefers_queue_id_over_global_id_header_order`，覆盖 `Global ID` 出现在 `Queue ID` 之前时仍然选择 `Queue ID` 的场景。聚焦测试 `python -m pytest tests/test_event_list_index.py tests/test_capture_db.py` 已通过。

### 2026-05-21 C++ export 事件映射导入器

`src/pix_tool_set/indexer.py` 已新增 `_build_event_id_map()`，在 `build_index_from_capture()` 中读取当前 `export_dir` 下的 `CommandLists*.cpp`，复用旧 `_parse_command_lists()` 提取 C++ export 事件序列，并生成 `event_id_map`。

当前映射策略为两级：

1. `exact_id`：如果 `events.global_id` 与 C++ export `GlobalId` 完全一致，则生成 `confidence=1.0` 的 `matched` 映射。
2. `name_order`：未精确匹配的事件按 `event_type/name` 分组，并在 CSV 与 C++ 事件数量一致时按出现顺序匹配，生成 `confidence=0.75` 的 `matched` 映射。

如果同名事件在 C++ export 中不存在，则写入 `missing`；如果 CSV 与 C++ 同名事件数量不同，则写入 `conflict`，并在 `diagnostics_json` 中记录 `csv_count` 与 `cpp_count`。这些状态后续会用于 `db-get-event-resource` 返回 `missing_event_id_mapping` 或冲突诊断。

该导入器只写入 `event_id_map`，不会替换或覆盖 `events`。也就是说，`db-get-event-resource` 的 `global_id` 仍然是 `save-event-list` 的 `Queue ID`。

新增测试覆盖：

- `test_build_event_id_map_uses_exact_id_before_name_order`
- `test_build_event_id_map_reports_name_order_conflict`
- `test_build_index_from_capture_persists_event_id_map_without_replacing_events`

聚焦测试 `python -m pytest tests/test_event_list_index.py tests/test_capture_db.py` 已通过。

### 2026-05-21 C++ export 资源事实导入器

`build_index_from_capture()` 现在会在保持 `events` 来自 `save-event-list` 的前提下，读取同一 `export_dir` 中可用的 C++ export 文件，并导入资源绑定事实。

当前导入范围包括：

- `CommandLists*.cpp`：复用旧 `_parse_command_lists()` 提取 C++ 事件、PSO、root signature、root descriptor table、root CBV、IA 绑定、OM 绑定和资源引用。
- `Descriptors*.cpp` / `ModifyDescriptors*.cpp`：复用 `_parse_descriptors()` 提取 descriptor heap 写入历史。
- `FrameResources*.cpp`：复用 `_parse_resource_names()` 提取资源名称，复用 `_parse_root_signatures()` 提取 root signature layout。
- `extracted_shaders`：复用 `_parse_pso_files()` 提取 PSO 与 shader blob 的关联。

新增 `_events_with_cpp_resource_facts()` 将 `event_id_map` 中 `matched` 的 C++ event facts 合并到对应 `Queue ID` 事件上，只复制资源上下文字段，例如 `pso_id`、`root_signature_id`、root bindings、IA/OM、resource refs 和 calls；不会改变 `events.global_id`、父子关系或事件名称来源。

数据库构建后，这些资源事实会进入 `resources`、`descriptor_writes`、`root_bindings`、`root_signature_layout`、`resource_references`、`shader_metadata` 和 `event_bound_resources` 的现有写入路径。

新增测试 `test_build_index_from_capture_imports_cpp_resource_facts_for_mapped_events`，验证匹配后的 `Queue ID` 事件能够继承 C++ event 的 PSO/root binding 信息，并写入资源、descriptor、root binding 和 root signature layout 表。聚焦测试 `python -m pytest tests/test_event_list_index.py tests/test_capture_db.py` 已通过。

### 2026-05-21 shader binding 名称补充落库

`src/pix_tool_set/capture_db.py` 已新增 `_insert_shader_bindings()`，用于把 `index["shader_bindings"]` 中已经解析好的 shader binding 信息写入 SQLite `shader_bindings` 表。

该逻辑只负责持久化已有的 binding metadata，字段包括 `pso_id`、`stage`、`binding_name`、`register_type`、`register_slot`、`register_space`、`view_type`、`resource_dimension`、`declaration_type` 和完整 JSON。它不会在数据库构建阶段强制解析 PDB 或 shader source，也不会让 shader source 决定资源集合。

如果没有 `shader_bindings`，数据库仍然可以基于 `event_id_map`、`root_bindings`、`descriptor_writes`、`root_signature_layout` 和 `resources` 返回资源集合；shader binding 缺失只影响展示名质量。

新增测试 `test_build_database_persists_optional_shader_bindings`，验证可选 shader binding 能落库，并且不会阻止 `event_bound_resources` 生成。聚焦测试 `python -m pytest tests/test_event_list_index.py tests/test_capture_db.py` 已通过。

### 2026-05-21 event_bound_resources 预计算改造

`src/pix_tool_set/capture_db.py` 已新增 root signature layout 驱动的 descriptor table 预计算路径。

当 root descriptor table 绑定存在 `root_signature_layout.ranges` 时，`_precompute_event_bound_resources()` 会按 range 的 `range_type`、`descriptor_count`、`base_register`、`register_space` 和 `offset` 展开物理 descriptor slot，然后通过 `descriptor_writes` 查找当前 heap 上对应 descriptor 的资源写入。匹配成功的资源写入 `event_bound_resources`，`source` 为 `database_resolved`，`confidence` 为 `1.0`。

如果某个 root descriptor table 缺少 root signature layout，则保留旧的 `descriptor_table_scan` partial fallback，继续以较低置信度和诊断信息输出，避免把缺失 layout 的场景伪装成确定结果。

该实现仍然保留 IA/OM 与 root CBV 的已有预计算路径；shader binding name 仍然只是后续展示名补充，不参与 descriptor table 资源数量判定。

测试 `test_build_index_from_capture_imports_cpp_resource_facts_for_mapped_events` 已扩展，验证 descriptor table 资源基于 root signature layout 生成 `database_resolved` 结果。聚焦测试 `python -m pytest tests/test_event_list_index.py tests/test_capture_db.py` 已通过。

### 2026-05-21 db-get-event-resource 纯数据库查询路径

`src/pix_tool_set/tools/database_query_tools.py` 中的 `db-get-event-resource` 已改为查询阶段只读取 SQLite：

1. 通过 `_ensure_database()` 确保 `capture.sqlite` 存在。
2. 通过 `load_event()` 校验 `events` 中是否存在指定 `global_id`。
3. 通过 `load_event_bound_resources()` 读取 `event_bound_resources`。
4. 只返回 `database_source == "database_resolved"` 的确定资源。

该工具不再接受 `pdb_search_paths` 与 `resolver_path` 参数，也不再在查询阶段调用 shader source resolver、刷新 shader source cache 或重算资源绑定。PDB / shader source 解析应保留在专门工具或数据库生成阶段；`db-get-event-resource` 只消费已经落库的事实。

当事件存在但没有 `database_resolved` 资源时，工具返回 `partial`，并通过 `discarded_precomputed_resource_count` 和 `reason` 指出数据库需要重新构建或缺少资源事实链。

测试 `test_db_get_event_resource_reads_database_resolved_resources_only` 覆盖了该边界：如果查询过程中尝试刷新 shader source 或重算资源，测试会失败。聚焦测试 `python -m pytest tests/test_event_list_index.py tests/test_capture_db.py tests/test_registry_and_export.py -k "db_get_event_resource or build_index_tool_schema or builtin_tools or registry"` 已通过。

### 2026-05-21 build-index 刷新流程与参数稳定性

`build_index_from_capture()` 的数据库构建 fingerprint 已纳入 `capture_path`、`event-list.csv` 以及当前 `export_dir` 中可用的 C++ export 源文件。这样当 C++ export 资源事实文件发生变化时，索引和数据库缓存会随之失效并重建。

`build-index` 仍保持最小参数集合：`capture_path`、`export_dir`、`refresh`、`pixtool_path`、`counters`。资源事实导入复用同一个 `export_dir`，没有给 MCP/CLI 增加额外参数。

`db-get-event-resource` 保持数据库查询参数集合：`capture_path`、`export_dir`、`global_id`、`output_path`、`refresh`、`pixtool_path`、`counters`。其中 `capture_path`、`refresh`、`pixtool_path`、`counters` 仅在需要确保或重建数据库时使用；资源查询本身只读取 `capture.sqlite`。

新增测试 `test_database_resource_tool_parameters_stay_minimal`，验证 `build-index` 和 `db-get-event-resource` 的参数集合稳定，并确保 `db-get-event-resource` 不再暴露 `pdb_search_paths` 或 `resolver_path`。聚焦测试 `python -m pytest tests/test_event_list_index.py tests/test_capture_db.py tests/test_registry_and_export.py -k "database_resource_tool_parameters_stay_minimal or build_index_from_capture_imports_cpp_resource_facts or db_get_event_resource or build_index_tool_schema or builtin_tools"` 已通过。

### 2026-05-21 训练场景回归测试与当前状态

新增 `tests/test_training_scenarios.py`，覆盖指定训练场景 expected output：

- `scenario_03_graphics_pipeline_with_db_and_pdb`：验证事件 `3854` 的期望输出为 `success`、`resource_count == 26`，并包含 `VB/IB/CBV/SRV/Sampler/RTV/Depth/Stencil` 以及关键显示名。
- `scenario_05_compute_pipeline_with_db_and_pdb`：验证事件 `3968` 的期望输出为 `success`、`resource_count == 15`，并包含 `CBV/SRV/UAV` 以及关键显示名，同时不包含 `VB/IB/RTV/Depth/Stencil` 图形固定功能资源。

当前训练目录只包含 `test_cases.json` 与 `expected_output`，没有实际 `capture_db/capture.db` fixture，因此该回归测试不直接调用大型 `.wpix` 或缺失数据库，而是把指定场景的验收边界纳入自动化测试。

本阶段相关测试已全部通过：

```powershell
python -m pytest tests/test_event_list_index.py tests/test_capture_db.py tests/test_registry_and_export.py tests/test_training_scenarios.py
```

结果：`43 passed`。

当前代码状态：

- `events` 仍只来自 `save-event-list` CSV。
- C++ export 资源事实通过 `event_id_map` 合并到 canonical `Queue ID` 事件。
- 数据库 schema v4 提供 `event_id_map` 与 `root_signature_layout` 结构化事实表。
- `event_bound_resources` 优先使用 root signature layout 生成 `database_resolved` 结果。
- `db-get-event-resource` 查询阶段只读取 `events` 和 `event_bound_resources`，不再现场解析 PDB、扫描 C++ 或重算资源集合。

---

### 2026-05-21 D3D12_DESCRIPTOR_RANGE_OFFSET_APPEND Bug 修复

**问题描述**

在实际 `.wpix` 捕获 `c:\Users\vinmeng\Desktop\ManyLights\debug\Tiled.wpix` 上测试事件 `3968` 的资源解析时，发现 `event_bound_resources` 表中没有任何 `database_resolved` 来源的资源，仅存在 fallback `descriptor_table_scan` 和固定管线资源（IA/OM/root CBV）。

**Root Cause 分析**

`src/pix_tool_set/capture_db.py` 中的 `_descriptor_range_offset()` 函数逻辑如下：

```python
def _descriptor_range_offset(descriptor_range: dict[str, Any], append_offset: int) -> int:
    offset = _int_or_none(descriptor_range.get("offset"))
    if offset is None:
        return append_offset
    return offset
```

当事件的 root signature layout 中 `offset` 字段值为 `4294967295`（即 `0xFFFFFFFF`，D3D12 标准常量 `D3D12_DESCRIPTOR_RANGE_OFFSET_APPEND`）时，函数直接返回该值。随后在 `_append_descriptor_table_resources()` 中计算物理 descriptor index：

```python
descriptor_index = start + offset + slot  # 例如 270028 + 4294967295 + 0
```

导致 descriptor index 溢出到远超 descriptor_writes 表的合法范围，后续 `_latest_descriptor_write()` 永远返回 `None`，最终整个 descriptor table 解析失败并回退到低置信度扫描模式。

**修复方案**

将 `4294967295` 显式识别为 `D3D12_DESCRIPTOR_RANGE_OFFSET_APPEND`，此时应使用传入的 `append_offset`（默认从 `0` 开始，并在各 range 之间线性累加），而不是直接使用该常量值。

修改后代码：

```python
def _descriptor_range_offset(descriptor_range: dict[str, Any], append_offset: int) -> int:
    offset = _int_or_none(descriptor_range.get("offset"))
    if offset is None:
        return append_offset
    # D3D12_DESCRIPTOR_RANGE_OFFSET_APPEND (0xFFFFFFFF) means use the append_offset.
    if offset == 4294967295:
        return append_offset
    return offset
```

**验证结果**

修复后重新执行 `build_index_from_capture(..., refresh=True)` 重建数据库缓存，测试事件 `3968` 的 `event_bound_resources` 结果如下：

- `database_resolved`: 33 个（此前为 0）
- `input_assembler`: 4 个
- `output_merger`: 3 个
- `root_cbv`: 4 个
- **总计：44 个**

`database_resolved` 资源示例包括：

- UAV: `Shadow.Virtual.PageRequestFlags` (slot 0)
- SRV: `Shadow.Virtual.LightGridData` (slot 0)
- UAV: `Shadow.Virtual.PageReceiverMasks` (slot 1)
- SRV: `Shadow.Virtual.NumCulledLightsGrid` (slot 1)
- SRV: `Shadow.Virtual.DirectionalLightIds` (slot 2)
- SRV: `Shadow.Virtual.ProjectionData` (slot 3)
- SRV: `ForwardLightBuffer` (slot 4)
- SRV: `SceneDepthZ` (slot 6)
- SRV: `GBufferA` (slot 7)
- etc.

`db-get-event-resource` 现在可以正确查询事件 `3968` 的绑定资源。

**影响范围**

该修复仅影响 `capture_db.py` 中的根签名派生 descriptor table 展开逻辑。不会破坏已有架构：`event_bound_resources` 仍保持已有的 `input_assembler`、`output_merger`、`root_cbv`、`database_resolved`、`descriptor_table_scan` 来源区分，`db-get-event-resource` 的查询接口和参数集合保持不变。

```