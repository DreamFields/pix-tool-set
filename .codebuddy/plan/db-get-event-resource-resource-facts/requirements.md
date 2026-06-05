# 需求文档

## 引言

本功能旨在规划并实现 `db-get-event-resource` 的数据库事实链，使该工具能够在新的 `build-index` 架构下继续稳定返回事件绑定资源。

当前架构中，事件列表事实已经迁移为由 `pixtool.exe save-event-list` 导出的 CSV 写入数据库；但 `save-event-list` 只能提供事件目录、父子关系和事件名称，无法提供 D3D12 资源绑定事实。因此，本功能采用分层事实来源：`events` 只来自 `save-event-list`，资源绑定事实来自 PIX C++ export，`event_id_map` 负责建立 `Queue ID` 与 C++ `GlobalId` 的映射。最终 `db-get-event-resource` 应基于生成的 SQLite 数据库，通过 SQL 查询返回结果，而不是在查询阶段临时扫描 C++ 文件、解析 PDB 或重新推导资源绑定。

本功能的验证数据包括：

- 图形管线场景：`g:\pix-tool-set\data\train\scenario_03_graphics_pipeline_with_db_and_pdb`
- 计算管线场景：`g:\pix-tool-set\data\train\scenario_05_compute_pipeline_with_db_and_pdb`
- 对应 PIX 捕获文件：`c:\Users\vinmeng\Desktop\ManyLights\debug\Tiled.wpix`

## 需求

### 需求 1：保持 `save-event-list` 作为唯一事件事实来源

**用户故事：** 作为一名 PIX 捕获分析工具使用者，我希望 `events` 表只来自 `save-event-list` CSV，以便数据库中的事件目录、父子关系和查询入口具有稳定且单一的来源。

#### 验收标准

1. WHEN 系统构建或刷新数据库 THEN 系统 SHALL 继续通过 `pixtool.exe save-event-list` 生成 CSV 事件列表并写入 `events` 表。
2. WHEN CSV 中存在 `Queue ID` 和 `Parent` 字段 THEN 系统 SHALL 使用 `Queue ID` 作为数据库对外查询使用的事件 ID，并使用 `Parent` 构建事件父子关系。
3. IF CSV 中的 `Global ID` 字段为空、稀疏或不能覆盖所有事件 THEN 系统 SHALL NOT 使用该字段替代 `events.global_id`。
4. IF 后续资源绑定事实来自 C++ export THEN 系统 SHALL NOT 使用 C++ export 重新写入或覆盖 `events` 主事件列表。
5. WHEN `db-get-event-resource` 接收 `global_id` 参数 THEN 系统 SHALL 将该参数解释为数据库 `events.global_id`，即当前 `save-event-list` 事件 ID。

### 需求 2：引入 `event_id_map` 映射 `Queue ID` 与 C++ `GlobalId`

**用户故事：** 作为一名工具维护者，我希望通过显式事件 ID 映射表连接 `save-event-list` 事件和 C++ export 事件，以便资源绑定事实可以准确关联到数据库事件。

#### 验收标准

1. WHEN 系统导入 C++ export 资源事实 THEN 系统 SHALL 建立或更新 `event_id_map`，记录 `events.global_id` 与 C++ export `GlobalId` 的对应关系。
2. WHEN 一个 `Queue ID` 能唯一匹配一个 C++ `GlobalId` THEN 系统 SHALL 将资源绑定事实归属到对应的 `events.global_id`。
3. IF 一个 `Queue ID` 无法匹配任何 C++ `GlobalId` THEN 系统 SHALL 保留事件本身，并在资源查询诊断中报告 `missing_event_id_mapping`。
4. IF 一个 `Queue ID` 匹配多个 C++ `GlobalId` 或一个 C++ `GlobalId` 匹配多个 `Queue ID` THEN 系统 SHALL 将该映射标记为冲突，并不得静默选择任意一个映射。
5. WHEN 映射过程使用事件名称、顺序、层级、源文件行号或其它启发式信号 THEN 系统 SHALL 在映射诊断信息中记录匹配依据和置信度。
6. IF 映射置信度低于实现定义的安全阈值 THEN 系统 SHALL NOT 生成 `database_resolved` 资源结果，并 SHALL 返回可诊断的 `partial` 状态。

### 需求 3：从 C++ export 导入资源绑定事实

**用户故事：** 作为一名资源分析使用者，我希望系统从 C++ export 导入 D3D12 资源绑定事实，以便 `db-get-event-resource` 能返回 Draw 和 Dispatch 事件实际绑定的资源。

#### 验收标准

1. WHEN C++ export 可用 THEN 系统 SHALL 从 `CommandLists*.cpp` 导入事件级管线状态，包括 PSO、root signature、root descriptor table、root descriptor、IA 绑定和 OM 绑定。
2. WHEN C++ export 可用 THEN 系统 SHALL 从 `Descriptors*.cpp` 和 `ModifyDescriptors*.cpp` 导入 descriptor heap 写入历史。
3. WHEN C++ export 可用 THEN 系统 SHALL 从 `FrameResources*.cpp` 或等价导出文件导入资源名称、资源别名和 root signature layout。
4. IF 某类 C++ export 文件缺失 THEN 系统 SHALL 导入其它可用事实，并在数据库元信息或工具诊断中明确缺失的事实链环节。
5. IF 资源绑定事实来自 C++ export THEN 系统 SHALL 将其与 `events` 通过 `event_id_map` 关联，而不是建立第二套对外事件 ID。
6. WHEN 系统写入资源绑定事实 THEN 系统 SHALL 保持 CLI 与 MCP 的参数含义一致，并避免为 `db-get-event-resource` 增加运行时解析所需的新参数。

### 需求 4：结构化存储资源绑定事实链

**用户故事：** 作为一名数据库工具维护者，我希望资源绑定事实被结构化写入 SQLite，以便所有数据库查询工具都能通过 SQL 获得确定结果。

#### 验收标准

1. WHEN 系统导入资源名称和元数据 THEN 系统 SHALL 写入 `resources` 和 `resource_aliases`，并至少保留资源 ID、资源名和可用的维度或格式信息。
2. WHEN 系统导入 descriptor 写入历史 THEN 系统 SHALL 写入 `descriptor_writes`，并保留 descriptor index、heap id、resource id、view type、写入顺序和诊断来源。
3. WHEN 系统导入事件 root 参数绑定 THEN 系统 SHALL 写入 `root_bindings`，并保留事件 ID、root index、binding type、descriptor index、heap id、resource id 和绑定来源。
4. WHEN 系统导入 root signature layout THEN 系统 SHALL 以可 SQL 查询的形式保存 root parameter 与 descriptor range 信息，包括 range type、base register、register space、descriptor count 和 offset。
5. WHEN 系统导入 shader binding 信息 THEN 系统 SHALL 写入 `shader_bindings` 或等价结构，并将其仅用于补充资源显示名称和 HLSL binding name。
6. IF shader source 或 PDB 信息不可用 THEN 系统 SHALL 仍可基于 root binding、descriptor write 和 resource facts 返回资源集合，只允许缺少 shader binding name。

### 需求 5：预计算或刷新 `event_bound_resources` 作为最终查询事实

**用户故事：** 作为一名工具调用者，我希望 `db-get-event-resource` 查询的是已经解析好的事件资源绑定结果，以便工具输出稳定、快速且可诊断。

#### 验收标准

1. WHEN 数据库具备事件映射、root binding、descriptor writes、root signature layout 和 resources THEN 系统 SHALL 生成 `event_bound_resources` 作为事件资源绑定的物化结果。
2. WHEN 解析 descriptor table THEN 系统 SHALL 使用 root descriptor table 起点和 root signature layout 展开物理 descriptor slot。
3. WHEN 查找 descriptor slot 对应资源 THEN 系统 SHALL 选择事件执行前对同一 heap id 与 descriptor index 的最后一次有效写入。
4. WHEN descriptor range 类型为 SRV、UAV、CBV 或 SAMPLER THEN 系统 SHALL 只接受匹配 view type 的 descriptor 写入。
5. WHEN 图形管线事件包含 IA 或 OM 绑定 THEN 系统 SHALL 将 VB、IB、RTV、Depth 和 Stencil 资源纳入 `event_bound_resources`。
6. WHEN 计算管线事件包含 CBV、SRV 或 UAV 绑定 THEN 系统 SHALL 将 CS 阶段资源纳入 `event_bound_resources`，并避免混入 IA 或 OM 图形管线资源。
7. IF 某个事件无法生成完整资源绑定结果 THEN 系统 SHALL 在 `event_bound_resources` 或诊断信息中记录缺失环节，而不得伪装为成功。

### 需求 6：让 `db-get-event-resource` 保持数据库 SQL 查询边界

**用户故事：** 作为一名 MCP 和 CLI 工具使用者，我希望 `db-get-event-resource` 只依赖生成好的数据库执行查询，以便工具参数少、行为确定且 CLI/MCP 完全一致。

#### 验收标准

1. WHEN 用户调用 `db-get-event-resource` THEN 系统 SHALL 通过 SQLite 查询 `events` 和 `event_bound_resources` 返回资源结果。
2. WHEN `event_bound_resources` 中存在指定事件的 `database_resolved` 资源 THEN 系统 SHALL 返回 `success` 状态和资源列表。
3. IF 指定事件不存在于 `events` THEN 系统 SHALL 返回 `partial` 状态，并报告事件不存在。
4. IF 指定事件存在但没有可用的 `database_resolved` 资源 THEN 系统 SHALL 返回 `partial` 状态，并报告缺失的资源事实链环节。
5. WHEN 用户调用 `db-get-event-resource` THEN 系统 SHALL NOT 在查询阶段临时扫描 C++ export、解析 PDB 或调用 shader source resolver 来决定资源集合。
6. WHEN CLI 与 MCP 暴露 `db-get-event-resource` THEN 系统 SHALL 保持相同参数、相同默认值、相同结果结构和相同错误诊断。

### 需求 7：支持图形管线训练场景验收

**用户故事：** 作为一名回归测试维护者，我希望图形管线训练场景能够验证 `db-get-event-resource` 的完整资源输出，以便确认 Draw 事件资源解析没有回归。

#### 验收标准

1. WHEN 使用 `g:\pix-tool-set\data\train\scenario_03_graphics_pipeline_with_db_and_pdb` 的测试数据查询事件 `3854` THEN 系统 SHALL 返回 `status == success`。
2. WHEN 查询事件 `3854` 成功 THEN 系统 SHALL 返回 `resource_count == 26`。
3. WHEN 查询事件 `3854` 成功 THEN 系统 SHALL 返回包含 `VB`、`IB`、`CBV`、`SRV`、`Sampler`、`RTV`、`Depth` 和 `Stencil` 的资源集合。
4. WHEN 查询事件 `3854` 成功 THEN 系统 SHALL 返回包含 `VB 0`、`VB 4`、`VB 5`、`IB`、`CBV 0 : View`、`CBV 1 : Scene`、`CBV 2 : LocalVF`、`Sampler 0 : OpaqueBasePass_DBufferATextureSampler`、`RTV 0 : SceneColor`、`Depth : SceneDepthZ` 和 `Stencil : SceneDepthZ` 的显示名称。
5. IF 图形管线资源绑定缺少 PDB 或 shader source 名称 THEN 系统 SHALL 仍返回资源集合，并在显示名称或诊断中明确名称补充缺失。

### 需求 8：支持计算管线训练场景验收

**用户故事：** 作为一名回归测试维护者，我希望计算管线训练场景能够验证 `db-get-event-resource` 的完整资源输出，以便确认 Dispatch 事件资源解析没有回归。

#### 验收标准

1. WHEN 使用 `g:\pix-tool-set\data\train\scenario_05_compute_pipeline_with_db_and_pdb` 的测试数据查询事件 `3968` THEN 系统 SHALL 返回 `status == success`。
2. WHEN 查询事件 `3968` 成功 THEN 系统 SHALL 返回 `resource_count == 15`。
3. WHEN 查询事件 `3968` 成功 THEN 系统 SHALL 返回包含 `CBV`、`SRV` 和 `UAV` 的资源集合。
4. WHEN 查询事件 `3968` 成功 THEN 系统 SHALL 返回包含 `CBV 0 : _RootShaderParameters`、`CBV 1 : View`、`CBV 2 : VirtualShadowMap`、`CBV 3 : ForwardLightStruct`、`SRV Buffer 0 : VirtualShadowMap_LightGridData`、`SRV Texture 6 : SceneTexturesStruct_SceneDepthTexture`、`UAV Texture 0 : OutPageRequestFlags` 和 `UAV Texture 1 : OutPageReceiverMasks` 的显示名称。
5. WHEN 查询事件 `3968` 成功 THEN 系统 SHALL NOT 返回 `VB`、`IB`、`RTV`、`Depth` 或 `Stencil` 图形管线固定功能资源。

### 需求 9：提供可诊断的缺失事实链错误

**用户故事：** 作为一名排查数据库构建问题的开发者，我希望资源查询失败时能知道缺少哪一环事实，以便快速定位是映射、descriptor、root signature 还是资源名问题。

#### 验收标准

1. IF 事件 ID 映射缺失 THEN 系统 SHALL 在诊断信息中报告 `missing_event_id_mapping`。
2. IF root binding 缺失 THEN 系统 SHALL 在诊断信息中报告 `missing_root_bindings`。
3. IF root signature layout 缺失 THEN 系统 SHALL 在诊断信息中报告 `missing_root_signature_layout`。
4. IF descriptor writes 缺失 THEN 系统 SHALL 在诊断信息中报告 `missing_descriptor_writes`。
5. IF resource metadata 缺失 THEN 系统 SHALL 在诊断信息中报告 `missing_resource_metadata`。
6. IF shader binding name 缺失但资源集合已确定 THEN 系统 SHALL 返回 `success`，并仅在资源级诊断中报告名称补充缺失。

### 需求 10：补充技术文档和回归测试

**用户故事：** 作为一名项目维护者，我希望实现前后都有技术文档和自动化测试，以便资源绑定事实链的设计、实现和限制可追溯。

#### 验收标准

1. WHEN 开始实现资源绑定事实链前 THEN 系统 SHALL 补充现有 `db-get-event-resource`、旧 C++ export 资源解析流程和当前 `save-event-list` 数据边界的技术路线文档。
2. WHEN 确定新方案 THEN 系统 SHALL 新增或更新技术文档，说明 `events`、`resource facts` 和 `event_id_map` 的职责边界。
3. WHEN 实现数据库 schema 或导入逻辑变化 THEN 系统 SHALL 更新技术文档，记录新增表、字段、数据来源和查询路径。
4. WHEN 实现完成 THEN 系统 SHALL 更新技术文档，记录当前代码实际逻辑、CLI/MCP 行为和已知限制。
5. WHEN 添加回归测试 THEN 系统 SHALL 覆盖 `scenario_03_graphics_pipeline_with_db_and_pdb` 和 `scenario_05_compute_pipeline_with_db_and_pdb` 的 `db-get-event-resource` 用例。
6. IF 测试环境无法实际调用 PIX 或读取大型 `.wpix` 文件 THEN 系统 SHALL 使用训练场景数据库、模拟 C++ export 输入或受控 fixture 验证核心映射与资源解析逻辑。
