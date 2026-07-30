# pix-tool-set

面向 AI 客户端的 PIX 截帧（`.wpix`）脚本化分析工具集。
按 [requirement.md](Doc/requirement.md) 的 12 大类需求实现，共 **62 个 CLI 工具**，
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

## 五、工具总览（62 个）

**会话管理（4）** `session-open` `session-close` `session-list` `capture-info`

**事件与 Action 导航（6）** `list-actions` `action-info` `search-actions`
`find-draw-calls` `locate-event` `find-pass`

**实测耗时（2）** `export-timing` `event-timing`

**帧统计（4）** `frame-stats` `list-passes` `pass-info` `pass-cost`

**纹理分析（8）** `list-textures` `texture-stats` `texture-info` `export-texture`
`export-draw-textures` `read-texture-pixels` `texture-pixel-stats` `pick-pixel`

**Shader 分析（9）** `shader-stats` `list-shaders` `shader-info` `disassemble-shader`
`shader-reflection` `shader-bindings` `constant-buffer` `pass-bindings`
`pass-shader-source` `session-set-pdb-dirs` `pass-values`

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

## 十、查看某个 pass 的 shader 源码（可取到真实 HLSL）

一次性告诉工具引擎的 shader 符号目录，之后按 pass 查源码即可：

```powershell
pix-tool-set session-set-pdb-dirs --pdb-dirs "F:\JL_TMR\UnrealEngine\Games\JyGame\Saved\ShaderSymbols\PCD3D_SM6"
pix-tool-set pass-shader-source --queue-id 18461
```

实测输出（Queue ID 18461 = `Light Grid Create (1 lights)`）：

```hlsl
[numthreads(8, 8, 1)]
void RayTracingBuildLightGridCS(uint3 DispatchThreadId : SV_DispatchThreadID)
{
	if (any(DispatchThreadId >= LightGridResolution) || DispatchThreadId.z >= 3)
	{
		return;
	}
	uint3 VoxelId = 0, VoxelRes = 1;
	int Axis = DispatchThreadId.z;
	...
}
```

真实变量名、缩进、控制流全在 —— 这是原始 HLSL，不是反汇编。

### 为什么截帧里没有，PDB 里却有

截帧内每个 shader 的 DXBC 容器只带 `ILDN`（PDB 文件名），不带 `ILDB`/`SPDB`（源码本体）。
从 PDB 读出的编译参数正好解释了原因：

```
-Zi -Qstrip_debug -E RayTracingBuildLightGridCS -HV 2021 -Zpr -O1 -WX
```

`-Zi` 生成调试信息、`-Qstrip_debug` 把它从截帧字节码里剥离并单独写进 `<hash>.pdb`。
所以源码一直存在，只是不在 `.wpix` 里。

### 恢复路径

主路径用官方 `IDxcPdbUtils`（`dxcompiler.dll`，CLSID `54621dfb-…`），通过 ctypes 裸 COM
调用，无需编译原生模块、无第三方依赖。若某个 DXC 版本拒绝加载该 PDB，回退到自己解
MSF 容器、取 DXBC 的 `SRCI` 块再 zlib 解压。

`source_tier` 明确标注答案来源：

| tier | 含义 |
|---|---|
| `pdb-hlsl` | 从引擎 shader PDB 恢复的真实 HLSL |
| `embedded-hlsl` | 截帧字节码里就带 HLSL（UE5 默认不会） |
| `dxil-disassembly` | 没有符号目录，退回 DXIL 反汇编 |
| `unavailable` | 连反汇编都产不出 |

### 输出范围

UE5 在真正的 shader 前会注入几百行生成代码（`select_internal` 重载等），
默认只返回入口函数（`scope=entry-function`）：

```powershell
pix-tool-set pass-shader-source --queue-id 18461                      # 仅入口函数，83 行
pix-tool-set pass-shader-source --queue-id 18461 --entry-only false   # 整个编译单元，335 行
pix-tool-set pass-shader-source --queue-id 18461 --body-only false    # 含注入的 cbuffer 前言
pix-tool-set pass-shader-source --queue-id 18461 --output-dir ./src   # 写文件
```

`--body-only false` 时能看到 UE5 生成的
`MoveShaderParametersToRootConstantBuffer` 段，里面是 `_RootShaderParameters` 的
`packoffset` 完整布局，对核对常量缓冲很有用。

### 实测覆盖率

抽样 60 个 shader：PDB 命中 **60/60**，HLSL 恢复 **60/60**，入口函数成功切出 **59/60**。
该符号目录共 129,989 个 `.pdb`。注意 hash 必须与截帧时的构建一致，换了构建就对不上。

## 十一、读取 shader 绑定资源的实际数值

```powershell
pix-tool-set pass-values --queue-id 18385
pix-tool-set pass-values --queue-id 18385 --element-type float4
pix-tool-set constant-buffer --draw-index 2469
pix-tool-set read-buffer --resource-id 2379 --length-bytes 64 --format R32G32B32_FLOAT
```

实测 Queue ID 18385（`RayTracingBuildInstanceBuffer`）：

```
summary: {'values_available': 5, 'values_stale': 0, 'values_unavailable': 0}

cbuffer _RootShaderParameters (CS):
  +    0 uint    8                            GPUSceneInstanceDataTileSizeLog2
  +    4 uint    255                          GPUSceneInstanceDataTileSizeMask
  +    8 uint    768                          GPUSceneInstanceDataTileStride
  +  128 uint    3                            MaxNumInstances
  +  160 float3  [-4877.1, -1759.1, -1330.9]  PreViewTranslationHigh
  +  188 float   15000                        CullingRadius
  +  192 float   1e+06                        FarFieldCullingRadius
```

`TileSizeLog2=8` 与 `Mask=255`（= 2⁸−1）自洽，可作为解码正确的旁证。

### resources.bin 的寻址

这个文件是**单条顺序 XPRESS 流，没有索引表**，只能靠重放 `Read()` 调用来定位。
实测确定的布局（顺序错一位，第一个块就解压失败）：

| 段 | 来源 | 块数 | 字节 |
|---|---|---|---|
| 1 | `CreatePSOs.cpp` | 376 | 8,762,741 |
| 2 | `CreateAndInitResources_00{0,1,2}.cpp` | 3,127 | — |
| 3 | `FrameResources_000.cpp` | 1 | 236,950,490 |
| 4 | 帧尾（**非文件序**） | 231 | 281,342 |

第 4 段是难点：它由两个交错的来源产生 —— `RenderFrameWorker` 里的
`ModifyResource_*` 调用，以及**嵌在 `PopulateCommandList_*` 函数体内**的 `Read`。
后者虽然写在 `CommandLists_*.cpp`，但执行时机取决于 `RenderFrameWorker` 何时调用它。
按文件序编号必然错位；沿 `RenderFrameWorker` 走一遍、遇到调用就展开被调函数自身的
读取，才能复现真实顺序。

结果：**3,735 / 3,735 个块全部解压成功，字节总数与文件大小完全相等（delta = 0）**。

### 帧内 CPU 改写必须重放

初始上传只是一半。UE5 在帧中用 `Map` + `memcpy` 按 4 KB 分页重写大上传缓冲，
导出代码把它记在 `ResourceModifications_*.cpp`：

```cpp
Map(resource 2955);
for (i = 0; i < 4; ++i)
    memcpy(mappedData + 4096 * PagesIndex_2955_8[i], &data[offset], 4096);
```

不重放这些写入，读到的就是帧前的旧字节。判断标准很直接 —— 修好之前
`BaseGroupDescriptorIndex` 读出 `1065353216`，那正是 float `1.0` 的位模式，
一个索引字段不可能是这个值。

### 与 PIX GUI 逐字段对照

拿 PIX 自己的常量缓冲视图当真值，对 Queue ID 18385 做了严格比对
（整数必须相等，浮点按 PIX 显示的有效位比较）：

```
 offset  field                                  PIX                    ours
      0  GPUSceneInstanceDataTileSizeLog2       8                      8            MATCH
      4  GPUSceneInstanceDataTileSizeMask       255                    255          MATCH
      8  GPUSceneInstanceDataTileStride         768                    768          MATCH
     12  GPUSceneFrameNumber                    59369                  59369        MATCH
    ...
    160  PreViewTranslationHigh   {-4877.11, -1759.08, -1330.95}  同上              MATCH
    176  PreViewTranslationLow    {-0.000216702, -2.56451e-05, 5.08402e-05}  同上   MATCH
    196  AngleThresholdRatioSq                  0.000304679            0.000304679  MATCH
    240  OutputStatsOffset                      0                      0            MATCH
    244  pad                                    (无值)                 (无值)        MATCH

match 21 | differ 0 | missing 0
```

回归脚本：`tests/verify_against_pix_gui.py`。

对照过程中修掉两处解析缺陷：

- **cbuffer 总大小取不到。** DXC 把成员包在一个 struct 里，只在收尾行给出大小
  （`} _RootShaderParameters;  ; Offset: 0 Size: 244`）。原逻辑把这行当成员处理，
  于是 `size` 永远是 `None`，外层 struct 还会以一个假字段的形式混进字段列表。
- **缺尾部 `pad`。** cbuffer 按 16 字节寄存器分配，244 会补齐到 256。PIX 会把这段
  显示成一行无值的 `pad`，现在与之一致（标 `is_padding`，不编造数值）。

全截帧 586 个 cbuffer：500 个取到声明大小，330 个含尾部 padding，struct 泄漏 0 个。

### 诚实边界

`pass-values` 为每个绑定单独给出可用性，不做整体断言：

| 状态 | 含义 |
|---|---|
| `values_available: true` | 字节可信，是 shader 实际读到的 |
| `values_are_stale: true` | 该页被 CPU 改写但补丁未能解码，给出旧值并标注 |
| `values_available: false` | 没有记录的字节 |

**UAV 输出通常属于第三类**：PIX 记录上传和 CPU 写入，不记录 GPU 产生的内容，
所以 dispatch 的计算结果读不到，读到的只是它执行前的内容。纹理数据走
`read-texture-pixels` / `save-render-target`。

实测覆盖率：**2,625 个带 root CBV 的 draw 全部可信解码（0 过期）**，
3,316 个资源中 3,127 个有可读字节（94.3%）。

## 十二、`partial` 的含义（重要）

`partial` 表示**答案可用，但某处被降级**，原因一定写在 `diagnostics` 里。
这比假装成功或直接报错更有用，因为它区分了「工具坏了」和「数据本就不存在」。
本工具集在以下情况返回 `partial`，都是 PIX 截帧的客观边界，不是实现缺陷：

- `pass-cost` — 截帧未采集 GPU 计数器时，耗时用工作量模型估算而非实测毫秒
- `post-vs-data` — 变换后顶点只存在于 PIX 实时回放会话，C++ 导出中没有
- `read-buffer` / `export-mesh` — GPU 运行期生成的缓冲区内容未被截帧记录
- `constant-buffer` — root CBV 被解析为 GPU 地址，逐 draw 的字节未内嵌
- `pass-bindings` — 部分 draw 的真实描述符写入未被导出记录（见第七章 `trust`）
- `disassemble-shader --prefer-source` / `pass-shader-source` — 未配置 shader PDB 目录，且截帧未嵌入 HLSL（配好 `session-set-pdb-dirs` 后即为 success）

关于 shader 源码：PIX 截帧存的是**编译后字节码**，原始 HLSL 只在编译时带
`/Zi /Qembed_debug` 才会嵌入。未嵌入时返回 DXIL 反汇编（含完整输入输出签名、
资源绑定表、入口函数名、numthreads 与全部 IR）。`has_embedded_source` 字段明确
告知属于哪种情况。

## 十三、Python API

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

## 十四、架构

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

## 十五、验证

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

## 十六、已知边界

- 依赖本机 PIX 安装；纹理与像素类工具需要该截帧能在本机 GPU 上回放。
- 首次 `session-open` 对 2.3 GB 截帧约需 30–60 秒，缓存约 2.5 GB。
- 单次纹理导出需 GPU 回放，约 30 秒；批量分析建议先用 `export-draw-textures`
  一次导出多张，再本地读取。
- 逐像素替换历史、实时寄存器级 shader 调试需要 PIX 实时回放会话，
  本工具提供静态等价物（覆盖分析 + 完整 shader 代码与输入）并明确标注。
- 部分资源在特定事件不是可保存的 RTV/DSV，PIX 会返回 `0x80070032`；
  错误信封会原样透传 PIX 的诊断文本。
