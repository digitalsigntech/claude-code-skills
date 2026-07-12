#!/usr/bin/env bash
# IDLE watcher WATCHDOG — run every 1 min (cron).
# Verifies the idle_watcher is both (a) alive AND (b) holds an ESTABLISHED Gmail
# IMAP socket on port 993. Restarts it if missing, or if it's been socket-less for
# >= STALE_SECS (a wedged IDLE where the process lives but the TCP connection is
# dead). Silent when healthy.
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # this script's own directory
LOG="$DIR/logs/idle_watchdog.log"
STALE_F="$DIR/logs/.idle_socketless_since"
STALE_SECS=90
mkdir -p "$DIR/logs"
log(){ echo "$(date -Is) $*" >> "$LOG"; }

PID=$(pgrep -f "idle_watcher.py" | head -1 || true)

# (a) process missing -> (re)start. start_idle_watcher.sh has flock + DNS-wait.
if [ -z "$PID" ]; then
  rm -f "$STALE_F"
  log "watcher not running -> starting"
  "$DIR/start_idle_watcher.sh"
  exit 0
fi

# (b) process up -> is there an ESTABLISHED IMAP (993) socket owned by it?
if ss -tnpH state established 'dport = :993' 2>/dev/null | grep -q "pid=$PID,"; then
  rm -f "$STALE_F"          # healthy: clear any grace timer
  exit 0
fi

# socket-less: start/extend a grace timer so a brief reconnect isn't punished.
now=$(date +%s)
if [ -f "$STALE_F" ]; then
  since=$(cat "$STALE_F" 2>/dev/null || echo "$now")
else
  echo "$now" > "$STALE_F"; since=$now
fi
elapsed=$(( now - since ))
if [ "$elapsed" -ge "$STALE_SECS" ]; then
  log "watcher pid $PID socket-less ${elapsed}s (>= $STALE_SECS) -> restarting"
  pkill -f "idle_watcher.py" 2>/dev/null || true
  sleep 2
  rm -f "$STALE_F"
  "$DIR/start_idle_watcher.sh"
else
  log "watcher pid $PID socket-less ${elapsed}s (grace < $STALE_SECS)"
fi
