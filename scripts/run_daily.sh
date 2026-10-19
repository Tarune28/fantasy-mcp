#!/bin/bash
# Wrapper launchd runs on a schedule. It loads secrets from autopilot.env (kept
# out of git and out of the plist), runs the autopilot once, and appends all
# output to a rotating-ish log. Kept dependency-free and absolute-path-safe
# because launchd runs with a bare environment and no working directory.

set -uo pipefail

# Resolve the project root from this script's own location.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

ENV_FILE="$PROJECT_DIR/autopilot.env"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/autopilot.log"

# A Python 3.10+ interpreter that has espn-api + mcp installed. Edit if yours
# differs (find it with `which python3.11`).
PYTHON="${AUTOPILOT_PYTHON:-/Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11}"

mkdir -p "$LOG_DIR"

{
  echo "----- $(date '+%Y-%m-%d %H:%M:%S') launchd run -----"
  if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
  else
    echo "ERROR: $ENV_FILE not found. Copy autopilot.example.env to autopilot.env."
    exit 1
  fi

  cd "$PROJECT_DIR" || exit 1
  "$PYTHON" run_daily.py
} >> "$LOG_FILE" 2>&1

# Keep the log from growing forever: trim to the last ~2000 lines.
if [[ -f "$LOG_FILE" ]]; then
  tail -n 2000 "$LOG_FILE" > "$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
fi
