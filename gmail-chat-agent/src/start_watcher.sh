#!/usr/bin/env bash
# Start the gmail-chat-agent watcher (IMAP IDLE, event-driven).
# flock single-instance: doubles as the @reboot launcher AND the watchdog
# entrypoint (instant no-op if already running). Waits for DNS on slow boots.
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$DIR/venv/bin/python"                              # adjust to your interpreter
LOG="$DIR/logs/agent_watcher.log"
LOCK="$DIR/logs/.agent.lock"
mkdir -p "$DIR/logs"

exec 9>"$LOCK"
if ! flock -n 9; then exit 0; fi   # already running -> no-op

for i in $(seq 1 60); do
  if getent hosts imap.gmail.com >/dev/null 2>&1; then break; fi
  sleep 5
done
if ! getent hosts imap.gmail.com >/dev/null 2>&1; then
  echo "$(date -Is) network still down; not starting agent_watcher" >> "$LOG"
  exit 0
fi

echo "$(date -Is) launching agent_watcher" >> "$LOG"
exec "$PY" -u "$DIR/agent_watcher.py" >> "$LOG" 2>&1
