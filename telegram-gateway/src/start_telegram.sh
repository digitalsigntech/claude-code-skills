#!/usr/bin/env bash
# Launch the Telegram gateway (single instance). Used by @reboot cron and by hand.
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" || exit 1
mkdir -p logs
export PYTHONWARNINGS="ignore"
export PATH="$HOME/.local/bin:$PATH"   # ensure `claude` is found from cron
# Single-instance lock: if another gateway holds it, exit quietly.
exec 9>state/gateway.lock
if ! flock -n 9; then
  echo "$(date '+%F %T') gateway already running — exiting." >> logs/gateway.log
  exit 0
fi
# Wait for network/DNS before launching (boot can fire this before DNS is up).
# Up to 60 tries x 5s = 5 min; exit quietly if still no network (watchdog cron retries later).
for i in $(seq 1 60); do
  if getent hosts api.telegram.org >/dev/null 2>&1; then
    break
  fi
  if [ "$i" -eq 1 ]; then
    echo "$(date '+%F %T') waiting for network/DNS (api.telegram.org)…" >> logs/gateway.log
  fi
  sleep 5
done
if ! getent hosts api.telegram.org >/dev/null 2>&1; then
  echo "$(date '+%F %T') network still down after wait — exiting, watchdog will retry." >> logs/gateway.log
  exit 0
fi

echo "$(date '+%F %T') starting gateway (pid $$)" >> logs/gateway.log
exec python3 gateway.py >> logs/gateway.log 2>&1
