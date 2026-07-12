#!/usr/bin/env bash
# Cron entry point: process new emails from the local archive into the KB + contacts DB.
#
# This script assumes the `emails` table has already been (or is being) populated by a
# separate downloader — that mailbox-sync step is out of scope for this skill. Wire your
# own download step in at step (1) if you have one.
set -u

# --- Paths / config (edit these) --------------------------------------------
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # this src/ dir
PYTHON="${EKB_PYTHON:-python3}"                             # or a venv python
LOG="${EKB_LOG:-$SKILL_DIR/logs/cron.log}"
LOCKFILE="$SKILL_DIR/logs/.lock"
mkdir -p "$SKILL_DIR/logs"

# single-instance lock (skip if a previous run is still going)
exec 9>"$LOCKFILE"
if ! flock -n 9; then echo "$(date -Is) another run in progress; skipping." >> "$LOG"; exit 0; fi

# Optional: if you run a LOCAL model server, ensure it's up here (idempotent). Example:
#   if ! curl -s -o /dev/null http://127.0.0.1:8080/health 2>/dev/null; then
#     setsid /path/to/start_servers.sh >> "$LOG" 2>&1 9>&-
#   fi

# Night window = 1:00–4:59 local: run backlog catch-up (whole unprocessed queue).
# Day (5:00–0:59): process only recent mail gently. run.sh sets EKB_MODE by hour.
H=$(date +%H); H=$((10#$H))
if [ "$H" -ge 1 ] && [ "$H" -lt 5 ]; then export EKB_MODE=night; else export EKB_MODE=day; fi

echo "=== $(date -Is) email-KB run (mode=$EKB_MODE) ===" >> "$LOG"

# 1) OPTIONAL: download new mail into the local archive DB (emails table). Plug your own
#    mailbox-sync command in here. Day only (pause at night) if you prefer:
# if [ "$EKB_MODE" = "day" ]; then
#   "$PYTHON" /path/to/download_corpus.py >> "$LOG" 2>&1
# fi

# 2) process from the local archive DB (no live mail) — day=recent, night=backlog catch-up
cd "$SKILL_DIR"
"$PYTHON" process_emails.py >> "$LOG" 2>&1

# 3) OPTIONAL: refresh a semantic index over the KB (separate skill), e.g.
#   "$PYTHON" /path/to/kb_index.py index >> "$LOG" 2>&1

echo "=== $(date -Is) done (exit $?) ===" >> "$LOG"
