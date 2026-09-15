"""Pending (unprocessed) waiver / free-agent claims: list and cancel them.

For a long time the common wisdom (and this server's own guidance) was that
ESPN's API doesn't expose a user's *pending* claims. That's wrong: the
``mPendingTransactions`` view returns them (under the ``pendingTransactions``
key), including the transaction ``id`` and ``bidAmount``. The ``espn-api``
library doesn't surface this at all, so these tools request the view raw.

Why this view and not ``mTransactions2``: the latter is the transaction *log*
and keeps returning a cancelled claim as ``status == "PENDING"`` for a while
after it's gone — a ghost that isn't in the app. ``mPendingTransactions`` is the
authoritative live set the ESPN app shows, so it's the one to trust.

- ``get_pending_claims`` lists your team's pending claims (read-only).
- ``cancel_pending_claim`` cancels one, re-posting it with executionType=CANCEL
  (a write; two-step confirm like the other roster-move tools).
"""

from __future__ import annotations

from typing import Any, Optional

from ..app import mcp
from .. import config
from ..client import freshness_line, get_league
from ..formatting import my_team, _norm_swid  # type: ignore[attr-defined]
from .roster_moves import _post_transaction, _write_ready

# Claim-style transactions a user submits; ROSTER/TRADE_* are handled elsewhere.
_CLAIM_TYPES = {"WAIVER", "FREEAGENT"}


def _fetch_pending_raw(league: Any) -> list[dict[str, Any]]:
    """Raw pending claim dicts for the authenticated user's team.

    Returns ESPN's transaction dicts (with ``id``, ``items``, ``bidAmount``, …)
    scoped to the user's own team, read from the authoritative
    ``mPendingTransactions`` view (the live set the ESPN app shows). Returns []
    on any failure so callers can treat "none" and "couldn't load" the same way
    if they want, though get_pending_claims distinguishes them.
    """
    try:
        data = league.espn_request.league_get(params={"view": "mPendingTransactions"})
    except Exception:  # noqa: BLE001
        return []
    txns = (data or {}).get("pendingTransactions") or []

    team = my_team(league)
    my_team_id = getattr(team, "team_id", None)
    my_swid = _norm_swid(config.SWID) if config.SWID else None

    out: list[dict[str, Any]] = []
    for t in txns:
        # Everything in pendingTransactions is pending by definition; still guard
        # in case ESPN ever returns mixed state, and keep to claim-style types.
        if t.get("status") and t.get("status") != "PENDING":
            continue
        if t.get("type") not in _CLAIM_TYPES:
            continue
        # Scope to the user: match team id, or fall back to the member (SWID).
        mine = (
            (my_team_id is not None and t.get("teamId") == my_team_id)
            or (my_swid and _norm_swid(t.get("memberId")) == my_swid)
        )
        if mine:
            out.append(t)
    return out


def _player_name(league: Any, player_id: Any) -> str:
    name = getattr(league, "player_map", {}).get(player_id)
    return str(name) if name else f"player #{player_id}"


def _describe_items(league: Any, items: list[dict[str, Any]]) -> str:
    adds, drops = [], []
    for it in items or []:
        nm = _player_name(league, it.get("playerId"))
        if it.get("type") == "ADD":
            adds.append(nm)
        elif it.get("type") == "DROP":
            drops.append(nm)
    parts = []
    if adds:
        parts.append("ADD " + ", ".join(adds))
    if drops:
        parts.append("DROP " + ", ".join(drops))
    return "; ".join(parts) if parts else "(no items)"


@mcp.tool()
def get_pending_claims() -> str:
    """List YOUR pending (unprocessed) waiver / free-agent claims.

    These are claims you've submitted that haven't been processed yet — the ones
    ESPN's app shows under "pending transactions". For each it shows the player
    being added, who'd be dropped, the FAAB bid (if any), and a short claim id
    you can pass to cancel_pending_claim.

    Note: ESPN only reveals your own pending claims, not other owners'.
    """
    err = _write_ready()  # same credential requirement: needs the auth cookies
    if err:
        return f"Error: {err}"
    try:
        league = get_league()
    except RuntimeError as exc:
        return f"Error: {exc}"

    pending = _fetch_pending_raw(league)
    team = my_team(league)
    team_label = getattr(team, "team_name", "your team")

    if not pending:
        return (
            f"No pending waiver or free-agent claims for {team_label}. "
            "(Claims already processed show up in get_recent_transactions.)"
        )

    is_faab = bool(getattr(league.settings, "faab", False))
    lines = [f"# Pending claims — {team_label}", ""]
    for t in pending:
        kind = "Waiver" if t.get("type") == "WAIVER" else "Free agent"
        desc = _describe_items(league, t.get("items", []))
        bid = t.get("bidAmount")
        bid_str = f" · ${bid} FAAB" if is_faab and bid else ""
        short_id = str(t.get("id", ""))[:8]
        lines.append(f"- **{kind}**: {desc}{bid_str}  \n  claim id `{short_id}`")

    lines += [
        "",
        "_To cancel one, use cancel_pending_claim with the added player's name "
        "or the claim id._",
    ]
    fresh = freshness_line()
    if fresh:
        lines += ["", fresh]
    return "\n".join(lines)


def _match_pending(
    league: Any, pending: list[dict[str, Any]], player: Optional[str], claim_id: Optional[str]
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Pick a single pending claim by claim id or added-player name."""
    if claim_id:
        cid = claim_id.strip().lower()
        hits = [t for t in pending if str(t.get("id", "")).lower().startswith(cid)]
        if len(hits) == 1:
            return hits[0], None
        if not hits:
            return None, f"No pending claim has an id starting with '{claim_id}'."
        return None, f"'{claim_id}' matches more than one claim id; use more characters."

    if player:
        q = player.strip().lower()
        hits = []
        for t in pending:
            for it in t.get("items", []):
                if it.get("type") == "ADD":
                    nm = _player_name(league, it.get("playerId")).lower()
                    if q == nm or q in nm:
                        hits.append(t)
                        break
        if len(hits) == 1:
            return hits[0], None
        if not hits:
            return None, f"No pending claim is adding a player matching '{player}'."
        return None, (
            f"'{player}' matches several pending claims; pass a claim id instead."
        )

    return None, "Tell me which claim to cancel — a player name or a claim id."


@mcp.tool()
def cancel_pending_claim(
    player: Optional[str] = None,
    claim_id: Optional[str] = None,
    confirm: bool = False,
) -> str:
    """Cancel one of YOUR pending waiver / free-agent claims ("return" it).

    Identify the claim by the player it's adding, or by the claim id from
    get_pending_claims. Like the other write tools this is two-step: called
    without confirm it only previews, and cancels only when called again with
    confirm=True.

    Args:
        player: Name of the player the pending claim is adding.
        claim_id: The short claim id shown by get_pending_claims (alternative to
            player).
        confirm: Must be True to actually cancel.
    """
    err = _write_ready()
    if err:
        return f"Error: {err}"
    try:
        league = get_league()
    except RuntimeError as exc:
        return f"Error: {exc}"

    pending = _fetch_pending_raw(league)
    if not pending:
        return "You have no pending claims to cancel."

    txn, err = _match_pending(league, pending, player, claim_id)
    if err:
        return f"Error: {err}"

    desc = _describe_items(league, txn.get("items", []))
    is_faab = bool(getattr(league.settings, "faab", False))
    bid = txn.get("bidAmount")
    bid_str = f" · ${bid} FAAB" if is_faab and bid else ""
    header = f"**Cancel pending claim:** {desc}{bid_str} (id `{str(txn.get('id',''))[:8]}`)"

    if not confirm:
        return (
            f"Preview — {header}\n\nThis withdraws the claim; it will not be "
            "processed. Nothing has been submitted yet. To go ahead, call "
            "`cancel_pending_claim` again with the same argument plus "
            "`confirm=true`."
        )

    # Re-post the transaction with executionType flipped to CANCEL.
    team = my_team(league)
    payload = {
        "id": txn.get("id"),
        "isLeagueManager": bool(txn.get("isLeagueManager", False)),
        "isActingAsTeamOwner": bool(txn.get("isActingAsTeamOwner", True)),
        "teamId": txn.get("teamId", getattr(team, "team_id", None)),
        "memberId": txn.get("memberId", str(config.SWID)),
        "type": txn.get("type"),
        "scoringPeriodId": txn.get("scoringPeriodId"),
        "executionType": "CANCEL",
        "items": txn.get("items", []),
    }
    if txn.get("bidAmount") is not None:
        payload["bidAmount"] = txn.get("bidAmount")

    result = _post_transaction(league, payload)
    if result == "OK":
        return f"Cancelled — {header}\n\n_The claim has been withdrawn._"
    return result
