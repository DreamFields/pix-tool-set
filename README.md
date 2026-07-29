# pix-tool-set

面向 AI 客户端的 PIX 截帧（`.wpix`）脚本化分析工具集。
按 [requirement.md](Doc/requirement.md) 的 12 大类需求实现，共 **55 个 CLI 工具**，
每个工具都自带 JSON Schema，输出统一的 JSON 信封，无需读文档即可被程序驱动。

## 一、为什么这样设计

AI 客户端调用命令行工具时有三个硬需求，本工具集逐一对应：

**能自己发现能力** — `list-tools` 输出全部工具的机器可读目录（含参数 JSON Schema、
返回值说明、示例、能力边界注记）；`describe <tool>` 给出单个工具的完整契约。
客户端不需要预置任何工具知识。

**输出可直接解析** — 每个工具都返回同一个信封，`status` 只有 `success` / `partial` /
`error` 三种，失败时 `error.code` 决定恢复路径、`error.suggestion` 给出下一步动作。
标准输出只有一个 JSON 对象，不掺杂日志。

**代价高的操作只做一次** — `session-open` 完成 pixtool 导出（2.3 GB 截帧约 30–60 秒），
把产物登记为命名会话；此后所有查询毫秒级复用，跨进程有效。

## 二、安装

```powershell
cd G:\pix-tool-set
pip install -e .
```

环境要求：Windows、Python 3.11+、已安装 Microsoft PIX（自动探测
`C:\Program Files\Microsoft PIX\<版本>`，也可用 `PIXTOOL_PATH` 或 `--pixtool` 指定）。
无第三方依赖。

安装后可用 `pix-tool-set` 或简写 `pixts`；未安装时用
`python -m pix_tool_set.cli`（需设置 `PYTHONPATH=G:\pix-tool-set\src`）。

## 三、三种调用方式

```powershell
# 1) 自描述：列出全部工具及其 schema
pix-tool-set list-tools
pix-tool-set list-tools --category shaders --brief
pix-tool-set describe draw-state

# 2) JSON 调用（推荐给 AI 客户端，参数结构化）
pix-tool-set run list-passes --json-args '{"limit": 10, "sort_by": "triangles"}'

# 3) 直接子命令（人手输入更顺）
pix-tool-set list-passes --limit 10 --sort-by triangles
```

退出码：成功 `0`，工具级错误 `1`，参数错误 `2`。

## 四、典型工作流

```powershell
pix-tool-set session-open --capture D:\caps\frame.wpix    # 一次性导出并建会话
pix-tool-set frame-stats                                  # 全帧概览
pix-tool-set list-passes --sort-by triangles --limit 10   # 找最重的 Pass
pix-tool-set analyze-pass --pass-index 12                 # 深挖某个 Pass
pix-tool-set draw-state --draw-index 2461                 # 看某次 draw 的全部绑定
pix-tool-set disassemble-shader --draw-index 2461 --stage PS -o ps.txt
pix-tool-set diagnose-mobile-risks                        # 移动端风险体检
```

## 五、工具总览（55 个）

**会话管理（4）** `session-open` `session-close` `session-list` `capture-info`

**事件与 Action 导航（5）** `list-actions` `action-info` `search-actions`
`find-draw-calls` `locate-event`

**帧统计（4）** `frame-stats` `list-passes` `pass-info` `pass-cost`

**纹理分析（8）** `list-textures` `texture-stats` `texture-info` `export-texture`
`export-draw-textures` `read-texture-pixels` `texture-pixel-stats` `pick-pixel`

**Shader 分析（7）** `shader-stats` `list-shaders` `shader-info` `disassemble-shader`
`shader-reflection` `shader-bindings` `constant-buffer`

**模型与 DrawCall（4）** `model-stats` `draw-call-stats` `list-draw-calls` `diff-draw-calls`

**管线状态（5）** `list-pipeline-states` `pipeline-state` `draw-state` `vertex-input`
`post-vs-data`

**资源管理（3）** `list-resources` `list-buffers` `resource-usage`

**数据导出（4）** `read-buffer` `export-mesh` `save-render-target` `export-report`

**高级分析（4）** `pixel-history` `analyze-pass` `sample-pixel-region` `debug-pixel-shader`

**性能分析（3）** `analyze-overdraw` `analyze-bandwidth` `analyze-state-changes`

**诊断（4）** `diagnose-negative-values` `diagnose-precision`
`diagnose-reflection-mismatch` `diagnose-mobile-risks`

## 六、输出信封

```json
{
  "status": "success",
  "tool": "list-passes",
  "data": { "passes": [ ... ], "total": 416, "has_more": true, "next_offset": 10 },
  "output_paths": [],
  "diagnostics": []
}
```

列表类工具统一分页：`total` / `offset` / `limit` / `returned` / `has_more` / `next_offset`，
客户端可据此翻页而不必猜测。

错误信封：

```json
{
  "status": "error",
  "tool": "texture-info",
  "error": {
    "code": "texture_not_found",
    "message": "No texture matches 99999.",
    "stage": "query",
    "suggestion": "Run list-textures to find valid ids.",
    "details": {}
  }
}
```

## 七、`partial` 的含义（重要）

`partial` 表示**答案可用，但某处被降级**，原因一定写在 `diagnostics` 里。
这比假装成功或直接报错更有用，因为它区分了「工具坏了」和「数据本就不存在」。
本工具集在以下情况返回 `partial`，都是 PIX 截帧的客观边界，不是实现缺陷：

- `pass-cost` — 截帧未采集 GPU 计数器时，耗时用工作量模型估算而非实测毫秒
- `post-vs-data` — 变换后顶点只存在于 PIX 实时回放会话，C++ 导出中没有
- `read-buffer` / `export-mesh` — GPU 运行期生成的缓冲区内容未被截帧记录
- `constant-buffer` — root CBV 被解析为 GPU 地址，逐 draw 的字节未内嵌
- `disassemble-shader --prefer-source` — 该 shader 未带嵌入式 HLSL 调试信息

关于 shader 源码：PIX 截帧存的是**编译后字节码**，原始 HLSL 只在编译时带
`/Zi /Qembed_debug` 才会嵌入。未嵌入时返回 DXIL 反汇编（含完整输入输出签名、
资源绑定表、入口函数名、numthreads 与全部 IR）。`has_embedded_source` 字段明确
告知属于哪种情况。

## 八、Python API

```python
from pix_tool_set import call_tool, list_tools, open_capture

# 工具级（与 CLI 完全一致的信封）
result = call_tool("list-passes", {"limit": 10})
result["status"], result["data"]["passes"]

# 引擎级（直接拿到解析对象）
capture = open_capture(r"D:\caps\frame.wpix")
capture.frame_statistics()
draws, total = capture.find_draw_calls(pass_name="Lumen", limit=10)
draw = capture.draw_calls[2461]
draw.render_targets, draw.srvs, draw.uavs
draw.shader("PS").disassembly
```

## 九、架构

```
src/pix_tool_set/
  cli.py            命令行：list-tools / describe / run / 自动生成的子命令
  registry.py       工具注册中心：JSON Schema、参数校验、分类
  results.py        统一结果信封（success / partial / error + diagnostics）
  errors.py         结构化错误（code / stage / suggestion）
  session.py        命名会话持久化（跨进程复用导出产物）
  context.py        执行上下文：会话 -> Capture 引擎，进程内缓存
  pixtool.py        pixtool.exe 定位与驱动
  engine/
    capture.py      引擎门面：惰性分层解析 + 查询 + 统计
    model.py        类型化模型（Event / DrawCall / Shader / Resource / View）
    cppparse.py     解析导出 C++：资源、描述符、PSO、root signature、命令列表状态机
    eventlist.py    事件 CSV 解析、事件分类、树重建
    dxbc.py         DXBC 容器、签名、反射、DXIL 反汇编
    xpress.py       resources.bin 的 XPRESS 解压与偏移索引
  tools/            12 个模块，每类需求一个
tests/verify_live.py 端到端验证：逐一调用全部工具
Doc/requirement.md   原始需求
```

数据来源是 `pixtool export-to-cpp` 产出的 C++ 工程加 `resources.bin`。
命令列表解析器是一台**状态机**：按序重放导出的 D3D12 调用，持续跟踪当前 PSO、
root signature、描述符堆、渲染目标、顶点/索引缓冲与 root 参数，在每次
draw/dispatch 处快照——这份快照正是 PIX 选中某次 draw 时展示的内容。
描述符表的展开跨度取自**真实 root signature 声明的范围**，而非固定猜测。

`resources.bin` 用 XPRESS 顺序流压缩（无索引表），本工具通过 `Cabinet.dll`
解压并重建偏移索引以支持随机访问；shader 反汇编调用 PIX 自带的
`dxcompiler.dll`（裸 COM vtable），因此零第三方依赖。
纹理像素读取内置纯 stdlib 的 PNG 解码器。

## 十、验证

```powershell
python tests\verify_live.py                 # 静态分析类工具
python tests\verify_live.py --with-replay    # 含 GPU 回放的纹理/像素类工具
```

在 `NoTiled.wpix`（2.33 GB，UE5 ManyLights 场景）上的实测结果：

| 项 | 结果 |
|---|---|
| 工具总数 | 55 |
| 成功 | 49 |
| partial | 4（均为已声明的数据边界）|
| 异常 | **0** |
| 跳过 | 2（`session-open` / `session-close`，会改动会话状态）|

解析规模：22,118 events、2,784 draw/dispatch、416 passes、3,293 resources、
480,958 descriptors、359 shaders、56 root signatures。

## 十一、已知边界

- 依赖本机 PIX 安装；纹理与像素类工具需要该截帧能在本机 GPU 上回放。
- 首次 `session-open` 对 2.3 GB 截帧约需 30–60 秒，缓存约 2.5 GB。
- 单次纹理导出需 GPU 回放，约 30 秒；批量分析建议先用 `export-draw-textures`
  一次导出多张，再本地读取。
- 逐像素替换历史、实时寄存器级 shader 调试需要 PIX 实时回放会话，
  本工具提供静态等价物（覆盖分析 + 完整 shader 代码与输入）并明确标注。
- 部分资源在特定事件不是可保存的 RTV/DSV，PIX 会返回 `0x80070032`；
  错误信封会原样透传 PIX 的诊断文本。
