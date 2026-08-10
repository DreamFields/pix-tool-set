# Tiled.wpix 截帧分析报告

> 分析对象：`C:\Users\vinmeng\Desktop\ManyLights\debug\Tiled.wpix`（2.49 GB，UE5 ManyLights 场景的 Tiled 版本）
> 分析工具：pix-tool-set CLI（会话 `tiled`）
> 截帧规模：22,155 events / 2,786 draw+dispatch+indirect / 363 shaders / 297 PSO / 3,316 resources
> 帧 ID：108407

---

## 一、Pass 组成分析

### 1.1 整体规模

| 项 | 值 |
|---|---|
| Pass 总数 | **419** |
| draw calls | 2,391 |
| dispatches | 208 |
| indirect | 187 |
| triangles | 25,502 |
| compute threads | 16,297,142（约 1,629 万）|
| events | 2,786 |

典型 **compute-bound** 帧：三角形极少（2.5 万），compute 线程数高达 1,629 万。

### 1.2 Pass 类型构成

| 类型 | 数量 | 占比 | 工作量 |
|---|---|---|---|
| 纯 Compute（dispatch/indirect）| 364 | 87% | dispatch=208, indirect=186, threads=16.3M |
| 纯 Graphics（draw）| 54 | 13% | draws=2,390, tri=25,500 |
| 空 Pass | 0 | — | — |

### 1.3 顶层结构

| 顶层 marker 路径 | pass 数 | 说明 |
|---|---|---|
| `Frame 108407 / SceneRender - ViewFamilies / RenderGraphExecute - /ViewFamilies` | 415 | UE5 主帧 3D 渲染 |
| `RenderGraphExecute - Slate` | 4 | Slate UI（编辑器）|

RDG 嵌套极深：大部分 pass 在 marker 树 depth 14–16，最深 17。共 273 种不同 pass 名（带分辨率/参数，几乎每个 compute pass 独特）。

### 1.4 Graphics 重负载其实是编辑器 UI（重要发现）

| pass | 路径 | draws | triangles |
|---|---|---|---|
| `ElementBatch` | SlateUI Title = **GPU Visualizer** | **2,098** | **15,718** |
| `ElementBatch` | SlateUI Title = JGame - Unreal Editor | 231 | 4,564 |

这两个 Slate ElementBatch 合计 2,329 draws / 20,282 tri，占全帧 draw call 的 **97%**、三角形的 **80%**。也就是说，**真实 3D 场景的 graphics pass 极少**：SkyDome 3,968 tri、TranslucencyVolumeBatchComposite 254×2、Lumen CardPage 系列 4~96 tri。

### 1.5 Compute 重负载 Top（按线程数）

| pass | 所在子系统 | threads |
|---|---|---|
| Velocity Combine DilateSceneMotionVectors | **DLSS** | 2,517,760 |
| ClearTextureFloat(RTSceneDepthZ) | Scene | 1,179,648 |
| RasterClear | Nanite::InitContext | 1,179,648 |
| TileClassificationMark | **StochasticLighting** | 1,179,648 |
| TemporalReprojection(1532x764) | **LumenScreenProbeGather** | 1,179,648 |
| VirtualShadowMapProjection(Input:GBuffer) | VirtualShadowMapProjectionMaskBits | 1,179,648 |
| SpatialFilter（3 dispatch）| TranslucencyVolumeLighting | 746,496 |
| ReduceHZB(mips=[0;3] Furthest/Closest) ×2 | BuildHZB | 524,288 |
| UpdateCacheForUsedProbes / AllocateUsedProbes / AllocateProbeTraces | UpdateRadianceCaches | 483,840 |

覆盖 UE5 全套现代管线：**Nanite、Lumen（ScreenProbeGather/SceneUpdate/RadianceCaches）、VSM、DLSS、StochasticLighting、TranslucencyVolume**。

### 1.6 Render Target 写入最频繁

| RT id | 被多少个 pass 写入 |
|---|---|
| 3031 | 20 |
| 718 | 17 |
| 835 | 16 |
| 828 | 15 |
| 755 | 14 |

前 20 个 RT 每个被 7~20 个 pass 复用，说明 RDG 对这些目标的复用密度很高。

### 1.7 与 NoTiled 对比

| 项 | Tiled | NoTiled（README 实测）|
|---|---|---|
| events | 22,155 | 22,118 |
| draw+dispatch+indirect | 2,786 | 2,784 |
| passes | 419 | 416 |
| shaders | 363 | 359 |

规模几乎一致，Tiled 多 3 个 pass，可能来自 tile 相关的额外 compute（如 `TileClassificationMark`）。

---

## 二、TileClassificationBuildLists pass 的 shader 资源绑定

Tiled.wpix 中有 **3 个** TileClassification 相关 pass，均为单 dispatch（`[24,12,1]` = 18,432 线程），**共用同一个 root signature (id=3005)**，即 UE5 的 FrameResources 全局绑定：

| root | 类型 | 声明 |
|---|---|---|
| root[0] | descriptor_table | SRV × 64（t0–t63, space 0）|
| root[1] | descriptor_table | UAV × 16（u0–u15, space 0）|
| root[2] | root_cbv | cb0（space 0）|

### 2.1 各 pass 的 shader 反射声明（权威，来自 CS 字节码）

#### PASS 145 — `DeferredLightingTileClassificationBuildLists`

- 路径：`StochasticLighting / DeferredLightingTileClassificationBuildLists`
- CS hash `91bb8c98cedd24fba18f42514986f814`（5,172 B）

| bind | name | type | format | dim |
|---|---|---|---|---|
| cb0 | `_RootShaderParameters` | cbuffer | — | — |
| t0 | `DeferredLightingTileBitmask` | texture | u32 | 2d |
| u0 | `RWDeferredLightingTileAllocator` | UAV | struct | r/w |
| u1 | `RWDeferredLightingTileData` | UAV | struct | r/w |

#### PASS 270 — `TileClassificationBuildLists`（LumenReflections 下）

- 路径：`RenderDeferredLighting / DiffuseIndirectAndAO / LumenReflections / TileClassificationBuildLists`
- CS hash `dfb985caaf44c87d6c41960962055981`（5,612 B）

| bind | name | type | format | dim |
|---|---|---|---|---|
| cb0 | `_RootShaderParameters` | cbuffer | — | — |
| t0 | `LumenTileBitmask` | texture | u32 | 2darray |
| u0 | `RWReflectionClearTileIndirectArgs` | UAV | u32 | buf |
| u1 | `RWReflectionClearTileData` | UAV | u32 | buf |
| u2 | `RWReflectionTileIndirectArgs` | UAV | u32 | buf |
| u3 | `RWReflectionTileData` | UAV | u32 | buf |

#### PASS 338 — `TileClassificationBuildLists`（LumenScreenProbeGather/Integrate 下）

- 路径：`…/LumenScreenProbeGather / Integrate / TileClassificationBuildLists`
- CS hash `f3c03e271af26a8486462f9da2af1003`（6,260 B）

| bind | name | type | format | dim |
|---|---|---|---|---|
| cb0 | `_RootShaderParameters` | cbuffer | — | — |
| t0 | `LumenTileBitmask` | texture | u32 | 2darray |
| u0 | `RWIntegrateIndirectArgs` | UAV | u32 | buf |
| u1 | `RWClearUnusedIntegrateTileIndirectArgs` | UAV | u32 | buf |
| u2 | `RWClearUnusedIntegrateTileData` | UAV | u32 | buf |
| u3 | `RWIntegrateTileData` | UAV | struct | r/w |

### 2.2 运行时常量缓冲（cb0，确定）

| pass | cb0 → rid | 资源描述 |
|---|---|---|
| 145 | rid=2955 | buffer, 4 MB |
| 270 | rid=2956 | buffer, 4 MB |
| 338 | rid=2956 | buffer, 4 MB |

这是 FrameResources 的统一 cbuffer（`_RootShaderParameters`）。

### 2.3 运行时 descriptor table 绑定（⚠️ 存在局限）

`shader-bindings` 的 `root_bindings.views` 给出了 descriptor heap 快照，但 **register → rid 的逐项映射不可靠**，原因有二：

1. **pass 338 异常**：root[0] 64 个 view + root[1] 16 个 view **全部是 rid=896**（2048×2048 `A8_UNORM`，明显是 dummy 占位纹理），但 shader 声明 t0 + u0–u3 共 5 个不同资源。这不可能是真实绑定。
2. **pass 270 不匹配**：root[0] views[0]（按线性约定应为 t0）= rid=102（1×1 `B8G8R8A8` dummy），但 shader t0 = `LumenTileBitmask`（u32 2darray）。

这说明 pix-tool-set 报告的 views 是 descriptor heap 的物理连续快照，而 UE5 FrameResources 对 table 做了子分配，**实际 t0/u0 的起始偏移并非 views[0]**。pass 270 的 root[0] 里混入了 UAV view（rid=655/488/500 等），也印证 views 列表跨越了 SRV/UAV table 边界。

#### pass 270 涉及的候选资源（供参考，无法确定对应哪个 register）

| rid | 类型 | 描述 | 推断 |
|---|---|---|---|
| 2489 | texture2d | `R32_UINT` 8192×192 mip7 | 格式最接近 `LumenTileBitmask` 声明的 u32 |
| 2930 | texture2d | `R32_FLOAT` 8192×1024 mip7 | — |
| 655 | buffer | 48 B | 尺寸符合 IndirectArgs |
| 502 | buffer | 16 B | 尺寸符合 IndirectArgs |
| 488 | buffer | 58 MB | 大缓冲，疑似 TileData |
| 500 | buffer | 201 MB | 大缓冲，疑似 TileData |
| 1194 | buffer | 12 MB | — |
| 1285 | buffer | 786 KB | — |
| 1456 | buffer | 196 KB | — |
| 1487 / 1485 | buffer | 64 KB | — |

> 以上均为推断，未经验证。

### 2.4 风险与建议

- **可信部分**：shader 反射声明的资源名/类型/register、root signature 结构、cb0 的 rid —— 这些直接回答了"绑定了哪些 shader 资源"。
- **不可信部分**：descriptor table 内 t/u register 到具体 rid 的逐项映射。若需要精确对应（例如确认 `LumenTileBitmask` 到底是哪个纹理），有两个路径：
  1. 用 `disassemble-shader --draw-index 2606 --stage CS` 看反汇编里的资源索引指令；
  2. 在 PIX GUI 里选中该 dispatch 查看已解析的绑定（PIX 客户端会做 register→resource 解析）。

---

## 三、如何获取某个 pass 的 shader 反射声明（方法论）

以 `TileClassificationBuildLists`（pass 270）为例，完整复现从"pass 名字"到"shader 反射声明资源表"的获取过程。核心是三步：**定位 pass → 拿 draw_index → 调 shader-bindings 解析反射**。

### 3.1 第一步：按名字定位 pass，拿到 draw_index

`list-passes` 支持 `--name` 子串过滤。一个 pass 名字在截帧里可能命中多个 pass（不同 RDG 子系统复用同名 CS），所以先列出全部匹配：

```powershell
pixts list-passes --session tiled --name TileClassificationBuildLists
```

返回 3 个匹配 pass，每个 pass 的关键字段是 `first_draw_index`（该 pass 内第一个 draw/dispatch 的全局索引）。对于单 dispatch pass，`first_draw_index == last_draw_index`，这就是后续工具要用的 `draw_index`。

```
pass_index=270  name=TileClassificationBuildLists
  first_draw_index=2606  last_draw_index=2606
  dispatch_count=1  thread_groups=[24,12,1]  pso_ids=[3321]
```

> **要点**：`draw_index` 不是 PIX 的 global_id，而是 pix-tool-set 内部的 draw 调用列表索引。`shader-bindings` / `draw-state` 都用它定位。`global_id` 现在也接受作为输入（`--global-id`），且是跨队列唯一选择器，推荐从 PIX GUI 抄 id 时使用。若只有 `global_id`，可直接 `draw-state --global-id <N>` 而无需先换算。

### 3.2 第二步：调 shader-bindings 拿反射声明

```powershell
pixts shader-bindings --session tiled --draw-index 2606 --stage CS --max-views 80
```

- `--stage CS`：compute shader。其它可选 VS/PS/GS/HS/DS/AS/MS/LIB。
- `--max-views 80`：**必须调大**。默认 16 会截断 descriptor table（FrameResources 的 SRV table 有 64 项），导致遗漏绑定。设为 ≥ root signature 声明的 num_descriptors 即可。

返回的 JSON 信封里，反射声明在 `data.stages[0].declared_registers`：

```jsonc
{
  "status": "success",
  "data": {
    "draw_index": 2606,
    "pso_id": 3321,
    "root_signature": { "root_signature_id": 3005, "parameter_count": 3, ... },
    "stages": [
      {
        "stage": "CS",
        "shader": {
          "shader_hash": "dfb985caaf44c87d6c41960962055981",
          "byte_size": 5612,
          "debug_name": "dfb985caaf44c87d6c41960962055981.pdb"
        },
        "declared_count": 6,
        "declared_registers": [
          { "name": "_RootShaderParameters",          "type": "cbuffer", "format": "NA",   "dimension": "NA",     "id": "CB0", "hlsl_bind": "cb0", "count": "1" },
          { "name": "LumenTileBitmask",                "type": "texture", "format": "u32", "dimension": "2darray", "id": "T0",  "hlsl_bind": "t0",  "count": "1" },
          { "name": "RWReflectionClearTileIndirectArgs","type": "UAV",    "format": "u32", "dimension": "buf",     "id": "U0",  "hlsl_bind": "u0",  "count": "1" },
          { "name": "RWReflectionClearTileData",       "type": "UAV",    "format": "u32", "dimension": "buf",     "id": "U1",  "hlsl_bind": "u1",  "count": "1" },
          { "name": "RWReflectionTileIndirectArgs",    "type": "UAV",    "format": "u32", "dimension": "buf",     "id": "U2",  "hlsl_bind": "u2",  "count": "1" },
          { "name": "RWReflectionTileData",            "type": "UAV",    "format": "u32", "dimension": "buf",     "id": "U3",  "hlsl_bind": "u3",  "count": "1" }
        ]
      }
    ],
    "root_bindings": [ ... ],          // 运行时 descriptor table 快照（见 2.3 局限）
    "descriptor_heap_ids": [32]
  }
}
```

**字段含义**：

| 字段 | 含义 |
|---|---|
| `hlsl_bind` | HLSL 里的绑定寄存器（cb0/t0/u0…），即 shader 代码里写的 `cb0`/`t0`/`u0` |
| `id` | 反射 ID（CB0/T0/U0…），与 hlsl_bind 对应 |
| `name` | 资源在 shader 源码里的变量名（PIX 从调试信息反射出）|
| `type` | cbuffer / texture / UAV / sampler |
| `format` | 声明的元素格式（u32/struct/NA…）|
| `dimension` | 维度（2d/2darray/buf/r/w/NA…）|
| `count` | 数组大小 |

这一步直接回答了"该 pass 的 shader 绑定了哪些资源"——上表就是答案。`declared_count` 是声明的资源总数。

### 3.3 第三步（可选）：翻译 rid 到资源描述

`shader-bindings` 的 `declared_registers` **不含** 运行时 resource_id（PIX 反射只给声明，不给实例）。要拿运行时实际绑定的 rid，看 `data.root_bindings[*].views`，但如 2.3 所述其 register 映射不可靠。

确定可信的运行时绑定只有 root_cbv（cb0）：

```jsonc
// root_bindings 里 root_index=2 的项
{ "root_index": 2, "binding_kind": "root_cbv", "resource_id": 2956, "views": [] }
```

拿到 rid 后用 `resource-usage` 翻译成人类可读描述：

```powershell
pixts resource-usage --session tiled --resource-id 2956
```

```jsonc
{ "data": { "resource": { "resource_id": 2956, "kind": "buffer", "size_bytes": 4194304,
  "description": "Buffer#2956 4194304 bytes", ... } } }
```

> **注意**：资源属性在 `data.resource` 子对象里，不是顶层。字段包括 `kind`/`format`/`width`/`height`/`depth_or_array_size`/`mip_levels`/`size_bytes`/`description`/`flags`。

### 3.4 流程小结

```
list-passes --name <关键词>
        │  拿到 pass 的 first_draw_index = draw_index
        ▼
shader-bindings --draw-index <idx> --stage CS --max-views 80
        │  data.stages[0].declared_registers  → shader 反射声明（权威）
        │  data.root_signature                → root signature 结构
        │  data.root_bindings[*].views        → 运行时 descriptor heap 快照（⚠️ 映射不可靠）
        │  data.root_bindings[root_cbv].resource_id → cb0 的 rid（可信）
        ▼
resource-usage --resource-id <rid>
        │  data.resource → 资源的 kind/format/尺寸/描述
```

### 3.5 关键坑点

1. **`max_views` 默认值会截断**：`shader-bindings` 默认 16、`draw-state` 默认 12。UE5 FrameResources 的 SRV table 声明 64 项，不调大会丢绑定。建议统一传 `--max-views 80`。
2. **`declared_registers` 与 `root_bindings.views` 是两层数据**：前者是 shader 字节码反射的"声明"（可信），后者是 descriptor heap 的"运行时快照"（在 UE5 子分配 table 下 register 映射不可靠，见 2.3）。回答"绑定了哪些 shader 资源"用前者；回答"某个 register 实际指向哪个 rid"需配合反汇编或 PIX GUI。
3. **pass 名可能重复**：`TileClassificationBuildLists` 命中 3 个 pass（分属 StochasticLighting / LumenReflections / LumenScreenProbeGather），要用 `marker_path` 区分所属子系统。
4. **draw_index、global_id 与 queue_id 的关系**：pix-tool-set 工具接受 `draw_index`（draw 调用列表索引）、`global_id`（PIX 事件 ID，跨队列唯一，推荐从 GUI 抄 id 时使用）和 `queue_id`（导出 CSV 行号，仅覆盖已导出队列）。`list-passes` 同时给出 `first_draw_index` 和 `first_global_id`。多队列截帧（本报告 90 个 async compute action 无 `queue_id`）下 `global_id` 和 `draw_index` 是全量选择器，`queue_id` 只覆盖一条队列。

---

## 四、附录：工具调用速查

### 4.1 会话建立

```powershell
pixts session-open --capture "C:\Users\vinmeng\Desktop\ManyLights\debug\Tiled.wpix" --session tiled
```

导出耗时 125.3s（含 49.1s C++ 导出），缓存目录 `…\Tiled.pixcache\cpp`。

### 4.2 Pass 组成分析

```powershell
pixts list-passes --session tiled --limit 500 --sort-by order
```

返回 419 个 pass 的完整列表（含 marker_path、draw/dispatch/triangle/thread 计数、render_target_ids、pso_ids）。按 name/marker_path 分组聚合后得到上述统计。

### 4.3 TileClassification 绑定分析

```powershell
pixts list-passes --session tiled --name TileClassificationBuildLists
pixts shader-bindings --session tiled --draw-index <idx> --stage CS --max-views 80
pixts draw-state --session tiled --draw-index <idx> --max-views 80
pixts resource-usage --session tiled --resource-id <rid>
```

涉及的 pass：draw_index 2476 / 2606 / 2688。

### 4.4 工具使用注意

- pixts `run <tool> --json-args '{...}'` 在 PowerShell 下因引号转义失败，改用直接子命令形式。
- 复杂查询优先用 Python API（`from pix_tool_set import call_tool`），避免 PowerShell 引号/编码问题，且能拿完整结构化数据。
- `draw-state` / `shader-bindings` 默认 `max_views=12/16` 会截断 descriptor table，需显式传 `max_views=80`。
- `resource-usage` 返回的资源属性在 `data.resource` 子对象（kind/format/width/height/size_bytes/description），不是顶层。
- PowerShell 重定向 `>` 默认 UTF-16 LE BOM，Python 读取需判断 `raw[:2]==b"\xff\xfe"` 后 `decode("utf-16-le")`。
