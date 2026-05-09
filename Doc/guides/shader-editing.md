# 改 shader 源码并应用（PIX Debug 面板 Apply 的等价物）
PIX GUI 的 Debug 面板可以直接改 shader 源码再点 Apply。这是 GUI 独有能力：
`pixtool` 的完整命令列表里**没有任何 shader 替换命令**。等价能力由两步拼出：

```powershell
# 1. 取出可编辑的真实 HLSL，连同 PDB 里记录的原始编译参数
pix-tool-set shader-edit-begin --queue-id 18461 --output G:\edit `
  --pdb-dirs "F:\JL_TMR\UnrealEngine\Games\JyGame\Saved\ShaderSymbols\PCD3D_SM6"

# 2. 编辑 G:\edit\q18461_CS_RayTracingBuildLightGridCS.hlsl 之后
pix-tool-set shader-edit-apply --queue-id 18461 `
  --source G:\edit\q18461_CS_RayTracingBuildLightGridCS.hlsl --patch
```

`begin` 会写出三个文件：可编辑源码、一份 pristine 副本（用于回退对比）、
以及 `.args.txt`。`apply` 默认自动读取那个 sidecar，所以第二步不必重复参数。

## 为什么这条路走得通

PDB 同时提供了重编译所需的两半，缺一不可：

| 需要什么 | 从哪来 |
|---|---|
| 自包含的预处理 HLSL（无需 include 路径） | PDB 的 `SRCI`，UE 写的是单个编译单元 |
| 精确编译参数 | PDB 记录 `-HV 2021 -Zpr -O1 -WX -auto-binding-space 0 -Zsb -Zi -Qstrip_debug -E <entry> -T cs_6_6` |

编译走 `IDxcCompiler3`（ctypes 裸 COM，零第三方依赖），`dxc.exe` 作为回退。
`dxil.dll` 会先加载，因此产物容器**已签名** —— 未签名的容器 D3D12 会直接拒绝。

## 核心安全检查：绑定签名

录制的命令列表**按 slot 绑定资源**，所以只有"资源、register、入口点、线程组尺寸
全部不变"的替换才是安全的。不满足就拒绝打补丁，而不是放行：

```
status: partial
binding_check.identical: false
warning: Compiled successfully but the replacement is not slot-compatible,
         so it was not patched in.  reason: bindings differ
```

实测：原样重编译时 5 个绑定（`cb0` / `t0` / `t1` / `u0` / `u1`）逐项一致；
一旦多声明一个 UAV 把 `RWLightGrid` 从 `u0` 挤到 `u1`，就会被上面这条拦住。
确实想改绑定时用 `--allow-binding-change` 显式放行。

编译失败则原样透出 DXC 自己的诊断，带行列号：

```
__UE_FILENAME_SENTINEL:259:32: error: expected expression
        uint3 VoxelId = 0, VoxelRes = ;
                                      ^
```

## 补丁做了什么，边界在哪

`--patch` 修改的是**导出的 C++ 回放工程**，不是 `.wpix`。截帧记录的是 API 调用序列，
本工具不会改写它。补丁采用"保留原赋值 + 下一行覆盖"而非整行替换：

```cpp
    g_resourceReader->Read(data, 12491);
    pssDesc.CS = { reinterpret_cast<BYTE*>(&data[offset]), 16436 };
    // pix-tool-set: CS replaced by shader-edit-apply
    static std::vector<BYTE> editedBytes_CS = Helpers::ReadFileBytes(LR"(edited_CreatePipelineState_3241_CS.dxil)");
    if (!editedBytes_CS.empty())
        pssDesc.CS = { editedBytes_CS.data(), editedBytes_CS.size() };
```

两个原因必须这样做：本工具集自身要解析 `CreatePSOs.cpp` 得到各 stage 字节码尺寸，
整行替换会让它再也找不到该 shader；而 `resources.bin` 是**无索引的顺序流**，
跳过一次 `Read` 会让后面所有 blob 错位。

补丁前自动留 `.orig` 备份，重复应用会被 `already_patched` 拦住。改完重建即可运行：

```powershell
cmake -S <export> -B <export>\build && cmake --build <export>\build --config Release
```

## 能不能看到新的渲染结果——能，实测过

导出工程是个真正的 Win32 程序（`Main.cpp` + `Win32Application.cpp`），有窗口、有
swapchain，`RenderFrameWorker_000.cpp` 里会 `Present`。所以重建后运行，画面就在窗口里。

实测流程（已跑通）：

```powershell
# 1. 首次配置。默认生成器可能是 Ninja 且无编译器，需显式指定 VS
cmake -S <export> -B <export>\build -G "Visual Studio 18 2026" -A x64
cmake --build <export>\build --config Release --parallel

# 2. 运行。工作目录必须是导出根目录，因为 resources.bin 和 edited_*.dxil 都在那里
Start-Process <export>\build\Release\<name>.exe -WorkingDirectory <export>
```

改 Slate 的 `ElementBatch` PS 加一行 `OutColor.rgb = float3(1,0,1)`，重建后窗口里整个
面板由深灰变品红，采样统计 22.8% 像素为品红主导。同一份 UI 布局、同一批数据，只有颜色变了。

## 一个必须知道的陷阱：改的 pass 要在上屏路径上

第一次实测改了 `Tonemap`，重建运行后**画面毫无变化**。不是工具坏了，是选错了 pass：

```
CopyResource(backBuffer, GetResource(23));    // 3864x2100，由 ElementBatch 写
CopyResource(backBuffer, GetResource(2966));  // 1815x1115，由 ElementBatch 写
Present(1, 0);
```

上屏拷的是这两个固定资源，而 `Tonemap` 写的是 rid 812，**本帧内没有被拷进 backbuffer**。
这个截帧是在编辑器里截的，窗口显示的是 GPU Visualizer 面板，3D 视口的结果并不上屏。

所以想看到变化，先确认目标 pass 的输出通向 backbuffer：

```powershell
pix-tool-set resource-usage --resource-id <被 CopyResource 拷贝的那个 rid>
```

也就是说，看不到变化有两种完全不同的原因——补丁没生效，或者补丁生效了但那个 pass
不影响这一帧的最终画面。先用 `--patch` 的返回确认前者，再用上面的方法排除后者。

## 把新的渲染结果显示在面板里

不必自己盯窗口。`replay-render` 会构建、运行、等到画面真正出现，再把它抓成 PNG
存进活动日志，`activity-viewer` 的面板里就能直接看：

```powershell
pix-tool-set replay-render --label baseline                        # 改之前先留一张
# ... shader-edit-apply --patch ...
pix-tool-set replay-render --label after-edit --compare-to baseline
```

面板的变化：调用流上方多一条缩略图带（只显示有截图的调用），详情多一个「渲染结果」
页签，概览顶部直接内嵌画面，点图放大。带 `--compare-to` 时是基线与本次并排显示，
外加一句判定：**画面已改变** 或 **与基线无可测差异**。

判定不只靠眼睛，还给数字：

```
mean_rgb_delta        [-181.6, -240.4, -181.6]
largest_channel_delta 240.4
hue_share_delta_percent  grey -76.9%   magenta +23.0%
visibly_different     true
```

阈值定得比抖动大一档（任一通道差 ≥ 8 级，或某色相占比变化 ≥ 3%），所以
dither 和 UI 闪烁不会被当成 shader 改动。

## 一个坑：窗口出现 ≠ 画面已渲染

这个截帧要跑 4 分钟以上才 present。窗口会先出现好几分钟的**纯白**，此时抓图会得到
一张空白页——而两张空白页互相对比是"完全一致"，读起来就像"补丁没生效"，比报错更糟。

所以工具等的不是窗口，而是**画面内容**：轮询计算内部区域的内容占比，达到阈值且连续两次
稳定才抓。判定只看内缩 12% 的内部区域，因为标题栏和边框本身就能让全图评分冲到 0.049，
足以骗过一个朴素阈值（这是实测踩到的，修正后白屏评分 0.0000、已渲染 0.2828）。

抓到空白时状态是 `partial` 并明确说明，不会伪装成结论：

```
warning: The window never showed a rendered frame within the settle window, so this
         capture is a blank page and says nothing about the patch.
```

`--settle-seconds` 默认 150，大截帧建议给到 400 以上。已经构建过就加 `--skip-build`。

回归覆盖见 [tests/verify_replay_render.py](../../tests/verify_replay_render.py)（40 项），
包含 PNG 编码往返、白屏与已渲染的分离判定、色相对比阈值与图片路由的路径防护。

## 构建环境的两个坑

先跑 `pix-tool-set env-check --scope replay`，下面两个坑它都能事前查出来，不必等编译
报错才发现。

`CMakeLists.txt` 原本会从 nuget.org 下载 Agility SDK 与 WinPixEventRuntime。若 SSL 失败，
它会留下 **0 字节的 .nupkg** 并继续，随后编译时报 `无法打开包括文件 d3d12.h`。

其中 WinPixEventRuntime **已随本仓库自带**，回放工具会直接把它铺进导出目录，这一半的坑
不存在了（要走回 nuget 路线就传 `--no-vendored-winpixruntime`）。剩下 Agility SDK 仍需
联网，失败时手动补：

```powershell
Invoke-WebRequest 'https://www.nuget.org/api/v2/package/Microsoft.Direct3D.D3D12' -OutFile <export>\D3D12AgilitySdk.nupkg
```

删掉 `build` 目录重新配置，CMake 会自行解包。另外换生成器前必须清掉 `build`，
否则报 `Does not match the generator used previously`。`env-check --scope replay` 会同时
检查这个包在不在位、是不是失败下载留下的 0 字节残包，以及本机该用哪个生成器。

回归覆盖见 [tests/verify_shader_edit.py](../../tests/verify_shader_edit.py)（41 项），
包含语法错误诊断、绑定拒绝、重复补丁检测与自动还原。
