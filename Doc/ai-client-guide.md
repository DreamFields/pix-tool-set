# AI 客户端集成指南

本文档面向要驱动 `pix-tool-set` 的 AI 客户端（Agent、MCP 封装、脚本编排器）。
目标：客户端**不预置任何工具知识**也能正确完成分析任务。

## 一、契约要点

1. 每次调用只在 stdout 输出**一个 JSON 对象**，没有额外日志。
2. 顶层固定字段：`status` / `tool` / `data` / `output_paths` / `diagnostics`，
   失败时额外有 `error`。
3. `status` 只有三种取值，语义严格：
   - `success` — 结果完整可信
   - `partial` — 结果可用但有降级，原因在 `diagnostics`
   - `error` — 未产出结果，恢复路径看 `error.code` 与 `error.suggestion`
4. 退出码：`0` 成功或 partial，`1` 工具级错误，`2` 命令行参数错误。

## 二、建议的调用循环

```
启动
 └─ pix-tool-set list-tools            读取工具目录，缓存到本轮会话
 └─ pix-tool-set session-open --capture <file.wpix>
      └─ 记下 data.session，后续调用带 --session 或省略（自动用最近会话）

分析
 └─ pix-tool-set run <tool> --json-args '{...}'
      ├─ status=success  → 用 data
      ├─ status=partial  → 用 data，同时把 diagnostics 里的原因告知用户
      └─ status=error    → 按 error.code 分支处理，见下表
```

## 三、错误码与处置

| code | 含义 | 处置 |
|---|---|---|
| `session_missing` | 没有活动会话 | 先调 `session-open` |
| `session_not_found` | 会话名不存在 | 调 `session-list` 取正确名字 |
| `capture_not_found` | 截帧文件路径不对 | 向用户核对路径 |
| `export_missing` / `export_incomplete` | 缓存缺失或不完整 | `session-open --force` 重导出 |
| `pixtool_missing` | 找不到 pixtool.exe | 提示安装 PIX 或传 `--pixtool` |
| `missing_parameter` / `unknown_parameter` / `invalid_argument` | 参数问题 | 调 `describe <tool>` 校对 schema 后重试 |
| `tool_not_found` | 工具名错误 | `error.details.closest` 给出近似名 |
| `*_not_found`（`texture_not_found` 等） | 对象 id 不存在 | 先调对应的 `list-*` 工具取有效 id |
| `disassembly_unavailable` | dxcompiler.dll 不可用 | 告知用户此类字段本机不可得 |
| `compiler_unavailable` | 没有可用的 HLSL 编译器 | 提示安装 PIX（含 dxcompiler.dll）或 Windows SDK（含 dxc.exe） |
| `shader_compile_failed` | 编辑后的 HLSL 编译不通过 | `error.details.compiler_output` 是 DXC 原文，含行列号，据此改源码 |
| `compile_args_missing` | PDB 未记录编译参数 | 用 `--args` 显式传入，否则无法复现截帧内的构建 |
| `source_unavailable` | 无法从 PDB 恢复 HLSL | 先用 `pass-shader-source` 确认该 PDB 是否可读 |
| `already_patched` | 该 PSO 的这个 stage 已打过补丁 | 从导出目录的 `.orig` 备份恢复后再应用 |
| `save_resource_failed` | PIX 无法保存该资源 | 换 `--global-id` 或改用 `--depth` / 其他 `--rtv` |
| `pixtool_timeout` | 导出超时 | 提高 `--timeout`，或先单独 `session-open` |

## 四、分页

所有列表类工具返回同一组分页字段，直接照用即可：

```json
{ "total": 416, "offset": 0, "limit": 10, "returned": 10,
  "has_more": true, "next_offset": 10 }
```

`has_more` 为 `true` 时用 `next_offset` 继续取，不要自己推算。
默认 `limit` 是 50；需要全量时显式传大值，但注意响应体积。

## 五、成本分级（影响调用策略）

**毫秒级（可自由多次调用）** — 除下面两档以外的全部工具，走已解析缓存。

**首次数十秒（一次性）** — `session-open`。同一截帧只需一次，之后所有查询都快。

**每次约 30 秒（需 GPU 回放）** — `export-texture`、`export-draw-textures`、
`read-texture-pixels`、`texture-pixel-stats`、`pick-pixel`、`sample-pixel-region`、
`save-render-target`，以及 `pixel-history --include-final-value`。
需要多张图时优先用 `export-draw-textures` 一次导出，再本地读取，避免逐张回放。

## 六、推荐分析路径

**「这一帧慢在哪」**
```
frame-stats → pass-cost --limit 15 → analyze-pass --pass-index <最重的>
→ analyze-overdraw / analyze-bandwidth / analyze-state-changes
```

**「这次 draw 到底绑了什么」**
```
find-draw-calls --pass-name <关键字> → draw-state --draw-index <n>
→ shader-bindings --draw-index <n> --stage PS
```

**「这个 shader 做了什么」**
```
list-shaders --stage CS --unique → shader-reflection --pso-id <id> --stage CS
→ disassemble-shader --pso-id <id> --stage CS -o cs.txt
```

**「这块纹理是谁写的」**
```
list-textures --render-target → resource-usage --resource-id <id>
→ pixel-history --resource-id <id> --x <x> --y <y>
```

**「上移动端有什么坑」**
```
diagnose-mobile-risks → diagnose-precision → diagnose-negative-values
→ diagnose-reflection-mismatch
```

**「改一下这个 shader 看效果」**
```
session-set-pdb-dirs --pdb-dirs <Project>\Saved\ShaderSymbols\PCD3D_SM6
→ shader-edit-begin --queue-id <id> --output <dir>     取出真实 HLSL + 编译参数
→ （编辑那个 .hlsl）
→ shader-edit-apply --queue-id <id> --source <file>    先只编译校验
→ shader-edit-apply ... --patch                        确认无误再打补丁
```
`apply` 不带 `--patch` 时只编译并校验绑定签名，不改动任何文件，适合先试错。
返回 `partial` 且 `binding_check.identical=false` 表示替换不是 slot 兼容的，
已被拒绝打补丁，需要先恢复原有资源声明。

**「两次 draw 为什么表现不同」**
```
diff-draw-calls --left-draw <a> --right-draw <b>
```

## 七、给用户汇报时的注意事项

- `pass-cost` 是**工作量模型估算**，不是实测毫秒；汇报时必须说明，
  只能用于相对排序。
- `analyze-overdraw` / `analyze-bandwidth` 同为静态估算，用于定位嫌疑对象。
- shader「源码」在多数情况下是 DXIL 反汇编而非原始 HLSL，
  由 `has_embedded_source` 判定，不要含糊表述。
- `pixel-history` 给的是**静态覆盖候选集**（视口/裁剪覆盖该像素且绑定了该目标的 draw），
  不是逐片元替换历史。
- `shader-edit-apply --patch` 改的是**导出的 C++ 回放工程**，不是 `.wpix`。
  向用户汇报时不要说成「修改了截帧」；要看到效果需重建并运行那个工程。
- 凡 `status=partial`，都应把 `diagnostics` 中的原因转达用户，
  不要静默当作完整结果。

## 八、最小可用示例

```python
import json, subprocess

def call(tool, **args):
    cmd = ["pix-tool-set", "run", tool, "--json-args", json.dumps(args)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    payload = json.loads(proc.stdout)
    if payload["status"] == "error":
        raise RuntimeError(
            f"{payload['error']['code']}: {payload['error']['message']}\n"
            f"hint: {payload['error'].get('suggestion')}"
        )
    if payload["status"] == "partial":
        for entry in payload["diagnostics"]:
            print("[degraded]", entry.get("message"))
    return payload["data"]

call("session-open", capture=r"D:\caps\frame.wpix")
stats = call("frame-stats")
print(stats["draw_calls"], stats["geometry"])

for entry in call("pass-cost", limit=5)["passes"]:
    print(entry["name"], entry["cost_score"], entry["cost_share_percent"])
```
