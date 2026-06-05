#!/usr/bin/env python3
# Script to generate README.md for data/train test dataset

readme_content = '''# MCP Database Query Tools Test Dataset

本测试集用于验证 `pix-tool-set` MCP 服务中的三个数据库查询工具的正确性。

## 覆盖的工具

| 工具名称 | 功能描述 | 必填参数 |
|---------|---------|---------|
| `db-get-event-shader-source` | 根据 GlobalID 获取事件的 Shader 源码（HLSL 源文件） | `global_id` |
| `db-get-event-resource` | 根据 GlobalID 获取事件当前绑定的所有资源列表 | `global_id` |
| `db-get-resource-access-history` | 根据 GlobalID 和资源选择器，获取该资源的使用历史 | `global_id`, `resource` |

## 目录结构

```
data/train/
├── README.md                                    # 本说明文档
├── scenario_01_simple_compute/                  # 场景1：简单 Compute Dispatch
│   ├── test_cases.json                          # 测试用例定义
│   ├── capture_db/                              # 模拟的 PIX 导出目录和 SQLite 数据库
│   └── expected_output/                         # 预期输出文件（JSON 格式）
├── scenario_02_graphics_pipeline/               # 场景2：Graphics Pipeline (VS/PS)
│   ├── test_cases.json
│   ├── capture_db/
│   └── expected_output/
├── scenario_03_multi_pass/                      # 场景3：多 Pass 渲染，资源跨事件复用
│   ├── test_cases.json
│   ├── capture_db/
│   └── expected_output/
└── scenario_04_edge_cases/                      # 场景4：边界情况和错误处理
    ├── test_cases.json
    ├── capture_db/
    └── expected_output/
```

## 场景说明

### scenario_01_simple_compute

一个最小的 Compute Shader Dispatch 场景，包含 1 个 UAV 资源和 1 个 Constant Buffer。用于测试三个工具的基本功能。

测试覆盖：
- CS Shader 源码获取
- Compute Dispatch 事件绑定资源查询（UAV + CBV）
- 资源访问历史（通过 resource_name 和 resource_id 选择）
- 非 Shader 事件查询

### scenario_02_graphics_pipeline

典型的图形渲染场景，包含 Vertex Shader 和 Pixel Shader，以及 Vertex Buffer、Index Buffer、Render Targets、Depth Stencil 和多组 Descriptor Table。

测试覆盖：
- VS/PS 多阶段 Shader 源码获取
- Graphics Draw 事件完整资源绑定查询（VB/IB/SRV/CBV/RTV/Depth）
- 各类资源（VB、RTV、Depth）的访问历史
- 带 `pdb_search_paths` 参数的 Shader 资源解析

### scenario_03_multi_pass

多 Pass 渲染场景，Compute PrePass 写入 GBuffer，Graphics Main Pass 读取 GBuffer，涉及资源跨事件读写。

测试覆盖：
- 跨事件资源读写历史追踪（UAV write -> SRV read）
- 资源别名（同一 resource_name 对应多个 resource_id）
- `resolver_path` 参数覆盖

### scenario_04_edge_cases

边界情况和错误处理场景，验证工具在异常输入下的行为。

测试覆盖：
- 不存在的 GlobalID
- 不存在的资源名（`resource_not_bound` 错误）
- 零绑定资源的事件
- 无 Shader Source Cache 的 PSO
- 资源名包含特殊字符
- `refresh=true` 强制重建数据库
- `output_path` 参数测试文件输出

## test_cases.json 格式

每个 `test_cases.json` 文件的结构如下：

```json
{
  "scenario_name": "场景名称",
  "description": "场景描述",
  "capture_db": "capture_db/capture.db",
  "test_cases": [
    {
      "id": "s1_tc01",
      "tool": "db-get-event-shader-source",
      "description": "测试描述",
      "input": {
        "global_id": 2
      },
      "expected_output_file": "expected_output/s1_tc01_shader_source.json",
      "assertions": [
        "status == success",
        "stage_count >= 1"
      ]
    }
  ]
}
```

### 字段说明

| 字段 | 说明 |
|-----|------|
| `id` | 测试用例唯一标识符 |
| `tool` | 调用的 MCP 工具名称 |
| `description` | 测试用例的功能描述 |
| `input` | 调用工具时传入的参数（JSON 对象） |
| `expected_output_file` | 预期输出 JSON 文件的相对路径 |
| `assertions` | 断言列表，用于验证返回结果 |

### assertion 语法

- `field == value`: 字段等于指定值
- `field >= value`: 字段大于等于指定值
- `field contain value`: 数组/字符串包含指定值
- `field is empty/null`: 字段为空或 null
- `field contains 'string'`: 字符串包含子串

## 如何运行测试

1. 确保每个 `capture_db/` 目录中包含有效的 PIX C++ 导出目录结构和由 `build_capture_database` 生成的 `capture.db` SQLite 数据库。
2. 读取对应场景的 `test_cases.json`。
3. 对每个 `test_case`，使用 `input` 参数调用指定的 MCP 工具。
4. 将实际输出与 `expected_output_file` 中的预期输出对比，或使用 `assertions` 进行断言验证。

## 注意事项

- `capture_db` 目录下需要放置真实的 PIX C++ 导出文件和 SQLite 数据库，测试数据可以通过 `tests/test_capture_db.py` 中的 `_sample_index` 辅助函数生成。
- `expected_output/` 下的文件在首次运行测试时可以通过工具实际输出自动生成，后续用于回归测试。
- `pdb_search_paths` 和 `resolver_path` 参数相关的测试需要本地存在对应的 PDB 文件和解析器可执行文件才能通过。
'''

with open("g:/pix-tool-set/data/train/README.md", "w", encoding="utf-8") as f:
    f.write(readme_content)
print("README.md generated successfully")
