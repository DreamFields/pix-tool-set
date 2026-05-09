# 贡献指南
感谢你愿意为 pix-tool-set 做贡献。请先花几分钟读完本文——本项目有几条**不可妥协的设计红线**，违反它们的 PR 会被直接关闭。
## 设计红线（务必遵守）
1. **零第三方 Python 依赖。** `pyproject.toml` 的 `dependencies = []` 必须保持为空。DXC 走 `dxcompiler.dll` 的 ctypes 裸 COM 调用，PNG 解码用纯 stdlib 实现——这是既定取舍，不接受"加个库更简单"的 PR。
2. **诚实边界，不编造数据。** 工具拿不到的数据必须返回 `partial` 并在 `diagnostics` 说明原因，禁止返回猜测值、静默钳制或伪造成功。参考 README「`partial` 的含义」。
3. **统一输出信封。** 所有工具返回 `success` / `partial` / `error` 三态信封，stdout 只有一个 JSON 对象，不掺杂日志。
4. **只读优先。** 诊断类工具（如 `env-check`）不得安装、下载或改配置；会改写导出工程的工具必须先备份、支持回滚（参考 `engine/override.py`）。
## 开发环境
```powershell
git clone https://github.com/DreamFields/pix-tool-set.git
cd pix-tool-set
pip install -e .
pix-tool-set env-check          # 只读体检，会告诉你缺什么
```
完整 GPU 回放链路需要 Microsoft PIX、CMake、VS C++ 工具链与 D3D12 GPU，详见 README「安装」。
## 提交信息
使用 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)：
```
feat(dxr): add analyze-raytracing
fix(bindings): treat per-subresource descriptors as distinct bindings
docs(verify): cover all tools in live verification
```
类型：`feat` / `fix` / `docs` / `test` / `refactor` / `chore`，scope 取模块名。
## 测试
```powershell
python tests\verify_replay_override.py    # 无需截帧无需 GPU，PR 必跑
python tests\verify_live.py               # 需要 .wpix 截帧与 PIX
python tests\verify_live.py --with-replay # 含 GPU 回放工具
```
- **PR 至少要求**：不依赖 GPU 的验证脚本全部通过，且不引入新异常。
- 新增工具必须能被 `verify_live.py` 覆盖到（或在 PR 中说明为何只能跳过）。
- 涉及取值/解析的改动，优先补 `tests/verify_*.py` 里的对照式回归（以 PIX GUI 为真值），而不是只断言"没报错"。
## 添加一个新工具
1. 在 `src/pix_tool_set/tools/` 对应分类模块中实现，向 `registry.py` 注册：名称、JSON Schema、分类、能力边界注记。
2. 通过 `results.py` 返回统一信封；错误用 `errors.py` 的结构化错误（`code` / `stage` / `suggestion`）。
3. 列表类结果遵循统一分页字段：`total` / `offset` / `limit` / `returned` / `has_more` / `next_offset`。
4. `pix-tool-set list-tools` 与 `describe <tool>` 必须能完整自描述你的工具——AI 客户端不读文档，契约就是文档。
5. 在 PR 描述里贴一次真实调用的输出信封。
## PR 流程
1. Fork 并创建特性分支（`feat/xxx` 或 `fix/xxx`）。
2. 保持 PR 聚焦：一个 PR 只做一件事。
3. 填写 PR 模板，关联相关 Issue。
4. 维护者 review 后合并；大改动建议先开 Issue 讨论方向。
## 行为准则
参与本项目即表示你同意遵守 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
## 许可证
你的贡献将按本项目的 [MIT 许可证](LICENSE) 发布。
