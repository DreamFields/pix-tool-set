# 需求文档

## 引言

本功能旨在优化 `pix-tool-set` 对 PIX C++ 导出工程的查询能力。当前项目已经能够从导出的 C++ 文件中构建 `index.json`，但在资源访问历史、事件绑定资源、shader 资源解析等场景中，仍存在运行时重复扫描事件、descriptor、shader 绑定关系的问题。随着 MCP 工具需要频繁回答“某个 event 中某个资源的访问历史”“某个 buffer/texture 被哪些 pass 使用”“某个 shader 关联了哪些资源”等问题，单纯依赖 JSON 与 Python 内存遍历会带来明显性能瓶颈。

本功能将引入一个基于导出 C++ 工程生成的捕获数据库，例如 SQLite 数据库 `capture.sqlite`。数据库应作为后续 MCP 操作的统一查询基础，逐步承载 event、resource、descriptor、resource reference、event-bound resource、shader metadata、shader binding、buffer/texture metadata 等结构化信息。第一阶段应保持现有 MCP 接口兼容，并优先优化资源访问历史与事件资源查询的高频路径。

## 需求

### 需求 1：生成捕获数据库

**用户故事：** 作为一名图形调试工具使用者，我希望系统能够从 PIX 导出的 C++ 工程生成结构化数据库，以便后续查询不再反复扫描大量 C++ 导出内容和 JSON 数据。

#### 验收标准

1. WHEN 用户执行构建索引操作 THEN 系统 SHALL 在导出目录的缓存目录下生成捕获数据库文件。
2. WHEN 捕获数据库生成成功 THEN 系统 SHALL 返回数据库路径、索引路径和构建状态信息。
3. IF 导出目录不存在或缺少必要 C++ 导出文件 THEN 系统 SHALL 返回明确的错误信息，并且不得生成不完整数据库。
4. IF 已存在数据库且导出内容未发生变化 THEN 系统 SHALL 复用现有数据库，避免不必要的重建。
5. IF 用户显式要求刷新索引 THEN 系统 SHALL 重新构建数据库并覆盖旧的缓存数据。

### 需求 2：保留现有索引兼容性

**用户故事：** 作为一名项目维护者，我希望数据库化改造不破坏现有 `index.json` 与 MCP 工具输出，以便可以渐进迁移并降低回归风险。

#### 验收标准

1. WHEN 系统生成捕获数据库 THEN 系统 SHALL 保留现有 `index.json` 的生成能力。
2. WHEN 现有 MCP 工具调用构建索引能力 THEN 系统 SHALL 保持已有响应字段兼容，并可新增数据库相关字段。
3. IF 数据库查询失败但 `index.json` 可用 THEN 系统 SHALL 能够回退到现有 JSON 查询逻辑。
4. WHEN 数据库 schema 版本变化 THEN 系统 SHALL 能够识别旧版本数据库并触发重建或给出明确诊断。
5. IF 捕获数据库未启用 THEN 系统 SHALL 保持当前行为不变。

### 需求 3：结构化存储事件与资源基础信息

**用户故事：** 作为一名 MCP 工具使用者，我希望 event、resource、marker、PSO 等基础信息被结构化存储，以便快速定位某个事件或资源的上下文。

#### 验收标准

1. WHEN 数据库构建时发现 event THEN 系统 SHALL 存储其 `global_id`、事件类型、名称、文件位置、marker path、PSO 信息和执行顺序。
2. WHEN 数据库构建时发现 resource THEN 系统 SHALL 存储其资源 ID、资源名称、声明位置和可解析的基础元数据。
3. WHEN 多个 resource 共享相同名称 THEN 系统 SHALL 存储资源别名关系，以支持同名资源访问历史合并查询。
4. WHEN 查询某个 `global_id` THEN 系统 SHALL 能够通过数据库返回对应 event 的基础上下文。
5. WHEN 查询某个资源名称 THEN 系统 SHALL 能够通过数据库返回匹配的资源 ID 集合。

### 需求 4：结构化存储 descriptor 与 root binding 信息

**用户故事：** 作为一名图形问题分析者，我希望系统能够保存 descriptor 写入和 root binding 关系，以便快速判断某个 event 当前绑定了哪些资源。

#### 验收标准

1. WHEN 数据库构建时解析到 descriptor 写入 THEN 系统 SHALL 存储 descriptor index、heap、resource ID、view type、调用位置和写入顺序。
2. WHEN 数据库构建时解析到 root descriptor table THEN 系统 SHALL 存储 event、stage、root index、descriptor 起点和 heap 信息。
3. WHEN 数据库构建时解析到 root CBV THEN 系统 SHALL 存储 event、stage、root index、resource ID 和偏移信息。
4. IF descriptor 写入无法解析到具体 resource THEN 系统 SHALL 保留原始文本和诊断信息，避免静默丢失。
5. WHEN 查询 event 当前绑定资源 THEN 系统 SHALL 优先使用数据库中的 descriptor 与 root binding 关系。

### 需求 5：预计算 event-bound resources

**用户故事：** 作为一名需要频繁检索资源访问历史的用户，我希望系统预先计算每个 shader event 与其绑定资源之间的关系，以便资源历史查询能够快速返回结果。

#### 验收标准

1. WHEN 数据库构建或懒加载解析 event-bound resources THEN 系统 SHALL 记录 event、resource、view type、shader stage、binding name、root index、descriptor index 和绑定来源。
2. WHEN 资源通过 IA 阶段绑定为 vertex/index buffer THEN 系统 SHALL 将该绑定关系写入 event-bound resources。
3. WHEN 资源通过 OM 阶段绑定为 render target 或 depth stencil THEN 系统 SHALL 将该绑定关系写入 event-bound resources。
4. WHEN 资源通过 SRV、UAV、CBV 或 sampler 方式绑定 THEN 系统 SHALL 将解析结果写入 event-bound resources。
5. IF 绑定关系来自启发式 descriptor 扫描 THEN 系统 SHALL 记录扫描范围、置信度或诊断信息。
6. WHEN 查询某个资源被哪些 shader event 使用 THEN 系统 SHALL 使用数据库索引查询，而不是每次遍历所有 shader event。

### 需求 6：优化资源访问历史查询

**用户故事：** 作为一名定位 GPU 资源问题的用户，我希望资源访问历史查询能够快速合并 API 级访问和 shader 级访问，以便判断资源在整帧中的读写路径。

#### 验收标准

1. WHEN 用户请求某个 event 中资源的访问历史 THEN 系统 SHALL 先解析该 event 当前绑定的目标资源。
2. WHEN 目标资源存在同名 alias THEN 系统 SHALL 将同名资源 ID 纳入访问历史查询范围。
3. WHEN 查询 API 级访问历史 THEN 系统 SHALL 从数据库的 resource references 中查询匹配记录。
4. WHEN 查询 shader 级访问历史 THEN 系统 SHALL 从 event-bound resources 中查询匹配记录。
5. WHEN 返回访问历史 THEN 系统 SHALL 按事件执行顺序合并、排序并去重 API 访问和 shader 访问。
6. IF 数据库中缺少必要记录 THEN 系统 SHALL 使用旧逻辑回退或返回明确的 partial result 诊断。

### 需求 7：支持 shader 信息数据库化

**用户故事：** 作为一名 shader 调试者，我希望 shader blob、shader source 和 shader binding 信息能够被缓存到数据库中，以便 MCP 工具可以快速回答 shader 与资源之间的关系。

#### 验收标准

1. WHEN 系统能够从导出工程中定位 shader blob THEN 系统 SHALL 存储 PSO、stage、blob 路径、blob 大小和提取状态。
2. IF shader source resolver 可用 THEN 系统 SHALL 存储解析出的 source 状态、source 文本或 source 摘要。
3. WHEN 系统能够解析 HLSL resource declaration THEN 系统 SHALL 存储 shader binding 名称、register、space、view type 和 resource dimension。
4. IF shader source 不可用 THEN 系统 SHALL 保留 blob metadata，并返回明确的 source unavailable 诊断。
5. WHEN 查询某个 event 的 shader source THEN 系统 SHALL 优先使用数据库缓存，必要时再触发解析并写回数据库。

### 需求 8：为 buffer 与 texture 深度索引预留扩展能力

**用户故事：** 作为一名图形性能和渲染问题分析者，我希望数据库结构能够逐步扩展到 buffer 数据、纹理贴图和资源元数据，以便未来可以进一步分析资源内容与生命周期。

#### 验收标准

1. WHEN 数据库 schema 设计资源表 THEN 系统 SHALL 允许后续扩展 resource dimension、format、size、width、height、depth、mip count、array size 等字段。
2. WHEN 当前版本无法解析完整 buffer 或 texture 元数据 THEN 系统 SHALL 保留可用字段，并记录未解析原因。
3. WHEN 后续新增 buffer 数据或 texture metadata 解析 THEN 系统 SHALL 能够通过 schema 版本迁移或数据库重建纳入新字段。
4. IF 资源二进制内容体积较大 THEN 系统 SHALL 避免默认将大块二进制内容直接写入数据库，除非用户明确启用。
5. WHEN MCP 工具查询资源基础描述 THEN 系统 SHALL 返回数据库中已有的结构化元数据和 partial 诊断。

### 需求 9：提供一致性校验与缓存失效机制

**用户故事：** 作为一名长期使用该工具分析不同 PIX 捕获的用户，我希望数据库缓存能够自动识别导出内容变化，以便避免查询到过期结果。

#### 验收标准

1. WHEN 构建数据库 THEN 系统 SHALL 记录导出目录 fingerprint、schema version、工具版本和构建时间。
2. WHEN 再次查询数据库 THEN 系统 SHALL 校验 fingerprint 与 schema version 是否匹配。
3. IF 导出目录内容发生变化 THEN 系统 SHALL 标记数据库过期，并触发重建或返回需要刷新的诊断。
4. IF schema version 不兼容 THEN 系统 SHALL 拒绝直接使用旧数据库，并执行重建或提示用户刷新。
5. WHEN 数据库构建失败 THEN 系统 SHALL 不得覆盖上一次仍可用的数据库，除非用户显式强制覆盖。

### 需求 10：提供查询诊断与性能可观测性

**用户故事：** 作为一名维护者，我希望 MCP 查询能够报告是否命中数据库、是否发生回退以及耗时信息，以便验证数据库化改造的收益。

#### 验收标准

1. WHEN MCP 工具使用数据库查询 THEN 系统 SHALL 在 diagnostics 中返回 `database_hit`、`database_path` 和 `query_mode`。
2. WHEN 查询发生 fallback THEN 系统 SHALL 在 diagnostics 中说明 fallback 原因。
3. WHEN 查询返回 partial result THEN 系统 SHALL 在 diagnostics 中说明缺失的数据类型。
4. WHEN 构建数据库完成 THEN 系统 SHALL 输出关键表的记录数量统计。
5. WHEN 用户对比优化前后表现 THEN 系统 SHALL 提供足够诊断信息来判断是否减少了全量扫描。

### 需求 11：保持 MCP 工具行为稳定

**用户故事：** 作为一名依赖 MCP 工具链的用户，我希望数据库化改造不会改变既有工具的调用方式，以便已有工作流可以平滑升级。

#### 验收标准

1. WHEN 用户调用现有 MCP 工具 THEN 系统 SHALL 保持参数兼容。
2. WHEN 用户不传入数据库相关参数 THEN 系统 SHALL 使用默认数据库缓存策略。
3. IF 用户传入 refresh 参数 THEN 系统 SHALL 同步刷新 `index.json` 与捕获数据库。
4. IF 用户传入 auto export 参数 THEN 系统 SHALL 在导出完成后继续生成或刷新数据库。
5. WHEN 工具返回查询结果 THEN 系统 SHALL 保持原有主要字段语义不变，并仅以兼容方式增加数据库诊断字段。

### 需求 12：提供测试覆盖与成功标准

**用户故事：** 作为一名项目维护者，我希望数据库化功能具备自动化测试，以便确认优化不会破坏资源历史、shader 查询和索引构建行为。

#### 验收标准

1. WHEN 新增数据库构建逻辑 THEN 系统 SHALL 提供覆盖 schema 创建、数据导入和缓存复用的测试。
2. WHEN 修改资源访问历史查询 THEN 系统 SHALL 提供覆盖同名资源 alias、API 访问和 shader 访问合并的测试。
3. WHEN 数据库不可用或过期 THEN 系统 SHALL 提供覆盖 fallback 行为的测试。
4. WHEN descriptor 或 shader source 信息不完整 THEN 系统 SHALL 提供覆盖 partial diagnostics 的测试。
5. WHEN 优化完成 THEN 系统 SHALL 证明资源访问历史查询不再默认遍历所有 shader event 进行重复解析。
