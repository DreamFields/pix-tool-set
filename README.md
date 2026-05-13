# pix-tool-set

A unified CLI and MCP toolkit for analyzing PIX captures through their exported C++ projects.

## Goals

- Keep MCP tool names and CLI command names aligned.
- Register every tool once and expose it through both CLI and MCP where appropriate.
- Export a PIX `.wpix` capture to a C++ project via `pixtool.exe` through a dedicated tool.
- Ensure a usable PIX C++ export exists before capture-dependent analysis runs.
- Analyze shader events, event shader sources, and resource history from exported C++ files.

## Quick start

```powershell
python -m pip install -e .[dev]
pix-tool-set list-tools
```

If you need to reinstall while MCP clients are running, use the provided script to force-kill running server processes first:

```powershell
.\scripts\reinstall.ps1
```

The script force-kills `pix_tool_set` processes and keeps a background killer job running during `pip install` to prevent MCP clients from immediately restarting the server and locking `pix_tool_set.exe`.

During development you can also run without installing:

```powershell
$env:PYTHONPATH = "src"
python -m pix_tool_set.cli list-tools
```

## Core tools

The CLI command name and MCP tool name are the same.

### Export a .wpix capture to a C++ project

```powershell
# 默认导出到 PIX 文件同目录下的 frame\ 文件夹
pix-tool-set export-to-cpp --capture-path "G:\captures\frame.wpix"
# 指定导出目录
pix-tool-set export-to-cpp --capture-path "G:\captures\frame.wpix" --export-dir "G:\captures\frame"
pix-tool-set export-to-cpp --capture-path "G:\captures\frame.wpix" --force
```

`export-to-cpp` invokes `pixtool.exe` to convert a `.wpix` file into a C++ project directory. It is the first step before any capture-dependent analysis. If the target directory already contains a valid export, the tool skips re-exporting unless `--force` is given.

### Analyze an exported C++ project

```powershell
# 默认导出目录与 PIX 文件同路径，以 PIX 文件名作为文件夹名
pix-tool-set check-cpp-export --export-dir "G:\captures\frame"
pix-tool-set build-index --export-dir "G:\captures\frame"
pix-tool-set extract-shader-events-tree --export-dir "G:\captures\frame" --output-path "G:\pix-tool-set\examples\shader_events_tree.json"
pix-tool-set get-event-shader-source --export-dir "G:\captures\frame" --global-id 13 --output-path "G:\pix-tool-set\examples\shader_source_13.json"
pix-tool-set get-event-resource-history --export-dir "G:\captures\frame" --global-id 13 --window 10
```

## Documentation

- Resource binding fixes and troubleshooting notes: [`doc/resource-history-fixes.md`](doc/resource-history-fixes.md)

## MCP server

After installation, MCP clients can start the server with a single command:

```jsonc
{
  "servers": {
    "pix-tool-set": {
      "command": "pix_tool_set"
    }
  }
}
```

For direct terminal testing:

```powershell
pix_tool_set
```

The MCP server loads the same registry as the CLI. Tools that declare `requires_cpp_export=True` run the same C++ export validation before their handlers execute.

## Debugging MCP features

Use this checklist when adding MCP tools or connecting this project to a new MCP client.

### 1. Verify the local CLI and registry first

```powershell
python -m pip install -e .[dev]
pix-tool-set list-tools
```

During source-tree development, run without installation by setting `PYTHONPATH`:

```powershell
$env:PYTHONPATH = "src"
python -m pix_tool_set.cli list-tools
```

`list-tools` is the fastest sanity check because the MCP server and CLI load the same tool registry.

### 2. Start the MCP server directly

```powershell
pix_tool_set
```

During source-tree development before installation, keep using the module form:

```powershell
$env:PYTHONPATH = "src"
python -m pix_tool_set.mcp_server
```

The MCP server uses stdio transport, so it may appear to wait without printing normal logs. That is expected. Stop it with `Ctrl+C` after confirming there is no startup exception.

### 3. Inspect tools with MCP Inspector

```powershell
npx @modelcontextprotocol/inspector pix_tool_set
```

In the inspector:

1. Connect to the server.
2. Confirm the MCP tool list matches `pix-tool-set list-tools`.
3. Call a low-cost tool first, such as `list-tools` if exposed by your client, or a tool with a known-valid `export_dir`.
4. For capture-dependent tools, test `check-cpp-export` before shader or resource-history tools.

If PIX auto-export is part of the test, set `PIXTOOL_PATH` explicitly:

```powershell
$env:PIXTOOL_PATH = "C:\Program Files\Microsoft PIX\2603.25\pixtool.exe"
```

### 4. Configure CodeBuddy / VS Code

Use the installed package entry point when the project is installed:

```jsonc
{
  "servers": {
    "pix-tool-set": {
      "command": "pix_tool_set",
      "env": {
        "PIXTOOL_PATH": "C:\\Program Files\\Microsoft PIX\\2603.25\\pixtool.exe"
      }
    }
  }
}
```

When running from the source tree without installation, use the module form and add `PYTHONPATH`:

```jsonc
{
  "servers": {
    "pix-tool-set": {
      "type": "stdio",
      "command": "python",
      "args": ["-m", "pix_tool_set.mcp_server"],
      "env": {
        "PYTHONPATH": "G:\\pix-tool-set\\src",
        "PIXTOOL_PATH": "C:\\Program Files\\Microsoft PIX\\2603.25\\pixtool.exe"
      }
    }
  }
}
```

Restart the MCP client after changing configuration or tool definitions, because MCP tools are registered when the server process starts.

### 5. Attach a Python debugger

For VS Code or another `debugpy` client:

```powershell
python -m pip install debugpy
python -m debugpy --listen 5678 -m pix_tool_set.mcp_server
```

When debugging from the source tree without installation, set `PYTHONPATH` first:

```powershell
$env:PYTHONPATH = "src"
python -m debugpy --listen 5678 -m pix_tool_set.mcp_server
```

Attach the debugger to port `5678`. Use `--wait-for-client` only when debugging startup, because MCP clients may time out while the server is paused.

### 6. Debugging a new tool

1. Add or update the tool definition under `src/pix_tool_set/tools/`.
2. Ensure the definition has a canonical name, CLI/MCP exposure, description, parameter schema, and handler.
3. Run `pix-tool-set list-tools` and confirm the new tool appears with the expected CLI and MCP names.
4. Test the same handler through CLI first, for example:

   ```powershell
   pix-tool-set run <tool-name> --json-args '{"export_dir":"G:\\captures\\frame"}'
   ```

5. Test the MCP call through MCP Inspector with the same arguments.
6. For tools with `requires_cpp_export=True`, verify invalid `export_dir`, missing key files, and optional `auto_export` behavior.
7. Keep regular diagnostics off stdout for stdio MCP; use structured tool results and stderr-only temporary diagnostics.

### 7. Common debugging issues

| Symptom | What to check |
|---------|---------------|
| Tool appears in CLI but not MCP | Confirm the tool exposure includes `mcp`, then restart the MCP server/client |
| Tool does not appear anywhere | Ensure `load_builtin_tools()` imports the module that registers the tool |
| `ModuleNotFoundError: pix_tool_set` | Install with `python -m pip install -e .[dev]` or set `PYTHONPATH=src` |
| Capture-dependent tool fails before handler logic | Run `check-cpp-export` with the same `export_dir` and inspect the structured error |
| `pixtool.exe` not found | Set `PIXTOOL_PATH` or pass `pixtool_path` when testing auto-export |
| JSON / protocol errors | Do not write normal logs to stdout in stdio MCP servers |
| Windows path errors | Prefer absolute paths; escape backslashes in JSON strings or use forward slashes |

## C++ export policy

There are two ways to obtain a C++ export:

1. **Standalone `export-to-cpp` tool** — Explicitly export a `.wpix` capture file to a C++ project directory. This is the recommended entry point: call `export-to-cpp` first, then pass the resulting `export_dir` to analysis tools.

   ```powershell
pix-tool-set export-to-cpp --capture-path "G:\captures\frame.wpix"
  ```

  By default, the C++ export is created in the same directory as the `.wpix` file, using the PIX file name (without extension) as the folder name. For example, `G:\\captures\\frame.wpix` will be exported to `G:\\captures\\frame\\`.

  The tool validates the `.wpix` extension, locates `pixtool.exe`, runs the export, and verifies the output. If the target directory already contains a valid export it skips re-exporting unless `--force` is given.

2. **Automatic export inside capture-dependent tools** — Every capture-dependent tool also accepts `export_dir` and optional `capture_path` / `auto_export` arguments as a fallback.

- If `export_dir` is valid, the tool reuses it.
- If `export_dir` is missing and `auto_export` is false, the tool returns a structured error.
- If `auto_export` is true, the tool tries to invoke `pixtool.exe` from `pixtool_path`, `PIXTOOL_PATH`, or the default PIX install directory.

## Migration notes

The first migrated capabilities are:

- `extract-shader-events-tree`: replaces the old standalone shader event extraction flow and writes a tree JSON.
- `get-event-shader-source`: locates the event PSO and extracted shader blobs by `global_id`; optional resolver integration can be supplied through `resolver_path`.
- `get-event-resource-history`: traces direct and nearby resource operations for a `global_id` and writes the full result to JSON.

All tool outputs use the same structured result shape: `status`, `data`, `output_paths`, `diagnostics`, and optional `error`.
