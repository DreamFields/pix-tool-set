# 读取 shader 绑定资源的实际数值
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

## resources.bin 的寻址

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

## 帧内 CPU 改写必须重放

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

## 与 PIX GUI 逐字段对照

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

## 第二组对照：PS 的 Scene cbuffer（77 行全覆盖）

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

## 多 cbuffer 的寄存器配对

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

## 诚实边界

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
