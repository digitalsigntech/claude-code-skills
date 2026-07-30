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
- **Pairing by QR**: `start.sh` brings up an HTTPS tunnel (cloudflared
  quick tunnel, or your own host via `PUBLIC_URL`) and prints a QR the app
  scans — no URLs or secrets typed on a phone.
- **Agent-agnostic**: any CLI that takes a question and prints an answer
  works via `agent_cmd` in `connector.json`.
- **No keys, no voice code**: speech models run hosted-side; nothing here
  touches OpenAI or costs you API money.

Agent-executable install runbook: [`SKILL.md`](SKILL.md). Tell your agent to
follow it, then scan the QR it prints.
