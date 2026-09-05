#!/usr/bin/env bash
# Restart the adapter WITHOUT dropping a turn. On 2026-09-04 a plane restart
# under a live session showed the owner "Not connected" and an empty chat, and
# an agent restart mid-turn turns one answer into two (the app resends). Wait
# for the recogniser and synthesiser to be idle and for no hook turn to be in
# flight; refuse after WAIT seconds rather than guess. FORCE=1 overrides.
#   ./restart_agent.sh [unit]      default unit: voice-agent
#   ./restart_agent.sh --check     report only
set -euo pipefail
UNIT="${1:-voice-agent}"; WAIT="${WAIT:-120}"
busy() {
  pgrep -x whisper-cli >/dev/null && { echo "whisper"; return; }
  pgrep -x piper >/dev/null && { echo "piper"; return; }
  # a hook turn in flight: the adapter logs "ask from" / "voice ask" before
  # and "answered in" / "voice turn:" after — a recent open pair means busy
  if command -v journalctl >/dev/null; then
    j="$(journalctl -u "$UNIT" --since '-10min' --no-pager 2>/dev/null | grep -E 'ask from|voice ask|answered in|voice turn:' | tail -1 || true)"
    case "$j" in *"ask from"*|*"voice ask"*) echo "turn"; return;; esac
  fi
  echo ""
}
if [ "${1:-}" = "--check" ]; then b="$(busy)"; echo "busy: ${b:-no}"; exit 0; fi
waited=0
while [ "${FORCE:-0}" != "1" ] && [ -n "$(busy)" ]; do
  if [ "$waited" -ge "$WAIT" ]; then echo "REFUSING: $(busy) in flight after ${WAIT}s (FORCE=1 to override)" >&2; exit 1; fi
  sleep 5; waited=$((waited + 5))
done
systemctl restart "$UNIT" && sleep 2 && systemctl is-active "$UNIT"
