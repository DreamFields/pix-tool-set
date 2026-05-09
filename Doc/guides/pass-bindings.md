# 按 pass 查询 shader 绑定
拿某个 pass 的 shader 绑定资源，一条命令即可（不必再手动 pass → draw_index → bindings 三步走）：

```powershell
pix-tool-set pass-bindings --pass-name TileClassificationBuildLists --stage CS
pix-tool-set pass-bindings --pass-name TileClassification --all-matches   # 同名多 pass 全列
pix-tool-set find-pass --name TileClassificationBuildLists               # 只要 id 时用这个
```

`pass-bindings` 会自动按 PSO 去重挑代表 draw，并把 descriptor table 默认展开到 128 项
（UE5 的 SRV table 声明 64 项，旧的 16 项默认值会截断）。

返回分两层，可信度不同：

- `stages[].declared_registers` —— 来自 shader 字节码反射，**权威**。这就是
  「该 pass 绑定了哪些 shader 资源」的答案，含 HLSL 寄存器、资源变量名、格式、维度。
- `root_descriptors` / `descriptor_tables` —— 从导出的 C++ 重建的运行时绑定，
  每项带 `trust` 字段：

| trust | 含义 |
|---|---|
| `reliable` | 直接来自记录的调用（如 root CBV 的 rid），或槽位数与 shader 声明吻合 |
| `partial` | 已重建但未经确认，不要依赖 register → resource 的逐项映射 |
| `filler` | 该窗口是 PIX 的初始化占位，真实描述符未被记录 |
| `unavailable` | 该 table 完全没有描述符数据 |

出现 `filler` / `unavailable` 时结果为 `partial`，并在 `diagnostics` 里提示改用
`declared_registers`。原因是 PIX 的 C++ 导出对部分 draw 未记录真实的描述符写入 ——
这是导出格式的边界，不是解析缺陷；此时精确的 register → rid 对应需要用
`disassemble-shader` 看资源索引指令，或在 PIX GUI 里查看。
