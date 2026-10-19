"""CLI entry point for the daily autopilot (see espn_fantasy_mcp/autopilot.py).

Run it directly to test:

    python3.11 run_daily.py            # dry-run unless AUTOPILOT_LIVE=1
    python3.11 run_daily.py --live     # force a real run this invocation
    python3.11 run_daily.py --dry-run  # force dry-run this invocation

Credentials and tuning come from the environment (the same ESPN_* vars the MCP
server uses, plus the AUTOPILOT_* knobs documented in autopilot.py). The launchd
wrapper under scripts/ loads them from an env file so secrets stay out of the
plist. This is the file launchd/cron should invoke.
"""

from __future__ import annotations

import argparse
import os

from espn_fantasy_mcp.autopilot import AutopilotConfig, alert, run


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the ESPN fantasy autopilot once.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--live", action="store_true",
                       help="Submit changes for real (overrides AUTOPILOT_LIVE).")
    group.add_argument("--dry-run", action="store_true",
                       help="Plan only; submit nothing (overrides AUTOPILOT_LIVE).")
    args = parser.parse_args()

    conf = AutopilotConfig.from_env()
    if args.live:
        conf.live = True
    elif args.dry_run:
        conf.live = False

    try:
        code = run(conf)
    except Exception as exc:  # noqa: BLE001 - last-resort net so cron failures alert
        alert(f"Autopilot crashed unexpectedly: {exc}")
        raise SystemExit(1) from exc
    raise SystemExit(code)


if __name__ == "__main__":
    main()
