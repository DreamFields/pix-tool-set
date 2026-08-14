# 工具层缺陷修复报告（光追分析可用性专项）

本文档记录一次针对**光追（DXR）分析路径可用性**的集中修复：问题清单来自一次真实的
端到端分析任务（会话 `Tiled`，截帧 `C:\Users\vinmeng\Desktop\ManyLights\debug\Tiled.wpix`，
PDB 目录 `F:\JL_TMR\UnrealEngine\Games\JyGame\Saved\ShaderSymbols\PCD3D_SM6`）。

该任务最终成功产出了完整的光追信息，但过程中被 **10 处工具层面的问题**打断。
本次修复了其中 **8 处**，剩余 2 处经确认属于数据物理限制、工具层无解，
现有处理方式（返回 null 并说明原因）已是正确做法，不做改动。

---

## 一、问题分类与结论

| # | 问题 | 类别 | 处置 |
|---|------|------|------|
| ① | `--compact` 必须前置，放子命令后报 `unrecognized arguments` | 工具缺陷 | ✅ 已修 |
| ② | PowerShell `>` 重定向产出 UTF-16，Python 读取报 `0xff` | 工具缺陷 | ✅ 已修 |
| ③ | `pass-shader-source` 对光追 pass 直接报 `shader_not_found` | 工具缺陷 | ✅ 已修 |
| ④ | `list-actions --kind raytracing` 找不到 ray dispatch | 工具缺陷 | ✅ 已修 |
| ⑤ | `locate-event` 拒绝 AS 构建事件 | 工具缺陷 | ✅ 已修 |
| ⑥ | `list-tools` 输出双层嵌套，脚本消费不便 | 工具缺陷 | ✅ 已修 |
| ⑦ | `RayTracingBuildScene` 查不到 pass，提示无指向性 | 设计取舍 | ✅ 已改善 |
| ⑧ | 缺"光追总览"命令，需 10+ 次调用才能拼出全貌 | 能力缺失 | ✅ 已新增 |
| ⑨ | BLAS 三角形/顶点数不可得 | 数据物理限制 | ⛔ 无解，保持现状 |
| ⑩ | postbuild info 缺失 | 数据物理限制 | ⛔ 无解，保持现状 |

---

## 二、逐项修复说明

### ① 全局参数位置限制

**症状**：`pix-tool-set list-tools --compact` 报 `unrecognized arguments: --compact`。

**根因**：`--compact` / `--traceback` 只注册在顶层 parser 上。argparse 的语义是
"标志只能被声明它的那个 parser 消费"，一旦进入子命令，argv 尾部归子 parser 所有。

**改法**（`cli.py`）：新增 `_build_global_parent()` 返回一个 `add_help=False` 的
parent parser，通过 `parents=[global_parent]` 挂到主 parser 与**全部**子命令（含别名）上。

```bash
pix-tool-set --compact list-tools      # 前置，原本就支持
pix-tool-set list-tools --compact      # 后置，现在也支持
```

> `--pixtool` **刻意不放进 parent**：若干工具在自己的 schema 里已声明同名参数，
> parent 再声明一份会触发 argparse 的 `conflicting option string`。它仍只挂在顶层。

### ② 输出编码陷阱

**症状**：`pix-tool-set ... > out.json` 在 PowerShell 下产出 UTF-16 文件，
`json.load(open(..., encoding='utf-8'))` 报 `UnicodeDecodeError: 0xff`。

**根因**：不是工具的错，是 PowerShell 重定向的行为。但工具有责任提供一条不依赖
shell 行为的可靠出口。

**改法**（`cli.py`）：新增全局参数 `--output-json <PATH>`，由工具自己以
`encoding="utf-8", newline=""` 写盘，stdout 只回一行确认。

```bash
pix-tool-set analyze-raytracing --output-json rt.json
# stdout: {"status": "written", "path": "...\rt.json"}
```

同时把所有出口统一到 `_emit_for(payload, namespace)`，避免各调用点重复处理输出选项。

### ③ `pass-shader-source` 对 DXR 主动失败（影响最大）

**症状**：对光追 pass 调用直接抛 `shader_not_found: No shader matches 'any'`。

**根因**：该函数只走 `draw.shaders`（PSO stages）。DXR shader 是 state object 里
DXIL_LIBRARY 的 export，`draw.shaders` 对 DispatchRays 恒为空。
**关键点**：原代码在报错分支里**已经拿到了** state object id、export 数量，甚至把正确
命令拼进了 suggestion —— 它什么都知道，却选择失败。

**改法**（`source_tools.py`）：新增 `_dxr_export_rows()`，在 `draw.state_object_id`
非空时走 DXR 分支，直接从 state object 的 exports 恢复源码。要点：

- 按 **HLSL 入口点去重**：一个入口点会被编译进多个 collection、生成多个 mangled 名。
  不去重会把同一份源码重复输出几十遍。用 `aliased_export_count` 说明该行代表多少个 export。
- 新增 `--export-name` 参数，可按 mangled 名或入口点名精确过滤。
- `--stage` 复用既有 DXR 阶段枚举（`RAYGEN` / `CLOSESTHIT` / `ANYHIT` / `MISS` 等）。
- 无 PDB 时降级为 DXIL 反汇编，与光栅路径的分层语义一致。
- 响应带 `binding_shape: "raytracing"`，并说明 `pso_id: null` 是**管线形状使然**，不是数据缺失。

```bash
pix-tool-set pass-shader-source --pass-name "ReflectionHardwareRayTracingRGS default" --stage RAYGEN
# → status=success, source_tier=pdb-hlsl, entry=LumenReflectionHardwareRayTracingRGS, aliases=8
```

### ④ 光追 dispatch 在事件视图中不可见

**症状**：`list-actions --kind raytracing` 只返回 3 个 AS 构建，两个 ray dispatch 完全找不到。

**根因**：`_KIND_VALUES` 来自 `EventKind` 枚举，是 **D3D12 API 字面名**。UE5 导出里
没有字面 `DispatchRays`，只有 `ExecuteIndirect`（挂在 DISPATCH_RAYS command signature 上）。
`find-draw-calls` 早已有 `effective_kind` 解决此问题，`list-actions` 却没有。

**改法**（`event_tools.py`）：为 `list-actions` 新增 `--effective-kind`。因为
`effective_kind` 是 draw call 上的派生字段、事件行上没有，实现方式是先按该字段收集
对应的 Global ID 集合，再过滤事件行。未命中时给出 diagnostic，提示未导出队列上的事件
需改用 `find-draw-calls`。

```bash
pix-tool-set list-actions --effective-kind dispatch_rays --detail
# → 2 条：gid 5311 (RGS default) / gid 5366 (hit-lighting)
```

### ⑤ `locate-event` 拒绝 AS 构建事件

**症状**：`locate-event --global-id 3752` 报
`Global ID 3752 is a <unknown> command, not an action`。

**根因**：该工具只接受 action（draw/dispatch/dispatch_rays）。但
`BuildRaytracingAccelerationStructure` 有 marker 路径、有生成源码行号、有前后邻居
—— 正是这个工具该回答的内容。拒绝服务等于放弃了本可给出的答案。

**改法**（`event_tools.py`）：在原报错前插入降级分支，对已能解析的非 action 命令返回
`status=partial`，内容包含 marker 路径、`pass_name`、源码位置、前后邻近 draw；
若该 id 是 AS 构建，额外附 `type` / `instance_count` / `dest_resource_id` / `flags`。

**连带修复**（`capture.py`）：`command_by_global_id` 原先只在 GlobalId 注释后
**5 行**窗口内、且只匹配 `GetCommandList(N)->Api(` 一种形式找 API 名。AS 构建要先填
一堆 D3D12 desc 结构体，调用行远在 5 行之外，且导出为局部变量形式
（`commandList->Api(...)`），所以恒为 `<unknown>`。现扩大到 60 行窗口、
增加局部变量调用形式匹配，并以"遇到下一个 GlobalId 即停"防止串行借用。

```bash
pix-tool-set locate-event --global-id 3752
# → api: BuildRaytracingAccelerationStructure（原为 <unknown>）
#   source: CommandLists_000.cpp:56277
#   acceleration_structure_build: {type: top_level, instance_count: 3, ...}
```

### ⑥ `list-tools` 结构不便脚本消费

**改法**（`cli.py`）：新增 `--flat`，输出扁平 `tools[]` 数组（按名排序、每项带 `category`），
免去遍历 `categories[].tools[]` 两层。

```bash
pix-tool-set list-tools --brief --flat     # → tool_count: 88, tools 为一维数组
```

### ⑦ 无 draw 的 marker 查不到 pass

**症状**：`pass-info --pass-name "RayTracingBuildScene"` 报
`No pass matches`，提示仅"Run list-passes"，无指向性。

**根因**：pass 的定义是"包含至少一个 draw 的 marker"（`capture.py` 的 `passes` 按
`draw_calls` 的 marker_path 分桶）。`RayTracingBuildScene` 内部只有 AS 构建命令、
没有 draw，因此不产生 pass。这个定义本身合理，但报错让人误以为"名字不存在"。

**改法**（`_common.py`）：`resolve_pass` 在名字未命中时，回查是否存在同名 marker。
若存在则明确说明"这是一个不含 draw 的 marker，因此不构成 pass"，并列出它包含的
AS 构建 global_id 与可用替代命令。

```
'RayTracingBuildScene' is a marker (queue_id=18403) that encloses no draw call,
so it forms no pass. It contains 3 acceleration structure build(s)
(global_id 3752, 3753, 3754). Use analyze-acceleration-structures for the full
description, list-raytracing-work for the ordered timeline, or
locate-event --global-id <id> for one build's context.
```

同时修正了同一函数中 `cmd.api` 误把 dict 当对象访问的隐患（应为 `cmd.get('api')`）。

### ⑧ 新增 `analyze-raytracing` 聚合命令

**动机**：原先要拼出一帧的光追全貌，需依次调用 `list-raytracing-work`、
`describe-state-object`（×2）、`describe-shader-table`（×2）、
`analyze-acceleration-structures`、`event-timing`（×2）、`list-passes` 等 10 余次。

**改法**（`raytracing_tools.py`）：新增 `analyze-raytracing`，一次返回：

- `summary` —— dispatch 数、AS 构建数、TLAS 实例总数、state object 数、SBT 数、inline pass 数
- `ray_dispatches[]` —— 每个 dispatch 的 pipeline 摘要（payload/attribute/递归深度、
  export 与 hit group 数、按阶段分布、**去重后的入口点列表**）、SBT 区域布局与记录分布、
  以及**实测 GPU 耗时**
- `acceleration_structures` —— 构建列表、实例（`--detail` 下含变换矩阵）、
  序列化 blob 总量、以及 geometry 不可得的说明
- `inline_raytracing[]` —— **`TraceRayInline`（DXR 1.1）的计算 pass**。这类 pass 没有
  state object、没有 SBT，所有 state-object 工具对它们完全盲视，但它们确实是光追工作。
- `timing` —— 是否有缓存实测值及说明

三个设计约束：

1. **绝不隐式触发 GPU 回放**：`include_timing` 走 `ensure_timing(allow_export=False)`，
   只读已缓存的测量值。总览命令若可能突然跑 100 秒回放，就不再是总览。
2. **inline 识别标注为证据而非声明**：靠 UE5 pass 命名（`...HardwareRayTracing...`）匹配，
   响应里以 `evidence: "pass_name"` 明示。并排除 `*IndirectArgs*` pass —— 它们只填间接
   参数缓冲、不发射光线，计入会高估光追占比。
3. **沿用既有诚实性约定**：`stage_source_note` 与 BLAS geometry 说明原文照带，
   不因为是聚合视图就省略限制条件。

```bash
pix-tool-set analyze-raytracing            # 结构 + 缓存耗时
pix-tool-set analyze-raytracing --detail   # 加 export 清单、实例变换、SBT 记录
```

---

## 三、无法在工具层解决的两项

### ⑨ BLAS 三角形 / 顶点数

pixtool 导出的 BLAS 是**驱动私有的序列化 blob**（通过
`CopyRaytracingAccelerationStructure DESERIALIZE` 重放），源数据里根本没有
`D3D12_RAYTRACING_GEOMETRY_DESC`。blob 大小是压缩后的驱动结构，不能用来估算几何量。

现有处理（`triangle_count: null` + `geometry_note` 说明原因）是正确的：
凭 blob 大小反推三角形数会产出一个"看起来是测量值的推测值"，下游无法分辨，比不给答案更糟。

### ⑩ postbuild info

`actual` / `compacted` / `serialized` 尺寸只有应用调用过
`EmitRaytracingAccelerationStructurePostbuildInfo` 才存在。UE5 这一帧没调用。
现有 note 已明确区分"应用没问"与"解析失败"，无需改动。

---

## 四、改动文件清单

| 文件 | 改动 |
|------|------|
| `src/pix_tool_set/cli.py` | ① parent parser；② `--output-json` + `_emit_for`；⑥ `--flat` |
| `src/pix_tool_set/tools/source_tools.py` | ③ `_dxr_export_rows()` + DXR 分支 + `--export-name`；抽出 `_SOURCE_TIERS` |
| `src/pix_tool_set/tools/event_tools.py` | ④ `list-actions --effective-kind`；⑤ `locate-event` 非 action 降级 |
| `src/pix_tool_set/engine/capture.py` | ⑤ 连带：`command_by_global_id` 的 API 名识别（窗口 + 调用形式） |
| `src/pix_tool_set/tools/_common.py` | ⑦ `resolve_pass` 的 marker 引导；修正 `cmd.api` 误访问 |
| `src/pix_tool_set/tools/raytracing_tools.py` | ⑧ 新增 `analyze-raytracing` |

---

## 五、验证结果

### 5.1 修复项验证（会话 `Tiled`）

| # | 验证命令 | 结果 |
|---|----------|------|
| ① | `list-tools --brief --flat --compact` | ✅ 后置生效，不再报错 |
| ② | `--output-json` | ✅ UTF-8 直接可读，无需处理 BOM |
| ③ | `pass-shader-source --pass-name "...RGS default" --stage RAYGEN` | ✅ `success` / `pdb-hlsl` / 8 别名去重为 1 行 |
| ④ | `list-actions --effective-kind dispatch_rays` | ✅ 命中 gid 5311、5366 |
| ⑤ | `locate-event --global-id 3752` | ✅ `partial` + `BuildRaytracingAccelerationStructure` + 源码行 + 邻居 |
| ⑥ | `list-tools --flat` | ✅ `tool_count=88`，一维数组 |
| ⑦ | `pass-info --pass-name "RayTracingBuildScene"` | ✅ 报错含 3 个 build 的 gid 与替代命令 |
| ⑧ | `analyze-raytracing` | ✅ 一次输出 2 dispatch + 3 build + 3 inline + 实测耗时 |

### 5.2 `analyze-raytracing` 实测输出摘要

```
summary: ray_dispatches=2, acceleration_structure_builds=3, tlas_instances_total=3,
         state_objects_declared=81, shader_binding_tables=2, inline_raytracing_passes=3

gid 5311 | ReflectionHardwareRayTracingRGS default     | 232 rays | 0.1889 ms
         exports=17  hit_groups=4   unique_entry_points=4  sbt=1415_1
gid 5366 | ReflectionHardwareRayTracingRGS hit-lighting |   2 rays | 0.2463 ms
         exports=84  hit_groups=58  unique_entry_points=8  sbt=1415_2

builds: (3752, top_level, 3 instances) (3753, top_level, 0) (3754, top_level, 0)
blobs:  655 个 / 322,986,432 字节

inline: LumenDirectLightingHardwareRayTracingCS            (gid 4796)
        HardwareRayTracingCS <indirect> 4x4 probes         (gid 4844)
        HardwareRayTracingCS default                       (gid 5021)
```

### 5.3 回归验证

单元测试：`test_shader_scope.py` / `test_editledger.py` / `test_detect_patches.py`
—— **24 passed**。

既有命令全部 `status=success`，未受影响：

```
list-raytracing-work · describe-state-object · describe-shader-table
analyze-acceleration-structures · list-actions --kind raytracing · frame-stats
```

光栅路径未被 DXR 分支影响：`pass-shader-source --pass-name "Light Grid Create" --stage CS`
→ `success` / `pdb-hlsl` / 走 rasterisation 默认分支。

---

## 六、给调用方的行为变更提示

1. **新命令优先**：了解一帧光追情况，先用 `analyze-raytracing`，
   再按需下钻到 `describe-state-object` / `describe-shader-table`。
2. **`pass-shader-source` 现已支持光追**：不必再为 DXR 改走 `shader-edit-begin`。
   仅需修改 shader 时才用后者（它额外产出编译参数与可回滚副本）。
3. **检索光追 dispatch 用 `--effective-kind dispatch_rays`**，不要用 `--kind`：
   `--kind raytracing` 只返回 AS 构建，这是 API 字面名决定的，不是缺陷。
4. **`--output-json` 优于 shell 重定向**，尤其在 PowerShell 下。
5. **`locate-event` 现在可能返回 `partial`**：对非 action 命令属正常降级，
   `is_action: false` 时不要期待 `draw_index`。
