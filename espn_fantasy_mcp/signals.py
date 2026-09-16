"""Tier-0 signals: everything we can learn about a player *without* a network
call, squeezed out of the data ``espn_api`` already returns on each Player.

A single projection hides more than it shows. These helpers turn a player's
weekly history and ESPN stat breakdown into the dimensions that actually drive
start/sit and trade decisions:

- **form** — last-3-weeks scoring vs the season, so a heater or a slump is visible
- **volatility** — floor, ceiling, and boom/bust rate, so a steady 12-a-week
  player isn't confused with a 4-or-28 coin flip
- **usage** — targets, carries and receiving/rushing volume from the stat
  breakdown; opportunity is stickier and more predictive than past points
- **market** — rostered %, started %, and ESPN positional rank
- **projection sanity** — whether ESPN's number lines up with recent reality

All functions are pure and defensive: unknown/missing data yields ``None`` or an
empty result rather than raising.
"""

from __future__ import annotations

from statistics import pstdev
from typing import Any, Optional

# ESPN stat-breakdown keys we treat as "opportunity", grouped for a readable
# summary. Keys vary a little across espn_api versions, so we probe several.
_USAGE_KEYS = {
    "targets": ("receivingTargets", "targets"),
    "receptions": ("receivingReceptions", "receptions"),
    "rec_yards": ("receivingYards",),
    "carries": ("rushingAttempts", "carries"),
    "rush_yards": ("rushingYards",),
    "pass_attempts": ("passingAttempts",),
    "pass_yards": ("passingYards",),
    "red_zone_targets": ("receivingTargetsInsideRedZone", "targetsInsideRedZone"),
}


def weekly_scores(player: Any) -> list[tuple[int, float, Optional[float]]]:
    """Sorted [(week, actual_points, projected_points)] for weeks with a score.

    Week 0 (the season bucket) is excluded. Weeks the player hasn't played yet
    (no actual points recorded) are skipped so form/volatility reflect games
    that actually happened.
    """
    stats = getattr(player, "stats", None)
    if not isinstance(stats, dict):
        return []
    rows: list[tuple[int, float, Optional[float]]] = []
    for wk in sorted(k for k in stats if isinstance(k, int) and k > 0):
        entry = stats[wk]
        if not isinstance(entry, dict):
            continue
        pts = entry.get("points")
        if not isinstance(pts, (int, float)):
            continue
        proj = entry.get("projected_points")
        rows.append((wk, float(pts), float(proj) if isinstance(proj, (int, float)) else None))
    return rows


def recent_form(player: Any, lookback: int = 3) -> dict[str, Any]:
    """Recent scoring vs season baseline. Empty dict if too little history."""
    scores = weekly_scores(player)
    if len(scores) < 2:
        return {}
    pts = [s[1] for s in scores]
    season_avg = sum(pts) / len(pts)
    recent = pts[-lookback:]
    recent_avg = sum(recent) / len(recent)
    delta_pct = ((recent_avg - season_avg) / season_avg * 100) if season_avg else 0.0
    if delta_pct > 12:
        trend = "heating up"
    elif delta_pct < -12:
        trend = "cooling off"
    else:
        trend = "steady"
    return {
        "games": len(scores),
        "season_avg": round(season_avg, 1),
        "recent_avg": round(recent_avg, 1),
        "recent_weeks": len(recent),
        "delta_pct": round(delta_pct, 0),
        "trend": trend,
        "last_scores": [round(p, 1) for p in recent],
    }


def volatility(player: Any, boom: float = 18.0, bust: float = 6.0) -> dict[str, Any]:
    """Floor/ceiling and boom/bust profile from weekly scoring. Empty if < 3 games.

    boom/bust thresholds are generic per-game points; Claude can re-interpret
    them per position (a 6-point game is a bust for a WR1, fine for a TE).
    """
    scores = [s[1] for s in weekly_scores(player)]
    if len(scores) < 3:
        return {}
    ordered = sorted(scores)
    n = len(ordered)
    floor = ordered[max(0, n // 4 - 1)]  # ~25th percentile
    ceiling = ordered[min(n - 1, (3 * n) // 4)]  # ~75th percentile
    booms = sum(1 for p in scores if p >= boom)
    busts = sum(1 for p in scores if p <= bust)
    return {
        "stdev": round(pstdev(scores), 1),
        "floor": round(floor, 1),
        "ceiling": round(ceiling, 1),
        "min": round(min(scores), 1),
        "max": round(max(scores), 1),
        "boom_rate": round(booms / n * 100, 0),
        "bust_rate": round(busts / n * 100, 0),
        "boom_threshold": boom,
        "bust_threshold": bust,
    }


def _breakdown_for(player: Any, week: Optional[int]) -> dict[str, Any]:
    stats = getattr(player, "stats", None)
    if not isinstance(stats, dict):
        return {}
    # Prefer the specific week; else the season bucket (0); else any week. A
    # present-but-empty breakdown is skipped so it doesn't shadow richer data.
    for key in ([week, str(week)] if week is not None else []) + [0, "0"]:
        entry = stats.get(key)
        if isinstance(entry, dict) and entry.get("breakdown"):
            return entry["breakdown"]
    for entry in stats.values():
        if isinstance(entry, dict) and entry.get("breakdown"):
            return entry["breakdown"]
    return {}


def usage(player: Any, week: Optional[int] = None) -> dict[str, float]:
    """Opportunity metrics (targets, carries, volume) from the stat breakdown.

    Returns only the metrics ESPN actually reports for this player. Season-level
    breakdown is used when a specific week isn't available, so values may be
    season totals — the caller labels them accordingly.
    """
    bd = _breakdown_for(player, week)
    if not bd:
        return {}
    out: dict[str, float] = {}
    for label, keys in _USAGE_KEYS.items():
        for k in keys:
            v = bd.get(k)
            if isinstance(v, (int, float)) and v:
                out[label] = float(v)
                break
    return out


def market(player: Any) -> dict[str, Any]:
    """Ownership / usage-by-managers / positional rank signals."""
    out: dict[str, Any] = {}
    for src, dst in (("percent_owned", "owned_pct"), ("percent_started", "started_pct")):
        v = getattr(player, src, None)
        if isinstance(v, (int, float)):
            out[dst] = round(float(v), 1)
    for attr in ("posRank", "positionRank", "pos_rank"):
        v = getattr(player, attr, None)
        if isinstance(v, int) and v > 0:
            out["pos_rank"] = v
            break
    return out


def projection_sanity(player: Any, week_projection: float) -> dict[str, Any]:
    """Flag when ESPN's projection diverges sharply from recent scoring reality.

    Returns {} when there isn't enough history to judge. A large gap doesn't mean
    the projection is wrong (a matchup or role change may justify it) — it means
    "don't take this number at face value; look at why".
    """
    form = recent_form(player)
    if not form or not week_projection:
        return {}
    recent = form["recent_avg"]
    gap = week_projection - recent
    gap_pct = (gap / recent * 100) if recent else 0.0
    note = ""
    if gap_pct > 25:
        note = "projection is well ABOVE recent scoring — verify the optimism"
    elif gap_pct < -25:
        note = "projection is well BELOW recent scoring — possible buy-low / upside"
    return {
        "week_projection": round(week_projection, 1),
        "recent_avg": recent,
        "gap": round(gap, 1),
        "gap_pct": round(gap_pct, 0),
        "note": note,
    } if note else {}
