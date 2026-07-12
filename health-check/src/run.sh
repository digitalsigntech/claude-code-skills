#!/usr/bin/env bash
# Weekly agent health check. flock = single instance. Logs appended.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$DIR/logs"
exec 9>"$DIR/.lock"
flock -n 9 || exit 0
{
  echo "===== $(date -u '+%Y-%m-%d %H:%M:%S UTC') ====="
  /usr/bin/python3 "$DIR/health_check.py"
} >> "$DIR/logs/health.log" 2>&1
