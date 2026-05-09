---
name: Bug 报告
about: 报告一个工具行为不符合预期的问题
title: "[Bug] "
labels: bug
assignees: ''
---
## 问题描述
<!-- 一句话说明发生了什么 -->
## 复现步骤
1. 执行的完整命令：`pix-tool-set ...`
2. 使用的会话 / 截帧：
3. 得到的输出（贴完整 JSON 信封）：
## 期望行为
<!-- 你认为正确的结果应该是什么 -->
## 环境自检输出
<!-- 请执行 `pix-tool-set env-check` 并把输出原样贴在这里——它一次性包含 PIX、dxcompiler、CMake、VS 生成器、Windows SDK、D3D12 设备状态，能省掉大部分来回确认 -->
<details>
<summary>env-check 输出</summary>

```json

```
</details>

## 补充信息
- pix-tool-set 版本：<!-- `pip show pix-tool-set` 的 Version -->
- 截帧来源（引擎/版本，如 UE5.x）：<!-- 可选 -->
- 若涉及 `partial` 状态，请贴出 `diagnostics` 字段内容
- ⚠️ 安全漏洞请勿在此报告，请走 [私密渠道](../../security/advisories/new)
