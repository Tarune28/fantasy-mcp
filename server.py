"""Entry point for the ESPN Fantasy Football MCP server.

Claude Desktop runs this file by path (see README). It just delegates to the
espn_fantasy_mcp package, which holds the config, client, helpers, and tools.
"""

from __future__ import annotations

from espn_fantasy_mcp import main

if __name__ == "__main__":
    main()
