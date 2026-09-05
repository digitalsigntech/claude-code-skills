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
ordinary ask path answers, Kokoro or Piper speaks. No audio leaves the machine and there is no
speech provider in the path. Skip this whole section and the tier simply reports itself
unavailable — which is the correct behaviour, not a degraded one. `local_voice.py --probe` says
which pieces are missing.

### Which models, by hardware — this is a rule, not a suggestion (the owner's, 2026-09-04)

The recogniser and the synthesiser both follow the hardware. Nothing here is a tuning knob;
each row was measured on a whole turn, and the wrong row is either too slow to converse or
silently worse.

| | **CPU only** (a 2-core VPS) | **iGPU** (AMD via Vulkan, Apple) | **NVIDIA** |
|---|---|---|---|
| recogniser | `ggml-small.bin`; English turns `ggml-small.en.bin` | `ggml-large-v3-turbo-q5_0.bin` on the Vulkan build | `large-v3-turbo` on CUDA (not yet installed anywhere — same rule as the iGPU column) |
| language ID for `lang:"auto"` | `ggml-base.bin` | `ggml-base.bin` | `ggml-base.bin` |
| synthesiser, `en es fr it ja pt zh` | **Kokoro** (`install_kokoro.sh`) | Kokoro | Kokoro today; Chatterbox is the planned branch, not built |
| synthesiser, `de nl pl ru sv tr uk` | Piper | Piper | Piper |
| concurrency | one recogniser/synthesiser at a time (`_SPEECH_CPU` lock) | same lock | same |
| a long English answer, measured | ~7 s STT + 3–7 s TTS | 2.5 s STT (turbo, Vulkan) + 1–4 s TTS | — |

Why each line:

- **CPU only, `small`.** `large-v3-turbo` on two cores is 40 s a turn; `small` is 7 s and
  its transcripts were character-identical on clean speech. `small.en` for English is the
  same size and the same speed; it is there for hard English (accents, crosstalk), see below.
- **iGPU, `large-v3-turbo`.** Measured on the whole turn on an AMD Rembrandt iGPU: 2.5 s
  where `small` on the same CPU took 7 — the largest model that beat small-on-CPU, which was
  the selection rule. Build whisper.cpp with `-DGGML_VULKAN=1`; the binary is
  `build-vulkan/bin/whisper-cli`.
- **Kokoro wherever it speaks the language.** Piper was judged not good enough to listen to;
  Kokoro's loopback intelligibility on the same sentences is higher and it does not rush.
  The cost, stated before the decision: French drops from four voices to one and has no male
  voice; Spanish four → three; Italian three → two. The owner chose the sound. Which engine
  speaks which language is DATA — `_engines` in `roster.json` — not a rule in code, and Piper
  stays installed as the floor: a Kokoro failure costs a log line, never an answer.
- **The lock.** Two whisper passes at once on two cores are SLOWER than one after the other
  (18.9 s each against 7.3 + 14.6). The lock covers the CPU work only, never the model call.
- **Phrase splitting.** A Piper voice given a 290-character sentence rushed it at 27
  characters a second and the recogniser read back 23% of it; in ≤180-character phrases
  joined with ffmpeg it reads back 81%. Rate is watched: a reply faster than 22 ch/s logs
  `faster than speech`; above 40 ch/s the voice is treated as broken and the default speaks.

Environment for the service — copy `src/voice-agent.service.d.example/lq.conf` and edit:

```
LQ_WHISPER_BIN            whisper-cli (the Vulkan build where there is a GPU)
LQ_WHISPER_MODEL          small (CPU) or large-v3-turbo (accelerator)
LQ_WHISPER_MODEL_EN       small.en, CPU-only agents
LQ_WHISPER_DETECT_MODEL   ggml-base.bin, for lang:"auto"
LQ_PIPER_BIN / LQ_PIPER_VOICE
LQ_KOKORO_SITE / LQ_KOKORO_MODEL / LQ_KOKORO_VOICES   printed by install_kokoro.sh
LQ_ROSTER                 roster.json if it is not beside the voices
LQ_REPLY_RATE             leave EMPTY: the reply is encoded at the synthesiser's native
                          rate (22.05 kHz Piper, 24 kHz Kokoro). Resampling to 16 kHz is how
                          "the replies sound worse than the samples" happened.
```

### Kokoro

```bash
src/install_kokoro.sh            # venv beside ~/kokoro, kokoro-onnx 0.6.1, model + voices, hashes checked
src/install_kokoro.sh --check    # verify an existing install
```

The venv is deliberate: `kokoro-onnx` pulls `onnxruntime`, which does not belong in the
system site-packages of a box that serves somebody. **One trap, found the hard way:** any file
named `signal.py` in the agent's working directory shadows the standard library for the
whole process, and onnxruntime dies on `signal.SIGINT` — rename it.

### What a turn carries, and what the turn line says

The app sends, per turn: `lang` (a code, or `auto`), `speaker` (`anna maria tom leo`), `tz`,
and since build 344 `speaker_from` (how the app chose the id, voice names only). The reply
carries `lang` (the language the turn RAN in), `speaker`, `peak_dbfs`, `reply_format`
(`aac 24000 Hz 32k kokoro:am_michael` — which voice actually spoke), `timing.stt_s /
think_s / tts_s`, and `no_speech` when the recogniser heard nothing (unbilled). The log line:

```
voice turn: 2.8s in, 13.8s out, stt 2.5s model 19.0s tts 4.0s, 75 KB reply,
  lang=en speaker=tom from=[selected=tom pool=anna,maria,tom,leo ui=en],
  tz=America/Toronto, peak -2.3 dBFS, reply aac 24000 Hz 32k kokoro:am_michael
```

Every field in it was added because a question needed it and the log could not answer: which
voice spoke, which id arrived, whether the model or the recogniser was the slow leg.

### Rules the turn applies, all measured on the owner's phone

- **Spoken length.** Prose is capped at 400 characters (~24 s) and ends with "The rest is on
  your screen"; tables and code are never read, the prose around them is. A model that sees
  the person asked to hear something in full prefixes `[read-in-full]` and the cap lifts.
- **The normaliser** (`say_text`, 30 cases in `test_say_text.py`): slash → "or"/"per", day and
  month abbreviations spelled out, hyphen ranges → "to", 2–3 capital codes spelled, currency and
  24 h times read as a person would, `**bold**`, `*italic*`, `_under_`, bullets and headings
  stripped. URLs, paths and emails are masked first so none of that touches them.
- **Short clips inherit the session language** (≤2.5 s, remembered 10 min per account): a
  one-word "yes" is not re-identified from scratch.
- **The same clip twice is a retry.** A 120 s replay cache keyed on the audio's sha256 returns
  the answer already given, and a duplicate that arrives while the first is still running
  waits for it — a 31 s clip used to be transcribed and answered twice, differently.
- **Both legs are cross-checked against the phone** (in: phone clip length vs decoded, floor
  0.5 s; out: phone played vs any of the last four replies, floor 0.75 s) and logged, never
  refused.
- **An unknown speaker id is logged**, not silently defaulted (`no voice named 'x'` /
  `no kokoro voice named 'x'`).

### Greetings and `voices_rev`

The picker's samples are built on the plane from the same roster and engine table
(`build_greetings.py`, plane-side), 42 clips for 14 languages. The local model row carries
`voices_rev`, a hash of the clip set; the app keys its on-device sample cache by it, so a
rebuilt clip is fetched again instead of a cached voice being played for a rebuilt one.

### Verify the tier, with tools, not by reading

```bash
python3 src/local_voice.py --probe            # what is installed
python3 src/local_voice.py --verify-voices    # every voice speaks (ok / MUTE), counts follow
python3 src/loopback_check.py                 # speak → transcribe → % of the sentence that survived
python3 src/test_say_text.py                  # the normaliser's cases
python3 src/gpu_bench.py                      # which recogniser beats small-on-CPU here
python3 src/tts_bench.py                      # Piper vs Kokoro on the same sentences
```

`loopback_check.py` is the only objective ear an operator without speakers has: the
recogniser reading back what the synthesiser said. 75–85% on a normal sentence is healthy;
23% was the gibberish of 2026-09-04.

**On the extra English model, measured rather than assumed.** `small.en` is the same size as
`small` — 487.6 MB each, within 12 KB — so it is not a smaller model and there is no less
arithmetic to do. On a two-core agent the two are a wash (`7.11 / 7.23 s` against `7.53 / 7.14 s`,
interleaved), and on clean English they produced character-identical transcripts. Its case is
accuracy on *hard* English — accents, noise, crosstalk — which clean synthesised speech cannot
demonstrate either way. It is installed because English turns are the common case and the model is
the one place to spend on them; if the 488 MB matters more on a given box, point
`LQ_WHISPER_MODEL_EN` at nothing and every turn uses the multilingual model as before.

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

## Real-time streaming (2026-09-05) — GPU installs only, declared by proof

Local quality sends one sealed clip per sentence. Streaming sends the sentence AS IT IS
SPOKEN: the phone streams 100 ms frames of PCM16/16 kHz over one WebSocket per session, a
resident recogniser on the GPU re-decodes the open utterance every 700 ms for partial words on
the screen, decodes it once more with full context when the person pauses, and the turn is then
the ordinary one — the same answer path, the same spoken cap, the same synthesiser — delivered
as a `reply` shaped exactly like a clip reply.

**The gate is a fact.** The local row says `"stream": true` only when the recogniser binary
reports a GPU backend (`ggml_vulkan`, CUDA, Metal) and a resident `whisper-server` is up and
answering. A CPU-only install starts the server once, reads "cpu", stops it and never declares
the flag: `small` on two cores is 7 s a clip and no streaming makes that converse.

**Why resident, why a sized window — measured.** Whisper's cost is a padded 30-second encoder
window plus a model load per invocation, not the audio length. On an AMD iGPU (Vulkan),
`large-v3-turbo` on a 4 s utterance: 2.39 s per `whisper-cli` call, 1.96 s resident, **0.5 s
resident with `audio_ctx` sized to the utterance** (floor 512 — 384 made a 3 s question come
back twice over, 320 made a 2 s clip decode to dots). `small`: 1.05 / 0.56 / 0.25 s. So one
resident turbo does partials AND finals; a real streaming decoder (a Parakeet-TDT port is in
the whisper.cpp tree) is a later stage that changes nothing on the wire.

**The wire** (app build 346). First frame: a sealed text frame (today's envelope) whose
plaintext is `{"type":"start","lang","speaker","tz","key_b64","format":"pcm16","rate":16000,
"frame_ms":100,…}`. Every later frame is binary: 1 kind byte (1 audio, 2 control JSON, 3 agent
JSON) ‖ 12-byte nonce (4 random ‖ 8-byte big-endian counter) ‖ AES-256-GCM ciphertext ‖ tag,
under the stream key; the agent answers with kind 3 under its own nonce prefix. Control:
`utterance_start {id}` (optional), `utterance_end {id, seconds, prefiltered}`,
`utterance_cancel {id}`, `heard_out {seconds}`. Agent: `hello {recogniser, backend,
partial_every_ms, max_utterance_s}` before any audio, `partial {id,text}`, `final {id,text}`,
`reply {…as a clip reply…, audio_seconds, audio_seconds_out}`, `no_speech {id}`, `error`.

**The plane is opaque to words, not to seconds.** A metered agent frame (reply, no_speech)
travels to the plane as JSON text `{"frame": <base64>, "id", "audio_seconds",
"audio_seconds_out"}`; the plane bills the clear fields exactly as it bills a clip and forwards
the binary frame to the phone unchanged. Phone-to-plane stays fully opaque.

**Files.** `src/ws_min.py` — WebSocket on the standard library, server and client, proven
against the `websockets` library in both roles. `src/stream_lq.py` — the resident recogniser,
the frame codec, the session; `python3 src/stream_lq.py --selftest` streams a synthesised
sentence through the whole thing with no network and expects a hello, partials, a final and a
metered reply; `--facts` prints what the row will say. The adapter serves `/stream` beside
`/hook` (the plane authenticates with the hook secret and names the account in headers, because
an upgrade has no body) and starts the recogniser at boot. `LQ_STREAM_PORT` (8098),
`LQ_STREAM_PARTIAL_MS` (700), `LQ_STREAM_MAX_UTTERANCE_S` (60) are the knobs; the restart script
recycles the recogniser's port because it is the adapter's child.

**Measured end to end** (a scripted phone through the real plane to the owner's GPU agent,
2026-09-05): hello 0.3 s after the start frame, partials at 3.4 s and 4.6 s while the sentence
was still streaming, the final 0.6 s after the end of speech, the reply after a real model
turn; the plane billed 3.1 s in + 4.8 s out as one utterance and the phone received the frame
with no wrapper.

## Sealed attachments (2026-09-04) — stage 3 of end-to-end encryption

Once an account is sealed (`e2ee` on, a device key on disk) and `e2ee_attachments`
is true in `config.json`, the agent lists the capability **`sealed-attachments`**
and the app stops sending photos and files in the clear:

- **Up.** The phone seals each file under its own random key (AES-256-GCM, nonce
  in the reference, tag appended), uploads the ciphertext to the plane's `blob`
  route, then sends an ordinary sealed `ask` whose plaintext is
  `{"attachments":[ref…], "caption": "…"}`. The plane relays that ask with the
  ciphertext beside the envelope as `blobs[]`; the agent opens each blob with the
  key from the envelope, checks the hash first, and from there it is the `photos`
  path: `save_upload()`, the archive row, the Telegram mirror. The reply carries
  `posted`, `posted_to`, `tokens` and `received` (the plane deletes what was
  received; a blob that fails to open is reported received too, so it never
  lingers).
- **Down.** A file the agent holds gets a sealed twin **once**, beside it:
  `<path>.sealed` and `<path>.sealed.json` (the reference, key included — the
  agent's disk is the trusted end). The `blob` hook serves the twin; the plane
  caches and serves ciphertext. The reference rides inside `meta_sealed` on
  attachments-feed items and history rows (`attachments: [ref…]`, one per token,
  album order). A token without a reference falls back to the plain `file` route
  on the phone — which the agent's own renders (charts, reminder thumbnails)
  still use.
- **Verify** with the plane, not by reading code: `GET /capabilities` lists
  `sealed-attachments`; a photo sent from a sealed account produces a `blobs ->
  1 sent, 1 received` line in the plane log and a `sealed attachments: 1 file(s)
  opened` line in the agent's; `GET blob/<token>` for a file the agent sent
  returns bytes whose sha256 matches the `.sealed.json` beside the file.

## What changed the week of 2026-09-01, and what an install must do by hand

Everything below is in `src/` unless marked *by hand*; both installs this was measured on run it.

- **Local tier by hardware class** — the table above; `install_kokoro.sh`; the drop-in
  `voice-agent.service.d.example/lq.conf`. *By hand on the hosted install:* the drop-in itself at
  `/etc/systemd/system/voice-agent.service.d/`, Kokoro under `/root/kokoro` + `/root/kokoro-venv`,
  `REMINDERS_MINT_MODULE=voice_agent` + `PYTHONPATH` so reminder thumbnails are minted in-process.
- **Roster** (`roster.json`): four ids per language, `_engines` per language, Kokoro voice ids
  under `kokoro`. `install_voices.py` fetches only what an engine will use.
- **Turn fields and the turn line** — `lang`, `speaker`, `speaker_from`, `reply_format`,
  `timing`, `peak_dbfs`, `no_speech`; the line prints `stt / model / tts`.
- **Normaliser, spoken cap, `[read-in-full]`, phrase split, rate watch, replay cache,
  in-flight wait, cross-checks, language memory** — all in `local_voice.py`.
- **`answer_question()`** (the owner's own agent): one answer path for typed and spoken questions —
  viewer time zone, in-process reflexes, table safety net, fresh-reminder append, figure
  ledger — because the voice path calling the model directly turned a milliseconds reminders
  answer into a 19-second model turn with no thumbnails. the adapter's voice path already goes
  through `ask()`; the lesson for an install is *one road, both origins*.
- **Archive guard**: `archive()` refuses voice envelopes and anything over 20,000 characters
  (a probe once put 170 KB of base64 audio into a guest's chat).
- **Restart guards**: `restart_agent.sh` waits for idle whisper/piper and no turn in flight.
  On the owner's agent and the plane the same scripts also refuse while a phone holds a live session
  (the plane's session table) — restarting under a live session shows the person
  "Not connected" and an empty chat.
- **Sealed attachments** (section above) and **`e2ee-v1`** (`README.md` § Security,
  `devices.py`, `e2ee_v2.py`).
- **Persona rule** *(by hand, in the workdir's `agent-system-prompt.md`)*: never name a file
  as your source — text in `src/persona-rules.md`.
- **Manual**: the plane serves `/manual` + `/manual/version`; the adapter polls the version every
  15 minutes and fetches on change (`sync_app_docs`), so the model answers from the current one.

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
