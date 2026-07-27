# Gmail Chat Agent

Turn a Gmail mailbox into a realtime chat channel for an AI agent — with
owner verification so nobody can impersonate you.

The watcher holds a persistent IMAP `IDLE` connection on the bot's mailbox
(new mail is seen within seconds, no polling). Every verified email from the
**owner** is treated exactly like a chat message: the body is piped into an
agent CLI (default: Claude Code headless, `claude -p`) and whatever the agent
prints is emailed back as a threaded reply. Email from **anyone else** is
never processed and never replied to — but it is reported to a Telegram chat
so the owner always knows what arrived.

Cloud-only by design: everything runs against cloud services (Gmail API, a
cloud agent CLI such as Claude Code, Telegram Bot API). No local model, GPU,
or on-box AI pipeline is required — the host just needs Python and cron.

## Security model

A message only becomes an agent turn if it clears ALL of these gates:

1. **Exact owner match** — the RFC 5322 From *address* (parsed with
   `parseaddr`) must equal one of `OWNER_EMAILS`. Display-name tricks like
   `From: "owner@real.com" <attacker@evil.com>` fail this gate.
2. **DKIM/SPF verification** — the `Authentication-Results` header stamped by
   Gmail itself (authserv-id `mx.google.com`; Gmail strips forged copies per
   RFC 8601 §5) must show `dkim=pass` with `header.d` aligned to the owner's
   domain — or to its Google-default signing domain
   (`<domain-with-dashes>.<selector>.gappssmtp.com`, which Workspaces without
   custom DKIM use; only that Workspace's Google-managed key can sign it). So a mail *claiming* `From: owner@yourdomain.com` sent from an
   attacker's server fails — the attacker cannot produce your domain's DKIM
   signature. Failures are reported to Telegram as a **possible impersonation
   attempt**. (`GCA_ALLOW_SPF_ONLY=1` relaxes to aligned `spf=pass` for the
   rare owner domain without DKIM.)
3. **Loop protection** — bounces, out-of-office replies, `Auto-Submitted`
   mail (RFC 3834), `Precedence: bulk/list`, mailer-daemon, and the bot's own
   address are all dropped. Outgoing replies carry
   `Auto-Submitted: auto-replied` so other well-behaved bots ignore them.
4. **Rate brake** — at most `GCA_MAX_TURNS_PER_HOUR` (default 20) agent turns
   per rolling hour; overflow is reported, not processed.

Everything that fails gate 1 or 2 → **no reply, no agent turn**, one line in
the Telegram report chat (`📧 ignored email from …` / `⚠️ POSSIBLE IMPERSONATION`).

## Files

| File | Role |
|------|------|
| `src/agent_watcher.py` | The watcher: IMAP IDLE loop, security gates, agent turn (stdin→stdout subprocess), threaded Gmail API reply, Telegram reporting, attachment download. |
| `src/config.py` | Accounts, owner list, agent command, limits — all env-overridable. |
| `src/auth.py` | Google OAuth: one-time browser sign-in, token save, silent refresh. |
| `src/start_watcher.sh` | Launcher — `flock` single-instance + DNS-wait (safe as `@reboot` and watchdog entrypoint). |
| `src/watchdog.sh` | Cron watchdog — restarts on dead process or wedged (socket-less ≥ 90 s) IDLE. |

## Prerequisites

- Python 3.8+ with `google-api-python-client`, `google-auth`,
  `google-auth-oauthlib` (launcher assumes a venv at `src/venv/` — edit `PY`
  in `start_watcher.sh` otherwise).
- A Google Cloud project with the **Gmail API** enabled and a **Desktop**
  OAuth client (see `credentials.example.json`).
- An agent CLI that reads a prompt on stdin and prints the reply on stdout
  (default `claude -p`; any equivalent works).
- Optional: a Telegram bot token + chat id for reporting.
- Unix tools for the shell scripts: `flock`, `getent`, `pgrep`, `ss`, `pkill`.

## Setup

1. Save your OAuth client JSON as `src/credentials.json`.
2. Configure (env vars, or edit `src/config.py`):
   - `GCA_AGENT_EMAIL` — the bot's mailbox (e.g. `bot@yourdomain.com`)
   - `GCA_OWNERS` — comma-separated owner addresses; ONLY these are obeyed
   - `GCA_AGENT_CMD` — agent command (default `claude -p`)
   - `GCA_AGENT_CWD` / `GCA_AGENT_TIMEOUT` — agent working dir / timeout (600 s)
   - Telegram reporting: token in `src/bot_token`, chat id in `src/report_chat`
     (or `GCA_BOT_TOKEN` / `GCA_REPORT_CHAT`)
3. Authorize: `cd src && python auth.py agent` → open the URL, sign in as the
   bot address, approve. Writes `token.json` (mode 600).
4. Run it: `./start_watcher.sh`, then install the crons:
   ```cron
   @reboot     /path/to/src/start_watcher.sh
   * * * * *   /path/to/src/watchdog.sh
   ```
5. Test: email the bot from the owner address — a reply lands in the same
   thread in seconds-to-a-minute (agent runtime dominates). Email it from any
   other address — no reply, and the Telegram chat gets a report line.

## How a turn works

1. IDLE push fires → watcher lists recent INBOX messages, skips seen ids.
2. Security gates (above). Attachments are saved to `src/attachments/<msg-id>/`
   and their local paths are included in the prompt.
3. The prompt (owner address, subject, attachment paths, body capped at
   `GCA_BODY_MAX`) is piped to `GCA_AGENT_CMD`; stdout becomes the reply.
4. Reply is sent via the Gmail API into the same thread (`In-Reply-To` /
   `References` set), as multipart plaintext + HTML.
5. First run primes the seen-set: pre-existing inbox mail is never processed.

## Notes

- State lives in `src/state.json` (seen ids + turn timestamps); logs under
  `src/logs/`.
- Each email is an independent agent turn. For conversation memory, point
  `GCA_AGENT_CMD` at something stateful, e.g. `claude -p -c` (continue most
  recent session in `GCA_AGENT_CWD`).
- The agent inherits the watcher's permissions — run it under a user whose
  filesystem access you're comfortable exposing to the owner-by-email channel.
