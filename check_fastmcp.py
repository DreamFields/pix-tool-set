import inspect
from mcp.server.fastmcp.server import FastMCP
from mcp.server.fastmcp.tools.base import Tool

with open("fastmcp_tool_src.txt", "w", encoding="utf-8") as f:
    f.write("=== FastMCP.tool ===\n")
    f.write(inspect.getsource(FastMCP.tool))
    f.write("\n\n=== FastMCP.add_tool ===\n")
    f.write(inspect.getsource(FastMCP.add_tool))
    f.write("\n\n=== Tool.from_function ===\n")
    f.write(inspect.getsource(Tool.from_function))

print("Done - check fastmcp_tool_src.txt")
