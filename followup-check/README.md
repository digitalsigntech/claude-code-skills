# Follow-up check

A small cron job that emails you a **daily digest of customer inquiries still awaiting a
reply**. It scans your connected mailboxes, finds threads whose latest message is from an
outside customer (and that nobody on your side has answered yet — in any mailbox), asks an
LLM whether each one actually needs a response, and sends you a clean HTML/plain digest of
the ones that do. If nothing is pending, it stays quiet.

## What it does

- Looks at recent inbox threads (default: last 10 days) across every connected mailbox.
- Flags a thread only if the **last** message is from an external sender (not your own
  domain) and isn't a known noise/marketing sender.
- Does **cross-thread reply detection**: if anyone on your side already emailed that person
  after their last message — even under a different subject — the thread is considered
  handled. A teammate's reply counts as yours.
- Runs the survivors through an LLM triage prompt that answers `YES: <reason>` /
  `NO: <reason>` — keeping only messages that genuinely require an action from you (open
  question, quote/price/availability request, order, unresolved issue) and dropping
  thank-yous, "I'll get back to you", newsletters, receipts, etc.
- Emails the pending list (sorted by days waiting; 2+ days flagged red) to the owner.

## How it works

| File | Role |
|------|------|
| `src/followups.py` | The whole tool: scan → cross-thread reply check → LLM triage → build & send digest. Run with `--dry` to print without emailing. |
| `src/followups_run.sh` | Cron wrapper: sets up logging and invokes `followups.py`. |
| `.env.example` | Template for the configurable knobs (see **Config**). |

## Prerequisites

- **Python 3** with `requests` and the Google API client
  (`pip install requests google-api-python-client google-auth google-auth-oauthlib`).
- **The `gmail-multi-mailbox` skill** — this tool imports `gmailer` (Gmail read/search/send
  helpers) and `config.token_path` (per-account OAuth tokens) from it. Place this tool's
  `followups.py` in the same directory as that skill's `gmailer.py` / `config.py` /
  `auth.py`, or add their directory to `PYTHONPATH`. That skill is where you set up the
  OAuth `credentials.json`, per-account `token.json` files, and the `ACCOUNTS` map.
- **An OpenAI-compatible chat endpoint** for the YES/NO triage. Any backend works — a local
  `llama.cpp`/vLLM server or a hosted provider. Configure it via `LLM_URL` / `LLM_MODEL` /
  `LLM_API_KEY`. Nothing is hardcoded to a specific vendor.

## Install / setup

1. Set up the **`gmail-multi-mailbox`** skill first and confirm you can list/send mail for
   your accounts. Note the account **keys** in its `config.ACCOUNTS` (e.g. `primary`,
   `secondary`).
2. Drop `src/followups.py` and `src/followups_run.sh` next to that skill's `gmailer.py` /
   `config.py` (or arrange `PYTHONPATH` so `import gmailer` / `from config import
   token_path` resolve).
3. Copy `.env.example` → `.env` and edit it (or export the same variables in your cron
   environment). At minimum set `OWN_DOMAIN`, `FOLLOWUP_MAILBOXES`, `FOLLOWUP_NOTIFY_TO`,
   `FOLLOWUP_NOTIFY_FROM`, and the `LLM_*` endpoint.
4. Point your LLM endpoint at a running model server.
5. Test it: `python3 src/followups.py --dry` (prints the pending list, sends nothing).
6. Schedule it. The intended cadence is **once every morning at 08:00** (a documented
   default — change freely). Example crontab line:
   ```
   0 8 * * *  /path/to/followup-check/src/followups_run.sh
   ```
   Set `PYTHON=/path/to/venv/bin/python` in the environment if your deps live in a venv.

## Config

All knobs are environment variables (see `.env.example`); code defaults in parentheses.

| Variable | Purpose |
|----------|---------|
| `FOLLOWUP_MAILBOXES` (`primary,secondary`) | Comma-separated account keys to scan; must match `config.ACCOUNTS`. |
| `OWN_DOMAIN` (`example.com`) | Your domain — senders here are "us", never pending. |
| `FOLLOWUP_NOTIFY_TO` (`owner@example.com`) | Digest recipient. |
| `FOLLOWUP_NOTIFY_FROM` (`agent@example.com`) | From address on the digest. |
| `FOLLOWUP_NOTIFY_ACCOUNT` (`primary`) | Account key used to send the digest. |
| `LLM_URL` / `LLM_MODEL` / `LLM_API_KEY` | The OpenAI-compatible triage endpoint. |

In-code (edit `followups.py` directly): `LOOKBACK` (default `newer_than:10d in:inbox
-in:chats`), `MAX_THREADS` (40), `NOISE_SENDER_DOMAINS` (generic marketing/social/automated
senders to skip — add your own), and `CLASSIFY_SYS` (the triage prompt).

## Caveats

- **Bring your own credentials.** No OAuth tokens, `credentials.json`, or mailboxes ship
  here — they live in the `gmail-multi-mailbox` skill. This tool only reads/sends through
  that layer.
- **Swappable model backend.** The triage call is a plain OpenAI-compatible request; point
  it wherever you like. Quality of the digest depends on the model — tune `CLASSIFY_SYS`
  for your business.
- **What was stripped for this generic release:** the original ran against a specific
  company's mailboxes and used a product-specific triage prompt with real product/model
  names as examples. Those were replaced with neutral placeholders, and the account
  selection / recipient / domain / schedule are all config now. The env var the gmail layer
  uses to select an account is set here as `MAIL_ACCOUNT`; if your gmail layer reads a
  different variable name, adjust the two `os.environ[...]` lines in `followups.py`.
- Logs are written to `src/logs/followups.log` at runtime (created on first run; not
  shipped).
