#!/usr/bin/env bash
# KB self-refinement loop — cron entry point. Single instance via flock.
# Watches for new sent replies from the owner to customer questions and runs the
# refine loop (headless Claude) on each; see refine_prompt.md and README.md.
set -u
DIR="/home/mercury/DST/email/kb-refine"
LOG="$DIR/logs/refine.log"
mkdir -p "$DIR/logs"

exec 9>"$DIR/.lock"
flock -n 9 || { echo "$(date -Is) already running; skip" >> "$LOG"; exit 0; }

echo "=== $(date -Is) kb-refine run ===" >> "$LOG"
/usr/bin/python3 "$DIR/watch.py" >> "$LOG" 2>&1
echo "=== $(date -Is) done (exit $?) ===" >> "$LOG"
