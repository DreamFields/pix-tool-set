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

## 对应的验收脚本

以下命令均可直接运行（均基于 `Tiled` 会话）：

```powershell
python tests\verify_state_object.py            # 38 项，含展开逻辑负向断言
python tests\verify_shader_table.py            # 37 项，含两阶段联合校验（孤儿标识=0）
python tests\verify_acceleration_structures.py # 34 项，含几何数恒为 None
python tests\verify_raytracing_tools.py        # 48 项，含 degrade 码与分页契约
```

---

## 相关文档

- [DXR 光追适配计划](/Doc/dxr-raytracing-adaptation-plan.md)
- [Tiled.wpix 分析报告](/Doc/Tiled-wpix-分析报告.md)
