#!/usr/bin/env bash
# TEMPLATE: single-instance launcher for a persistent connection worker
# (e.g. an IMAP IDLE watcher, a websocket listener, a message-queue consumer).
#
# The flock makes this script safe to use as BOTH the @reboot launcher AND the
# */5 watchdog cron: it's an instant no-op if the worker is already running.
# Waits for DNS before launching so a fast @reboot doesn't die on cold DNS.
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # project dir, e.g. ~/myproject
PY="${PY:-$DIR/venv/bin/python}"                      # project virtualenv python
LOG="$DIR/logs/watcher.log"
LOCK="$DIR/logs/.watcher.lock"
REMOTE_HOST="${REMOTE_HOST:-imap.example.com}"        # host to wait for DNS on
mkdir -p "$DIR/logs"

# Single-instance lock -> no-op if already running.
exec 9>"$LOCK"
if ! flock -n 9; then exit 0; fi

# Wait for network/DNS (up to 60 x 5s = 5 min) so a fast @reboot doesn't die on cold DNS.
for i in $(seq 1 60); do
  if getent hosts "$REMOTE_HOST" >/dev/null 2>&1; then break; fi
  sleep 5
done
if ! getent hosts "$REMOTE_HOST" >/dev/null 2>&1; then
  echo "$(date -Is) network still down; not starting watcher" >> "$LOG"
  exit 0
fi

echo "$(date -Is) launching watcher" >> "$LOG"
exec "$PY" -u "$DIR/watcher.py" >> "$LOG" 2>&1
