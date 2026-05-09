# Security Policy
## 报告漏洞
**请不要在公开 Issue 中报告安全漏洞。**
请通过 GitHub 的 [Private Vulnerability Reporting](https://github.com/DreamFields/pix-tool-set/security/advisories/new) 私密提交。我们会在 72 小时内确认收到，并在修复发布后公开致谢（除非你希望匿名）。
## 支持版本
| 版本 | 支持状态 |
|---|---|
| 2.x（最新） | ✅ 接受安全修复 |
| < 2.0 | ❌ 不再维护 |
## 安全模型说明
使用本工具前，请了解它的能力边界，这些属于**设计行为**而非漏洞：
- 本工具会**执行本机外部程序**：`pixtool.exe`、CMake、MSBuild，以及由 PIX 导出的 C++ 工程编译出的回放程序。请只分析来源可信的 `.wpix` 截帧。
- `shader-edit-apply --patch` 与 `replay-override` 会**改写导出目录下的 C++ 工程文件**（改写前自动留 `.orig` 备份，支持逐字节回滚），但不会改写 `.wpix` 截帧本体。
- `activity-viewer` 的 HTTP 服务**只绑定 `127.0.0.1`**（不可配置），日志内含本机文件路径，请勿通过端口转发暴露到局域网。
- 活动日志默认写入 `%LOCALAPPDATA%\pix-tool-set\activity\`，其中包含命令原文与结果摘要；涉及敏感路径时可用 `PIX_TOOL_SET_NO_LOG=1` 关闭记录。
如果你发现上述边界被突破（例如路径穿越、未授权的文件改写、服务绑定到非回环地址），那就是我们需要修复的漏洞，请按上方渠道报告。
