> **Superseded by [`voice-agent`](../voice-agent/).** That skill does the same job with a supervised tunnel that re-registers when its URL moves, a `health` check that costs no model turn, a configurable `workdir`, and systemd units. Install `voice-agent` instead; this stays only for machines already running it.

# voice-agent-connector

Connect **your own AI agent** to the [Agent Voice Mode] iOS app — talk to it
like a phone call. The app and its hosted voice service do all the speech
(and the billing); this connector is the thin bridge that lets the voice
relay ask *your* agent questions and speak back its answers.

```
you speak ── iOS app ── hosted voice service ──HTTPS──▶ this connector ─▶ your agent
you hear ◀─ iOS app ◀── hosted voice service ◀──JSON─── {"answer": …} ◀──┘
```

- **~150 lines, stdlib-only** `connector.py`: bearer-authenticated webhook,
  runs your agent CLI per question (default: Claude Code with conversation
  continuity), returns the answer.
- **One QR is the whole onboarding**: `start.sh` brings up an HTTPS tunnel
  (cloudflared quick tunnel, or your own host via `PUBLIC_URL`), signs the
  user up with the hosted voice service (which live-probes this webhook
  first), and prints a QR carrying the account — one scan signs the phone in
  with a welcome credit AND connects the agent. Nothing typed on a phone,
  no separate signup anywhere.
- **QRs expire**: each `qr.py` run mints a short-lived scan-token
  (~15 minutes; the exact expiry is printed). Unscanned it dies; the first
  scan redeems it into a permanent sign-in. The account's real credential
  stays in `connector.json` and is never rendered into a QR. If an agent
  posts the QR into a chat, it should schedule deletion of that chat message
  at expiry.
- **Agent-agnostic**: any CLI that takes a question and prints an answer
  works via `agent_cmd` in `connector.json`.
- **No keys, no voice code**: speech models run hosted-side; nothing here
  touches OpenAI or costs you API money.
- **Capability handshake**: the voice service sends
  `{"v":1,"type":"capabilities"}` and this connector answers `["ask"]` — the
  app shows only the features a connection supports. Richer connectors may
  additionally implement `group`, `groups`, `switch_group`, `leave_group`,
  `attachments` (`{token,ts,kind,filename,caption}` items),
  `file` (token → `{b64, content_type, filename}`), `branding`
  (→ `{bot_name, company_name, user_name, user_email?, logo_token?}`),
  `media` (`{query}` → `{items:[{token,kind,caption}]}` KB media search), `photo`
  (`{b64, content_type, caption?}` → `{ok, token, answer?}` — a captioned photo may earn a full agent reply, spoken back to the user) and `reset`
  (clear the agent's conversation context → `{ok}`); unknown types should
  return HTTP 400, which the service treats as "not supported".

Agent-executable install runbook: [`SKILL.md`](SKILL.md). Tell your agent to
follow it, then scan the QR it prints.

## Staying signed in

This skill starts `claude` on its own, alongside whatever else on the machine does
(a chat gateway, a voice adapter, crons, someone's terminal). OAuth refresh tokens
are single-use and every `claude` shares `~/.claude/.credentials.json`, so two
processes refreshing at the same moment sign the whole machine out ("OAuth session
expired", everyone runs `/login` again).

`src/claude_token.py` prevents it: before each spawn it loads a long-lived token
into the child's environment, and that token never refreshes, so it cannot race.
Set it up once per Claude account:

```bash
claude setup-token          # browser sign-in, prints a token valid for one year
echo 'CLAUDE_CODE_OAUTH_TOKEN=<token>' >> ~/.config/<name>/secrets.env && chmod 600 ~/.config/<name>/secrets.env
python3 src/claude_token.py # prints "background token: configured"
```

It is read from `$CLAUDE_SECRETS_ENV` or any `~/.config/*/secrets.env`, on every
turn, so renewing it a year later needs no restart. The same token serves every
machine on that account. With no token the skill still works, it is just exposed
to the race.

