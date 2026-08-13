# pix-tool-set CLI 测试用例清单

> 适用范围：`pix-tool-set`（PIX `.wpix` GPU 抓帧脚本化分析工具）
> 测试目录：`G:\pix-tool-set\tests\`
> 默认会话：`Tiled`（Tiled.wpix 多队列抓帧）
> 相关文档：`tests\expected_results.md`（本套用例的应有结果）

---

## 一、验收测试（Acceptance）

### 1. `acceptance_global_id_selector.py`
- **类型**：功能验收（实现前编写，固定基线）
- **目标**：让 Global ID 成为可接受的事件选择器，覆盖三类不同失败形态的 ID
- **被测对象**：`find-pass`、`draw-state`、`action-info`、`find-draw-calls`、`shader-bindings`、引擎解析层
- **关键用例**：
  - Global ID 5099 → pass `CompactTraces WaveOps:1`（ExecuteIndirect 展开子项，compute 队列，CSV 无行）
  - Global ID 3893 → pass `TileClassificationMark`（3D 队列直接 Dispatch）
  - Global ID 5367 → pass `ReflectionHardwareRayTracingRGS hit-lighting`（DispatchRays，N-1 展开规则）
  - 第二个 DispatchRays：Global ID 5312 → `ReflectionHardwareRayTracingRGS default`（无管线分支）
  - 陷阱检查：range containment 不可决、nearest preceding action 不可作通用回退、SetPipelineState1 不继承旧 PSO
  - 混淆 ID 检查：3893 既是 Global ID 又是 CSV 行号（IASetVertexBuffers）的交叉提示
- **运行方式**：`python tests/acceptance_global_id_selector.py [session-name]`
- **期望**：全部检查 PASS，exit 0

### 2. `acceptance_queue_baseline.py`
- **类型**：数据基线验收（实现前编写，防回归）
- **目标**：固化多队列 Queue ID 工作的基线数字，任何分支不得静默回归
- **被测对象**：引擎捕获模型（draw/event/indirect/descriptor/资源使用）
- **关键用例**（基线值见 expected_results.md）：
  - draw call 清单：总数 / 无 queue_id 数 / 有 queue_id 数 / 无 queue_id 的 pass 数
  - 事件清单：总行数 / 带 Global ID 数
  - ExecuteIndirect：总数 / 空绑定数 / 无 root signature 数
  - descriptor 覆盖：绑定表数 / 空表数
  - ScreenProbeSceneDepth（资源 3026）：读次数 / 写次数
  - 全部 draw 可被 draw_index 与 global_id 寻址
  - 无合成 Queue ID（诚实上报缺失）
  - 队列归属分布（仅 plan A 分支）
- **运行方式**：`python tests/acceptance_queue_baseline.py [session-name]`
- **期望**：`BASELINE OK`，exit 0

---

## 二、单元测试（pytest / 无捕获依赖）

### 3. `test_detect_patches.py`
- **类型**：单元测试（D5 基线门禁）
- **目标**：验证 `detect_patches` 与基线指纹逻辑，无需 PIX 抓帧
- **关键用例**：构造 mock 导出目录（含 shader-edit-apply 写入的标记），验证 detect_patches 能正确发现补丁
- **期望**：全部通过

### 4. `test_editledger.py`
- **类型**：单元测试（D3 账本记账 / D4 重置语义）
- **目标**：验证 EditLedger 的 `add_group` / `add_checkpoint` / `compare` / `reset`
- **关键用例**：纯 Python 逻辑，JSON 账本文件置于临时目录，自包含快速
- **期望**：全部通过

### 5. `test_shader_scope.py`
- **类型**：单元测试（D1 shader 作用域解析）
- **目标**：验证 `shader-edit-apply --scope` 的决定逻辑
- **关键用例**：一个 shader 被 N 个 PSO 使用时，`--scope auto`（默认）必须报错——静默部分修改是最昂贵的失败模式
- **期望**：全部通过

---

## 三、回归验证脚本（verify_*.py）

### 3.1 事件 / 队列 / 选择器

| 脚本 | 验证目标 | 关键断言点 |
|------|---------|-----------|
| `verify_event_list_parse.py` | 事件列表 CSV 解析（含引号逗号名） | 28 行 over-split 行正确合并；名称不截断；括号平衡；Global ID 列不错位；树链接存活 |
| `verify_selector_semantics.py` | 各事件选择器在多队列抓帧上的能力边界 | draw_index 可寻址全部 action（含 90 个无 queue_id）；Queue ID 越界/非 action 报错并提示行号陷阱；queue-less pass 诊断；Global ID 新选择器可用 |
| `verify_gui_id_lookup.py` | PIX GUI ID 直接驱动工具 | TileClassificationMark dispatch → Global ID 3893, Queue ID 18704 |
| `verify_global_id_uniqueness.py` | Global ID 是否全局唯一 | C++ 导出注释 vs CSV 列两源比对；queue-less action 的 Global ID 不与 CSV 冲突 |
| `verify_queue_attribution.py` | 多队列命令队列归属 | 每个 draw 的队列可从 C++ 导出得知；90 个缺失 queue_id 的 draw 未被合成 ID 掩盖 |
| `verify_probe_claims.py` | 独立复核 probe 代理两大主张 | Claim1: 5099 是 5098 的展开子项、187/187 的 ExecuteIndirect+1 规则；Claim2: 5190 个 3D 队列 Global ID 与 CSV 双向一致 |

### 3.2 ExecuteIndirect 绑定

| 脚本 | 验证目标 | 关键断言点 |
|------|---------|-----------|
| `verify_execute_indirect_bindings.py` | ExecuteIndirect 绑定快照完整性 | 命令签名全部解析；每个 indirect 解析签名；dispatch 类无空绑定/无缺 root sig；图形类仍读图形集；分类与绑定 PSO 一致；参考例（GID 5098, sig 3346, PSO 3854, rootsig 3005, 表 @152869+152871, root CBV 2956）；ScreenProbeSceneDepth 回到读历史；descriptor 覆盖率 ≥95% |

### 3.3 Shader 编辑 / 热替换

| 脚本 | 验证目标 | 关键断言点 |
|------|---------|-----------|
| `verify_shader_edit.py` | PIX Debug 面板 "Apply" 的脚本化等价 | 6 阶段：begin 恢复 HLSL+编译参数；未修改往返保持绑定签名；真实编辑可编译且槽位兼容；语法错误透传 DXC 诊断；绑定变更拒绝（partial）；--patch 改写导出并留可恢复备份（含顺序 blob 读、双重 patch 拒绝） |
| `verify_shader_edit_diff.py` | shader-edit-diff 周边逻辑 | patch 切换异常安全（含 .hold 遗留模拟）；缺失/inert patch 在重放前拒绝；差值数学正确、共享显示范围；数值复现 RWNormalTexture 手工真值 |
| `verify_pixel_debug.py` | pixel-debug 与 impact-tracking 集成 | 9 步：sibling_psos(D1)；--scope auto 拒绝(D1)；replay-edits 列补丁(D3)；replay-reset 回滚(D4)；replay-baseline-check 检测(D5)；pixel-value-history 按 draw 排序(P0)；trace-downstream 影响链(B)；shader-edit-diff 空 checkpoint；frame-replay-dump schema(D2) |
| `verify_export_cleanliness.py` | replay-reset 清洁性 | 三种注入机制（shader-edit-apply、read-uav probe、pixel-history-replay sampler）后 clean 判定正确；基于合成导出副本 |

### 3.4 PDB / Shader 源码恢复

| 脚本 | 验证目标 | 关键断言点 |
|------|---------|-----------|
| `verify_pdb_coverage.py` | 抓帧 shader 可还原真实 HLSL 的比例 | 覆盖率统计 |
| `verify_pdb_dirs_from_session.py` | session 存储的 PDB 目录被实际使用 | `session-set-pdb-dirs` 落盘目录在无 --session/--pdb-dirs 时命中 active session |
| `verify_pdb_end_to_end.py` | PIX GUI Queue ID → 真实 HLSL 端到端 | 经引擎 shader PDB 恢复 |
| `verify_pdb_source.py` | UE5 shader PDB 目录 HLSL 恢复 | 恢复结果校验 |
| `verify_pass_shader_source.py` | 按 Queue ID 查 pass 并检视 shader 源码 | Queue ID = 18461（Tiled.wpix） |

### 3.5 资源 / 绑定 / 常量缓冲

| 脚本 | 验证目标 | 关键断言点 |
|------|---------|-----------|
| `verify_pass_bindings.py` | 报告 3 步配方 vs 新一键工具 | 结果一致 |
| `verify_binding_labels.py` | Binding 列与 PIX GUI 截图比对 | GBufferA（资源 756）真值；GUI Global ID 作 key，ExecuteIndirect 用展开子项 id（= 我们的 id + 1） |
| `verify_bound_values.py` | 读取 pass shader 配置的实际值 | 值读取正确 |
| `verify_cbv_register_match.py` | root CBV 与 cbuffer 寄存器全帧匹配 | 每个 root 参数一个布局，按 root signature 声明的 shader 寄存器连接 |
| `verify_mip_subresource_bindings.py` | mip 链写入不被误报为 filler | 参考例 GID 3167 `ReduceHZB(mips=[8;9] Furthest) 4x2`：5 个 CS 绑定、双 UAV 同纹理不同 mip 不再降级 |
| `verify_reflection_columns.py` | dxc 绑定表宽列溢出 | `unorm_f32`（9 字符）溢出 7 字符 Format 列后各单元格不错位 |
| `verify_clean_cbuffer.py` | 找到 cbuffer 页未被重写的 pass | 证明值解码工作 |
| `verify_scene_cbuffer.py` | 读取命名 cbuffer 值 | 例：PS 'Scene' 缓冲 |
| `verify_scene_against_pix_gui.py` | PS 'Scene' cbuffer 与 PIX GUI 逐字段比对 | Queue ID 17765（Emit Scene Depth/Resolve/Velocity），77 行 offset 0→316；按 offset 键控，整数相等、浮点按 PIX 精度、向量逐元素匹配 |
| `verify_against_pix_gui.py` | 与 PIX GUI 常量缓冲视图逐字段比对 | Queue ID 18385（RayTracingBuildInstanceBuffer），精确比较 |

### 3.6 UAV / 重放回读

| 脚本 | 验证目标 | 关键断言点 |
|------|---------|-----------|
| `verify_read_uav_decode.py` | read-uav 解码路径 | 位打包格式解码全部通道（R10G10B10A2 四通道）；行 pitch 填充丢弃（6144 字节 pitch 承载 6128 像素字节） |
| `verify_replay_render.py` | 重放渲染帧捕获与查看器展示 | 真实 magenta 捕获 vs 空白捕获 fixtures 区分；不因两张空白页"identical"误判 |
| `verify_replay_values.py` | GPU 重放读取真实像素值 | 展示两个硬限制 |
| `verify_viewport_blank.py` | 区域级空白判定（真实抓帧） | UI-over-black-viewport 捕获判定为"内容局限于部分帧"；RWNormalTexture 真 UAV 导出保持"内容铺满"；合成用例覆盖形状 |

### 3.7 深度 / 纹理导出

| 脚本 | 验证目标 | 关键断言点 |
|------|---------|-----------|
| `verify_depth_content.py` | 定位含几何的深度并读取层级 | 深度内容定位与读取 |
| `verify_depth_export.py` | 从抓帧读 pass 深度缓冲并导出磁盘 | 导出成功 |
| `verify_depth_two_paths.py` | 两条深度获取路径对比 | Path A `read-resource-texture`（无重放，初始内容）vs Path B `save-render-target`（GPU 重放，实际内容）；两者数字不同须并排可见 |
| `verify_dds_formats.py` | R11G11B10 小浮点重建 | 11 位浮点：6 尾数位 + 5 指数位（bias 15），1.0 = 指数域 15 零尾数 |
| `verify_lightgrid_export.py` | RWLightGrid 切片导出 | 导出成功；越界切片被拒绝 |
| `verify_resource_stream.py` | resources.bin 全索引 | 每个 blob 可寻址、可解码 |

### 3.8 像素调试 / 历史

| 脚本 | 验证目标 | 关键断言点 |
|------|---------|-----------|
| `verify_pixel_history_gui.py` | pixel-history 视图 vs PIX Pixel History 面板 | 像素 (810,284) GBufferA（资源 756）GID 5417；四行历史（Recreation/Clear/Draw/Failed depth）；断言哪些事件在历史中及类型 |
| `verify_pixel_value_history.py` | pixel-value-history / pixel-history-replay vs PIX GUI 基线 | 四行基线（GID 0/3828/3851/3854）；断言基于语义（原始整数字段+verdict 常量）而非措辞；--no-replay 纯解码检查恒跑；实测行无重放则 SKIPPED 且非零退出 |

### 3.9 数值 / 时间线 / 状态

| 脚本 | 验证目标 | 关键断言点 |
|------|---------|-----------|
| `verify_value_reads.py` | 缓冲/2D 纹理/Texture3D z 切片值读取 | 不同 z 值产生不同字节；越界 z 拒绝而非 clamp；体积短字节明确报告 |
| `verify_value_coverage.py` | 全帧值读取覆盖率 | 覆盖率统计 |
| `verify_pass_values.py` | 单 pass 所有绑定资源值端到端读取 | 读取成功 |
| `verify_pass_cost_measured.py` | pass-cost 报告实测 GPU 时间而非估算 | 实测时间存在 |
| `verify_timing.py` | 实测 GPU 计时接入工具 | 计时数据正确 |
| `verify_activity.py` | activity 日志与查看器 | 7 项：CLI+call_tool 均记录；失败也记录；字节游标只交付新条目；payload 可检索且 id 遍历被拒；摘要标记清晰；快照自包含；可关闭且不破坏调用 |
| `verify_resource_history_gui.py` | resource-history 视图 vs PIX GUI 截图 | GBufferA 25 行真值（Global ID/Name/Binding/Read-Write/States）；GUI 号按 gui_global_id 匹配 |
| `verify_frame_snapshots.py` | 每次编辑的帧快照可区分且不重编号 | 基于合成导出目录；两 dump 不可混淆 |
| `verify_live.py` | 真实抓帧导出的端到端 | 注册 session 指向现有 pixtool 导出，遍历每个注册工具并报告状态 |
| `verify_table_fix.py` | descriptor 表展开修复（报告 2.3 节） | root[0] 展开不再全是 rid=896 filler；在下一绑定表基址处停止；拒绝视图类型矛盾的槽 |

---

## 四、辅助 / 研究脚本

| 脚本 | 用途 |
|------|------|
| `check_coverage.py` | 最终验收：Doc/requirement.md 需求项被注册工具全覆盖；schema 健全性（summary/参数描述/类型/returns）；CLI smoke（list-tools/describe 输出合法 JSON） |
| `probe_queue_ownership.py` | 研究：能否从 C++ 导出本地推导队列归属（commandLists[] 提交 + GetCommandQueue）；检查队列数、命令列表归属、是否出现同列表多队列提交 |
| `show_resource_timeline.py` | 工具：打印单个资源的完整读写时间线可读表格 |

---

## 五、覆盖主题速查

| 主题域 | 对应脚本 |
|--------|---------|
| 多队列 / 选择器 | acceptance_*、verify_selector_semantics、verify_queue_attribution、verify_global_id_uniqueness、verify_gui_id_lookup、probe_queue_ownership |
| ExecuteIndirect | verify_execute_indirect_bindings、acceptance_queue_baseline |
| Shader 编辑 / 补丁 | verify_shader_edit、verify_shader_edit_diff、verify_pixel_debug、verify_export_cleanliness、verify_frame_snapshots、test_detect_patches、test_editledger、test_shader_scope |
| PDB / 源码恢复 | verify_pdb_*、verify_pass_shader_source |
| 绑定 / cbuffer | verify_pass_bindings、verify_binding_labels、verify_bound_values、verify_cbv_register_match、verify_mip_subresource_bindings、verify_reflection_columns、verify_clean_cbuffer、verify_scene_cbuffer、verify_scene_against_pix_gui、verify_against_pix_gui |
| UAV / 重放 | verify_read_uav_decode、verify_replay_render、verify_replay_values、verify_viewport_blank |
| 深度 / 纹理 | verify_depth_*、verify_dds_formats、verify_lightgrid_export、verify_resource_stream |
| 像素历史 | verify_pixel_history_gui、verify_pixel_value_history |
| 数值 / 计时 / 日志 | verify_value_reads、verify_value_coverage、verify_pass_values、verify_pass_cost_measured、verify_timing、verify_activity、verify_resource_history_gui、verify_live、verify_table_fix |
