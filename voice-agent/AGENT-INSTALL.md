# Agent install brief

You are an agent installing this voice adapter on the machine you are running on.
The operator handed you this file so they would not have to type the instructions.

Read `README.md` in this directory **in full** first. This file adds the judgement
calls and the traps; it does not replace it.

When you are done, the operator will be able to speak to the app on their phone and
get an answer out of **this machine's files**.

---

## 1. Which directory should the agent work in

`workdir` in `config.json` is where every voice turn runs. It is the whole point of
the install: a turn that runs somewhere empty can only answer from general knowledge,
which the phone could have done without you.

Work it out before you ask — nobody can put it in this file:

```bash
ls -la ~
find ~ -maxdepth 3 -name CLAUDE.md -not -path '*/.*' 2>/dev/null
find ~ -maxdepth 3 -name '.git' -type d 2>/dev/null
```

A directory with a `CLAUDE.md` is a strong signal — read it, it usually says what the
machine is for. Your own session's directory is a good default. **State your choice and
why, and let the operator confirm before you continue.** Do not silently pick.

## 2. Decide the transport — this is where installs fail

The plane calls the agent, so it needs an address that resolves from the public
internet. Check what this machine actually has; do not assume.

```bash
curl -s --max-time 10 https://api.ipify.org ; echo        # our egress address
ip -4 addr show scope global | grep -oP 'inet \K[\d.]+'   # our local addresses
```

- **The two match, and something already terminates HTTPS here** (Caddy, nginx,
  Traefik): use the direct transport. Add a route that proxies to `127.0.0.1:8787`,
  then pair with that URL. Do not open the adapter's port to the world directly —
  keep the bind on localhost and let the proxy do TLS.
- **They differ, or nothing serves HTTPS**: this machine is behind NAT — the common
  case, and fully supported. Use `tunnel.py`: it gets a public HTTPS URL over an
  outbound connection, signs up or re-pairs with it, and keeps doing so every time
  the URL moves. Install `cloudflared` (a single binary) if it is missing — on PATH,
  in `$CLOUDFLARED`, or dropped beside the scripts.

If you are unsure, prefer the tunnel: it works in both cases.

## 3. Get the account — two routes, and the NAT one has an order

**If the operator already has the app and an account**, ask them for the account
token (app Settings, or its pairing QR) and pair with `--token`. You cannot derive
it: it is the credential that says which account this agent belongs to.

**If they have no account yet**, create it from here — that is the normal case, and
it means the operator needs nothing beforehand:

    python3 pair.py --signup --url <your public URL>
    python3 pair.py --qr        # show them this; one scan signs their phone in

You do not need to ask which plane to pair with, and the operator will not know:
`pair.py` defaults to the service behind the app. `--api` overrides it, and the only
reason to pass it is a different deployment.

Order matters and cannot be swapped: the plane **probes the webhook before it
creates anything**, so the adapter must already be serving at a publicly reachable
URL. On a NAT machine you do not run `--signup` by hand at all — `tunnel.py` does
it for you the moment the tunnel URL exists, because until then there is no URL to
sign up with. Then show the QR.

Either way `config.json` ends up holding the token: `chmod 600`, never print it back
into a chat, never commit it.

The QR is a different, short-lived thing (~15 min, one scan) and is safe to show. If
you reach your user over Telegram, send it with `pair.py --qr --telegram <chat id>`
rather than posting the PNG yourself: that records the message and the adapter deletes
it at expiry. Do not schedule the deletion by hand and do not promise to come back for
it — you will not be running by then, which is exactly why this is in the tool.

## 4. Check the identity it worked out — do not fill it in

The app's panel (user's name, company, your own name, logo) is derived, not
configured: `pair.py` spends one turn before registering asking you to describe
yourself from this project's files, and caches it. You do not have to do anything
for this to happen. Look at the result:

    python3 voice_agent.py --identity

**A field that came back empty is usually correct.** Values have to be attested — the
company, user and email in the project's own files, your own name in how the user
actually addresses you in this project's session logs. An agent asked who its user is
with nothing to read will otherwise answer from the account the CLI is signed in as: a
real person with nothing to do with this install.

If something is missing and the operator wants it shown, the fix is usually to write it
where it belongs (`CLAUDE.md`, a company file) and re-derive — then the next agent to
read this project knows it too.

Override in `config.json` (`agent_name`, `company_name`, `user_name`, `user_email`,
`logo`) only for something the files genuinely should not say.

If you change any of it later: restart the adapter, then re-run `pair.py --url <your
URL>` so the plane re-probes capabilities, and give it a minute — it caches both for
about five. That re-run also re-asserts the account's display name from the derived
identity, which is what the app's Account screen shows.

## 5. History is automatic — check which source it found

The app restores the conversation from your message archive if this machine has one
(`chatlog/chatdb.py` in the workdir — the `chat-archive` component), and otherwise
from your own session transcripts. Nothing to configure either way.

Worth telling the operator which one is in use: with an archive, the app shows one
timeline across every channel you talk on, and voice turns join it. Without, it shows
this project's sessions. If they want the first and have the second, installing
`chat-archive` is the change — not a setting here.

## 6. Run both pieces as services

The adapter, and — on a NAT machine — the tunnel. Templates are in `src/`.

**The trap that catches everyone:** the adapter shells out to `claude`. A service
manager does not inherit the operator's login `PATH`, so if `claude` lives in
`~/.local/bin` the service starts cleanly and then fails on *every* turn with a
command-not-found that is invisible unless you read the logs. Set an absolute `PATH`
in the unit. Verify with `which claude` as the user the service will run as.

Second trap, if the service runs as root: the CLI refuses
`--dangerously-skip-permissions` for root. The adapter handles this (`IS_SANDBOX=1`,
then a prompt-limited retry), but a non-root user is better where the machine allows it.

## 7. Run the conformance check — before your user finds the gaps

    python3 conformance.py

One request per message type the plane can send, with what breaks in the app when
each is missing. It exits non-zero if anything REQUIRED is unanswered.

Do this before you tell the operator you are done. Every problem this skill has had
in the field was a message type nobody had checked: a blank identity panel, a
crossed-out connect button, an empty chat — each one five minutes to fix and a day to
notice, because the only detector was a person looking at a phone.

Optional gaps are fine to report and leave: a machine with no chat channel has no
business pretending it can mirror to one.

## 8. Verify from the plane's side, not yours

A local `curl` proving your own server answers proves nothing about whether the plane
can reach it. Run:

    python3 pair.py --test

and read the reason it returns:

- **ok** — done.
- **unreachable** — the plane could not get to you. Wrong URL, tunnel down, proxy
  route wrong, adapter not running.
- **signed_out** — it reached you, and Claude on this machine is logged out. **Fix it
  here**, in a terminal on this machine, by running `claude` and logging in.
  Re-pairing changes nothing.

Do not report success on a running process. Then ask the operator to speak to the app
and confirm the answer came from this machine's files — something only these files
would know is the honest test.

## 9. Report back with

- The `workdir`, and why you chose it
- The transport, and the public URL the plane will call
- Service names, and how to check status, logs and restart
- Where `config.json` lives and that it is `chmod 600`
- The result of `pair.py --test`, verbatim
- The `conformance.py` output, verbatim
- The identity it derived, verbatim — and anything it could not, with where you
  would write that fact so it can

---

## Quick sanity checklist

- [ ] `workdir` is the project the operator confirmed
- [ ] Transport matches reality (public address vs NAT), not an assumption
- [ ] Adapter bound to `127.0.0.1`; only the proxy or tunnel is public
- [ ] `config.json` is `chmod 600` and uncommitted
- [ ] Absolute `PATH` in the unit so `claude` resolves
- [ ] On NAT: tunnel runs as a service so a new URL re-pairs by itself
- [ ] Identity derived and checked; blanks understood, not papered over
- [ ] `conformance.py` exits 0 — no required type unanswered
- [ ] `pair.py --test` returns ok
- [ ] A real spoken question answered from this machine's files

## The local tier (LQ) — optional, and it must prove itself (2026-09-04)

LQ is speech in and speech out **on the agent's own machine**: whisper.cpp hears, the agent's
ordinary ask path answers, Piper speaks. No audio leaves the machine and there is no speech
provider in the path. Skip this whole section and the tier simply reports itself unavailable —
which is the correct behaviour, not a degraded one.

Four things have to exist, and `local_voice.py --probe` says which are missing:

```bash
# 1. the recogniser. small MULTILINGUAL on a CPU-only box; large-v3-turbo where
#    there is an accelerator. small.en is English-only and the row will say so.
#    (whisper.cpp, built per its own README)
# 2. piper
pip install piper-tts
# 3. ffmpeg on PATH
# 4. the voices — see below
# 5. optional but worth it: ggml-base.bin beside the main model, used ONLY to
#    identify the language when the app sends lang:"auto" (see below)
# 6. on a CPU-only agent, ggml-small.en.bin as well: English turns use it
```

**On the extra English model, measured rather than assumed.** `small.en` is the same size as
`small` — 487.6 MB each, within 12 KB — so it is not a smaller model and there is no less
arithmetic to do. On a two-core agent the two are a wash (`7.11 / 7.23 s` against `7.53 / 7.14 s`,
interleaved), and on clean English they produced character-identical transcripts. Its case is
accuracy on *hard* English — accents, noise, crosstalk — which clean synthesised speech cannot
demonstrate either way. It is installed because English turns are the common case and the model is
the one place to spend on them; if the 488 MB matters more on a given box, point
`LQ_WHISPER_MODEL_EN` at nothing and every turn uses the multilingual model as before.

Point the service at them:

```
LQ_WHISPER_BIN=/path/to/whisper-cli
LQ_WHISPER_MODEL=/path/to/ggml-small.bin
LQ_PIPER_BIN=/path/to/venv/bin/piper
LQ_PIPER_VOICE=/path/to/voices/en_US-lessac-medium.onnx
```

### Voices: fetch what you will use, not all of them

```bash
python3 src/install_voices.py --langs en,de --dest /path/to/voices
python3 src/install_voices.py --all          # all fourteen, ~2.6 GB
```

The roster names four ids — `anna`, `maria` (female), `tom`, `leo` (male) — and each language fills
the roles it has voices for. **Not every language has four.** Turkish has exactly one voice in the
whole of Piper; Japanese has two. The model row publishes `voices_by_lang` so the picker offers only
what this machine can actually say, so a partial install is honest rather than broken: install two
languages and the row reports two.

The installer fetches from upstream rather than from us (measured 6.2 MB/s against 1.3 MB/s copying
from the box), installs the phonemizers Piper does **not** ship — `pyopenjtalk` for Japanese, `g2pW`
for Chinese — and ends by synthesising one word with every voice it installed.

That last step is not ceremony. Japanese and Chinese voices load, produce no audio, and fail with
`wave.Error: # channels not specified`, an error about the output file that says nothing about the
cause. For a day the tier advertised fourteen languages and could speak twelve, because the count
read filenames. Run it again yourself any time:

```bash
python3 src/local_voice.py --verify-voices
```

It prints `ok` or `MUTE` per voice and the language count that follows from it, and the model row's
published reason ends in either *"proven to speak"* or *"installed but unverified"*.

### `lang: "auto"` — pay a small model to do the language ID

When the app cannot say which language a turn is in, it sends `auto`, and whisper's own `-l auto`
answers by running the **whole model twice**: 13.4 s against 7.2 s on a two-core agent. Identifying
a language does not need a transcription-grade model, so if `ggml-base.bin` sits beside the main
one, the tier detects with base and then transcribes once with the code it found.

Measured on a two-core agent, ten languages:

| model | correct | average |
|---|---|---|
| `tiny` | 9/10 | 0.84 s |
| `base` | **10/10** | 1.82 s |
| `small` | 10/10 | 6.59 s |

End to end, the same three clips: **13.2–13.8 s before, 9.3–9.7 s after**, same transcripts.

**Not tiny, and the reason is Ukrainian** — tiny called it Russian, which is the one confusion in
this set that matters most, and a wrong `-l` does not fail, it *translates*. Tiny was also barely
confident when right (Turkish p=0.39, Dutch p=0.49), so no threshold rescues it. Base was never
below p=0.97.

Below `LQ_DETECT_MIN_P` (0.85) the detector is not believed and the turn falls back to the full
two-pass `auto` — slower and correct. No `ggml-base.bin`, same fallback. Nothing breaks by skipping
this; turns in an unknown language just cost four seconds more.

## Reminders that actually fire (2026-08-13)

The adapter can list and amend reminders as soon as `telegram-gateway`'s
`reminders_reflex.py` and a store are present. **Firing is separate**, and an
install without it creates reminders that are listed correctly and go off
nowhere — the agent will reach for whatever scheduler it can find, and a cloud
routine delivers into the Claude app rather than into the owner's chat.

    cp fire_reminders.py /opt/voice-agent/
    apt-get install -y python3-pil          # or imagemagick / ffmpeg
    crontab -e
    TZ=<the owner's zone, e.g. America/Toronto>
    * * * * * VOICE_ACCOUNT=acct-xxxx /usr/bin/python3 /opt/voice-agent/fire_reminders.py >> <workdir>/operations/reminders/cron.log 2>&1

`TZ` matters: a VPS runs UTC and reminder times are the owner's. **The image
library matters too** — without one, every push banner is silently dropped and
the notification arrives with no picture. The loop now says so in its log
rather than failing quietly, but installing one is better.

Delivery is Telegram first (that IS the reminder) and an APNs nudge second,
best-effort, through the plane's `/api/notify`. The agent authenticates with
its own plane secret, which the plane scopes to that one account.
