"""Decision-support tools: trade candidates and start/sit recommendations."""

from __future__ import annotations

from typing import Any, Optional

from ..app import mcp
from ..client import freshness_line, get_league
from ..formatting import (
    eligible_slots,
    find_team,
    injury_flag,
    is_starter,
    latest_projection_week,
    league_position_averages,
    owner_name,
    player_position,
    player_projected,
    player_slot,
    scoring_kind,
    team_choices,
    upcoming_week,
)


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

    week = upcoming_week(league)
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
    fresh = freshness_line()
    if fresh:
        lines += ["", fresh]
    return "\n".join(lines)
