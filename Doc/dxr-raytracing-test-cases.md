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
- [Tiled.wpix 分析报告](/Doc/Tiled-wpix-分析报告.md)
