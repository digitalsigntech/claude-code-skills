#!/usr/bin/env bash
# Weekly report SENDER. Emails all configured report PDFs (one message, PDFs
# attached) to a recipient list, posts each to a chat, and uploads each to a
# cloud-drive folder. Plain Python (no agent dependency at send time).
# Fallback: any report with no fresh PDF (generator failed earlier) is
# regenerated inline before sending.
#
# Bring-your-own helpers (see README): this reuses two small local modules —
#   * a Gmail sender exposing gmailer.svc()  -> googleapiclient service
#   * a chat/Telegram helper exposing tg_api.send_message() / tg_api._call()
# Point EMAIL_LIB_DIR / TELEGRAM_LIB_DIR / EMAIL_VENV / GDRIVE at yours.
set -u

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$DIR/logs/send.log"
mkdir -p "$DIR/logs"

# ---- Config (edit reports.config.sh; see reports.config.example.sh) ----------
[ -f "$DIR/reports.config.sh" ] && . "$DIR/reports.config.sh"

REPORTS="${REPORTS:-default}"                       # space-separated report keys, in order
EMAIL_VENV="${EMAIL_VENV:-python3}"                 # python with google-api client + your helpers
EMAIL_LIB_DIR="${EMAIL_LIB_DIR:-$DIR/lib}"          # dir containing gmailer.py
TELEGRAM_LIB_DIR="${TELEGRAM_LIB_DIR:-$DIR/lib}"    # dir containing tg_api.py
GDRIVE="${GDRIVE:-$DIR/lib/gdrive.py}"              # drive upload helper (upload <file> --folder <id>)
MAIL_TO="${MAIL_TO:-owner@example.com, Second User <user2@example.com>}"
MAIL_SUBJECT="${MAIL_SUBJECT:-Weekly Competitive Analysis}"
MAIL_BODY="${MAIL_BODY:-Hi,

Attached are the latest competitive-analysis reports. Full sources and
methodology are inside each PDF.

- automated report bot
}"
CHAT_ID="${CHAT_ID:-123456789}"                     # chat/group id for posting
# Cloud-drive destination folder per report key. Each folder must be shared with
# the uploading account as Editor, else uploads 404. Set in reports.config.sh:
#   declare -A DRIVE_FOLDER=( [default]="<YOUR_DRIVE_FOLDER_ID>" )
declare -A DRIVE_FOLDER 2>/dev/null || true
[ "${#DRIVE_FOLDER[@]}" -eq 0 ] && DRIVE_FOLDER=( [default]="<YOUR_DRIVE_FOLDER_ID>" )
# -----------------------------------------------------------------------------

exec 9>"$DIR/.send.lock"
flock -n 9 || { echo "$(date -Is) send already running; skip" >> "$LOG"; exit 0; }

echo "=== $(date -Is) send start (reports: $REPORTS) ===" >> "$LOG"

PDFS=()
for k in $REPORTS; do
  pdf="$DIR/$k-latest.pdf"
  # Fresh = regenerated within 48h. If stale/missing, regenerate this report inline.
  if [ ! -e "$pdf" ] || [ -z "$(find "$pdf" -mmin -2880 2>/dev/null)" ]; then
    echo "$(date -Is) [$k] no fresh PDF — running generator inline as fallback" >> "$LOG"
    "$DIR/generate.sh" "$k" >> "$LOG" 2>&1
  fi
  if [ -e "$pdf" ]; then
    PDFS+=( "$(readlink -f "$pdf")" )
  else
    echo "$(date -Is) [$k] WARNING: no report available to send" >> "$LOG"
  fi
done

if [ "${#PDFS[@]}" -eq 0 ]; then
  echo "$(date -Is) ABORT: no reports to send" >> "$LOG"
  TELEGRAM_LIB_DIR="$TELEGRAM_LIB_DIR" CHAT_ID="$CHAT_ID" python3 - <<'PY' >> "$LOG" 2>&1
import os, sys
sys.path.insert(0, os.environ["TELEGRAM_LIB_DIR"])
import tg_api
tg_api.send_message(int(os.environ["CHAT_ID"]),
  "Weekly reports could not be generated this week — nothing to send. Check logs/.")
PY
  exit 1
fi

echo "$(date -Is) sending ${#PDFS[@]} report(s): ${PDFS[*]}" >> "$LOG"

REPORT_PDFS="${PDFS[*]}" MAIL_TO="$MAIL_TO" MAIL_SUBJECT="$MAIL_SUBJECT" \
MAIL_BODY="$MAIL_BODY" CHAT_ID="$CHAT_ID" \
EMAIL_LIB_DIR="$EMAIL_LIB_DIR" TELEGRAM_LIB_DIR="$TELEGRAM_LIB_DIR" \
  "$EMAIL_VENV" - <<'PY' >> "$LOG" 2>&1
import os, base64, sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
sys.path.insert(0, os.environ["EMAIL_LIB_DIR"])
import gmailer

PDFS = os.environ["REPORT_PDFS"].split()
TO   = os.environ["MAIL_TO"]
SUBJ = os.environ["MAIL_SUBJECT"]
BODY = os.environ["MAIL_BODY"]

svc = gmailer.svc()
msg = MIMEMultipart(); msg["To"] = TO; msg["Subject"] = SUBJ
msg.attach(MIMEText(BODY))
for p in PDFS:
    with open(p, "rb") as f:
        part = MIMEApplication(f.read(), _subtype="pdf")
    part.add_header("Content-Disposition", "attachment", filename=os.path.basename(p))
    msg.attach(part)
raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
res = svc.users().messages().send(userId="me", body={"raw": raw}).execute()
print("EMAIL sent id=", res["id"], "attachments=", len(PDFS))

# Post each report to the chat
sys.path.insert(0, os.environ["TELEGRAM_LIB_DIR"])
import tg_api
chat_id = int(os.environ["CHAT_ID"])
for p in PDFS:
    with open(p, "rb") as f:
        r = tg_api._call("sendDocument",
                         _files={"document": (os.path.basename(p), f, "application/pdf")},
                         chat_id=chat_id,
                         caption="Weekly Competitive Analysis — emailed to the recipient list.")
    print("CHAT", os.path.basename(p), "ok=", r.get("ok") if isinstance(r, dict) else r)
PY
rc=$?

# --- Cloud drive: post each report to its designated folder (non-fatal) -------
for k in $REPORTS; do
  pdf="$DIR/$k-latest.pdf"; fid="${DRIVE_FOLDER[$k]:-}"
  [ -e "$pdf" ] || continue
  [ -n "$fid" ] || { echo "$(date -Is) [$k] no Drive folder configured; skip" >> "$LOG"; continue; }
  out="$("$EMAIL_VENV" "$GDRIVE" upload "$(readlink -f "$pdf")" --folder "$fid" 2>&1)"
  if printf '%s' "$out" | grep -q "uploaded:"; then
    echo "$(date -Is) [$k] Drive upload OK: $(printf '%s' "$out" | grep 'uploaded:')" >> "$LOG"
  else
    echo "$(date -Is) [$k] Drive upload FAILED: $(printf '%s' "$out" | tail -1)" >> "$LOG"
    TELEGRAM_LIB_DIR="$TELEGRAM_LIB_DIR" CHAT_ID="$CHAT_ID" python3 - "$k" "$fid" <<'PY' >> "$LOG" 2>&1
import os, sys
sys.path.insert(0, os.environ["TELEGRAM_LIB_DIR"])
import tg_api
k, fid = sys.argv[1], sys.argv[2]
tg_api.send_message(int(os.environ["CHAT_ID"]),
  f"Weekly report '{k}' emailed OK but Drive upload failed — folder {fid} may not be "
  f"shared with the uploading account (Editor). Reports still delivered by email/chat.")
PY
  fi
done

echo "=== $(date -Is) send done (rc=$rc) ===" >> "$LOG"
exit $rc
