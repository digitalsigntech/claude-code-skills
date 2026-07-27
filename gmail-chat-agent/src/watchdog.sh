#!/usr/bin/env bash
# gmail-chat-agent WATCHDOG — run every 1 min (cron).
# Verifies the watcher is alive AND holds an ESTABLISHED Gmail IMAP socket on
# port 993. Restarts it if missing, or if socket-less for >= STALE_SECS
# (a wedged IDLE). Silent when healthy.
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$DIR/logs/watchdog.log"
STALE_F="$DIR/logs/.socketless_since"
STALE_SECS=90
mkdir -p "$DIR/logs"
log(){ echo "$(date -Is) $*" >> "$LOG"; }

PID=$(pgrep -f "agent_watcher.py" | head -1 || true)

if [ -z "$PID" ]; then
  rm -f "$STALE_F"
  log "watcher not running -> starting"
  "$DIR/start_watcher.sh"
  exit 0
fi

if ss -tnpH state established 'dport = :993' 2>/dev/null | grep -q "pid=$PID,"; then
  rm -f "$STALE_F"
  exit 0
fi

now=$(date +%s)
if [ -f "$STALE_F" ]; then
  since=$(cat "$STALE_F" 2>/dev/null || echo "$now")
else
  echo "$now" > "$STALE_F"; since=$now
fi
elapsed=$(( now - since ))
if [ "$elapsed" -ge "$STALE_SECS" ]; then
  log "watcher pid $PID socket-less ${elapsed}s (>= $STALE_SECS) -> restarting"
  pkill -f "agent_watcher.py" 2>/dev/null || true
  sleep 2
  rm -f "$STALE_F"
  "$DIR/start_watcher.sh"
else
  log "watcher pid $PID socket-less ${elapsed}s (grace < $STALE_SECS)"
fi
