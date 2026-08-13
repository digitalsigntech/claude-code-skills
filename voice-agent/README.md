# Voice Agent Adapter

### Installing it? Paste this to your agent

```
Install the voice agent adapter from
https://github.com/vladeasytag/claude-code-skills — clone it, then read and
follow voice-agent/AGENT-INSTALL.md.
```

Connect **your own** Claude Code machine to a voice app, so a spoken question is
answered by an agent that can see your files. The app is the microphone; your
machine is the brain. Nothing about your project leaves it except the answer.

---

## What it does

The voice plane holds one webhook per account. This adapter is that webhook:

- **`ask`** — runs a real Claude turn with `cwd` set to your project, and returns the
  answer. Sessions are per account, so a spoken conversation keeps its thread.
- **`health`** — answers whether this machine's Claude is signed in, from **file reads
  only**. A connection test must never cost a model turn, or the test times out behind
  the very turn it started.
- **`capabilities`** — tells the plane what this agent supports.

Anything else returns HTTP 400, which the plane reads as "ask-only agent" rather than
as a failure. That is how an adapter stays compatible with a plane that grows.

Stdlib only. No dependencies to install.

## Two ways the plane can reach you

The plane calls *you*, so it needs an address. Pick by what your machine has:

| Your machine | Transport | How |
|---|---|---|
| Public IP + HTTPS (VPS, cloud box) | direct | Reverse-proxy `/voice` to `127.0.0.1:8787`, then `pair.py --url https://you/voice` |
| **No public IP** (laptop, home server, office appliance) | **tunnel** | `tunnel.py` — an outbound Cloudflare tunnel gets you a public HTTPS URL and pairs it |

The tunnel case is the normal one. Nothing listens on a public address, no router or
firewall is touched, and the connection is outbound only — the same shape as any
app phoning home.

A quick tunnel's URL changes whenever it restarts, so `tunnel.py` is a supervisor: it
watches the tunnel and **re-registers the new URL with the plane automatically**. That
is the difference between this and a one-line `cloudflared` command that works until
the first restart. For a permanent address, use a Cloudflare *named* tunnel and pass
its hostname to `pair.py --url` instead.

## Install

1. **Place the code**, e.g. `/opt/voice-agent/`.
2. **Point it at your project** — copy `config.example.json` to `config.json` and set
   `workdir` to the directory the agent should work in. This is the whole value of the
   thing: the agent answers out of *those* files.
3. **Start it** (`voice-agent.service.example` is a systemd unit):

       python3 voice_agent.py

   A `secret` is generated on first run — that is the bearer the plane must present.
4. **Get your account token** from the app (Settings, or the pairing QR).
5. **Pair**:

       # public machine
       python3 pair.py --api https://<plane>/api/ --token <token> --url https://you/voice

       # no public IP
       python3 pair.py --api https://<plane>/api/ --token <token>   # saves them
       python3 tunnel.py                                            # URL + pairing

6. **Verify** — from the plane's side, not yours:

       python3 pair.py --test

## Verifying, and the two failures that look identical

`pair.py --test` asks the plane to test *it → you*. It distinguishes:

- **unreachable** — nothing answered. Wrong URL, agent down, tunnel dead.
- **signed_out** — you answered, and said Claude here is logged out.

They have opposite remedies, which is why one boolean was never enough. Re-pairing
fixes the first and does nothing for the second: fix that one **on this machine**, by
running `claude` in a terminal here and logging in.

## Running as a service

Both unit templates set an explicit `PATH`. A service manager does not inherit your
login `PATH`, so without it the adapter starts cleanly and then fails on *every* turn
with a command-not-found for `claude` — invisible unless you read the logs.

    systemctl status voice-agent
    journalctl -u voice-agent -f

## Security

- The plane must present the generated `secret` as a bearer; everything else gets 401.
- Bind stays on `127.0.0.1`. Public exposure is the reverse proxy's or the tunnel's job.
- **Turns run with permissions bypassed** — the agent can read, write and execute in
  `workdir`. Point it at a project you would let an agent work in unattended.
- Running as root, the CLI refuses `--dangerously-skip-permissions`; the adapter sets
  `IS_SANDBOX=1` and falls back to a prompt-limited turn rather than failing. Prefer a
  non-root user where you can.
- `config.json` holds your account token and the webhook secret: `chmod 600`, never
  commit it.

## Files

| File | What it is |
|---|---|
| `src/voice_agent.py` | The webhook server. Protocol, health, turns. |
| `src/pair.py` | Register with the plane; `--test`, `--status`. |
| `src/tunnel.py` | Public URL for machines behind NAT; re-pairs on URL change. |
| `src/config.example.json` | Copy to `config.json`. |
| `src/*.service.example` | systemd units for the adapter and the tunnel. |
