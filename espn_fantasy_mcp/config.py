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

# Identifies "my" team so tools can default to it instead of asking every time.
# Resolution order (see formatting.my_team): ESPN_TEAM_ID, then ESPN_TEAM_NAME,
# then auto-detect from SWID (a private-league SWID cookie is the owner's id, so
# no extra config is needed for private leagues).
TEAM_ID = os.environ.get("ESPN_TEAM_ID") or None
TEAM_NAME = os.environ.get("ESPN_TEAM_NAME") or None

# How long (seconds) a fetched League object is trusted before the next tool
# call transparently re-fetches it from ESPN. This is what keeps rosters and
# waiver results from going stale between calls. Set to 0 to cache forever
# (only refresh_league re-fetches). Default: 3 minutes.
try:
    CACHE_TTL_SECONDS = int(os.environ.get("ESPN_CACHE_TTL") or 180)
except ValueError:
    CACHE_TTL_SECONDS = 180


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


# --- Tier-1 external enrichment (see sources.py) --------------------------
# ESPN's own projection is one weak signal. When enabled, tools also pull free,
# no-auth context — Vegas implied totals, opponent defense strength, and Sleeper
# role/injury/market data — so player review isn't a single number. Set
# ESPN_MCP_EXTERNAL=0 to disable all outbound calls (ESPN-fantasy data only).
EXTERNAL_ENABLED = (os.environ.get("ESPN_MCP_EXTERNAL") or "1").strip().lower() not in (
    "0", "false", "no", "off",
)
EXTERNAL_TIMEOUT = _int_env("ESPN_MCP_EXTERNAL_TIMEOUT", 8)
EXTERNAL_TTL_GAME = _int_env("ESPN_MCP_TTL_GAME", 1800)      # odds/defense: 30 min
EXTERNAL_TTL_SLEEPER = _int_env("ESPN_MCP_TTL_SLEEPER", 21600)  # player map: 6 h
EXTERNAL_TTL_TREND = _int_env("ESPN_MCP_TTL_TREND", 3600)    # trending: 1 h

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

Identifying the user's team:
- Team-specific tools (get_team_roster, get_start_sit, get_team_analysis,
  get_team_schedule, get_trade_candidates, and team_name_1 of compare_teams)
  default to the USER'S OWN team when you omit the team argument. For "my roster",
  "who should I start", etc., call them with NO team name — do not ask the user
  who they are. Only pass a team name when they ask about a specific other team.
- Call get_my_team if you need to confirm which team that is. If it reports none
  is configured, then ask the user for their team name.

Evaluating players — DO NOT judge a player by a single projection number:
- ESPN's projection is ONE weak input. get_start_sit, get_team_analysis, etc.
  rank by it for a first pass, but a projection alone is not an analysis.
- For any close or consequential call (a start/sit toss-up, a trade piece, a
  waiver target, "is this player good?"), call analyze_player. It returns a
  multi-factor briefing — recent form, floor/ceiling volatility, opportunity
  (targets/carries), matchup (Vegas implied team total + opponent defense
  rank), depth-chart role, injury/practice status, and market/trending — and
  it flags when the projection diverges from reality. Synthesize those factors;
  explain the WHY, don't just relay the projected points.
- Think in the terms a sharp manager would: a WR's ceiling depends on his QB and
  the team's implied total (a low total or a backup QB caps a good receiver); a
  RB's value is about volume/role, not name; a boom/bust player is a different
  bet in a must-win week than a high-floor one; recent usage beats old points.
- A high implied team total signals a good scoring environment; a low one (or a
  tough opponent-defense rank) is a fade even for a talented player. Weigh
  floor vs ceiling against whether the user needs safety or upside this week.

When the user asks about trades, start/sit decisions, or team improvement:
- Always check league scoring settings first (PPR vs standard changes player values dramatically)
- Look at bye weeks when evaluating roster construction
- Consider the team's remaining schedule strength
- Compare position group strength to the league average, not just raw points
- A "weakness" means a position where the team's starter underperforms the league average at that position
- When suggesting trades, identify what the OTHER team needs too, since a good trade proposal addresses both sides
- Run analyze_player on the key players on BOTH sides before endorsing a trade

On waivers and transactions:
- For "who has waiver priority" or FAAB budgets, use get_waiver_order
- To confirm whether an add/drop/waiver claim actually processed, use get_recent_transactions
- The user's OWN pending (unprocessed) waiver/free-agent claims ARE available: use get_pending_claims to list them and cancel_pending_claim to withdraw one. This only works for the user's own team, not other owners' pending claims
- Adding a player usually requires a free roster slot; check get_team_roster's capacity line (it reports open slots) before assuming a drop is or isn't needed

Making roster changes (WRITE actions — add_drop_player, submit_waiver_claim, set_lineup):
- These change the user's real ESPN team. They act ONLY on the user's own team and require the private-league cookies (ESPN_S2/ESPN_SWID) to be configured.
- They are two-step by design. First call each tool WITHOUT confirm to show the user the exact preview it returns, then WAIT for the user to approve before calling again with confirm=true. Never pass confirm=true on the first call or without the user having seen and okayed the specific move.
- add_drop_player is immediate and releases the dropped player to the league at once (effectively irreversible). submit_waiver_claim only queues a claim for the next waiver run and can lose. Make sure the user picked the one they meant.
- Prefer resolving players and roster spots first (get_free_agents, get_team_roster, get_start_sit) so the add/drop/lineup names you pass are unambiguous.
- After a successful add/drop, roster data is refreshed automatically; you can re-read the roster to show the result. A submitted waiver claim will NOT appear in any read tool until it processes.
"""
