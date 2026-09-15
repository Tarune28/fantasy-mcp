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

# How long (seconds) a fetched League object is trusted before the next tool
# call transparently re-fetches it from ESPN. This is what keeps rosters and
# waiver results from going stale between calls. Set to 0 to cache forever
# (only refresh_league re-fetches). Default: 3 minutes.
try:
    CACHE_TTL_SECONDS = int(os.environ.get("ESPN_CACHE_TTL") or 180)
except ValueError:
    CACHE_TTL_SECONDS = 180

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

On waivers and transactions:
- For "who has waiver priority" or FAAB budgets, use get_waiver_order
- To confirm whether an add/drop/waiver claim actually processed, use get_recent_transactions
- ESPN's API does NOT expose a user's PENDING (unprocessed) waiver claims. Do not claim to see, list, or confirm someone's pending claims. If asked, say the server can't read pending claims and point them to the ESPN app/site, then offer waiver priority, the processing schedule, or completed transactions instead
- Adding a player usually requires a free roster slot; check get_team_roster's capacity line (it reports open slots) before assuming a drop is or isn't needed
"""
