# pix-tool-set

面向 AI 客户端的 PIX 截帧（`.wpix`）脚本化分析工具集。
按 [requirement.md](Doc/requirement.md) 的 12 大类需求实现，共 **60 个 CLI 工具**，
每个工具都自带 JSON Schema，输出统一的 JSON 信封，无需读文档即可被程序驱动。

## 一、为什么这样设计

AI 客户端调用命令行工具时有三个硬需求，本工具集逐一对应：

**能自己发现能力** — `list-tools` 输出全部工具的机器可读目录（含参数 JSON Schema、
返回值说明、示例、能力边界注记）；`describe <tool>` 给出单个工具的完整契约。
客户端不需要预置任何工具知识。

**输出可直接解析** — 每个工具都返回同一个信封，`status` 只有 `success` / `partial` /
`error` 三种，失败时 `error.code` 决定恢复路径、`error.suggestion` 给出下一步动作。
标准输出只有一个 JSON 对象，不掺杂日志。

**代价高的操作只做一次** — `session-open` 完成 pixtool 导出（2.3 GB 截帧约 30–60 秒），
把产物登记为命名会话；此后所有查询毫秒级复用，跨进程有效。

## 二、安装

```powershell
cd G:\pix-tool-set
pip install -e .
```

环境要求：Windows、Python 3.11+、已安装 Microsoft PIX（自动探测
`C:\Program Files\Microsoft PIX\<版本>`，也可用 `PIXTOOL_PATH` 或 `--pixtool` 指定）。
无第三方依赖。

安装后可用 `pix-tool-set` 或简写 `pixts`；未安装时用
`python -m pix_tool_set.cli`（需设置 `PYTHONPATH=G:\pix-tool-set\src`）。

## 三、三种调用方式

```powershell
# 1) 自描述：列出全部工具及其 schema
pix-tool-set list-tools
pix-tool-set list-tools --category shaders --brief
pix-tool-set describe draw-state

# 2) JSON 调用（推荐给 AI 客户端，参数结构化）
pix-tool-set run list-passes --json-args '{"limit": 10, "sort_by": "triangles"}'

# 3) 直接子命令（人手输入更顺）
pix-tool-set list-passes --limit 10 --sort-by triangles
```

退出码：成功 `0`，工具级错误 `1`，参数错误 `2`。

## 四、典型工作流

```powershell
pix-tool-set session-open --capture D:\caps\frame.wpix    # 一次性导出并建会话
pix-tool-set frame-stats                                  # 全帧概览
pix-tool-set list-passes --sort-by triangles --limit 10   # 找最重的 Pass
pix-tool-set analyze-pass --pass-index 12                 # 深挖某个 Pass
pix-tool-set draw-state --draw-index 2461                 # 看某次 draw 的全部绑定
pix-tool-set disassemble-shader --draw-index 2461 --stage PS -o ps.txt
pix-tool-set diagnose-mobile-risks                        # 移动端风险体检
```

## 五、工具总览（60 个）

**会话管理（4）** `session-open` `session-close` `session-list` `capture-info`

**事件与 Action 导航（6）** `list-actions` `action-info` `search-actions`
`find-draw-calls` `locate-event` `find-pass`

**实测耗时（2）** `export-timing` `event-timing`

**帧统计（4）** `frame-stats` `list-passes` `pass-info` `pass-cost`

**纹理分析（8）** `list-textures` `texture-stats` `texture-info` `export-texture`
`export-draw-textures` `read-texture-pixels` `texture-pixel-stats` `pick-pixel`

**Shader 分析（9）** `shader-stats` `list-shaders` `shader-info` `disassemble-shader`
`shader-reflection` `shader-bindings` `constant-buffer` `pass-bindings`
`pass-shader-source`

**模型与 DrawCall（4）** `model-stats` `draw-call-stats` `list-draw-calls` `diff-draw-calls`

**管线状态（5）** `list-pipeline-states` `pipeline-state` `draw-state` `vertex-input`
`post-vs-data`

**资源管理（3）** `list-resources` `list-buffers` `resource-usage`

**数据导出（4）** `read-buffer` `export-mesh` `save-render-target` `export-report`

**高级分析（4）** `pixel-history` `analyze-pass` `sample-pixel-region` `debug-pixel-shader`

**性能分析（3）** `analyze-overdraw` `analyze-bandwidth` `analyze-state-changes`

**诊断（4）** `diagnose-negative-values` `diagnose-precision`
`diagnose-reflection-mismatch` `diagnose-mobile-risks`

## 六、输出信封

```json
{
  "status": "success",
  "tool": "list-passes",
  "data": { "passes": [ ... ], "total": 416, "has_more": true, "next_offset": 10 },
  "output_paths": [],
  "diagnostics": []
}
```

列表类工具统一分页：`total` / `offset` / `limit` / `returned` / `has_more` / `next_offset`，
客户端可据此翻页而不必猜测。

错误信封：

```json
{
  "status": "error",
  "tool": "texture-info",
  "error": {
    "code": "texture_not_found",
    "message": "No texture matches 99999.",
    "stage": "query",
    "suggestion": "Run list-textures to find valid ids.",
    "details": {}
  }
}
```

## 七、按 pass 查询 shader 绑定

拿某个 pass 的 shader 绑定资源，一条命令即可（不必再手动 pass → draw_index → bindings 三步走）：

```powershell
pix-tool-set pass-bindings --pass-name TileClassificationBuildLists --stage CS
pix-tool-set pass-bindings --pass-name TileClassification --all-matches   # 同名多 pass 全列
pix-tool-set find-pass --name TileClassificationBuildLists               # 只要 id 时用这个
```

`pass-bindings` 会自动按 PSO 去重挑代表 draw，并把 descriptor table 默认展开到 128 项
（UE5 的 SRV table 声明 64 项，旧的 16 项默认值会截断）。

返回分两层，可信度不同：

- `stages[].declared_registers` —— 来自 shader 字节码反射，**权威**。这就是
  「该 pass 绑定了哪些 shader 资源」的答案，含 HLSL 寄存器、资源变量名、格式、维度。
- `root_descriptors` / `descriptor_tables` —— 从导出的 C++ 重建的运行时绑定，
  每项带 `trust` 字段：

| trust | 含义 |
|---|---|
| `reliable` | 直接来自记录的调用（如 root CBV 的 rid），或槽位数与 shader 声明吻合 |
| `partial` | 已重建但未经确认，不要依赖 register → resource 的逐项映射 |
| `filler` | 该窗口是 PIX 的初始化占位，真实描述符未被记录 |
| `unavailable` | 该 table 完全没有描述符数据 |

出现 `filler` / `unavailable` 时结果为 `partial`，并在 `diagnostics` 里提示改用
`declared_registers`。原因是 PIX 的 C++ 导出对部分 draw 未记录真实的描述符写入 ——
这是导出格式的边界，不是解析缺陷；此时精确的 register → rid 对应需要用
`disassemble-shader` 看资源索引指令，或在 PIX GUI 里查看。

## 八、用 PIX GUI 的 Global ID / Queue ID 定位

PIX GUI 事件列表里每行有两个 id，本工具集都能直接接受，不需要先换算成 `draw_index`：

| GUI 列 | 覆盖范围 | 说明 |
|---|---|---|
| `Global ID` | 仅 action（draw/dispatch/copy 等），本截帧 5,334 / 22,155 行 | 在 GUI 里选中一次 draw 就能看到 |
| `Queue ID` | **每一行都有**，22,155 / 22,155 | 唯一全量主键，marker（pass 行）只有这个 |

所以查 pass 时优先用 `Queue ID`：pass 的标记行本身没有 Global ID。

```powershell
pix-tool-set find-pass --global-id 3893        # -> pass TileClassificationMark
pix-tool-set find-pass --queue-id 18704        # 同一个 pass
pix-tool-set pass-bindings --global-id 3893    # 直接出 shader 绑定
pix-tool-set event-timing --global-id 3893     # 直接出实测耗时
```

`find-pass` 返回里同时给出 `global_id`、`queue_id`、`marker_queue_id`（pass 标记自身的
Queue ID）和 `draw_index`，四者互通，可用于和 GUI 交叉核对。

### CSV 是否需要改？

不需要加列 —— `Queue ID`、`Parent`、`Name`、`Global ID` 四列已经够了，`Parent` 链就是
权威的 marker 层级。真正值得做的是**另外导出一份带 GPU 计数器的事件列表**（见第九章），
因为基础 CSV 不含耗时。

## 九、实测 GPU 耗时（可选，一次性）

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


### marker 路径以事件列表为准

C++ 导出把多个 command list 交错写在一起，靠流式跟踪 `PIXBeginEvent`/`PIXEndEvent`
维护的标记栈会串味：在一个 command list 上已闭合的标记，在回放另一个 list 时仍留在栈上。
这曾导致 pass 路径出现重复段（`Frame N / … / Frame N / …`），419 个 pass 里 416 个受影响。

事件列表 CSV 的 `Parent` 列是显式父链，天然正确。现在 draw 的 marker 路径以事件列表为准
（2,696 / 2,786 个 draw 可对齐），仅剩 90 个 bundle 内部调用未被 CSV 收录、继续沿用解析值。

## 十、查看某个 pass 的 shader 源码

```powershell
pix-tool-set pass-shader-source --queue-id 18461
pix-tool-set pass-shader-source --pass-name "Light Grid Create" --stage CS --max-lines 0
pix-tool-set pass-shader-source --queue-id 18461 --output-dir ./src_dump
```

### 先说结论：UE5 截帧里没有原始 HLSL

实测这份截帧的全部 **363 个 shader**：

| 容器块 | 数量 | 含义 |
|---|---|---|
| `ILDN` | 363 | 只记录外部 PDB 的**文件名** |
| `ILDB` / `SPDB` | **0** | 嵌入式源码块，一个都没有 |

原始 HLSL 只在编译时带 `/Zi /Qembed_debug` 才会进容器。UE5 把调试信息放在独立 PDB 里，
截帧只留了个文件名（如 `3e92071c09a522dfa4e259e557334efc.pdb`），所以源码文本不在截帧内。
这不是解析能力问题 —— PIX GUI 打开同一个截帧也看不到 HLSL。

### 能拿到什么

`pass-shader-source` 返回 `source_tier` 明确标注答案的层级：

| tier | 含义 |
|---|---|
| `embedded-hlsl` | 真的取到了原始 HLSL（本截帧为 0 个） |
| `dxil-disassembly` | 无嵌入源码，返回 DXIL 反汇编 + 入口函数名 |
| `unavailable` | 连反汇编都产不出 |

以 Queue ID 18461 为例，实测输出：

```
pass        : Light Grid Create (1 lights)
draw_index  : 2470   pso: 3241
stage       : CS
source_tier : dxil-disassembly
entry_point : RayTracingBuildLightGridCS
num_threads : [8, 8, 1]
pdb_name    : 3e92071c09a522dfa4e259e557334efc.pdb
lines       : 2052
```

### `entry_point` 是找回源码的关键

**363 / 363 个 shader 都能取到入口函数名**（100%）。拿它在引擎源码树里搜就能定位 `.usf`：

```powershell
rg -l "RayTracingBuildLightGridCS" D:\UE5\Engine\Shaders
```

反汇编本身也含完整的输入输出签名、cbuffer 字段布局、资源绑定表和 `numthreads`，
逆向分析所需的信息基本齐全，只是没有变量名和注释。

如果你有 shader PDB 的输出目录，传 `--pdb-dirs` 可以让工具去找对应 PDB：

```powershell
pix-tool-set pass-shader-source --queue-id 18461 --pdb-dirs D:\UE5\Saved\ShaderDebugInfo
```

## 十一、`partial` 的含义（重要）

`partial` 表示**答案可用，但某处被降级**，原因一定写在 `diagnostics` 里。
这比假装成功或直接报错更有用，因为它区分了「工具坏了」和「数据本就不存在」。
本工具集在以下情况返回 `partial`，都是 PIX 截帧的客观边界，不是实现缺陷：

- `pass-cost` — 截帧未采集 GPU 计数器时，耗时用工作量模型估算而非实测毫秒
- `post-vs-data` — 变换后顶点只存在于 PIX 实时回放会话，C++ 导出中没有
- `read-buffer` / `export-mesh` — GPU 运行期生成的缓冲区内容未被截帧记录
- `constant-buffer` — root CBV 被解析为 GPU 地址，逐 draw 的字节未内嵌
- `pass-bindings` — 部分 draw 的真实描述符写入未被导出记录（见第七章 `trust`）
- `disassemble-shader --prefer-source` / `pass-shader-source` — 该 shader 未带嵌入式 HLSL 调试信息

关于 shader 源码：PIX 截帧存的是**编译后字节码**，原始 HLSL 只在编译时带
`/Zi /Qembed_debug` 才会嵌入。未嵌入时返回 DXIL 反汇编（含完整输入输出签名、
资源绑定表、入口函数名、numthreads 与全部 IR）。`has_embedded_source` 字段明确
告知属于哪种情况。

## 十二、Python API

```python
from pix_tool_set import call_tool, list_tools, open_capture

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

## 十三、架构

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
    cppparse.py     解析导出 C++：资源、描述符、PSO、root signature、命令列表状态机
    eventlist.py    事件 CSV 解析、事件分类、树重建
    dxbc.py         DXBC 容器、签名、反射、DXIL 反汇编
    xpress.py       resources.bin 的 XPRESS 解压与偏移索引
  tools/            12 个模块，每类需求一个
tests/verify_live.py 端到端验证：逐一调用全部工具
Doc/requirement.md   原始需求
```

数据来源是 `pixtool export-to-cpp` 产出的 C++ 工程加 `resources.bin`。
命令列表解析器是一台**状态机**：按序重放导出的 D3D12 调用，持续跟踪当前 PSO、
root signature、描述符堆、渲染目标、顶点/索引缓冲与 root 参数，在每次
draw/dispatch 处快照——这份快照正是 PIX 选中某次 draw 时展示的内容。
描述符表的展开跨度取自**真实 root signature 声明的范围**，而非固定猜测。

`resources.bin` 用 XPRESS 顺序流压缩（无索引表），本工具通过 `Cabinet.dll`
解压并重建偏移索引以支持随机访问；shader 反汇编调用 PIX 自带的
`dxcompiler.dll`（裸 COM vtable），因此零第三方依赖。
纹理像素读取内置纯 stdlib 的 PNG 解码器。

## 十四、验证

```powershell
python tests\verify_live.py                 # 静态分析类工具
python tests\verify_live.py --with-replay    # 含 GPU 回放的纹理/像素类工具
```

在 `NoTiled.wpix`（2.33 GB，UE5 ManyLights 场景）上的实测结果：

| 项 | 结果 |
|---|---|
| 工具总数 | 55 |
| 成功 | 49 |
| partial | 4（均为已声明的数据边界）|
| 异常 | **0** |
| 跳过 | 2（`session-open` / `session-close`，会改动会话状态）|

解析规模：22,118 events、2,784 draw/dispatch、416 passes、3,293 resources、
480,958 descriptors、359 shaders、56 root signatures。

## 十五、已知边界

- 依赖本机 PIX 安装；纹理与像素类工具需要该截帧能在本机 GPU 上回放。
- 首次 `session-open` 对 2.3 GB 截帧约需 30–60 秒，缓存约 2.5 GB。
- 单次纹理导出需 GPU 回放，约 30 秒；批量分析建议先用 `export-draw-textures`
  一次导出多张，再本地读取。
- 逐像素替换历史、实时寄存器级 shader 调试需要 PIX 实时回放会话，
  本工具提供静态等价物（覆盖分析 + 完整 shader 代码与输入）并明确标注。
- 部分资源在特定事件不是可保存的 RTV/DSV，PIX 会返回 `0x80070032`；
  错误信封会原样透传 PIX 的诊断文本。
