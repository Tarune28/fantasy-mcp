# ESPN Fantasy Football MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io) server that gives Claude
Desktop full read access to your ESPN Fantasy Football league — standings, rosters,
matchups, free agents, trades, and purpose-built tools for trade and roster-improvement
analysis.

Built on [`espn-api`](https://github.com/cwendt94/espn-api) and the
[`mcp`](https://pypi.org/project/mcp/) Python SDK.

---

## Tools exposed

| Tool | What it does |
| --- | --- |
| `get_league_overview` | League name, settings, current week, team count |
| `get_standings` | Teams ranked by record and points, with streaks |
| `get_teams` | Every team + owner (use to find names for other tools) |
| `get_my_team` | Report which team is configured as yours |
| `get_team_roster` | Roster for a team (defaults to yours; starters/bench, projections, injuries) |
| `get_matchups` | All matchups for a week (scores + projections) |
| `get_scoreboard` | Current week's live scores |
| `get_head_to_head` | Season history between two teams |
| `get_free_agents` | Top available FAs by position |
| `get_player_stats` | A player's season stats, weekly scores, status |
| `get_power_rankings` | Power rankings for a week |
| `get_trade_activity` | Recent completed trades |
| `get_recent_transactions` | Recent adds, drops, waiver claims, and trades |
| `get_waiver_order` | Waiver priority / FAAB budgets + processing schedule |
| `get_playoff_picture` | Seeds, clinched / in-contention / eliminated |
| `compare_teams` | Side-by-side starter comparison, position by position |
| `get_league_settings` | Scoring format, roster slots, playoff/trade rules |
| `get_team_schedule` | Remaining schedule + opponent strength |
| `get_player_schedule` | A player's NFL bye week + remaining matchups |
| `get_team_analysis` | Aggregated snapshot for improvement advice |
| `get_trade_candidates` | Trade targets for a team's weakest position |
| `get_start_sit` | Projection-based start/sit swaps + injury/bye flags |
| `refresh_league` | Force a re-fetch of all league data |

### Write tools (change your roster)

These modify your **real** ESPN team. They require the private-league cookies
(`ESPN_S2` / `ESPN_SWID`) and act only on the team detected as yours.

| Tool | What it does |
| --- | --- |
| `add_drop_player` | Add a free agent and drop a player to make room (immediate) |
| `submit_waiver_claim` | Queue a waiver claim (+ FAAB bid) for the next waiver run |
| `set_lineup` | Move a player into a starting slot or the bench (start/sit) |
| `cancel_pending_claim` | Withdraw one of your pending waiver / FA claims |

There's also a read tool, `get_pending_claims`, that lists your own submitted-but-
unprocessed claims (add/drop, FAAB bid, and a claim id). Despite a widespread
belief that ESPN hides pending claims from the API, the `mTransactions2` view
returns them for the authenticated owner — `cancel_pending_claim` uses that id to
withdraw one.

Every write tool is **two-step**: called without `confirm=true` it only *previews*
the exact move and changes nothing. It submits to ESPN only when called again with
`confirm=true`. Ask for the preview first, check it, then confirm.

> **How writes work.** The read tools use the `espn-api` library, which is
> GET-only. The write tools instead POST directly to ESPN's undocumented
> `lm-api-writes` transactions endpoint, reusing your cookies. That endpoint is
> reverse-engineered and unsupported by ESPN — it can change or break, and a
> dropped player is released to the league immediately. ESPN also can't read back
> *pending* waiver claims, so a submitted claim won't reappear in any read tool
> until it processes; check/cancel it in the ESPN app.

All team-name arguments accept **either the team name or the owner name**, matched
case-insensitively as a substring. If nothing matches, the tool returns the list of
valid teams so you can try again.

---

## Project layout

```
server.py                      # entry point Claude Desktop runs (delegates to the package)
espn_fantasy_mcp/
  config.py                    # env vars, constants, prompting guidance
  app.py                       # the MCP server instance + main()
  client.py                    # ESPN League caching (get_league / reset_league)
  formatting.py                # version-tolerant helpers (projections, fuzzy match, weeks…)
  tools/
    league.py                  # league-wide tools (overview, standings, matchups, settings…)
    teams.py                   # roster, head-to-head, schedule, comparison, analysis
    players.py                 # free agents, player stats, player NFL schedule
    advice.py                  # trade candidates + start/sit
    roster_moves.py            # WRITE tools: add/drop, waiver claim, set lineup
```

Importing `espn_fantasy_mcp.tools` runs the `@mcp.tool()` decorators, so `main()`
registers every tool by importing that package before starting the stdio server.

---

## 1. Install dependencies

**Requires Python 3.10+** (the `mcp` SDK does not support 3.8/3.9). Check with
`python3 --version`; if it's older, install/use a newer one (e.g. `python3.11`).

```bash
cd ESPN-Fantasy-MCP
python3.11 -m pip install -r requirements.txt
```

(Or `python3.11 -m pip install espn-api mcp` directly.)

---

## 2. Get your ESPN cookies (private leagues only)

Public leagues need only `ESPN_LEAGUE_ID`. **Private** leagues also need two cookies,
`ESPN_S2` and `SWID`. To find them:

1. In a desktop browser, log in to <https://fantasy.espn.com> and open your league.
2. Open **Developer Tools** (`F12`, or right-click → *Inspect*).
3. Go to the **Application** tab (Chrome/Edge) or **Storage** tab (Firefox).
4. In the left sidebar, expand **Cookies** and click `https://fantasy.espn.com`.
5. Find these two cookies and copy their **Value**:
   - **`espn_s2`** — a long string (often with `%` characters). This is your `ESPN_S2`.
   - **`SWID`** — looks like `{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}` (keep the braces).
     This is your `ESPN_SWID`.

Your **league id** is in the league URL:
`https://fantasy.espn.com/football/league?leagueId=123456` → `ESPN_LEAGUE_ID=123456`.

> Keep these cookies private — they authenticate as your ESPN account. Never commit them.

---

## 3. Add to Claude Desktop

Edit your `claude_desktop_config.json`:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

Add this block (`command` must be a Python 3.10+ interpreter that has the deps
installed — plain `python`/`python3` may be too old):

```json
{
  "mcpServers": {
    "espn-fantasy": {
      "command": "python3.11",
      "args": ["path/to/server.py"],
      "env": {
        "ESPN_LEAGUE_ID": "your_league_id",
        "ESPN_S2": "your_espn_s2_cookie",
        "ESPN_SWID": "your_swid_cookie",
        "ESPN_YEAR": "2026"
      }
    }
  }
}
```

Notes:
- For a **public** league, omit `ESPN_S2` and `ESPN_SWID`.
- `ESPN_YEAR` is optional; it defaults to the current calendar year.
- **Identifying your team** (so tools default to it and you don't repeat who you
  are): for a **private** league nothing extra is needed — your `ESPN_SWID`
  cookie is your owner id, so the server auto-detects your team. To set it
  explicitly (or for a public league), add `ESPN_TEAM_ID` (preferred, stable) or
  `ESPN_TEAM_NAME`. Resolution order is `ESPN_TEAM_ID` → `ESPN_TEAM_NAME` →
  auto-detect from `ESPN_SWID`. Confirm with the `get_my_team` tool.
- `ESPN_CACHE_TTL` is optional (default `180` seconds). League data is cached
  between calls for speed, but any tool call made after the cache passes this
  age transparently re-fetches from ESPN, so rosters and waiver results stay
  current without a manual refresh. Set to `0` to cache for the whole process
  (only `refresh_league` re-fetches); lower it for fresher live-scoring reads.
- `command` needs an **absolute path** if `python3.11` isn't on Claude Desktop's PATH
  (find yours with `which python3.11`). A venv's Python works too
  (e.g. `"/absolute/path/to/.venv/bin/python"`).
- Use an **absolute path** for `server.py`.

Restart Claude Desktop. The `espn-fantasy` tools will appear in the tools menu (the
hammer/plug icon).

---

## Usage tips

Ask Claude things like:

- "What are the current standings?"
- "Show me the roster for the Gridiron Gang."
- "Compare my team to the first-place team."
- "Analyze my roster and tell me my weakest position."
- "Who should I target in a trade to fix my RB depth?"
- "What free-agent WRs are available this week?"

The server ships with prompting guidance (in `server.py`) telling Claude to check
scoring settings, bye weeks, and schedule strength before giving trade or start/sit
advice.

---

## Troubleshooting

- **"Could not load league …"** — check `ESPN_LEAGUE_ID`, and for private leagues verify
  both cookies. `espn_s2` is long and may contain `%` characters; copy the whole value.
- **Data looks stale** — cached data auto-refreshes once it passes `ESPN_CACHE_TTL`
  (default 3 min), so most staleness clears on the next call. To force the latest
  immediately (e.g. a waiver just ran), call `refresh_league` or ask Claude to refresh.
  Roster tools also print a "Data as of HH:MM" line so you can see the age.
- **A player isn't found** — try a fuller name; lookups are provided by ESPN's search.
- **Tools don't appear in Claude Desktop** — confirm the JSON is valid, the path to
  `server.py` is absolute, and the configured `python` can import `mcp` and `espn_api`.
