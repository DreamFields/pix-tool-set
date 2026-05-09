# 查看某个 pass 的 shader 源码（可取到真实 HLSL）
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

## 为什么截帧里没有，PDB 里却有

截帧内每个 shader 的 DXBC 容器只带 `ILDN`（PDB 文件名），不带 `ILDB`/`SPDB`（源码本体）。
从 PDB 读出的编译参数正好解释了原因：

```
-Zi -Qstrip_debug -E RayTracingBuildLightGridCS -HV 2021 -Zpr -O1 -WX
```

`-Zi` 生成调试信息、`-Qstrip_debug` 把它从截帧字节码里剥离并单独写进 `<hash>.pdb`。
所以源码一直存在，只是不在 `.wpix` 里。

## 恢复路径

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

## 输出范围

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

## 实测覆盖率

抽样 60 个 shader：PDB 命中 **60/60**，HLSL 恢复 **60/60**，入口函数成功切出 **59/60**。
该符号目录共 129,989 个 `.pdb`。注意 hash 必须与截帧时的构建一致，换了构建就对不上。
