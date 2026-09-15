"""Waiver and transaction tools: waiver order, FAAB budgets, transaction log.

These tools cover waiver priority, FAAB budgets, the processing schedule, and
completed transactions. A user's *own* pending (unprocessed) claims are handled
separately in ``pending.py`` (get_pending_claims / cancel_pending_claim) via the
mTransactions2 view — that view does expose them, contrary to a common belief
that ESPN hides pending claims from the API.
"""

from __future__ import annotations

from typing import Any, Optional

from ..app import mcp
from ..client import freshness_line, get_league
from ..formatting import fmt_ts, owner_name

# Human-friendly action labels emitted by espn_api Activity objects.
_ADD_ACTIONS = {"FA ADDED", "WAIVER ADDED"}
_TRADE_ACTIONS = {"TRADE_SENT", "TRADE_RECEIVED", "TRADED"}
_KIND_FILTERS = {
    "add": _ADD_ACTIONS,
    "waiver": {"WAIVER ADDED"},
    "drop": {"DROPPED"},
    "trade": _TRADE_ACTIONS,
}


def _raw_acquisition_settings(league: Any) -> dict[str, Any]:
    """Best-effort raw acquisitionSettings (waiver schedule lives here).

    espn_api parses only ``faab`` and ``acquisition_budget`` from this block, so
    we re-request the mSettings view to read the processing day/time. Returns an
    empty dict on any failure — callers must treat every key as optional.
    """
    try:
        data = league.espn_request.league_get(params={"view": "mSettings"})
        return (data or {}).get("settings", {}).get("acquisitionSettings", {}) or {}
    except Exception:  # noqa: BLE001 - schedule is a nice-to-have, never fatal
        return {}


def _waiver_schedule_lines(league: Any) -> list[str]:
    """Format the waiver processing schedule from raw settings, if available."""
    acq = _raw_acquisition_settings(league)
    if not acq:
        return []
    lines: list[str] = []

    acq_type = acq.get("acquisitionType")
    if acq_type:
        pretty = str(acq_type).replace("WAIVERS_", "").replace("_", " ").title()
        lines.append(f"- Waiver type: {pretty}")

    days = acq.get("waiverProcessDays")
    if isinstance(days, list) and days:
        lines.append(f"- Processes on: {', '.join(str(d).title() for d in days)}")

    hour = acq.get("waiverProcessHour")
    if isinstance(hour, int):
        ampm = "AM" if hour < 12 else "PM"
        h12 = hour % 12 or 12
        lines.append(f"- Process hour: ~{h12}:00 {ampm} (league timezone)")

    waiver_hours = acq.get("waiverHours")
    if isinstance(waiver_hours, (int, float)) and waiver_hours:
        lines.append(f"- Claim/waiver period: {int(waiver_hours)} hours after a drop")

    return lines


@mcp.tool()
def get_waiver_order() -> str:
    """Show waiver priority (rolling leagues) or FAAB budgets, plus the schedule.

    For a standard/rolling-waiver league this lists every team's waiver priority
    (1 = next in line to win a contested claim). For a FAAB league it lists each
    team's remaining and spent budget instead. It also reports the waiver
    processing schedule when ESPN exposes it.

    Note: to see the players you've actually claimed (your pending claims), use
    get_pending_claims. This tool only reports priority/budgets and the schedule.
    """
    try:
        league = get_league()
    except RuntimeError as exc:
        return f"Error: {exc}"

    s = league.settings
    is_faab = bool(getattr(s, "faab", False))
    lines = [f"# Waiver {'budgets (FAAB)' if is_faab else 'priority'} — {s.name}", ""]

    if is_faab:
        budget = getattr(s, "acquisition_budget", 0) or 0
        header = f"{'Team':<28}{'Spent':>8}{'Remaining':>11}"
        lines += [f"Season FAAB budget: ${budget}", "", header, "-" * len(header)]
        # Highest remaining budget first (most spending power on waivers).
        teams = sorted(
            league.teams,
            key=lambda t: (getattr(t, "acquisition_budget_spent", 0) or 0),
        )
        for t in teams:
            spent = getattr(t, "acquisition_budget_spent", 0) or 0
            remaining = budget - spent
            lines.append(
                f"{t.team_name[:27]:<28}{f'${spent}':>8}{f'${remaining}':>11}"
            )
    else:
        header = f"{'#':<4}{'Team':<28}Owner"
        lines += [header, "-" * len(header)]
        # waiver_rank of 0 means "not reported"; sort those to the bottom.
        teams = sorted(
            league.teams,
            key=lambda t: (getattr(t, "waiver_rank", 0) or 999),
        )
        for t in teams:
            rank = getattr(t, "waiver_rank", 0) or 0
            rank_str = str(rank) if rank else "-"
            lines.append(f"{rank_str:<4}{t.team_name[:27]:<28}{owner_name(t)}")
        lines += [
            "",
            "Lower number = higher priority. After a team wins a claim it usually "
            "drops to the bottom of the order.",
        ]

    schedule = _waiver_schedule_lines(league)
    if schedule:
        lines += ["", "## Processing schedule"] + schedule

    lines += [
        "",
        "_To see the claims you've submitted, use get_pending_claims. Once a "
        "claim processes it also appears in get_recent_transactions._",
    ]
    fresh = freshness_line()
    if fresh:
        lines += ["", fresh]
    return "\n".join(lines)


@mcp.tool()
def get_recent_transactions(limit: int = 25, kind: Optional[str] = None) -> str:
    """List recent completed transactions: adds, drops, waiver claims, and trades.

    Args:
        limit: Maximum number of activity entries to scan (default 25).
        kind: Optional filter — "add", "drop", "waiver", or "trade". Omit for all.

    Use this to confirm whether a waiver claim or add/drop actually went through
    (pending claims won't appear here until they process). Waiver claims in a
    FAAB league show the winning bid amount.
    """
    try:
        league = get_league()
    except RuntimeError as exc:
        return f"Error: {exc}"

    wanted = None
    if kind:
        wanted = _KIND_FILTERS.get(kind.lower().strip())
        if wanted is None:
            return (
                f"Unknown kind '{kind}'. Use one of: add, drop, waiver, trade "
                "(or omit for all)."
            )

    try:
        activity = league.recent_activity(size=max(limit, 1))
    except Exception as exc:  # noqa: BLE001
        return f"Could not load recent transactions: {exc}"

    def describe(action_tuple: Any) -> Optional[str]:
        # espn_api actions are (team, action, player, bid_amount); older
        # versions omit the bid, so read positionally and tolerate short tuples.
        team = action_tuple[0] if len(action_tuple) > 0 else None
        action = str(action_tuple[1]) if len(action_tuple) > 1 else "UNKNOWN"
        player = action_tuple[2] if len(action_tuple) > 2 else None
        bid = action_tuple[3] if len(action_tuple) > 3 else 0

        if wanted is not None and action not in wanted:
            return None

        team_name = getattr(team, "team_name", None) or "Unknown team"
        player_name = getattr(player, "name", None) or (
            str(player) if player is not None else "?"
        )

        if action == "WAIVER ADDED":
            bid_str = f" (${bid} FAAB)" if isinstance(bid, (int, float)) and bid else ""
            return f"  {team_name} claimed {player_name} off waivers{bid_str}"
        if action == "FA ADDED":
            return f"  {team_name} added {player_name} (free agent)"
        if action == "DROPPED":
            return f"  {team_name} dropped {player_name}"
        if action == "TRADE_RECEIVED":
            return f"  {team_name} received {player_name} (trade)"
        if action == "TRADE_SENT":
            return f"  {team_name} traded away {player_name}"
        return f"  {team_name}: {action.title()} {player_name}"

    lines = [f"# Recent transactions — {league.settings.name}", ""]
    found = False
    for act in activity or []:
        rows = [d for d in (describe(a) for a in getattr(act, "actions", []) or []) if d]
        if not rows:
            continue
        found = True
        lines.append(f"## {fmt_ts(getattr(act, 'date', None))}")
        lines += rows
        lines.append("")

    if not found:
        which = f" of type '{kind}'" if kind else ""
        return f"No recent transactions{which} found in the league's activity feed."

    fresh = freshness_line()
    if fresh:
        lines.append(fresh)
    return "\n".join(lines).rstrip()
