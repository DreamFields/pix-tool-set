# 用 PIX GUI 的 Global ID / Queue ID 定位
导出的事件列表 CSV 里每行有两个 id，工具都能直接接受，不需要先换算成 `draw_index`：

| 列 | 覆盖范围 | 说明 |
|---|---|---|
| `Global ID` | 全部 2,786 个 action（跨队列），外加 2,548 个非 action 命令 | 在 GUI 里选中一次 draw 就能看到；跨队列唯一 |
| `Queue ID` | 导出 CSV 的每一行都有，22,155 / 22,155 行 | marker（pass 行）只有这个 id；**仅覆盖被导出的那一个队列** |
| `draw_index` | **全部 2,786 个 action** | 工具自己的序号，来自 C++ 导出，跨队列完整 |

`Global ID` 是跨队列的主选择器，推荐从 PIX GUI 抄 id 时使用。它也支持 ExecuteIndirect
展开：如果 GUI 里看到的一个 id 在导出里不存在（PIX 把 ExecuteIndirect 展开成了子 action），
工具会自动解析到父 ExecuteIndirect 并在诊断里说明。

查 pass 的标记行时只能用 `Queue ID`（或 `pass_name`/`pass_index`），因为 marker 没有
Global ID。

```powershell
pix-tool-set find-pass --global-id 5099      # -> CompactTraces WaveOps:1 (compute 队列)
pix-tool-set find-pass --queue-id 18704      # -> pass TileClassificationMark
pix-tool-set pass-bindings --queue-id 18704  # 直接出 shader 绑定
pix-tool-set draw-state --global-id 5367     # DispatchRays，跨队列可达
```

## 多队列边界

**`Queue ID` 只覆盖被导出的那一个 command queue。** 事件列表 CSV 是按队列导出的，本截帧
导出的是 22,155 行的那一个队列；提交到其他队列（典型是 Lumen 的 async compute）的 action
在 CSV 里**根本没有对应行**，因此 `queue_id` 为 `null`。实测：2,786 个 action 里有 **90 个**
（分布在 **72 个 pass**）没有 `Queue ID`。这不是解析缺陷，是导出范围的边界。

`Global ID` 和 `draw_index` 都跨队列覆盖全部 2,786 个 action。推荐用 `Global ID`——它是
PIX GUI 原生显示的 id，不需要先换算：

```powershell
pix-tool-set find-pass --global-id 5099             # compute 队列的 action，queue_id 为 null
pix-tool-set draw-state --global-id 5099            # 绑定数据完整，与队列无关
```

绑定、PSO、资源等数据都来自 C++ 导出，**不受这个边界影响**；缺的只是"用事件列表 id 称呼
它"这一种寻址方式。所有会回报 `queue_id` 的工具在其为 `null` 时都会给出明确说明
（`queue_id_unavailable` 字段或 `diagnostics` 里的一条），而不是丢一个裸 `null`。

## ⚠️ 不要把 PIX GUI 里的 Queue ID 直接抄进来

导出 CSV 的 `Queue ID` 实测**恒等于行号**（`qids == range(0, 22155)`）。这意味着任何小于
行数的整数都能命中*某一行*：从多队列截帧的 PIX GUI 里抄一个 Queue ID 传进来，工具**不会
报错**，而是会安静地返回另一个不相干事件的数据。这是目前最危险的用法。

`Global ID` 和 `Queue ID` 的整数空间重叠（5,424 个整数在两个空间都合法），且 0 个 action
满足 `queue_id == global_id`，所以混用**永远错**且无法自动判别。规则很简单：**从 PIX GUI
抄 id 时一律用 `--global-id`**，`--queue-id` 只用于工具自己输出过的 id。

工具能做的只是在**未命中**时把话说清楚，并区分两种情况：

```powershell
pix-tool-set draw-state --queue-id 99999
#   -> The exported event list has 22155 rows and none carries this id.
pix-tool-set draw-state --queue-id 1611
#   -> Row 1611 is 'IASetVertexBuffers'. But 1611 is a valid Global ID
#      (draw_index=..., pass=...) -- use --global-id 1611 instead.
```

如果传 `--queue-id` 但该整数恰好是合法的 `Global ID`，错误消息会提示改用 `--global-id`。
命中错行时无法检测，所以仍需遵守上面的规则。

`find-pass` 返回里同时给出 `global_id`、`queue_id`、`marker_queue_id`（pass 标记自身的
Queue ID）和 `draw_index`，可用于和 GUI 交叉核对。

## DispatchRays 在 C++ 导出里的表示

PIX 的 C++ 导出里**没有 `->DispatchRays(...)` 调用**。DispatchRays 被写成
`ExecuteIndirect(GetCommandSignature(N))`，其中 command signature 的 `command_type`
是 `DISPATCH_RAYS`。工具通过派生字段 `effective_kind` 暴露这一点：`kind` 仍如实描述
API 调用（`execute_indirect`），但 `effective_kind` 报告 GPU 实际执行的工作（`dispatch_rays`）。

```powershell
pix-tool-set find-draw-calls --effective-kind dispatch_rays
#   -> 列出帧里所有 DispatchRays（本帧 2 个）
```

DispatchRays 的管线由 `SetPipelineState1(StateObject N)` 绑定，而非普通 `SetPipelineState`。
状态对象（state object）目前未建模，所以 `shader-bindings` 对 DispatchRays 返回 `partial`
并在诊断里说明，而不是返回错误的 compute shader。

## 想知道哪些队列被用到了：`queue-attribution`

`queue-attribution` 直接回答"这个帧跑在哪几条命令队列上、每条队列多少 draw、导出的事件
列表覆盖了哪几条"：

```powershell
pix-tool-set queue-attribution
#   -> 3D Queue (GPU 0)       2696 draws（全部有 queue_id）
#      Compute Queue (GPU 0)    90 draws（event list 未覆盖，无 queue_id）
#      Copy Queue (GPU 0)        0 draws
#      event_list_is_complete: false
```

队列归属是从 C++ 导出里的 `ExecuteCommandLists` 调用推导的，所以**对事件列表未覆盖的
队列同样有效**——这正是那 90 个 draw 唯一能回答"我在哪条队列"的地方。带上
`--queue-name` / `--queue-object-id` 还能列出该队列上的 draw（未导出队列的 draw 只能这样
浏览）：

```powershell
pix-tool-set queue-attribution --queue-name Compute --limit 10
```

## 用队列限定参数拒绝跨队列 ID

`draw-state` / `pass-bindings` 等接受 `queue_id` 的工具，支持用
`--queue-name` / `--queue-object-id` 限定。**限定后，不属于该队列的 ID 会被直接拒绝**，
而不是命中别的队列上的错行：

```powershell
pix-tool-set draw-state --queue-id 1049 --queue-name Compute
#   -> error: The id resolved to a draw on a different queue, or to nothing at all.
```

不加限定时的 1049 会命中 3D 队列的某一行（行号即 id，详见上文警告）——这正是限定参数
存在的意义。从 PIX GUI 抄 ID 回来查时，**先跑一次 `queue-attribution` 确认目标事件所在
队列，再带上 `--queue-name`**，就能把"静默命中错行"变成"明确报错"。

## CSV 是否需要改？

不需要加列 —— `Queue ID`、`Parent`、`Name`、`Global ID` 四列已经够了，`Parent` 链就是
权威的 marker 层级。真正值得做的是**另外导出一份带 GPU 计数器的事件列表**（见
[gpu-timing.md](gpu-timing.md)），因为基础 CSV 不含耗时。若要消除上面那 90 个 action 的
`queue_id` 空缺，唯一正确的办法是**把其余队列的事件列表也导出**；用"每队列内序号"之类的
假设去反推 id 已被实测否决（队列 1 按记录调用数算出 102,136 行，而事件列表只有 22,155
行），推算出来的 id 会指向错误的行，比诚实的 `null` 更糟。
