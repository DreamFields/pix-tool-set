# DXR 光追适配 —— 测试用例

本文档汇总 DXR 光追适配（阶段一至四 + 阶段六）落地后，用于验证工具链正确性的测试用例。

所有用例均基于会话 `Tiled`（截帧文件 `C:\Users\vinmeng\Desktop\ManyLights\debug\Tiled.wpix`）跑通。

---

## 用例 1：`shader-bindings` 对光追 pass（核心价值跃迁）

对 `ReflectionHardwareRayTracingRGS default`（`draw_index=2705`，state object `3891`）执行 `shader-bindings`。

**改动前后对比**

| 字段 | 改动前 | 改动后 |
|------|--------|--------|
| `status` | `partial` | `success` |
| `stages` | `[]`（空） | `['ANYHIT', 'CLOSESTHIT', 'MISS', 'RAYGEN']` |
| `state_object_unmodelled` | 有（标记未建模） | 无 |
| `exports` | 缺失 | 17 个 export，逐个带 `original_name` |
| `hit_groups` | 缺失 | 4 个 hit group 完整列出 |
| global / local 绑定 | 混成一个假列表（~40 条） | 11 个 global 绑定 + 9 条按 record 分开的 local 绑定，**分列** |

**看点**：光追 pass 的 binding 从"无法解析"跃迁为"与光栅 pass 同级可读"，export 的 `original_name`（如 `LumenHardwareRayTracingMaterialCHS`）直接对应 UE5 源码语义。

---

## 用例 2：RTPSO 展开（反向断言）

`describe-state-object --state-object-id 3930`

| 字段 | 值 | 含义 |
|------|-----|------|
| `own_exports` | `0` | 3930 自身没有声明任何 shader |
| `exports` | `84` | 全部来自引用的 collection |
| `hit_groups` | `58` | 同上，来自 collection |
| `existing_collections` | `66` | 被合并的 collection 数量 |
| `desc_segments` | `3` | 三段 `AddToStateObject` 链合并 |
| `type` | `raytracing_pipeline` | RTPSO |
| `global_root_signature_id` | `3889` | 全局根签名 |

**看点**：这是最容易"静默出错"的地方——若只读单个函数体，会得到"这个 pipeline 没有 shader"这种看着正确的错误答案。`own_exports=0` 反向断言了展开逻辑确实遍历了 collection 引用。

---

## 用例 3：SBT 的"越界记录"判断

`describe-shader-table --draw-index 2711 --table hit_group`

| 字段 | 值 | 含义 |
|------|-----|------|
| `record_count` | `13` | 总记录数 |
| `records_by_table.hit_group` | `8` | hit_group 记录数 |
| `records_by_table.miss` | `2` | miss 记录数 |
| `records_by_table.raygen` | `1` | raygen 记录数 |
| `records_outside_declared_regions` | `2` | 越界记录数 |
| `stride_in_bytes` | `128` | hit_group 步长 |

**看点**：那 2 条越界记录是 `CreateShaderTable_03` 写在 `&output[131072]`、`&output[131200]` 的 miss 标识——落在 hit-group buffer 尾部（buffer `147456` > 声明的 `131072`）。它们是应用原始合并布局的复现，**不被本次 dispatch 读取**。工具将它们标为 `in_declared_region: false`，既没误判成 hit_group，也没丢弃。

---

## 用例 4：instance → hit group 完整链

`analyze-acceleration-structures --resolve-hit-groups --indirect-buffer-key 1415_2`

链路：

```
instance 0
  ├─ contribution_to_hit_group_index = 6
  │    └─ × stride 128 = offset 768
  │         └─ 命中 HitGroup_ee4e6808208cbd63
  │              └─ CHS = CHS_ee4e6808208cbd63
  └─ 即：场景里这个物体使用的光追材质
```

**看点**：这是"场景里某个物体用了哪个光追材质"的完整答案链，也是相对 PIX GUI 的差异化能力。

---

## 用例 5：几何数的诚实拒绝（负向）

`analyze-acceleration-structures`

每个 BLAS build：

| 字段 | 值 |
|------|-----|
| `triangle_count` | `None` |
| `vertex_count` | `None` |
| `geometry_note` | 说明 BLAS 是驱动私有序列化 blob 重建，`GEOMETRY_DESC` 不在导出中 |

**看点**：这个负向断言专门防住"用 blob 大小反推三角形数"这类会编造数据的后续改动。

---

## 用例 6：RayGen 的 Root Signature 绑定视图（对齐 PIX GUI）

对 `ReflectionHardwareRayTracingRGS hit-lighting`（`draw_index=2711`，state object `3930`，即图片标注的 `global id=5367`）执行 `shader-bindings`，在 `exports` 里找到 raygen export 的 `bindings` 字段——它就是 PIX GUI 的「RayGen Record → Root Signature → Shader」面板内容。

**结果（与 PIX GUI 逐一对应）**

```
CBV [0, space=1]  : _RootShaderParameters
CBV [1, space=1]  : SceneTexturesStruct
CBV [1, space=4]  : View
CBV [2, space=1]  : LumenCardScene
CBV [3, space=1]  : ReflectionStruct
CBV [4, space=1]  : ReflectionCaptureSM5
CBV [5, space=1]  : FogStruct
CBV [6, space=1]  : ForwardLightStruct
CBV [7, space=1]  : RaytracingLightGridData
Static Sampler [1, space=1000] : D3DStaticPointClampedSampler
Static Sampler [3, space=1000] : D3DStaticBilinearClampedSampler
```

命令示例：

```powershell
pix-tool-set shader-bindings --global-id 5367
```

**如何定位到这份结果**

1. `shader-bindings --global-id 5367` 返回的 `exports` 中，每个 `stage == "RAYGEN"` 的 export 都带一个 `bindings` 字段。
2. `bindings` 含两个列表：`cbuffers`（每个 `{name, register, space}`）与 `static_samplers`（每个 `{name, slot, space}`）。
3. 图片所指的「RayGen 0」对应 `name == "RayGen_fb3c7b0c9e02fb73"`（SBT raygen 区域里的那条记录），它的 `bindings` 与上图逐项、逐序一致。

**看点（数据源辨析——这是本用例最重要的结论）**

- 这份 CBV 列表的**语义名**（`_RootShaderParameters` 等）与 `[register, space]` 标注，真实来源是 **RayGen shader 的 DXIL 反汇编 `Resource Bindings` 表**（`hlsl_bind` 列 `cb0,space1` / `cb1,space4` / `s1,space1000`），**不是** root signature 参数表。
- 原因：global root signature（`3889`）的 root CBV 参数只有 `register` + `space`，**没有语义名**；语义名只有 shader 反射才知道。而 raytracing shader 不是 PSO，`draw.shader()` 拿不到它，所以必须走 `DxilExport.dxil_blob_index` 直接反汇编 DXIL 库。
- 因此工具经 `capture.export_disassembly`（读 blob → dxcompiler 反汇编）→ `parse_resource_bindings`（拆 `register`/`register_space`）→ `_export_binding_view`（按 `(register, space)` 升序排序）才复现出与 PIX 面板一致的顺序（尤其 `View` 的 `[1, space=4]` 落在 `SceneTexturesStruct` 的 `[1, space=1]` 之后）。

---

## 用例 7：`pass-bindings` 对光追 pass（过期免责声明的修复）

用户按最自然的路径提问「global id 5312 / 5367 的 pass 绑定了哪些资源」，直觉命令就是
`pass-bindings --global-id <id>`。改动前该命令返回的是一份**看着完整、实则劝退**的答案。

**改动前后对比**

| 字段 | 改动前 | 改动后 |
|------|--------|--------|
| `stages` | `[]` | `['ANYHIT', 'CLOSESTHIT', 'MISS', 'RAYGEN']` |
| `exports` / `hit_groups` | 缺失 | 5312→8/4，5367→84/58 |
| `pipeline_note` | "State objects are **not yet modelled**" | 指向 `exports` 的真实读法 |
| `descriptor_tables` 空值语义 | 无解释，读作"解析失败" | `rasterisation_fields_note` 明示为结构性空 |
| `binding_shape` | 无 | `raytracing` / `rasterisation` |
| RayGen CBV 面板 | 拿不到 | 9 CBV + 2 静态采样器，与 GUI 同序 |

**根因（本用例最重要的结论）**

`pass-bindings` 无条件走光栅路径：只读 `draw.shaders`（光追恒为空），从不看
`draw.state_object`，再用一句**阶段一至四落地前写下的**注释把这个缺口解释成"状态对象尚未建模"。
而同一截帧上 `shader-bindings` 早已解出全部 export。**能力是有的，工具却主动劝退了调用方。**

过期的能力声明比没有声明更糟：报错会让人重试，而一句自信的"这里没有"会直接终止调查。

**修复方式**

光追绑定视图抽到 `src/pix_tool_set/tools/_raytracing_bindings.py`，由 `shader-bindings` 与
`pass-bindings` 共用同一个 builder（用例 4 的一致性断言即为此设的护栏）；顺带修掉
`per_pso` 去重按 `pso_id` 折叠光追 dispatch 的问题（光追 `pso_id` 恒为 `None`，
同一 pass 内多个 dispatch 会被误折叠成一条），改为按 state object id 归并。

命令示例：

```powershell
pix-tool-set pass-bindings --global-id 5312
pix-tool-set pass-bindings --global-id 5367 --stage RAYGEN
```

---

## 用例 8：全仓「光栅致盲」扫描（三项新缺陷）

修完 `pass-bindings` 后按三种致盲模式全仓扫描：①只读 `draw.shaders`/`pipeline_state`（光追恒空）、
②按 `pso_id` 分组去重（光追恒 `None`）、③只走 descriptor table 而漏 root 绑定。
88 个工具中仅 5 个模块懂光追，扫出**三项真实缺陷**，其中两项比原缺陷更危险（原缺陷至少返回 `partial`）。

| 工具 | 改动前 | 危险等级 | 改动后 |
|------|--------|----------|--------|
| `analyze-pass` | `success` + 全零：`draw_count=0`/`triangles=0`/`shader_mix={}`/`inputs`+`outputs` 全空 | **最高**：自信的错误答案 | `pass_kind=raytracing` + `raytracing` 块（1 dispatch / 2 rays / 84 export）+ `shader_mix` 按 DXR 阶段填充 |
| `pass-shader-source` | `error: This pass binds no such stage` —— **字面就是错的** | 高：事实错误 | 报错点明 state object 3930 有 84 个 export，并给出 `describe-state-object` / `pass-bindings` / `shader-edit-begin` 三条可用路径 |
| `pipeline-state` | CLI 拒收 `--global-id`（内部 `resolve_draw` 本就支持，只是参数漏注册） | 中：GUI 抄来的 ID 用不了 | 补齐 `global_id`，`--global-id 5367` → `resolved_kind=raytracing` / 84 export |

**顺带修好的通用缺陷（不限光追）**：`analyze-pass` 的 `inputs`/`outputs` 只遍历
`draw.srvs`/`draw.uavs`，而这两者只走 descriptor table 的 `resolved_views`，**root 级绑定被整体漏算**。
补上 root SRV/CBV/UAV 后，光栅 compute pass `TileClassificationBuildLists` 的 outputs 由 3 增至 4，
与该 CS 声明的 `u0..u3` 数量对齐（原先少算一个 root UAV）。

**已正确处理、无需改动**：`draw-state`、`pass-values`（已遍历 `root_bindings`，其 `partial` 理由本身成立）、
`pipeline-state` 的 `_raytracing_pipeline` 分支逻辑（缺的只是参数注册）。

命令示例：

```powershell
pix-tool-set analyze-pass --global-id 5367
pix-tool-set pipeline-state --global-id 5367
```

---

## 用例 8：光追分析可用性专项修复（2026-08）

一次真实端到端分析暴露的 8 处工具层问题，修复后的验证用例。
完整根因与改法见 [光追工具链修复报告](/Doc/raytracing-toolchain-fixes.md)。

### 8.1 全局参数可后置

```powershell
pix-tool-set list-tools --brief --flat --compact
```

| 项 | 改动前 | 改动后 |
|---|---|---|
| 退出码 | `2`，`unrecognized arguments: --compact` | `0` |
| `--flat` | 无此参数 | `tool_count=88`，`tools` 为一维数组 |

**看点**：`--compact` / `--traceback` / `--output-json` 现在挂在每个子 parser 上，
命令名前后皆可。`--pixtool` 仍只在命令名之前——若干工具自己的 schema 已声明同名参数，
parent 再声明会触发 argparse 冲突。

### 8.2 `--output-json` 绕开 UTF-16 陷阱

```powershell
pix-tool-set analyze-raytracing --output-json rt.json
```

| 项 | shell 重定向（`>`） | `--output-json` |
|---|---|---|
| 编码 | UTF-16（PowerShell 行为） | UTF-8 |
| `json.load(..., encoding='utf-8')` | `UnicodeDecodeError: 0xff` | 正常 |

### 8.3 `pass-shader-source` 支持光追（影响最大）

```powershell
pix-tool-set pass-shader-source --pass-name "ReflectionHardwareRayTracingRGS default" --stage RAYGEN
```

| 字段 | 改动前 | 改动后 |
|---|---|---|
| `status` | `error: shader_not_found` | `success` |
| `source_tier` | 无 | `pdb-hlsl` |
| `entry_point` | 无 | `LumenReflectionHardwareRayTracingRGS` |
| `aliased_export_count` | 无 | `8` |
| `binding_shape` | 无 | `raytracing` |

**看点**：改动前该函数**已经拿到**了 state object id 与 export 数量，甚至把正确命令拼进了
suggestion —— 信息齐备却选择失败。现在直接从 state object 的 DXIL library exports 恢复源码，
并按 HLSL 入口点去重（同一入口点会被编译进多个 collection、产生多个 mangled export，
不去重会把同一份源码重复输出几十遍）。

### 8.4 光追 dispatch 在事件视图可检索

```powershell
pix-tool-set list-actions --effective-kind dispatch_rays --detail
```

| 过滤方式 | 结果 |
|---|---|
| `--kind raytracing` | 3 条，**全是** AS 构建，dispatch 一个不见 |
| `--kind dispatch_rays` | 0 条（导出里没有字面 `DispatchRays`） |
| `--effective-kind dispatch_rays` | ✅ 2 条：gid 5311、5366 |

**看点**：`find-draw-calls` 早有 `effective_kind`，`list-actions` 却没有，导致同一份数据
在两个视图里可见性不一致。

### 8.5 `locate-event` 接受加速结构构建

```powershell
pix-tool-set locate-event --global-id 3752
```

| 字段 | 改动前 | 改动后 |
|---|---|---|
| `status` | `error: event_not_found` | `partial` |
| `command.api` | —（原报错里写 `<unknown>`） | `BuildRaytracingAccelerationStructure` |
| `command.source` | 无 | `CommandLists_000.cpp:56277` |
| `neighbouring_draws` | 无 | 前 gid 3742 / 后 gid 3808 |
| `acceleration_structure_build` | 无 | `top_level` / 3 instances / dest 3223 |

**连带修复**：`command_by_global_id` 原先只在 GlobalId 注释后 **5 行**窗口内、
且只匹配 `GetCommandList(N)->Api(` 一种形式。AS 构建要先填一串 D3D12 desc 结构体，
调用行远在窗口之外且为局部变量形式，故恒判为 `<unknown>`。现扩至 60 行、
增加局部变量形式，并以"遇到下一个 GlobalId 即停"防止借用后一条命令的 API 名。

### 8.6 无 draw 的 marker 给出精确引导

```powershell
pix-tool-set pass-info --pass-name "RayTracingBuildScene"
```

改动前：`No pass matches 'RayTracingBuildScene'` + "Run list-passes"（无指向性，
读起来像"这个名字不存在"）。

改动后仍是 `pass_not_found`（语义正确：它确实不是 pass），但 suggestion 变为：

```
'RayTracingBuildScene' is a marker (queue_id=18403) that encloses no draw call,
so it forms no pass. It contains 3 acceleration structure build(s)
(global_id 3752, 3753, 3754). Use analyze-acceleration-structures for the full
description, list-raytracing-work for the ordered timeline, or
locate-event --global-id <id> for one build's context.
```

**看点**：pass = "包含至少一个 draw 的 marker" 这个定义本身合理，
问题只在于报错没区分"名字错了"与"这个概念不适用"。

### 8.7 `analyze-raytracing` 一次拿到全貌

```powershell
pix-tool-set analyze-raytracing
pix-tool-set analyze-raytracing --detail
```

实测输出（会话 `Tiled`）：

```
summary: ray_dispatches=2, acceleration_structure_builds=3, tlas_instances_total=3,
         state_objects_declared=81, shader_binding_tables=2, inline_raytracing_passes=3

gid 5311 | ReflectionHardwareRayTracingRGS default     | 232 rays | 0.1889 ms
         exports=17  hit_groups=4   unique_entry_points=4  sbt=1415_1
gid 5366 | ReflectionHardwareRayTracingRGS hit-lighting |   2 rays | 0.2463 ms
         exports=84  hit_groups=58  unique_entry_points=8  sbt=1415_2

builds: (3752, top_level, 3 instances) (3753, top_level, 0) (3754, top_level, 0)
blobs:  655 个 / 322,986,432 字节
inline: LumenDirectLightingHardwareRayTracingCS      (gid 4796)
        HardwareRayTracingCS <indirect> 4x4 probes   (gid 4844)
        HardwareRayTracingCS default                 (gid 5021)
```

三条必须验证的语义约束：

| 约束 | 验证点 |
|---|---|
| 绝不隐式触发 GPU 回放 | 走 `ensure_timing(allow_export=False)`；无缓存时 `timing.available=false` 且照常返回结构 |
| inline 识别是证据非声明 | 每条带 `evidence: "pass_name"`；`*IndirectArgs*` pass 被排除（只填间接参数、不发射光线，计入会高估光追占比：6 → 3） |
| 沿用既有诚实性约定 | `stage_source_note` 与 BLAS geometry 说明原文照带，聚合视图不省略限制条件 |

**看点**：`unique_entry_points` 与 `exports` 的差值本身即信息 ——
84 个 export 仅对应 8 个真实 HLSL 入口点，说明 UE5 把同一 shader 编进了大量 collection。

### 8.8 回归验证

```powershell
python -m pytest tests	est_shader_scope.py tests	est_editledger.py tests	est_detect_patches.py -q
# 24 passed
```

既有命令全部 `status=success`，未受影响：
`list-raytracing-work`、`describe-state-object`、`describe-shader-table`、
`analyze-acceleration-structures`、`list-actions --kind raytracing`、`frame-stats`。

光栅路径未被 DXR 分支影响：

```powershell
pix-tool-set pass-shader-source --pass-name "Light Grid Create" --stage CS
# → success / pdb-hlsl / 走 rasterisation 默认分支
```

### 8.9 确认无解、保持现状的两项

| 项 | 原因 | 现有处理 |
|---|---|---|
| BLAS 三角形/顶点数 | 导出的是驱动私有序列化 blob（`DESERIALIZE` 重放），源数据无 `D3D12_RAYTRACING_GEOMETRY_DESC`；blob 大小是压缩结构 | `null` + `geometry_note`，**正确**：凭 blob 大小反推会产出"看起来是测量值的推测值" |
| postbuild info | 仅当应用调用过 `EmitRaytracingAccelerationStructurePostbuildInfo` 才存在，UE5 这一帧未调用 | note 明确区分"应用没问"与"解析失败" |

---

## 对应的验收脚本

以下命令均可直接运行（均基于 `Tiled` 会话）：

```powershell
python tests\verify_state_object.py            # 43 项，含展开逻辑负向断言
python tests\verify_shader_table.py            # 37 项，含两阶段联合校验（孤儿标识=0）
python tests\verify_acceleration_structures.py # 34 项，含几何数恒为 None
python tests\verify_raytracing_tools.py        # 51 项，含 degrade 码、分页契约与 RayGen 绑定视图对齐
python tests\verify_raytracing_pass_bindings.py # 47 项，钉住过期免责声明与两工具一致性
python tests\verify_raytracing_pass_analysis.py # 27 项，钉住静默空答、错误消息与 root 绑定漏算
```

---

## 相关文档

- [DXR 光追适配计划](/Doc/dxr-raytracing-adaptation-plan.md)
- [光追工具链修复报告](/Doc/raytracing-toolchain-fixes.md)
- [Tiled.wpix 分析报告](/Doc/Tiled-wpix-分析报告.md)
