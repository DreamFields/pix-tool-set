# 设计、架构与验证
本文收录从 README 移出的设计与工程细节：设计理念、`partial` 语义、Python API、架构、验证与已知边界。使用指南见 [guides/](guides/)。
## 为什么这样设计
AI 客户端调用命令行工具时有三个硬需求，本工具集逐一对应：

**能自己发现能力** — `list-tools` 输出全部工具的机器可读目录（含参数 JSON Schema、
返回值说明、示例、能力边界注记）；`describe <tool>` 给出单个工具的完整契约。
客户端不需要预置任何工具知识。

**输出可直接解析** — 每个工具都返回同一个信封，`status` 只有 `success` / `partial` /
`error` 三种，失败时 `error.code` 决定恢复路径、`error.suggestion` 给出下一步动作。
标准输出只有一个 JSON 对象，不掺杂日志。

**代价高的操作只做一次** — `session-open` 完成 pixtool 导出（2.3 GB 截帧约 30–60 秒），
把产物登记为命名会话；此后所有查询毫秒级复用，跨进程有效。

## env-check 的探测取舍
几个探测方式上的取舍，都是为了不给出假结论：

- **dxcompiler 真的去 load**。文件存在不代表 `DxcCreateInstance` 在这台机器上能成功，
  而后者才是每个 shader 工具真正依赖的东西。
- **D3D12 只问不建**。`D3D12CreateDevice` 传空 `ppDevice` 是官方的能力查询方式，返回
  `S_FALSE` 即代表「能建」，所以探测全程不创建设备、不建队列、没有东西需要释放。
- **`required: false` 表示"有替代路线"**，不是硬依赖。Windows SDK 的 `dxc.exe` 只在 PIX
  的 `dxcompiler.dll` 拒绝某个 shader 时才用得上，缺它不阻塞任何事。
- **没跑过的探测不给结论**。`--scope core` 的输出里没有 `gpu_replay` 字段，因为一个
  replay 探测都没跑；编一个 `true` 出来比什么都不说更糟。
- **`--check-network` 默认关闭**。默认保持离线、快速；不测网络时 Agility SDK 一项会明说
  「尚未缓存，CMake 首次配置时会下载」，而不是伪造一个你无法处置的失败。

## `partial` 的含义（重要）
`partial` 表示**答案可用，但某处被降级**，原因一定写在 `diagnostics` 里。
这比假装成功或直接报错更有用，因为它区分了「工具坏了」和「数据本就不存在」。
本工具集在以下情况返回 `partial`，都是 PIX 截帧的客观边界，不是实现缺陷：

- `pass-cost` — 截帧未采集 GPU 计数器时，耗时用工作量模型估算而非实测毫秒
- `post-vs-data` — 变换后顶点只存在于 PIX 实时回放会话，C++ 导出中没有
- `read-buffer` / `export-mesh` — GPU 运行期生成的缓冲区内容未被截帧记录
- `constant-buffer` — root CBV 被解析为 GPU 地址，逐 draw 的字节未内嵌
- `pass-bindings` — 部分 draw 的真实描述符写入未被导出记录（见[绑定指南](guides/pass-bindings.md)的 `trust`）
- `disassemble-shader --prefer-source` / `pass-shader-source` — 未配置 shader PDB 目录，且截帧未嵌入 HLSL（配好 `session-set-pdb-dirs` 后即为 success）

关于 shader 源码：PIX 截帧存的是**编译后字节码**，原始 HLSL 只在编译时带
`/Zi /Qembed_debug` 才会嵌入。未嵌入时返回 DXIL 反汇编（含完整输入输出签名、
资源绑定表、入口函数名、numthreads 与全部 IR）。`has_embedded_source` 字段明确
告知属于哪种情况。

## Python API

```python
from pix_tool_set import call_tool, list_tools, open_capture

# 先确认本机依赖齐备（无需会话，只读）
env = call_tool("env-check")["data"]
if not env["ready"]["read_only_analysis"]:
    raise SystemExit(env["next_step"])

# 工具级（与 CLI 完全一致的信封）
result = call_tool("list-passes", {"limit": 10})
result["status"], result["data"]["passes"]

# 按 pass 一步拿 shader 绑定
data = call_tool("pass-bindings", {"pass_name": "TileClassificationBuildLists",
                                   "stage": "CS"})["data"]
for entry in data["passes"][0]["draws"][0]["stages"]:
    for reg in entry["declared_registers"]:
        print(reg["hlsl_bind"], reg["type"], reg["name"])

# 引擎级（直接拿到解析对象）
capture = open_capture(r"D:\caps\frame.wpix")
capture.frame_statistics()
draws, total = capture.find_draw_calls(pass_name="Lumen", limit=10)
draw = capture.draw_calls[2461]
draw.render_targets, draw.srvs, draw.uavs
draw.shader("PS").disassembly
```

可运行的完整示例见 [examples/demo_session.py](../examples/demo_session.py) 与
[examples/pass_shader_bindings.py](../examples/pass_shader_bindings.py)。

## 架构

```
src/pix_tool_set/
  cli.py            命令行：list-tools / describe / run / 自动生成的子命令
  registry.py       工具注册中心：JSON Schema、参数校验、分类
  results.py        统一结果信封（success / partial / error + diagnostics）
  errors.py         结构化错误（code / stage / suggestion）
  session.py        命名会话持久化（跨进程复用导出产物）
  context.py        执行上下文：会话 -> Capture 引擎，进程内缓存
  pixtool.py        pixtool.exe 定位与驱动
  engine/
    capture.py      引擎门面：惰性分层解析 + 查询 + 统计
    model.py        类型化模型（Event / DrawCall / Shader / Resource / View）
    cppparse.py     解析导出 C++：资源、描述符、PSO、root signature、命令列表状态机、
                    command signature（判定 ExecuteIndirect 走图形还是计算管线）、
                    command queue 提交记录（队列归属推导）
    eventlist.py    事件 CSV 解析、事件分类、树重建
    dxbc.py         DXBC 容器、签名、反射、DXIL 反汇编
    lineage.py      资源生产-消费链合成：写入方、读取方、状态时间线与断言判定
    override.py     固定功能状态干预：PSO 文本改写、PSO 克隆重定向、draw 屏蔽/独显，
                    每个被改文件先备份，支持逐字节回滚
    xpress.py       resources.bin 的 XPRESS 解压与偏移索引
    envcheck.py     本机依赖探测：PIX / dxcompiler / CMake / VS 生成器 / Windows SDK /
                    D3D12 设备 / 自带 WinPixEventRuntime / Agility SDK，全程只读
  tools/            32 个模块，每类需求一个（events / pipeline / textures / shaders …）
tests/verify_live.py 端到端验证：逐一调用全部工具
Doc/requirement.md   原始需求
```

数据来源是 `pixtool export-to-cpp` 产出的 C++ 工程加 `resources.bin`。
命令列表解析器是一台**状态机**：按序重放导出的 D3D12 调用，持续跟踪当前 PSO、
root signature、描述符堆、渲染目标、顶点/索引缓冲与 root 参数，在每次
draw/dispatch 处快照——这份快照正是 PIX 选中某次 draw 时展示的内容。
描述符表的展开跨度取自**真实 root signature 声明的范围**，而非固定猜测。

`ExecuteIndirect` 按 **command signature 的 argument type** 判定走图形还是计算
root 参数集（间接只覆盖计数，绑定仍由紧邻的 `Set*Root*` 调用建立；直接当图形调用
读会把 134/187 个间接调用的绑定读成空）。队列归属（每个 action 跑在哪条命令队列）
从 `RenderFrameWorker_*.cpp` 的 `ExecuteCommandLists` 提交记录推导——它跨全部队列，
比只覆盖一条队列的事件列表 CSV 更完备，且实测归属无歧义（无 command list 提交到
多个队列）。

`resources.bin` 用 XPRESS 顺序流压缩（无索引表），本工具通过 `Cabinet.dll`
解压并重建偏移索引以支持随机访问；shader 反汇编调用 PIX 自带的
`dxcompiler.dll`（裸 COM vtable），因此零第三方依赖。
纹理像素读取内置纯 stdlib 的 PNG 解码器。

## 验证

```powershell
python tests\verify_live.py                 # 静态分析类工具
python tests\verify_live.py --with-replay    # 含 GPU 回放的纹理/像素类工具
python tests\verify_shader_edit.py           # shader 改源码并应用的全链路（41 项）
python tests\verify_activity.py              # 调用活动记录与查看器（45 项）
python tests\verify_replay_render.py         # 渲染结果抓取与面板展示（42 项）
python tests\verify_value_reads.py           # buffer / 2D 纹理 / 3D 纹理 z 切片取值（52 项）
python tests\verify_replay_override.py       # 固定功能状态干预与逐字节回滚（38 项，无需 GPU）
```

`verify_replay_override.py` 跑在合成的导出工程上，不需要截帧也不需要 GPU：它钉住的是
一轮真实回放要花几分钟才能发现的性质——`skip_draw` 只注释目标 draw、`solo_draw` 反过来
只保留目标、缺选择器时拒绝执行而非清空整帧、`write_mask` 的通道组合展开，以及每种改写
之后都能逐字节还原。

在 `NoTiled.wpix`（2.33 GB，UE5 ManyLights 场景）上的实测结果（验证时工具总数 93；
此后新增 `env-check`，当前共 94 个）：

| 项 | 静态（默认） | 含 GPU 回放（`--with-replay`）|
|---|---|---|
| 工具总数 | 93 | 93 |
| 成功 | 63 | **74** |
| partial | 6 | 6 |
| 异常 | **0** | **0** |
| 跳过 | 24 | 13 |

两种模式下 `partial` 都是同样那 6 个，全部落在「`partial` 的含义」列出的数据边界上：
`export-mesh`、`post-vs-data`、`pass-shader-source`、`pass-values`、`export-uav-slice`、
`read-resource-texture`。没有一个是实现缺陷。

默认模式跳过的 24 个按原因分布：11 个需 GPU 回放；3 个会改写导出工程
（`replay-override` / `replay-reset` / `snapshot-remove`，由 `verify_replay_override.py`
覆盖）；3 个要构建并运行整个工程（`replay-render` / `bisect-render-state` /
`frame-replay-dump`）；2 个依赖前置产物（`shader-edit-apply` / `shader-edit-diff`）；
2 个会改动会话状态（`session-open` / `session-close`）；2 个需要先有基线渲染
（`replay-baseline-check` / `snapshot-compare`）；1 个会常驻服务（`activity-viewer`）。
`--with-replay` 把前 11 个补齐，剩下 13 个跳过项与 GPU 无关。

回放类的实测耗时：纹理导出与像素读取各 26–45 秒（每次都要 `pixtool` 重放该帧）；
`read-uav` 82 秒、`pixel-history-replay` 94 秒，这两个要编译并运行探针工程，
在大截帧上默认等待窗口不够，实测里显式放宽到 `settle_seconds=240`。

解析规模：22,118 events、2,784 draw/dispatch、406 passes、3,293 resources、359 shaders。

## 已知边界

- 依赖本机 PIX 安装；纹理与像素类工具需要该截帧能在本机 GPU 上回放。
- 首次 `session-open` 对 2.3 GB 截帧约需 30–60 秒，缓存约 2.5 GB。
- 单次纹理导出需 GPU 回放，约 30 秒；批量分析建议先用 `export-draw-textures`
  一次导出多张，再本地读取。
- 逐像素替换历史、实时寄存器级 shader 调试需要 PIX 实时回放会话，
  本工具提供静态等价物（覆盖分析 + 完整 shader 代码与输入）并明确标注。
- 改 shader 源码后的替换（`shader-edit-apply`）生效范围是**导出的 C++ 回放工程**，
  不改写 `.wpix`；且需要引擎的 shader PDB 才能取到真实 HLSL 与原始编译参数。
- 固定功能状态干预（`replay-override`）同样只改导出工程，逐字节可回滚；但 PSO 由多个
  draw 共享，`scope=pso` 会影响全部使用者，隔离到单个 draw 要用 `scope=draw`（克隆 PSO
  后只重定向目标 draw）。
- `bisect-render-state` 的判据读的是**呈现到 backbuffer 的画面**，每轮都要重新构建并运行
  整个工程（大截帧上每轮数分钟）。从未到达 backbuffer 的症状无法用它二分，改用 `read-uav`
  或 `read-replay-target` 直接读数值。
- 部分资源在特定事件不是可保存的 RTV/DSV，PIX 会返回 `0x80070032`；
  错误信封会原样透传 PIX 的诊断文本。
