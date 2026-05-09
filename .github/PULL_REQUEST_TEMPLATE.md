## 变更说明
<!-- 这个 PR 做了什么、为什么 -->
## 关联 Issue
<!-- Fixes #123 / Related to #456 -->
## 变更类型
- [ ] feat 新工具 / 新能力
- [ ] fix 缺陷修复
- [ ] docs 文档
- [ ] test 测试
- [ ] refactor / chore
## 自查清单
- [ ] 未引入任何第三方 Python 依赖（`pyproject.toml` 的 `dependencies` 仍为空）
- [ ] 拿不到的数据返回 `partial` + `diagnostics` 说明，没有编造或静默钳制
- [ ] 输出遵循统一信封；列表类结果带统一分页字段
- [ ] `python tests\verify_replay_override.py` 通过（无需 GPU）
- [ ] 涉及解析/取值的改动已补充或更新 `tests/verify_*.py` 回归
- [ ] 提交信息符合 Conventional Commits
- [ ] 新增工具已确认 `list-tools` / `describe` 能完整自描述
## 真实调用输出
<!-- 新工具或行为变更：贴一次真实调用的 JSON 信封 -->
<details>
<summary>输出示例</summary>

```json

```
</details>
