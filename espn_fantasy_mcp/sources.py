"""Tier-1 external data sources that enrich ESPN's own projections.

ESPN's fantasy projection is a single, mediocre number. To review a player
*thoughtfully* we pull a few extra, freely-available signals from public,
no-auth endpoints and let Claude weigh them against the projection:

- **NFL game environment** (``site.api.espn.com`` scoreboard): each team's
  opponent, home/away, and — when the books have posted them — the Vegas
  over/under and spread, from which we derive an *implied team total*. A high
  implied total is one of the single best predictors of fantasy output, because
  it captures game script and offensive strength in one number.
- **Opponent defense strength** (``site.api.espn.com`` standings): season
  points-allowed per team, ranked 1 (toughest) .. 32 (softest). A position-
  agnostic but reliable read on matchup difficulty.
- **Player role, health, and market** (``api.sleeper.app``): Sleeper's player
  map (joined by ESPN id, or by name+team when the id is missing — it usually
  is) gives depth-chart order, granular injury/practice status, and — via the
  trending endpoints — how hard the fantasy market is adding/dropping a player.
- **Snap / touch share** (Sleeper weekly stats): offensive snaps vs team snaps,
  carries and targets vs the team's, red-zone looks — week by week, so a
  committee or a shrinking role is visible before the box score shows it.
- **NFL team situation** (``site.api.espn.com`` team roster / depth chart /
  schedule, ``sports.core.api.espn.com`` depth charts): head coach, offense and
  defense rank, the full positional room with every teammate's injury status
  (is the starter's backup about to return?), the official depth-chart order,
  bye week and the remaining schedule.
- **News** (ESPN fantasy player-news feed + athlete overview): dated Rotowire
  blurbs and ESPN's current story for a player — practice reports, coach quotes,
  role changes — so advice reflects this week, not August.

Everything here is **best-effort and non-fatal**: every fetch is cached with a
TTL and any network/parse failure degrades to ``None`` so a tool never breaks
because a source was slow or changed shape. Set ``ESPN_MCP_EXTERNAL=0`` to turn
the whole layer off (tools then fall back to ESPN-only behaviour).
"""

from __future__ import annotations

import time
from typing import Any, Optional

from . import config

# ``requests`` ships with ``espn-api`` (and bundles certifi), so it is always
# available here and — unlike stdlib urllib on python.org macOS builds — verifies
# TLS certs out of the box. We fall back to urllib only if it is somehow absent.
try:
    import requests as _requests
except ImportError:  # pragma: no cover - requests is an espn-api dependency
    _requests = None
    import urllib.request as _urllib

_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
_STANDINGS = "https://site.api.espn.com/apis/v2/sports/football/nfl/standings"
_SLEEPER_PLAYERS = "https://api.sleeper.app/v1/players/nfl"
_SLEEPER_TREND = "https://api.sleeper.app/v1/players/nfl/trending/{kind}?lookback_hours=24&limit=200"
_SLEEPER_STATS = "https://api.sleeper.app/v1/stats/nfl/regular/{season}/{week}"
_TEAMS = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams?limit=40"
_TEAM_ROSTER = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{abbr}/roster"
_TEAM_SCHEDULE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{abbr}/schedule"
_DEPTH_CHART = ("https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/"
                "seasons/{season}/teams/{team_id}/depthcharts")
_PLAYER_NEWS = ("https://site.web.api.espn.com/apis/fantasy/v2/games/ffl/news/players"
                "?playerId={pid}&limit={limit}")
_PLAYER_OVERVIEW = ("https://site.web.api.espn.com/apis/common/v3/sports/football/nfl/"
                    "athletes/{pid}/overview")

# ESPN and Sleeper mostly agree on team abbreviations; these are the exceptions.
_TEAM_ALIASES = {
    "WSH": "WSH",
    "WAS": "WSH",
    "LA": "LAR",
    "JAC": "JAX",
    "OAK": "LV",
    "SD": "LAC",
}


def _norm_team(abbr: Any) -> str:
    a = str(abbr or "").strip().upper()
    return _TEAM_ALIASES.get(a, a)


# --- tiny TTL cache -------------------------------------------------------

# key -> (fetched_at_epoch, value). Values may be None (a fetch that failed),
# which we still cache briefly so a dead source isn't hammered on every call.
_cache: dict[str, tuple[float, Any]] = {}


def _cached(key: str, ttl: float, producer) -> Any:
    now = time.time()
    hit = _cache.get(key)
    if hit is not None and (now - hit[0]) <= ttl:
        return hit[1]
    value = producer()
    # A failed fetch (None) is cached for a short window only, so a transient
    # outage recovers quickly rather than being pinned for the full TTL.
    _cache[key] = (now if value is not None else now - ttl + min(ttl, 60), value)
    return value


_HEADERS = {"User-Agent": "espn-fantasy-mcp/0.3 (+github)"}


def _get_json(url: str) -> Optional[Any]:
    """GET and parse JSON, returning None on any failure (never raises)."""
    if not config.EXTERNAL_ENABLED:
        return None
    try:
        if _requests is not None:
            resp = _requests.get(url, headers=_HEADERS, timeout=config.EXTERNAL_TIMEOUT)
            if resp.status_code >= 400:
                return None
            return resp.json()
        # Fallback: stdlib urllib (may fail on macOS builds lacking CA certs).
        import json
        req = _urllib.Request(url, headers=_HEADERS)
        with _urllib.urlopen(req, timeout=config.EXTERNAL_TIMEOUT) as resp:  # noqa: S310
            if getattr(resp, "status", 200) >= 400:
                return None
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - defensive: a source must never crash a tool
        return None


def external_status() -> str:
    """Human-readable note on whether external enrichment is available."""
    if not config.EXTERNAL_ENABLED:
        return "External enrichment is OFF (ESPN_MCP_EXTERNAL=0) — ESPN data only."
    return ""


# --- NFL game environment (opponent, home/away, Vegas implied totals) -----


def _implied_totals(over_under: Optional[float], fav_abbr: str, fav_line: float,
                    home: str, away: str) -> dict[str, Optional[float]]:
    """Split an over/under into per-team implied totals using the spread.

    The favorite's implied total is O/U-half plus half the line; the underdog's
    is O/U-half minus half the line. Returns {abbr: implied_total}.
    """
    if not over_under:
        return {home: None, away: None}
    half = over_under / 2.0
    edge = abs(fav_line) / 2.0
    if fav_abbr == home:
        return {home: round(half + edge, 1), away: round(half - edge, 1)}
    if fav_abbr == away:
        return {away: round(half + edge, 1), home: round(half - edge, 1)}
    # Unknown favorite: fall back to an even split.
    return {home: round(half, 1), away: round(half, 1)}


def _parse_odds(odds_list: Any, home: str, away: str) -> dict[str, Any]:
    """Pull over/under, spread and implied totals out of a scoreboard odds block."""
    out: dict[str, Any] = {"over_under": None, "spread": None, "details": None,
                           "implied": {home: None, away: None}}
    if not isinstance(odds_list, list) or not odds_list:
        return out
    o = odds_list[0] if isinstance(odds_list[0], dict) else {}
    ou = o.get("overUnder")
    details = o.get("details")  # e.g. "SEA -3.5"
    out["over_under"] = ou if isinstance(ou, (int, float)) else None
    out["details"] = details

    fav_abbr, fav_line = "", 0.0
    if isinstance(details, str) and " " in details:
        parts = details.rsplit(" ", 1)
        try:
            fav_line = float(parts[1])
            fav_abbr = _norm_team(parts[0])
        except (ValueError, IndexError):
            fav_abbr, fav_line = "", 0.0
    if not fav_abbr:
        # Fall back to homeTeamOdds/awayTeamOdds.favorite + numeric spread.
        spread = o.get("spread")
        if isinstance(spread, (int, float)):
            fav_line = spread
            ht = o.get("homeTeamOdds") or {}
            at = o.get("awayTeamOdds") or {}
            if ht.get("favorite"):
                fav_abbr = home
            elif at.get("favorite"):
                fav_abbr = away
    out["spread"] = fav_line if fav_abbr else o.get("spread")
    out["implied"] = _implied_totals(out["over_under"], fav_abbr, fav_line, home, away)
    return out


def _weather_text(weather: Any, venue: Any) -> str:
    """'Rain, 48F' / 'indoor' / '' — only what matters for passing games/kickers."""
    if isinstance(venue, dict) and venue.get("indoor"):
        return "indoor"
    if not isinstance(weather, dict):
        return ""
    desc = str(weather.get("displayValue") or "").strip()
    temp = weather.get("temperature")
    bits = [b for b in (desc, f"{temp}F" if isinstance(temp, (int, float)) else "") if b]
    return ", ".join(bits)


def game_environment(week: Optional[int] = None) -> dict[str, dict[str, Any]]:
    """Map each team abbrev -> its game context for the given week.

    Each value: {opponent, home (bool), kickoff, over_under, spread,
    implied_total, opp_implied_total, has_odds}. Empty dict if unavailable.
    """
    key = f"scoreboard:{week or 'cur'}"

    def fetch() -> dict[str, dict[str, Any]]:
        url = _SCOREBOARD + (f"?week={week}" if week else "")
        data = _get_json(url)
        if not isinstance(data, dict):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for ev in data.get("events", []) or []:
            comps = (ev.get("competitions") or [{}])[0]
            competitors = comps.get("competitors") or []
            home = away = ""
            for c in competitors:
                abbr = _norm_team((c.get("team") or {}).get("abbreviation"))
                if c.get("homeAway") == "home":
                    home = abbr
                elif c.get("homeAway") == "away":
                    away = abbr
            if not (home and away):
                continue
            odds = _parse_odds(comps.get("odds"), home, away)
            implied = odds["implied"]
            kickoff = ev.get("date")
            has_odds = odds["over_under"] is not None
            weather = _weather_text(ev.get("weather"), comps.get("venue"))
            status = ((ev.get("status") or {}).get("type") or {}).get("state")  # pre/in/post
            out[home] = {
                "opponent": away, "home": True, "kickoff": kickoff,
                "over_under": odds["over_under"], "spread": odds["spread"],
                "implied_total": implied.get(home), "opp_implied_total": implied.get(away),
                "has_odds": has_odds, "weather": weather, "state": status,
            }
            out[away] = {
                "opponent": home, "home": False, "kickoff": kickoff,
                "over_under": odds["over_under"], "spread": odds["spread"],
                "implied_total": implied.get(away), "opp_implied_total": implied.get(home),
                "has_odds": has_odds, "weather": weather, "state": status,
            }
        return out

    result = _cached(key, config.EXTERNAL_TTL_GAME, fetch)
    return result or {}


# --- team strength: defense (points allowed) and offense (points scored) --


def team_strength() -> dict[str, dict[str, Any]]:
    """Map team abbrev -> {def_rank, off_rank, pa_pg, pf_pg, record}. Empty if unavailable.

    def_rank 1 = fewest points allowed per game (toughest matchup); 32 = softest.
    off_rank 1 = most points scored per game (best offense); 32 = worst.
    """

    def fetch() -> dict[str, dict[str, Any]]:
        data = _get_json(_STANDINGS)
        if not isinstance(data, dict):
            return {}
        rows: list[dict[str, Any]] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                if "team" in node and "stats" in node:
                    abbr = _norm_team((node["team"] or {}).get("abbreviation"))
                    stats = {s.get("name"): s.get("value") for s in node["stats"]
                             if isinstance(s, dict)}
                    pa = stats.get("pointsAgainst")
                    pf = stats.get("pointsFor")
                    wins = int(stats.get("wins") or 0)
                    losses = int(stats.get("losses") or 0)
                    ties = int(stats.get("ties") or 0)
                    games = (wins + losses + ties) or 1
                    if abbr and isinstance(pa, (int, float)):
                        rows.append({
                            "abbr": abbr,
                            "pa_pg": round(float(pa) / games, 1),
                            "pf_pg": round(float(pf) / games, 1) if isinstance(pf, (int, float)) else None,
                            "record": f"{wins}-{losses}" + (f"-{ties}" if ties else ""),
                            "games": games,
                        })
                    return
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(data)
        out: dict[str, dict[str, Any]] = {}
        for i, r in enumerate(sorted(rows, key=lambda r: r["pa_pg"])):
            out[r["abbr"]] = {"def_rank": i + 1, "pa_pg": r["pa_pg"], "pf_pg": r["pf_pg"],
                              "record": r["record"], "games": r["games"], "off_rank": None}
        scored = [r for r in rows if r["pf_pg"] is not None]
        for i, r in enumerate(sorted(scored, key=lambda r: -r["pf_pg"])):
            out[r["abbr"]]["off_rank"] = i + 1
        return out

    return _cached("team_strength", config.EXTERNAL_TTL_GAME, fetch) or {}


def defense_rankings() -> dict[str, dict[str, Any]]:
    """Map team abbrev -> {rank, avg_points_against, games}. Empty if unavailable.

    Rank 1 = fewest points allowed per game (toughest matchup); 32 = softest.
    (A view over team_strength kept for existing callers.)
    """
    return {
        abbr: {"rank": v["def_rank"], "avg_points_against": v["pa_pg"], "games": v["games"]}
        for abbr, v in team_strength().items()
    }


# --- NFL team situation: coach, roster room, depth chart, schedule ---------


def team_directory() -> dict[str, dict[str, str]]:
    """abbrev -> {id, name, location, display} for all 32 NFL teams."""

    def fetch() -> dict[str, dict[str, str]]:
        data = _get_json(_TEAMS)
        out: dict[str, dict[str, str]] = {}
        try:
            for entry in data["sports"][0]["leagues"][0]["teams"]:
                t = entry.get("team") or {}
                if t.get("id") and t.get("abbreviation"):
                    out[_norm_team(t["abbreviation"])] = {
                        "id": str(t["id"]),
                        "name": str(t.get("name") or ""),
                        "location": str(t.get("location") or ""),
                        "display": str(t.get("displayName") or ""),
                    }
        except (KeyError, IndexError, TypeError):
            return {}
        return out

    return _cached("team_directory", config.EXTERNAL_TTL_SLEEPER, fetch) or {}


def resolve_nfl_team(query: Any) -> str:
    """'Panthers' / 'carolina' / 'CAR' -> 'CAR' ('' if no unique match)."""
    q = str(query or "").strip().lower()
    if not q:
        return ""
    directory = team_directory()
    abbr = _norm_team(q)
    if abbr in directory or (not directory and len(abbr) <= 3):
        return abbr
    hits = [a for a, t in directory.items()
            if q in (t["name"].lower(), t["location"].lower(), t["display"].lower())]
    if not hits:
        hits = [a for a, t in directory.items() if q in t["display"].lower()]
    return hits[0] if len(hits) == 1 else ""


def _team_ids() -> dict[str, str]:
    """abbrev -> ESPN numeric team id (needed by the core depth-chart API)."""
    return {a: t["id"] for a, t in team_directory().items()}


def nfl_roster(abbr: Any) -> dict[str, Any]:
    """The real NFL roster for a team: head coach + every player's status.

    Returns {coach, players: [{espn_id, name, position, age, exp, status, group,
    injury_status, injury_type, injury_date}]} or {} if unavailable. ``group`` is
    ESPN's bucket: offense / defense / specialTeam / injuredReserveOrOut /
    suspended / practiceSquad — the IR bucket is where a starter's injured
    backup sits until he's activated.
    """
    team = _norm_team(abbr)
    if not team:
        return {}

    def fetch() -> dict[str, Any]:
        data = _get_json(_TEAM_ROSTER.format(abbr=team.lower()))
        if not isinstance(data, dict):
            return {}
        coach = ""
        for c in data.get("coach") or []:
            if isinstance(c, dict):
                coach = f"{c.get('firstName', '')} {c.get('lastName', '')}".strip()
                break
        players: list[dict[str, Any]] = []
        for grp in data.get("athletes") or []:
            gname = grp.get("position") if isinstance(grp, dict) else None
            for a in (grp.get("items") or []) if isinstance(grp, dict) else []:
                if not isinstance(a, dict):
                    continue
                inj = (a.get("injuries") or [{}])[0] if a.get("injuries") else {}
                details = inj.get("details") or {} if isinstance(inj, dict) else {}
                players.append({
                    "espn_id": str(a.get("id") or ""),
                    "name": a.get("fullName") or a.get("displayName") or "?",
                    "position": ((a.get("position") or {}).get("abbreviation") or "").upper(),
                    "age": a.get("age"),
                    "exp": (a.get("experience") or {}).get("years"),
                    "status": (a.get("status") or {}).get("name") or "",
                    "group": gname or "",
                    "injury_status": inj.get("status") if isinstance(inj, dict) else None,
                    "injury_type": details.get("type") if isinstance(details, dict) else None,
                    "injury_date": (inj.get("date") if isinstance(inj, dict) else None),
                    "return_date": details.get("returnDate") if isinstance(details, dict) else None,
                })
        return {"coach": coach, "players": players}

    return _cached(f"roster:{team}", config.EXTERNAL_TTL_TREND, fetch) or {}


def depth_chart(abbr: Any, season: int) -> dict[str, list[str]]:
    """Official offensive depth chart: {'QB': [espn_id, ...] in rank order, 'RB': ...}.

    Picks the chart that carries the skill positions (ESPN names it by formation,
    e.g. '3WR 1TE'). Empty if unavailable.
    """
    team = _norm_team(abbr)
    tid = _team_ids().get(team)
    if not tid:
        return {}

    def fetch() -> dict[str, list[str]]:
        data = _get_json(_DEPTH_CHART.format(season=season, team_id=tid))
        if not isinstance(data, dict):
            return {}
        best: dict[str, list[str]] = {}
        for chart in data.get("items") or []:
            positions = chart.get("positions") if isinstance(chart, dict) else None
            if not isinstance(positions, dict) or "qb" not in positions:
                continue
            out: dict[str, list[str]] = {}
            for key, block in positions.items():
                pos = key.upper()
                if pos not in ("QB", "RB", "WR", "TE", "FB"):
                    continue
                ranked: list[tuple[int, str]] = []
                for a in (block.get("athletes") or []) if isinstance(block, dict) else []:
                    ref = ((a.get("athlete") or {}).get("$ref") or "") if isinstance(a, dict) else ""
                    if "/athletes/" in ref:
                        pid = ref.split("/athletes/")[1].split("?")[0]
                        ranked.append((int(a.get("rank") or 99), pid))
                out[pos] = [pid for _, pid in sorted(ranked)]
            if len(out) > len(best):
                best = out
        return best

    return _cached(f"depth:{team}:{season}", config.EXTERNAL_TTL_SLEEPER, fetch) or {}


def team_schedule(abbr: Any) -> dict[str, Any]:
    """{bye_week, games: [{week, opponent, home, date, result}]} for an NFL team."""
    team = _norm_team(abbr)
    if not team:
        return {}

    def fetch() -> dict[str, Any]:
        data = _get_json(_TEAM_SCHEDULE.format(abbr=team.lower()))
        if not isinstance(data, dict):
            return {}
        games: list[dict[str, Any]] = []
        for ev in data.get("events") or []:
            comp = (ev.get("competitions") or [{}])[0]
            opp, home, result = "", None, ""
            for c in comp.get("competitors") or []:
                cab = _norm_team((c.get("team") or {}).get("abbreviation"))
                if cab == team:
                    home = c.get("homeAway") == "home"
                    if c.get("winner") is True:
                        result = "W"
                    elif c.get("winner") is False and (comp.get("status") or {}).get("type", {}).get("completed"):
                        result = "L"
                else:
                    opp = cab
            games.append({
                "week": (ev.get("week") or {}).get("number"),
                "opponent": opp, "home": home, "date": ev.get("date"), "result": result,
            })
        bye = data.get("byeWeek")
        return {"bye_week": bye if isinstance(bye, int) else None, "games": games}

    return _cached(f"sched:{team}", config.EXTERNAL_TTL_SLEEPER, fetch) or {}


# --- News: ESPN fantasy player feed (Rotowire blurbs) + athlete overview ----


def player_news(espn_player_id: Any, limit: int = 8) -> list[dict[str, Any]]:
    """Dated news blurbs for a player, newest first: [{published, type, headline, body}]."""
    if espn_player_id in (None, ""):
        return []
    pid = str(espn_player_id)

    def fetch() -> list[dict[str, Any]]:
        data = _get_json(_PLAYER_NEWS.format(pid=pid, limit=limit))
        if not isinstance(data, dict):
            return []
        out: list[dict[str, Any]] = []
        for item in data.get("feed") or []:
            if not isinstance(item, dict):
                continue
            out.append({
                "published": item.get("published") or item.get("lastModified") or "",
                "type": item.get("type") or "",
                "headline": (item.get("headline") or item.get("title") or "").strip(),
                "body": (item.get("description") or "").strip(),
                "premium": bool(item.get("premium")),
            })
        out.sort(key=lambda x: x["published"], reverse=True)
        return out

    return _cached(f"news:{pid}:{limit}", 900, fetch) or []


def player_overview(espn_player_id: Any) -> dict[str, Any]:
    """ESPN's current Rotowire story + fantasy outlook for a player (may be {})."""
    if espn_player_id in (None, ""):
        return {}
    pid = str(espn_player_id)

    def fetch() -> dict[str, Any]:
        data = _get_json(_PLAYER_OVERVIEW.format(pid=pid))
        if not isinstance(data, dict):
            return {}
        roto = data.get("rotowire") or {}
        fant = data.get("fantasy") or {}
        return {
            "headline": (roto.get("headline") or "").strip(),
            "story": (roto.get("story") or "").strip(),
            "published": roto.get("published") or roto.get("lastModified") or "",
            "pos_rank": fant.get("positionRank"),
            "pct_owned": fant.get("percentOwned"),
            "outlook": (fant.get("projection") or "").strip(),
        }

    return _cached(f"overview:{pid}", 900, fetch) or {}


# --- Sleeper: role, health, market ---------------------------------------


def norm_name(name: Any) -> str:
    """Normalize a player name for cross-source matching ('A.J. Brown Jr.' -> 'aj brown')."""
    n = str(name or "").lower()
    n = n.replace(".", "").replace("'", "").replace("-", " ")
    parts = [w for w in n.split() if w not in ("jr", "sr", "ii", "iii", "iv", "v")]
    return " ".join(parts)


def _sleeper_players() -> dict[str, Any]:
    """Sleeper player map indexed three ways: {'by_espn', 'by_name_team', 'by_sid'}.

    Only ~1 in 4 active skill players carries an espn_id in Sleeper's file, so
    the name+team index is the join that usually works. Cached for hours.
    """

    def fetch() -> dict[str, Any]:
        data = _get_json(_SLEEPER_PLAYERS)
        if not isinstance(data, dict):
            return {}
        by_espn: dict[str, dict[str, Any]] = {}
        by_name_team: dict[tuple[str, str], dict[str, Any]] = {}
        by_sid: dict[str, dict[str, Any]] = {}
        for sleeper_id, p in data.items():
            if not isinstance(p, dict) or not p.get("position"):
                continue
            team = _norm_team(p.get("team"))
            info = {
                "sleeper_id": str(sleeper_id),
                "name": p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip(),
                "depth_chart_order": p.get("depth_chart_order"),
                "depth_chart_position": p.get("depth_chart_position"),
                "injury_status": p.get("injury_status"),
                "injury_body_part": p.get("injury_body_part"),
                "injury_notes": p.get("injury_notes"),
                "injury_start_date": p.get("injury_start_date"),
                "practice_participation": p.get("practice_participation"),
                "practice_description": p.get("practice_description"),
                "status": p.get("status"),  # Active / Inactive / Injured Reserve ...
                "news_updated": p.get("news_updated"),  # epoch ms
                "team": team,
                "position": p.get("position"),
                "age": p.get("age"),
                "years_exp": p.get("years_exp"),
            }
            by_sid[str(sleeper_id)] = info
            espn_id = p.get("espn_id")
            if espn_id not in (None, ""):
                by_espn[str(espn_id)] = info
            if team and info["name"]:
                by_name_team[(norm_name(info["name"]), team)] = info
        return {"by_espn": by_espn, "by_name_team": by_name_team, "by_sid": by_sid}

    return _cached("sleeper_players", config.EXTERNAL_TTL_SLEEPER, fetch) or {}


def _sleeper_trending() -> dict[str, dict[str, int]]:
    """{'add': {sleeper_id: count}, 'drop': {sleeper_id: count}}."""

    def fetch() -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {"add": {}, "drop": {}}
        for kind in ("add", "drop"):
            data = _get_json(_SLEEPER_TREND.format(kind=kind))
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and "player_id" in item:
                        out[kind][str(item["player_id"])] = int(item.get("count", 0))
        return out

    return _cached("sleeper_trending", config.EXTERNAL_TTL_TREND, fetch) or {"add": {}, "drop": {}}


def _sleeper_lookup(espn_player_id: Any, name: Any = None, team: Any = None) -> dict[str, Any]:
    idx = _sleeper_players()
    if not idx:
        return {}
    info = idx["by_espn"].get(str(espn_player_id)) if espn_player_id not in (None, "") else None
    if not info and name:
        key = (norm_name(name), _norm_team(team))
        info = idx["by_name_team"].get(key)
        if not info:
            # Team mismatch (just traded/signed): fall back to a unique name match.
            hits = [v for (n, _), v in idx["by_name_team"].items() if n == key[0]]
            info = hits[0] if len(hits) == 1 else None
    return info or {}


def sleeper_info(espn_player_id: Any, name: Any = None, team: Any = None) -> dict[str, Any]:
    """Role/health/market for one player, joined by ESPN id or (name, NFL team).

    Returns {} if the player can't be matched or external data is off.
    """
    info = _sleeper_lookup(espn_player_id, name, team)
    if not info:
        return {}
    trending = _sleeper_trending()
    sid = info.get("sleeper_id")
    out = dict(info)
    out["trending_add"] = trending["add"].get(sid)
    out["trending_drop"] = trending["drop"].get(sid)
    return out


def position_room(team: Any, position: str) -> list[dict[str, Any]]:
    """Every Sleeper-listed player on an NFL team at a position, by depth order.

    This is how a 'his backup is coming back' situation surfaces: the room
    shows each teammate's depth slot, injury status/notes, and when his news
    last moved.
    """
    idx = _sleeper_players()
    if not idx:
        return []
    t = _norm_team(team)
    pos = str(position or "").upper()
    room = [v for v in idx["by_sid"].values()
            if v.get("team") == t and str(v.get("position") or "").upper() == pos]
    room.sort(key=lambda v: (v.get("depth_chart_order") is None,
                             v.get("depth_chart_order") or 99))
    trending = _sleeper_trending()
    out = []
    for v in room:
        item = dict(v)
        item["trending_add"] = trending["add"].get(v["sleeper_id"])
        out.append(item)
    return out


# --- Sleeper weekly stats: snap share, touch share, red-zone usage ----------


def _sleeper_week_stats(season: int, week: int, ttl: float) -> dict[str, dict[str, Any]]:
    def fetch() -> dict[str, dict[str, Any]]:
        data = _get_json(_SLEEPER_STATS.format(season=season, week=week))
        return data if isinstance(data, dict) else {}

    return _cached(f"sleeper_stats:{season}:{week}", ttl, fetch) or {}


def usage_trend(espn_player_id: Any, name: Any, team: Any, season: int,
                through_week: int, lookback: int = 4) -> list[dict[str, Any]]:
    """Per-week role metrics for the last ``lookback`` played weeks, oldest first.

    Each row: {week, snap_pct, snaps, carries, carry_share, targets,
    target_share, rz_touches, pts_ppr}. Shares are vs the player's team's
    totals for that week (summed over the team's players). Empty if unmatched.
    """
    info = _sleeper_lookup(espn_player_id, name, team)
    sid = info.get("sleeper_id")
    if not sid:
        return []
    idx = _sleeper_players()
    by_sid = idx.get("by_sid", {})
    rows: list[dict[str, Any]] = []
    for wk in range(max(1, through_week - lookback + 1), through_week + 1):
        # Finished weeks never change; the current week may still be in progress.
        ttl = config.EXTERNAL_TTL_SLEEPER if wk < through_week else config.EXTERNAL_TTL_GAME
        stats = _sleeper_week_stats(season, wk, ttl)
        st = stats.get(sid)
        if not st or not st.get("gp"):
            continue
        pteam = (by_sid.get(sid) or {}).get("team")
        team_rush = team_tgt = 0.0
        for other_sid, ost in stats.items():
            if (by_sid.get(other_sid) or {}).get("team") == pteam:
                team_rush += float(ost.get("rush_att") or 0)
                team_tgt += float(ost.get("rec_tgt") or 0)
        snaps = float(st.get("off_snp") or 0)
        tm_snaps = float(st.get("tm_off_snp") or 0)
        carries = float(st.get("rush_att") or 0)
        targets = float(st.get("rec_tgt") or 0)
        rows.append({
            "week": wk,
            "snaps": int(snaps),
            "snap_pct": round(snaps / tm_snaps * 100) if tm_snaps else None,
            "carries": int(carries),
            "carry_share": round(carries / team_rush * 100) if team_rush and carries else None,
            "targets": int(targets),
            "target_share": round(targets / team_tgt * 100) if team_tgt and targets else None,
            "rz_touches": int((st.get("rush_rz_att") or 0) + (st.get("rec_rz_tgt") or 0)),
            "pts_ppr": st.get("pts_ppr"),
        })
    return rows
