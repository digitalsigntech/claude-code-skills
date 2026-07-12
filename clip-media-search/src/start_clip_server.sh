#!/usr/bin/env bash
# Launch the warm CLIP media-search server (single instance).
# Suitable for @reboot cron + a periodic watchdog (flock makes re-runs a no-op).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE" || exit 1
mkdir -p logs
exec 9>/tmp/clip_server.lock
if ! flock -n 9; then exit 0; fi          # already running -> quiet no-op
export PYTHONWARNINGS="ignore"
PY="$HERE/venv/bin/python"
[ -x "$PY" ] || PY="python3"
echo "$(date '+%F %T') starting clip_server (pid $$)" >> logs/clip_server.log
exec "$PY" pipeline/clip_server.py >> logs/clip_server.log 2>&1
