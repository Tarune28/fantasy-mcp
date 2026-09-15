"""Write tools: add/drop, waiver claims, and lineup (start/sit) changes.

The read side of this server goes through ``espn-api``, which only ever issues
GET requests — it has no way to *change* anything. ESPN has no official write
API either, so these tools POST directly to ESPN's reverse-engineered
transactions endpoint on the ``lm-api-writes`` host, reusing the same
``espn_s2`` / ``SWID`` cookies the server already loads for private leagues.

Because these are real, mostly-irreversible actions (a dropped player can be
grabbed by someone else immediately), every tool here is **two-step**: called
without ``confirm=True`` it only *previews* the exact move and returns without
touching ESPN. It submits only when called again with ``confirm=True``.

Caveats worth knowing:
- The write endpoint is undocumented and can change or break without notice.
- Writes require ESPN_S2 and ESPN_SWID to be set, and those credentials must
  belong to an owner of the team being changed.
- ESPN still does not expose *pending* waiver claims, so a submitted claim can
  be queued here but won't show up in any read tool until it processes.
"""

from __future__ import annotations

from typing import Any, Optional

import requests

from ..app import mcp
from .. import config
from ..client import get_league, reset_league
from ..formatting import my_team, player_slot, roster_slot_counts

try:
    from espn_api.football.constant import POSITION_MAP
except Exception:  # noqa: BLE001 - fall back to a minimal map if layout changes
    POSITION_MAP = {}

# ESPN serves writes from a dedicated host, distinct from the read endpoint.
_WRITE_HOST = "https://lm-api-writes.fantasy.espn.com"

# Slots that are not starting spots (mirrors config.BENCH_SLOTS but as ids too).
_BENCH_SLOT_ID = 20
_IR_SLOT_ID = 21

# User-typed slot names -> ESPN lineup slot id. Built from POSITION_MAP plus a
# few friendly aliases so "bench", "flex", "dst" all resolve.
_SLOT_NAME_TO_ID = {
    "BENCH": _BENCH_SLOT_ID,
    "BE": _BENCH_SLOT_ID,
    "IR": _IR_SLOT_ID,
    "FLEX": 23,
    "RB/WR/TE": 23,
    "DST": 16,
    "DEF": 16,
    "D/ST": 16,
    "PK": 17,
}


def _slot_name_to_id(name: str) -> Optional[int]:
    """Resolve a user-typed slot name to an ESPN lineup slot id, or None."""
    key = str(name or "").strip().upper()
    if not key:
        return None
    if key in _SLOT_NAME_TO_ID:
        return _SLOT_NAME_TO_ID[key]
    # POSITION_MAP has string->id entries for the standard positions.
    val = POSITION_MAP.get(key)
    return val if isinstance(val, int) else None


def _slot_id_to_name(slot_id: int) -> str:
    if slot_id == _BENCH_SLOT_ID:
        return "Bench"
    name = POSITION_MAP.get(slot_id)
    return str(name) if name else str(slot_id)


def _norm(text: Any) -> str:
    return str(text or "").strip().lower()


def _write_ready() -> Optional[str]:
    """Return an error string if the server can't make writes, else None."""
    if not config.ESPN_S2 or not config.SWID:
        return (
            "Write actions require ESPN_S2 and ESPN_SWID to be set in the "
            "server config (they're the private-league cookies from a logged-in "
            "ESPN session). Read-only tools work without them, but roster moves "
            "do not. See the README for where to get these cookies."
        )
    if not config.LEAGUE_ID:
        return "ESPN_LEAGUE_ID is not set; cannot submit transactions."
    return None


def _cookies() -> dict[str, str]:
    return {"espn_s2": str(config.ESPN_S2), "SWID": str(config.SWID)}


def _post_transaction(league: Any, payload: dict[str, Any]) -> str:
    """POST a transaction payload to ESPN and return a human-readable result.

    Returns a string starting with 'OK' on success or 'Error' on failure so
    callers can branch, but it's already user-presentable either way.
    """
    year = getattr(league, "year", config.YEAR)
    url = (
        f"{_WRITE_HOST}/apis/v3/games/ffl/seasons/{year}"
        f"/segments/0/leagues/{config.LEAGUE_ID}/transactions/"
    )
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        # These identify the request as coming from ESPN's own fantasy client;
        # some write endpoints reject requests without them.
        "X-Fantasy-Source": "kona",
        "X-Fantasy-Platform": "kona-PROD",
    }
    try:
        r = requests.post(
            url, json=payload, headers=headers, cookies=_cookies(), timeout=20
        )
    except requests.RequestException as exc:
        return f"Error: could not reach ESPN's write endpoint: {exc}"

    if r.status_code in (200, 201):
        # A successful transaction invalidates our cached roster/free-agent data.
        reset_league()
        return "OK"

    # Surface ESPN's own error detail when it provides one.
    detail = ""
    try:
        body = r.json()
        detail = body.get("messages") or body.get("message") or body
    except Exception:  # noqa: BLE001
        detail = (r.text or "")[:400]

    if r.status_code in (401, 403):
        return (
            f"Error: ESPN rejected the request (HTTP {r.status_code}). The "
            "credentials are likely expired or don't own this team. Refresh "
            f"ESPN_S2/ESPN_SWID and confirm you own the team. Detail: {detail}"
        )
    return f"Error: ESPN returned HTTP {r.status_code}. Detail: {detail}"


def _scoring_period(league: Any) -> int:
    val = getattr(league, "scoringPeriodId", None) or getattr(
        league, "current_week", None
    )
    return int(val) if isinstance(val, int) and val > 0 else 1


def _find_on_roster(team: Any, name: str) -> tuple[Any, Optional[str]]:
    """Find a player on a team's roster by (fuzzy) name."""
    q = _norm(name)
    if not q:
        return None, "No player name given."
    roster = getattr(team, "roster", None) or []
    exact = [p for p in roster if _norm(getattr(p, "name", "")) == q]
    if exact:
        return exact[0], None
    subs = [p for p in roster if q in _norm(getattr(p, "name", ""))]
    if len(subs) == 1:
        return subs[0], None
    if len(subs) > 1:
        names = ", ".join(getattr(p, "name", "?") for p in subs)
        return None, f"'{name}' matches several players on the roster: {names}."
    have = ", ".join(getattr(p, "name", "?") for p in roster)
    return None, f"'{name}' isn't on the roster. Roster: {have}"


def _find_free_agent(league: Any, name: str) -> tuple[Any, Optional[str]]:
    """Find an addable player (free agent) by name.

    Searches the free-agent pool first (so we only add genuinely available
    players); falls back to player_info to at least resolve an id.
    """
    q = _norm(name)
    if not q:
        return None, "No player name given."
    try:
        agents = league.free_agents(size=300)
    except Exception:  # noqa: BLE001
        agents = []
    exact = [p for p in agents if _norm(getattr(p, "name", "")) == q]
    if exact:
        return exact[0], None
    subs = [p for p in agents if q in _norm(getattr(p, "name", ""))]
    if len(subs) == 1:
        return subs[0], None
    if len(subs) > 1:
        names = ", ".join(getattr(p, "name", "?") for p in subs[:12])
        return None, f"'{name}' matches several free agents: {names}. Be more specific."
    # Not in the FA pool — maybe rostered elsewhere, or a name typo.
    try:
        info = league.player_info(name=name)
        player = info[0] if isinstance(info, list) else info
    except Exception:  # noqa: BLE001
        player = None
    if player is not None and getattr(player, "playerId", None):
        return None, (
            f"'{getattr(player, 'name', name)}' exists but isn't a free agent "
            "right now (likely on another roster or on waivers). ESPN would "
            "reject an add."
        )
    return None, f"Couldn't find a free agent named '{name}'."


def _resolve_my_team(league: Any) -> tuple[Any, Optional[str]]:
    team = my_team(league)
    if team is None:
        return None, (
            "I can't tell which team is yours, so I won't guess for a write "
            "action. Set ESPN_TEAM_ID or ESPN_TEAM_NAME (or ESPN_SWID for a "
            "private league) in the server config."
        )
    return team, None


def _confirm_hint(tool: str) -> str:
    return (
        f"\n\n**Nothing has been submitted yet.** To go ahead, call "
        f"`{tool}` again with the same arguments plus `confirm=true`."
    )


@mcp.tool()
def add_drop_player(
    add_player: str,
    drop_player: str,
    confirm: bool = False,
) -> str:
    """Add a free agent to YOUR team, dropping a player to make room.

    This is an immediate free-agent transaction (not a waiver claim). The
    dropped player becomes available to the rest of the league right away, so
    this is effectively irreversible — review the preview carefully.

    Args:
        add_player: Name of the free agent to add.
        drop_player: Name of a player on your roster to drop.
        confirm: Must be True to actually submit. When False (default), this
            only previews the move and changes nothing.

    Use get_free_agents to find addable players and get_team_roster to see who
    you'd drop. To claim a contested player through waivers instead, use
    submit_waiver_claim.
    """
    err = _write_ready()
    if err:
        return f"Error: {err}"
    try:
        league = get_league()
    except RuntimeError as exc:
        return f"Error: {exc}"

    team, err = _resolve_my_team(league)
    if err:
        return f"Error: {err}"

    add, err = _find_free_agent(league, add_player)
    if err:
        return f"Error: {err}"
    drop, err = _find_on_roster(team, drop_player)
    if err:
        return f"Error: {err}"

    add_name = getattr(add, "name", add_player)
    drop_name = getattr(drop, "name", drop_player)
    header = (
        f"**Add/drop on {team.team_name}**\n"
        f"- ADD: {add_name} ({getattr(add, 'position', '?')})\n"
        f"- DROP: {drop_name} ({getattr(drop, 'position', '?')})"
    )

    if not confirm:
        return (
            f"Preview — {header}\n\nThe dropped player is released to the whole "
            "league immediately." + _confirm_hint("add_drop_player")
        )

    payload = {
        "isLeagueManager": False,
        "teamId": team.team_id,
        "memberId": str(config.SWID),
        "type": "FREEAGENT",
        "scoringPeriodId": _scoring_period(league),
        "executionType": "EXECUTE",
        "items": [
            {"playerId": add.playerId, "type": "ADD", "toTeamId": team.team_id},
            {"playerId": drop.playerId, "type": "DROP", "fromTeamId": team.team_id},
        ],
    }
    result = _post_transaction(league, payload)
    if result == "OK":
        return f"Done — {header}\n\n_Submitted to ESPN. It may take a moment to show up in the app._"
    return result


@mcp.tool()
def submit_waiver_claim(
    add_player: str,
    drop_player: str,
    bid: int = 0,
    confirm: bool = False,
) -> str:
    """Submit a WAIVER claim for a player, dropping someone if the claim wins.

    Unlike add_drop_player, this queues a claim to be processed at the league's
    next waiver run — it does not add the player immediately, and it can lose to
    a higher-priority team or bid. In a FAAB league, ``bid`` is your bid in
    dollars.

    Args:
        add_player: Name of the player to claim off waivers.
        drop_player: Name of a player on your roster to drop if the claim wins.
        bid: FAAB bid amount in dollars (ignored in a priority-waiver league).
        confirm: Must be True to actually submit. When False (default), this
            only previews the claim and changes nothing.

    Note: ESPN's API can't read back pending claims, so after submitting you
    won't be able to list it here — check the ESPN app to see or cancel it.
    Confirm it processed later with get_recent_transactions.
    """
    err = _write_ready()
    if err:
        return f"Error: {err}"
    try:
        league = get_league()
    except RuntimeError as exc:
        return f"Error: {exc}"

    team, err = _resolve_my_team(league)
    if err:
        return f"Error: {err}"

    # For a waiver, the target may be on waivers rather than a pure free agent;
    # resolve an id via free agents first, then player_info as a fallback.
    add, err = _find_free_agent(league, add_player)
    if err:
        # Waiver targets are often not in the FA pool; try to resolve an id.
        try:
            info = league.player_info(name=add_player)
            add = info[0] if isinstance(info, list) else info
        except Exception:  # noqa: BLE001
            add = None
        if add is None or not getattr(add, "playerId", None):
            return f"Error: {err}"

    drop, err = _find_on_roster(team, drop_player)
    if err:
        return f"Error: {err}"

    is_faab = bool(getattr(league.settings, "faab", False))
    add_name = getattr(add, "name", add_player)
    drop_name = getattr(drop, "name", drop_player)
    bid_line = f"\n- BID: ${bid} FAAB" if is_faab else ""
    header = (
        f"**Waiver claim for {team.team_name}**\n"
        f"- CLAIM: {add_name} ({getattr(add, 'position', '?')})\n"
        f"- DROP if won: {drop_name}{bid_line}"
    )

    if is_faab and bid < 0:
        return "Error: FAAB bid can't be negative."

    if not confirm:
        when = "at the next waiver run" if True else ""
        return (
            f"Preview — {header}\n\nThis is queued and processes {when}; it may "
            "not win." + _confirm_hint("submit_waiver_claim")
        )

    payload = {
        "isLeagueManager": False,
        "teamId": team.team_id,
        "memberId": str(config.SWID),
        "type": "WAIVER",
        "scoringPeriodId": _scoring_period(league),
        "executionType": "EXECUTE",
        "items": [
            {"playerId": add.playerId, "type": "ADD", "toTeamId": team.team_id},
            {"playerId": drop.playerId, "type": "DROP", "fromTeamId": team.team_id},
        ],
    }
    if is_faab:
        payload["bidAmount"] = int(bid)

    result = _post_transaction(league, payload)
    if result == "OK":
        return (
            f"Submitted — {header}\n\n_Queued as a waiver claim. It won't appear "
            "in any read tool until it processes; check the ESPN app to see or "
            "cancel it._"
        )
    return result


@mcp.tool()
def set_lineup(
    player: str,
    slot: str,
    swap_with: Optional[str] = None,
    confirm: bool = False,
) -> str:
    """Move a player on YOUR roster into a lineup slot (start or bench them).

    Args:
        player: Name of a player on your roster to move.
        slot: Target lineup slot — a starting spot like "QB", "RB", "WR", "TE",
            "FLEX", "K", "DST", or "bench" to sit them.
        swap_with: When starting a player into a slot that's already full, name
            the player to swap out (they take ``player``'s old slot). Required if
            the target starting slot has no open spot and you aren't benching.
        confirm: Must be True to actually submit. When False (default), this
            only previews the change and changes nothing.

    Lineup changes only take effect for players whose games haven't started.
    Use get_start_sit for advice, then this to apply it.
    """
    err = _write_ready()
    if err:
        return f"Error: {err}"
    try:
        league = get_league()
    except RuntimeError as exc:
        return f"Error: {exc}"

    team, err = _resolve_my_team(league)
    if err:
        return f"Error: {err}"

    target_id = _slot_name_to_id(slot)
    if target_id is None:
        return (
            f"Error: '{slot}' isn't a lineup slot I recognize. Try QB, RB, WR, "
            "TE, FLEX, K, DST, or bench."
        )

    p, err = _find_on_roster(team, player)
    if err:
        return f"Error: {err}"

    from_id = _slot_name_to_id(player_slot(p)) or _BENCH_SLOT_ID
    p_name = getattr(p, "name", player)

    if from_id == target_id:
        return f"{p_name} is already in {_slot_id_to_name(target_id)}. No change needed."

    items = [
        {
            "playerId": p.playerId,
            "type": "LINEUP",
            "fromLineupSlotId": from_id,
            "toLineupSlotId": target_id,
        }
    ]
    move_lines = [f"- {p_name}: {_slot_id_to_name(from_id)} → {_slot_id_to_name(target_id)}"]

    # Starting a player into a possibly-full slot: figure out if a swap is needed.
    benching = target_id == _BENCH_SLOT_ID
    if not benching:
        occupants = [
            r
            for r in (getattr(team, "roster", None) or [])
            if getattr(r, "playerId", None) != p.playerId
            and (_slot_name_to_id(player_slot(r)) == target_id)
        ]
        capacity = _slot_capacity(league, target_id)
        if len(occupants) >= capacity:
            # Need to free a spot. Use swap_with if given, else the first occupant.
            if swap_with:
                other, err = _find_on_roster(team, swap_with)
                if err:
                    return f"Error: {err}"
                if getattr(other, "playerId", None) == p.playerId:
                    return "Error: swap_with must be a different player."
            else:
                other = occupants[0]
            other_from = _slot_name_to_id(player_slot(other)) or target_id
            items.append(
                {
                    "playerId": other.playerId,
                    "type": "LINEUP",
                    "fromLineupSlotId": other_from,
                    "toLineupSlotId": from_id,
                }
            )
            move_lines.append(
                f"- {getattr(other, 'name', '?')}: "
                f"{_slot_id_to_name(other_from)} → {_slot_id_to_name(from_id)}"
            )

    header = f"**Lineup change on {team.team_name}**\n" + "\n".join(move_lines)

    if not confirm:
        return (
            f"Preview — {header}" + _confirm_hint("set_lineup")
        )

    payload = {
        "isLeagueManager": False,
        "teamId": team.team_id,
        "memberId": str(config.SWID),
        "type": "ROSTER",
        "scoringPeriodId": _scoring_period(league),
        "executionType": "EXECUTE",
        "items": items,
    }
    result = _post_transaction(league, payload)
    if result == "OK":
        return f"Done — {header}\n\n_Applied. Players whose games already started can't be moved._"
    return result


def _slot_capacity(league: Any, slot_id: int) -> int:
    """How many starting spots the league has for a given slot id (>=1)."""
    counts = roster_slot_counts(league)
    label = _slot_id_to_name(slot_id)
    # roster_slot_counts keys are slot labels (e.g. 'RB', 'RB/WR/TE').
    for key, n in counts.items():
        if _slot_name_to_id(key) == slot_id and isinstance(n, int):
            return max(n, 1)
    # Fall back to the plain label lookup.
    val = counts.get(label)
    return max(val, 1) if isinstance(val, int) else 1
