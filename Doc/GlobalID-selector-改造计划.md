# Global ID 选择器改造计划

## 目标

让 PIX 的 Global ID 成为工具层可接受的选择器，覆盖三个验收目标：

| Global ID | 期望 pass | 形态 |
|---|---|---|
| 5099 | `CompactTraces WaveOps:1` | compute 队列，CSV 无行，是 gid=5098 ExecuteIndirect 的展开 |
| 3893 | `TileClassificationMark` | 3D 队列 Dispatch，引擎已通，只差工具层收参数 |
| 5367 | `ReflectionHardwareRayTracingRGS hit-lighting` | 3D 队列 DispatchRays，CSV 有行，是 gid=5366 ExecuteIndirect 的展开 |

验收测试：`tests/acceptance_global_id_selector.py`，当前 16/44 红，目标 44/44。

## 前置事实（全部本次实测，Tiled.wpix，PIX 2603.25）

- Global ID 是帧内全局递增计数器，跨队列零碰撞（`tests/verify_global_id_uniqueness.py` 24/24 通过）。
- 2786 个 action 全部有 global_id；queue_id 只覆盖 2696（90 个 async compute 无）。
- 不连续：1..6042 中 221 个空洞（两个来源都没有）。
- 不止 action：query/barrier/clear/copy/sync 也有 gid；**marker 没有**（564/564 为 None）→ queue_id 不能删。
- 导出里 DispatchRays 写成 `ExecuteIndirect(GetCommandSignature(3890))`，command_type=DISPATCH_RAYS，**没有 `->DispatchRays(...)` 调用**。
- 状态对象（`SetPipelineState1`）完全未建模；解析器退而报之前的 compute PSO → draw 2711 的 `shader-bindings` 返回错误的 `stages=['CS']`。
- `find_pass_by_event` 只查 CSV → 90 个无 queue_id 的 action 全部返回 None。
- `passes` 由 draw_calls 按 marker_path 分桶 → draw-less marker（如 `RayTracingBuildScene`）不产生 pass。
- id 空间重叠：queue_id 0..22154、global_id 1..6042，5424 个整数两者都合法，0 个 action 满足 queue_id==global_id → 混用永远错且无法自动判别。

## 三个陷阱（必须堵死，都有实测反证）

1. **区间包含法不可用**：5099 同时落在 3 个 pass 的 gid 区间内，最窄的两个宽度相同（29）。
2. **"最近前一个 action"不可作通用回退**：221 个空洞里只有 43 个前一位是 ExecuteIndirect。
3. **SetPipelineState1 跨越**：draw 2711 的真实管线是 state object 3930，解析器报的 3883 在 99 行之前，是 compute PSO。

## 阶段 0：堵住 DispatchRays 的错 shader（必须最先做）

**理由**：这是唯一一个"改造本身会放大危害"的缺陷。其余阶段把不可达变可达；这一条把"不可达因而无害"变成"可达且撒谎"。

- **0.1** `cppparse.CommandListParser` 识别 `SetPipelineState1(GetStateObject(N))`，在 state 里记 `state_object_id` 并**清除** `pso_id`。宁可 `pso_id=None` 也不能报 3883。
- **0.2** `parse_state_objects(root)` 解析 `CreatePSOs.cpp` 的 `CreateStateObject`（82 个），建出 `{state_object_id, shader_hashes, root_signature_id}`。若一期做不到，`draw-state`/`shader-bindings` 在 `state_object_id` 非空而未建模时必须返回 `partial` + 诊断，不得返回错误的 compute shader。
- **0.3** 验收：`shader-bindings --draw-index 2711` 不得返回 `stages=['CS']`。

## 阶段 1：全量 gid 索引 + effective_kind

- **1.1** `cppparse.parse_global_id_index(root) -> dict[int, GlobalIdEntry]`，覆盖全部 5539 个 gid（不只 2786 个 DrawCall）。每项：`{global_id, api, command_list_id, source_file, source_line, marker_path, draw_index|None, command_signature_id|None}`。
- **1.2** `Capture.global_id_index` cached_property + `Capture.command_by_global_id(gid)`。
- **1.3** 索引项加 `indirect_command_type`，新增 `effective_kind` 派生：`Dispatch→dispatch`、`ExecuteIndirect+DISPATCH_RAYS→dispatch_rays` 等。**不动 `kind`**。
- **1.4** `DrawCall.effective_kind` property + 写入 `to_dict()`；`find-draw-calls`/`list-actions` 支持 `effective_kind` 过滤。
- **1.5** 修 `cppparse.py:1439-1446` 非 draw 分支不清 `pending_global_id` 的潜在 bug。

## 阶段 2：pass 查找脱离 CSV

- **2.1** `find_pass_by_event` 增加非 CSV 路径：`global_id → global_id_index[gid].marker_path → pass entry`（精确 marker_path 相等匹配，**不做区间**）。
- **2.2** 新增 `Capture.pass_for_draw(draw)`。
- **2.3** draw-less marker：`find_pass_by_event` 返回"该 marker 存在但不含 action"，附 marker_path，而非 None、更不得就近匹配。
- **2.4** 两条路径（CSV / marker_path）必须给出同一个 pass entry。加断言：对全部 action，两路径结果一致或其中一条为 None，**不得冲突**。
- **2.5** 明令禁止用 `first_global_id..last_global_id` 做包含判断，在 `passes` docstring 写明。

## 阶段 3：ExecuteIndirect N−1 展开规则

- **3.1** `resolve_draw(global_id=N)` 未命中时，严格判定 `N-1` 是否为索引里的 ExecuteIndirect（用 `N-1` 而非"最近前一个"）。命中则解析到该 ExecuteIndirect，并**必须**在 `diagnostics` 里声明"N 是 N-1 的 ExecuteIndirect 展开出的子 action"。
- **3.2** 其余空洞诚实报 not-found，区分三类文案：命中索引但非 action / 落在空洞且 N-1 非 ExecuteIndirect / 超出范围。
- **3.3** 实现时统计"每个 ExecuteIndirect 在 CSV 里的子行数"，若任一 >1 就改为区间规则并重测。

## 阶段 4：工具层暴露 global_id

- **4.1** `_common.py`：`DRAW_SELECTOR`/`PASS_SELECTOR` 加 `global_id`；`draw_selector_args` **必须**加 key（漏了就是静默吞参数）。
- **4.2** `resolve_draw`/`resolve_pass` 加 global_id 分支与三类专用错误。
- **4.3** `pass_binding_tools.py:381` 错误消息已写 `name/global_id/queue_id` 却不接受 —— 改成真接受。
- **4.4** 多选择器同时传入且矛盾时报错，不沿用"取第一个成功的"。

## 阶段 5：交叉提示

- **5.1** `queue_id` 失败或落在非 action 行时，反查该整数是否是合法 global_id，命中就明说改用 `--global-id`。反向同理。
- **5.2** 3893 作 queue_id 命中第 3893 行 `IASetVertexBuffers` → 提示"3893 是合法 Global ID（Dispatch，pass TileClassificationMark），你要的大概是 --global-id 3893"。
- **5.3** README:222-223 的 3893 例子必须换（同一整数在文档里既正例又反例）。

## 阶段 6：测试与文档

- **6.1** `tests/acceptance_global_id_selector.py` 打到 44/44。
- **6.2** 反转 `verify_selector_semantics.py:249-255` §9。
- **6.3** `acceptance_queue_baseline.py` 加 `draws_unreachable_by_global_id == 0`。
- **6.4** 文档：`README.md:173-230`（含 :222-223 的 3893 例子）、`Doc/ai-client-guide.md:130-134`、`Doc/Tiled-wpix-分析报告.md:210,314`、`Doc/pix-tool-set-UAV-shader-hotswap-pitfalls.md:83`、`skills/pix-shader-hotswap/SKILL.md:21`。
- **6.5** 新增一节"DispatchRays 在 C++ 导出里是 ExecuteIndirect + DISPATCH_RAYS command signature"。

## 风险

- DispatchRays 样本只有 2 个，都在同一 pass 家族、同一 command signature、同一 SetPipelineState1 模式。inline raytracing / 直接 DispatchRays 路径未验证。
- 阶段 0.2 可能做不完（82 个 state object，D3D12_STATE_OBJECT_DESC 结构复杂）。超预算则退到降级分支（报 partial），不可退回现状。
- draw-less marker 处理会改变 `find-pass` 返回形状（多一种状态），调用方需能区分。
- >2 队列、multi-GPU、多帧截帧下 Global ID 是否跨帧重置，未测。
