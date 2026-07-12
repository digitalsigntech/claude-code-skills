#!/usr/bin/env bash
# TEMPLATE: stop the services started by start_servers.sh, using their PID files,
# with a name-based pkill fallback for any stray process.
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; LOGS="$BASE/logs"

for name in primary secondary; do
  if [ -f "$LOGS/$name.pid" ]; then
    pid=$(cat "$LOGS/$name.pid")
    if kill -0 "$pid" 2>/dev/null; then kill -TERM "$pid" && echo "stopped $name (pid $pid)"; fi
    rm -f "$LOGS/$name.pid"
  fi
done

# Fallback: any stray server process launched from this project's binary.
pkill -TERM -f "$BASE/bin/my-server" 2>/dev/null
echo "done."
