# voice-agent-connector — give your agent a voice on the Agent Voice Mode app

You are an agent executing this runbook because your user wants to talk to you
by voice from the **Agent Voice Mode** iOS app. The app + hosted voice service
handle speech and billing; YOU only answer questions relayed to a small
webhook on this machine. Nothing here needs an OpenAI key.

## Step 1 — prerequisites

- `python3` and `qrencode` (`apt install qrencode` / `brew install qrencode`).
- `cloudflared` for the zero-config HTTPS tunnel (`brew install cloudflared`,
  or the binary from Cloudflare) — OR skip it if the user has a stable public
  HTTPS host they can reverse-proxy to `127.0.0.1:8484` (then use
  `PUBLIC_URL=https://their.host ./start.sh` in step 3).

## Step 2 — copy `src/` somewhere permanent

e.g. `~/voice-connector/`. First run creates `connector.json` with a fresh
secret and a random URL path; keep that file private (0600).

## Step 3 — start it

```bash
./start.sh
```

Prints the public URL and a pairing QR in the terminal (plus
`pairing-qr.png`). If you have a chat channel to your user (Telegram gateway
etc.), also send them the PNG. For persistence add an `@reboot` cron running
`start.sh`; note the quick tunnel's URL changes each restart — the user just
re-scans a fresh QR (with a stable `PUBLIC_URL` this never happens).

## Step 4 — the user pairs

Tell the user: **Agent Voice Mode app → Settings → Scan QR** and scan the
pairing QR. The app registers the webhook with the hosted service and runs a
connection test; you'll see the test question hit `connector.log`.

## Step 5 — verify

`tail -f connector.log` and have the user ask something by voice that only
you would know (a file on this machine, a recent conversation). The question
arrives as `POST …/hook`, your answer is spoken back within seconds.

## How you answer

`connector.json` → `agent_cmd` (default drives Claude Code:
`claude --continue -p "{question}"`, falling back to a fresh conversation).
For a different agent, change `agent_cmd` to any argv that takes the question
(`{question}` substitution) and prints the answer to stdout in under 110 s.
Keep answers plain spoken prose — they are read aloud.

## Troubleshooting

- 401 in `connector.log` → the app holds an old secret: re-run `qr.py`,
  re-scan.
- "agent failed" → run the `agent_cmd` from `connector.json` by hand and read
  its stderr.
- No question ever arrives → `curl -s -X POST <url from qr.py --payload> -H
  "Authorization: Bearer <secret>" -d '{"question":"ping"}'` from another
  network; if that works, re-pair the app.
