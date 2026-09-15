"""Player-centric tools: free agents, player stats, player NFL schedule."""

from __future__ import annotations

from typing import Optional

from ..app import mcp
from ..client import freshness_line, get_league
from ..config import POSITION_ALIASES
from ..formatting import (
    current_week,
    injury_flag,
    player_avg,
    player_position,
    player_projected,
)


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
    fresh = freshness_line()
    if fresh:
        lines += ["", fresh]
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
