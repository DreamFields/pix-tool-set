# pix-tool-set

面向 AI 客户端的 PIX 截帧（`.wpix`）脚本化分析工具集。
按 [requirement.md](Doc/requirement.md) 的 12 大类需求实现，共 **66 个 CLI 工具**，
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

## 五、工具总览（70 个）

**会话管理（4）** `session-open` `session-close` `session-list` `capture-info`

**事件与 Action 导航（6）** `list-actions` `action-info` `search-actions`
`find-draw-calls` `locate-event` `find-pass`

**实测耗时（2）** `export-timing` `event-timing`

**帧统计（4）** `frame-stats` `list-passes` `pass-info` `pass-cost`

**纹理分析（8）** `list-textures` `texture-stats` `texture-info` `export-texture`
`export-draw-textures` `read-texture-pixels` `texture-pixel-stats` `pick-pixel`

**Shader 分析（12）** `shader-stats` `list-shaders` `shader-info` `disassemble-shader`
`shader-reflection` `shader-bindings` `constant-buffer` `pass-bindings`
`pass-shader-source` `pass-values` `shader-edit-begin` `shader-edit-apply`

**Shader 源码与编辑（3）** `session-set-pdb-dirs` `shader-edit-begin` `shader-edit-apply`
—— 从引擎 shader PDB 恢复真实 HLSL，改完重编译并校验绑定签名后打补丁到导出工程，
是 PIX Debug 面板 Apply 按钮的可脚本化等价物。

**纹理数值读取（4）** `read-resource-texture` `read-replay-target` `find-depth-content`
`export-uav-slice`

**模型与 DrawCall（4）** `model-stats` `draw-call-stats` `list-draw-calls` `diff-draw-calls`

**管线状态（5）** `list-pipeline-states` `pipeline-state` `draw-state` `vertex-input`
`post-vs-data`

**资源管理（3）** `list-resources` `list-buffers` `resource-usage`

**数据导出（4）** `read-buffer` `export-mesh` `save-render-target` `export-report`

**高级分析（4）** `pixel-history` `analyze-pass` `sample-pixel-region` `debug-pixel-shader`

**性能分析（3）** `analyze-overdraw` `analyze-bandwidth` `analyze-state-changes`

**诊断（4）** `diagnose-negative-values` `diagnose-precision`
`diagnose-reflection-mismatch` `diagnose-mobile-risks`

**调用活动（2）** `activity-viewer` `activity-log` —— 本地网页实时显示每次调用与结果，
并支持逐步回放调用历史。

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

### 第二组对照：PS 的 Scene cbuffer（77 行全覆盖）

Queue ID 17765（`Emit Scene Depth/Resolve/Velocity`）的 `Scene` 是个 316 字节、
76 字段的大缓冲，按 offset 逐行比对（offset 比名字可靠，PIX 的名字列会截断）：

```
 offset  PIX                      ours
      0  8                        8                        MATCH
     32  14150                    14150                    MATCH   BindlessSRV_..._InstanceSceneData
     72  1.82731e+28              1.82731e+28              MATCH   Scene_Padding72
     96  {4294967295, 0, 0, 0}    {4294967295, 0, 0, 0}    MATCH   MeshPaint_PackedUniform
    136  -6.94327e+37             -6.94327e+37             MATCH   Scene_Padding136
    224  {1, 1}                   {1, 1}                   MATCH   SplineMesh_SplineTextureInvExtent
    280  5.82588e-10              5.82588e-10              MATCH   Scene_Padding280
    316  (无值)                   (无值)                   MATCH   pad

match 76 | differ 0 | missing 0
```

这一组把之前只有内部自洽性支撑的几类都验证了：`uint4` / `float2` 向量、
全部 bindless 句柄（14150、5210-5213、917、3324、6498…）、以及
`1.82731e+28` / `-6.94327e+37` 这类极端浮点。

回归脚本：`tests/verify_scene_against_pix_gui.py`。

### 多 cbuffer 的寄存器配对

这个 draw 的 PS 同时绑了三个 cbuffer，是上一组（单 cbuffer 的 compute pass）
测不出来的场景：

```
root[1] -> cb0 -> _RootShaderParameters
root[2] -> cb1 -> View
root[3] -> cb2 -> Scene       <- Scene 在这里
```

配对键必须是 `(shader_register, visibility)` 而非仅寄存器号。原因是 root signature
可以合法地把同一寄存器声明两次、靠阶段区分 —— 这个截帧的 Slate draw 就是
root[2] 为 `PIXEL` 的 b0、root[3] 为 `VERTEX` 的 b0。

```powershell
pix-tool-set pass-values --queue-id 17765 --stage PS --cbuffer Scene
```

抽样 60 个多 CBV draw：每个绑定恰好解码 1 个布局。
回归脚本：`tests/verify_cbv_register_match.py`。

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

## 十二、读取深度缓冲 / 纹理（两条路径）

```powershell
# 路径 A：直接读截帧字节，不需要 GPU 回放
pix-tool-set read-resource-texture --queue-id 17765 --target depth --output G:\out --png G:\out
pix-tool-set read-resource-texture --queue-id 17765 --target depth --at-x 766 --at-y 382

# 路径 B：GPU 回放，拿到 pass 真正写出的结果
pix-tool-set save-render-target --queue-id 17765 --depth -o depth.png
```

**两条路径回答的是不同问题**，这是最需要注意的一点。

### 路径 A：截帧里记录的字节

Queue ID 17765 的深度目标是 rid 1985（`R32G8X24_TYPELESS` 1532x764）。
PIX 用 `CopyTextureRegion` + placed footprint 上传它的初始内容，所以字节在
`resources.bin` 里，但**不是**扁平像素数组：

```
subresource 0  R32_TYPELESS  1532x764  row pitch 6144   offset 0          (深度平面)
subresource 1  R8_TYPELESS   1532x764  row pitch 1536   offset 4,694,016  (模板平面)
```

必须按 footprint 解析。忽略它、直接用总字节除以像素数会得到 5.013 B/px 这种
无意义的结果（既不是 4 也不是 8）。按 footprint 解出来是
**1,170,448 像素 = 1532×764 精确匹配**。

导出会写两个文件（去掉 pitch 填充的紧凑行）加可选的归一化 PNG：

```
resource1985_sub0_1532x764_R32_TYPELESS.bin   4,681,792 bytes
resource1985_sub1_1532x764_R8_TYPELESS.bin    1,170,448 bytes
```

### 关键限制：A 拿到的不是 pass 的输出

对这个 pass，路径 A 解出的深度范围是 `0.00096..0.00139`，看着像合理的反向 Z。
但工具会标 `content_character: analytic_gradient` 并降级为 `partial`：

```
neighbour step distinct values : 2
depth discontinuities          : 0
```

邻域差值恒为 `2.14262e-07`，全图**没有任何几何不连续点**。渲染出来的深度一定有
遮挡边缘；这是解析梯度，即截帧初始化时的内容，而非 pass 写入的结果。
归一化 PNG 一看就是纯渐变，而 GPU 回放那张有清晰的建筑轮廓。

原因很简单：`resources.bin` 只存 PIX 观察到的上传和 CPU 写入。GPU 渲染进深度
缓冲的内容从未经过上传，所以不在截帧里。

### 路径 B：GPU 回放

`save-render-target --depth` 通过 `pixtool` 重放该帧，拿到的是 pass 执行后的真实
深度。注意直接导出的 PNG 因反向 Z 值极小会接近全黑，需要自行拉伸对比度才便于
肉眼查看（路径 A 的 `--png` 会自动做归一化）。

### 从 GPU 回放读取真实数值（DDS 路径）

`save-render-target` 写出的是图片，回答"长什么样"，但回答不了"这个像素的值是多少"
—— PNG 已经压到 8 位并做过映射。要拿数值必须走无损格式：

```powershell
pix-tool-set read-replay-target --draw-index 2328 --rtv 0 --at-x 900 --at-y 500
```

```
R10G10B10A2_UNORM  1815x1115  dxgi=24
payload matches dimensions: True
nonzero bytes: 8,091,074 (99.95%)
pixel: {'x': 900, 'y': 500, 'value': [0.1017, 0.1017, 0.1017, 1.0]}
```

`pixtool` 只接受 `.png` 和 `.dds` 两种扩展名（其他一律 PIXTOOL14）。DDS 保留源
DXGI 格式，所以数值可还原。位域打包的格式会解包成通道值而不是原始整数：
`R10G10B10A2`、`R11G11B10_FLOAT`（含 11/10 位小浮点与次正规数）都已单元验证，
见 `tests/verify_dds_formats.py`。

### 两个硬限制（都来自 pixtool，不是解析问题）

**深度不能导出为 DDS。** `pixtool` 直接拒绝：

```
PIXTOOL13 - Cannot save Depth Buffer as DDS
save-resource cannot save Depth Buffer (DXGI_FORMAT '40') as a DDS file.
Make sure file name for Depth Buffer ends with .png
```

所以**回放的深度只有 8 位 PNG，拿不到浮点值**。要深度的浮点数只能走
`read-resource-texture`（但那是初始化内容，见上一节）。

**回放采样的是事件执行前的状态。** 这点很容易踩坑。实测 Queue ID 17765：

| 探测点 | 结果 |
|---|---|
| 该 pass 自己的 rt0/rt1 | 100% 全零 |
| 更早 draw 写过的目标（draw 2328） | 99.95% 有内容 |

它自己的 RT 全零不是 bug，而是**正确答案** —— 该 draw 还没执行。工具会标
`surface_is_empty` 并降级为 `partial`，提示改用后续事件。

同理，17765 那张深度 PNG 有几何，是因为深度在此 pass 之前已被 Nanite 光栅化填充，
不是这个 pass 的产出。

### 深度：16 位而非 8 位，但只有一个事件有几何

先纠正上一节的一处说法。`pixtool` 导出的深度 PNG 是 **16 位灰度**（`bit_depth=16`），
不是 8 位。65536 个色阶足以做量化分析，不只是缩略图。

但更关键的是**时点问题**。rid 1985 在全帧有 16 个深度绑定事件，逐个探测后：

| 事件 | 色阶数 | 范围 | 不连续点 | 判定 |
|---|---|---|---|---|
| draw 2352 | 439 | 0..1177 | 3 | **rendered** |
| 其余 13 个可导出事件 | 548 | 738..1439 | 0 | analytic_gradient |

**只有 draw 2352 含真实几何**，其余 15 个返回同一条解析梯度。靠猜事件，16 次里错 15 次。
所以新增了自动扫描：

```powershell
pix-tool-set find-depth-content --queue-id 17765
pix-tool-set read-replay-target --draw-index 2352 --depth --at-x 766 --at-y 382
```

```
best event: draw #2352   levels=439 edges=3   character=rendered
pixel: {'x': 766, 'y': 382, 'level': 908, 'normalised': 0.01385519}
```

对比同样的读取用在问题里那个 pass 上，会被标 `analytic_gradient` 并降级 `partial`。

### 两条被证伪的思路

**"不指定 `--global-id` 就能拿最终内容"** —— help 里确实写着取"最后一个绑定该资源的
事件"，但实测导出的是**完全不同的资源**（2243x1119 的 `R10G10B10A2`，而非 rid 771）。
因为 `--rtv` 只是槽位号，脱离具体事件就失去意义。

**"找后续把深度当 SRV 采样的事件，用 `--rtv` 导成彩色面"** —— 统计下来
rid 1985 **被当作 render target 的事件数为 0**，`--rtv` 永远到不了它。思路不成立。

### 深度数值的最终结论

`pixtool --help save-resource` 原文：

```
--depth   Specifies to save a visual representation of
          a depth buffer. Only PNG files are supported.
```

官方明确这是"visual representation"且只支持 PNG。因此：

| 想要 | 结论 |
|---|---|
| 深度的原始 32 位浮点（pass 产出） | **不可得**，工具链无此导出 |
| 深度的 16 位量化色阶（pass 产出） | ✅ `read-replay-target --depth`，需先定位事件 |
| 深度的原始浮点（初始化内容） | ✅ `read-resource-texture`，但非 pass 产出 |
| RT 的原始数值 | ✅ `read-replay-target`（DDS 保留源格式） |

### 选哪条

| 需求 | 路径 |
|---|---|
| RT 的真实像素数值 | `read-replay-target`（DDS，需选后续事件） |
| pass 执行后的深度 | `find-depth-content` 定位事件 + `read-replay-target --depth`（16 位量化） |
| 单个像素的精确浮点值 | A（`--at-x --at-y`），若内容标为 rendered |
| 原始字节、自己做后续处理 | A（`--output`） |
| `pixtool` 拒绝的资源 | A（不依赖回放） |
| 纹理的初始上传内容 | A |

### Texture3D 选 z、Tex2DArray 选 slice

两者布局不同，别混用：

| 类型 | 存储方式 | 用什么选 |
|---|---|---|
| `Texture3D` | 所有 z 切片挤在**同一个** subresource 里，相邻切片相隔 `row_pitch × height` 字节 | `read-resource-texture --z <n>` |
| `Tex2DArray` | **每层一个** subresource | `export-uav-slice --slice <n>` |

```powershell
# 468x468x450 的体积纹理，取第 225 层的某个体素
pix-tool-set read-resource-texture --resource-id 1896 --z 225 --at-x 234 --at-y 234

# 导出该层为 bin + 可视化 PNG（文件名带 z，不会互相覆盖）
pix-tool-set read-resource-texture --resource-id 1896 --z 225 --output G:\out --png G:\out
```

返回里 `volume.z_slices` 给出总层数，`z_availability` 说明录到的字节实际覆盖多少层：

```
z_availability: declared=450  complete=449  partial=true
                bytes_recorded=107,827,156  bytes_declared=107,827,200
```

少了 44 字节，所以**末片不完整**——这种情况会明说，而不是返回一张看起来完整的截断图。
越界同样是拒绝而非钳制：

```
invalid_argument: resource 1896 has 450 depth slice(s), so valid indices are 0..449; 450 is out of range.
```

### 读 buffer 的值

```powershell
pix-tool-set read-buffer --resource-id 448 --length-bytes 64 --format R32_FLOAT
pix-tool-set read-buffer --resource-id 448 --offset-bytes 4096 --length-bytes 256 --output G:\out\buf.bin
```

给 `--format` 就按类型解码成 `elements`，不给就只回 hex。`--stride` 可覆盖默认步长
（结构化缓冲区里挑某个字段时有用）。

同样的前提仍然成立：`resources.bin` 只存上传与 CPU 写入，**GPU 在帧内算出来的
buffer 内容不在其中**，此时 `bytes_available` 为 `false` 并说明原因。

## 十三、导出纹理 UAV 的数组切片（如 RWLightGrid）

```powershell
pix-tool-set export-uav-slice --queue-id 18461 --name RWLightGrid --slice 2 --output G:\out
```

Queue ID 18461（`Light Grid Create`，CS `RayTracingBuildLightGridCS`）的
`RWLightGrid` 是 **rid 824，`R8_UINT` 256x256x3**：

```
resolved_by=name   slice 2 of 3
footprint: 256x256 pitch=256 offset=131,072
values: min=0 max=1 nonzero=20,962 distinct=2
```

### 切片数由三处独立证据确定

不靠猜，三个来源互相印证：

| 来源 | 证据 |
|---|---|
| cbuffer | `LightGridResolution = 256`、`LightGridAxis = -1`、`LightGridMaxCount = 1` |
| 命令列表 | `Dispatch(32, 32, 3)` × `numthreads(8,8,1)` = 256x256x3 |
| 恢复的 HLSL | `if (... \|\| DispatchThreadId.z >= 3) return;` |

所以**只有 slice 0/1/2**，`.z` 就是数组层，一层对应一个投影轴。请求越界切片会被明确
拒绝而非静默钳制：

```
invalid_argument: resource 824 has 3 slice(s), so valid indices are 0..2; 4 is out of range.
```

### 为什么不能靠描述符表定位

这个 dispatch 的 UAV 表基址是**过期的**。root[1] 记录 heap 32 index 134140，那里放的
是 buffer SRV/UAV；`RWLightGrid` 的描述符实际在 index **134034**，相差 106。命令列表
里本次 dispatch 只重设了 root[0] 和 root[2]，UAV 表沿用了上一次 dispatch 的。

所以按名字解析时改用三重收窄：shader 声明的维度（`2darray`）→ 数组层数大于 1 →
cbuffer 里的 `*Resolution` 字段匹配宽高。

### 数据有效性与来源

三个切片互不相同（差异 1.1 万~1.2 万字节），bbox 分别从 (129,84)、(86,84)、(86,129)
起、都延伸到 255 —— 正是同一个光源球体沿三轴投影、各缺一个轴偏移的特征。归一化后的
PNG 直接可见：左上直角是场景边界裁切，右下圆弧是球面，与 `AABBOverlapsSphere` 吻合。

但来源必须说清：`provenance` 标为 `initial upload at capture time`。
`resources.bin` 只存上传和 CPU 写入，GPU 在 dispatch 中写入的值不在其中。这份数据与
dispatch 输出高度一致，但**无法仅凭截帧证明二者相等**。

## 十四、改 shader 源码并应用（PIX Debug 面板 Apply 的等价物）

PIX GUI 的 Debug 面板可以直接改 shader 源码再点 Apply。这是 GUI 独有能力：
`pixtool` 的完整命令列表里**没有任何 shader 替换命令**。等价能力由两步拼出：

```powershell
# 1. 取出可编辑的真实 HLSL，连同 PDB 里记录的原始编译参数
pix-tool-set shader-edit-begin --queue-id 18461 --output G:\edit `
  --pdb-dirs "F:\JL_TMR\UnrealEngine\Games\JyGame\Saved\ShaderSymbols\PCD3D_SM6"

# 2. 编辑 G:\edit\q18461_CS_RayTracingBuildLightGridCS.hlsl 之后
pix-tool-set shader-edit-apply --queue-id 18461 `
  --source G:\edit\q18461_CS_RayTracingBuildLightGridCS.hlsl --patch
```

`begin` 会写出三个文件：可编辑源码、一份 pristine 副本（用于回退对比）、
以及 `.args.txt`。`apply` 默认自动读取那个 sidecar，所以第二步不必重复参数。

### 为什么这条路走得通

PDB 同时提供了重编译所需的两半，缺一不可：

| 需要什么 | 从哪来 |
|---|---|
| 自包含的预处理 HLSL（无需 include 路径） | PDB 的 `SRCI`，UE 写的是单个编译单元 |
| 精确编译参数 | PDB 记录 `-HV 2021 -Zpr -O1 -WX -auto-binding-space 0 -Zsb -Zi -Qstrip_debug -E <entry> -T cs_6_6` |

编译走 `IDxcCompiler3`（ctypes 裸 COM，零第三方依赖），`dxc.exe` 作为回退。
`dxil.dll` 会先加载，因此产物容器**已签名** —— 未签名的容器 D3D12 会直接拒绝。

### 核心安全检查：绑定签名

录制的命令列表**按 slot 绑定资源**，所以只有"资源、register、入口点、线程组尺寸
全部不变"的替换才是安全的。不满足就拒绝打补丁，而不是放行：

```
status: partial
binding_check.identical: false
warning: Compiled successfully but the replacement is not slot-compatible,
         so it was not patched in.  reason: bindings differ
```

实测：原样重编译时 5 个绑定（`cb0` / `t0` / `t1` / `u0` / `u1`）逐项一致；
一旦多声明一个 UAV 把 `RWLightGrid` 从 `u0` 挤到 `u1`，就会被上面这条拦住。
确实想改绑定时用 `--allow-binding-change` 显式放行。

编译失败则原样透出 DXC 自己的诊断，带行列号：

```
__UE_FILENAME_SENTINEL:259:32: error: expected expression
        uint3 VoxelId = 0, VoxelRes = ;
                                      ^
```

### 补丁做了什么，边界在哪

`--patch` 修改的是**导出的 C++ 回放工程**，不是 `.wpix`。截帧记录的是 API 调用序列，
本工具不会改写它。补丁采用"保留原赋值 + 下一行覆盖"而非整行替换：

```cpp
    g_resourceReader->Read(data, 12491);
    pssDesc.CS = { reinterpret_cast<BYTE*>(&data[offset]), 16436 };
    // pix-tool-set: CS replaced by shader-edit-apply
    static std::vector<BYTE> editedBytes_CS = Helpers::ReadFileBytes(LR"(edited_CreatePipelineState_3241_CS.dxil)");
    if (!editedBytes_CS.empty())
        pssDesc.CS = { editedBytes_CS.data(), editedBytes_CS.size() };
```

两个原因必须这样做：本工具集自身要解析 `CreatePSOs.cpp` 得到各 stage 字节码尺寸，
整行替换会让它再也找不到该 shader；而 `resources.bin` 是**无索引的顺序流**，
跳过一次 `Read` 会让后面所有 blob 错位。

补丁前自动留 `.orig` 备份，重复应用会被 `already_patched` 拦住。改完重建即可运行：

```powershell
cmake -S <export> -B <export>\build && cmake --build <export>\build --config Release
```

回归覆盖见 [tests/verify_shader_edit.py](tests/verify_shader_edit.py)（41 项），
包含语法错误诊断、绑定拒绝、重复补丁检测与自动还原。

## 十五、实时查看调用活动与历史回放

每次调用（CLI 与 `call_tool` 两个入口）都会追加到一份活动日志，配套一个本地网页
实时跟随，并可把历史逐步回放：

```powershell
pix-tool-set activity-viewer                 # 起服务并打开浏览器，Ctrl+C 停止
pix-tool-set activity-viewer --port 9000 --no-browser
```

页面左侧是调用流（时间、工具名、关键参数、状态、耗时），右侧是详情三视图：
概览（命令原文、诊断、参数、结果摘要、产出文件）、结果数据、原始信封。
顶部可按工具名/命令/参数过滤，也可只看 `error` 或 `partial`。

回放用于复盘"当时按什么顺序做了什么"：`▶ 回放` 自动逐条前进，`下一步` 手动单步，
速度可选 1.6s / 0.8s / 0.3s，或**真实间隔**（按当时两次调用的实际时间差，
上限 5 秒，避免中间挂机一小时把回放卡住）。快捷键 `j`/`k` 上下移动，空格开始或停止。

### 不用网页时

```powershell
pix-tool-set activity-log --limit 10                  # 最近 10 次调用
pix-tool-set activity-log --status error              # 只看失败的
pix-tool-set activity-log --tool-name export-uav-slice
pix-tool-set activity-log --record-id <id>            # 取回某次调用的完整信封
pix-tool-set activity-log --stats-only                # 只要聚合统计
pix-tool-set activity-log --clear                     # 清空日志与所有 payload
```

### 存储与实时的实现取舍

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

### 分享或归档

```powershell
pix-tool-set activity-viewer --export G:\out\pix-activity.html
```

产出单个 HTML，历史与 payload 全部内嵌、零外部引用，离线双击可开（此模式下不再跟随
新调用）。内嵌 payload 有 8 MB 预算，超出的会被跳过并在 `diagnostics` 里说明。

### 关掉记录

```powershell
$env:PIX_TOOL_SET_NO_LOG = '1'
```

记录失败永不影响调用本身：日志目录不可写、磁盘满等情况都被静默吞掉——丢一条日志无所谓，
丢用户的真实结果不行。

## 十六、`partial` 的含义（重要）

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

## 十七、Python API

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

## 十八、架构

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

## 十九、验证

```powershell
python tests\verify_live.py                 # 静态分析类工具
python tests\verify_live.py --with-replay    # 含 GPU 回放的纹理/像素类工具
python tests\verify_shader_edit.py           # shader 改源码并应用的全链路（41 项）
python tests\verify_activity.py              # 调用活动记录与查看器（44 项）
python tests\verify_value_reads.py           # buffer / 2D 纹理 / 3D 纹理 z 切片取值（52 项）
```

在 `NoTiled.wpix`（2.33 GB，UE5 ManyLights 场景）上的实测结果：

| 项 | 结果 |
|---|---|
| 工具总数 | 70 |
| 成功 | 51 |
| partial | 6（均为已声明的数据边界）|
| 异常 | **0** |
| 跳过 | 13（GPU 回放类需 `--with-replay`；`session-open`/`session-close` 会改动会话状态；`shader-edit-apply` 依赖前置产物；`activity-viewer` 会常驻服务，后两者分别由 `verify_shader_edit.py` 与 `verify_activity.py` 覆盖）|

解析规模：22,118 events、2,784 draw/dispatch、416 passes、3,293 resources、
480,958 descriptors、359 shaders、56 root signatures。

## 二十、已知边界

- 依赖本机 PIX 安装；纹理与像素类工具需要该截帧能在本机 GPU 上回放。
- 首次 `session-open` 对 2.3 GB 截帧约需 30–60 秒，缓存约 2.5 GB。
- 单次纹理导出需 GPU 回放，约 30 秒；批量分析建议先用 `export-draw-textures`
  一次导出多张，再本地读取。
- 逐像素替换历史、实时寄存器级 shader 调试需要 PIX 实时回放会话，
  本工具提供静态等价物（覆盖分析 + 完整 shader 代码与输入）并明确标注。
- 改 shader 源码后的替换（`shader-edit-apply`）生效范围是**导出的 C++ 回放工程**，
  不改写 `.wpix`；且需要引擎的 shader PDB 才能取到真实 HLSL 与原始编译参数。
- 部分资源在特定事件不是可保存的 RTV/DSV，PIX 会返回 `0x80070032`；
  错误信封会原样透传 PIX 的诊断文本。
