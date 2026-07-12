#!/usr/bin/env bash
# Start the IMAP IDLE watcher (event-driven new-mail push).
# flock single-instance: this doubles as the @reboot launcher AND the watchdog
# entrypoint (instant no-op if already running). Waits for DNS before launching
# (slow-boot safe).
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # this script's own directory
PY="$DIR/venv/bin/python"                              # adjust to your interpreter
LOG="$DIR/logs/idle_watcher.log"
LOCK="$DIR/logs/.idle.lock"
mkdir -p "$DIR/logs"

exec 9>"$LOCK"
if ! flock -n 9; then exit 0; fi   # already running -> no-op

# wait for network/DNS (up to 5 min) so a fast @reboot doesn't die on cold DNS
for i in $(seq 1 60); do
  if getent hosts imap.gmail.com >/dev/null 2>&1; then break; fi
  sleep 5
done
if ! getent hosts imap.gmail.com >/dev/null 2>&1; then
  echo "$(date -Is) network still down; not starting idle_watcher" >> "$LOG"
  exit 0
fi

echo "$(date -Is) launching idle_watcher" >> "$LOG"
exec "$PY" -u "$DIR/idle_watcher.py" >> "$LOG" 2>&1
