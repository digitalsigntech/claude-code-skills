#!/usr/bin/env bash
# TEMPLATE: socket-aware watchdog for a persistent-connection worker — runs
# every 1 min from cron. A plain "is the process alive?" check is not enough for
# a long-lived socket client: the process can stay up while its TCP connection is
# silently dead (a wedged IDLE/websocket). This watchdog verifies BOTH:
#   (a) the worker process is alive, AND
#   (b) it holds an ESTABLISHED socket to the remote port.
# It restarts on a missing process, or after the socket has been gone for
# >= STALE_SECS (a grace window so a brief reconnect isn't punished). Silent when healthy.
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # project dir, e.g. ~/myproject
LOG="$DIR/logs/watchdog.log"
STALE_F="$DIR/logs/.socketless_since"
STALE_SECS=90
REMOTE_PORT="${REMOTE_PORT:-993}"                      # port the worker keeps open (e.g. 993 IMAPS)
PROC_MATCH="${PROC_MATCH:-watcher.py}"                 # pgrep pattern identifying the worker
LAUNCHER="$DIR/start_watcher.sh"                       # has flock + DNS-wait
mkdir -p "$DIR/logs"
log(){ echo "$(date -Is) $*" >> "$LOG"; }

PID=$(pgrep -f "$PROC_MATCH" | head -1 || true)

# (a) process missing -> (re)start via the flock+DNS-wait launcher.
if [ -z "$PID" ]; then
  rm -f "$STALE_F"
  log "watcher not running -> starting"
  "$LAUNCHER"
  exit 0
fi

# (b) process up -> is there an ESTABLISHED socket to REMOTE_PORT owned by it?
if ss -tnpH state established "dport = :$REMOTE_PORT" 2>/dev/null | grep -q "pid=$PID,"; then
  rm -f "$STALE_F"          # healthy: clear any grace timer
  exit 0
fi

# Socket-less: start/extend a grace timer so a brief reconnect isn't punished.
now=$(date +%s)
if [ -f "$STALE_F" ]; then
  since=$(cat "$STALE_F" 2>/dev/null || echo "$now")
else
  echo "$now" > "$STALE_F"; since=$now
fi
elapsed=$(( now - since ))
if [ "$elapsed" -ge "$STALE_SECS" ]; then
  log "watcher pid $PID socket-less ${elapsed}s (>= $STALE_SECS) -> restarting"
  pkill -f "$PROC_MATCH" 2>/dev/null || true
  sleep 2
  rm -f "$STALE_F"
  "$LAUNCHER"
else
  log "watcher pid $PID socket-less ${elapsed}s (grace < $STALE_SECS)"
fi
