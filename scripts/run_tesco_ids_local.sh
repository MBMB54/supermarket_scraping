#!/bin/bash
# Weekly Tesco ID refresh from this machine (tesco.ie blocks AWS IPs). Run by launchd; see docs/tesco_ids_local.md.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$HOME/Library/Logs/supermarket"
LOG="$LOG_DIR/tesco_ids_$(date +%F).log"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
mkdir -p "$LOG_DIR"

notify() {
  osascript -e "display notification \"$1\" with title \"Tesco ID refresh\"" || true
}

cd "$REPO"
uv run --group scraper python tesco/tesco_ids.py >>"$LOG" 2>&1
rc=$?
if [ $rc -ne 0 ]; then
  notify "Failed (exit $rc). See $LOG"
  exit $rc
fi
if grep -q "IDs rejected" "$LOG"; then
  notify "IDs rejected by the guard; ids/latest unchanged. See $LOG"
  exit 2
fi
