#!/usr/bin/env bash
# Daily follow-up digest — run from cron.
# Default schedule is 08:00 daily; e.g. add to your crontab:
#   0 8 * * *  /path/to/followup-check/src/followups_run.sh
# Adjust the "0 8" fields for a different time / timezone.
#
# Set PYTHON to a virtualenv interpreter if your deps live in one, e.g.:
#   PYTHON=/path/to/venv/bin/python
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"
LOG="$HERE/logs/followups.log"
mkdir -p "$HERE/logs"
echo "=== $(date -Is) follow-up check ===" >> "$LOG"
PYTHONWARNINGS="ignore::FutureWarning" "$PYTHON" "$HERE/followups.py" >> "$LOG" 2>&1
echo "=== $(date -Is) done (exit $?) ===" >> "$LOG"
