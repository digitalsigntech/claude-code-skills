#!/usr/bin/env bash
# TEMPLATE: single-instance launcher for a long-running foreground service that
# talks to a remote host (e.g. a bot gateway, a chat bridge, an API poller).
#
# Same script serves THREE roles because of the flock:
#   - @reboot launcher (starts it on boot)
#   - */5 watchdog cron (re-runs it; a no-op if already holding the lock)
#   - manual start from a shell
#
# Pattern demonstrated:
#   - flock single-instance guard (exit quietly if another copy holds the lock)
#   - network/DNS wait before launch, so a fast boot doesn't die on cold DNS
#   - exec the real process so it inherits this shell's PID (clean supervision)
cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1   # project dir, e.g. ~/myproject
mkdir -p logs state
export PYTHONWARNINGS="ignore"
export PATH="$HOME/.local/bin:$PATH"            # ensure user-local tools are found from cron

REMOTE_HOST="${REMOTE_HOST:-api.example-service.com}"  # host to wait for DNS on

# --- Single-instance lock: if another copy holds it, exit quietly. -----------
exec 9>state/gateway.lock
if ! flock -n 9; then
  echo "$(date '+%F %T') gateway already running — exiting." >> logs/gateway.log
  exit 0
fi

# --- Wait for network/DNS before launching (boot can fire before DNS is up). --
# Up to 60 tries x 5s = 5 min; exit quietly if still no network (watchdog retries).
for i in $(seq 1 60); do
  if getent hosts "$REMOTE_HOST" >/dev/null 2>&1; then
    break
  fi
  if [ "$i" -eq 1 ]; then
    echo "$(date '+%F %T') waiting for network/DNS ($REMOTE_HOST)…" >> logs/gateway.log
  fi
  sleep 5
done
if ! getent hosts "$REMOTE_HOST" >/dev/null 2>&1; then
  echo "$(date '+%F %T') network still down after wait — exiting, watchdog will retry." >> logs/gateway.log
  exit 0
fi

# --- Launch the real service in the foreground (holds the flock while alive). -
echo "$(date '+%F %T') starting gateway (pid $$)" >> logs/gateway.log
exec python3 gateway.py >> logs/gateway.log 2>&1
