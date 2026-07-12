#!/usr/bin/env bash
# Stop the local model servers and free GPU memory.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOGS="${LOGS_DIR:-$HERE/logs}"
for name in chat emb; do
  if [ -f "$LOGS/$name.pid" ]; then
    pid=$(cat "$LOGS/$name.pid")
    if kill -0 "$pid" 2>/dev/null; then kill -TERM "$pid" && echo "stopped $name (pid $pid)"; fi
    rm -f "$LOGS/$name.pid"
  fi
done
echo "done."
