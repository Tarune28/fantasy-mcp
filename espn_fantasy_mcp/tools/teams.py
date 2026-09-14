"""Team-centric tools: roster, head-to-head, schedule, comparison, analysis."""

from __future__ import annotations

from typing import Any, Optional

from ..app import mcp
from ..client import get_league
from ..formatting import (
    current_week,
    find_team,
    injury_flag,
    is_starter,
    latest_projection_week,
    league_position_averages,
    owner_name,
    player_avg,
    player_position,
    player_projected,
    player_slot,
    team_choices,
    upcoming_week,
)


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

    week = upcoming_week(league)

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

    week = upcoming_week(league)
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
