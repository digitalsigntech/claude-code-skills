# Voice Agent Adapter

### Installing it? Paste this to your agent

```
Install the voice agent adapter from
https://github.com/digitalsigntech/claude-code-skills — clone it, then read and
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
- **`branding`** — the identity panel the app shows: your name, your company, what the
  agent is called, and a logo. **Derived, not configured** — see below.
- **`file`** — serves the logo's bytes back when the plane asks for the token branding
  handed it. Only paths this process minted a token for; nothing else is readable.
- **`progress`** — what the agent is working on right now, without a model. The app
  probes this to decide whether an agent is *there*: refuse it and the connect button
  is drawn crossed out on a machine that is answering fine.
- **`history`** — the conversation, so the app's chat is populated on launch rather
  than blank. From the machine's message archive if it has one, otherwise from its own
  Claude Code session transcripts — see below. Nothing is kept in parallel to sync.
- **`photo` / `photos`** — a picture taken in the app, stored on this machine, written
  into the archive as a message, and mirrored into your chat. A caption runs as a real
  turn with the saved paths named, so "remind me at 5:30 to analyse this sample" reaches
  an agent that can act on it instead of dying with the upload.
- **`attachments`** — the files this agent holds, newest last, each with a token the app
  fetches through `file`. **Only files that made it into the archive are listed**: the app
  treats presence in this feed as presence in the conversation, and it is right to.
- **`capabilities`** — tells the plane what this agent supports, from what is actually
  configured. Claiming `branding` and then 404-ing it makes the plane ask a question it
  already knows the answer to.

### What `posted` means, and why it is worth being strict about

An upload answers with `posted`, and the app draws a delivery tick from it. It is true only
when the file is somewhere **the user can actually open** — stored, recorded as a message,
and fetchable. If a Telegram gateway is installed (see below) that also means Telegram
accepted it; without one, the app's own chat is the destination. A failed send writes
`[NOT delivered to the chat]` into the archive row, so the written record cannot disagree
with the tick on screen.

This is stricter than it looks and deliberately so. On 2026-08-13 three photos were
uploaded to an agent that did not implement `photo`; the app showed an error *and* ticks
beside them, and it took an afternoon across two machines to establish that nothing had
arrived anywhere. A flag that means "handed over" rather than "delivered" is a flag that
will eventually lie.

### If the machine has a Telegram gateway

Voice traffic mirrors into it: the spoken question labelled with the speaker's name, the
agent's answer, and uploads as real photos or documents. Archive rows are filed under the
**Telegram chat id** rather than a separate pseudo-chat, so the app's history and the chat
are one conversation instead of two half-records of the same one.

The destination is resolved, never hardcoded — `telegram_chat` in `config.json`, then the
gateway's own owner id, then a single-entry allowlist. More than one candidate and it stays
unset rather than guessing: guessing here posts one person's conversation into another
person's window.

Plus `qr_spent` (a scanned login QR is deleted the moment it is redeemed), and
`reset`, `log`, `attachments`, `photos` for the chat-side features. Anything else
returns HTTP 400, which the plane reads as "this agent does not do that" rather than
as a failure — that is how an adapter stays compatible with a plane that grows.

**Check yourself against the protocol rather than waiting to be told:**

    python3 conformance.py

One request per message type, with what breaks in the app when each is missing, and
a non-zero exit if anything required is unanswered. This exists because the first
version of this adapter was extracted by copying a working box's SHAPE and not its
SURFACE — and every gap that left was then found from the outside, by someone looking
at a phone.

Stdlib only. No dependencies to install.

## Identity works itself out

After the QR is scanned the app shows who it is talking to: the user's name, the
company, what the agent is called, and a logo. None of that is typed in.

On startup — and again before pairing, so the very first scan is already right — the
adapter spends one turn asking the agent to describe itself **from this project's
files**, and caches the answer (`state.json`, re-derived weekly). The agent's own name
is usually in `CLAUDE.md`, the company in its documents, the user in the work they do
together; the logo is found on disk. The account is named from the same answer — at
signup, and re-asserted on every pairing, so a name that was right when the account
was created cannot quietly stay wrong afterwards. (Nobody ends up with an account
called `root`, or one named after their company where their own name belongs.)

**Your agent's own name comes from how you address it.** It is the one identity fact
nobody writes down — "you are <name>" appears in no file on the machine of an agent
everyone calls by that name, because a name is established by being used. So the
adapter counts the names its user has used in an
address position in this project's own session logs, ranks them by how many separate
sessions they recur across, and offers those to the agent as evidence with the sample
lines attached. A name that spikes once inside a pasted email loses to one the user
keeps coming back to; a log full of colleagues and no agent name yields nothing rather
than a colleague's name. The agent can only pick a name that was really said.

**Every other derived value must appear in the project's own files or it is dropped.** Asked
who its user is, an agent with nothing to read will happily answer from the account the
CLI is signed in as — which is a real person with no connection to the install. A name
the project never writes down does not go on the panel; the app falls back to its
generic one, which is the honest look for a machine that has nothing to say about
itself.

It costs a model turn, so it never happens while the plane is waiting: `branding` is
served from cache in about a millisecond.

    python3 voice_agent.py --identity     # show what it worked out (re-derives)
    python3 pair.py --identity            # same, from the pairing side

To override any field — a nickname the files do not use, a different logo — set
`agent_name`, `company_name`, `user_name`, `user_email` or `logo` in `config.json`.
Config wins for everything the app *displays about your agent*.

**The person's name is the exception: the derived value wins.** It is what the account
is named and what a login QR carries, and it has to be able to change. An install that
guessed wrong once would otherwise pin that guess forever — every later correction
overwritten by the oldest value in the system. `--name` still overrides, and is stored
under `user_name` where a deliberate choice belongs.

## Where history comes from

Two sources, in order, and both are the real thread rather than a copy:

1. **A message archive**, if the machine has one — the [`chat-archive`](../chat-archive/)
   component. That is one timeline across every channel the agent talks on, so a
   conversation that happened in chat this morning is there when the phone asks about
   it tonight. Voice turns are written into it as they happen, which is what makes the
   timeline whole rather than a second stream beside it.
2. **Claude Code's own session transcripts** (`~/.claude/projects/<project>/`) — every
   machine has these, so this is the floor, not a fallback to apologise for. Voice
   turns are resumed sessions in the same project, so the app's own conversation is in
   there too.

Detected, not configured: an archive is used if `chatlog/chatdb.py` sits in your
workdir (`archive_dir` in `config.json` overrides). Either way the app polls this
every few seconds, so reads tail the newest data rather than scanning from the front,
and skip the machinery — thinking, tool calls, tool results — that the user never saw.

## The QR deletes itself

A login QR is a credential with a clock on it, and the fastest way to get one onto a
phone is to post it into the chat you already share with your agent. The code dies
server-side at expiry; the image does not, and an expired credential in a chat looks
exactly like a live one.

    python3 pair.py --qr --telegram <chat id>

Sending it and taking it back are one operation: the send records the message, and
the deletion is swept every 30 seconds by the adapter — the one process on the
machine still running a quarter of an hour later, when the installing agent has
finished and `pair.py` has long exited. `pair.py --qr` sweeps too, so a QR posted by
a run that died is still collected. Both are idempotent; a message already gone
counts as deleted.

The bot token is found, not configured: `$TELEGRAM_BOT_TOKEN`, `$TG_BOT_TOKEN`, or a
`telegram/bot_token` file in your workdir — where an agent that already talks over
Telegram keeps it. Set `telegram_chat` in `config.json` to make it the default.

Any other channel: show `pairing-qr.png` however you like — but then the expiry is
yours to honour.

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
the first restart. A fresh hostname also takes ~10–30 s to resolve from the plane's
side, so registering it retries instead of accepting the first "does not resolve" —
otherwise every tunnel restart leaves the agent silently unreachable. For a permanent
address, use a Cloudflare *named* tunnel and pass its hostname to `pair.py --url`.

## Install

1. **Place the code**, e.g. `/opt/voice-agent/`.
2. **Point it at your project** — copy `config.example.json` to `config.json` and set
   `workdir` to the directory the agent should work in. This is the whole value of the
   thing: the agent answers out of *those* files.

   That is the only thing you have to decide. The identity panel works itself out
   (below).
3. **Start it** (`voice-agent.service.example` is a systemd unit):

       python3 voice_agent.py

   A `secret` is generated on first run — that is the bearer the plane must present.
4. **Pair.** If you already have an account, its token is in the app (Settings, or the
   pairing QR). If you do not, the agent creates one — you need nothing beforehand:

       # public machine, existing account
       python3 pair.py --token <token> --url https://you/voice

       # public machine, no account yet
       python3 pair.py --signup --url https://you/voice

       # no public IP — the tunnel does both, in the only order that works
       python3 tunnel.py                  # URL, then signup or re-pair

   The plane address is already set: these scripts default to the service behind the
   Agent Voice Mode app. Pass `--api https://<host>/api/` only to use a different
   deployment.

   The plane **probes the webhook before it creates an account**, so the account
   cannot exist before the URL does. That is why a NAT machine signs up from inside
   `tunnel.py` and not before it.

5. **Sign the phone in** — `python3 pair.py --qr`. One scan signs the app in *and*
   the agent is already attached. The code carries a short-lived scan-token (~15 min,
   dies unscanned), never the stored account credential. `qrencode` renders it in the
   terminal and to `pairing-qr.png`; without it the payload is printed raw so the
   install is never blocked on a rendering tool.

   If your agent talks to you over Telegram, `--telegram <chat id>` posts it there and
   **deletes it when it expires** — see below.

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
| `src/voice_agent.py` | The webhook server. Protocol, health, turns, identity panel. |
| `src/pair.py` | Sign up or register with the plane; `--qr`, `--test`, `--status`. |
| `src/tunnel.py` | Public URL for machines behind NAT; signs up on first run, re-pairs on URL change. |
| `src/qr_send.py` | Posts the QR to Telegram; deletes it when scanned, or at expiry. |
| `src/conformance.py` | Checks this install against every message the plane can send. |
| `src/config.example.json` | Copy to `config.json`. |
| `src/*.service.example` | systemd units for the adapter and the tunnel. |
