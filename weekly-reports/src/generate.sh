#!/usr/bin/env bash
# Weekly report GENERATOR. Runs a headless Claude Code agent to research a topic
# and render a dated PDF into this directory. Report-keyed so several reports can
# share one engine. Single-instance per key via flock.
#
# Usage: generate.sh <key>
#   <key> selects a prompt file (generate_prompt_<key>.md, falling back to
#   generate_prompt.md) and names the output PDF. Define your own keys by dropping
#   in a prompt file per key. Optional overrides via reports.config.sh (see the
#   .example): REPORT_PREFIX_<key>, CLAUDE, MODEL, TIMEOUT_SECS.
set -u

# Resolve this script's own directory — no hard-coded absolute paths.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$DIR/logs/generate.log"
mkdir -p "$DIR/logs"

# Optional local config (endpoints, prefixes). Not committed; see the .example.
[ -f "$DIR/reports.config.sh" ] && . "$DIR/reports.config.sh"

# Swappable research backend. Default is the local Claude Code CLI, but you can
# point CLAUDE/MODEL at any Claude-Code-compatible agent runner (see README).
CLAUDE="${CLAUDE:-claude}"
MODEL="${MODEL:-claude-opus-4-8}"
TIMEOUT_SECS="${TIMEOUT_SECS:-2400}"   # 40-min ceiling
DATE="$(date +%F)"

KEY="${1:-default}"

# Per-key prompt file, with a generic fallback.
PROMPT_FILE="$DIR/generate_prompt_$KEY.md"
[ -f "$PROMPT_FILE" ] || PROMPT_FILE="$DIR/generate_prompt.md"
if [ ! -f "$PROMPT_FILE" ]; then
  echo "$(date -Is) [$KEY] no prompt file (looked for generate_prompt_$KEY.md, generate_prompt.md)" >> "$LOG"
  exit 2
fi

# Output PDF name prefix. Override per key with REPORT_PREFIX_<key> in the config.
prefix_var="REPORT_PREFIX_${KEY}"
PREFIX="${!prefix_var:-Report-$KEY}"
OUT="$DIR/$PREFIX-$DATE.pdf"

exec 9>"$DIR/.gen.$KEY.lock"
flock -n 9 || { echo "$(date -Is) [$KEY] generate already running; skip" >> "$LOG"; exit 0; }

echo "=== $(date -Is) [$KEY] generate start -> $OUT ===" >> "$LOG"
PROMPT="$(cat "$PROMPT_FILE")
OUT (write the PDF to EXACTLY this path): $OUT"

# --dangerously-skip-permissions: unattended run, no human to approve tool use.
timeout "$TIMEOUT_SECS" "$CLAUDE" -p "$PROMPT" \
  --model "$MODEL" \
  --dangerously-skip-permissions >> "$LOG" 2>&1
rc=$?

if [ -f "$OUT" ] && [ "$(stat -c%s "$OUT")" -gt 20000 ]; then
  ln -sf "$OUT" "$DIR/$KEY-latest.pdf"
  echo "$(date -Is) [$KEY] generate OK ($(stat -c%s "$OUT") bytes), $KEY-latest.pdf -> $OUT" >> "$LOG"
else
  echo "$(date -Is) [$KEY] generate FAILED (rc=$rc, no valid PDF at $OUT)" >> "$LOG"
  exit 1
fi
