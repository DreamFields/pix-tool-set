---
name: pix-shader-hotswap
description: >-
  在 PIX 抓帧（.wpix）上热替换 Compute/Pixel Shader 并验证 GPU 实际写入结果。
  当用户要求「改某个 pass 的 shader 看效果」「读某个 UAV 的内容」「对比修改前后的
  GPU 输出」「验证 shader 补丁是否生效」时使用本 skill。
  关键词：shader 热替换、shader-edit、read-uav、UAV 回读、pass 补丁、
  TileClassificationMark、queue id、CreatePSOs、dxil、before/after 对比。
  不适用于：修改真实引擎源码、非 PIX 抓帧的 shader 调试、Render Target 截图
  （用 replay-render / read-replay-target）。
---

# PIX Shader 热替换与 UAV 验证

`pix-tool-set` 有 81 个工具。本 skill 覆盖"改 shader → 看 GPU 结果 → 全帧调试"这条链，
目的是让你**一次选对工具**，而不是靠试错逼近。

新增工具（2.0.0+）：
- `shader-edit-apply --scope` (pso/shader/auto)：多 PSO 作用域补丁
- `shader-info` → `sibling_psos`：同一 shader 被多少 PSO 引用
- `replay-baseline-check`：null-patch 基线检查（D5 信任门）
- `replay-edits`：查看当前补丁列表
- `replay-reset`：一键回退所有补丁；`clean` 按三个注入器分报（shader-edit / read-uav 探针 / pixel-history 探针），默认顺带还原探针
- `frame-replay-dump`：全帧资源 dump；加 `--snapshot` 则每次落在独立编号目录并记录当时的编辑状态
- `pixel-value-history`：像素值变化历史
- `trace-downstream`：下游影响链追踪
- `shader-edit-diff --checkpoint`：跨编辑检查点对比
- `pixel-history-replay`：GPU 回放实测单个纹素的 Previous/New Value（PIX Pixel History 面板对齐）
- `snapshot-list` / `snapshot-compare` / `snapshot-remove`：浏览与逐资源比对每次编辑的整帧快照

### 多次编辑的推荐流程
先 `frame-replay-dump --snapshot` 存基线（0000-baseline），之后每改一次 shader 就再存一次，
快照目录位于 `<capture>.pixcache/snapshots/NNNN-label/`（与 `cpp/` 平级，不受 patch/rebuild/reset 影响）。
用 `snapshot-compare --a 0 --b 1` 看这次编辑到底改了整帧里的哪些资源——
字节相同即证明该资源未被触碰，比统计量相等更强。序号只增不复用，删掉一个不会让其余重新编号。

## 0. 铁律

**先 `describe`，再调用。** 参数名不能猜：是 `--pdb-dirs` 不是 `--dirs`，
是 `--global-id`、`--draw-index` 或 `--queue-id`（三者都可作为输入；从 PIX GUI 抄 id
推荐用 `--global-id`，跨队列唯一；`--queue-id` 只对已导出队列有效，多队列截帧上误用会命中错行）。

```bash
pix-tool-set describe <tool-name>   # 返回完整 JSON Schema
pix-tool-set list-tools --brief     # 74 个工具的全量目录
```

**Session 会自动复用。** `session-open` 之后，后续命令都不必再传 `--session`；
`session-set-pdb-dirs` 存下的 PDB 目录会被 `shader-edit-*` 自动读取，不必反复传 `--pdb-dirs`。

## 1. 工具选择决策表

这张表是本 skill 最重要的部分。选错工具会浪费数分钟的构建加回放。

| 我要读什么 | 用什么 |
|---|---|
| **Compute Shader 写入的 UAV 内容** | `read-uav` ← **唯一可行**，走 GPU 回放 + readback |
| 修改前后的 UAV 对比 | `shader-edit-diff` ← **首选**，一次构建跑两次回放，直接出量化差异 |
| UAV 的初始字节（仅 CPU 上传的部分） | `export-uav-slice` |
| 绑定的 Render Target | `read-replay-target` / `export-texture` / `read-texture-pixels` |
| Depth Buffer | `save-render-target --depth` |
| pass 的绑定表、resource_id | `pass-bindings` |

**硬性边界（外部限制，不是 bug）**：`pixtool save-resource` 只能导出**绑定的 Render Target**，
所以 `export-texture` / `read-texture-pixels` 对 compute-only 的 UAV 一律失败
（报 `PIXTOOL9 Render Target ... does not exist`）。看到这个错误就换 `read-uav`，不要试别的纹理工具。

**`export-uav-slice` 读出全 0 是预期行为**，不是 bug：它读 `resources.bin`，那里只有
CPU 上传和 CPU 页写入；GPU 在 dispatch 里写的值只存在于显存，抓帧记录的是 API 调用序列而非执行后快照。

## 2. 主干工作流：用 shader-edit-diff（推荐）

绝大多数"改 shader 看效果"的任务用这条链就够了，**不要手工跑两次 `read-uav` 再自己 diff**。

```bash
pix-tool-set session-open --capture "C:\path\Tiled.wpix"
pix-tool-set session-set-pdb-dirs --pdb-dirs "F:\...\ShaderSymbols\PCD3D_SM6"
pix-tool-set pass-bindings --queue-id 18704          # 拿 resource_id 和 register
pix-tool-set shader-edit-begin --queue-id 18704 --stage CS --output "G:\edit"
# 编辑 G:\edit\*.hlsl
pix-tool-set shader-edit-apply --queue-id 18704 --stage CS --source "G:\edit\edited.hlsl" --patch --force
pix-tool-set shader-edit-diff --queue-id 18704 --name RWNormalTexture --output "G:\diff" --settle-seconds 300
```

`shader-edit-diff` 内部通过把补丁 `.dxil` 改名 `.hold` 来切换原始版/补丁版，
**一次构建服务两次回放**，产出 BEFORE / AFTER / DIFF / 并排图和量化差异。改名放在 `try/finally` 里，
异常也不会把补丁留在禁用状态。

## 3. 关键参数

`--patch` 必须显式加，否则 `shader-edit-apply` 只编译校验、**什么都不写**（默认 dry-run）。

`--force` 用于重复打补丁。同一个 stage 已打过补丁时，不加 `--force` 会报 `already_patched`；
加了则先剥离旧 override 再重新注入，不会叠加。（旧版本必须手工
`Copy-Item CreatePSOs.cpp.orig CreatePSOs.cpp` 恢复，现已不需要。）

`--settle-seconds` 默认 240，指**回放启动后**等 probe 完成的时间，不含构建时间
（构建由 `--build-timeout` 控制，默认 1800）。GB 级抓帧建议给 300 以上：
2.3GB 的 capture 首帧前要加载数 GB 资源。

`--keep-probe` 保留注入的 readback probe，后续调用可复用已编译的 exe。

`--skip-build` 只在 probe 已编译进 exe 时才有意义。若 probe 是本次刚注入的，
工具会**自动降级为完整构建**并给出 warning——不必自己判断。

## 4. 验证纪律

**看 `hash_changed`。** `shader-edit-apply` 返回的 `new_container` 里同时有
`shader_hash`、`previous_shader_hash` 和 `hash_changed`。`hash_changed: false` 说明
DXC 编出了和抓帧一样的字节码，即**你的编辑没进到编译器**（或被优化掉了）——
此时不要去查构建系统，回去查 `--source` 指对了没、改动是不是死代码。

**补丁字节码是运行时读取的。** 注入的代码是 `Helpers::ReadFileBytes(...)` + `static` 变量，
所以换 `.dxil` **只需重启 exe，不需要重新编译**。不要用"删 exe + 删 obj + `--force-reconfigure`"
去解决"改了没效果"——那几乎总是误诊，代价是每次多烧 2~3 分钟。

**`replay-render` 的 `visibly_different: false` 不等于补丁没生效。** 回放窗口截的是整个窗口；
若窗口是 UE GPU Visualizer 这类 Slate UI，3D 视口本身是黑的，改中间 G-Buffer UAV 对整窗截图零影响。
判断补丁是否生效要用 `read-uav`，不要用截图。

## 5. 排错对照

| 现象 | 真实原因 | 动作 |
|---|---|---|
| `unrecognized arguments` | 猜了参数名 | `describe <tool>` |
| `already_patched` | 该 stage 已有补丁 | 加 `--force` |
| UAV 数据全 0 | 用了 `export-uav-slice` | 换 `read-uav` |
| `PIXTOOL9 Render Target` | 拿 RT 工具读 UAV | 换 `read-uav` |
| `--name` 返回数百候选 | 名字无法唯一定位 | `pass-bindings` 拿 `resource_id`，改用 `--resource-id` |
| 无 dump / `finished: false` | settle 窗口不够 | `--settle-seconds 300+` |
| 改了 shader 但结果不变 | 先看 `hash_changed` | false→查源文件；true→确认 exe 已重启 |

## 参考

完整踩坑实录见 [pix-tool-set-UAV-shader-hotswap-pitfalls.md](../Doc/pix-tool-set-UAV-shader-hotswap-pitfalls.md)，
工具契约见 `pix-tool-set describe <tool>` 与 [ai-client-guide.md](../Doc/ai-client-guide.md)。
