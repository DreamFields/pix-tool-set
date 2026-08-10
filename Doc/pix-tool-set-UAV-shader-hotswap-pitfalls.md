# pix-tool-set UAV Shader 热替换踩坑记录

> 基于 Tiled.wpix 抓帧文件，对 TileClassificationMark pass（queue id=18704）的 Compute Shader 进行热替换，并使用 read-uav 读取 GPU 写入内容的完整实践记录。
>
> 日期：2026-08-05
> 环境：Windows + pix-tool-set + Visual Studio 2026 + CMake
> 抓帧文件：C:\Users\vinmeng\Desktop\ManyLights\debug\Tiled.wpix（约 2.3GB）
> PDB 路径：F:\JL_TMR\UnrealEngine\Games\JyGame\Saved\ShaderSymbols\PCD3D_SM6

---

## ⚠️ 事后更正（2026-08-05 复盘）

本文记录的 11 个问题中，**问题 10 和问题 11 是错误归因**，其结论不要照做：

- **真实根因**：`shader-edit-apply --patch` 报 `already_patched` 时，`raise` 发生在写
  `.dxil` **之前**，所以补丁被拒时 `.dxil` 保持旧内容，而 `CreatePSOs.cpp` 里的 override
  仍在读它 → 回放当然是旧结果。这被误判为「CMake 增量构建没拾取 dxil 变化」（问题 10），
  又进一步误判为「HLSL 编译器优化 / R10G10B10A2 精度」（问题 11）。
- **「删 exe + 删 obj + --force-reconfigure」并未修复任何问题**，纯属多烧了两次
  160~200 秒的构建。真正让结果变化的是「恢复 .orig 后重新 patch 成功」。
- 注入的 override 是运行时 `ReadFileBytes` + `static`，**换 .dxil 只需重启 exe，不需重编译**。
- 问题 11 的两次 HLSL 修改（交换 `Normal.x/y` 与交换打包后的 R/G）**语义等价**，
  结果表里 R/G 均值恰好互换，正说明第一次只是没落地。
- **最大的浪费**：全程未用 `shader-edit-diff`。该工具本就一次构建服务两次回放并直接产出
  量化差异，Step 3/6/7 可压缩成一条命令。

### 已在工具侧修复（本文多数问题不再会发生）

- `shader-edit-apply` 新增 `--force`：幂等重打补丁，不再需要手工 `Copy-Item` 恢复 `.orig`（问题 9）。
- `already_patched` 被拒时也会刷新 `.dxil`，并在 `details.bytecode_refreshed` 说明（问题 10 根因）。
- 返回体新增 `previous_shader_hash` 与 `hash_changed`，hash 未变化时主动告警（问题 11 的检测手段）。
- `read-uav` / `shader-edit-diff` 的 `--skip-build` 在 probe 刚注入时**自动降级为完整构建**（问题 6）。

现推荐的精简工作流见 [SKILL.md](../skills/pix-shader-hotswap/SKILL.md)。

---

## 目录

1. [工作流概览](#1-工作流概览)
2. [问题 1：CLI 参数名与文档示例不一致](#2-问题-1cli-参数名与文档示例不一致)
3. [问题 2：export-uav-slice 的 --register 参数不存在](#3-问题-2export-uav-slice-的---register-参数不存在)
4. [问题 3：export-uav-slice 用 --name 匹配 UAV 时返回多个候选](#4-问题-3export-uav-slice-用---name-匹配-uav-时返回多个候选)
5. [问题 4：export-uav-slice 导出的 UAV 数据全为 0](#5-问题-4export-uav-slice-导出的-uav-数据全为-0)
6. [问题 5：read-texture-pixels / export-texture 无法导出 UAV](#6-问题-5read-texture-pixels--export-texture-无法导出-uav)
7. [问题 6：read-uav 首次使用 --skip-build 失败](#7-问题-6read-uav-首次使用---skip-build-失败)
8. [问题 7：read-uav 默认 settle-seconds 不足导致超时](#8-问题-7read-uav-默认-settle-seconds-不足导致超时)
9. [问题 8：shader-edit-apply 不加 --patch 只编译不写入](#9-问题-8shader-edit-apply-不加---patch-只编译不写入)
10. [问题 9：shader-edit-apply --patch 报 already_patched](#10-问题-9shader-edit-apply---patch-报-already_patched)
11. [问题 10：CMake 增量构建未检测到 shader 字节码文件变化](#11-问题-10cmake-增量构建未检测到-shader-字节码文件变化)
12. [问题 11：交换 Normal.x/y 后 UAV 数据无变化](#12-问题-11交换-normalxy-后-uav-数据无变化)
13. [最终正确工作流](#13-最终正确工作流)
14. [关键命令速查表](#14-关键命令速查表)

---

## 1. 工作流概览

```
session-open -> session-set-pdb-dirs -> find-pass -> pass-bindings
      |
shader-edit-begin -> 手动编辑 HLSL -> shader-edit-apply --patch
      |
read-uav (注入 probe -> 构建 -> 运行 -> GPU readback)
      |
对比 before / after 的 .bin / .png
```

---

## 2. 问题 1：CLI 参数名与文档示例不一致

### 现象

多个工具的 CLI 参数名与直觉或文档示例中的写法不同，导致首次调用报 unrecognized arguments 错误。

### 涉及工具及正确参数

| 工具 | 错误写法 | 正确写法 | 说明 |
|------|---------|---------|------|
| session-set-pdb-dirs | --dirs "F:\..." | --pdb-dirs "F:\..." | 参数名带 pdb- 前缀 |
| find-pass | --queue-id 18704（从多队列 GUI 抄来） | --global-id 18704 或 --draw-index 2606 | 从 PIX GUI 抄 id 推荐用 `--global-id`（跨队列唯一）；`--queue-id` 只用于工具自己输出过的 id（详见 README 第八章） |
| export-uav-slice | --register u1 | --name RWNormalTexture 或 --resource-id 3032 | 无 --register 参数，用 UAV 声明名或 resource_id |

### 解决方案

在首次使用任何工具前，先用 pix-tool-set describe <tool-name> 查看其参数 schema：

```bash
pix-tool-set describe session-set-pdb-dirs
pix-tool-set describe find-pass
pix-tool-set describe export-uav-slice
```

describe 返回的 JSON 中 parameters.properties 列出了所有合法参数名及其类型和描述。

### 教训

**不要猜测参数名**，始终先 describe。

---

## 3. 问题 2：export-uav-slice 的 --register 参数不存在

### 现象

```bash
pix-tool-set export-uav-slice --queue-id 18704 --register u1 --output "...\before.png"
# error: unrecognized arguments: --register u1
```

### 原因

export-uav-slice 没有 --register 参数。它支持两种方式指定 UAV：
- --name：UAV 声明名（如 RWNormalTexture），需要配合 --queue-id 以读取 shader 声明
- --resource-id：直接用 resource id（整数）

### 解决方案

先用 pass-bindings --queue-id 18704 查看 UAV 绑定信息，获取 resource_id，然后用 --resource-id 导出：

```bash
pix-tool-set export-uav-slice --resource-id 3032 --output "...\before" --pixels 16
```

---

## 4. 问题 3：export-uav-slice 用 --name 匹配 UAV 时返回多个候选

### 现象

```bash
pix-tool-set export-uav-slice --queue-id 18704 --name RWNormalTexture --output "...\before"
# error: UAV_not_found - "No UAV matches 'RWNormalTexture'."
# suggestion: "Could not narrow it to one resource; candidates: [448, 487, 488, ...]"
```

返回了数百个候选 resource_id，无法定位到唯一的 RWNormalTexture。

### 原因

--name 在某些情况下无法从 shader 声明中唯一解析出 resource_id。这可能与 PIX 导出的 C++ replay 项目中 descriptor 的组织方式有关——同一个 UAV 名可能在不同 pass 中指向不同的 resource。

### 解决方案

先用 pass-bindings --queue-id 18704 获取该 pass 的完整绑定表，从中找到 RWNormalTexture 对应的 resource_id，然后用 --resource-id 精确导出。

```bash
# Step 1: 获取绑定信息
pix-tool-set pass-bindings --queue-id 18704
# 输出中找到: U1 / RWNormalTexture / resource_id: 3032

# Step 2: 用 resource_id 导出
pix-tool-set export-uav-slice --resource-id 3032 --output "...\before" --pixels 16
```

---

## 5. 问题 4：export-uav-slice 导出的 UAV 数据全为 0

### 现象

export-uav-slice 成功导出了 UAV 的 .bin 文件，但统计显示所有像素值均为 0：

```json
{
  "min": 0,
  "max": 0,
  "nonzero": 0,
  "distinct_values": 1
}
```

诊断信息：
```
"Slice 0 is entirely zero in the recorded bytes."
"This UAV is filled by the dispatch on the GPU, and resources.bin only holds uploads and CPU writes, so the written values are not present."
```

### 原因

**这是 pixtool 的设计限制，不是 bug。**

export-uav-slice 从 resources.bin（PIX 导出的 C++ replay 项目中的资源数据文件）中读取纹理字节。resources.bin 只记录：
- CPU 上传的初始数据（Upload 堆）
- CPU 页面写入

但 **GPU 在 dispatch 执行期间写入 UAV 的内容不会被记录**。因为 .wpix 记录的是 API 调用序列，不是 GPU 执行后的内存快照。一个 UAV 如果是由 Compute Shader dispatch 写入的，其初始内容为空（或未初始化），GPU 写入的值只存在于 GPU 显存中。

### 解决方案

使用 read-uav 工具代替 export-uav-slice。read-uav 通过在 C++ replay 项目中注入一个 GPU readback probe，在 replay 运行时从 GPU 显存读取 UAV 的实际写入内容。

```bash
pix-tool-set read-uav --queue-id 18704 --name RWNormalTexture --output "...\uav_before" --pixels 16 --keep-probe --settle-seconds 300
```

### 关键区别

| 工具 | 数据来源 | 能否看到 GPU 写入 |
|------|---------|------------------|
| export-uav-slice | resources.bin 的记录字节 | 不能 |
| read-uav | GPU replay 后的 readback heap | 能 |

---

## 6. 问题 5：read-texture-pixels / export-texture 无法导出 UAV

### 现象

尝试用 read-texture-pixels 和 export-texture 导出 UAV 纹理，均报错：

```
pixtool error: PIXTOOL9 - Requested Render Target with specified index does not exist
pixtool currently only supports outputting bound Render Targets.
```

### 原因

read-texture-pixels 和 export-texture 底层调用的是 pixtool 的 save-resource 功能，而 pixtool **只支持导出绑定的 Render Target（RTV）**，不支持 UAV。

Compute Shader dispatch 的输出是 UAV 而非 RTV，所以这些工具无法工作。

### 解决方案

对于 UAV 内容的读取，唯一可行的方式是 read-uav：

```bash
pix-tool-set read-uav --queue-id 18704 --name RWNormalTexture --output "...\out" --pixels 16
```

### 工具选择指南

| 场景 | 推荐工具 |
|------|---------|
| 读取 Render Target 内容 | read-replay-target / export-texture / read-texture-pixels |
| 读取 Depth Buffer | save-render-target --depth |
| 读取 UAV 初始字节（CPU 上传） | export-uav-slice |
| **读取 UAV 的 GPU 写入内容** | **read-uav**（唯一方式） |

---

## 7. 问题 6：read-uav 首次使用 --skip-build 失败

### 现象

第一次调用 read-uav 时加了 --skip-build，结果 probe 未完成：

```json
{
  "probe_injection": { "already_installed": false, "rebuild_needed": true },
  "build": { "skipped": true },
  "run": { "probe": { "finished": false } }
}
```

诊断信息：
```
"--skip-build was given but the probe had to be injected just now, so the existing executable does not contain it and will produce no dump. Re-run without --skip-build."
```

### 原因

read-uav 的工作原理是：在 C++ replay 项目的源码中注入一个 readback probe（PixToolSetProbe.cpp），修改 RenderFrame.cpp 使其在帧执行后调用 probe 的 readback 函数，修改 CMakeLists.txt 加入新源文件。

如果是首次注入 probe（already_installed: false），则现有可执行文件不包含 probe 代码，--skip-build 跳过构建会导致 probe 不会被编译进去。

### 解决方案

**首次运行 read-uav 时不要加 --skip-build。** 只有在 probe 已经注入并编译过（already_installed: true）的情况下，--skip-build 才有效。

如果需要加速后续调用，可以使用 --keep-probe 保留 probe 代码不删除，这样后续调用可以复用已编译的可执行文件。

---

## 8. 问题 7：read-uav 默认 settle-seconds 不足导致超时

### 现象

首次 read-uav 运行（无 --skip-build），构建成功后运行 replay，但 probe 在 settle 窗口内未完成：

```json
{
  "run": { "seconds": 240.1, "probe": { "finished": false }, "dump": null }
}
```

诊断信息：
```
"The replay produced no readback dump within the settle window."
"Raise --settle-seconds; a multi-gigabyte capture can take minutes before its first frame."
```

### 原因

默认 --settle-seconds 为 240 秒（4 分钟）。对于约 2.3GB 的大型抓帧文件（Tiled.wpix），replay 需要加载数 GB 的资源数据，GPU 首帧渲染前需要较长的初始化时间。240 秒可能不够。

### 解决方案

将 --settle-seconds 提高到 300 或更高：

```bash
pix-tool-set read-uav --queue-id 18704 --name RWNormalTexture --output "...\out" --pixels 16 --keep-probe --settle-seconds 300
```

实测中，构建完成后 replay 运行约 52-60 秒 probe 就完成了。但首次构建本身需要约 160-200 秒，加上运行时间总计可能接近默认 240 秒上限。设置 300 秒留有足够余量。

### 注意

--settle-seconds 是指 **replay 运行后** 等待 probe 完成的时间，不包括构建时间。构建超时由 --build-timeout 控制（默认 1800 秒）。

---

## 9. 问题 8：shader-edit-apply 不加 --patch 只编译不写入

### 现象

```bash
pix-tool-set shader-edit-apply --queue-id 18704 --stage CS --source "...\edited.hlsl"
```

返回 status: success，编译成功，binding check 通过，但诊断信息提示：

```
"Compiled and verified slot-compatible. Nothing was modified; add --patch to write it into the exported replay project."
```

### 原因

shader-edit-apply 有两种模式：
- **不带 --patch**：仅编译 HLSL 并验证 binding 兼容性，不修改任何文件。相当于 "dry run"。
- **带 --patch**：编译 + 验证 + 将新字节码写入 C++ replay 项目的 CreatePSOs.cpp，使其在 replay 时加载编辑后的 shader。

### 解决方案

确保加上 --patch 参数：

```bash
pix-tool-set shader-edit-apply --queue-id 18704 --stage CS --source "...\edited.hlsl" --patch
```

--patch 会：
1. 编译 HLSL 为 DXIL 字节码
2. 验证新 shader 的 resource binding 与原始 shader 完全一致（相同 register、相同 slot）
3. 将字节码写入 edited_CreatePipelineState_<pso_id>_CS.dxil 文件
4. 修改 CreatePSOs.cpp 中的 CreatePipelineState_<pso_id> 函数，使其从文件加载编辑后的字节码而非从 resources.bin 读取
5. 创建 CreatePSOs.cpp.orig 备份

---

## 10. 问题 9：shader-edit-apply --patch 报 already_patched

### 现象

```bash
pix-tool-set shader-edit-apply --queue-id 18704 --stage CS --source "...\edited.hlsl" --patch
# error: already_patched
# "CreatePipelineState_3255 was already patched for CS."
```

### 原因

上一次 --patch 已经修改了 CreatePSOs.cpp，添加了从文件加载编辑字节码的代码。再次 patch 时检测到目标函数已被 patch 过，拒绝重复修改以防止代码混乱。

### 解决方案

从 .orig 备份恢复 CreatePSOs.cpp，然后重新 patch：

```bash
# 恢复原始文件
Copy-Item "...\Tiled.pixcache\cpp\CreatePSOs.cpp.orig" "...\Tiled.pixcache\cpp\CreatePSOs.cpp" -Force

# 重新 patch
pix-tool-set shader-edit-apply --queue-id 18704 --stage CS --source "...\edited.hlsl" --patch
```

### 教训

每次修改 HLSL 后重新 patch 前，都要先恢复 CreatePSOs.cpp。可以将恢复步骤加入工作流脚本中自动化。

---

## 11. 问题 10：CMake 增量构建未检测到 shader 字节码文件变化

### 现象

成功 --patch 后，shader 字节码文件 edited_CreatePipelineState_3255_CS.dxil 已更新（shader hash 不同），read-uav 构建也报告 exit_code: 0，但读取到的 UAV 数据与修改前完全相同（0 字节差异）。

### 原因

CMake/MSBuild 的增量构建机制基于源文件的 **时间戳**。CreatePSOs.cpp 在 --patch 时被修改了，但如果 .obj 文件的时间戳比 .cpp 新，MSBuild 可能跳过重新编译。

更关键的是：CreatePSOs.cpp 中的 patch 代码是 **在运行时从文件读取** shader 字节码（Helpers::ReadFileBytes），而非编译时嵌入。这意味着即使 CreatePSOs.cpp 本身没有重新编译，只要 edited_CreatePipelineState_3255_CS.dxil 文件更新了，运行时应该读取到新的字节码。

但实际无变化的原因可能是：**可执行文件没有被重新生成**，旧的 exe 仍然在运行。或者 CMake 构建系统认为没有任何源文件变化（因为 .dxil 文件不在 CMake 的依赖列表中）。

### 解决方案

强制删除可执行文件和相关 .obj 文件，迫使 CMake 重新编译和链接：

```bash
# 删除可执行文件和目标文件
Remove-Item "...\build\Release\UnrealEditor.exe" -Force -ErrorAction SilentlyContinue
Remove-Item "...\build\CreatePSOs.cpp.obj" -Force -ErrorAction SilentlyContinue

# 或者使用 --force-reconfigure 完全重置构建目录
pix-tool-set read-uav --queue-id 18704 --name RWNormalTexture --output "...\out" --force-reconfigure
```

### 教训

当 shader patch 后看不到效果时，**不要信任增量构建**。删除 exe + obj 或使用 --force-reconfigure 确保完全重建。

---

## 12. 问题 11：交换 Normal.x/y 后 UAV 数据无变化

### 现象

第一次修改 HLSL：在 PackNormalAndShadingInfo 调用前交换 Info.Normal.x 和 Info.Normal.y：

```hlsl
Info.Normal = float3(Info.Normal.y, Info.Normal.x, Info.Normal.z);
RWNormalTexture[Coord.SvPosition] = PackNormalAndShadingInfo(Info);
```

强制重建后，read-uav 返回的 UAV 数据与修改前 **完全相同**（0 字节差异）。

### 原因分析

经过分析，有两个可能的原因：

1. **R10G10B10A2_UNORM 格式精度问题**：PackNormalAndShadingInfo 将法线从 [-1,1] 映射到 [0,1]，然后打包到 R10G10B10A2 格式。R 通道有 10 bit（1024 级），G 通道也有 10 bit。如果法线的 x 和 y 值在某些区域相同或非常接近，交换后打包结果可能相同。但从统计数据看，修改前 R 通道 mean=0.841 而 G 通道恒定 0.4995，两者明显不同，所以这个解释不成立。

2. **编译器优化**：HLSL 编译器（-O1 优化）可能将 Info.Normal = float3(Info.Normal.y, Info.Normal.x, Info.Normal.z) 和后续的 PackNormalAndShadingInfo 调用内联优化，在某些情况下可能识别出交换操作并做了某种等价变换。但这也不太可能，因为交换 x/y 确实改变了语义。

3. **最可能的原因**：虽然 shader 源码修改了，但第一次强制重建时 CMake 增量构建可能仍然没有正确拾取变化（见问题 10）。需要确认 edited_CreatePipelineState_3255_CS.dxil 文件的 hash 确实发生了变化。

### 解决方案

改为在 **打包后** 直接交换 R 和 G 通道（float4 的 x 和 y），绕过可能的编译器优化和格式精度问题：

```hlsl
Info.Normal = float3(Info.Normal.y, Info.Normal.x, Info.Normal.z);
float4 PackedNormal = PackNormalAndShadingInfo(Info);
// 在打包结果上交换 R 和 G 通道
RWNormalTexture[Coord.SvPosition] = float4(PackedNormal.g, PackedNormal.r, PackedNormal.b, PackedNormal.a);
```

这次修改后，新编译的 shader hash 从 80c51be5... 变为 57ebfa3b...（确认编译器确实看到了不同代码），read-uav 返回的数据也发生了明显变化：

| 通道 | 修改前 mean | 修改后 mean |
|------|------------|------------|
| R | 0.841 | 0.4995 |
| G | 0.4995 | 0.841 |
| B | 0.658 | 0.658 |

### 教训

- 修改 shader 后，检查 shader-edit-apply 返回的 new_container.shader_hash 是否与之前不同，确认编译器确实编译了新代码。
- 如果修改法线值后看不到效果，尝试在 **打包函数输出之后** 直接操作 float4 通道，避免编译器优化和格式精度的影响。
- 用 Python 脚本做字节级 diff 确认 before/after 数据是否真的有变化。

---

## 13. 最终正确工作流

以下是经过所有踩坑后验证可行的完整工作流：

### Step 1: 打开会话并设置 PDB

```bash
pix-tool-set session-open --capture "C:\...\Tiled.wpix"
pix-tool-set session-set-pdb-dirs --pdb-dirs "F:\...\ShaderSymbols\PCD3D_SM6"
```

### Step 2: 查找 pass 并获取绑定信息

```bash
pix-tool-set find-pass --queue-id 18704
pix-tool-set pass-bindings --queue-id 18704
# 记录 RWNormalTexture 的 resource_id（如 3032）和 register（如 u1）
```

### Step 3: 读取修改前的 UAV 内容

```bash
# 首次运行不要加 --skip-build，不要加 --force-reconfigure（除非构建出问题）
pix-tool-set read-uav --queue-id 18704 --name RWNormalTexture --output "...\uav_before" --pixels 16 --keep-probe --settle-seconds 300
```

### Step 4: 导出并编辑 HLSL

```bash
pix-tool-set shader-edit-begin --queue-id 18704 --stage CS --pdb-dirs "F:\...\PCD3D_SM6" --output "...\shader_edit"
# 编辑 .hlsl 文件
```

### Step 5: 编译并 patch shader

```bash
# 如果之前 patch 过，先恢复
Copy-Item "...\CreatePSOs.cpp.orig" "...\CreatePSOs.cpp" -Force

# 编译 + patch
pix-tool-set shader-edit-apply --queue-id 18704 --stage CS --source "...\edited.hlsl" --patch
# 检查返回的 shader_hash 是否变化
```

### Step 6: 强制重建并读取修改后的 UAV

```bash
# 删除 exe 和 obj 强制重建
Remove-Item "...\build\Release\UnrealEditor.exe" -Force -ErrorAction SilentlyContinue

# 读取修改后 UAV（probe 已通过 --keep-probe 保留）
pix-tool-set read-uav --queue-id 18704 --name RWNormalTexture --output "...\uav_after" --pixels 16 --keep-probe --settle-seconds 300
```

### Step 7: 对比

```bash
# Python 字节级 diff
python -c "b=open('...before.bin','rb').read(); a=open('...after.bin','rb').read(); diff=sum(1 for i in range(len(b)) if b[i]!=a[i]); print(f'Different bytes: {diff}')"
```

---

## 14. 关键命令速查表

| 操作 | 命令 |
|------|------|
| 打开会话 | pix-tool-set session-open --capture "<path>.wpix" |
| 设置 PDB | pix-tool-set session-set-pdb-dirs --pdb-dirs "<path>" |
| 查找 pass | pix-tool-set find-pass --queue-id <id> |
| 查看绑定 | pix-tool-set pass-bindings --queue-id <id> |
| 导出 HLSL | pix-tool-set shader-edit-begin --queue-id <id> --stage CS --pdb-dirs "<path>" --output "<dir>" |
| 编译+patch | pix-tool-set shader-edit-apply --queue-id <id> --stage CS --source "<hlsl>" --patch |
| 读取 UAV（GPU readback） | pix-tool-set read-uav --queue-id <id> --name <uav_name> --output "<dir>" --pixels <n> --keep-probe --settle-seconds 300 |
| 查看工具参数 | pix-tool-set describe <tool-name> |
| 恢复 patch 备份 | Copy-Item "CreatePSOs.cpp.orig" "CreatePSOs.cpp" -Force |
| 强制重建 | Remove-Item "build\Release\UnrealEditor.exe" -Force |

---

## 附录：read-uav 原理说明

read-uav 是 pix-tool-set 中 **唯一** 能读取 Compute Shader dispatch 写入 UAV 内容的工具。其工作原理：

1. **注入 probe**：在 C++ replay 项目中添加 PixToolSetProbe.cpp，修改 RenderFrame.cpp 使其在帧执行后调用 PixToolSetProbeReadback()，修改 CMakeLists.txt 加入新源文件。
2. **构建**：用 CMake 编译修改后的 replay 项目。
3. **运行**：启动 replay 可执行文件，设置环境变量 PIXTS_PROBE_TARGETS=<resource_id> 和 PIXTS_PROBE_OUT=<output_path>。
4. **GPU readback**：replay 执行帧的 API 调用序列，dispatch 在 GPU 上执行并写入 UAV。probe 在帧结束后将目标 UAV 资源复制到 READBACK heap，CPU 从中读取实际写入的字节。
5. **输出**：生成 .bin（原始字节）、.bin.txt（布局 sidecar）、.png（可视化的 RGB PNG）。
6. **清理**：除非指定 --keep-probe，否则从 .orig 备份恢复修改的文件。

与 export-uav-slice 的关键区别：export-uav-slice 从 resources.bin 读取记录的初始字节（CPU 上传），无法看到 GPU 写入；read-uav 通过实际 GPU 重放获取写入后的真实内容。