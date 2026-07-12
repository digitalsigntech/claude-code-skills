# Email IDLE Watcher

Event-driven (push) new-mail watcher for a Gmail account. It holds a persistent
IMAP IDLE connection so new mail is seen within seconds — no polling — and drops
each qualifying message as a JSON job into a queue directory for a downstream
consumer (for example, a chat gateway that runs the email as a chat turn). A
watchdog wrapper keeps the connection alive and restarts it if it wedges.

## What it does

- Keeps a persistent Gmail IMAP `IDLE` connection open (XOAUTH2, reusing the
  OAuth access token). The server pushes a notification the instant new mail
  lands in the INBOX.
- On each wake it lists recent INBOX messages and acts only on ones from an
  **allow-listed set of senders** — everything else is ignored.
- It filters out automated noise (bounces, out-of-office / vacation auto-replies,
  bot error notices) via RFC 3834 headers, sender heuristics, and text patterns,
  so those never trigger a downstream turn.
- Qualifying messages are written atomically as `<ts>-<msgid>.json` into a queue
  directory. Each job carries `{id, from, subject, body, chat_id, ts}`.
- An optional `notify()` helper can ping a chat bot (a Telegram Bot API endpoint
  is shown as an example — swap for any backend).

## How it works

| File | Role |
|------|------|
| `src/idle_watcher.py` | The watcher. Persistent IMAP IDLE loop, new-mail detection, sender allow-list, auto-noise filter, atomic queue enqueue. |
| `src/start_idle_watcher.sh` | Launcher. `flock` single-instance guard (so it's safe as both an `@reboot` starter and a watchdog entrypoint) + DNS-wait for slow boots. |
| `src/idle_watchdog.sh` | Watchdog (run every 1 min via cron). Restarts the watcher if the process is missing, or if it has held no ESTABLISHED port-993 socket for ≥ 90s (a wedged IDLE). Silent when healthy. |
| `src/auth.py` | Account-aware Google OAuth: one-time browser sign-in, token save, silent refresh. |
| `src/config.py` | Accounts, OAuth scopes, token/credential paths. |

The watcher renews IDLE every 5 min (Gmail drops idle connections around 29 min)
and does a full reconnect every 25 min to pick up a fresh access token. On each
IDLE renewal it also does a cheap safety-net poll, so a dropped push still gets
caught.

## Prerequisites

- Python 3.8+
- Python packages: `google-api-python-client`, `google-auth`,
  `google-auth-oauthlib` (a `venv` at `src/venv/` is assumed by the launcher —
  edit `PY` in `start_idle_watcher.sh` to point at your interpreter).
- A Google Cloud project with the **Gmail API** enabled and a **Desktop** OAuth
  client.
- Standard Unix tools used by the shell scripts: `flock`, `getent`, `pgrep`,
  `ss`, `pkill`.

## Install / setup

1. **Create OAuth credentials.** In Google Cloud, enable the Gmail API and create
   a Desktop OAuth client. Download the client JSON and save it as
   `src/credentials.json` (see `credentials.example.json` for the shape).
2. **Set your accounts.** Edit `src/config.py` — set the `ACCOUNTS` addresses.
   The watcher listens on the `"agent"` account.
3. **Authorize.** From `src/`, run `python auth.py agent`, open the printed URL on
   this machine's browser, sign in as the agent address, and approve. A
   `token.json` is written (mode 600). Re-running refreshes it.
4. **Choose your allow-list and queue.** Set the env vars below (or edit the
   defaults at the top of `idle_watcher.py`).
5. **(Optional) notify target.** Copy `idle_notify_chat.example` to
   `src/idle_notify_chat` and put your chat/route id in it; put your bot token in
   the file referenced by `IDLE_BOT_TOKEN_F`.
6. **Run it.** `./src/start_idle_watcher.sh` (foreground-execs the watcher under
   `flock`). For production, add cron entries:

   ```cron
   @reboot           /path/to/src/start_idle_watcher.sh
   * * * * *         /path/to/src/idle_watchdog.sh
   ```

## Config

Environment variables (all optional; defaults derived from the script location):

| Var | Default | Meaning |
|-----|---------|---------|
| `IDLE_INJECT_DIR` | `../queue` | Directory jobs are dropped into. |
| `IDLE_BOT_TOKEN_F` | `../gateway/bot_token` | File holding a chat-bot API token (for the optional `notify()`). |
| `IDLE_ALLOWED` | `owner@example.com,peer@example.com` | Comma-separated allow-listed sender addresses (substring match). |
| `IDLE_DEFAULT_CHAT` | `123456789` | Fallback chat/route id stamped on jobs and used for notifications. |

In-file knobs (top of `idle_watcher.py`): `BODY_MAX` (email body cap fed
downstream), `IDLE_RENEW`, `RECONNECT_EVERY`, `SEEN_CAP`. Watchdog grace window:
`STALE_SECS` in `idle_watchdog.sh`.

Files the watcher writes at runtime (not shipped): `src/idle_state.json`
(seen-message ids), `src/logs/idle_watcher.log`, `src/logs/idle_watchdog.log`,
`src/idle_notify_chat` (your chat id).

## Caveats

- **Bring your own credentials.** No tokens or OAuth secrets are included. You must
  supply your own `credentials.json` and generate `token.json` locally. `token.json`,
  `idle_state.json`, and `credentials.json` are never distributed.
- **Swappable notify / consumer backend.** The queue is just JSON files in a
  directory — wire up any consumer. The example `notify()` uses a Telegram Bot API
  URL; replace it with whatever chat/webhook backend you use. Nothing downstream
  is bundled here.
- **What was genericized / stripped.** Company, owner, and peer-agent names, real
  email addresses/domains, Telegram user/chat ids, the bot token path, and the
  original hardcoded queue path (`telegram/inject`) were all replaced with neutral
  placeholders or made configurable. The allow-list and default chat are now env
  vars. The auto-noise filter's text patterns are kept as-is (generic bounce/OOO
  phrasing plus a generic "model returned empty content" pattern).
- **Gmail specifics.** IDLE timing and the XOAUTH2 handshake are tuned for
  `imap.gmail.com`. Another IMAP provider may need different renew/reconnect
  intervals.
