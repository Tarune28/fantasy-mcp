"""Pure helper functions shared across tools.

These take espn-api objects (League / Team / Player) and return primitives or
formatted strings. They contain the version-tolerant getattr logic that keeps
the tools resilient to espn-api changes.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any, Optional

from . import config
from .config import BENCH_SLOTS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from espn_api.football import League

__all__ = [
    "owner_name",
    "current_week",
    "week_is_complete",
    "upcoming_week",
    "latest_projection_week",
    "is_starter",
    "player_slot",
    "player_projected",
    "player_avg",
    "player_position",
    "injury_flag",
    "find_team",
    "my_team",
    "resolve_team",
    "team_choices",
    "scoring_kind",
    "roster_slot_counts",
    "roster_capacity",
    "fmt_ts",
    "league_position_averages",
    "eligible_slots",
]


def _norm(text: Any) -> str:
    """Lower-cased, stripped string for fuzzy comparison."""
    return str(text or "").strip().lower()


def owner_name(team: Any) -> str:
    """Return a readable owner name for a Team across espn-api versions."""
    # Newer espn-api: team.owners is a list of dicts.
    owners = getattr(team, "owners", None)
    if owners:
        names = []
        for o in owners:
            if isinstance(o, dict):
                first = o.get("firstName") or o.get("firstname") or ""
                last = o.get("lastName") or o.get("lastname") or ""
                full = f"{first} {last}".strip()
                names.append(full or o.get("id") or "Unknown")
            else:
                names.append(str(o))
        joined = ", ".join(n for n in names if n)
        if joined:
            return joined
    # Older espn-api: team.owner is a plain string.
    owner = getattr(team, "owner", None)
    if owner:
        return str(owner)
    return "Unknown owner"


def current_week(league: "League") -> int:
    """Best-effort current scoring week."""
    for attr in ("current_week", "nfl_week"):
        val = getattr(league, attr, None)
        if isinstance(val, int) and val > 0:
            return val
    return 1


def week_is_complete(league: "League", week: int) -> bool:
    """True if the given week's games are effectively finished.

    Uses each BoxPlayer's game_played (100 == game over). ESPN keeps the scoring
    period on the finished week until ~Tuesday, so this lets us tell "current
    week" from "current week is done, look ahead". A few stragglers that never
    reach 100 (Monday-night, suspended, or inactive players) are tolerated.
    """
    try:
        box = league.box_scores(week)
    except Exception:  # noqa: BLE001
        return False
    total = done = in_progress = 0
    for m in box:
        lineup = (getattr(m, "home_lineup", None) or []) + (
            getattr(m, "away_lineup", None) or []
        )
        for p in lineup:
            gp = getattr(p, "game_played", None)
            if gp is None:
                continue
            total += 1
            if gp >= 100:
                done += 1
            elif gp > 0:
                in_progress += 1
    if total == 0 or in_progress > 0:
        # No data, or at least one game is actively being played.
        return False
    return (done / total) >= 0.85


def upcoming_week(league: "League") -> int:
    """The week to make lineup decisions for.

    Normally the current scoring period, but if that week's games are all
    complete (and ESPN hasn't rolled the period over yet), the next week.
    """
    cur = current_week(league)
    final = getattr(league, "finalScoringPeriod", None) or getattr(
        league.settings, "reg_season_count", 18
    )
    if cur < final and week_is_complete(league, cur):
        return cur + 1
    return cur


def latest_projection_week(league: "League") -> int:
    """Highest week for which ESPN has posted per-week player projections."""
    best = 0
    try:
        for p in league.teams[0].roster:
            st = getattr(p, "stats", None)
            if isinstance(st, dict):
                for k in st:
                    if isinstance(k, int) and k > best:
                        # week 0 is the season bucket; skip it.
                        best = max(best, k)
    except Exception:  # noqa: BLE001
        pass
    return best


def is_starter(player: Any) -> bool:
    """True if the player occupies a starting lineup slot (not bench/IR)."""
    slot = getattr(player, "lineupSlot", None) or getattr(player, "slot_position", None)
    return bool(slot) and slot not in BENCH_SLOTS


def player_slot(player: Any) -> str:
    return (
        getattr(player, "lineupSlot", None)
        or getattr(player, "slot_position", None)
        or "-"
    )


def player_projected(player: Any, week: Optional[int] = None) -> float:
    """Best-effort projected points for a player.

    Prefers a specific week's projection, then season projected average, then
    projected total. Returns 0.0 if nothing is available.
    """
    # BoxPlayer objects carry a per-week projection directly.
    direct = getattr(player, "projected_points", None)
    if isinstance(direct, (int, float)) and direct:
        return float(direct)

    stats = getattr(player, "stats", None)
    if isinstance(stats, dict) and week is not None:
        wk = stats.get(week) or stats.get(str(week))
        if isinstance(wk, dict):
            proj = wk.get("projected_points")
            if isinstance(proj, (int, float)):
                return float(proj)

    for attr in ("projected_avg_points", "projected_total_points"):
        val = getattr(player, attr, None)
        if isinstance(val, (int, float)):
            return float(val)

    if isinstance(direct, (int, float)):
        return float(direct)
    return 0.0


def player_avg(player: Any) -> float:
    """Best-effort average points per game."""
    for attr in ("avg_points", "projected_avg_points"):
        val = getattr(player, attr, None)
        if isinstance(val, (int, float)) and val:
            return float(val)
    total = getattr(player, "total_points", None)
    if isinstance(total, (int, float)):
        return float(total)
    return 0.0


def player_position(player: Any) -> str:
    return getattr(player, "position", None) or "?"


def injury_flag(player: Any) -> str:
    status = getattr(player, "injuryStatus", None)
    if status and status not in ("ACTIVE", "NORMAL"):
        return str(status)
    if getattr(player, "injured", False):
        return "INJURED"
    return ""


def find_team(league: "League", query: str) -> Any:
    """Fuzzy-match a team by team name or owner name (case-insensitive substring).

    Returns the Team, or None if there is no match.
    """
    q = _norm(query)
    if not q:
        return None

    teams = league.teams
    # Exact team-name match wins first.
    for team in teams:
        if _norm(team.team_name) == q:
            return team
    # Then substring on team name.
    for team in teams:
        if q in _norm(team.team_name):
            return team
    # Then substring on owner name.
    for team in teams:
        if q in _norm(owner_name(team)):
            return team
    return None


def _norm_swid(value: Any) -> str:
    """Normalize a SWID/owner id for comparison (strip braces, spaces, case)."""
    return str(value or "").strip().strip("{}").strip().upper()


def my_team(league: "League") -> Any:
    """Resolve the configured 'my team', or None if it can't be determined.

    Resolution order:
    1. ESPN_TEAM_ID  — exact team id match
    2. ESPN_TEAM_NAME — fuzzy team/owner name match
    3. ESPN_SWID     — auto-detect: a private-league SWID cookie is the owner's
       member id, so match the team whose owners include it.
    """
    if config.TEAM_ID:
        try:
            wanted = int(config.TEAM_ID)
        except (TypeError, ValueError):
            wanted = None
        if wanted is not None:
            for team in league.teams:
                if getattr(team, "team_id", None) == wanted:
                    return team

    if config.TEAM_NAME:
        team = find_team(league, config.TEAM_NAME)
        if team is not None:
            return team

    if config.SWID:
        target = _norm_swid(config.SWID)
        if target:
            for team in league.teams:
                for owner in getattr(team, "owners", None) or []:
                    owner_id = owner.get("id") if isinstance(owner, dict) else owner
                    if _norm_swid(owner_id) == target:
                        return team

    return None


def resolve_team(league: "League", query: Optional[str]) -> tuple[Any, Optional[str]]:
    """Resolve a team argument, defaulting to 'my team' when none is given.

    Returns (team, None) on success, or (None, error_message) with guidance the
    caller can return directly.
    """
    if query and query.strip():
        team = find_team(league, query)
        if team is not None:
            return team, None
        return None, f"No team matched '{query}'.\n\n{team_choices(league)}"

    team = my_team(league)
    if team is not None:
        return team, None
    return None, (
        "I don't know which team is yours yet. Tell me your team or owner name, "
        "or set ESPN_TEAM_ID (or ESPN_TEAM_NAME) in the server config — for a "
        "private league with ESPN_SWID set I can usually detect it automatically."
        f"\n\n{team_choices(league)}"
    )


def team_choices(league: "League") -> str:
    """Formatted list of valid team names + owners for error messages."""
    lines = ["Valid teams:"]
    for team in league.teams:
        lines.append(f"  - {team.team_name} (owner: {owner_name(team)})")
    return "\n".join(lines)


def scoring_kind(league: "League") -> str:
    """Return 'PPR', 'Half-PPR', or 'Standard' based on reception scoring."""
    fmt = getattr(league.settings, "scoring_format", None)
    if not fmt:
        return "Unknown"
    for item in fmt:
        if not isinstance(item, dict):
            continue
        abbr = str(item.get("abbr") or item.get("abbrev") or "").upper()
        if abbr in ("REC", "RECEPTION"):
            pts = item.get("points", 0) or 0
            if pts >= 1:
                return "PPR"
            if pts >= 0.5:
                return "Half-PPR"
            return "Standard"
    return "Standard"


def roster_slot_counts(league: "League") -> dict[str, int]:
    """Best-effort mapping of lineup slot -> count."""
    for attr in ("position_slot_counts", "roster_positions", "roster"):
        val = getattr(league.settings, attr, None)
        if isinstance(val, dict) and val:
            return {k: v for k, v in val.items() if v}
    # Fall back to inferring from an actual roster's lineup slots.
    try:
        counts: dict[str, int] = {}
        for player in league.teams[0].roster:
            slot = player_slot(player)
            counts[slot] = counts.get(slot, 0) + 1
        return counts
    except Exception:  # noqa: BLE001
        return {}


_IR_SLOTS = {"IR", "Injured Reserve"}


def _settings_slot_counts(league: "League") -> dict[str, int]:
    """Authoritative slot->count from league settings, or {} if unavailable.

    Unlike ``roster_slot_counts``, this never falls back to inferring counts
    from a filled roster (which would report every slot as occupied and make it
    look like there are zero open spots). We only report roster capacity when we
    have real settings data to base it on.
    """
    for attr in ("position_slot_counts", "roster_positions"):
        val = getattr(league.settings, attr, None)
        if isinstance(val, dict) and val:
            return {k: v for k, v in val.items() if v}
    return {}


def roster_capacity(league: "League", team: Any) -> dict[str, Optional[int]]:
    """Compute roster occupancy so tools never have to guess if a team is full.

    Returns a dict with:
        used      - active (non-IR) players currently rostered
        total     - active roster capacity (starters + bench), or None if unknown
        open      - open active slots (total - used), or None if unknown
        ir_used   - players currently in IR slots
        ir_total  - IR slot capacity, or None if unknown

    ``total``/``open`` are None when league settings don't expose slot counts,
    so callers can say "capacity unknown" instead of reporting a wrong number.
    """
    slot_counts = _settings_slot_counts(league)
    total: Optional[int] = None
    ir_total: Optional[int] = None
    if slot_counts:
        active_total = 0
        for slot, count in slot_counts.items():
            if slot in _IR_SLOTS:
                ir_total = (ir_total or 0) + count
            else:
                active_total += count
        total = active_total

    roster = getattr(team, "roster", None) or []
    ir_used = sum(1 for p in roster if player_slot(p) in _IR_SLOTS)
    used = len(roster) - ir_used
    open_slots = max(total - used, 0) if total is not None else None

    return {
        "used": used,
        "total": total,
        "open": open_slots,
        "ir_used": ir_used,
        "ir_total": ir_total,
    }


def fmt_ts(ts: Any) -> str:
    """Format an ESPN epoch-ms timestamp as a date, if possible."""
    if not ts:
        return "Not set"
    try:
        seconds = float(ts) / 1000.0
        return datetime.datetime.fromtimestamp(seconds).strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return str(ts)


def league_position_averages(league: "League", week: int) -> dict[str, float]:
    """Average projected points of *starters* by position across the league."""
    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for team in league.teams:
        for player in getattr(team, "roster", []) or []:
            if not is_starter(player):
                continue
            pos = player_position(player)
            totals[pos] = totals.get(pos, 0.0) + player_projected(player, week)
            counts[pos] = counts.get(pos, 0) + 1
    return {pos: totals[pos] / counts[pos] for pos in totals if counts.get(pos)}


def eligible_slots(player: Any) -> set[str]:
    """Starting slots a player is eligible to fill (excludes bench/IR)."""
    slots = getattr(player, "eligibleSlots", None)
    if slots:
        return {s for s in slots if s not in BENCH_SLOTS}
    pos = player_position(player)
    out = {pos}
    if pos in {"RB", "WR", "TE"}:
        out |= {"FLEX", "RB/WR/TE"}
    return out
