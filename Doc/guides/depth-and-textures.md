# 读取深度缓冲 / 纹理（两条路径）
```powershell
# 路径 A：直接读截帧字节，不需要 GPU 回放
pix-tool-set read-resource-texture --queue-id 17765 --target depth --output G:\out --png G:\out
pix-tool-set read-resource-texture --queue-id 17765 --target depth --at-x 766 --at-y 382

# 路径 B：GPU 回放，拿到 pass 真正写出的结果
pix-tool-set save-render-target --queue-id 17765 --depth -o depth.png
```

**两条路径回答的是不同问题**，这是最需要注意的一点。

## 路径 A：截帧里记录的字节

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

## 关键限制：A 拿到的不是 pass 的输出

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

## 路径 B：GPU 回放

`save-render-target --depth` 通过 `pixtool` 重放该帧，拿到的是 pass 执行后的真实
深度。注意直接导出的 PNG 因反向 Z 值极小会接近全黑，需要自行拉伸对比度才便于
肉眼查看（路径 A 的 `--png` 会自动做归一化）。

## 从 GPU 回放读取真实数值（DDS 路径）

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

## 两个硬限制（都来自 pixtool，不是解析问题）

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

## 深度：16 位而非 8 位，但只有一个事件有几何

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

## 两条被证伪的思路

**"不指定 `--global-id` 就能拿最终内容"** —— help 里确实写着取"最后一个绑定该资源的
事件"，但实测导出的是**完全不同的资源**（2243x1119 的 `R10G10B10A2`，而非 rid 771）。
因为 `--rtv` 只是槽位号，脱离具体事件就失去意义。

**"找后续把深度当 SRV 采样的事件，用 `--rtv` 导成彩色面"** —— 统计下来
rid 1985 **被当作 render target 的事件数为 0**，`--rtv` 永远到不了它。思路不成立。

## 深度数值的最终结论

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

## 选哪条

| 需求 | 路径 |
|---|---|
| RT 的真实像素数值 | `read-replay-target`（DDS，需选后续事件） |
| pass 执行后的深度 | `find-depth-content` 定位事件 + `read-replay-target --depth`（16 位量化） |
| 单个像素的精确浮点值 | A（`--at-x --at-y`），若内容标为 rendered |
| 原始字节、自己做后续处理 | A（`--output`） |
| `pixtool` 拒绝的资源 | A（不依赖回放） |
| 纹理的初始上传内容 | A |

## Texture3D 选 z、Tex2DArray 选 slice

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

## 读 buffer 的值

```powershell
pix-tool-set read-buffer --resource-id 448 --length-bytes 64 --format R32_FLOAT
pix-tool-set read-buffer --resource-id 448 --offset-bytes 4096 --length-bytes 256 --output G:\out\buf.bin
```

给 `--format` 就按类型解码成 `elements`，不给就只回 hex。`--stride` 可覆盖默认步长
（结构化缓冲区里挑某个字段时有用）。

同样的前提仍然成立：`resources.bin` 只存上传与 CPU 写入，**GPU 在帧内算出来的
buffer 内容不在其中**，此时 `bytes_available` 为 `false` 并说明原因。
