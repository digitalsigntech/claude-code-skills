# Weekly Competitive Reports

Auto-generates a set of research / competitive-analysis reports on a schedule,
renders each to a styled A4 PDF, and distributes them by **email + chat + cloud
drive**. Each report is keyed, so one engine drives any number of topics — you
supply one prompt file per topic. The research step runs a headless AI agent;
delivery is plain Python with no agent dependency.

## How it works

Two scripts, one per phase, both single-instanced with `flock`:

| File | Role |
|---|---|
| `src/generate.sh <key>` | Runs a headless Claude Code agent with `generate_prompt_<key>.md`, which researches the market (WebSearch/WebFetch, optional subagents), builds an HTML report from your style template, and prints it to a dated PDF via headless Chrome. Updates `<key>-latest.pdf`. |
| `src/send.sh` | For every key in `REPORTS`: emails all PDFs in one message, posts each to a chat, and uploads each to its cloud-drive folder. If a report has no PDF fresher than 48h, it regenerates it inline first. |
| `src/generate_prompt.md` | **Generic example prompt.** Copy it to `generate_prompt_<key>.md` per topic and fill in your product/market/sources. Its 5-step structure (research → HTML → print to the exact `OUT` path → `WEEKLY_REPORT_OK` sentinel) is what the scripts depend on. |
| `reports.config.example.sh` | Template for `src/reports.config.sh` (recipients, chat id, drive folder ids, backend). |

Typical cadence: run `generate.sh <key>` for each key one day (e.g. Thursday),
then `send.sh` the next morning (e.g. Friday) via cron.

## Prerequisites

- **Bash**, `flock`, and **headless Chrome/Chromium** (`google-chrome --headless=new`) for PDF rendering.
- A **Claude Code CLI** (or any compatible agent runner) on `PATH`, invoked as
  `claude -p "<prompt>" --model <id> --dangerously-skip-permissions`.
- **Python 3** with `google-api-python-client` for email + drive upload.
- Two small local helper modules you provide (or reuse from companion skills):
  - `gmailer.py` — exposes `gmailer.svc()` returning an authenticated Gmail API service.
  - `tg_api.py` — exposes `send_message(chat_id, text)` and `_call(method, ...)` for a chat/Telegram bot.
  - `gdrive.py` — CLI `gdrive.py upload <file> --folder <id>` that prints `uploaded: ...` on success.
  Point `EMAIL_LIB_DIR`, `TELEGRAM_LIB_DIR`, and `GDRIVE` at wherever these live.

## Install / setup

1. Copy `reports.config.example.sh` → `src/reports.config.sh` and edit it
   (recipients, `CHAT_ID`, `DRIVE_FOLDER` ids, backend, lib paths). This file is
   git-ignored — it holds your private delivery targets; do not commit it.
2. For each report topic, copy `src/generate_prompt.md` →
   `src/generate_prompt_<key>.md`, fill in your product/site/KB path/style
   template, and add `<key>` to `REPORTS` in the config.
3. Provide the credentials your helpers need (Gmail OAuth `token.json` /
   `credentials.json`, chat bot token, drive access). **None are shipped here** —
   bring your own. The uploading account must have Editor access to each
   `DRIVE_FOLDER` id or uploads 404.
4. Test one report end to end: `src/generate.sh <key>` then `src/send.sh`.
   Watch `src/logs/generate.log` and `src/logs/send.log`.
5. Schedule with cron, e.g.:
   ```
   0 15 * * 4  /path/to/src/generate.sh topic-a   # Thu 3pm
   0 16 * * 4  /path/to/src/generate.sh topic-b   # Thu 4pm
   0  7 * * 5  /path/to/src/send.sh                # Fri 7am (regenerates stale ones inline)
   ```

## Config knobs (`reports.config.sh`)

| Var | Meaning |
|---|---|
| `REPORTS` | Space-separated report keys, in delivery order. |
| `CLAUDE`, `MODEL`, `TIMEOUT_SECS` | Research backend command, model id, and per-report time ceiling. |
| `REPORT_PREFIX_<key>` | Output PDF filename prefix for that key. Name it after the product(s) the report covers (both names if one report spans two products) — recipients see the filename. |
| `EMAIL_VENV`, `EMAIL_LIB_DIR`, `GDRIVE` | Python interpreter and helper locations for delivery. |
| `MAIL_TO`, `MAIL_SUBJECT`, `MAIL_BODY` | Email recipients and content. |
| `TELEGRAM_LIB_DIR`, `CHAT_ID` | Chat helper location and target chat id. |
| `DRIVE_FOLDER` | Bash assoc array: report key → cloud-drive folder id. |

## Caveats

- **Swappable research backend.** The default is the local Claude Code CLI with a
  specific model id, but `CLAUDE`/`MODEL` accept any Claude-Code-compatible agent
  runner — bring your own model endpoint. `--dangerously-skip-permissions` is used
  only because the run is unattended; understand the implications before enabling.
- **Bring your own credentials & helpers.** No tokens, OAuth files, mailboxes,
  databases, chat/bot ids, drive folder ids, recipient addresses, or generated
  PDFs are included. The `gmailer` / `tg_api` / `gdrive` helpers are referenced by
  interface only — supply your own (companion Gmail and chat-gateway skills expose
  compatible modules).
- **Drive upload is non-fatal.** If a folder isn't shared/accessible, email + chat
  still deliver and a warning is posted to the chat.
- **Prompts are yours.** Only a single genericized example prompt ships. The real
  product/market/competitor prompts are intentionally omitted — write your own
  `generate_prompt_<key>.md` per topic.
