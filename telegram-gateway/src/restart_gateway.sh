#!/bin/bash
# Safe gateway restart: kill only the python3 gateway process (not any shell whose
# command line merely mentions it), then relaunch via the flock launcher.
#
# Waits for in-flight Claude turns to finish BEFORE killing, because the gateway
# delivers a turn's reply only after its `claude -p` child exits — killing early
# orphans the turn and the reply is silently lost (happened 2026-07-08: /grammar
# removal confirmed 35s after the kill, reply never reached Telegram, and the next
# queued message died with the old process). Run me in the BACKGROUND from inside
# a gateway-spawned turn, or the wait below can only end at the 5-min cap.
#
# MUST be spawned DETACHED (setsid nohup ... &): a plain background job inherits
# the claude turn's process group and dies with it — happened 2026-07-10, the
# doc-reflex restart never ran and the gateway kept serving day-old code.
sleep "${1:-10}"

gwpid=""
for pid in $(pgrep -f "gateway.py"); do
  [ "$(cat /proc/$pid/comm 2>/dev/null)" = "python3" ] && gwpid=$pid && break
done

if [ -n "$gwpid" ]; then
  # Wait (up to 5 min) while the gateway has claude-turn children still running.
  for i in $(seq 1 60); do
    busy=0
    for c in $(pgrep -P "$gwpid" 2>/dev/null); do
      if tr '\0' ' ' < "/proc/$c/cmdline" 2>/dev/null | grep -q "claude"; then
        busy=1; break
      fi
    done
    [ "$busy" = "0" ] && break
    sleep 5
  done
  # Grace period: the reply is sent to Telegram AFTER the claude child exits —
  # give the gateway a moment to parse the result and deliver it.
  sleep 10
fi

for pid in $(pgrep -f "gateway.py"); do
  [ "$(cat /proc/$pid/comm 2>/dev/null)" = "python3" ] && kill "$pid"
done
sleep 2
exec "$(dirname "$0")/start_telegram.sh"
