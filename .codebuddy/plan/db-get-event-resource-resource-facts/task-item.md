# 实施计划

- [ ] 1. 补充现有流程与新事实链技术文档
   - 梳理当前 `build-index`、`save-event-list` 导入、旧 `db-get-event-resource` 与 C++ export 资源解析流程，并写入技术路线文档
   - 在文档中明确 `events` 只来自 `save-event-list`，资源绑定事实来自 C++ export，`event_id_map` 负责连接两套事件 ID
   - _需求：1.1、1.2、1.4、2.1、3.5、10.1、10.2_

- [ ] 2. 扩展数据库 schema 与迁移初始化逻辑
   - 新增或更新 `event_id_map`、`resources`、`resource_aliases`、`descriptor_writes`、`root_bindings`、`root_signature_layout`、`shader_bindings`、`event_bound_resources` 等表结构
   - 为缺失事实链诊断、来源文件、匹配置信度、写入顺序和 `database_resolved` 状态预留字段
   - 更新 schema 技术文档，记录表职责、字段语义和 SQL 查询路径
   - _需求：2.1、2.3、2.4、2.5、4.1、4.2、4.3、4.4、4.5、5.1、5.7、9.1、9.2、9.3、9.4、9.5、10.3_

- [ ] 3. 保持 `events` 导入边界并统一查询 ID 语义
   - 检查并加固 `save-event-list` CSV 导入逻辑，确保 `Queue ID` 写入 `events.global_id`，`Parent` 写入父子关系
   - 禁止 C++ export 导入流程覆盖 `events` 主事件列表或使用 CSV `Global ID` 替代 `Queue ID`
   - 保持 CLI 与 MCP 中 `global_id` 参数均表示数据库 `events.global_id`
   - _需求：1.1、1.2、1.3、1.4、1.5、3.6、6.6_

- [ ] 4. 实现 C++ export 事件映射导入器
   - 从 C++ export 中提取可用于匹配的 C++ `GlobalId`、事件名称、顺序、层级、源文件位置等信号
   - 实现 `Queue ID` 到 C++ `GlobalId` 的唯一匹配、冲突检测、缺失映射记录和置信度输出
   - 为映射成功、缺失、冲突和低置信度场景编写单元测试
   - _需求：2.1、2.2、2.3、2.4、2.5、2.6、9.1_

- [ ] 5. 实现 C++ export 资源事实导入器
   - 从 `CommandLists*.cpp` 导入 PSO、root signature、root descriptor table、root descriptor、IA 绑定和 OM 绑定
   - 从 `Descriptors*.cpp`、`ModifyDescriptors*.cpp` 导入 descriptor heap 写入历史
   - 从 `FrameResources*.cpp` 或等价导出文件导入资源名称、资源别名和 root signature layout
   - 对缺失导出文件生成可查询的诊断信息，并为部分缺失输入编写测试
   - _需求：3.1、3.2、3.3、3.4、3.5、4.1、4.2、4.3、4.4、9.2、9.3、9.4、9.5_

- [ ] 6. 实现 shader binding 名称补充导入逻辑
   - 将可用的 shader binding 信息写入 `shader_bindings` 或等价结构，只用于补充显示名称和 HLSL binding name
   - 确保 PDB 或 shader source 不可用时仍能返回资源集合，并仅在资源级诊断中标记名称补充缺失
   - 为有无 shader binding 信息的输入分别添加测试
   - _需求：4.5、4.6、7.5、9.6_

- [ ] 7. 实现 `event_bound_resources` 预计算与刷新逻辑
   - 基于 `event_id_map`、`root_bindings`、`descriptor_writes`、`root_signature_layout` 和 `resources` 生成物化资源结果
   - 实现 descriptor table 展开、执行前最后一次 descriptor 写入选择、view type 匹配过滤
   - 分别处理图形管线 IA/OM 固定功能资源和计算管线 CS 阶段资源，避免跨管线混入
   - 为缺失映射、缺失 root binding、缺失 root signature、缺失 descriptor writes 和缺失 resource metadata 编写诊断测试
   - _需求：5.1、5.2、5.3、5.4、5.5、5.6、5.7、8.5、9.1、9.2、9.3、9.4、9.5_

- [ ] 8. 改造 `db-get-event-resource` 为纯数据库查询路径
   - 将 CLI 与 MCP 的 `db-get-event-resource` 实现改为查询 `events` 和 `event_bound_resources`
   - 移除或隔离查询阶段对 C++ export 扫描、PDB 解析、shader source resolver 的依赖
   - 统一 `success`、`partial`、事件不存在、事实链缺失等输出结构和错误诊断
   - _需求：6.1、6.2、6.3、6.4、6.5、6.6、9.1、9.2、9.3、9.4、9.5、9.6_

- [ ] 9. 接入 `build-index` 刷新流程并保持工具参数稳定
   - 在数据库构建或刷新阶段接入 C++ export 检查、资源事实导入、事件映射生成和 `event_bound_resources` 预计算
   - 确保除导出 C++、生成索引、生成数据库、解析 PDB 外，资源查询工具都只依赖生成后的数据库 SQL 查询
   - 保持 MCP 与 CLI 参数数量尽可能少、含义确定、默认值和结果结构一致
   - _需求：3.4、3.6、5.1、6.1、6.5、6.6_

- [ ] 10. 添加训练场景回归测试并更新最终技术文档
   - 为 `scenario_03_graphics_pipeline_with_db_and_pdb` 验证事件 `3854` 返回 `26` 个资源和指定 `VB/IB/CBV/SRV/Sampler/RTV/Depth/Stencil` 显示名称
   - 为 `scenario_05_compute_pipeline_with_db_and_pdb` 验证事件 `3968` 返回 `15` 个资源和指定 `CBV/SRV/UAV` 显示名称，且不混入图形固定功能资源
   - 在无法实际调用 PIX 或读取大型 `.wpix` 时，使用训练数据库、模拟 C++ export 或受控 fixture 覆盖核心映射与资源解析逻辑
   - 实现完成后更新技术路线文档，记录当前代码逻辑、CLI/MCP 行为、已知限制和测试方式
   - _需求：7.1、7.2、7.3、7.4、7.5、8.1、8.2、8.3、8.4、8.5、10.4、10.5、10.6_
