# reports.config.example.sh — copy to src/reports.config.sh and fill in.
# Sourced by both generate.sh and send.sh. Never commit the filled-in copy
# (it holds recipient addresses, chat ids, and drive folder ids).

# ---- Research backend (swappable) -------------------------------------------
# Any Claude-Code-compatible agent runner invoked as: CLAUDE -p "<prompt>" --model MODEL
CLAUDE="claude"                        # path to your Claude Code CLI, or a wrapper
MODEL="claude-opus-4-8"                # model id your CLI accepts
TIMEOUT_SECS=2400                      # per-report generation ceiling (seconds)

# ---- Reports -----------------------------------------------------------------
# Space-separated report keys, in delivery order. One prompt file per key:
# generate_prompt_<key>.md (falls back to generate_prompt.md).
REPORTS="topic-a topic-b"

# Output PDF filename prefix per key (REPORT_PREFIX_<key>). Optional.
# Name it after the product(s) the report covers (a key spanning two products
# gets both names, e.g. "ProductA-ProductB-Competitive-Analysis") — recipients
# see the filename, so a generic company-wide prefix is ambiguous.
REPORT_PREFIX_topic_a="TopicA-Competitive-Analysis"
REPORT_PREFIX_topic_b="TopicB-Competitive-Analysis"

# ---- Delivery: email ---------------------------------------------------------
# Reuses a local Gmail helper exposing gmailer.svc(). See README for the interface.
EMAIL_VENV="python3"                   # python with google-api-python-client + your helpers
EMAIL_LIB_DIR="/path/to/your/lib"      # dir containing gmailer.py
GDRIVE="/path/to/your/lib/gdrive.py"   # drive upload helper: upload <file> --folder <id>
MAIL_TO="owner@example.com, Second User <user2@example.com>"
MAIL_SUBJECT="Weekly Competitive Analysis"
MAIL_BODY="Hi,

Attached are this week's competitive-analysis reports. Full sources and
methodology are inside each PDF.

- automated report bot
"

# ---- Delivery: chat (Telegram-style helper exposing send_message / _call) -----
TELEGRAM_LIB_DIR="/path/to/your/lib"   # dir containing tg_api.py
CHAT_ID=123456789                      # target chat/group id

# ---- Delivery: cloud drive ---------------------------------------------------
# One folder id per report key. Each folder must be shared with the uploading
# account as Editor, else uploads 404. Keys must match REPORTS above.
declare -A DRIVE_FOLDER=(
  [topic-a]="<YOUR_DRIVE_FOLDER_ID>"
  [topic-b]="<YOUR_DRIVE_FOLDER_ID>"
)
