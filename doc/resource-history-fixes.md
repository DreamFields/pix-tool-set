# Resource history fixes and troubleshooting notes

This document records the recent fixes and pitfalls around PIX resource binding and access-history analysis.

## Scope

The fixes focus on `get-event-resource` and `get-resource-access-history` for exported PIX C++ projects:

- Resolve shader-declared `CBV`, `SRV`, `UAV`, and static sampler bindings from recovered shader source.
- Match descriptor table entries back to shader declarations instead of returning only raw descriptor resources.
- Handle compute and graphics pipeline layouts, including input assembler and output merger resources.
- Keep CLI and MCP tool names unified through the shared registry.

## Fixes

### Nullable CLI/MCP parameters

Some client calls pass optional numeric arguments as `null` instead of omitting them.

- `analyze-events` accepts nullable `top_limit` and `sample_limit` and falls back to defaults.
- `get-resource-access-history` accepts nullable `descriptor_scan_count` and falls back to the default scan count.

Pitfall: treat `None` from JSON clients as a caller asking for the default value, not as an invalid integer.

### Shader declaration parsing

Resource binding resolution now parses declarations from recovered shader source:

- Constant buffers with explicit registers, such as `cbuffer View : register(b0)`.
- Constant buffers without explicit registers.
- `Texture*`, `Buffer`, `StructuredBuffer`, `ByteAddressBuffer`, and `RW*` resources.
- Static samplers with `register(sN, spaceM)`.
- Static samplers without explicit registers.

Pitfall: resource declarations inside `cbuffer` bodies must be ignored. The parser removes constant-buffer bodies before scanning resource declarations so member variables are not mistaken for resources.

### Multiple CBVs and fallback names

Some captures bind more root `CBV`s than the shader source clearly declares.

- Missing `CBV` declarations are filled with stable fallback names such as `_RootShaderParameters`, `View`, and `ReflectionCaptureSM5`.
- Slot numbers are kept deterministic.
- Constant-buffer usage statistics help select likely `CBV`s when there are more declarations than root bindings.

Pitfall: generic buffer resource names like `Resource Allocator Underlying Buffer` do not identify the shader binding. Display names must combine the resource name with the shader declaration name.

### Descriptor table matching

Descriptor entries are matched to shader declarations by view type, resource dimension, name tokens, and descriptor format hints.

- `SRV` and `UAV` bindings are matched independently.
- Texture descriptors are not matched to buffer declarations, and buffer descriptors are not matched to texture declarations.
- Name-token scoring handles cases such as normal/tangent/color/position buffers.
- Descriptor tables can overlap; the resolver keeps the table with the most specific root descriptor range.

Pitfall: scanning a fixed small number of descriptors is not enough when shader declarations exceed the default scan window. The scan window must be at least the number of declared `SRV` plus `UAV` resources.

### Static sampler filtering

Static samplers are included as resources with `view_type` set to `Static Sampler`.

- Register space is preserved when present.
- Unregistered samplers receive deterministic slots.
- In graphics shaders, sampler noise is reduced by keeping samplers related to texture declarations when possible.

Pitfall: static samplers do not have resource IDs or descriptor writes, so they need a separate resolution path.

### Graphics pipeline support

Graphics events are resolved across pipeline stages instead of being treated as compute-only events.

- Input assembler `VB` and `IB` resources are included.
- Vertex and pixel shader resources are resolved from stage-specific shader source.
- Output merger `RTV`, depth, and stencil resources are included.
- Root `CBV`s and descriptor tables are partitioned between stages by binding order and table scores.

Pitfall: graphics root bindings may appear in stage runs rather than in simple numeric root-index order. Use line/order metadata when available, then score candidate tables by declared resources.

### Descriptor heap disambiguation

Descriptor indices can repeat across different heaps.

- Descriptor lookup filters writes by `heap_id` when the root binding provides one.
- This avoids matching resources from another heap that happens to use the same descriptor index.

Pitfall: descriptor index alone is not always globally unique in exported PIX code.

### Access history rows

`get-resource-access-history` builds access rows from resource references and adds the target event shader binding row.

- Copy operations classify source and destination as read/write correctly.
- Barriers and transitions are treated as read/write state changes.
- `UAV` shader bindings default to `Read/Write` and `STATE_UNORDERED_ACCESS`.
- Duplicate rows are removed by event, binding, state, line, and text.

Pitfall: the event itself may not contain a direct API reference to the resource even though the shader binding uses it. Add a shader-binding row for the selected event.

## Validation checklist

Run the focused tests after editing resource binding logic:

```powershell
python -m pytest tests/test_registry_and_export.py
```

Important cases covered by the tests:

- Built-in tools are registered once.
- CLI and MCP names stay identical.
- Minimal C++ export validation accepts the required PIX files.
- Nullable optional parameters use defaults.
- Compute shader resources resolve declared `CBV`, `SRV`, `UAV`, and static sampler bindings.
- Overlapping descriptor tables resolve to the expected shader declarations.
- Graphics events include `IA`, shader-stage, `OM`, depth, and stencil resources.

## Implementation guardrails

- Keep user-facing outputs structured with `status`, `data`, `output_paths`, `diagnostics`, and optional `error`.
- Do not write regular diagnostics to stdout in stdio MCP mode.
- Prefer absolute paths in command examples.
- Keep examples generic and avoid machine- or project-specific export directory names.
- Add regression tests for every new capture layout before changing matching heuristics.