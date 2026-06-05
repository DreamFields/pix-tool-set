# MCP Database Query Tools Test Dataset

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
├── scenario_03_graphics_pipeline_with_db_and_pdb/  # 场景3：Graphics Pipeline with DB and PDB (GlobalID=3854)
│   ├── test_cases.json                          # 测试用例定义
│   └── expected_output/                         # 预期输出文件（JSON 格式）
│       └── s3_tc01_event_resources.json
├── scenario_04_graphics_pipeline_with_db_and_pdb/  # 场景4：Graphics/Compute Pipeline with DB and PDB (GlobalID=3553)
│   ├── test_cases.json
│   └── expected_output/
│       ├── s4_tc01_event_resources.json
│       └── s4_tc02_shader_source.json
└── scenario_05_compute_pipeline_with_db_and_pdb/  # 场景5：Compute Pipeline with DB and PDB (GlobalID=3968)
    ├── test_cases.json
    └── expected_output/
        └── s5_tc01_event_resources.json
```

## 场景说明

### scenario_03_graphics_pipeline_with_db_and_pdb

图形渲染场景（GlobalID=3854），包含 Vertex Shader 和 Pixel Shader，以及完整的管线资源绑定。

- **capture_db**: `capture_db/capture.db`
- **测试用例**: 仅保留 `db-get-event-resource` 用例（`s3_tc01`）
- **资源覆盖**: 26 个资源，按阶段细分：
  - **IA 阶段**: 3 个 VB (`VB 0`, `VB 4`, `VB 5`) + 1 个 IB
  - **VS 阶段**: 3 个 CBV (`View`, `Scene`, `LocalVF`) + 4 个 SRV Buffer
  - **PS 阶段**: 2 个 CBV (`View`, `Material`) + 3 个 SRV Texture + 1 个 SRV Buffer + 1 个 Sampler
  - **OM 阶段**: 6 个 RTV (`SceneColor`, `GBufferA~D`, `GBufferG`) + Depth + Stencil

> **注意**: 该场景已裁减，仅保留 `db-get-event-resource` 工具用例，移除了 `db-get-event-shader-source` 用例。

### scenario_04_graphics_pipeline_with_db_and_pdb

图形/计算管线场景（GlobalID=3553），Compute Shader Dispatch 事件，包含 CBV、SRV、UAV 和 Sampler。

- **capture_db**: `capture_db/capture.db`
- **测试用例**:
  - `s4_tc01`: `db-get-event-resource` — 查询事件绑定的全部 15 个资源
  - `s4_tc02`: `db-get-event-shader-source` — 查询 Compute Shader 源码
- **资源覆盖**: 15 个资源
  - **CS 阶段**: 3 个 CBV (`_RootShaderParameters`, `View`, `ReflectionCaptureSM5`) + 1 个 SRV Texture (`HZBTexture`) + 5 个 SRV Buffer + 5 个 UAV Buffer + 1 个 Static Sampler

### scenario_05_compute_pipeline_with_db_and_pdb

纯计算管线场景（GlobalID=3968），Compute Shader Dispatch 事件，包含 CBV、SRV Buffer、SRV Texture 和 UAV Texture。

- **capture_db**: `capture_db/capture.db`
- **测试用例**:
  - `s5_tc01`: `db-get-event-resource` — 查询事件绑定的全部 15 个资源
- **资源覆盖**: 15 个资源
  - **CS 阶段**: 4 个 CBV (`_RootShaderParameters`, `View`, `VirtualShadowMap`, `ForwardLightStruct`) + 6 个 SRV Buffer + 3 个 SRV Texture + 2 个 UAV Texture

## test_cases.json 格式

每个 `test_cases.json` 文件的结构如下：

```json
{
  "scenario_name": "场景名称",
  "description": "场景描述",
  "capture_db": "capture_db/capture.db",
  "test_cases": [
    {
      "id": "s3_tc01",
      "tool": "db-get-event-resource",
      "description": "测试描述",
      "input": {
        "global_id": 3854
      },
      "expected_output_file": "expected_output/s3_tc01_event_resources.json",
      "assertions": [
        "status == success",
        "resource_count == 26"
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
- `resources contain display_name == '...'`: 验证 resources 数组中存在指定 display_name 的资源

## 清洗规范

所有 `expected_output` JSON 文件已按以下规则清洗，确保字段具备确定性：

1. **抹除以下字段**（因 PIX 版本或环境不同可能变化）：
   - `resource_id`
   - `shader_binding_slot`
   - `descriptor_index`

2. **保留以下确定性字段**：
   - `root_index`, `stage`, `view_type`
   - `resource_name`, `display_name`
   - `resource_dimension`

## 如何运行测试

1. 确保测试用例包含有效的 PIX C++ 导出目录结构和由 `build_capture_database` 生成的 `capture.db` SQLite 数据库。
2. 读取对应场景的 `test_cases.json`。
3. 对每个 `test_case`，使用 `input` 参数调用指定的 MCP 工具。
4. 将实际输出与 `expected_output_file` 中的预期输出对比，或使用 `assertions` 进行断言验证。

## 注意事项

- `capture_db` 目录下需要放置真实的 PIX C++ 导出文件和 SQLite 数据库，测试数据可以通过 `tests/test_capture_db.py` 中的 `_sample_index` 辅助函数生成。
- `expected_output/` 下的文件在首次运行测试时可以通过工具实际输出自动生成，后续用于回归测试。
- `pdb_search_paths` 和 `resolver_path` 参数相关的测试需要本地存在对应的 PDB 文件和解析器可执行文件才能通过。
