"""League-wide tools: overview, standings, teams, matchups, settings, etc."""

from __future__ import annotations

from typing import Optional

from ..app import mcp
from ..client import fetched_at, freshness_line, get_league, reset_league
from ..config import YEAR
from ..formatting import (
    current_week,
    fmt_ts,
    owner_name,
    roster_slot_counts,
    scoring_kind,
    upcoming_week,
)


@mcp.tool()
def refresh_league() -> str:
    """Force an immediate re-fetch of all league data from ESPN.

    Other tools already auto-refresh once their cached copy passes the staleness
    TTL (ESPN_CACHE_TTL, default 3 min), so you usually don't need this. Reach
    for it when you want the very latest right now — e.g. a waiver just
    processed, someone made a trade, or scores are moving during games.
    """
    reset_league()
    try:
        league = get_league()
    except RuntimeError as exc:
        return f"Error: {exc}"
    stamp = fetched_at()
    when = f" at {stamp.strftime('%H:%M')}" if stamp else ""
    return (
        f"League data refreshed{when}: {league.settings.name} "
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
    fresh = freshness_line()
    if fresh:
        lines += ["", fresh]
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
    """List recent completed trades in the league.

    For a broader feed that also includes adds, drops, and waiver claims, use
    get_recent_transactions.
    """
    try:
        league = get_league()
    except RuntimeError as exc:
        return f"Error: {exc}"

    try:
        activity = league.recent_activity(size=50, msg_type="TRADED")
    except Exception as exc:  # noqa: BLE001
        return f"Could not load recent activity: {exc}"

    # espn_api emits each trade as TRADE_SENT (giver) and TRADE_RECEIVED
    # (getter) rows; older versions used a single "TRADED" label. Handle both.
    trade_labels = {"TRADE_SENT", "TRADE_RECEIVED", "TRADED"}

    lines = [f"# Recent trades — {league.settings.name}", ""]
    found = False
    for act in activity or []:
        actions = getattr(act, "actions", None) or []
        trade_actions = [
            a for a in actions if len(a) > 1 and str(a[1]).upper() in trade_labels
        ]
        if not trade_actions:
            continue
        found = True
        lines.append(f"## {fmt_ts(getattr(act, 'date', None))}")
        for a in trade_actions:
            team = a[0]
            action = str(a[1]).upper()
            player = a[2] if len(a) > 2 else None
            team_name = getattr(team, "team_name", None) or "Unknown team"
            player_name = getattr(player, "name", None) or (
                str(player) if player is not None else "?"
            )
            if action == "TRADE_RECEIVED":
                lines.append(f"  - {team_name} received {player_name}")
            elif action == "TRADE_SENT":
                lines.append(f"  - {team_name} traded away {player_name}")
            else:
                lines.append(f"  - {team_name} traded for {player_name}")
        lines.append("")

    if not found:
        return "No recent trades found in the league's activity feed."
    fresh = freshness_line()
    if fresh:
        lines.append(fresh)
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
