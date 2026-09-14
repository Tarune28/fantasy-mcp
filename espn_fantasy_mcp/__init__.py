"""ESPN Fantasy Football MCP server package.

A Model Context Protocol server (stdio) exposing an ESPN Fantasy Football
league to Claude Desktop via the espn-api library.
"""

from __future__ import annotations

from .app import main, mcp

__all__ = ["main", "mcp"]
