"""Autonomous daily manager ("autopilot") for the ESPN Fantasy team.

This module is what turns the read/write tools into something a scheduler (see
``run_daily.py`` + the launchd plist under ``scripts/``) can run unattended once
a day to *manage the user's team for them*:

1. **Set the optimal lineup.** Unlike ``get_start_sit`` (which only proposes
   one-for-one bench<->starter swaps), this solves the whole lineup as an
   assignment problem — every roster player against every startable slot,
   respecting eligibility and FLEX, maximizing total projected points — then
   submits exactly the lineup moves needed to reach it. Low risk: lineup changes
   are reversible and never cost anything.

2. **Make free-agent upgrades (optional, guarded).** Scans the free-agent pool
   and, only when adding a player and dropping the *worst non-starter* would
   actually raise the optimal starting-lineup total by a margin, submits that
   add/drop. Hard guardrails: never drops a player in the optimal lineup, never
   drops a widely-rostered player (a stud having a down week), one move per run.

3. **Waiver claims (optional, off by default).** FAAB bidding is genuinely hard
   to automate well, so it's opt-in behind ``AUTO_WAIVER`` with a capped bid.

Everything is **dry-run by default** (``AUTOPILOT_LIVE`` unset): the first runs
log exactly what they *would* do so the decisions can be eyeballed before real,
irreversible moves happen. Flip ``AUTOPILOT_LIVE=1`` to go live.

Reliability: the ESPN write cookies (ESPN_S2/SWID) expire and every write then
silently 401s. This module treats an auth failure — or any crash — as a loud
alert (macOS notification + optional ``AUTOPILOT_ALERT_CMD`` hook) so a dead
cookie doesn't quietly strand the team.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import sys
from collections import deque
from dataclasses import dataclass
from typing import Any, Optional

from . import config as cfg
from .client import get_league, reset_league
from .config import BENCH_SLOTS
from .formatting import (
    _settings_slot_counts,
    eligible_slots,
    injury_flag,
    my_team,
    player_position,
    player_projected,
    player_slot,
    upcoming_week,
)
from .tools.roster_moves import (
    _BENCH_SLOT_ID,
    _post_transaction,
    _scoring_period,
    _slot_id_to_name,
    _slot_name_to_id,
    _write_ready,
)

# Injury states that mean "will not play this week" — start no one carrying one.
_WONT_PLAY = {"OUT", "INJURY_RESERVE", "SUSPENSION"}
_IR_LABELS = {"IR", "Injured Reserve"}


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


@dataclass
class AutopilotConfig:
    """Tunable behavior, all read from the environment so it needs no code edits."""

    live: bool = False              # AUTOPILOT_LIVE — submit for real vs dry-run
    set_lineup: bool = True         # AUTOPILOT_LINEUP — auto-optimize the lineup
    add_free_agents: bool = True    # AUTOPILOT_ADD — auto add/drop free agents
    submit_waivers: bool = False    # AUTOPILOT_WAIVER — auto submit waiver claims

    min_upgrade_pts: float = 2.0    # AUTOPILOT_MIN_UPGRADE — starter-total gain to act
    protect_owned_pct: float = 55.0  # AUTOPILOT_PROTECT_OWNED — never drop above this
    max_faab_pct: float = 20.0      # AUTOPILOT_MAX_FAAB_PCT — cap a bid at % of budget
    fa_scan: int = 150              # AUTOPILOT_FA_SCAN — free agents to consider

    @classmethod
    def from_env(cls) -> "AutopilotConfig":
        return cls(
            live=_flag("AUTOPILOT_LIVE", False),
            set_lineup=_flag("AUTOPILOT_LINEUP", True),
            add_free_agents=_flag("AUTOPILOT_ADD", True),
            submit_waivers=_flag("AUTOPILOT_WAIVER", False),
            min_upgrade_pts=_num("AUTOPILOT_MIN_UPGRADE", 2.0),
            protect_owned_pct=_num("AUTOPILOT_PROTECT_OWNED", 55.0),
            max_faab_pct=_num("AUTOPILOT_MAX_FAAB_PCT", 20.0),
            fa_scan=int(_num("AUTOPILOT_FA_SCAN", 150)),
        )


# --------------------------------------------------------------------------- #
# Logging + alerting
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    """Timestamped line to stdout (the launchd wrapper redirects this to a file)."""
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


def alert(msg: str) -> None:
    """Surface something the user must see even when away from the logs.

    Always logs. On macOS also fires a Notification Center banner. If
    ``AUTOPILOT_ALERT_CMD`` is set, runs it with the message on stdin so the user
    can wire up email / ntfy / Pushover / a Shortcut, etc.
    """
    log(f"ALERT: {msg}")
    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification {json.dumps(msg)} '
                f'with title "ESPN Fantasy Autopilot"',
            ],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except Exception:  # noqa: BLE001 - notifications are best-effort
        pass

    hook = os.environ.get("AUTOPILOT_ALERT_CMD")
    if hook:
        try:
            subprocess.run(
                hook, shell=True, input=msg.encode(), check=False,
                capture_output=True, timeout=20,
            )
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# Lineup optimization (assignment problem via min-cost max-flow)
# --------------------------------------------------------------------------- #
def _effective_proj(player: Any, week: int) -> float:
    """Projection to optimize on — zeroed for players who won't play (OUT/bye)."""
    if injury_flag(player) in _WONT_PLAY:
        return 0.0
    return max(player_projected(player, week), 0.0)


def _starting_slots(league: Any) -> list[str]:
    """The startable slot labels to fill, expanded by count (e.g. two 'RB')."""
    counts = _settings_slot_counts(league)
    slots: list[str] = []
    for label, count in counts.items():
        if label in BENCH_SLOTS or label in _IR_LABELS:
            continue
        slots.extend([label] * int(count))
    return slots


def _assign(players: list[Any], slots: list[str], week: int) -> tuple[dict[int, int], float]:
    """Max-projection assignment of players to slot instances.

    Returns ``(matched, total)`` where ``matched`` maps a player index to a slot
    index (into ``slots``), and ``total`` is the summed projection of matched
    players. Solved exactly as min-cost max-flow: max flow fills as many starting
    slots as possible (an empty slot scores 0, so more filled is never worse),
    and min cost maximizes projected points among those.
    """
    P, S = len(players), len(slots)
    if P == 0 or S == 0:
        return {}, 0.0

    source = 0
    p0 = 1               # player nodes: p0 .. p0 + P - 1
    s0 = p0 + P          # slot nodes:   s0 .. s0 + S - 1
    sink = s0 + S
    n = sink + 1

    graph: list[list[list[int]]] = [[] for _ in range(n)]

    def add_edge(u: int, v: int, cap: int, cost: int) -> None:
        graph[u].append([v, cap, cost, len(graph[v])])
        graph[v].append([u, 0, -cost, len(graph[u]) - 1])

    projs = [_effective_proj(p, week) for p in players]
    elig = [eligible_slots(p) for p in players]
    for i in range(P):
        add_edge(source, p0 + i, 1, 0)
        for j, slot in enumerate(slots):
            if slot in elig[i]:
                # Cost is negative points (scaled to int) so min-cost == max-points.
                add_edge(p0 + i, s0 + j, 1, -int(round(projs[i] * 100)))
    for j in range(S):
        add_edge(s0 + j, sink, 1, 0)

    INF = float("inf")
    while True:  # SPFA (Bellman-Ford queue) shortest augmenting path
        dist = [INF] * n
        in_q = [False] * n
        prevv = [-1] * n
        preve = [-1] * n
        dist[source] = 0
        dq = deque([source])
        while dq:
            u = dq.popleft()
            in_q[u] = False
            du = dist[u]
            for i, e in enumerate(graph[u]):
                v, cap, cost, _rev = e
                if cap > 0 and du + cost < dist[v]:
                    dist[v] = du + cost
                    prevv[v] = u
                    preve[v] = i
                    if not in_q[v]:
                        in_q[v] = True
                        dq.append(v)
        if dist[sink] == INF:
            break
        f = INF
        v = sink
        while v != source:
            f = min(f, graph[prevv[v]][preve[v]][1])
            v = prevv[v]
        v = sink
        while v != source:
            e = graph[prevv[v]][preve[v]]
            e[1] -= f
            graph[v][e[3]][1] += f
            v = prevv[v]

    matched: dict[int, int] = {}
    total = 0.0
    for i in range(P):
        for e in graph[p0 + i]:
            v, cap, _cost, _rev = e
            if s0 <= v < s0 + S and cap == 0:  # cap consumed => this edge carried flow
                j = v - s0
                matched[i] = j
                total += projs[i]
                break
    return matched, total


def optimal_total(players: list[Any], slots: list[str], week: int) -> float:
    """Best achievable starting-lineup projection for a set of players."""
    return _assign(players, slots, week)[1]


def _lineup_candidates(team: Any) -> list[Any]:
    """Roster players eligible to be started (excludes IR-slotted players)."""
    return [p for p in (getattr(team, "roster", None) or []) if player_slot(p) not in _IR_LABELS]


def plan_lineup(league: Any, team: Any, week: int) -> list[tuple[Any, int, int]]:
    """Compute the lineup moves needed to reach the optimal lineup.

    Returns a list of ``(player, from_slot_id, to_slot_id)`` for every player
    whose slot must change. Empty if the lineup is already optimal.
    """
    players = _lineup_candidates(team)
    slots = _starting_slots(league)
    matched, _ = _assign(players, slots, week)

    moves: list[tuple[Any, int, int]] = []
    for i, p in enumerate(players):
        target_label = slots[matched[i]] if i in matched else None
        target_id = _slot_name_to_id(target_label) if target_label else _BENCH_SLOT_ID
        if target_id is None:
            target_id = _BENCH_SLOT_ID
        cur_id = _slot_name_to_id(player_slot(p))
        if cur_id is None:
            cur_id = _BENCH_SLOT_ID
        if cur_id != target_id:
            moves.append((p, cur_id, target_id))
    return moves


# --------------------------------------------------------------------------- #
# Submitting transactions
# --------------------------------------------------------------------------- #
def _is_auth_error(result: str) -> bool:
    return result.startswith("Error") and ("401" in result or "403" in result)


def _submit_lineup(league: Any, team: Any, moves: list[tuple[Any, int, int]]) -> bool:
    """Submit lineup moves as one transaction, falling back to one-at-a-time.

    A single locked player (game already started) makes ESPN reject the whole
    batch, so on batch failure we retry each move alone and skip the ones ESPN
    won't allow. Returns True if anything was applied.
    """
    def items_for(mvs: list[tuple[Any, int, int]]) -> list[dict[str, Any]]:
        return [
            {
                "playerId": p.playerId,
                "type": "LINEUP",
                "fromLineupSlotId": frm,
                "toLineupSlotId": to,
            }
            for (p, frm, to) in mvs
        ]

    def payload(mvs: list[tuple[Any, int, int]]) -> dict[str, Any]:
        return {
            "isLeagueManager": False,
            "teamId": team.team_id,
            "memberId": str(cfg.SWID),
            "type": "ROSTER",
            "scoringPeriodId": _scoring_period(league),
            "executionType": "EXECUTE",
            "items": items_for(mvs),
        }

    result = _post_transaction(league, payload(moves))
    if result == "OK":
        return True
    if _is_auth_error(result):
        alert("ESPN rejected a lineup change (auth). Cookies likely expired — "
              "refresh ESPN_S2 / ESPN_SWID.")
        return False

    log(f"Batch lineup submit failed ({result}); retrying moves individually.")
    applied = False
    for mv in moves:
        r = _post_transaction(league, payload([mv]))
        name = getattr(mv[0], "name", "?")
        if r == "OK":
            applied = True
            log(f"  applied: {name} -> {_slot_id_to_name(mv[2])}")
        elif _is_auth_error(r):
            alert("ESPN auth failure mid-lineup — refresh ESPN_S2 / ESPN_SWID.")
            break
        else:
            log(f"  skipped {name} ({_slot_id_to_name(mv[1])} -> "
                f"{_slot_id_to_name(mv[2])}): {r}")
    return applied


def _submit_add_drop(league: Any, team: Any, add: Any, drop: Any) -> bool:
    payload = {
        "isLeagueManager": False,
        "teamId": team.team_id,
        "memberId": str(cfg.SWID),
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
        return True
    if _is_auth_error(result):
        alert("ESPN rejected an add/drop (auth). Refresh ESPN_S2 / ESPN_SWID.")
    else:
        log(f"Add/drop failed: {result}")
    return False


def _submit_waiver(league: Any, team: Any, add: Any, drop: Any, bid: int) -> bool:
    payload = {
        "isLeagueManager": False,
        "teamId": team.team_id,
        "memberId": str(cfg.SWID),
        "type": "WAIVER",
        "scoringPeriodId": _scoring_period(league),
        "executionType": "EXECUTE",
        "items": [
            {"playerId": add.playerId, "type": "ADD", "toTeamId": team.team_id},
            {"playerId": drop.playerId, "type": "DROP", "fromTeamId": team.team_id},
        ],
    }
    if bool(getattr(league.settings, "faab", False)):
        payload["bidAmount"] = int(bid)
    result = _post_transaction(league, payload)
    if result == "OK":
        return True
    if _is_auth_error(result):
        alert("ESPN rejected a waiver claim (auth). Refresh ESPN_S2 / ESPN_SWID.")
    else:
        log(f"Waiver claim failed: {result}")
    return False


# --------------------------------------------------------------------------- #
# Acquisition planning (free-agent upgrades)
# --------------------------------------------------------------------------- #
def _owned_pct(player: Any) -> float:
    v = getattr(player, "percent_owned", None)
    return float(v) if isinstance(v, (int, float)) else 0.0


def plan_acquisition(
    league: Any, team: Any, week: int, conf: AutopilotConfig
) -> Optional[dict[str, Any]]:
    """Find the single best free-agent add/drop that improves the starting lineup.

    Principle: an add is only worth making if swapping it in for the worst
    droppable player would raise the *optimal starting-lineup projection* by at
    least ``min_upgrade_pts``. That naturally ignores adds that wouldn't crack
    the lineup, and never recommends dropping a current starter.

    Returns a dict describing the move (add/drop/gain) or None if nothing clears
    the bar. Applies guardrails: protects the optimal lineup and widely-rostered
    players from being dropped.
    """
    roster = _lineup_candidates(team)
    slots = _starting_slots(league)
    matched, base_total = _assign(roster, slots, week)
    optimal_ids = {roster[i].playerId for i in matched}

    # Droppable = not in the optimal lineup, not widely rostered (protect studs).
    droppable = [
        p for p in roster
        if p.playerId not in optimal_ids
        and _owned_pct(p) < conf.protect_owned_pct
    ]
    if not droppable:
        return None
    # The worst bench player by projection is the natural drop.
    drop = min(droppable, key=lambda p: _effective_proj(p, week))

    try:
        agents = league.free_agents(size=conf.fa_scan)
    except Exception as exc:  # noqa: BLE001
        log(f"Could not load free agents: {exc}")
        return None

    # Only bother testing FAs that project well enough to matter, at positions we
    # actually roster (keeps the what-if search small).
    roster_positions = {player_position(p) for p in roster}
    contenders = sorted(
        (a for a in agents
         if player_position(a) in roster_positions
         and _effective_proj(a, week) > _effective_proj(drop, week)),
        key=lambda a: _effective_proj(a, week),
        reverse=True,
    )[:40]

    best: Optional[dict[str, Any]] = None
    kept = [p for p in roster if p.playerId != drop.playerId]
    for fa in contenders:
        new_total = optimal_total(kept + [fa], slots, week)
        gain = new_total - base_total
        if gain >= conf.min_upgrade_pts and (best is None or gain > best["gain"]):
            best = {"add": fa, "drop": drop, "gain": round(gain, 1)}
    return best


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run(conf: Optional[AutopilotConfig] = None) -> int:
    """Run one full autopilot pass. Returns a process exit code (0 = ok)."""
    conf = conf or AutopilotConfig.from_env()
    mode = "LIVE" if conf.live else "DRY-RUN (no changes will be submitted)"
    log(f"=== ESPN Fantasy autopilot — {mode} ===")

    err = _write_ready()
    if err:
        alert(f"Autopilot can't make changes: {err}")
        return 2

    try:
        league = get_league(max_age=0)  # always start from fresh ESPN data
    except Exception as exc:  # noqa: BLE001
        alert(f"Could not load the league from ESPN: {exc}")
        return 2

    team = my_team(league)
    if team is None:
        alert("Autopilot can't tell which team is yours (set ESPN_TEAM_ID / "
              "ESPN_TEAM_NAME, or ESPN_SWID for a private league).")
        return 2

    week = upcoming_week(league)
    log(f"Team: {team.team_name} — planning for week {week}")

    changed = False

    # 1) Lineup ----------------------------------------------------------------
    if conf.set_lineup:
        try:
            moves = plan_lineup(league, team, week)
        except Exception as exc:  # noqa: BLE001
            alert(f"Lineup planning crashed: {exc}")
            moves = []
        if not moves:
            log("Lineup already optimal — no changes.")
        else:
            log(f"Lineup: {len(moves)} move(s) to reach the optimal lineup:")
            for p, frm, to in moves:
                log(f"  {getattr(p, 'name', '?')}: "
                    f"{_slot_id_to_name(frm)} -> {_slot_id_to_name(to)}")
            if conf.live:
                if _submit_lineup(league, team, moves):
                    changed = True
                    log("Lineup submitted.")
            else:
                log("(dry-run) lineup NOT submitted.")

    # 2) Free-agent upgrade ----------------------------------------------------
    if conf.add_free_agents:
        try:
            move = plan_acquisition(league, team, week, conf)
        except Exception as exc:  # noqa: BLE001
            alert(f"Acquisition planning crashed: {exc}")
            move = None
        if not move:
            log("No free-agent upgrade clears the bar — no add/drop.")
        else:
            add, drop, gain = move["add"], move["drop"], move["gain"]
            log(f"Add/drop: ADD {getattr(add, 'name', '?')} "
                f"({player_position(add)}), DROP {getattr(drop, 'name', '?')} "
                f"({player_position(drop)})  [+{gain} pts to starting lineup]")
            if conf.live:
                if _submit_add_drop(league, team, add, drop):
                    changed = True
                    log("Add/drop submitted.")
            else:
                log("(dry-run) add/drop NOT submitted.")

    # 3) Waiver claim (opt-in) -------------------------------------------------
    if conf.submit_waivers:
        log("AUTOPILOT_WAIVER is on, but automated FAAB bidding is intentionally "
            "left as a stub — enable/extend deliberately in autopilot.py.")

    if changed:
        reset_league()
    log("=== autopilot done ===")
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
