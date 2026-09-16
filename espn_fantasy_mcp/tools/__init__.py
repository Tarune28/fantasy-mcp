"""Importing this package registers every @mcp.tool() with the server.

Each submodule attaches its tools to the shared ``mcp`` instance at import
time, so importing the package is all that's needed to register them.
"""

from __future__ import annotations

from . import (  # noqa: F401
    advice,
    analysis,
    league,
    pending,
    players,
    roster_moves,
    teams,
    waivers,
)

__all__ = [
    "advice",
    "analysis",
    "league",
    "pending",
    "players",
    "roster_moves",
    "teams",
    "waivers",
]
