"""The MCP server instance and entry point.

The server object lives here on its own so tool modules can import it without
creating an import cycle. Tools are registered lazily inside ``main`` (importing
the ``tools`` package runs the ``@mcp.tool()`` decorators).
"""

from __future__ import annotations

from .config import GUIDANCE, SERVER_NAME

try:
    # mcp >= 2.0 renamed FastMCP to MCPServer.
    from mcp.server.mcpserver import MCPServer as _MCPServer
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _MCPServer


try:
    mcp = _MCPServer(SERVER_NAME, instructions=GUIDANCE)
except TypeError:  # older signatures without an instructions kwarg
    mcp = _MCPServer(SERVER_NAME)


def main() -> None:
    """Register all tools and run the MCP server over stdio (Claude Desktop)."""
    from . import tools  # noqa: F401  (importing registers every @mcp.tool())

    mcp.run(transport="stdio")
