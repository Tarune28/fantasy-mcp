"""Configuration and constants for the ESPN Fantasy Football MCP server.

All ESPN credentials come from environment variables so nothing sensitive is
ever committed:

    ESPN_LEAGUE_ID   (required) - your ESPN league id
    ESPN_YEAR        (optional) - defaults to the current calendar year
    ESPN_S2          (optional) - cookie for private leagues
    ESPN_SWID        (optional) - cookie for private leagues

If ESPN_S2 / ESPN_SWID are not set, the league is treated as public.
"""

from __future__ import annotations

import datetime
import os

SERVER_NAME = "espn-fantasy"

LEAGUE_ID = os.environ.get("ESPN_LEAGUE_ID")
YEAR = int(os.environ.get("ESPN_YEAR") or datetime.datetime.now().year)
ESPN_S2 = os.environ.get("ESPN_S2") or None
SWID = os.environ.get("ESPN_SWID") or None

# Lineup slots that are not starting spots.
BENCH_SLOTS = {"BE", "Bench", "IR"}

# ESPN uses "D/ST" internally; users usually type "DST".
POSITION_ALIASES = {
    "DST": "D/ST",
    "DEF": "D/ST",
    "D": "D/ST",
    "PK": "K",
}

# Surfaced to the MCP client (Claude) as server instructions so it frames
# fantasy advice correctly.
GUIDANCE = """\
This MCP server provides full access to an ESPN Fantasy Football league.
When the user asks about trades, start/sit decisions, or team improvement:
- Always check league scoring settings first (PPR vs standard changes player values dramatically)
- Look at bye weeks when evaluating roster construction
- Consider the team's remaining schedule strength
- Compare position group strength to the league average, not just raw points
- A "weakness" means a position where the team's starter underperforms the league average at that position
- When suggesting trades, identify what the OTHER team needs too, since a good trade proposal addresses both sides
"""
