# 实测 GPU 耗时（可选，一次性）
`pass-cost` 默认用工作量模型估算。跑一次 `export-timing` 就能换成**真实测量值**：

```powershell
pix-tool-set export-timing            # 约 100s，结果缓存，之后秒开
pix-tool-set pass-cost --limit 10     # model 变为 measured-gpu-time
pix-tool-set event-timing --group-by pass --limit 15
pix-tool-set session-open --capture frame.wpix --with-timing   # 开会话时一并导出
```

实测数据（Tiled.wpix）：5,562 个事件带耗时样本，335 个 pass 有实测值，
`TileClassificationMark` 的 dispatch 实测 **11.795 ms**。

底层是 `pixtool save-event-list --counters=<glob>`。实测出的两个硬限制：

- 计数器名**含空格时不能直接传**，pixtool 会报 `Unknown option`。必须用 glob，
  例如 `*Duration*`。
- `--counters=*` 在大截帧上会失败（约 39s 后报 `E_PIX_PERFORMANCE_ANALYSIS_FAILED`），
  必须收窄 glob。

注意逐事件耗时之和会超过帧的墙钟时间，因为异步队列的工作是重叠的。

## marker 路径以事件列表为准

C++ 导出把多个 command list 交错写在一起，靠流式跟踪 `PIXBeginEvent`/`PIXEndEvent`
维护的标记栈会串味：在一个 command list 上已闭合的标记，在回放另一个 list 时仍留在栈上。
这曾导致 pass 路径出现重复段（`Frame N / … / Frame N / …`），419 个 pass 里 416 个受影响。

事件列表 CSV 的 `Parent` 列是显式父链，天然正确。现在 draw 的 marker 路径以事件列表为准
（2,696 / 2,786 个 draw 可对齐），仅剩 90 个 draw 未被 CSV 收录、继续沿用解析值——这与
[global-id-queue-id.md](global-id-queue-id.md) 说的 90 个无 `queue_id` 的 action 是同一批
（提交到未导出事件列表的队列上，典型是 Lumen 的 async compute）。
