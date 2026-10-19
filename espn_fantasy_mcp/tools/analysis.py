"""Deep, multi-factor player review — the antidote to judging by one projection.

``analyze_player`` assembles every signal we have about a player into a single
dossier and hands it to Claude to synthesize: ESPN weekly history + stat
breakdown, the real NFL situation (head coach, offense quality, official depth
chart, every teammate in the positional room and his injury status, snap /
carry / target share week by week), health, the Vegas game environment and
weather, the rest-of-season schedule, and dated news. The tool deliberately
does NOT emit a verdict or a blended score — it lays out the evidence and the
open questions, so the judgment stays with the model.

``player_briefing`` is shared by the composite tools (evaluate_trade,
compare_players, ...) so every player-level decision sees the same evidence.
"""

from __future__ import annotations

import datetime as _dt
import time
from typing import Any, Optional

from ..app import mcp
from ..client import freshness_line, get_league
from .. import signals, sources
from ..formatting import (
    find_player,
    injury_flag,
    player_position,
    player_projected,
    season_year,
    upcoming_week,
)

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # noqa: BLE001 - tz database missing; fall back to UTC labels
    _ET = None


def _norm_team(abbr: Any) -> str:
    return sources._norm_team(abbr)


# --- small formatting helpers ------------------------------------------------


def _fmt_iso(stamp: Any, with_time: bool = False) -> str:
    """'2026-10-18T15:44Z' -> 'Oct 18' (or 'Sun Oct 18 1:00pm ET')."""
    if not stamp:
        return ""
    try:
        s = str(stamp).replace("Z", "+00:00")
        dt = _dt.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        if _ET is not None:
            dt = dt.astimezone(_ET)
        if with_time:
            return dt.strftime("%a %b %-d %-I:%M%p ET").replace("AM", "am").replace("PM", "pm")
        return dt.strftime("%b %-d")
    except (ValueError, TypeError):
        return str(stamp)[:10]


def _ago(epoch_ms: Any) -> str:
    if not isinstance(epoch_ms, (int, float)):
        return ""
    secs = time.time() - epoch_ms / 1000.0
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


_card_cache: dict[str, tuple[float, Any]] = {}


def full_stats_player(league: Any, player: Any) -> Any:
    """The player-card version of a player (all weeks of stats), cached ~10 min.

    Roster objects only carry the season bucket and the current week, which is
    useless for form/volatility/usage. ESPN's player card has every week. Falls
    back to the given object if the card can't be fetched.
    """
    pid = getattr(player, "playerId", None)
    if pid in (None, ""):
        return player
    stats = getattr(player, "stats", None)
    if isinstance(stats, dict) and sum(1 for k in stats if isinstance(k, int) and k > 0) >= 3:
        return player  # already a card
    hit = _card_cache.get(str(pid))
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    try:
        card = league.player_info(playerId=pid)
        if isinstance(card, list):
            card = card[0] if card else None
    except Exception:  # noqa: BLE001
        card = None
    if card is None:
        return player
    _card_cache[str(pid)] = (time.time(), card)
    return card


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


# --- evidence sections ---------------------------------------------------------


def _matchup_lines(player: Any, week: int) -> list[str]:
    """Vegas game environment, weather + opponent-defense lines for the player's team."""
    pro = _norm_team(getattr(player, "proTeam", None))
    if not pro:
        return ["- Matchup: pro team unknown"]
    env = sources.game_environment(week).get(pro, {})
    strength = sources.team_strength()
    opp = env.get("opponent") or _schedule_opponent(player, week)

    out: list[str] = []
    if not opp:
        return [f"- Matchup (wk {week}): no game found — BYE week"]

    where = "vs" if env.get("home") else "@"
    line = f"- Matchup (wk {week}): {where} {opp}"
    if env.get("kickoff"):
        line += f", {_fmt_iso(env['kickoff'], with_time=True)}"
    if env.get("has_odds"):
        it = env.get("implied_total")
        ou = env.get("over_under")
        if it is not None:
            env_read = ("high-scoring spot" if it >= 25 else
                        "low-scoring spot" if it <= 18 else "average scoring spot")
            line += f" | Vegas implied team total {it} ({env_read}); O/U {ou}, spread {env.get('spread')}"
    else:
        line += " | Vegas odds not posted yet"
    if env.get("weather"):
        line += f" | weather: {env['weather']}"
    if env.get("state") == "in":
        line += " | GAME IN PROGRESS"
    elif env.get("state") == "post":
        line += " | game final"
    out.append(line)

    opp_def = strength.get(opp)
    if opp_def:
        rank = opp_def["def_rank"]
        n = len(strength)
        tier = ("tough D" if rank <= 8 else "soft D" if rank >= n - 7 else "middling D")
        out.append(
            f"- Opponent defense: {opp} allows {opp_def['pa_pg']} pts/game — rank {rank}/{n} "
            f"({tier}); all-position points allowed, not vs-{player_position(player)} specifically"
        )
    return out


def _form_lines(player: Any) -> list[str]:
    form = signals.recent_form(player)
    if not form:
        return ["- Recent form: not enough game history yet"]
    missed = signals.missed_games(player)
    return [
        f"- Recent form: last {form['recent_weeks']} wk avg {form['recent_avg']} "
        f"vs season {form['season_avg']} ({form['delta_pct']:+.0f}%, {form['trend']}); "
        f"last games: {', '.join(str(s) for s in form['last_scores'])}"
        + (f"; MISSED wk {', '.join(map(str, missed))} (no stat line)" if missed else "")
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
    use = signals.usage(player)  # summed over played weeks
    if not use:
        return []
    labels = {
        "targets": "targets", "receptions": "rec", "rec_yards": "rec yds",
        "carries": "carries", "rush_yards": "rush yds", "pass_attempts": "pass att",
        "pass_yards": "pass yds", "red_zone_targets": "RZ targets",
    }
    games = use.pop("games", 0)
    if games:
        parts = [f"{labels.get(k, k)} {v:.0f} ({v / games:.1f}/g)" for k, v in use.items()]
        return [f"- Opportunity (ESPN, {games:.0f} games): {', '.join(parts)}"]
    parts = [f"{labels.get(k, k)} {v:.0f}" for k, v in use.items()]
    return [f"- Opportunity (ESPN season): {', '.join(parts)}"]


def _situation_lines(player: Any, season: int) -> list[str]:
    """Team quality, coach, official depth chart, and the positional room."""
    pro = _norm_team(getattr(player, "proTeam", None))
    pos = player_position(player)
    pid = str(getattr(player, "playerId", "") or "")
    name = getattr(player, "name", "")
    out: list[str] = []
    if not pro:
        return out

    strength = sources.team_strength().get(pro, {})
    roster = sources.nfl_roster(pro)
    coach = roster.get("coach") or "?"
    n = len(sources.team_strength()) or 32
    team_bits = [f"{pro} {strength.get('record', '?')}"]
    if strength.get("off_rank"):
        team_bits.append(f"offense rank {strength['off_rank']}/{n} ({strength.get('pf_pg')} ppg)")
    if strength.get("def_rank"):
        team_bits.append(f"defense rank {strength['def_rank']}/{n}")
    out.append(f"- Team: {', '.join(team_bits)}; head coach {coach}")

    # Official ESPN depth chart position for this player.
    chart = sources.depth_chart(pro, season).get(pos if pos != "D/ST" else "", [])
    sl = sources.sleeper_info(pid, name, pro)
    role_bits: list[str] = []
    if chart and pid in chart:
        role_bits.append(f"official depth chart {pos}{chart.index(pid) + 1} of {len(chart)}")
    if sl.get("depth_chart_order"):
        role_bits.append(f"Sleeper depth #{sl['depth_chart_order']}")
    me = next((p for p in roster.get("players", []) if p["espn_id"] == pid), None)
    if me:
        bits = []
        if me.get("age"):
            bits.append(f"age {me['age']}")
        if me.get("exp") is not None:
            bits.append(f"{'rookie' if not me['exp'] else str(me['exp']) + ' yrs exp'}")
        if bits:
            role_bits.append(", ".join(bits))
    if role_bits:
        out.append(f"- Role: {'; '.join(role_bits)}")

    # The positional room: every teammate at the position and his status. This
    # is where "his backup is coming back" or "he's in a committee" shows up.
    if pos in ("QB", "RB", "WR", "TE"):
        room_rows: list[str] = []
        sidelined: list[str] = []
        espn_room = {p["espn_id"]: p for p in roster.get("players", []) if p["position"] == pos}
        sleeper_room = sources.position_room(pro, pos)
        seen: set[str] = set()
        ordered: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
        for s_p in sleeper_room:
            e_p = next((e for e in espn_room.values()
                        if sources.norm_name(e["name"]) == sources.norm_name(s_p["name"])), {})
            if e_p:
                seen.add(e_p["espn_id"])
            order = s_p.get("depth_chart_order") or 99
            ordered.append((order, s_p, e_p))
        for e_id, e_p in espn_room.items():
            if e_id not in seen:
                ordered.append((98, {}, e_p))
        ordered.sort(key=lambda t: t[0])
        for order, s_p, e_p in ordered[:7]:
            nm = s_p.get("name") or e_p.get("name") or "?"
            is_me = (e_p.get("espn_id") == pid) or (
                sources.norm_name(nm) == sources.norm_name(name))
            status = e_p.get("injury_status") or s_p.get("injury_status") or ""
            grp = e_p.get("group") or ""
            tags: list[str] = []
            if status:
                detail = s_p.get("injury_body_part") or e_p.get("injury_type") or ""
                notes = s_p.get("injury_notes") or ""
                since = _fmt_iso(e_p.get("injury_date"))
                t = status
                if detail:
                    t += f" ({detail})"
                if notes and notes.lower() != str(detail).lower():
                    t += f" — {notes}"
                if since:
                    t += f", since {since}"
                tags.append(t)
            if grp == "injuredReserveOrOut" and "IR" not in " ".join(tags).upper():
                tags.append("on IR/out list")
            elif grp == "practiceSquad":
                tags.append("practice squad")
            if s_p.get("practice_description"):
                tags.append(f"practice: {s_p['practice_description']}")
            if s_p.get("trending_add"):
                tags.append(f"+{s_p['trending_add']:,} adds/24h")
            if s_p.get("news_updated"):
                tags.append(f"news {_ago(s_p['news_updated'])}")
            label = f"{pos}{order}" if order < 98 else pos
            row = f"    {label:<4} {nm}{' (THIS PLAYER)' if is_me else ''}"
            if tags:
                row += f" — {'; '.join(tags)}"
            room_rows.append(row)
            if status and not is_me and (order <= 4 or grp == "injuredReserveOrOut"):
                sidelined.append(f"{nm} ({status})")
        if room_rows:
            out.append(f"- {pos} room on {pro}:")
            out.extend(room_rows)
        if sidelined:
            out.append(
                f"  ⚠ Sidelined teammates at {pos}: {', '.join(sidelined)}. A returning "
                "teammate changes the touch split — check the news below and recent "
                "reports for a return timeline before valuing this player on current usage."
            )

    return out


def _usage_trend_lines(player: Any, season: int, through_week: int) -> list[str]:
    pro = _norm_team(getattr(player, "proTeam", None))
    rows = sources.usage_trend(getattr(player, "playerId", None), getattr(player, "name", ""),
                               pro, season, through_week)
    if not rows:
        return []
    pos = player_position(player)
    cells = []
    for r in rows:
        bits = [f"wk{r['week']}:"]
        if r["snap_pct"] is not None:
            bits.append(f"{r['snap_pct']}% snaps")
        elif r["snaps"] == 0 and (r["carries"] or r["targets"]):
            bits.append("snaps pending")
        if pos in ("RB", "QB") or r["carries"]:
            c = f"{r['carries']} car"
            if r["carry_share"]:
                c += f" ({r['carry_share']}% of team)"
            bits.append(c)
        if pos in ("RB", "WR", "TE") or r["targets"]:
            t = f"{r['targets']} tgt"
            if r["target_share"]:
                t += f" ({r['target_share']}%)"
            bits.append(t)
        if r["rz_touches"]:
            bits.append(f"{r['rz_touches']} RZ")
        cells.append(" ".join(bits))
    out = [f"- Usage trend (Sleeper, last {len(rows)} played):"]
    out += [f"    {c}" for c in cells]
    snaps = [r["snap_pct"] for r in rows if r["snap_pct"] is not None]
    if len(snaps) >= 2:
        if snaps[-1] <= snaps[0] - 12:
            out.append("  ⚠ Snap share is falling — role may be shrinking (committee, injury, or scheme).")
        elif snaps[-1] >= snaps[0] + 12:
            out.append("  ↑ Snap share is rising — role is growing.")
        if max(snaps) < 55 and pos in ("RB", "WR", "TE"):
            out.append("  ⚠ Never above ~55% of snaps — this is a part-time / committee role.")
    return out


def _health_lines(player: Any) -> list[str]:
    pro = _norm_team(getattr(player, "proTeam", None))
    sl = sources.sleeper_info(getattr(player, "playerId", None), getattr(player, "name", ""), pro)
    me = next((p for p in sources.nfl_roster(pro).get("players", [])
               if p["espn_id"] == str(getattr(player, "playerId", ""))), {})
    bits: list[str] = []
    inj = injury_flag(player)
    if inj:
        bits.append(f"ESPN fantasy status {inj}")
    if me.get("injury_status"):
        t = f"NFL report: {me['injury_status']}"
        if me.get("injury_type"):
            t += f" ({me['injury_type']})"
        if me.get("injury_date"):
            t += f" as of {_fmt_iso(me['injury_date'])}"
        if me.get("return_date"):
            t += f", est. return {_fmt_iso(me['return_date'])}"
        bits.append(t)
    if sl.get("injury_status"):
        t = f"Sleeper: {sl['injury_status']}"
        if sl.get("injury_body_part"):
            t += f" ({sl['injury_body_part']})"
        if sl.get("injury_notes"):
            t += f" — {sl['injury_notes']}"
        bits.append(t)
    if sl.get("practice_participation") or sl.get("practice_description"):
        bits.append(f"practice: {sl.get('practice_description') or sl.get('practice_participation')}")
    if sl.get("status") and sl["status"] != "Active":
        bits.append(f"roster status {sl['status']}")
    if sl.get("news_updated"):
        bits.append(f"news last moved {_ago(sl['news_updated'])}")
    if not bits:
        return ["- Health: no injury designation on ESPN, the NFL report, or Sleeper"]
    return [f"- Health: {'; '.join(bits)}"]


def _market_lines(player: Any) -> list[str]:
    pro = _norm_team(getattr(player, "proTeam", None))
    sl = sources.sleeper_info(getattr(player, "playerId", None), getattr(player, "name", ""), pro)
    bits: list[str] = []
    mkt = signals.market(player)
    if "owned_pct" in mkt:
        bits.append(f"{mkt['owned_pct']:.0f}% rostered")
    if "started_pct" in mkt:
        bits.append(f"{mkt['started_pct']:.0f}% started")
    if "pos_rank" in mkt:
        bits.append(f"ESPN {player_position(player)}{mkt['pos_rank']}")
    add, drop = sl.get("trending_add"), sl.get("trending_drop")
    if add:
        bits.append(f"Sleeper: added by {add:,} teams/24h")
    if drop:
        bits.append(f"dropped by {drop:,}/24h")
    return [f"- Market: {', '.join(bits)}"] if bits else []


def _schedule_lines(player: Any, league: Any, from_week: int) -> list[str]:
    pro = _norm_team(getattr(player, "proTeam", None))
    sched = sources.team_schedule(pro)
    strength = sources.team_strength()
    n = len(strength) or 32
    reg = getattr(league.settings, "reg_season_count", 0) or 14
    final = getattr(league, "finalScoringPeriod", None) or 17
    games = {g["week"]: g for g in sched.get("games", []) if g.get("week")}
    if not games:
        return []
    cells: list[str] = []
    ranks: list[int] = []
    playoff_ranks: list[int] = []
    for wk in range(from_week, final + 1):
        g = games.get(wk)
        if not g or not g.get("opponent"):
            cells.append(f"wk{wk} BYE")
            continue
        d = strength.get(g["opponent"], {}).get("def_rank")
        tag = f"wk{wk} {'vs' if g.get('home') else '@'}{g['opponent']}"
        if d:
            tag += f"(D{d})"
            (playoff_ranks if wk > reg else ranks).append(d)
        cells.append(tag)
    out = [f"- Remaining schedule (Dn = opponent defense rank, 1 toughest .. {n} softest):"]
    out.append("    " + "  ".join(cells))
    summary = []
    if ranks:
        summary.append(f"avg opp D rank through wk {reg}: {sum(ranks) / len(ranks):.0f}")
    if playoff_ranks:
        summary.append(f"fantasy playoffs (wk {reg + 1}-{final}): {sum(playoff_ranks) / len(playoff_ranks):.0f}")
    bye = sched.get("bye_week")
    if bye and bye >= from_week:
        summary.append(f"BYE week {bye} still ahead")
    if summary:
        out.append("    " + "; ".join(summary))
    return out


def _news_lines(player: Any, limit: int) -> list[str]:
    pid = getattr(player, "playerId", None)
    items = sources.player_news(pid, limit)
    ov = sources.player_overview(pid)
    out: list[str] = []
    for it in items:
        if it.get("premium"):
            continue
        head = it["headline"]
        body = it["body"]
        text = head if (not body or body[:40] in head) else f"{head} — {body}"
        out.append(f"    {_fmt_iso(it['published'])} [{it['type']}] {text[:320]}")
    if ov.get("story") and ov["story"][:60] not in " ".join(out):
        out.append(f"    ESPN current story ({str(ov.get('published'))[:16]}): {ov['story'][:600]}")
    if ov.get("outlook"):
        out.append(f"    Season outlook (preseason, may be dated): {ov['outlook'][:300]}")
    if not out:
        return []
    return ["- Latest news (newest first; check the dates — anything older than this "
            "week may be stale):"] + out


def player_briefing(player: Any, week: int, league: Any = None, full: bool = True,
                    news_limit: int = 6) -> list[str]:
    """The multi-factor evidence block for one player (list of md lines).

    ``full`` adds the NFL situation (coach, offense, depth chart, positional
    room), snap/touch trend, health detail, rest-of-season schedule, and news.
    The compact form is for lists where many players appear.
    """
    if full and league is not None:
        player = full_stats_player(league, player)
    proj = player_projected(player, week)
    inj = injury_flag(player)
    season = season_year(league) if league is not None else sources.config.YEAR
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
    if full:
        lines += _usage_trend_lines(player, season, max(1, week - 1))
        lines += _situation_lines(player, season)
        lines += _health_lines(player)
    lines += _matchup_lines(player, week)
    lines += _market_lines(player)
    if full:
        if league is not None:
            lines += _schedule_lines(player, league, week)
        lines += _news_lines(player, news_limit)
    return lines


ANALYST_QUESTIONS = """\
## Questions to settle before giving advice (answer from the evidence above + recent news)
1. Role security: is he the clear starter, or in a committee / timeshare? Is anyone
   in his room injured and coming back, or a rookie/trade pushing for snaps?
2. Offense & coaching: is this a good offense (rank, ppg, QB play)? What does the
   head coach / play-caller's scheme mean for this position's volume?
3. Health: his own injury status and practice reports — and does he have a history
   of soft-tissue or recurring issues?
4. Usage, not points: are snaps, carries, targets and red-zone looks rising, flat,
   or falling? Volume predicts next week better than last week's score.
5. This week: implied team total, spread/game script, opponent defense, weather.
6. Rest of season: schedule difficulty, bye still ahead, playoff-week matchups.
7. Floor vs ceiling: does the user need a safe floor or a high ceiling here?
8. Scoring format fit (PPR vs standard, TE premium, etc.).
9. For the user's roster: does this change who actually starts for them?
If any of these can't be answered from the data here, say so and — when web
search is available — look it up (beat reports, practice reports, coach quotes).
Never assume a player's situation from memory; it changes weekly.
"""


@mcp.tool()
def analyze_player(player_name: str, week: Optional[int] = None) -> str:
    """Full situational dossier on one player — everything that matters, not just a projection.

    Args:
        player_name: Full or partial player name, misspellings tolerated
            (rostered or free agent).
        week: Week to analyze for. Defaults to the upcoming actionable week.

    Returns, in one call: ESPN projection + sanity check vs recent scoring;
    recent form and floor/ceiling; season opportunity; week-by-week snap /
    carry / target share; the real NFL situation (team record, offense rank,
    head coach, official depth-chart slot, every teammate in his positional
    room with injury status — so a returning backup or a committee is obvious);
    health and practice reports; this week's matchup (Vegas implied total,
    spread, opponent defense rank, weather, kickoff); rest-of-season schedule
    incl. fantasy-playoff weeks; market (rostered %, trending adds); and dated
    news blurbs. Ends with the analyst questions to settle before advising.

    Use this for ANY player-level judgment: a trade piece, a start/sit
    toss-up, a waiver target, "is X good?", "what's going on with X?".
    """
    try:
        league = get_league()
    except RuntimeError as exc:
        return f"Error: {exc}"

    player, owner, suggestions = find_player(league, player_name)
    if player is None:
        if suggestions:
            return (f"No exact match for '{player_name}'. Did you mean: "
                    f"{', '.join(suggestions)}? Call again with the full name.")
        return (f"No player found matching '{player_name}'. Check the spelling or "
                "try a fuller name.")

    wk = week or upcoming_week(league)
    lines = [f"# Player review — week {wk}  (today {_dt.date.today():%a %b %-d, %Y})", ""]
    lines += player_briefing(player, wk, league, full=True)
    if owner is not None:
        lines.append(f"- Fantasy owner: {owner.team_name}")
    else:
        lines.append("- Fantasy owner: FREE AGENT / on waivers in this league")
    lines += ["", ANALYST_QUESTIONS.rstrip()]
    off = sources.external_status()
    if off:
        lines += ["", f"_{off} Situation/news/matchup sections above may be blank._"]
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
    strength = sources.team_strength()
    opp = env.get("opponent") or _schedule_opponent(player, week)
    bits: list[str] = []
    if opp:
        where = "vs" if env.get("home") else "@"
        seg = f"{where}{opp}"
        opp_def = strength.get(opp)
        if opp_def:
            n = len(strength) or 32
            seg += f" (D {opp_def['def_rank']}/{n})"
        if env.get("has_odds") and env.get("implied_total") is not None:
            seg += f", ITT {env['implied_total']}"
        bits.append(seg)
    form = signals.recent_form(player)
    if form and form["trend"] != "steady":
        bits.append(form["trend"])
    info = sources.sleeper_info(getattr(player, "playerId", None), getattr(player, "name", ""), pro)
    if info.get("injury_status"):
        bits.append(info["injury_status"])
    return "  {" + ", ".join(bits) + "}" if bits else ""
