"""MCP prompts: one-click entry points Claude Desktop lists in its prompt menu.

Each returns a user message that tells Claude which composite tool to run and
how to frame the answer, so a thin ask ("trade check") still produces a full
analysis. Importing this module registers the prompts on the shared server.
"""

from __future__ import annotations

from .app import mcp


@mcp.prompt(name="trade_check", description="Evaluate a trade: what I give vs what I get")
def trade_check(give: str, receive: str) -> str:
    return (
        f"Should I trade {give} for {receive}? Run get_fantasy_context, then "
        f"evaluate_trade(give='{give}', receive='{receive}'). Weigh role security "
        "(including injured teammates coming back), offense/coaching, usage trend, health, "
        "this week's matchup, rest-of-season and playoff schedule, scoring format, and whether "
        "it fixes my actual weakness. If recent news could change it and you have web search, "
        "check it. Give me a clear yes/no/counter and the reasons."
    )


@mcp.prompt(name="weekly_briefing", description="My team right now: lineup, injuries, byes, waivers")
def weekly_briefing() -> str:
    return (
        "Give me my weekly briefing. Call get_fantasy_context and get_start_sit, run "
        "analyze_player on any starter with an injury flag or a close start/sit call, and "
        "check the best free agents at my weak spots. Tell me: the lineup to set, any moves "
        "to make (with who to drop), and what to watch before kickoff."
    )


@mcp.prompt(name="start_or_sit", description="Pick between two or more players this week")
def start_or_sit(players: str) -> str:
    return (
        f"Who should I start this week: {players}? Call get_fantasy_context, then "
        f"compare_players('{players}'). Decide on role, usage, matchup (implied total, "
        "defense, weather), health and floor vs ceiling given whether I need safety or "
        "upside in my matchup. Give me the call and why."
    )


@mcp.prompt(name="waiver_targets", description="Best pickups for my team and who to drop")
def waiver_targets() -> str:
    return (
        "What should I pick up on waivers? Call get_fantasy_context (openings, weak spots, "
        "available players, pending claims) and get_waiver_order, then analyze_player on the "
        "top 2-3 candidates. Recommend the claim(s), the drop, and a FAAB bid or priority "
        "use, and explain the role/usage reasons."
    )
