#!/usr/bin/env bash
# Cron entrypoint: activates the project venv and runs the backup, logging output.
# Edit the two paths below (or export them in your crontab) to match your install.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="${BACKUP_VENV_PY:-$HERE/venv/bin/python}"
LOG="${BACKUP_LOG:-$HERE/backup.log}"

mkdir -p "$(dirname "$LOG")"
echo "=== $(date -Is) backup ===" >> "$LOG"
PYTHONWARNINGS="ignore::FutureWarning" "$VENV_PY" \
  "$HERE/backup_to_drive.py" >> "$LOG" 2>&1
echo "=== $(date -Is) done (exit $?) ===" >> "$LOG"
