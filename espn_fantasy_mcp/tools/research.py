"""Composite "just answer the question" tools.

The user should be able to say "should I trade X for Y?" or "what's my team
look like?" and have everything relevant pulled automatically. These tools
bundle the calls a sharp manager would make — the user's own roster and needs,
both sides of a trade, scoring format, lineup impact, bye weeks, schedules,
and the full situational dossier on every player involved — into one response
so Claude can reason instead of orchestrate.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any, Optional

from ..app import mcp
from ..client import freshness_line, get_league
from .. import sources
from ..formatting import (
    current_week,
    find_player,
    find_team,
    injury_flag,
    is_starter,
    latest_projection_week,
    my_team,
    owner_name,
    player_avg,
    player_position,
    player_projected,
    player_slot,
    position_strength,
    resolve_team,
    roster_capacity,
    roster_slot_counts,
    scoring_kind,
    season_year,
    team_choices,
    upcoming_week,
)
from .analysis import ANALYST_QUESTIONS, _fmt_iso, context_tag, full_stats_player, player_briefing

_SPLIT = re.compile(r"\s*(?:,|;|\+|\band\b|&)\s*", re.IGNORECASE)


def _split_names(text: str) -> list[str]:
    return [t.strip() for t in _SPLIT.split(text or "") if t and t.strip()]


def _name(p: Any) -> str:
    return getattr(p, "name", "?")


def _optimal_total(league: Any, players: list[Any], week: int) -> Optional[float]:
    """Best starting-lineup projection for a set of players (None if unavailable)."""
    try:
        from ..autopilot import _starting_slots, optimal_total
        slots = _starting_slots(league)
        if not slots:
            return None
        return optimal_total([p for p in players if player_slot(p) != "IR"], slots, week)
    except Exception:  # noqa: BLE001 - optimizer is a bonus, never a blocker
        return None


def _bye_weeks(players: list[Any], from_week: int, to_week: int) -> dict[int, list[str]]:
    """{week: [names]} of players with no NFL game that week (bye)."""
    out: dict[int, list[str]] = {}
    for p in players:
        sched = getattr(p, "schedule", None)
        if not isinstance(sched, dict) or not sched:
            continue
        present = set()
        for k in sched:
            try:
                present.add(int(k))
            except (TypeError, ValueError):
                pass
        for w in range(from_week, to_week + 1):
            if w not in present:
                out.setdefault(w, []).append(_name(p))
    return out


def _strength_lines(league: Any, team: Any, week: int) -> list[str]:
    ps = position_strength(league, team, week)
    out = []
    for pos in sorted(ps):
        v = ps[pos]
        out.append(f"  {pos}: {v['per_starter']}/starter vs league {v['league_avg']} "
                   f"({v['pct']:+d}%) -> {v['verdict']}")
    return out


def _roster_rows(team: Any, week: int, with_context: bool = True) -> list[str]:
    rows = []
    for p in sorted(team.roster, key=lambda p: (not is_starter(p), player_slot(p))):
        inj = injury_flag(p)
        flag = f" [{inj}]" if inj else ""
        tag = context_tag(p, week) if with_context else ""
        rows.append(
            f"  {'S' if is_starter(p) else 'b'} {player_slot(p):<8}{player_position(p):<5}"
            f"{_name(p)[:22]:<23}proj {player_projected(p, week):>5.1f}  avg {player_avg(p):>5.1f}{flag}{tag}"
        )
    return rows


# --------------------------------------------------------------------------- #
# get_fantasy_context
# --------------------------------------------------------------------------- #
@mcp.tool()
def get_fantasy_context() -> str:
    """START HERE for any question about the user's team — one call, all the context.

    Returns today's date and NFL week, the user's team (record, standing,
    points), league scoring format and lineup slots, this week's matchup and
    projected totals, the full roster with injury/bye/matchup flags, position
    strength vs the league, upcoming bye problems, roster openings, pending
    waiver claims, and the best available free agents at weak positions
    (with market trend). Cheap and cached — call it before answering any
    "should I…", "my team", trade, waiver, or start/sit question so advice
    reflects the user's actual needs without them having to spell them out.
    """
    try:
        league = get_league()
    except RuntimeError as exc:
        return f"Error: {exc}"

    team = my_team(league)
    if team is None:
        return ("I don't know which team is yours. Tell me your team or owner name "
                f"(or set ESPN_TEAM_ID / ESPN_TEAM_NAME).\n\n{team_choices(league)}")

    cur = current_week(league)
    wk = upcoming_week(league)
    today = _dt.date.today()
    reg = getattr(league.settings, "reg_season_count", 0) or 14
    final = getattr(league, "finalScoringPeriod", None) or 17
    posted = wk <= latest_projection_week(league)

    standings = sorted(league.teams, key=lambda t: (-getattr(t, "wins", 0), -getattr(t, "points_for", 0)))
    rank = next((i + 1 for i, t in enumerate(standings) if t is team), None)

    lines = [
        f"# Fantasy context — {team.team_name} (owner: {owner_name(team)})",
        f"Today: {today:%A %b %-d, %Y}. NFL week {cur}"
        + (f" is complete; decisions are for week {wk}." if wk != cur else " — current/upcoming week.")
        + ("" if posted else f" ESPN has NOT posted week {wk} projections yet; projections below are season-average placeholders."),
        f"Record {getattr(team, 'wins', 0)}-{getattr(team, 'losses', 0)}"
        + (f", #{rank} of {len(league.teams)}" if rank else "")
        + f", PF {getattr(team, 'points_for', 0):.1f} / PA {getattr(team, 'points_against', 0):.1f}."
        f" Regular season through week {reg}; fantasy playoffs weeks {reg + 1}-{final}.",
        f"Scoring: {scoring_kind(league)}. Lineup slots: "
        + ", ".join(f"{k}x{v}" for k, v in roster_slot_counts(league).items() if k not in ("BE", "Bench")),
    ]

    # This week's matchup.
    sched = getattr(team, "schedule", None) or []
    opp = sched[wk - 1] if 0 < wk <= len(sched) else None
    if opp is not None and hasattr(opp, "roster"):
        mine = sum(player_projected(p, wk) for p in team.roster if is_starter(p))
        theirs = sum(player_projected(p, wk) for p in opp.roster if is_starter(p))
        lines.append(
            f"Week {wk} matchup: vs {opp.team_name} ({getattr(opp, 'wins', 0)}-{getattr(opp, 'losses', 0)}) — "
            f"projected {mine:.1f} to {theirs:.1f} "
            f"({'favored' if mine > theirs else 'underdog' if mine < theirs else 'even'})."
        )

    lines += ["", "## Roster (S=starter, b=bench; {matchup, D rank, implied total, form, injury})"]
    lines += _roster_rows(team, wk)
    cap = roster_capacity(league, team)
    if cap["total"] is not None:
        lines.append(f"  Roster spots: {cap['used']}/{cap['total']} used, {cap['open'] or 0} open"
                     + (f"; IR {cap['ir_used']}/{cap['ir_total']}" if cap["ir_total"] else ""))

    lines += ["", "## Position strength (starters vs league average)"]
    lines += _strength_lines(league, team, wk)
    weak = [pos for pos, v in position_strength(league, team, wk).items() if v["verdict"] == "WEAK"]

    # Trouble: injured/out starters, byes in the next 3 weeks.
    trouble = [f"{_name(p)} ({injury_flag(p)})" for p in team.roster
               if is_starter(p) and injury_flag(p) in ("OUT", "INJURY_RESERVE", "SUSPENSION", "DOUBTFUL")]
    byes = _bye_weeks([p for p in team.roster if player_slot(p) != "IR"], wk, min(wk + 2, final))
    lines += ["", "## Watch list"]
    if trouble:
        lines.append(f"  Starters unlikely to play: {', '.join(trouble)}")
    for w in sorted(byes):
        starters = [n for n in byes[w] if any(_name(p) == n and is_starter(p) for p in team.roster)]
        lines.append(f"  Week {w} byes: {', '.join(byes[w])}"
                     + (f"  <-- {len(starters)} current starter(s)" if starters else ""))
    if not trouble and not byes:
        lines.append("  No injured starters or byes in the next 3 weeks.")

    # Pending claims (own team only; needs cookies).
    try:
        from .pending import _describe_items, _fetch_pending_raw
        items = _fetch_pending_raw(league)
        if items:
            lines += ["", "## Pending waiver / FA claims", _describe_items(league, items)]
    except Exception:  # noqa: BLE001
        pass

    # Best available at weak positions (plus RB/WR always, since that's where value is).
    scan = list(dict.fromkeys(weak + ["RB", "WR"]))
    lines += ["", f"## Best available free agents ({', '.join(scan)}; proj wk {wk}, Sleeper adds/24h)"]
    try:
        fas = league.free_agents(size=120)
    except Exception:  # noqa: BLE001
        fas = []
    for pos in scan:
        pool = [p for p in fas if player_position(p) == pos]
        pool.sort(key=lambda p: player_projected(p, wk), reverse=True)
        for p in pool[:3]:
            sl = sources.sleeper_info(getattr(p, "playerId", None), _name(p), getattr(p, "proTeam", None))
            trend = f", +{sl['trending_add']:,} adds" if sl.get("trending_add") else ""
            inj = f" [{injury_flag(p)}]" if injury_flag(p) else ""
            lines.append(f"  {pos:<4}{_name(p)[:22]:<23}proj {player_projected(p, wk):>5.1f}  "
                         f"avg {player_avg(p):>5.1f}{trend}{inj}{context_tag(p, wk)}")

    lines += [
        "",
        "Next: for any specific player in a decision, call analyze_player (full dossier). "
        "For a trade, call evaluate_trade with the exact players. For 'X or Y?', compare_players.",
    ]
    fresh = freshness_line()
    if fresh:
        lines += ["", fresh]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# evaluate_trade
# --------------------------------------------------------------------------- #
@mcp.tool()
def evaluate_trade(give: str, receive: str, partner_team: Optional[str] = None,
                   week: Optional[int] = None) -> str:
    """Pull everything needed to judge a trade in one call — both sides, both rosters.

    Args:
        give: Player(s) the user would send, comma-separated ("Chuba Hubbard" or
            "Hubbard, Pittman"). Must be on the user's team.
        receive: Player(s) the user would get back, comma-separated. Must all be
            on one other team (the partner is detected from who owns them).
        partner_team: Optional team/owner name to confirm the partner.
        week: Week to project for. Defaults to the upcoming actionable week.

    Returns: scoring format; the user's current needs (position strength vs
    league); the partner's needs (does the offer make sense for THEM?);
    roster-size legality (who would need to be dropped for a 2-for-1); the
    user's optimal starting-lineup projection before vs after; the same for the
    partner; position depth and bye coverage after the deal; and the full
    situational dossier (role, teammates' injuries, usage trend, coach/offense,
    matchups, schedule, news) for EVERY player in the trade. No verdict is
    given — weigh it and explain the why.
    """
    try:
        league = get_league()
    except RuntimeError as exc:
        return f"Error: {exc}"

    me = my_team(league)
    if me is None:
        return ("I don't know which team is yours — tell me your team/owner name "
                f"(or set ESPN_TEAM_ID).\n\n{team_choices(league)}")
    wk = week or upcoming_week(league)

    def resolve(names: list[str], side: str) -> tuple[list[tuple[Any, Any]], Optional[str]]:
        found: list[tuple[Any, Any]] = []
        for nm in names:
            pl, owner, sugg = find_player(league, nm)
            if pl is None:
                hint = f" Did you mean: {', '.join(sugg)}?" if sugg else ""
                return [], f"Could not find '{nm}' ({side}).{hint}"
            found.append((pl, owner))
        return found, None

    give_names, recv_names = _split_names(give), _split_names(receive)
    if not give_names or not recv_names:
        return "Name at least one player on each side (give=..., receive=...)."
    gives, err = resolve(give_names, "give")
    if err:
        return err
    recvs, err = resolve(recv_names, "receive")
    if err:
        return err

    problems = []
    for pl, owner in gives:
        if owner is None or owner.team_id != me.team_id:
            problems.append(f"{_name(pl)} is not on your team "
                            f"(owner: {owner.team_name if owner else 'free agent'}).")
    partners = {owner.team_id: owner for _, owner in recvs if owner is not None}
    for pl, owner in recvs:
        if owner is None:
            problems.append(f"{_name(pl)} is a free agent — just add him; no trade needed.")
        elif owner.team_id == me.team_id:
            problems.append(f"{_name(pl)} is already on your team.")
    if len(partners) > 1:
        problems.append("The players you'd receive are on different teams: "
                        + ", ".join(f"{_name(pl)} ({o.team_name})" for pl, o in recvs if o))
    if partner_team:
        pt = find_team(league, partner_team)
        if pt is None:
            problems.append(f"No team matched '{partner_team}'.")
        elif partners and pt.team_id not in partners:
            problems.append(f"{pt.team_name} doesn't own the players you'd receive "
                            f"({', '.join(o.team_name for o in partners.values())} does).")
    if problems:
        return "Trade doesn't line up:\n- " + "\n- ".join(problems)
    partner = next(iter(partners.values()))

    give_pl = [pl for pl, _ in gives]
    recv_pl = [pl for pl, _ in recvs]
    my_after = [p for p in me.roster if p not in give_pl] + recv_pl
    their_after = [p for p in partner.roster if p not in recv_pl] + give_pl

    lines = [
        f"# Trade evaluation — week {wk} (today {_dt.date.today():%a %b %-d, %Y})",
        f"{me.team_name} gives: {', '.join(_name(p) for p in give_pl)}",
        f"{me.team_name} gets:  {', '.join(_name(p) for p in recv_pl)}  (from {partner.team_name}, "
        f"{getattr(partner, 'wins', 0)}-{getattr(partner, 'losses', 0)})",
        f"Scoring: {scoring_kind(league)}. Your record {getattr(me, 'wins', 0)}-{getattr(me, 'losses', 0)}.",
    ]

    # Roster legality.
    cap = roster_capacity(league, me)
    net = len(recv_pl) - len(give_pl)
    if net > 0 and cap["total"] is not None:
        openn = cap["open"] or 0
        if openn < net:
            bench = sorted((p for p in me.roster if not is_starter(p) and p not in give_pl
                            and player_slot(p) != "IR"), key=player_avg)
            lines.append(f"Roster: you'd take on {net} extra player(s) with {openn} open spot(s) — "
                         f"you'd need to drop {net - openn}. Weakest bench by avg: "
                         + ", ".join(f"{_name(p)} ({player_avg(p):.1f})" for p in bench[:3]))
        else:
            lines.append(f"Roster: {net} extra player(s) fits ({openn} open spot(s)).")
    elif net < 0:
        lines.append(f"Roster: frees {-net} spot(s) — remember to use them (waiver adds).")

    # Lineup impact.
    b_me = _optimal_total(league, list(me.roster), wk)
    a_me = _optimal_total(league, my_after, wk)
    b_th = _optimal_total(league, list(partner.roster), wk)
    a_th = _optimal_total(league, their_after, wk)
    lines += ["", f"## Optimal starting lineup projection, week {wk} (before -> after)"]
    if b_me is not None and a_me is not None:
        lines.append(f"  {me.team_name}: {b_me:.1f} -> {a_me:.1f} ({a_me - b_me:+.1f})")
    if b_th is not None and a_th is not None:
        lines.append(f"  {partner.team_name}: {b_th:.1f} -> {a_th:.1f} ({a_th - b_th:+.1f})")
    lines.append("  (This is one week on ESPN projections only — a floor for the analysis, not the answer. "
                 "Byes, injuries returning, role changes, and playoff schedules below can outweigh it.)")

    # Needs on both sides.
    lines += ["", f"## Your position strength now (starters vs league avg, wk {wk})"]
    lines += _strength_lines(league, me, wk)
    lines += ["", f"## {partner.team_name}'s position strength now"]
    lines += _strength_lines(league, partner, wk)
    lines.append("  A deal only happens if it fills THEIR need too — check that what you send helps a WEAK spot of theirs.")

    # Depth at affected positions, after.
    positions = sorted({player_position(p) for p in give_pl + recv_pl})
    lines += ["", "## Your depth at affected positions AFTER the trade (by season avg)"]
    for pos in positions:
        group = sorted((p for p in my_after if player_position(p) == pos), key=player_avg, reverse=True)
        lines.append(f"  {pos}: " + ", ".join(
            f"{_name(p)} {player_avg(p):.1f}{' [' + injury_flag(p) + ']' if injury_flag(p) else ''}"
            for p in group) if group else f"  {pos}: nobody")

    # Bye coverage for the involved players.
    final = getattr(league, "finalScoringPeriod", None) or 17
    byes_in = _bye_weeks(recv_pl, wk, final)
    byes_out = _bye_weeks(give_pl, wk, final)
    if byes_in or byes_out:
        lines += ["", "## Remaining byes of the players involved"]
        for w in sorted(set(byes_in) | set(byes_out)):
            parts = []
            if w in byes_in:
                parts.append("you'd receive: " + ", ".join(byes_in[w]))
            if w in byes_out:
                parts.append("you'd send: " + ", ".join(byes_out[w]))
            lines.append(f"  Week {w}: " + " | ".join(parts))

    # Full dossiers.
    lines += ["", "# Players you'd GIVE"]
    for p in give_pl:
        lines += [""] + player_briefing(p, wk, league, full=True, news_limit=5)
    lines += ["", "# Players you'd RECEIVE"]
    for p in recv_pl:
        lines += [""] + player_briefing(p, wk, league, full=True, news_limit=5)

    lines += ["", ANALYST_QUESTIONS.rstrip(),
              "Then answer: who wins the deal this week, over the rest of the season, and in the "
              "fantasy playoffs — and whether it addresses the user's actual weakness above."]
    fresh = freshness_line()
    if fresh:
        lines += ["", fresh]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# compare_players
# --------------------------------------------------------------------------- #
@mcp.tool()
def compare_players(player_names: str, week: Optional[int] = None) -> str:
    """Side-by-side dossiers for 2-5 players — for "X or Y?" start/sit, waiver, or value questions.

    Args:
        player_names: Comma-separated names ("Hubbard, Zay Flowers, Pittman").
        week: Week to project for. Defaults to the upcoming actionable week.

    Starts with a summary table (projection, recent avg, floor/ceiling, last
    snap share, implied total, opponent defense, injury, owner) then the full
    situational dossier for each player. Also notes which of them the user
    already owns and whether they'd actually start.
    """
    try:
        league = get_league()
    except RuntimeError as exc:
        return f"Error: {exc}"
    names = _split_names(player_names)
    if len(names) < 2:
        return "Give at least two player names, comma-separated."
    if len(names) > 5:
        return "Compare at most five players at once."
    wk = week or upcoming_week(league)
    me = my_team(league)
    season = season_year(league)

    found: list[tuple[Any, Any]] = []
    for nm in names:
        pl, owner, sugg = find_player(league, nm)
        if pl is None:
            hint = f" Did you mean: {', '.join(sugg)}?" if sugg else ""
            return f"Could not find '{nm}'.{hint}"
        found.append((pl, owner))

    from .. import signals
    lines = [f"# Player comparison — week {wk} (today {_dt.date.today():%a %b %-d, %Y}); "
             f"scoring {scoring_kind(league)}", "",
             "## Summary",
             "  player                 pos  proj   rec-avg  floor/ceil  snap%  ITT   oppD  status      owner"]
    for pl, owner in found:
        card = full_stats_player(league, pl)
        form = signals.recent_form(card)
        vol = signals.volatility(card)
        pro = sources._norm_team(getattr(pl, "proTeam", None))
        env = sources.game_environment(wk).get(pro, {})
        opp = env.get("opponent", "")
        d = sources.team_strength().get(opp, {}).get("def_rank")
        rows = sources.usage_trend(getattr(pl, "playerId", None), _name(pl), pro, season, max(1, wk - 1), 2)
        snap = next((r["snap_pct"] for r in reversed(rows) if r["snap_pct"] is not None), None)
        own = ("YOU" if owner is not None and me is not None and owner.team_id == me.team_id
               else (owner.team_name[:12] if owner else "FA"))
        lines.append(
            f"  {_name(pl)[:22]:<23}{player_position(pl):<5}{player_projected(pl, wk):>5.1f}  "
            f"{(form.get('recent_avg') if form else 0) or 0:>7.1f}  "
            f"{(str(vol.get('floor')) + '/' + str(vol.get('ceiling'))) if vol else '  n/a   ':>10}  "
            f"{(str(snap) + '%') if snap is not None else '  - ':>5}  "
            f"{env.get('implied_total') if env.get('has_odds') else '-':>4}  "
            f"{(str(d) if d else '-'):>4}  {(injury_flag(pl) or 'ok')[:11]:<11} {own}"
        )
    if me is not None:
        for pl, owner in found:
            if owner is not None and owner.team_id == me.team_id:
                lines.append(f"  {_name(pl)} is on your roster, currently {'starting' if is_starter(pl) else 'on the bench'} ({player_slot(pl)}).")

    for pl, owner in found:
        lines += [""] + player_briefing(pl, wk, league, full=True, news_limit=4)
        lines.append(f"- Fantasy owner: {owner.team_name if owner else 'FREE AGENT'}")
    lines += ["", ANALYST_QUESTIONS.rstrip()]
    fresh = freshness_line()
    if fresh:
        lines += ["", fresh]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# get_player_news
# --------------------------------------------------------------------------- #
@mcp.tool()
def get_player_news(player_name: str, limit: int = 10) -> str:
    """Latest dated news for a player (Rotowire/ESPN blurbs, practice reports, ESPN's current story).

    Args:
        player_name: Full or partial name (misspellings tolerated).
        limit: Max blurbs (default 10).

    Use when the question is "what's going on with X?" or to check a specific
    rumour (injury, role change, coach comment). analyze_player already
    includes the latest few; this returns more, with dates, nothing else.
    """
    try:
        league = get_league()
    except RuntimeError as exc:
        return f"Error: {exc}"
    pl, owner, sugg = find_player(league, player_name)
    if pl is None:
        return (f"Could not find '{player_name}'."
                + (f" Did you mean: {', '.join(sugg)}?" if sugg else ""))
    pid = getattr(pl, "playerId", None)
    items = sources.player_news(pid, limit)
    ov = sources.player_overview(pid)
    pro = sources._norm_team(getattr(pl, "proTeam", None))
    sl = sources.sleeper_info(pid, _name(pl), pro)
    lines = [f"# News — {_name(pl)} ({player_position(pl)}, {getattr(pl, 'proTeam', '?')})  "
             f"today {_dt.date.today():%a %b %-d, %Y}"]
    if sl.get("news_updated"):
        from .analysis import _ago
        lines.append(f"Sleeper last news update: {_ago(sl['news_updated'])}"
                     + (f"; injury {sl.get('injury_status')} ({sl.get('injury_body_part') or ''}) {sl.get('injury_notes') or ''}".rstrip()
                        if sl.get("injury_status") else ""))
    lines.append("")
    if not items and not ov.get("story"):
        lines.append("No news items available from ESPN's feed for this player.")
    for it in items:
        if it.get("premium"):
            continue
        text = it["headline"] if (not it["body"] or it["body"][:40] in it["headline"]) else f"{it['headline']} — {it['body']}"
        lines.append(f"- {_fmt_iso(it['published'])} [{it['type']}] {text}")
    if ov.get("story"):
        lines += ["", f"## ESPN current story ({str(ov.get('published'))[:20]})", ov["story"]]
    if ov.get("outlook"):
        lines += ["", "## Season outlook (written preseason — may be stale)", ov["outlook"]]
    lines += ["", "If the decision hinges on something newer than these dates (a practice report, "
              "a coach quote), use web search for the latest beat-writer reporting."]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# get_nfl_team_situation
# --------------------------------------------------------------------------- #
@mcp.tool()
def get_nfl_team_situation(nfl_team: str) -> str:
    """The real NFL team behind a fantasy player: coach, offense quality, depth chart, injuries, schedule.

    Args:
        nfl_team: Team abbreviation or name ("CAR", "Panthers", "Carolina").

    Use for "is this offense any good?", "who's the starting RB in X?",
    "which skill players on X are hurt?", or to judge a schedule.
    """
    try:
        league = get_league()
    except RuntimeError as exc:
        return f"Error: {exc}"
    abbr = sources.resolve_nfl_team(nfl_team)
    strength = sources.team_strength()
    if not abbr:
        return f"Unknown NFL team '{nfl_team}'. Use an abbreviation like CAR, BAL, KC or a nickname."
    st = strength.get(abbr, {})
    roster = sources.nfl_roster(abbr)
    season = season_year(league)
    wk = upcoming_week(league)
    n = len(strength) or 32
    lines = [f"# {abbr} — team situation (today {_dt.date.today():%a %b %-d, %Y})",
             f"Record {st.get('record', '?')}; offense rank {st.get('off_rank')}/{n} ({st.get('pf_pg')} ppg); "
             f"defense rank {st.get('def_rank')}/{n} ({st.get('pa_pg')} allowed/g); head coach {roster.get('coach') or '?'}"]
    env = sources.game_environment(wk).get(abbr, {})
    if env:
        lines.append(f"Week {wk}: {'vs' if env.get('home') else '@'} {env.get('opponent')} "
                     f"{_fmt_iso(env.get('kickoff'), with_time=True)}"
                     + (f"; implied total {env.get('implied_total')}, O/U {env.get('over_under')}, spread {env.get('spread')}" if env.get("has_odds") else "; odds not posted")
                     + (f"; weather {env['weather']}" if env.get("weather") else ""))
    chart = sources.depth_chart(abbr, season)
    by_id = {p["espn_id"]: p for p in roster.get("players", [])}
    lines += ["", "## Offensive depth chart (official) with status"]
    for pos in ("QB", "RB", "WR", "TE"):
        ids = chart.get(pos, [])
        cells = []
        for i, pid in enumerate(ids[:5]):
            p = by_id.get(pid, {})
            nm = p.get("name", pid)
            tag = f" [{p['injury_status']}{(' ' + p['injury_type']) if p.get('injury_type') else ''}]" if p.get("injury_status") else ""
            cells.append(f"{pos}{i + 1} {nm}{tag}")
        lines.append(f"  {pos}: " + (", ".join(cells) if cells else "n/a"))
    hurt = [p for p in roster.get("players", [])
            if p["position"] in ("QB", "RB", "WR", "TE", "OT", "G", "C", "OL") and (p.get("injury_status") or p.get("group") == "injuredReserveOrOut")]
    lines += ["", "## Injured / IR on offense"]
    if hurt:
        for p in hurt:
            lines.append(f"  {p['position']:<3} {p['name']} — {p.get('injury_status') or 'IR/out list'}"
                         + (f" ({p['injury_type']})" if p.get("injury_type") else "")
                         + (f", since {_fmt_iso(p['injury_date'])}" if p.get("injury_date") else "")
                         + (f", est. return {_fmt_iso(p['return_date'])}" if p.get("return_date") else ""))
    else:
        lines.append("  none listed")
    sched = sources.team_schedule(abbr)
    if sched.get("games"):
        lines += ["", f"## Schedule (bye week {sched.get('bye_week')})"]
        cells = []
        for g in sched["games"]:
            if not g.get("week"):
                continue
            d = strength.get(g["opponent"], {}).get("def_rank")
            cells.append(f"wk{g['week']} {'vs' if g.get('home') else '@'}{g['opponent']}"
                         + (f"(D{d})" if d else "") + (f" {g['result']}" if g.get("result") else ""))
        lines.append("  " + "  ".join(cells))
    return "\n".join(lines)
