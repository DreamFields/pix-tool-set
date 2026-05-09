# pix-tool-set CLI 测试应有结果（Expected Results）

> 适用范围：`pix-tool-set` CLI 测试套件
> 测试目录：`G:\pix-tool-set\tests\`
> 默认会话：`Tiled`（Tiled.wpix 多队列抓帧）
> 配套文档：`tests\test_cases.md`（用例清单）
> 说明：以下为各测试脚本的**应有结果**——通过标准、关键基线值、期望输出形态。若实际输出与此不符，即视为回归。

---

## 一、通用结果约定

| 项目 | 应有结果 |
|------|---------|
| 退出码 | 通过 = 0；用例断言失败 = 1；会话不存在/事件列表缺失等前置条件不满足 = 2 |
| 输出形态 | 每个 CLI 命令向 stdout 打印**单个 JSON 对象**；成功 `status: "success"`，工具级错误 `status: "error"`（exit 1） |
| 回归准则 | 任何分支不得静默改变 acceptance 基线数字；改动需显式说明 |

---

## 二、验收测试应有结果

### `acceptance_global_id_selector.py`
**通过标准**：所有检查 PASS，`N/N checks passed`，exit 0。

| # | 检查项 | 应有结果 |
|---|--------|---------|
| 1 | engine: find_pass_by_event 解析三个目标 GID | 5099→`CompactTraces WaveOps:1`（pass_index 320）；3893→`TileClassificationMark`（140）；5367→`ReflectionHardwareRayTracingRGS hit-lighting`（347） |
| 2 | engine: resolve_draw 落在正确 action | 5099→draw 2671（ExecuteIndirect）；3893→draw 2475（Dispatch）；5367→draw 2711（ExecuteIndirect） |
| 3 | find-pass --global-id 命名 pass | 与 #1 同名 |
| 4 | draw-state --global-id 到达 action | draw_index 分别为 2671 / 2475 / 2711 |
| 5 | ExecuteIndirect 重定向披露 | 诊断消息含 `5098`（展开源）与 "executeindirect" |
| 6 | queue_id 诚实上报 | 5099→`None`（compute 队列，不得发明）；3893→18704；5367→20648 |
| 7 | 队列仍被命名 | 5099→`Compute Queue (GPU 0)`；3893/5367→`3D Queue (GPU 0)` |
| 8 | kind 保持真实 API 调用 | 5099/5367→`execute_indirect`；3893→`dispatch` |
| 9 | action-info 暴露 GPU 实际执行类型 | 5099→`dispatch`；5367→`dispatch_rays`（effective_kind） |
| 10 | 帧内两个 DispatchRays 可列出 | find-draw-calls effective_kind=dispatch_rays → draw 索引 `[2705, 2711]` |
| 11 | DispatchRays 不继承旧计算 PSO | draw 2711 的 pso_id ≠ 3883（stale）；shader-bindings stages ≠ `["CS"]`；管线不可用时明确说明（消息含 "state object"） |
| 12 | 光线追踪状态对象可达或显式报未建模 | `pipeline_state(3930)` 非 None 或 pso_id 为 None |
| 13 | 第二个 DispatchRays 同样规则 | GID 5312→draw 2705；pass 为 `ReflectionHardwareRayTracingRGS default` |
| 14 | 陷阱 1：range containment 不可决 | 5099 落在 ≥2 个 pass 的 gid 范围内；narrowest 宽度平局 ≥2 |
| 15 | 陷阱 2：nearest preceding action 不作通用回退 | draw-state global_id=5100（WriteBufferImmediate）→ `error`，建议提示 "writebufferimmediate" 或 "not an action" |
| 16 | 陷阱 3：draw-less marker 不产生 pass | BuildRaytracingAccelerationStructure 行存在；其 GID 被拒绝时建议含 "raytracingbuildscene" 或 "no pass"，或定位到真实 marker `RayTracingBuildScene` |
| 17 | 交叉提示：同一整数两个含义 | CSV 行 3893 仍为 `IASetVertexBuffers`；作为 queue_id 传入 → `error`，建议指向 `--global-id` |
| 18 | 无回归 | draw_index 解析全部 action；queue_id 在导出队列上仍解析 |

### `acceptance_queue_baseline.py`
**通过标准**：全部 `ok`，无 FAIL，`BASELINE OK`，exit 0（基线来自 pixrev-dev @ 4c46552，Tiled.wpix）。

| 检查项 | 应有值 |
|--------|--------|
| draw_calls | 2786 |
| draws_without_queue_id | 90 |
| draws_with_queue_id | 2696 |
| passes_without_queue_id | 72 |
| events | 22155 |
| events_with_global_id | 5334 |
| indirect_calls | 187 |
| indirect_empty_bindings | 0 |
| indirect_without_rootsig | 0 |
| descriptor_tables_bound | 3536 |
| descriptor_tables_empty | 0 |
| resource_3026_reads | 19 |
| resource_3026_writes | 2 |
| draws_unreachable_by_index | 0 |
| draws_unreachable_by_global_id | 0 |
| 无合成 Queue ID | 仍有 90 个 draw 诚实上报无 queue_id（若为 0 则 WARN 并需证明来自真实事件列表） |
| queue attribution（plan A 分支） | draws_without_queue_attribution = 0 |

---

## 三、单元测试应有结果

### `test_detect_patches.py`
**应有结果**：pytest 全部用例通过（exit 0）。mock 导出目录中的补丁标记被 `detect_patches` 全部正确发现。

### `test_editledger.py`
**应有结果**：pytest 全部通过。`add_group` / `add_checkpoint` / `compare` / `reset` 行为符合 D3/D4 设计；JSON 账本自包含、可重置。

### `test_shader_scope.py`
**应有结果**：pytest 全部通过。关键不变量：一个 shader 被 N 个 PSO 使用时，`--scope auto` 必须报错（拒绝静默部分修改）。

---

## 四、回归验证脚本应有结果

### 4.1 事件 / 队列 / 选择器

| 脚本 | 应有结果 |
|------|---------|
| `verify_event_list_parse.py` | `PASS: event-list CSV parses cleanly, including quoted names`，exit 0。28 行 over-split 行全部正确合并；每行解析数 = 原始行数；名称不含引号、括号平衡；无荒谬 Global ID；带 Global ID 事件 > 5000；有 parent 事件 > 50% |
| `verify_selector_semantics.py` | `26/26 checks passed`，exit 0。draw 数 2786；无 queue_id 90；CSV 行 22155；qids == range(0, 22155)；draw_index 全可解析（含 90 个无 queue_id）；queue-less action 的 draw-state 诊断含 "no queue id"/"does not cover that queue"/"cannot be derived" 并给 draw_index；越界 queue_id 报错且提示 "none carries this id" + "row number"；in-range 非 action 报错并命名行、提示 "is not an action"；queue-less pass 经 find-pass 给出 draw_index 与 queue_id_unavailable；locate-event(pass_name=CompactTraces WaveOps)→draw 2671 ExecuteIndirect；find-draw-calls 返回 (2671, 2676)；既有 queue_id 路径 100% 仍解析；find-pass/draw-state --global-id 生效 |
| `verify_gui_id_lookup.py` | 11/11 PASS。TileClassificationMark dispatch（GID 3893, Queue ID 18704）可驱动 pass 工具 |
| `verify_global_id_uniqueness.py` | 全部 PASS。决定性测试 #4：queue-less action 的 Global ID 不与 CSV 中描述不同 action 的行冲突（无碰撞） |
| `verify_queue_attribution.py` | 全部 PASS。90 个无 queue_id 的 draw 队列可从 C++ 导出确定；无合成 queue_id |
| `verify_probe_claims.py` | 两个主张独立复核成立：①5099 = ExecuteIndirect 5098 的展开子项，且 187/187 个未归属 Global ID 恰为 ExecuteIndirect+1；②3D 队列 5190 个 Global ID 与 CSV 双向一致（无单向泄漏） |

### 4.2 ExecuteIndirect 绑定

| 脚本 | 应有结果 |
|------|---------|
| `verify_execute_indirect_bindings.py` | `PASS: ExecuteIndirect binding snapshots are complete`，exit 0。命令签名全部有 command_type；187 个 ExecuteIndirect 全部解析签名；compute 类无空绑定、无缺 root signature；图形类读图形集；command signature 与 PSO 分类处处一致；参考例 GID 5098：launches_compute、TYPE_DISPATCH、sig 3346、PSO 3854、rootsig 3005、表 @152869+152871、root CBV 资源 2956、SRV ≥3、UAV ≥2、TraceHit（res 785）在 SRV 中、每个 SRV 都记录该 draw 到 read_draws；descriptor 覆盖率 ≥95% |

### 4.3 Shader 编辑 / 热替换

| 脚本 | 应有结果 |
|------|---------|
| `verify_shader_edit.py` | 51 项全部 PASS（`51 passed, 0 failed`），exit 0。阶段 1 begin 成功：entry=RayTracingBuildLightGridCS、编译参数含 `-T` 与 `cs_6_6`、editable_hlsl/args/pristine 三文件写出、5 个原始绑定；阶段 2 往返：编译成功、bindings identical、entry_and_threads_match、容器 signed、经 dxc 编译、无 --patch 不产生 patch；阶段 3 真实编辑：编译成功、槽位兼容、args 读自 sidecar（args.txt）、shader_hash 变化（≠3e92071c09a522dfa4e259e557334efc）；阶段 4 语法错误：status=error、code=shader_compile_failed、dxc 文本透传（含 "error:"、行号 :N:N:）、无 NUL 泄漏；阶段 5 绑定变更：status=partial、identical=False、未 patch、RWLightGrid 从 u0 位移、诊断含 "binding"；阶段 6 --patch：patch 函数 CreatePipelineState_3241、替换 CS、bytecode 写出、备份创建、源码含 marker "pix-tool-set: CS replaced"、Helpers.h 有 ReadFileBytes、顺序 blob 读（Read(data, 12491)）与记录赋值（&data[offset]), 16436）保留、override 紧随赋值、二次 patch → error + already_patched、恢复后 marker 消失 |
| `verify_shader_edit_diff.py` | 36/36 PASS。patch 切换异常安全（强制失败后编辑不丢失；.hold 遗留可恢复）；缺失/inert patch 在重放前被拒且错误指明修复方式；差值数学正确、共享显示范围；RWNormalTexture 实测均值与手工记录一致 |
| `verify_pixel_debug.py` | 9 步全部 PASS（需 Tiled.wpix + PIX runtime + 已 session-open）。D1 sibling_psos 正确；--scope auto 有 sibling 时报错；replay-edits 列出补丁；replay-reset 回滚；replay-baseline-check 检出补丁；pixel-value-history 返回 draw 序历史；trace-downstream 返回影响链；shader-edit-diff --list-checkpoints 返回空列表；frame-replay-dump schema 合法 |
| `verify_export_cleanliness.py` | 全部 PASS。三种注入机制后 clean 判定正确（不再出现 16 个注入 sample call 仍报 clean:true 的回归） |

### 4.4 PDB / Shader 源码恢复

| 脚本 | 应有结果 |
|------|---------|
| `verify_pdb_coverage.py` | 输出覆盖率统计；退出 0（覆盖率达标） |
| `verify_pdb_dirs_from_session.py` | 全部 PASS。session 存储的 PDB 目录在无 --pdb-dirs 时被 active session 命中并使用 |
| `verify_pdb_end_to_end.py` | 全部 PASS。PIX GUI Queue ID → 经引擎 PDB 恢复真实 HLSL |
| `verify_pdb_source.py` | 全部 PASS。UE5 shader PDB 目录 HLSL 恢复成功 |
| `verify_pass_shader_source.py` | 全部 PASS。Queue ID 18461 的 shader 源码可检视（带 PDB 目录时恢复 HLSL） |

### 4.5 资源 / 绑定 / 常量缓冲

| 脚本 | 应有结果 |
|------|---------|
| `verify_pass_bindings.py` | 全部 PASS。3 步配方与新一键工具结果一致 |
| `verify_binding_labels.py` | 全部 PASS。GBufferA（资源 756）Binding 列与 PIX GUI 截图一致；GUI Global ID（ExecuteIndirect 用展开子项 = 我们的 id + 1）可匹配 |
| `verify_bound_values.py` | 全部 PASS。pass shader 配置值读取正确 |
| `verify_cbv_register_match.py` | 全部 PASS。每个 root 参数一个布局；根 CBV 与 cbuffer 寄存器全帧匹配（无每缓冲三答案） |
| `verify_mip_subresource_bindings.py` | 全部 PASS。GID 3167 的 5 个 CS 绑定如实上报；双 UAV（mips 8/9）不再被误判为 filler；sampler 表不再降级 partial |
| `verify_reflection_columns.py` | 全部 PASS。宽列溢出（unorm_f32 粘连 UAV）后绑定表单元格不错位 |
| `verify_clean_cbuffer.py` | 全部 PASS。找到 cbuffer 页未被重写的 pass（证明值解码可用） |
| `verify_scene_cbuffer.py` | 全部 PASS。PS 'Scene' 等命名 cbuffer 值读取成功 |
| `verify_scene_against_pix_gui.py` | 全部 PASS。77 行（offset 0→316）逐字段与 PIX GUI 一致：整数相等、浮点按 PIX 精度、向量逐元素匹配 |
| `verify_against_pix_gui.py` | 全部 PASS。Queue ID 18385 常量缓冲与 PIX GUI 精确一致 |

### 4.6 UAV / 重放回读

| 脚本 | 应有结果 |
|------|---------|
| `verify_read_uav_decode.py` | 全部 PASS。R10G10B10A2_UNORM 解码出 4 通道（不再标灰）；6144 字节 pitch 中 6128 字节像素正确提取 |
| `verify_replay_render.py` | 全部 PASS。真实 magenta 捕获与真实空白捕获可区分（不出现两空白页判 identical）；fixtures 缺失时按需生成 |
| `verify_replay_values.py` | 全部 PASS。GPU 重放读取真实像素值，两个硬限制被展示 |
| `verify_viewport_blank.py` | 全部 PASS。UI-over-black-viewport（replay_baseline-18704_...1280x720.png）判定为"内容局限于部分帧"；RWNormalTexture_BEFORE/_AFTER 保持"内容铺满"；合成用例全部符合 |

### 4.7 深度 / 纹理导出

| 脚本 | 应有结果 |
|------|---------|
| `verify_depth_content.py` | 全部 PASS。定位到含几何的深度并读取层级 |
| `verify_depth_export.py` | 全部 PASS。pass 深度缓冲导出磁盘成功 |
| `verify_depth_two_paths.py` | 全部 PASS。Path A（read-resource-texture）与 Path B（save-render-target）均产出可查看结果且数字不同、来源标注清晰 |
| `verify_dds_formats.py` | 全部 PASS。R11G11B10 小浮点重建正确（1.0 = 指数域 15 零尾数） |
| `verify_lightgrid_export.py` | 全部 PASS。RWLightGrid 切片导出成功；越界切片被拒绝 |
| `verify_resource_stream.py` | 全部 PASS。resources.bin 每个 blob 可寻址、可解码 |

### 4.8 像素调试 / 历史

| 脚本 | 应有结果 |
|------|---------|
| `verify_pixel_history_gui.py` | 全部 PASS。像素 (810,284) GBufferA（GID 5417）历史恰好四行：Recreation #1、Clear、Draw（Failed depth/stencil test）、Draw（写入） |
| `verify_pixel_value_history.py` | 无重放：纯解码/候选集/深度证据/一致性/诚实 "value unavailable" 路径全 PASS；有重放：GID 0/3828/3851/3854 四行语义断言全 PASS（R:0.4995(0x1FF) G:1.0000(0x3FF) B:0.4995(0x1FF) A:0.3333(0x1) 等原始整数字段与 verdict 常量）；实测行缺重放时输出 SKIPPED 且 exit 非 0 |

### 4.9 数值 / 时间线 / 状态

| 脚本 | 应有结果 |
|------|---------|
| `verify_value_reads.py` | 全部 PASS。不同 z 值产生不同字节；越界 z 被拒绝（报真实绑定）而非 clamp；体积短字节明确报告 |
| `verify_value_coverage.py` | 全部 PASS。全帧值读取覆盖率达标 |
| `verify_pass_values.py` | 全部 PASS。单 pass 所有绑定资源值端到端读取成功 |
| `verify_pass_cost_measured.py` | 全部 PASS。pass-cost 报告实测 GPU 时间（非仅估算） |
| `verify_timing.py` | 全部 PASS。实测 GPU 计时正确接入工具 |
| `verify_activity.py` | 7 项全部 PASS。CLI 与 call_tool 调用均记录（含失败）；字节游标只交付新条目且不重复；payload 可检索、id 遍历被拒；摘要标记明确；快照自包含；可关闭且不破坏包裹调用 |
| `verify_resource_history_gui.py` | 全部 PASS。GBufferA 25 行与 PIX GUI 截图一致（Global ID/Name/Binding/Read-Write/States）；按 gui_global_id 匹配 |
| `verify_frame_snapshots.py` | 全部 PASS。每次编辑的快照可区分、不重编号；两 dump 不可混淆 |
| `verify_live.py` | 每个注册工具 `status: success`（遍历全部工具，无 error） |
| `verify_table_fix.py` | 全部 PASS。root[0] 展开不再全是 rid=896 filler；在下一绑定表基址停止；拒绝视图类型矛盾的槽 |

---

## 五、辅助脚本应有结果

| 脚本 | 应有结果 |
|------|---------|
| `check_coverage.py` | `RESULT: PASS`，exit 0。requirement 需求项全部映射到已注册工具（缺失 0、未映射 0）；schema 零问题（summary/参数描述/类型/returns 齐全）；CLI smoke 输出合法 JSON（list-tools --brief、describe frame-stats 均 status 合法） |
| `probe_queue_ownership.py` | 输出队列数、命令列表归属、90 个无 queue_id draw 是否全部位于事件列表未覆盖队列；无命令列表被提交到多个队列（映射无歧义） |
| `show_resource_timeline.py` | 输出单个资源完整读写时间线表格（可读格式） |

---

## 六、历史回归基准（已确认的通过记录）

| 日期 | 结果 |
|------|------|
| 2026-08-05 | 15 个回归脚本全绿（checked 15, failing: 0），工具数 71→73 |
| 2026-08-06 | ExecuteIndirect 修复后：134 个空绑定→0；view 826→3000；ScreenProbeSceneDepth 读写历史恢复（19 读 / 2 写） |
| 2026-08-07 | 方案 B 落地后 6 套验收全绿（baseline、selector_semantics、execute_indirect、event_list_parse、gui_id_lookup、pass_bindings） |
| 当前基线 | draw_calls 2786、无 queue_id 90、事件 22155、Global ID 5334、ExecuteIndirect 187 空绑定 0、descriptor 表 3536 空表 0、资源 3026 读 19 写 2 |
