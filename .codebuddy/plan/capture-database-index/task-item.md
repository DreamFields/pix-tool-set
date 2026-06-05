# 实施计划

- [ ] 1. 设计并实现捕获数据库基础模块
  - 新增数据库路径解析、连接管理、schema version 常量、metadata 表和原子写入策略
  - 创建 `events`、`resources`、`resource_aliases`、`resource_references`、`descriptor_writes`、`root_bindings`、`event_bound_resources`、`shader_metadata`、`shader_bindings` 等核心表结构与索引
  - 编写 schema 创建、旧版本检测、数据库不可用诊断的单元测试
  - _需求：1.1、1.2、2.4、3.1、3.2、4.1、5.1、7.1、9.1、9.4、12.1_

- [ ] 2. 将数据库构建接入现有索引构建流程
  - 修改索引构建入口，使其在生成或复用 `index.json` 后同步生成或复用 `capture.sqlite`
  - 实现导出目录 fingerprint、schema version 校验、refresh 强制重建和构建失败不覆盖旧数据库的逻辑
  - 在构建结果中兼容性新增数据库路径、构建状态和关键表记录数量统计
  - _需求：1.1、1.2、1.4、1.5、2.1、2.2、9.1、9.2、9.3、9.5、10.4、11.3、12.1_

- [ ] 3. 导入事件、资源与基础引用数据
  - 基于现有 `index.json` 和 C++ 导出解析结果写入 event 基础信息、执行顺序、marker path、PSO、资源 ID、资源名称和声明位置
  - 实现同名资源 alias 归并表，并将现有 API 级 resource reference 写入数据库
  - 为 event 和 resource 查询创建数据库访问函数及测试
  - _需求：3.1、3.2、3.3、3.4、3.5、6.2、6.3、12.1、12.2_

- [ ] 4. 导入 descriptor 写入与 root binding 数据
  - 将 descriptor 写入解析结果结构化保存为 descriptor index、heap、resource ID、view type、调用位置和写入顺序
  - 将 root descriptor table、root CBV、stage、root index、descriptor 起点和 heap 信息写入 root binding 表
  - 对无法解析到具体 resource 的 descriptor 保留原始文本与诊断信息，并添加覆盖测试
  - _需求：4.1、4.2、4.3、4.4、4.5、12.4_

- [ ] 5. 预计算并缓存 event-bound resources
  - 实现从 IA、OM、SRV、UAV、CBV 和 sampler 绑定中提取 event-resource 关系的构建逻辑
  - 记录 shader stage、view type、binding name、root index、descriptor index、绑定来源、扫描范围和置信度诊断
  - 提供按 event 查询绑定资源、按 resource 查询使用事件的数据库 API，并验证不再默认全量遍历 shader event
  - _需求：5.1、5.2、5.3、5.4、5.5、5.6、6.4、12.5_

- [ ] 6. 改造事件绑定资源查询 MCP 路径
  - 修改 `get-event-resource` 的内部查询逻辑，优先使用数据库返回当前 event 的绑定资源
  - 保留旧 JSON/扫描逻辑作为 fallback，并在 diagnostics 中报告 database hit、query mode、database path、fallback 原因和 partial result
  - 保持现有 MCP 参数与主要返回字段兼容，补充数据库诊断测试
  - _需求：2.3、4.5、10.1、10.2、10.3、11.1、11.2、11.5、12.3_

- [ ] 7. 改造资源访问历史查询 MCP 路径
  - 修改 `get-resource-access-history` 的内部查询逻辑，先解析目标 event 当前绑定资源，再合并同名 alias 的资源 ID 集合
  - 使用数据库查询 API 级 resource references 与 shader 级 event-bound resources，并按执行顺序合并、排序、去重
  - 保留数据库缺失时的 fallback 或 partial result 诊断，并补充同名 alias、API 访问、shader 访问合并测试
  - _需求：6.1、6.2、6.3、6.4、6.5、6.6、10.1、10.2、10.3、12.2、12.3_

- [ ] 8. 引入 shader metadata 与 shader binding 缓存
  - 将 shader blob 定位结果、PSO、stage、blob 路径、大小和提取状态写入数据库
  - 在 source resolver 可用时缓存 source 解析状态、source 文本或摘要，并解析 HLSL resource declaration 写入 shader binding 表
  - 修改 `get-event-shader-source` 优先读取数据库缓存，必要时触发解析并写回数据库
  - _需求：7.1、7.2、7.3、7.4、7.5、10.1、10.3、11.5、12.4_

- [ ] 9. 为 buffer、texture 和资源元数据扩展建立最小闭环
  - 扩展 resource 表或关联表以容纳 dimension、format、size、width、height、depth、mip count、array size 等可选字段
  - 导入当前可解析的 buffer/texture 元数据，对不可解析字段记录 partial diagnostics，避免默认写入大块二进制内容
  - 在 MCP 资源相关输出中兼容性返回已有结构化元数据和缺失原因
  - _需求：8.1、8.2、8.3、8.4、8.5、10.3、11.5_

- [ ] 10. 完成端到端测试、性能诊断与回归验证
  - 添加覆盖数据库构建、缓存复用、refresh、auto export 后建库、fallback、schema 失效和 partial diagnostics 的测试
  - 对 `build-index`、`get-event-resource`、`get-resource-access-history`、`get-event-shader-source` 进行端到端回归验证
  - 验证资源访问历史查询通过数据库索引减少重复扫描，并在 diagnostics 中暴露可观测信息
  - _需求：1.3、2.5、9.3、10.1、10.2、10.4、10.5、11.1、11.3、11.4、12.1、12.2、12.3、12.4、12.5_
