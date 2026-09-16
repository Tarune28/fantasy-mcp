"""Deep, multi-factor player review — the antidote to judging by one projection.

``analyze_player`` assembles every signal we have about a player (ESPN weekly
history + stat breakdown, plus free external context: Vegas implied totals,
opponent defense strength, Sleeper role/injury/market) into a single briefing
and hands it to Claude to synthesize. The tool deliberately does NOT emit a
verdict or a blended score — it lays out the evidence and the reasons a raw
projection might mislead, so the judgment stays with the model.
"""

from __future__ import annotations

from typing import Any, Optional

from ..app import mcp
from ..client import freshness_line, get_league
from .. import signals, sources
from ..formatting import (
    injury_flag,
    player_position,
    player_projected,
    upcoming_week,
)


def _norm_team(abbr: Any) -> str:
    return sources._norm_team(abbr)


def _schedule_opponent(player: Any, week: int) -> str:
    """Opponent abbrev for a week from the player's own ESPN schedule, or ''."""
    sched = getattr(player, "schedule", None)
    if not isinstance(sched, dict):
        return ""
    for key in (week, str(week)):
        game = sched.get(key)
        if isinstance(game, dict):
            return _norm_team(game.get("team") or game.get("opponent"))
    return ""


def _matchup_lines(player: Any, week: int) -> list[str]:
    """Vegas game environment + opponent-defense lines for the player's team."""
    pro = _norm_team(getattr(player, "proTeam", None))
    if not pro:
        return ["- Matchup: pro team unknown"]
    env = sources.game_environment(week).get(pro, {})
    defense = sources.defense_rankings()
    opp = env.get("opponent") or _schedule_opponent(player, week)

    out: list[str] = []
    if not opp:
        return ["- Matchup: no game found for this week (bye week?)"]

    where = "vs" if env.get("home") else "@"
    line = f"- Matchup: {where} {opp}"
    if env.get("has_odds"):
        it = env.get("implied_total")
        ou = env.get("over_under")
        if it is not None:
            env_read = ("high-scoring spot" if it >= 25 else
                        "low-scoring spot" if it <= 18 else "average scoring spot")
            line += f" | Vegas implied team total {it} pts ({env_read}); game O/U {ou}"
    else:
        line += " | Vegas odds not posted yet"
    out.append(line)

    opp_def = defense.get(opp)
    if opp_def:
        rank = opp_def["rank"]
        n = len(defense)
        tier = ("tough D" if rank <= 8 else "soft D" if rank >= n - 7 else "middling D")
        out.append(
            f"- Opponent defense: {opp} allows {opp_def['avg_points_against']} pts/game "
            f"— rank {rank}/{n} ({tier}); note this is all-position, not vs-{player_position(player)}"
        )
    return out


def _form_lines(player: Any) -> list[str]:
    form = signals.recent_form(player)
    if not form:
        return ["- Recent form: not enough game history yet"]
    return [
        f"- Recent form: last {form['recent_weeks']} wk avg {form['recent_avg']} "
        f"vs season {form['season_avg']} ({form['delta_pct']:+.0f}%, {form['trend']}); "
        f"last games: {', '.join(str(s) for s in form['last_scores'])}"
    ]


def _volatility_lines(player: Any) -> list[str]:
    vol = signals.volatility(player)
    if not vol:
        return ["- Consistency: not enough games to judge floor/ceiling"]
    return [
        f"- Consistency: floor ~{vol['floor']} / ceiling ~{vol['ceiling']} "
        f"(range {vol['min']}-{vol['max']}, stdev {vol['stdev']}); "
        f"boom {vol['boom_rate']:.0f}% of games (>={vol['boom_threshold']}), "
        f"bust {vol['bust_rate']:.0f}% (<= {vol['bust_threshold']})"
    ]


def _usage_lines(player: Any, week: int) -> list[str]:
    use = signals.usage(player, week)
    if not use:
        return []
    labels = {
        "targets": "targets", "receptions": "rec", "rec_yards": "rec yds",
        "carries": "carries", "rush_yards": "rush yds", "pass_attempts": "pass att",
        "pass_yards": "pass yds", "red_zone_targets": "RZ targets",
    }
    parts = [f"{labels.get(k, k)} {v:.0f}" for k, v in use.items()]
    return [f"- Opportunity (season): {', '.join(parts)}"]


def _role_market_lines(player: Any) -> list[str]:
    out: list[str] = []
    info = sources.sleeper_info(getattr(player, "playerId", None))
    if info:
        role_bits = []
        order = info.get("depth_chart_order")
        dpos = info.get("depth_chart_position")
        if order:
            role_bits.append(f"depth-chart {dpos or ''}#{order}".strip())
        inj = info.get("injury_status")
        if inj:
            part = info.get("injury_body_part")
            pr = info.get("practice_participation")
            role_bits.append(f"injury: {inj}{f' ({part})' if part else ''}"
                             f"{f', practice: {pr}' if pr else ''}")
        if role_bits:
            out.append(f"- Role / health (Sleeper): {'; '.join(role_bits)}")
        add, drop = info.get("trending_add"), info.get("trending_drop")
        if add or drop:
            m = []
            if add:
                m.append(f"added by {add:,} teams/24h")
            if drop:
                m.append(f"dropped by {drop:,} teams/24h")
            out.append(f"- Market (Sleeper trending): {'; '.join(m)}")

    mkt = signals.market(player)
    bits = []
    if "owned_pct" in mkt:
        bits.append(f"{mkt['owned_pct']:.0f}% rostered")
    if "started_pct" in mkt:
        bits.append(f"{mkt['started_pct']:.0f}% started")
    if "pos_rank" in mkt:
        bits.append(f"ESPN {player_position(player)}{mkt['pos_rank']}")
    if bits:
        out.append(f"- ESPN market: {', '.join(bits)}")
    return out


def player_briefing(player: Any, week: int) -> list[str]:
    """The full multi-factor evidence block for one player (list of md lines)."""
    proj = player_projected(player, week)
    inj = injury_flag(player)
    lines = [
        f"## {getattr(player, 'name', '?')} "
        f"({player_position(player)}, {getattr(player, 'proTeam', 'N/A')})",
        f"- ESPN projection (wk {week}): {proj:.1f} pts"
        + (f"  |  status: {inj}" if inj else ""),
    ]
    sanity = signals.projection_sanity(player, proj)
    if sanity.get("note"):
        lines.append(f"  - ⚠ {sanity['note']} (proj {sanity['week_projection']} "
                     f"vs recent {sanity['recent_avg']})")
    lines += _form_lines(player)
    lines += _volatility_lines(player)
    lines += _usage_lines(player, week)
    lines += _matchup_lines(player, week)
    lines += _role_market_lines(player)
    return lines


@mcp.tool()
def analyze_player(player_name: str, week: Optional[int] = None) -> str:
    """Deep, multi-factor review of one player — not just their projection.

    Args:
        player_name: Full or partial player name (rostered or free agent).
        week: Week to analyze for. Defaults to the upcoming actionable week.

    Assembles recent form, floor/ceiling volatility, opportunity (targets /
    carries), the Vegas game environment (implied team total), opponent defense
    strength, depth-chart role, injury/practice status, and add/drop market
    trends into one briefing — and flags when ESPN's projection disagrees with
    recent reality. Use this for any close start/sit call, trade piece, or
    "is this player actually good?" question instead of trusting the projection.
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

    wk = week or upcoming_week(league)
    lines = [f"# Player review — week {wk}", ""]
    lines += player_briefing(player, wk)
    lines += [
        "",
        "## How to read this",
        "No single number here is the answer. Weigh the factors together: a good "
        "projection with a low implied total or a tough defense is a fade; a "
        "modest projection with rising usage and a high team total is a buy. Match "
        "floor vs ceiling to the situation (safety in a close week, upside when "
        "chasing points), and factor the scoring format and the player's role.",
    ]
    off = sources.external_status()
    if off:
        lines += ["", f"_{off} Matchup/role/market factors above may be blank._"]
    fresh = freshness_line()
    if fresh:
        lines += ["", fresh]
    return "\n".join(lines)


def context_tag(player: Any, week: int) -> str:
    """Compact one-line matchup+form flag for embedding in other tools.

    Returns '' when no external context is available, so callers can append it
    conditionally without cluttering ESPN-only output.
    """
    pro = _norm_team(getattr(player, "proTeam", None))
    env = sources.game_environment(week).get(pro, {}) if pro else {}
    defense = sources.defense_rankings()
    opp = env.get("opponent") or _schedule_opponent(player, week)
    bits: list[str] = []
    if opp:
        where = "vs" if env.get("home") else "@"
        seg = f"{where}{opp}"
        opp_def = defense.get(opp)
        if opp_def:
            n = len(defense) or 32
            seg += f" (D {opp_def['rank']}/{n})"
        if env.get("has_odds") and env.get("implied_total") is not None:
            seg += f", ITT {env['implied_total']}"
        bits.append(seg)
    form = signals.recent_form(player)
    if form and form["trend"] != "steady":
        bits.append(form["trend"])
    info = sources.sleeper_info(getattr(player, "playerId", None))
    if info.get("injury_status"):
        bits.append(info["injury_status"])
    return "  {" + ", ".join(bits) + "}" if bits else ""
