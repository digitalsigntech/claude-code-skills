#!/usr/bin/env bash
# TEMPLATE: start one or more long-running local server processes (e.g. a model
# server, an API worker) and wait until each reports healthy.
#
# Pattern demonstrated:
#   - idempotent start(): if a health endpoint already answers, do nothing
#   - nohup + PID file per service so a watchdog / stop script can find it
#   - a bounded health-wait loop so the caller knows when things are actually up
#
# Bind to 127.0.0.1 unless you deliberately want the service reachable off-box.
set -u

# Resolve paths relative to this script so it works wherever the project lives.
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # e.g. ~/myproject
SERVER_BIN="${SERVER_BIN:-$BASE/bin/my-server}"        # the executable to run
LOGS="$BASE/logs"
mkdir -p "$LOGS"

# If the server needs shared libraries alongside its binary, export them:
# export LD_LIBRARY_PATH="$(dirname "$SERVER_BIN"):${LD_LIBRARY_PATH:-}"

[ -x "$SERVER_BIN" ] || { echo "server binary not found at $SERVER_BIN"; exit 1; }

# start NAME PORT [args...] — launch a service if its /health isn't already answering.
start() {
  local name="$1" port="$2"; shift 2
  if curl -s "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
    echo "$name already up on :$port"; return
  fi
  echo "starting $name on :$port ..."
  nohup "$SERVER_BIN" "$@" --host 127.0.0.1 --port "$port" > "$LOGS/$name.log" 2>&1 &
  echo $! > "$LOGS/$name.pid"
}

# --- Declare your services here. Replace the flags with your own. -------------
# Example: a primary service on 18182 and a secondary one on 18183.
start primary   18182 --config "$BASE/primary.conf"
start secondary 18183 --config "$BASE/secondary.conf"
# -----------------------------------------------------------------------------

# Wait for each service's health endpoint (up to 60 x 2s = 2 min each).
echo "waiting for health..."
for name_port in primary:18182 secondary:18183; do
  name="${name_port%%:*}"; port="${name_port##*:}"
  for i in $(seq 1 60); do
    if curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/health" 2>/dev/null | grep -q 200; then
      echo "  $name UP (:$port)"; break
    fi
    sleep 2
    [ "$i" -eq 60 ] && echo "  $name did NOT come up — see $LOGS/$name.log"
  done
done
