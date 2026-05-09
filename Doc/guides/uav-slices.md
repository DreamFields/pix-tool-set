# 导出纹理 UAV 的数组切片（如 RWLightGrid）
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

## 切片数由三处独立证据确定

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

## 为什么不能靠描述符表定位

这个 dispatch 的 UAV 表基址是**过期的**。root[1] 记录 heap 32 index 134140，那里放的
是 buffer SRV/UAV；`RWLightGrid` 的描述符实际在 index **134034**，相差 106。命令列表
里本次 dispatch 只重设了 root[0] 和 root[2]，UAV 表沿用了上一次 dispatch 的。

所以按名字解析时改用三重收窄：shader 声明的维度（`2darray`）→ 数组层数大于 1 →
cbuffer 里的 `*Resolution` 字段匹配宽高。

## 数据有效性与来源

三个切片互不相同（差异 1.1 万~1.2 万字节），bbox 分别从 (129,84)、(86,84)、(86,129)
起、都延伸到 255 —— 正是同一个光源球体沿三轴投影、各缺一个轴偏移的特征。归一化后的
PNG 直接可见：左上直角是场景边界裁切，右下圆弧是球面，与 `AABBOverlapsSphere` 吻合。

但来源必须说清：`provenance` 标为 `initial upload at capture time`。
`resources.bin` 只存上传和 CPU 写入，GPU 在 dispatch 中写入的值不在其中。这份数据与
dispatch 输出高度一致，但**无法仅凭截帧证明二者相等**。
