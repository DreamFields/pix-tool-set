# 实时查看调用活动与历史回放
每次调用（CLI 与 `call_tool` 两个入口）都会追加到一份活动日志，配套一个本地网页
实时跟随，并可把历史逐步回放：

```powershell
pix-tool-set activity-viewer                 # 起服务并打开浏览器，Ctrl+C 停止
pix-tool-set activity-viewer --port 9000 --no-browser
```

页面左侧是调用流（时间、工具名、关键参数、状态、耗时），右侧是详情三视图：
概览（命令原文、诊断、参数、结果摘要、产出文件）、结果数据、原始信封。
顶部可按工具名/命令/参数过滤，也可只看 `error` 或 `partial`。

`replay-render` 抓到的渲染画面也在这里：调用流上方出现缩略图带，详情多一个
「渲染结果」页签，概览顶部直接内嵌画面，点图放大。截图存在
`%LOCALAPPDATA%\pix-tool-set\activity\renders\`，页面通过 `api/render?name=` 取，
文件名经过校验（只接受该目录下的裸 PNG 名，拒绝任何路径穿越）。详见
[shader-editing.md](shader-editing.md)。

回放用于复盘"当时按什么顺序做了什么"：`▶ 回放` 自动逐条前进，`下一步` 手动单步，
速度可选 1.6s / 0.8s / 0.3s，或**真实间隔**（按当时两次调用的实际时间差，
上限 5 秒，避免中间挂机一小时把回放卡住）。快捷键 `j`/`k` 上下移动，空格开始或停止。

## 不用网页时

```powershell
pix-tool-set activity-log --limit 10                  # 最近 10 次调用
pix-tool-set activity-log --status error              # 只看失败的
pix-tool-set activity-log --tool-name export-uav-slice
pix-tool-set activity-log --record-id <id>            # 取回某次调用的完整信封
pix-tool-set activity-log --stats-only                # 只要聚合统计
pix-tool-set activity-log --clear                     # 清空日志与所有 payload
```

## 存储与实时的实现取舍

日志放在 `%LOCALAPPDATA%\pix-tool-set\activity\`，可用 `PIX_TOOL_SET_ACTIVITY_DIR`
改位置。每次调用写两处：

| 文件 | 内容 | 为什么分开 |
|---|---|---|
| `activity.jsonl` | 一行索引（工具、状态、耗时、命令原文、参数与结果的摘要） | 行足够小，多个 CLI 进程并发写时单次 `write` 实际原子；页面可按字节偏移增量读取 |
| `payloads/<id>.json` | 完整结果信封 | 结果可能几百 KB（如反汇编），内嵌进索引会让索引膨胀、页面变慢；每个进程写自己的文件，无写竞争 |

页面的游标是**索引文件的字节偏移**，不是条数。这是并发下唯一正确的游标：轮询时
另一个进程可能正在追加，用偏移就不必猜有多少条，也不会重复解析已拿到的部分；
读到不完整的末行会留到下次轮询，避免读进半条记录。

摘要里折叠的容器统一标成 `<list: n>` 与 `<dict: n keys>`，不用 `[n items]` 这类
写法——后者与真实字符串值长得太像，容易被当成数据本身。完整值在「结果数据」页按需拉取。

服务只绑 `127.0.0.1`，这一点不可配置：日志里含本机结果与文件路径，绑到其他地址是错的。

## 分享或归档

```powershell
pix-tool-set activity-viewer --export G:\out\pix-activity.html
```

产出单个 HTML，历史与 payload 全部内嵌、零外部引用，离线双击可开（此模式下不再跟随
新调用）。内嵌 payload 有 8 MB 预算，超出的会被跳过并在 `diagnostics` 里说明。

## 关掉记录

```powershell
$env:PIX_TOOL_SET_NO_LOG = '1'
```

记录失败永不影响调用本身：日志目录不可写、磁盘满等情况都被静默吞掉——丢一条日志无所谓，
丢用户的真实结果不行。
