"""A tiny MCP server for tests."""

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

mcp = MCPServer("demo")
NOTES: list[str] = []


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@mcp.tool()  # no annotations: Naman must treat it as a write
def save_note(text: str) -> str:
    """Save a note."""
    NOTES.append(text)
    return f"saved note #{len(NOTES)}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def boom() -> str:
    """Always fails."""
    raise RuntimeError("kaboom")


if __name__ == "__main__":
    mcp.run()
