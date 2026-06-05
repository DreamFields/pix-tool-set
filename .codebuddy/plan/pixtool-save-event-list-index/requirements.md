# 需求文档

## 引言

本功能旨在改造本项目中的 `build-index` 工具：将 `build-index` 当前使用的索引构建数据来源替换为调用 `C:\Program Files\Microsoft PIX\2603.25\pixtool.exe` 的 `save-event-list <filename> [--counters=<pattern>]` 命令，以 CSV 格式保存 PIX 捕获文件的事件列表，然后由 `build-index` 基于该 CSV 事件列表构建项目所需的数据库。

本需求中的“构建索引的步骤”特指项目已经暴露的 `build-index` 工具，包括对应的 MCP 工具入口与 CLI 入口；本次改造不泛指其它分析工具，也不要求将所有数据库查询工具改成直接调用 `pixtool.exe`。后续分析工具仍应以 `build-index` 生成的数据库文件为基础，通过 SQL 查询获得结果。

该改造需要保持 MCP 工具与 CLI 工具行为一致，工具参数尽可能少且语义明确，并覆盖 `pixtool.exe` 路径、CSV 解析、数据库构建、缓存刷新、错误处理、现有能力兼容、测试验证和技术文档更新。

## 需求

### 需求 1：将 `build-index` 的事件数据来源替换为 `save-event-list` CSV

**用户故事：** 作为一名 PIX 捕获分析工具使用者，我希望 `build-index` 工具通过 `pixtool.exe save-event-list` 导出 CSV 事件列表，以便索引数据库基于稳定、直接的事件列表数据构建。

#### 验收标准

1. WHEN 用户触发 `build-index` 工具 THEN 系统 SHALL 调用 `C:\Program Files\Microsoft PIX\2603.25\pixtool.exe` 的 `save-event-list <filename>` 命令生成 CSV 格式事件列表。
2. WHEN 用户通过 `build-index` 提供 counters 模式 THEN 系统 SHALL 将该模式转换为 `save-event-list` 的 `--counters=<pattern>` 参数。
3. IF 用户未通过 `build-index` 提供 counters 模式 THEN 系统 SHALL 使用不带 `--counters` 的 `save-event-list` 命令生成默认事件列表。
4. IF `build-index` 当前存在旧的事件索引导出或扫描步骤 THEN 系统 SHALL 用 `save-event-list` CSV 导出与导入流程替换该步骤，而不是在旧步骤之外额外叠加一套并行索引来源。
5. IF 指定的 `pixtool.exe` 路径不存在或不可执行 THEN `build-index` SHALL 返回明确错误，说明 PIX 工具路径不可用，并不得继续构建数据库。
6. WHEN `save-event-list` 命令执行失败 THEN `build-index` SHALL 捕获退出码和错误输出，并向 CLI 与 MCP 调用方返回一致的失败信息。

### 需求 2：由 `build-index` 基于 CSV 事件列表构建数据库

**用户故事：** 作为一名工具维护者，我希望 `build-index` 能够从 `save-event-list` 生成的 CSV 文件构建数据库，以便后续工具继续统一通过 SQL 查询访问事件数据。

#### 验收标准

1. WHEN `save-event-list` CSV 事件列表生成成功 THEN `build-index` SHALL 解析该 CSV 文件并写入项目约定的数据库文件。
2. WHEN CSV 中包含事件层级、事件名称、事件 ID、开始时间、持续时间或 counters 字段 THEN `build-index` SHALL 将可识别字段映射到数据库中可查询的结构化表字段。
3. IF CSV 缺少某些可选字段 THEN `build-index` SHALL 继续导入可用字段，并为缺失字段使用空值或默认值，而不得导致整体导入失败。
4. IF CSV 缺少构建数据库所需的关键字段 THEN `build-index` SHALL 返回明确错误，指出缺失字段名称和来源 CSV 文件。
5. WHEN 数据库构建完成 THEN `build-index` SHALL 输出或返回数据库路径，使后续分析工具能够基于该数据库执行 SQL 查询。

### 需求 3：保持 `build-index` 的 CLI 入口与 MCP 入口行为一致

**用户故事：** 作为一名自动化集成使用者，我希望 `build-index` 的 CLI 与 MCP 入口完全一致，以便在脚本、IDE 和代理调用中获得相同结果。

#### 验收标准

1. WHEN 用户通过 CLI 触发 `build-index` THEN 系统 SHALL 使用与 MCP `build-index` 相同的 `save-event-list` 导出逻辑和数据库构建逻辑。
2. WHEN 用户通过 MCP 触发 `build-index` THEN 系统 SHALL 使用与 CLI `build-index` 相同的参数语义、默认值和错误处理。
3. IF CLI 与 MCP 都支持某个 `build-index` 参数 THEN 系统 SHALL 保证该参数名称、含义和处理结果一致。
4. IF 某个参数无法做到 CLI 与 MCP 行为一致 THEN 系统 SHALL 不引入该参数，除非有明确技术文档说明原因和替代方案。
5. WHEN `build-index` 成功或失败 THEN CLI 输出与 MCP 返回值 SHALL 包含一致的核心信息，包括 CSV 路径、数据库路径、刷新状态和错误阶段。

### 需求 4：控制 `build-index` 参数数量并保持参数用途确定

**用户故事：** 作为一名工具调用者，我希望 `build-index` 参数尽可能少且用途明确，以便降低调用成本和误用风险。

#### 验收标准

1. WHEN 系统暴露 `build-index` 能力 THEN 系统 SHALL 仅保留生成事件列表与构建数据库所必需的输入参数。
2. WHEN 参数用于指定 PIX 捕获文件、输出目录、刷新策略、PIX 工具路径或 counters 模式 THEN 系统 SHALL 在 CLI 帮助信息、MCP schema 和技术文档中说明其确定用途。
3. IF 某个参数可由捕获文件路径或输出目录推导得到 THEN `build-index` SHALL 优先自动推导，而不是要求用户额外传入。
4. IF 用户向 `build-index` 传入冲突参数 THEN 系统 SHALL 返回明确错误，并说明冲突参数及建议修正方式。
5. IF 固定默认 PIX 工具路径可满足需求 THEN `build-index` SHALL 使用 `C:\Program Files\Microsoft PIX\2603.25\pixtool.exe` 作为默认路径，并仅在确有必要时允许覆盖。

### 需求 5：兼容现有数据库驱动的分析工具

**用户故事：** 作为一名现有功能使用者，我希望 `build-index` 的数据来源变化不破坏已有分析工具，以便继续使用事件分析、资源查询和着色器查询能力。

#### 验收标准

1. WHEN 数据库由 `build-index` 基于 CSV 事件列表构建完成 THEN 现有以数据库为基础的分析工具 SHALL 能够继续读取数据库并执行 SQL 查询。
2. WHEN 现有工具依赖事件全局 ID、事件名称或事件层级信息 THEN 新数据库 SHALL 提供兼容字段或明确的迁移映射。
3. IF 某些现有字段无法从 `save-event-list` CSV 中获得 THEN 系统 SHALL 在技术文档中记录限制，并在相关工具中提供明确错误或降级行为。
4. WHEN 数据库 schema 发生变化 THEN 系统 SHALL 更新 schema 初始化、迁移或读取逻辑，避免旧逻辑静默读取错误数据。
5. IF 某个后续工具仍需要非 CSV 来源的数据 THEN 系统 SHALL 明确区分该工具的数据来源，不得把该需求混入 `build-index` 的事件列表数据库构建职责中。

### 需求 6：`build-index` 文件生成、缓存与刷新策略

**用户故事：** 作为一名频繁分析捕获文件的使用者，我希望 `build-index` 生成的事件列表 CSV 与数据库文件行为可预测，以便避免重复耗时导出或使用过期数据。

#### 验收标准

1. WHEN 输出目录中已存在匹配当前捕获文件的事件列表 CSV 和数据库文件 THEN `build-index` SHALL 根据刷新策略决定复用或重新生成。
2. IF 用户显式请求刷新 THEN `build-index` SHALL 重新执行 `save-event-list` 并重建数据库。
3. IF 捕获文件更新时间晚于已生成 CSV 或数据库 THEN `build-index` SHALL 将缓存视为过期并重新生成。
4. WHEN `build-index` 生成 CSV 或数据库文件 THEN 系统 SHALL 使用稳定、可预测的文件路径，便于用户检查和复用。
5. IF 输出目录不存在 THEN `build-index` SHALL 自动创建必要目录，或在无法创建时返回明确错误。

### 需求 7：`build-index` 错误处理与用户可诊断性

**用户故事：** 作为一名排查问题的开发者，我希望 `build-index` 失败时获得清晰、可操作的错误信息，以便快速定位是 PIX 导出、CSV 解析还是数据库写入问题。

#### 验收标准

1. WHEN `save-event-list` 阶段失败 THEN `build-index` SHALL 报告失败阶段、执行命令摘要、退出码和关键错误输出。
2. WHEN CSV 解析阶段失败 THEN `build-index` SHALL 报告失败阶段、CSV 文件路径、行号或字段名等可定位信息。
3. WHEN 数据库写入阶段失败 THEN `build-index` SHALL 报告失败阶段、数据库路径和底层错误原因。
4. IF `build-index` 过程中产生了中间文件 THEN 错误信息 SHALL 说明中间文件是否保留及其路径。
5. WHEN CLI 与 MCP 返回错误 THEN 系统 SHALL 使用一致的错误分类和核心信息。

### 需求 8：技术文档与现有 `build-index` 流程说明

**用户故事：** 作为一名项目维护者，我希望改造前后都有清晰技术文档，以便理解现有 `build-index` 流程、变更原因和新流程的技术路线。

#### 验收标准

1. WHEN 开始实现该方案前 THEN 系统 SHALL 补充现有 `build-index` 工具的详细技术路线说明。
2. WHEN 确定新方案 THEN 系统 SHALL 新增或更新技术文档，说明 `build-index` 使用 `save-event-list` 导出 CSV 并构建数据库的技术路线。
3. WHEN 实现完成 THEN 系统 SHALL 更新技术文档，记录当前 `build-index` 代码的实际逻辑、关键入口、数据流和限制。
4. IF 用户对 `build-index` 新方案提出疑问并形成结论 THEN 系统 SHALL 将解答同步更新到对应技术文档中。
5. WHEN 文档包含代码注释或示例命令 THEN 文档 SHALL 使用中文说明，代码中的注释仍保持英文。

### 需求 9：`build-index` 验证与回归保障

**用户故事：** 作为一名代码维护者，我希望本次 `build-index` 改造具备自动化验证，以便确认 CSV 导出、数据库构建和现有工具查询没有回归。

#### 验收标准

1. WHEN 实现 CSV 解析与数据库构建逻辑 THEN 系统 SHALL 提供覆盖正常 CSV、缺失可选字段、缺失关键字段和 counters 字段的测试。
2. WHEN 修改 CLI 与 MCP 的 `build-index` 入口 THEN 系统 SHALL 提供验证二者参数语义与核心行为一致的测试或检查。
3. WHEN 数据库 schema 或导入逻辑变化 THEN 系统 SHALL 提供验证关键 SQL 查询可运行的测试。
4. IF 测试环境无法实际调用 `pixtool.exe` THEN 系统 SHALL 支持通过模拟 CSV 或命令执行抽象进行测试。
5. WHEN 测试失败 THEN 系统 SHALL 能够区分导出命令、CSV 解析、数据库写入和查询兼容性相关失败。
