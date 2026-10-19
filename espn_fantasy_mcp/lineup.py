"""Optimal-lineup solver used by the trade evaluator.

Given a set of roster players and the league's startable slots, finds the
assignment that maximizes projected points while respecting slot eligibility
(including FLEX). Solved exactly as a min-cost max-flow so a 16-player roster
is instant and never approximated.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from .config import BENCH_SLOTS
from .formatting import (
    _settings_slot_counts,
    eligible_slots,
    injury_flag,
    player_projected,
)

# Injury states that mean "will not play this week" — contribute 0 to a lineup.
_WONT_PLAY = {"OUT", "INJURY_RESERVE", "SUSPENSION"}
_IR_LABELS = {"IR", "Injured Reserve"}


def _effective_proj(player: Any, week: int) -> float:
    """Projection to optimize on — zeroed for players who won't play (OUT/bye)."""
    if injury_flag(player) in _WONT_PLAY:
        return 0.0
    return max(player_projected(player, week), 0.0)


def starting_slots(league: Any) -> list[str]:
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
