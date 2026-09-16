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
  map carries each player's ESPN id, so we can join it to ESPN rosters and read
  depth-chart order (is this the WR1 or the WR3?), granular injury/practice
  status, and — via the trending endpoints — how hard the fantasy market is
  adding or dropping the player right now.

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
            out[home] = {
                "opponent": away, "home": True, "kickoff": kickoff,
                "over_under": odds["over_under"], "spread": odds["spread"],
                "implied_total": implied.get(home), "opp_implied_total": implied.get(away),
                "has_odds": has_odds,
            }
            out[away] = {
                "opponent": home, "home": False, "kickoff": kickoff,
                "over_under": odds["over_under"], "spread": odds["spread"],
                "implied_total": implied.get(away), "opp_implied_total": implied.get(home),
                "has_odds": has_odds,
            }
        return out

    result = _cached(key, config.EXTERNAL_TTL_GAME, fetch)
    return result or {}


# --- opponent defense strength (season points allowed, ranked) ------------


def defense_rankings() -> dict[str, dict[str, Any]]:
    """Map team abbrev -> {rank, avg_points_against, games}. Empty if unavailable.

    Rank 1 = fewest points allowed per game (toughest matchup); 32 = softest.
    """

    def fetch() -> dict[str, dict[str, Any]]:
        data = _get_json(_STANDINGS)
        if not isinstance(data, dict):
            return {}
        rows: list[tuple[str, float, float]] = []  # (abbr, avg_pa, games)

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                if "team" in node and "stats" in node:
                    abbr = _norm_team((node["team"] or {}).get("abbreviation"))
                    stats = {s.get("name"): s.get("value") for s in node["stats"]
                             if isinstance(s, dict)}
                    pa = stats.get("pointsAgainst")
                    avg = stats.get("avgPointsAgainst")
                    wins = stats.get("wins") or 0
                    losses = stats.get("losses") or 0
                    ties = stats.get("ties") or 0
                    games = (wins + losses + ties) or 0
                    if abbr and isinstance(pa, (int, float)):
                        per = avg if isinstance(avg, (int, float)) and avg else (
                            pa / games if games else pa)
                        rows.append((abbr, float(per), float(games)))
                    return
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(data)
        rows.sort(key=lambda r: r[1])  # fewest points allowed first
        return {
            abbr: {"rank": i + 1, "avg_points_against": round(per, 1), "games": int(g)}
            for i, (abbr, per, g) in enumerate(rows)
        }

    return _cached("defense", config.EXTERNAL_TTL_GAME, fetch) or {}


# --- Sleeper: role, health, market ---------------------------------------


def _sleeper_players() -> dict[str, dict[str, Any]]:
    """espn_id (str) -> selected Sleeper fields. Large file; cached for hours."""

    def fetch() -> dict[str, dict[str, Any]]:
        data = _get_json(_SLEEPER_PLAYERS)
        if not isinstance(data, dict):
            return {}
        by_espn: dict[str, dict[str, Any]] = {}
        for sleeper_id, p in data.items():
            if not isinstance(p, dict):
                continue
            espn_id = p.get("espn_id")
            if espn_id in (None, ""):
                continue
            by_espn[str(espn_id)] = {
                "sleeper_id": sleeper_id,
                "depth_chart_order": p.get("depth_chart_order"),
                "depth_chart_position": p.get("depth_chart_position"),
                "injury_status": p.get("injury_status"),
                "injury_body_part": p.get("injury_body_part"),
                "practice_participation": p.get("practice_participation"),
                "team": _norm_team(p.get("team")),
                "position": p.get("position"),
                "years_exp": p.get("years_exp"),
            }
        return by_espn

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


def sleeper_info(espn_player_id: Any) -> dict[str, Any]:
    """Role/health/market for one ESPN player id, joined via Sleeper's espn_id.

    Returns {} if the player can't be matched or external data is off.
    """
    if espn_player_id in (None, ""):
        return {}
    players = _sleeper_players()
    info = players.get(str(espn_player_id))
    if not info:
        return {}
    trending = _sleeper_trending()
    sid = info.get("sleeper_id")
    out = dict(info)
    out["trending_add"] = trending["add"].get(sid)
    out["trending_drop"] = trending["drop"].get(sid)
    out.pop("sleeper_id", None)
    return out
