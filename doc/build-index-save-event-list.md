# build-index 使用 save-event-list 构建数据库技术路线

## 背景与范围

本文档记录 `build-index` 工具从现有 C++ 导出扫描流程，迁移到 `pixtool.exe save-event-list` CSV 事件列表流程的技术路线。

本次改造范围只包含已经暴露给 CLI 与 MCP 的 `build-index` 工具。后续 `db-*` 分析工具仍以 `build-index` 生成的 SQLite 数据库为事实来源，通过 SQL 查询读取数据；本次不会把这些分析工具改成直接调用 `pixtool.exe`。

## 现有 build-index 流程

### 入口

- CLI 入口：`pix-tool-set build-index`，由 `src/pix_tool_set/cli.py` 根据工具注册表自动生成命令参数。
- MCP 入口：`build-index`，由 `src/pix_tool_set/tools/cpp_export_tools.py` 中的工具定义注册。
- 共享处理函数：`build_export_index(args, context)` 调用 `pix_tool_set.indexer.build_index(args["export_dir"], refresh=...)`。

### 现有参数流转

`build-index` 当前暴露参数如下：

| 参数 | 现有用途 |
|------|----------|
| `capture_path` | 由 `requires_cpp_export=True` 的前置校验或自动导出逻辑使用，处理函数内部不直接读取。 |
| `export_dir` | C++ 导出目录，也是索引与数据库缓存的根目录。 |
| `auto_export` | 由前置校验决定是否在缺少 C++ 导出时调用导出流程。 |
| `refresh` | 传入 `build_index()`，决定是否跳过 JSON 索引缓存与 SQLite 数据库缓存。 |

CLI 与 MCP 共享同一个工具 schema，因此命令行参数与 MCP 参数名称、默认值和语义来自同一份定义。

### 旧索引构建数据来源

`src/pix_tool_set/indexer.py` 的 `build_index()` 当前以 `export_dir` 下的 PIX C++ 导出文件为输入：

- `CommandLists*.cpp`：扫描 `GlobalId`、`PIXBeginEvent`、draw/dispatch 调用、PSO、root signature、root descriptor table、root CBV、IA/OM 绑定等事件上下文。
- `Descriptors*.cpp` 与 `ModifyDescriptors*.cpp`：扫描 descriptor 写入，构建 `descriptor_index`。
- `FrameResources*.cpp`：扫描资源名称与 root signature layout。
- `extracted_shaders/pso_*_*.cso`：构建 `pso_index`，供后续 shader source 缓存使用。

旧流程输出的内存索引包含 `events`、`events_by_global_id`、`shader_event_global_ids`、`pso_index`、`root_signatures`、`descriptor_index`、`resource_names`、`resource_refs_by_resource_id` 和 `diagnostics`。

### 旧缓存与数据库输出

现有缓存路径固定在 `export_dir/.cache/pix-tool-set/`：

- `index.json`：`build_index()` 写入的 JSON 索引缓存，使用源文件 fingerprint 与 `INDEX_VERSION` 判断是否可复用。
- `capture.sqlite`：`capture_db.build_capture_database()` 写入的 SQLite 数据库，使用数据库 schema version 与同一组 fingerprint 判断是否可复用。

数据库 schema 当前由 `src/pix_tool_set/capture_db.py` 初始化，核心表包括：

- `events`
- `resources`
- `resource_aliases`
- `resource_references`
- `descriptor_writes`
- `root_bindings`
- `event_bound_resources`
- `shader_metadata`
- `shader_bindings`

### 旧流程中需要替换的步骤

本次改造要替换的是 `build_index()` 中直接从 `CommandLists*.cpp` 解析事件列表的步骤。新流程应改为：

1. 通过 `pixtool.exe save-event-list <filename>` 从 `.wpix` 捕获文件导出 CSV 事件列表。
2. 解析 CSV 中的事件 ID、名称、层级、时间、持续时间和 counters 字段。
3. 使用 CSV 事件列表构建数据库中的事件基础事实。

旧流程中与 C++ 导出强绑定的资源、descriptor、root binding、shader blob 等信息不能与 CSV 事件来源并行写入事件列表，避免同一数据库中出现两套不一致的事件事实。若后续工具仍需要 CSV 无法提供的资源或 shader 细节，应在文档和工具诊断中明确说明数据来源限制或降级行为。

## 当前实现摘要

当前 `build-index` 已迁移为以下实际流程：

1. 通过 `capture_path` 定位 `.wpix` 捕获文件，并推导或使用调用方传入的 `export_dir` 作为输出根目录。
2. 调用 `pixtool.exe open-capture <capture_path> save-event-list <csv_path>` 导出 CSV 事件列表；如传入 `counters`，追加 `--counters=<pattern>`。
3. 将 CSV 写入或复用 `export_dir/.cache/pix-tool-set/event-list.csv`。
4. 解析 CSV 事件 ID、事件名称、层级、父事件、开始时间、持续时间和 counters 字段。
5. 构建 `index.json` 与 `capture.sqlite`，数据库路径仍为 `export_dir/.cache/pix-tool-set/capture.sqlite`。
6. CLI 与 MCP 共享同一 `build-index` schema，返回相同核心字段：CSV 路径、数据库路径、刷新状态、缓存状态和诊断信息。

## 阶段实现记录

### 事件列表导出边界

已新增 `src/pix_tool_set/event_list_export.py`，作为 `build-index` 调用 `pixtool.exe save-event-list` 的独立服务边界：

- 默认 `pixtool.exe` 路径优先使用 `C:\Program Files\Microsoft PIX\2603.25\pixtool.exe`，显式 `pixtool_path` 可覆盖；默认路径不可用时会继续复用现有 `PIXTOOL_PATH` 与 PATH 发现逻辑。
- CSV 输出路径固定推导为 `export_dir/.cache/pix-tool-set/event-list.csv`；如果未传 `export_dir`，则根据 `capture_path` 复用现有默认导出目录规则推导输出根目录。
- `refresh=false` 且 CSV 文件更新时间不早于 `.wpix` 捕获文件时复用 CSV；否则重新执行导出。
- 命令格式为 `pixtool.exe open-capture <capture_path> save-event-list <csv_path>`，提供 counters 时追加 `--counters=<pattern>`。
- 该模块允许测试注入命令执行器，避免自动化测试依赖真实 PIX 安装。

### CSV 解析与数据库构建

已新增 `src/pix_tool_set/event_list_csv.py`，负责把 `save-event-list` CSV 映射为 `build-index` 可写入 SQLite 的事件索引结构：

- 必需字段：事件行 ID 与事件名称。事件行 ID 优先兼容 `GlobalId`、`Global ID`、`Event ID`、`ID` 等常见表头，同时兼容 PIX `save-event-list` 实际输出中的 `Queue ID`。
- 可选字段：层级、父事件 ID、开始时间、持续时间。其中 `Parent` 会映射为 `parent_global_id`，用于保留 `Queue ID` 事件树关系。
- counters 字段：未被识别为核心字段的非空列会保留到 `event_list.counters`，并写入数据库 `events.counters_json`。
- 若缺少必需表头或必需值，错误会包含阶段、CSV 路径、缺失字段和行号。

PIX `save-event-list` 的基础 CSV 实际表头可能为 `Queue ID, Parent, Name, Global ID`。其中 `Global ID` 对部分命令行为空，但 `Queue ID` 是完整且稳定的事件列表行标识。因此当前导入逻辑使用 `Queue ID` 作为数据库 `events.global_id` 的来源，并使用 `Parent` 填充 `events.parent_global_id`，避免因为稀疏的 `Global ID` 中断数据库构建。

解析器会把原始 CSV 行保存在 `event_list.raw` 中，用于诊断和后续追溯。若 CSV 行存在超出表头数量的额外列，Python CSV 解析器会产生空键；当前实现会将该键规范化为 `extra_columns`，保证 `event_json` 能稳定写入 SQLite。

`src/pix_tool_set/capture_db.py` 已将数据库 schema 提升到版本 3，并扩展 `events` 表字段：`event_depth`、`start_time`、`duration`、`counters_json`。这些字段让后续 SQL 查询可以直接读取 CSV 中的结构化事件信息。

### build-index 入口替换

`src/pix_tool_set/tools/cpp_export_tools.py` 中的 `build-index` 已改为直接调用 `build_index_from_capture()`：

- 必需参数为 `capture_path`。
- 可选参数为 `export_dir`、`refresh`、`pixtool_path`、`counters`。
- CLI 与 MCP 仍共享同一份工具 schema，因此参数名称、默认值和返回字段保持一致。
- 成功返回包含 `event_list_csv_path`、`database_path`、`event_list_cache_hit`、`event_list_refreshed` 和数据库表行数。

`src/pix_tool_set/indexer.py` 当前保留旧 C++ 扫描辅助函数，但 `build-index` 不再调用它们。新的索引 payload 以 `save_event_list_csv` 作为来源，资源、descriptor、root binding、shader metadata 等 CSV 无法提供的索引段写为空结构，避免和 CSV 事件事实并行混写。

`build_index_from_capture()` 额外保留内部 `runner` 注入参数，仅用于自动化测试模拟 `save-event-list` 命令执行；该参数不暴露到 CLI 或 MCP，避免增加用户可见参数数量。

### 后续数据库工具边界

`src/pix_tool_set/tools/database_query_tools.py` 已改为数据库优先：

- 如果已有 `export_dir/.cache/pix-tool-set/capture.sqlite` 且未要求刷新，可以直接读取数据库。
- 如果需要刷新或数据库不存在，则必须提供 `capture_path`，并复用同一个 `save-event-list` 构建流程。
- 数据库工具不再强制 C++ 导出前置校验；`pixtool_path` 与 `counters` 仅在需要重建数据库时使用。
- CSV 无法提供 descriptor、root binding、shader source 等细节，因此依赖这些细节的查询可能返回 `partial`，原因会通过诊断信息暴露。

## 实测：手动导出 CSV 示例

### 测试对象

- 捕获文件：`c:\Users\vinmeng\Desktop\ManyLights\debug\Tiled.wpix`
- PIX 工具路径：`C:\Program Files\Microsoft PIX\2603.25\pixtool.exe`
- 导出目标：`c:\Users\vinmeng\Desktop\ManyLights\debug\Tiled_event_list_basic.csv`

### 基础导出（成功）

命令：

```powershell
& "C:\Program Files\Microsoft PIX\2603.25\pixtool.exe" open-capture "c:\Users\vinmeng\Desktop\ManyLights\debug\Tiled.wpix" save-event-list "c:\Users\vinmeng\Desktop\ManyLights\debug\Tiled_event_list_basic.csv"
```

输出：

```text
Writing c:\Users\vinmeng\Desktop\ManyLights\debug\Tiled_event_list_basic.csv
```

结果：

- 文件大小：`903295` bytes
- 行数：`22156`
- CSV 表头：`Queue ID, Parent, Name, Global ID`
- 前几条事件示例：`Wait`、`Reset`、`EndQuery`、`BeginQuery`

### 带计数器导出（失败）

命令：

```powershell
& "C:\Program Files\Microsoft PIX\2603.25\pixtool.exe" open-capture "c:\Users\vinmeng\Desktop\ManyLights\debug\Tiled.wpix" save-event-list "c:\Users\vinmeng\Desktop\ManyLights\debug\Tiled_event_list.csv" "--counters=*"
```

错误：

```text
save-event-list failed: Performance analysis failed (E_PIX_PERFORMANCE_ANALYSIS_FAILED)
```

结论：

- `save-event-list` 基础导出（不带 `--counters`）对该 `.wpix` 文件可用。
- `--counters=*` 会触发 PIX 性能分析阶段，当前测试文件下该阶段失败，因此计数器导出需要视具体捕获文件而定。
- `build-index` 的 `counters` 参数应设计为可选，并在失败时向用户暴露诊断信息。

