#!/usr/bin/env bash
# Reply Auto-Drafting scan: learn from sent replies, then draft new inquiries.
# Single-instance (flock) so a slow LLM run can't overlap the next cron tick.
set -euo pipefail
# Privacy layer ON: mask customer PII (names/emails/phones/addresses/order nos.)
# to [[TYPE_N]] tokens before any text reaches the cloud LLM; unmask the reply.
# If the NER backend is down or hard PII survives masking, the whole message is
# drafted on the local model instead of the cloud. See privacy.py. Set to 0 to
# send raw text to the cloud LLM (no masking).
export AUTODRAFT_PRIVACY="${AUTODRAFT_PRIVACY:-1}"
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HERE/logs"
exec 9>"$HERE/.lock"
flock -n 9 || { echo "autodraft: already running, skip"; exit 0; }
cd "$HERE"
exec /usr/bin/python3 "$HERE/autodraft.py" >> "$HERE/logs/autodraft.log" 2>&1
