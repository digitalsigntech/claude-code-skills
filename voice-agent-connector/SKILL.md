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
etc.), also send them the PNG — and because the QR expires in ~15 minutes
(see step 4), **schedule deletion of that chat message at the expiry time**
using whatever scheduler your gateway has (a chat is a terrible place for a
credential to linger, even a short-lived one). For persistence add an
`@reboot` cron running `start.sh`; note the quick tunnel's URL changes each
restart — the user just re-scans a fresh QR (with a stable `PUBLIC_URL` this
never happens).

## Step 4 — the user pairs (one scan, that's everything)

`start.sh` ends by running `qr.py`, which on first run **signs your user up
with the hosted voice service automatically**: it presents this connector's
webhook (the service probes it live before creating anything) and receives
the account — new accounts start with a small welcome credit; talk minutes
are billed by the hosted service, that's their product. The printed QR
carries a **short-lived scan-token (expires in ~15 minutes** — `qr.py` prints
the exact time**)**, so tell the user promptly: **Agent Voice Mode app →
Scan QR** — one scan signs the phone in AND connects you as their agent.
An unscanned QR simply dies (re-run `qr.py` for a fresh one); once scanned
the phone stays signed in permanently. The account's own credential never
leaves `connector.json`. Against an older hosted service that predates
scan-tokens, `qr.py` falls back to the permanent credential and says so —
then the QR must be deleted immediately after scanning.

Re-runs keep the account and just re-sync the webhook URL (quick tunnels get
a new hostname per restart). On the first `qr.py` run pass what you know
about your user — **you are their agent, so you know this**:

- `--name "Alice"` — their display name (the voice greets them with it);
- `--language ru` — the ISO 639-1 code of the language **you and your user
  actually converse in** (not the OS locale — qr.py only falls back to
  `$LANG` if you omit this). It pre-sets the app's language so the very
  first login is already localized.

If signup answers 429, the service's daily signup cap is reached — try again
tomorrow.

Every `qr.py` run also pushes basic profile metadata to the hosted service's
admin dashboard: the user's country (public-IP geolocation of this machine,
locale fallback), the agent type behind this connector (from `agent_cmd`),
and the display name. It is fill-if-empty on the server — never overwrites
what the operator set by hand — and older hosted services without
`POST /profile` are skipped silently.

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
- Scan fails / app says the code is invalid → the QR probably expired
  (~15 min window): re-run `qr.py` and scan the fresh one.
- "agent failed" → run the `agent_cmd` from `connector.json` by hand and read
  its stderr.
- No question ever arrives → `curl -s -X POST <url from qr.py --payload> -H
  "Authorization: Bearer <secret>" -d '{"question":"ping"}'` from another
  network; if that works, re-pair the app.
