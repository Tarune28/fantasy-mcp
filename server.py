"""ESPN Fantasy Football MCP Server.

This MCP server provides full access to an ESPN Fantasy Football league.
When the user asks about trades, start/sit decisions, or team improvement:
- Always check league scoring settings first (PPR vs standard changes player values dramatically)
- Look at bye weeks when evaluating roster construction
- Consider the team's remaining schedule strength
- Compare position group strength to the league average, not just raw points
- A "weakness" means a position where the team's starter underperforms the league average at that position
- When suggesting trades, identify what the OTHER team needs too, since a good trade proposal addresses both sides

Configuration via environment variables:
    ESPN_LEAGUE_ID   (required) - your ESPN league id
    ESPN_YEAR        (optional) - defaults to the current calendar year
    ESPN_S2          (optional) - cookie for private leagues
    ESPN_SWID        (optional) - cookie for private leagues

If ESPN_S2 / ESPN_SWID are not set, the league is treated as public.

Transport: stdio (for Claude Desktop).
"""

from __future__ import annotations

import datetime
import os
from typing import Any, Optional

try:
    # mcp >= 2.0 renamed FastMCP to MCPServer.
    from mcp.server.mcpserver import MCPServer as _MCPServer
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _MCPServer

try:
    from espn_api.football import League
except ImportError as exc:  # pragma: no cover - surfaced at runtime
    raise SystemExit(
        "The 'espn-api' package is required. Install it with: pip install espn-api"
    ) from exc


# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #

LEAGUE_ID = os.environ.get("ESPN_LEAGUE_ID")
YEAR = int(os.environ.get("ESPN_YEAR") or datetime.datetime.now().year)
ESPN_S2 = os.environ.get("ESPN_S2") or None
SWID = os.environ.get("ESPN_SWID") or None

BENCH_SLOTS = {"BE", "Bench", "IR"}

# ESPN uses "D/ST" internally; users usually type "DST".
POSITION_ALIASES = {
    "DST": "D/ST",
    "DEF": "D/ST",
    "D": "D/ST",
    "PK": "K",
}


mcp = _MCPServer("espn-fantasy")


# --------------------------------------------------------------------------- #
# League caching                                                              #
# --------------------------------------------------------------------------- #

_league: Optional[League] = None


def get_league() -> League:
    """Return a cached League object, initializing it on first use.

    Raises RuntimeError with a helpful message if configuration is missing or
    the league cannot be loaded.
    """
    global _league
    if _league is not None:
        return _league

    if not LEAGUE_ID:
        raise RuntimeError(
            "ESPN_LEAGUE_ID is not set. Add it to the server's environment "
            "(see the claude_desktop_config.json snippet in the README)."
        )

    try:
        _league = League(
            league_id=int(LEAGUE_ID),
            year=YEAR,
            espn_s2=ESPN_S2,
            swid=SWID,
        )
    except Exception as exc:  # noqa: BLE001 - report any init failure clearly
        raise RuntimeError(
            f"Could not load league {LEAGUE_ID} for {YEAR}: {exc}. "
            "For private leagues, verify ESPN_S2 and ESPN_SWID are set correctly."
        ) from exc

    return _league


# --------------------------------------------------------------------------- #
# Small helpers                                                               #
# --------------------------------------------------------------------------- #


def _norm(text: Any) -> str:
    """Lower-cased, stripped string for fuzzy comparison."""
    return str(text or "").strip().lower()


def owner_name(team: Any) -> str:
    """Return a readable owner name for a Team across espn-api versions."""
    # Newer espn-api: team.owners is a list of dicts.
    owners = getattr(team, "owners", None)
    if owners:
        names = []
        for o in owners:
            if isinstance(o, dict):
                first = o.get("firstName") or o.get("firstname") or ""
                last = o.get("lastName") or o.get("lastname") or ""
                full = f"{first} {last}".strip()
                names.append(full or o.get("id") or "Unknown")
            else:
                names.append(str(o))
        joined = ", ".join(n for n in names if n)
        if joined:
            return joined
    # Older espn-api: team.owner is a plain string.
    owner = getattr(team, "owner", None)
    if owner:
        return str(owner)
    return "Unknown owner"


def current_week(league: League) -> int:
    """Best-effort current scoring week."""
    for attr in ("current_week", "nfl_week"):
        val = getattr(league, attr, None)
        if isinstance(val, int) and val > 0:
            return val
    return 1


def week_is_complete(league: League, week: int) -> bool:
    """True if every player in the given week's box scores has finished playing.

    Uses each BoxPlayer's game_played (100 == game over). ESPN keeps the scoring
    period on the finished week until ~Tuesday, so this lets us tell "current
    week" from "current week is done, look ahead".
    """
    try:
        box = league.box_scores(week)
    except Exception:  # noqa: BLE001
        return False
    total = done = in_progress = 0
    for m in box:
        lineup = (getattr(m, "home_lineup", None) or []) + (
            getattr(m, "away_lineup", None) or []
        )
        for p in lineup:
            gp = getattr(p, "game_played", None)
            if gp is None:
                continue
            total += 1
            if gp >= 100:
                done += 1
            elif gp > 0:
                in_progress += 1
    if total == 0 or in_progress > 0:
        # No data, or at least one game is actively being played.
        return False
    # Tolerate a few stragglers (Monday-night, suspended, or inactive players)
    # that never reach 100, so the week still counts as finished.
    return (done / total) >= 0.85


def upcoming_week(league: League) -> int:
    """The week to make lineup decisions for.

    Normally the current scoring period, but if that week's games are all
    complete (and ESPN hasn't rolled the period over yet), the next week.
    """
    cur = current_week(league)
    final = getattr(league, "finalScoringPeriod", None) or getattr(
        league.settings, "reg_season_count", 18
    )
    if cur < final and week_is_complete(league, cur):
        return cur + 1
    return cur


def latest_projection_week(league: League) -> int:
    """Highest week for which ESPN has posted per-week player projections."""
    best = 0
    try:
        for p in league.teams[0].roster:
            st = getattr(p, "stats", None)
            if isinstance(st, dict):
                for k in st:
                    if isinstance(k, int) and k > best:
                        # week 0 is the season bucket; skip it.
                        best = max(best, k)
    except Exception:  # noqa: BLE001
        pass
    return best


def is_starter(player: Any) -> bool:
    """True if the player occupies a starting lineup slot (not bench/IR)."""
    slot = getattr(player, "lineupSlot", None) or getattr(player, "slot_position", None)
    return bool(slot) and slot not in BENCH_SLOTS


def player_slot(player: Any) -> str:
    return (
        getattr(player, "lineupSlot", None)
        or getattr(player, "slot_position", None)
        or "-"
    )


def player_projected(player: Any, week: Optional[int] = None) -> float:
    """Best-effort projected points for a player.

    Prefers a specific week's projection, then season projected average, then
    projected total. Returns 0.0 if nothing is available.
    """
    # BoxPlayer objects carry a per-week projection directly.
    direct = getattr(player, "projected_points", None)
    if isinstance(direct, (int, float)) and direct:
        return float(direct)

    stats = getattr(player, "stats", None)
    if isinstance(stats, dict) and week is not None:
        wk = stats.get(week) or stats.get(str(week))
        if isinstance(wk, dict):
            proj = wk.get("projected_points")
            if isinstance(proj, (int, float)):
                return float(proj)

    for attr in ("projected_avg_points", "projected_total_points"):
        val = getattr(player, attr, None)
        if isinstance(val, (int, float)):
            return float(val)

    if isinstance(direct, (int, float)):
        return float(direct)
    return 0.0


def player_avg(player: Any) -> float:
    """Best-effort average points per game."""
    for attr in ("avg_points", "projected_avg_points"):
        val = getattr(player, attr, None)
        if isinstance(val, (int, float)) and val:
            return float(val)
    total = getattr(player, "total_points", None)
    if isinstance(total, (int, float)):
        return float(total)
    return 0.0


def player_position(player: Any) -> str:
    return getattr(player, "position", None) or "?"


def injury_flag(player: Any) -> str:
    status = getattr(player, "injuryStatus", None)
    if status and status not in ("ACTIVE", "NORMAL"):
        return str(status)
    if getattr(player, "injured", False):
        return "INJURED"
    return ""


def find_team(league: League, query: str) -> Any:
    """Fuzzy-match a team by team name or owner name (case-insensitive substring).

    Returns the Team, or None if there is no match.
    """
    q = _norm(query)
    if not q:
        return None

    teams = league.teams
    # Exact team-name match wins first.
    for team in teams:
        if _norm(team.team_name) == q:
            return team
    # Then substring on team name.
    for team in teams:
        if q in _norm(team.team_name):
            return team
    # Then substring on owner name.
    for team in teams:
        if q in _norm(owner_name(team)):
            return team
    return None


def team_choices(league: League) -> str:
    """Formatted list of valid team names + owners for error messages."""
    lines = ["Valid teams:"]
    for team in league.teams:
        lines.append(f"  - {team.team_name} (owner: {owner_name(team)})")
    return "\n".join(lines)


def scoring_kind(league: League) -> str:
    """Return 'PPR', 'Half-PPR', or 'Standard' based on reception scoring."""
    fmt = getattr(league.settings, "scoring_format", None)
    if not fmt:
        return "Unknown"
    for item in fmt:
        if not isinstance(item, dict):
            continue
        abbr = str(item.get("abbr") or item.get("abbrev") or "").upper()
        if abbr in ("REC", "RECEPTION"):
            pts = item.get("points", 0) or 0
            if pts >= 1:
                return "PPR"
            if pts >= 0.5:
                return "Half-PPR"
            return "Standard"
    return "Standard"


def roster_slot_counts(league: League) -> dict[str, int]:
    """Best-effort mapping of lineup slot -> count."""
    for attr in ("position_slot_counts", "roster_positions", "roster"):
        val = getattr(league.settings, attr, None)
        if isinstance(val, dict) and val:
            return {k: v for k, v in val.items() if v}
    # Fall back to inferring from an actual roster's lineup slots.
    try:
        counts: dict[str, int] = {}
        for player in league.teams[0].roster:
            slot = player_slot(player)
            counts[slot] = counts.get(slot, 0) + 1
        return counts
    except Exception:  # noqa: BLE001
        return {}


def fmt_ts(ts: Any) -> str:
    """Format an ESPN epoch-ms timestamp as a date, if possible."""
    if not ts:
        return "Not set"
    try:
        seconds = float(ts) / 1000.0
        return datetime.datetime.fromtimestamp(seconds).strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return str(ts)


def league_position_averages(league: League, week: int) -> dict[str, float]:
    """Average projected points of *starters* by position across the league."""
    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for team in league.teams:
        for player in getattr(team, "roster", []) or []:
            if not is_starter(player):
                continue
            pos = player_position(player)
            totals[pos] = totals.get(pos, 0.0) + player_projected(player, week)
            counts[pos] = counts.get(pos, 0) + 1
    return {pos: totals[pos] / counts[pos] for pos in totals if counts.get(pos)}


# --------------------------------------------------------------------------- #
# Tools                                                                       #
# --------------------------------------------------------------------------- #


@mcp.tool()
def refresh_league() -> str:
    """Force a re-fetch of all league data from ESPN.

    Use this when scores, rosters, or transactions may have changed since the
    server started. All other tools use cached data until this is called.
    """
    global _league
    _league = None
    try:
        league = get_league()
    except RuntimeError as exc:
        return f"Error: {exc}"
    return (
        f"League data refreshed: {league.settings.name} "
        f"({YEAR}), currently week {current_week(league)}."
    )


@mcp.tool()
def get_league_overview() -> str:
    """Get a high-level overview of the league.

    Returns the league name, season year, current week, number of teams, and
    key settings (scoring type, playoff team count).
    """
    try:
        league = get_league()
    except RuntimeError as exc:
        return f"Error: {exc}"

    s = league.settings
    cur = current_week(league)
    up = upcoming_week(league)
    week_line = f"- Current week: {cur}"
    if up != cur:
        week_line += f"  (Week {cur} is final — set lineups for Week {up})"
    lines = [
        f"# {s.name} ({YEAR})",
        "",
        week_line,
        f"- Teams: {len(league.teams)}",
        f"- Scoring: {scoring_kind(league)}",
        f"- Regular season weeks: {getattr(s, 'reg_season_count', 'N/A')}",
        f"- Playoff teams: {getattr(s, 'playoff_team_count', 'N/A')}",
        f"- Trade deadline: {fmt_ts(getattr(s, 'trade_deadline', None))}",
    ]
    return "\n".join(lines)


@mcp.tool()
def get_standings() -> str:
    """Get league standings ranked by record and points for.

    Includes each team's wins-losses(-ties), points for/against, and current
    win/loss streak.
    """
    try:
        league = get_league()
    except RuntimeError as exc:
        return f"Error: {exc}"

    teams = sorted(
        league.teams,
        key=lambda t: (
            getattr(t, "wins", 0),
            getattr(t, "points_for", 0),
        ),
        reverse=True,
    )

    header = f"{'#':<3}{'Team':<28}{'Rec':<9}{'PF':>9}{'PA':>9}  Streak"
    lines = [f"# Standings — {league.settings.name}", "", header, "-" * len(header)]
    for i, t in enumerate(teams, 1):
        ties = getattr(t, "ties", 0) or 0
        rec = f"{getattr(t, 'wins', 0)}-{getattr(t, 'losses', 0)}"
        if ties:
            rec += f"-{ties}"
        streak_len = getattr(t, "streak_length", 0) or 0
        streak_type = getattr(t, "streak_type", "") or ""
        streak = f"{streak_type[:1]}{streak_len}" if streak_len else "-"
        lines.append(
            f"{i:<3}{t.team_name[:27]:<28}{rec:<9}"
            f"{getattr(t, 'points_for', 0):>9.1f}{getattr(t, 'points_against', 0):>9.1f}"
            f"  {streak}"
        )
    return "\n".join(lines)


@mcp.tool()
def get_teams() -> str:
    """List every team in the league with its team name and owner.

    Use this first so you know the exact names to pass to other tools. Team
    lookups elsewhere accept either the team name or the owner name.
    """
    try:
        league = get_league()
    except RuntimeError as exc:
        return f"Error: {exc}"

    lines = [f"# Teams in {league.settings.name}", ""]
    for t in league.teams:
        rec = f"{getattr(t, 'wins', 0)}-{getattr(t, 'losses', 0)}"
        lines.append(f"- {t.team_name}  (owner: {owner_name(t)}, {rec})")
    return "\n".join(lines)


@mcp.tool()
def get_team_roster(team_name: str, week: Optional[int] = None) -> str:
    """Get the full roster for any team in the league.

    Args:
        team_name: Team name or owner name (fuzzy, case-insensitive substring).
        week: Week to project for. Defaults to the upcoming (actionable) week —
            if the current week's games are already final, this is next week.

    Shows each player's name, position, projected points, injury status, and
    whether they are in a starting slot or on the bench.
    """
    try:
        league = get_league()
    except RuntimeError as exc:
        return f"Error: {exc}"

    team = find_team(league, team_name)
    if team is None:
        return f"No team matched '{team_name}'.\n\n{team_choices(league)}"

    wk = week or upcoming_week(league)
    posted = wk <= latest_projection_week(league)
    starters = [p for p in team.roster if is_starter(p)]
    bench = [p for p in team.roster if not is_starter(p)]

    def row(p: Any) -> str:
        inj = injury_flag(p)
        inj = f"  [{inj}]" if inj else ""
        return (
            f"  {player_slot(p):<9}{player_position(p):<5}"
            f"{getattr(p, 'name', '?')[:24]:<25}"
            f"proj {player_projected(p, wk):>6.1f}{inj}"
        )

    proj_note = (
        f"Week {wk} projections"
        if posted
        else f"Week {wk} outlook — ESPN hasn't posted Week {wk} projections yet "
        f"(usually ~Tue); figures are season-average per-game placeholders"
    )
    lines = [
        f"# {team.team_name} — roster (owner: {owner_name(team)})",
        proj_note,
        "",
        "## Starters",
    ]
    lines += [row(p) for p in starters] or ["  (none)"]
    lines += ["", "## Bench / IR"]
    lines += [row(p) for p in bench] or ["  (none)"]
    return "\n".join(lines)


@mcp.tool()
def get_matchups(week: Optional[int] = None) -> str:
    """Get all matchups for a given week with scores and projections.

    Args:
        week: Week number. Defaults to the current week.
    """
    try:
        league = get_league()
    except RuntimeError as exc:
        return f"Error: {exc}"

    wk = week or current_week(league)
    try:
        box = league.box_scores(wk)
    except Exception as exc:  # noqa: BLE001
        return f"Could not load matchups for week {wk}: {exc}"

    if not box:
        return f"No matchups found for week {wk}."

    lines = [f"# Week {wk} matchups", ""]
    for m in box:
        home = getattr(m, "home_team", None)
        away = getattr(m, "away_team", None)
        home_name = getattr(home, "team_name", "BYE") if home else "BYE"
        away_name = getattr(away, "team_name", "BYE") if away else "BYE"
        hs = getattr(m, "home_score", 0) or 0
        as_ = getattr(m, "away_score", 0) or 0
        hp = getattr(m, "home_projected", None)
        ap = getattr(m, "away_projected", None)
        proj = ""
        if isinstance(hp, (int, float)) and isinstance(ap, (int, float)) and (hp or ap):
            proj = f"   (proj {hp:.1f} - {ap:.1f})"
        lines.append(f"{away_name[:26]:<27} {as_:>6.1f}  @  {hs:<6.1f} {home_name}{proj}")
    return "\n".join(lines)


@mcp.tool()
def get_scoreboard() -> str:
    """Get the current week's live scores across all matchups."""
    try:
        league = get_league()
    except RuntimeError as exc:
        return f"Error: {exc}"

    wk = current_week(league)
    try:
        box = league.box_scores(wk)
    except Exception as exc:  # noqa: BLE001
        return f"Could not load the scoreboard for week {wk}: {exc}"

    if not box:
        return f"No live scores available for week {wk}."

    lines = [f"# Live scoreboard — week {wk}", ""]
    for m in box:
        home = getattr(m, "home_team", None)
        away = getattr(m, "away_team", None)
        home_name = getattr(home, "team_name", "BYE") if home else "BYE"
        away_name = getattr(away, "team_name", "BYE") if away else "BYE"
        hs = getattr(m, "home_score", 0) or 0
        as_ = getattr(m, "away_score", 0) or 0
        leader = ">" if hs >= as_ else "<"
        lines.append(
            f"{away_name[:26]:<27} {as_:>6.1f}  {leader}  {hs:<6.1f} {home_name}"
        )
    return "\n".join(lines)


@mcp.tool()
def get_head_to_head(team_name_1: str, team_name_2: str) -> str:
    """Get the season head-to-head history between two teams.

    Args:
        team_name_1: First team (name or owner, fuzzy).
        team_name_2: Second team (name or owner, fuzzy).

    Scans each team's weekly schedule for games against the other and reports
    the results and point totals.
    """
    try:
        league = get_league()
    except RuntimeError as exc:
        return f"Error: {exc}"

    t1 = find_team(league, team_name_1)
    t2 = find_team(league, team_name_2)
    if t1 is None:
        return f"No team matched '{team_name_1}'.\n\n{team_choices(league)}"
    if t2 is None:
        return f"No team matched '{team_name_2}'.\n\n{team_choices(league)}"
    if t1 is t2:
        return "Please provide two different teams."

    schedule = getattr(t1, "schedule", None) or []
    scores = getattr(t1, "scores", None) or []
    opp_scores = getattr(t2, "scores", None) or []

    lines = [f"# Head-to-head: {t1.team_name} vs {t2.team_name}", ""]
    t1_wins = t2_wins = ties = 0
    found = False
    for i, opp in enumerate(schedule):
        if opp is not t2 and getattr(opp, "team_id", None) != getattr(t2, "team_id", None):
            continue
        found = True
        wk = i + 1
        s1 = scores[i] if i < len(scores) else 0
        # opponent's score for that week comes from t2's own score list.
        s2 = opp_scores[i] if i < len(opp_scores) else 0
        if s1 > s2:
            result = t1.team_name
            t1_wins += 1
        elif s2 > s1:
            result = t2.team_name
            t2_wins += 1
        else:
            result = "Tie"
            ties += 1
        lines.append(
            f"Week {wk:>2}: {t1.team_name[:20]} {s1:.1f} - {s2:.1f} {t2.team_name[:20]}"
            f"  -> {result}"
        )

    if not found:
        lines.append("These two teams have not played each other this season.")
    else:
        summary = f"Series: {t1.team_name} {t1_wins} - {t2_wins} {t2.team_name}"
        if ties:
            summary += f" ({ties} tie{'s' if ties != 1 else ''})"
        lines += ["", summary]
    return "\n".join(lines)


@mcp.tool()
def get_free_agents(position: Optional[str] = None, limit: int = 10) -> str:
    """List the top available free agents.

    Args:
        position: Optional position filter (QB, RB, WR, TE, K, DST). Omit for all.
        limit: Maximum number of players to return (default 10).

    Sorted by projected points (falling back to average points).
    """
    try:
        league = get_league()
    except RuntimeError as exc:
        return f"Error: {exc}"

    pos = None
    if position:
        pos = POSITION_ALIASES.get(position.upper().strip(), position.upper().strip())

    try:
        agents = league.free_agents(size=max(limit, 1), position=pos)
    except Exception as exc:  # noqa: BLE001
        return f"Could not load free agents: {exc}"

    if not agents:
        return f"No free agents found{f' at {pos}' if pos else ''}."

    week = current_week(league)
    agents = sorted(
        agents,
        key=lambda p: (player_projected(p, week), player_avg(p)),
        reverse=True,
    )[:limit]

    title = f"# Top {len(agents)} free agents" + (f" — {pos}" if pos else "")
    header = f"{'Player':<25}{'Pos':<5}{'Proj':>7}{'Avg':>7}  Status"
    lines = [title, "", header, "-" * len(header)]
    for p in agents:
        inj = injury_flag(p)
        lines.append(
            f"{getattr(p, 'name', '?')[:24]:<25}{player_position(p):<5}"
            f"{player_projected(p, week):>7.1f}{player_avg(p):>7.1f}  {inj or 'OK'}"
        )
    return "\n".join(lines)


@mcp.tool()
def get_player_stats(player_name: str) -> str:
    """Look up a specific player's season stats and current status.

    Args:
        player_name: Full or partial player name.

    Returns season totals/averages, projected points, injury status, and
    available weekly scores.
    """
    try:
        league = get_league()
    except RuntimeError as exc:
        return f"Error: {exc}"

    try:
        player = league.player_info(name=player_name)
    except Exception as exc:  # noqa: BLE001
        return f"Could not look up '{player_name}': {exc}"

    if not player:
        return (
            f"No player found matching '{player_name}'. "
            "Check the spelling or try a fuller name."
        )

    week = current_week(league)
    lines = [
        f"# {getattr(player, 'name', player_name)}",
        "",
        f"- Position: {player_position(player)}",
        f"- Pro team: {getattr(player, 'proTeam', 'N/A')}",
        f"- Injury status: {injury_flag(player) or 'Active'}",
        f"- Total points: {getattr(player, 'total_points', 0):.1f}",
        f"- Avg points: {player_avg(player):.1f}",
        f"- Projected (season avg): {player_projected(player, week):.1f}",
        f"- Owned: {getattr(player, 'percent_owned', 'N/A')}%",
    ]

    stats = getattr(player, "stats", None)
    if isinstance(stats, dict) and stats:
        weekly = []
        for wk in sorted(k for k in stats if isinstance(k, int)):
            entry = stats[wk]
            if isinstance(entry, dict):
                pts = entry.get("points")
                if isinstance(pts, (int, float)):
                    weekly.append(f"  W{wk}: {pts:.1f}")
        if weekly:
            lines += ["", "## Weekly scores"] + weekly
    return "\n".join(lines)


@mcp.tool()
def get_power_rankings(week: Optional[int] = None) -> str:
    """Get power rankings for a week, if the library can compute them.

    Args:
        week: Week number. Defaults to the current week.
    """
    try:
        league = get_league()
    except RuntimeError as exc:
        return f"Error: {exc}"

    wk = week or current_week(league)
    try:
        rankings = league.power_rankings(week=wk)
    except Exception as exc:  # noqa: BLE001
        return f"Power rankings are not available for week {wk}: {exc}"

    if not rankings:
        return f"No power rankings available for week {wk}."

    lines = [f"# Power rankings — week {wk}", ""]
    for i, entry in enumerate(rankings, 1):
        # Entries are (score, team) tuples.
        try:
            score, team = entry
        except (TypeError, ValueError):
            continue
        lines.append(f"{i:>2}. {getattr(team, 'team_name', '?')[:28]:<29} {score}")
    return "\n".join(lines)


@mcp.tool()
def get_trade_activity() -> str:
    """List recent completed trades in the league."""
    try:
        league = get_league()
    except RuntimeError as exc:
        return f"Error: {exc}"

    try:
        activity = league.recent_activity(size=50)
    except Exception as exc:  # noqa: BLE001
        return f"Could not load recent activity: {exc}"

    lines = [f"# Recent trades — {league.settings.name}", ""]
    found = False
    for act in activity or []:
        actions = getattr(act, "actions", None) or []
        trade_actions = [a for a in actions if len(a) > 1 and "TRADED" in str(a[1]).upper()]
        if not trade_actions:
            continue
        found = True
        date = fmt_ts(getattr(act, "date", None))
        lines.append(f"## {date}")
        for a in trade_actions:
            team = a[0]
            player = a[2] if len(a) > 2 else None
            team_name = getattr(team, "team_name", "Unknown team")
            player_name = getattr(player, "name", str(player)) if player else "?"
            lines.append(f"  - {team_name} traded for {player_name}")
        lines.append("")

    if not found:
        return "No recent trades found in the league's activity feed."
    return "\n".join(lines).rstrip()


@mcp.tool()
def get_playoff_picture() -> str:
    """Show the projected playoff picture.

    Ranks teams by record and points for, marks the current playoff seeds, and
    gives a rough clinched / in-contention read based on games remaining.
    """
    try:
        league = get_league()
    except RuntimeError as exc:
        return f"Error: {exc}"

    s = league.settings
    playoff_spots = getattr(s, "playoff_team_count", 0) or 0
    reg_weeks = getattr(s, "reg_season_count", 0) or 0
    wk = current_week(league)
    games_left = max(reg_weeks - (wk - 1), 0)

    teams = sorted(
        league.teams,
        key=lambda t: (getattr(t, "wins", 0), getattr(t, "points_for", 0)),
        reverse=True,
    )

    lines = [
        f"# Playoff picture — {s.name}",
        f"Playoff spots: {playoff_spots} | Reg-season weeks: {reg_weeks} | "
        f"Games remaining: ~{games_left}",
        "",
    ]

    if playoff_spots and len(teams) > playoff_spots:
        cutoff_wins = getattr(teams[playoff_spots - 1], "wins", 0)
        bubble_wins = getattr(teams[playoff_spots], "wins", 0)
    else:
        cutoff_wins = bubble_wins = 0

    for i, t in enumerate(teams, 1):
        wins = getattr(t, "wins", 0)
        in_spot = playoff_spots and i <= playoff_spots
        # Rough status heuristic.
        if in_spot and (wins - bubble_wins) > games_left:
            status = "CLINCHED"
        elif not in_spot and (cutoff_wins - wins) > games_left:
            status = "ELIMINATED"
        else:
            status = "IN CONTENTION"
        marker = "*" if in_spot else " "
        rec = f"{wins}-{getattr(t, 'losses', 0)}"
        lines.append(
            f"{marker}{i:>2}. {t.team_name[:26]:<27} {rec:<8} "
            f"PF {getattr(t, 'points_for', 0):>7.1f}  [{status}]"
        )

    lines += ["", "* = currently in a playoff seed"]
    return "\n".join(lines)


@mcp.tool()
def get_team_schedule(team_name: str) -> str:
    """Show a team's remaining schedule with opponent strength.

    Args:
        team_name: Team name or owner name (fuzzy).

    Lists each remaining week's opponent along with that opponent's record and
    points for, so you can gauge schedule difficulty.
    """
    try:
        league = get_league()
    except RuntimeError as exc:
        return f"Error: {exc}"

    team = find_team(league, team_name)
    if team is None:
        return f"No team matched '{team_name}'.\n\n{team_choices(league)}"

    schedule = getattr(team, "schedule", None) or []
    wk = current_week(league)

    lines = [f"# {team.team_name} — remaining schedule (from week {wk})", ""]
    upcoming = False
    for i, opp in enumerate(schedule):
        game_week = i + 1
        if game_week < wk:
            continue
        upcoming = True
        opp_rec = f"{getattr(opp, 'wins', 0)}-{getattr(opp, 'losses', 0)}"
        lines.append(
            f"Week {game_week:>2}: vs {getattr(opp, 'team_name', 'BYE')[:26]:<27} "
            f"({opp_rec}, PF {getattr(opp, 'points_for', 0):.1f})"
        )
    if not upcoming:
        lines.append("No remaining regular-season games on the schedule.")
    return "\n".join(lines)


@mcp.tool()
def get_player_schedule(player_name: str) -> str:
    """Show a player's NFL bye week and remaining NFL matchups.

    Args:
        player_name: Full or partial player name.

    Useful for spotting bye-week conflicts and upcoming tough/easy stretches.
    """
    try:
        league = get_league()
    except RuntimeError as exc:
        return f"Error: {exc}"

    try:
        player = league.player_info(name=player_name)
    except Exception as exc:  # noqa: BLE001
        return f"Could not look up '{player_name}': {exc}"

    if not player:
        return f"No player found matching '{player_name}'."

    schedule = getattr(player, "schedule", None)
    lines = [
        f"# {getattr(player, 'name', player_name)} — NFL schedule",
        f"Pro team: {getattr(player, 'proTeam', 'N/A')} | "
        f"Position: {player_position(player)}",
        "",
    ]

    if not isinstance(schedule, dict) or not schedule:
        lines.append(
            "NFL schedule data is not available for this player from the ESPN API."
        )
        return "\n".join(lines)

    wk = current_week(league)
    weeks_present = set()
    for key, game in sorted(schedule.items(), key=lambda kv: str(kv[0])):
        try:
            week_num = int(key)
        except (TypeError, ValueError):
            week_num = None
        if week_num is not None:
            weeks_present.add(week_num)
        opp = ""
        if isinstance(game, dict):
            opp = game.get("team") or game.get("opponent") or ""
        tag = ""
        if week_num is not None and week_num < wk:
            tag = " (played)"
        lines.append(f"Week {key}: {opp or 'TBD'}{tag}")

    # Infer bye week: a missing week within the regular-season range.
    reg_weeks = getattr(league.settings, "reg_season_count", 0) or 0
    if reg_weeks and weeks_present:
        byes = [w for w in range(1, reg_weeks + 1) if w not in weeks_present]
        if byes:
            lines += ["", f"Likely bye week(s): {', '.join(map(str, byes))}"]
    return "\n".join(lines)


@mcp.tool()
def get_league_settings() -> str:
    """Get the league's scoring format and roster/structure settings.

    Essential context for evaluating player value: scoring type (PPR / half /
    standard), roster slot requirements, playoff structure, trade deadline, and
    waiver/FAAB type.
    """
    try:
        league = get_league()
    except RuntimeError as exc:
        return f"Error: {exc}"

    s = league.settings
    lines = [
        f"# League settings — {s.name} ({YEAR})",
        "",
        "## Scoring",
        f"- Format: {scoring_kind(league)}",
    ]

    fmt = getattr(s, "scoring_format", None)
    if fmt:
        notable = []
        for item in fmt:
            if not isinstance(item, dict):
                continue
            pts = item.get("points", 0) or 0
            if pts:
                abbr = item.get("abbr") or item.get("abbrev") or "?"
                notable.append(f"  {abbr}: {pts}")
        if notable:
            lines += ["- Non-zero scoring rules:"] + notable[:40]

    lines += ["", "## Roster slots"]
    slots = roster_slot_counts(league)
    if slots:
        for slot, count in slots.items():
            lines.append(f"  {slot}: {count}")
    else:
        lines.append("  (Roster slot detail not available)")

    lines += [
        "",
        "## Structure",
        f"- Teams: {getattr(s, 'team_count', len(league.teams))}",
        f"- Regular-season weeks: {getattr(s, 'reg_season_count', 'N/A')}",
        f"- Playoff teams: {getattr(s, 'playoff_team_count', 'N/A')}",
        f"- Trade deadline: {fmt_ts(getattr(s, 'trade_deadline', None))}",
        f"- Veto votes required: {getattr(s, 'veto_votes_required', 'N/A')}",
        f"- Keepers: {getattr(s, 'keeper_count', 'N/A')}",
        f"- Waiver type: {'FAAB' if getattr(s, 'faab', False) else 'Standard/rolling'}",
    ]
    return "\n".join(lines)


@mcp.tool()
def compare_teams(team_name_1: str, team_name_2: str) -> str:
    """Compare two teams' starting lineups side by side, position by position.

    Args:
        team_name_1: First team (name or owner, fuzzy).
        team_name_2: Second team (name or owner, fuzzy).

    Groups starters by position and shows projected points for each slot, plus a
    projected-points total for each team.
    """
    try:
        league = get_league()
    except RuntimeError as exc:
        return f"Error: {exc}"

    t1 = find_team(league, team_name_1)
    t2 = find_team(league, team_name_2)
    if t1 is None:
        return f"No team matched '{team_name_1}'.\n\n{team_choices(league)}"
    if t2 is None:
        return f"No team matched '{team_name_2}'.\n\n{team_choices(league)}"

    week = current_week(league)

    def starters_by_pos(team: Any) -> dict[str, list[Any]]:
        out: dict[str, list[Any]] = {}
        for p in team.roster:
            if is_starter(p):
                out.setdefault(player_position(p), []).append(p)
        return out

    b1 = starters_by_pos(t1)
    b2 = starters_by_pos(t2)
    positions = sorted(set(b1) | set(b2))

    def cell(players: list[Any]) -> str:
        if not players:
            return "-"
        return "; ".join(
            f"{getattr(p, 'name', '?')[:16]} {player_projected(p, week):.1f}"
            for p in players
        )

    lines = [
        f"# {t1.team_name} vs {t2.team_name} (week {week} projections)",
        "",
    ]
    total1 = total2 = 0.0
    for pos in positions:
        p1 = b1.get(pos, [])
        p2 = b2.get(pos, [])
        s1 = sum(player_projected(p, week) for p in p1)
        s2 = sum(player_projected(p, week) for p in p2)
        total1 += s1
        total2 += s2
        edge = "<-" if s1 > s2 else ("->" if s2 > s1 else "==")
        lines.append(f"## {pos}  ({s1:.1f} {edge} {s2:.1f})")
        lines.append(f"  {t1.team_name[:20]}: {cell(p1)}")
        lines.append(f"  {t2.team_name[:20]}: {cell(p2)}")
        lines.append("")

    lines.append(
        f"TOTAL projected: {t1.team_name} {total1:.1f}  vs  {t2.team_name} {total2:.1f}"
    )
    return "\n".join(lines)


@mcp.tool()
def get_team_analysis(team_name: str) -> str:
    """Aggregated snapshot of a team for trade / improvement advice.

    Args:
        team_name: Team name or owner name (fuzzy).

    Returns:
    - Full roster with average points and a last-3-weeks trend (up/down/flat)
    - Bye-week conflicts (weeks where multiple starters are out)
    - Position-group strength vs the league average at that position
    - Bench depth by position
    """
    try:
        league = get_league()
    except RuntimeError as exc:
        return f"Error: {exc}"

    team = find_team(league, team_name)
    if team is None:
        return f"No team matched '{team_name}'.\n\n{team_choices(league)}"

    week = current_week(league)
    lg_avg = league_position_averages(league, week)

    def trend(player: Any) -> str:
        stats = getattr(player, "stats", None)
        if not isinstance(stats, dict):
            return "flat"
        pts = []
        for wk in sorted(k for k in stats if isinstance(k, int)):
            entry = stats[wk]
            if isinstance(entry, dict) and isinstance(entry.get("points"), (int, float)):
                pts.append(entry["points"])
        recent = pts[-3:]
        if len(recent) < 2:
            return "flat"
        first_half = sum(recent[: len(recent) // 2]) / max(len(recent) // 2, 1)
        second_half = sum(recent[len(recent) // 2 :]) / max(len(recent) - len(recent) // 2, 1)
        if second_half > first_half * 1.1:
            return "up"
        if second_half < first_half * 0.9:
            return "down"
        return "flat"

    starters = [p for p in team.roster if is_starter(p)]
    bench = [p for p in team.roster if not is_starter(p)]

    lines = [
        f"# Team analysis — {team.team_name} (owner: {owner_name(team)})",
        f"Week {week} | Record {getattr(team, 'wins', 0)}-{getattr(team, 'losses', 0)}",
        "",
        "## Roster (avg pts, trend)",
    ]
    for p in team.roster:
        star = "S" if is_starter(p) else "b"
        lines.append(
            f"  [{star}] {player_slot(p):<9}{player_position(p):<5}"
            f"{getattr(p, 'name', '?')[:22]:<23} avg {player_avg(p):>6.1f}  {trend(p)}"
        )

    # Position-group strength vs league average.
    lines += ["", "## Position group strength (starters, projected vs league avg)"]
    starters_by_pos: dict[str, list[Any]] = {}
    for p in starters:
        starters_by_pos.setdefault(player_position(p), []).append(p)
    for pos in sorted(starters_by_pos):
        players = starters_by_pos[pos]
        team_proj = sum(player_projected(p, week) for p in players)
        avg_per = team_proj / len(players)
        league_avg = lg_avg.get(pos, 0.0)
        if league_avg:
            pct = (avg_per - league_avg) / league_avg * 100
            verdict = "STRONG" if pct > 8 else ("WEAK" if pct < -8 else "average")
            lines.append(
                f"  {pos}: {avg_per:.1f}/starter vs league {league_avg:.1f} "
                f"({pct:+.0f}%) -> {verdict}"
            )
        else:
            lines.append(f"  {pos}: {avg_per:.1f}/starter (no league baseline)")

    # Bench depth.
    lines += ["", "## Bench depth by position"]
    bench_by_pos: dict[str, int] = {}
    for p in bench:
        if player_slot(p) == "IR":
            continue
        bench_by_pos[player_position(p)] = bench_by_pos.get(player_position(p), 0) + 1
    if bench_by_pos:
        for pos in sorted(bench_by_pos):
            lines.append(f"  {pos}: {bench_by_pos[pos]} on bench")
    else:
        lines.append("  (no bench players)")

    # Bye-week conflicts among starters.
    lines += ["", "## Bye-week conflicts (starters)"]
    bye_map: dict[int, list[str]] = {}
    for p in starters:
        sched = getattr(p, "schedule", None)
        reg_weeks = getattr(league.settings, "reg_season_count", 0) or 0
        if isinstance(sched, dict) and sched and reg_weeks:
            present = set()
            for k in sched:
                try:
                    present.add(int(k))
                except (TypeError, ValueError):
                    pass
            for w in range(week, reg_weeks + 1):
                if w not in present:
                    bye_map.setdefault(w, []).append(getattr(p, "name", "?"))
    conflicts = {w: names for w, names in bye_map.items() if len(names) > 1}
    if conflicts:
        for w in sorted(conflicts):
            lines.append(f"  Week {w}: {', '.join(conflicts[w])}")
    else:
        lines.append("  No multi-starter bye conflicts detected (or NFL schedule unavailable).")

    return "\n".join(lines)


@mcp.tool()
def get_trade_candidates(team_name: str) -> str:
    """Identify potential trade targets for a team.

    Args:
        team_name: Team name or owner name (fuzzy).

    Finds the team's weakest starting position group, then scans every other
    team for players at that position who sit on the bench or represent surplus
    depth. Returns candidates grouped by position need, with the owning team so
    the user knows whom to approach.
    """
    try:
        league = get_league()
    except RuntimeError as exc:
        return f"Error: {exc}"

    team = find_team(league, team_name)
    if team is None:
        return f"No team matched '{team_name}'.\n\n{team_choices(league)}"

    week = current_week(league)
    lg_avg = league_position_averages(league, week)

    # Rank the team's starting position groups by deficit vs league average.
    starters_by_pos: dict[str, list[Any]] = {}
    for p in team.roster:
        if is_starter(p):
            starters_by_pos.setdefault(player_position(p), []).append(p)

    deficits: list[tuple[str, float]] = []
    for pos, players in starters_by_pos.items():
        avg_per = sum(player_projected(p, week) for p in players) / len(players)
        league_avg = lg_avg.get(pos, 0.0)
        if league_avg:
            deficits.append((pos, avg_per - league_avg))
    deficits.sort(key=lambda x: x[1])  # most negative (weakest) first

    if not deficits:
        return (
            f"Could not compute position strength for {team.team_name} "
            "(projection data unavailable)."
        )

    need_positions = [pos for pos, diff in deficits if diff < 0][:2]
    if not need_positions:
        need_positions = [deficits[0][0]]

    lines = [
        f"# Trade candidates for {team.team_name}",
        f"Weakest position group(s): {', '.join(need_positions)}",
        "",
    ]

    for pos in need_positions:
        lines.append(f"## Targets at {pos}")
        candidates: list[tuple[float, str, Any]] = []
        for other in league.teams:
            if other is team:
                continue
            # Count starters that team already has at this position for depth read.
            at_pos = [p for p in other.roster if player_position(p) == pos]
            starters_at_pos = [p for p in at_pos if is_starter(p)]
            for p in at_pos:
                surplus = (not is_starter(p)) or len(starters_at_pos) > 1
                if not surplus:
                    continue
                proj = player_projected(p, week)
                tag = "bench" if not is_starter(p) else "surplus starter"
                candidates.append(
                    (
                        proj,
                        f"  {getattr(p, 'name', '?')[:22]:<23} proj {proj:>6.1f}  "
                        f"({tag}) — held by {other.team_name}",
                        p,
                    )
                )
        candidates.sort(key=lambda x: x[0], reverse=True)
        if candidates:
            lines += [c[1] for c in candidates[:8]]
        else:
            lines.append("  No obvious surplus players found at this position.")
        lines.append("")

    lines.append(
        "Tip: a good offer also fills the OTHER team's need — check compare_teams "
        "and get_team_analysis on the target team before proposing."
    )
    return "\n".join(lines).rstrip()


def eligible_slots(player: Any) -> set[str]:
    """Starting slots a player is eligible to fill (excludes bench/IR)."""
    slots = getattr(player, "eligibleSlots", None)
    if slots:
        return {s for s in slots if s not in BENCH_SLOTS}
    pos = player_position(player)
    out = {pos}
    if pos in {"RB", "WR", "TE"}:
        out |= {"FLEX", "RB/WR/TE"}
    return out


@mcp.tool()
def get_start_sit(team_name: str, week: Optional[int] = None) -> str:
    """Recommend start/sit moves for a team's lineup for a given week.

    Args:
        team_name: Team name or owner name (fuzzy, case-insensitive substring).
        week: Week number. Defaults to the upcoming (actionable) week — if the
            current week's games are already final, this is next week.

    Flags starters who are injured or on bye, then compares bench players to the
    current starters at slots they are eligible to fill and suggests any swap
    where a bench player out-projects a starter. Purely projection-driven — treat
    it as a starting point, not gospel (matchups and game scripts still matter).
    """
    try:
        league = get_league()
    except RuntimeError as exc:
        return f"Error: {exc}"

    team = find_team(league, team_name)
    if team is None:
        return f"No team matched '{team_name}'.\n\n{team_choices(league)}"

    wk = week or upcoming_week(league)
    posted = wk <= latest_projection_week(league)
    starters = [p for p in team.roster if is_starter(p)]
    bench = [
        p for p in team.roster if not is_starter(p) and player_slot(p) != "IR"
    ]

    lines = [
        f"# Start/Sit — {team.team_name} (week {wk})",
        f"Owner: {owner_name(team)}",
    ]
    if not posted:
        lines.append(
            f"NOTE: ESPN hasn't posted Week {wk} projections yet (usually ~Tue). "
            "These are season-average placeholders — revisit once ESPN updates."
        )
    lines += ["", "## Current starters"]

    # Flag starters with problems (injury or a zero projection == likely bye/out).
    warnings: list[str] = []
    for p in starters:
        proj = player_projected(p, wk)
        inj = injury_flag(p)
        flag = ""
        if inj in ("OUT", "INJURY_RESERVE", "SUSPENSION"):
            flag = f"  <-- {inj}, likely won't play"
            warnings.append(getattr(p, "name", "?"))
        elif inj:
            flag = f"  <-- {inj}, check status"
        elif proj <= 0:
            flag = "  <-- 0 projected (bye week or inactive?)"
            warnings.append(getattr(p, "name", "?"))
        lines.append(
            f"  {player_slot(p):<9}{player_position(p):<5}"
            f"{getattr(p, 'name', '?')[:22]:<23}proj {proj:>6.1f}{flag}"
        )

    # Greedy swap search: highest projection gain first, each starter slot and
    # each bench player used at most once.
    potential: list[tuple[float, Any, Any]] = []
    for b in bench:
        b_slots = eligible_slots(b)
        b_proj = player_projected(b, wk)
        for s in starters:
            if player_slot(s) in b_slots and b_proj > player_projected(s, wk):
                potential.append((b_proj - player_projected(s, wk), b, s))
    potential.sort(key=lambda x: x[0], reverse=True)

    used_bench: set[int] = set()
    used_starter: set[int] = set()
    recs: list[tuple[float, Any, Any]] = []
    for diff, b, s in potential:
        if id(b) in used_bench or id(s) in used_starter:
            continue
        used_bench.add(id(b))
        used_starter.add(id(s))
        recs.append((diff, b, s))

    lines += ["", "## Recommended moves"]
    if recs:
        for diff, b, s in recs:
            lines.append(
                f"  START {getattr(b, 'name', '?')} "
                f"({player_position(b)}, proj {player_projected(b, wk):.1f})"
            )
            lines.append(
                f"    over {getattr(s, 'name', '?')} "
                f"({player_slot(s)}, proj {player_projected(s, wk):.1f})  "
                f"[+{diff:.1f} pts]"
            )
    else:
        lines.append("  Lineup already looks optimal by projection. No swaps suggested.")

    if warnings:
        lines += [
            "",
            "## Heads up",
            "  These starters are OUT/on bye — make sure you have replacements:",
            "  " + ", ".join(warnings),
        ]

    lines += [
        "",
        f"Scoring format: {scoring_kind(league)} — weigh this in close calls "
        "(receptions matter more in PPR).",
    ]
    return "\n".join(lines)


def main() -> None:
    """Entry point: run the MCP server over stdio (for Claude Desktop)."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
