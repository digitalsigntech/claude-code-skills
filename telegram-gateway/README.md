# Telegram Gateway for Headless Claude Code

### Installing it? Paste this to your agent

```
Install the Telegram gateway from
https://github.com/vladeasytag/claude-code-skills — clone it, then read and
follow telegram-gateway/AGENT-INSTALL.md.
```

That is the whole install.

### Already installed? Paste this instead

```
Update the Telegram gateway: pull the latest
https://github.com/vladeasytag/claude-code-skills and follow
telegram-gateway/AGENT-UPDATE.md.
```

[`AGENT-UPDATE.md`](AGENT-UPDATE.md) covers which files are safe to overwrite, which hold your token/allowlist/state and must survive, and how to roll back. [`AGENT-INSTALL.md`](AGENT-INSTALL.md) tells the agent to
read this README first, then walks it through the parts that decide whether the
install actually works: finding the right directory, what to ask you for, the
service-manager `PATH` trap, and verifying a real Telegram round trip before
claiming success.

Start your agent with permissions already granted — `claude --dangerously-skip-permissions`
— or it will stop on every file write and command. It will still have to ask you for
the bot token and your Telegram user ID; no permission setting can supply those.

---

Chat with a headless **Claude Code** agent over Telegram. Every message you send
runs a **real Claude turn** with full tools (bash, file I/O, and whatever else is
wired into the working directory), and each Telegram chat/group keeps its **own
persistent conversation**. Send files and the bot offers to ingest, analyze, or
hold them for the conversation.

It long-polls the Telegram Bot API (no webhook), so it runs fine on a box behind
NAT with no public IP or inbound ports.

> This is a generated copy of a gateway that runs on a private workspace box, with
> the original owner's identity, paths and group ids replaced by config. Set your
> own in `src/tgconf.py` (or the `TG_*` env vars) — `BOT_NAME`, `HOST_LABEL`,
> `WORKSPACE_LABEL` and `WORKSPACE_ROOT` are what the bot says about itself and
> where it looks for files. Some features below (KB reflex, docpipe ingest, CLIP
> media search, email injection, chat archive) are integrations with companion
> tools you may not have. They **degrade
> gracefully** — if the referenced tools/modules aren't present, that feature just
> no-ops and the core chat gateway runs unaffected. See
> [Optional integrations](#optional-integrations) for how to strip or keep them.


## The agent profile — configure this before anything else

One file describes your deployment; the code reads roles, never names.

    cp agent-profile.example.json <your-workspace>/agent-profile.json
    $EDITOR <your-workspace>/agent-profile.json

| Section | What it sets |
|---|---|
| `agent` | bot name, its mailbox, model, and `system_prompt_file` |
| `host` | how the bot refers to the machine (defaults to the hostname) |
| `org` | company name, short name, domain |
| `workspace` | `root`, display label, the `dirs` map, dirs to never index |
| `people` | `owner` / `second_owner` / `friend` — by ROLE, each with name, email, mailbox, Telegram id |
| `capabilities` | which local services actually exist here |
| `channels` | Telegram chat ids per behaviour |

**Capabilities are the important part.** Features that need a local service —
the semantic answer cache, the CLIP photo reflex, the KB reflex, the privacy
router — ask `has()` before arming themselves. Everything ships **off**, so a
fresh install degrades quietly instead of erroring on every message. Turn one on
only once the service behind it is really there.

**Your system prompt is content, not code.** Put it in a markdown file at your
workspace root (`agent-system-prompt.md` by default) — where your facts live,
what needs whose approval, what is private. With no file you get a generic
instruction that runs fine and says nothing about you.

Overrides, in order: `AGENT_<SECTION>_<KEY>` env → the profile → the built-in
default. A missing profile is never an error.

---

## The reminders queue ships with the gateway now

`src/reminders.py` is the scheduled-job queue the reminders reflex reads: add,
list, edit, cancel, plus firing into a chat. Install it at
`<workspace>/operations/reminders/reminders.py` — that is where the reflex and the
firing cron look for it.

It was previously not published in this form, so a second install had a copy taken
off the machine it was extracted from, carrying that deployment's owner keys, staff
mail addresses and voice-app push accounts. A reminder firing there would have
emailed and paged people with no connection to it.

Everything that was a literal is now config:

| Variable | What it decides |
|---|---|
| `TG_PRIMARY_OWNER_KEY` | the owner key rows default to (`owner` if unset) |
| `TG_SECOND_OWNER_KEY` | a second owner, if this deployment has one |
| `TG_OWNER_EMAIL`, `TG_SECOND_OWNER_EMAIL` | where a fired reminder mails; unset means it does not mail |
| `TG_OWNER_PUSH_ACCOUNT` / `TG_PUSH_ACCOUNTS` | voice-app accounts to push; unset means it does not push |
| `TG_REMINDER_CHAT_ID` | where a reminder fires when the caller names no chat; `add` refuses rather than queue one that would fire nowhere |
| `REMINDERS_DB` | overrides the path derived from `WORKSPACE_ROOT` |
| `REMINDERS_TZ` | the zone the OWNER lives in, IANA name — set it on any rented box |

Installing the queue is half of it. The other half is `AGENT-REMINDERS.md`: the
instruction block that has to reach the agent's own context, because an agent that
cannot see the queue will schedule the reminder with a cloud tool it can see, say
"done", and mean it — while the queue stays empty and the amendment two turns later
edits a row that never existed.

Owner keys live in the DATABASE, so changing one after rows exist means migrating
them. Pick it at install time.

**`REMINDERS_TZ` is not optional on a hosted box.** The times in the queue are the
owner's wall clock — "07:30 Monday" is 07:30 in their shop, not on a server in
another country. Unset, a rented machine parses it as UTC and the reminder fires
five hours early while the list still reads 07:30. The reflex reads the same
variable, so what is displayed and what fires come from one source.

Rows written before it was set keep their old epoch. `reminders.py list` names them:

    reminders: 1 row(s) will fire at a different time than they display …
      #6 says 2026-08-18 07:30, fires 2026-08-18 07:30

Re-set each with `edit <id> --when "<the time you meant>"`. Nothing rewrites them
automatically — the queue cannot know whether the stored hour or the stored epoch
was the intention.

## When the bot goes quiet, something has to say so

`watchdog.py`, from cron every few minutes:

    */5 * * * * cd /path/to/telegram && python3 watchdog.py >> logs/watchdog.log 2>&1

The failure it exists for is the one nothing else catches: the process is up,
`systemctl is-active` says active, Telegram is delivering — and every message dies in
the handler, so the bot receives everything and answers nothing. Every component
reports itself healthy, so the only detector is a person who eventually asks why they
are being ignored. That took hours the first time.

It watches two things: an inbound message in the archive with nothing after it for
ten minutes, and tracebacks written to the log since its last run. The alert goes
straight to the Bot API from the watchdog's own process — routing it through the
gateway would be asking the broken component to report that it is broken.

It alerts once per incident, not once per run, and its first run only sets a
baseline: a watchdog that cries about last week's log is one that gets muted.


## What it does

- **Every message → a real Claude turn.** `bridge.py` shells out to
  `claude -p <msg> --model opus --dangerously-skip-permissions` with
  `cwd` = your working directory, so the agent has full tool access.
- **One session per chat.** Each Telegram `chat_id` maps to a Claude session UUID
  (`state/sessions.json`). First message uses `--session-id`; later messages
  `--resume`. Separate group chats = separate rolling conversations (use one group
  per topic). Calls are serialized per-chat, concurrent across chats.
- **Files: save-then-ask.** Incoming document/photo/voice/audio/video is downloaded
  to `inbox/<chat_id>/`. If the message had a caption, the caption is treated as a
  question and answered directly (in Always-Nemotron chats this stays on the local
  private path — file captions never escape to the cloud turn); otherwise the bot
  posts inline buttons: **📚 Ingest to KB · 🔍 Analyze · 📎 Hold for chat**.
  Exception: in Always-Nemotron (private) chats a no-caption document is **ingested
  into the KB by default** — no keyboard — and the confirmation names the document
  plus a one-line gist of its contents.
- **Allowlist-locked.** Only Telegram user IDs listed in `allowlist.json` can talk
  to the bot; everyone else is denied and logged. The file is re-read on every
  message, so you can add/remove users with no restart.
- **Markdown rendering.** Claude replies in GitHub-flavored markdown; the gateway
  converts it to Telegram's HTML subset. Tables and task lists route through Bot
  API 10.1 `sendRichMessage` for native rendering (with a clean text fallback).
- **Commands:** `/help`, `/whoami` (show chat/user IDs), `/clear` (a.k.a. `/new`,
  `/reset` — forget this chat's session), plus optional `/cloud` and `/topic`.
- **Photo reflex (optional, sub-second).** An image request ("show me the qs256
  heads", `/pic voxeljet`) is answered deterministically — no LLM turn: query a warm
  CLIP search server, send the hits with cached Telegram `file_id`s (no re-upload).
  Fires only on an exact keyword match against curated tags/annotations; anything
  fuzzy falls through to the normal Claude turn (~10ms wasted). Inbound photos'
  `file_id`s are harvested automatically so re-sending them is instant. Video
  hits (`.mp4/.mov/.webm/.m4v`) are sent via `sendVideo` — the full clip, not
  the indexed mid-frame; their `file_id`s are cached the same way. Toggle
  `PHOTO_REFLEX` / env `TG_PHOTO_REFLEX`. Measured ~0.4s end-to-end.
- **Reminders are per-user.** Each row belongs to one person (or `shared`); a
  viewer sees their own plus shared and nobody else's, in listings, in the
  amend path, and when naming a row by id. Firing goes to the owner's own
  chat and emails that owner. Resolved from the sender's Telegram id via the
  accounts registry; unknown senders get nothing.
- **Reminders reflex (optional, ~5ms).** "Show me my reminders" is a SELECT, not
  a question for a model. Renders a two-column table, then sends each attached
  photo as a separate captioned message. The two-column shape is a finding, not
  a preference: an image in a rich-message table cell is dropped by Telegram
  without error, so the column looked correct in a client with its own renderer
  and empty in Telegram.
- **Tasks reflex (optional, ~50ms).** "What's running" answered from the live
  task registry over a loopback hook — no model, no shelling out.
- **Doc reflex (optional, ~1s).** Requests for a curated registered document
  ("fetch my expo pass", "send the price list") are answered by a direct
  `sendDocument` — no LLM turn. Docs live in `doc_registry.json` (copy
  `doc_registry.example.json`): each entry maps keyword groups (all must match)
  to one file + caption. The registry is re-read per message (no restart to add
  docs); `file_id`s are cached after first upload so re-sends skip the upload.
  Question-shaped messages ("how much was the pass?") fall through to the full
  turn. Toggle `DOC_REFLEX` / env `TG_DOC_REFLEX`.
- **Personal notes (optional, owner-private).** Any file the OWNER sends in their
  DM with no caption is auto-saved as a private note (`personal/notes/` +
  `personal/notes.db`) instead of getting the ingest keyboard. Notes are
  deliverable only to the owner's DM or a live-verified bot+owner-only group
  (`getChatMemberCount == 2`, fails closed) — never to shared groups or other
  allowlisted users. The `personal/` tree is excluded from the file-reflex walk
  and any agent file search; in the owner's DM, "get my <name> note" retrieves
  one sub-second via the file reflex. Notes can carry a `label` (description)
  and content `keywords` — both searchable via `search()`. See `src/personal_notes.py` (set `OWNER` to
  your owner user id).
- **Dictated notes, no model turn.** "my notes: big iPad password is 1248" /
  "note to self: …" writes a text note and answers in ~15ms (`add_text`), and
  "what's my iPad password" reads it back in under a millisecond
  (`search_text(spoken=True)`) — both halves used to be full model turns. The
  read half is gated twice: the chat must pass `allowed_chat()`, and only SHORT
  text notes (≤300 chars) whose name/label/keywords match a query word are ever
  spoken — a word found only in a long document's body does not qualify.
  Ambiguous or unmatched questions return nothing and fall through to the model.
  See `src/personal_note_reflex.py`.
- **Voice conversation mode (optional, fully on-box).** In chats listed in
  `VOICE_CHATS`, a voice note becomes a spoken turn: ogg/opus → ffmpeg 16k wav →
  whisper.cpp (language autodetected; a Vulkan build runs the model on an
  iGPU/dGPU, a CPU build works too) → the normal Claude turn (prompted for short,
  speakable prose in the speaker's language) → Piper TTS (voice picked per
  detected language) → an opus voice note back, followed by the full reply text.
  The transcription is echoed back (`🎙️ …`) so a bad hearing is immediately
  visible, and any audio-side failure degrades to a plain text reply — never a
  lost turn. Audio never leaves the machine; only the transcribed text goes to
  the LLM. In all other chats voice notes keep the save-then-ask file handling.
  See `src/voice_mode.py`; configure `WHISPER_BIN`/`WHISPER_MODEL`/`PIPER`/
  `PIPER_VOICES` in `tgconf.py`.
- **Albums.** Photos/files sent together as one Telegram album (which arrive as
  separate messages sharing a `media_group_id`, only one carrying the caption) are
  buffered until the album settles, then handled as a group with the shared caption.
  In `ALWAYS_NEMOTRON_CHATS` (privacy-router skill) the whole album routes to the
  local private turn with a per-file note (PDF → `read_pdf` pointer, image →
  on-policy vision description), captioned or not — never to the cloud bridge.
  Every file/album route rule must be mirrored in the private-chat branch: a path
  that falls through to `bridge.ask` from a private group is a privacy leak (bit
  us for single files 2026-07-20, for albums 2026-07-28).
- **Project chats (optional).** Groups listed in `PROJECT_CHATS` (or bound at
  runtime via `/project <slug>`) become self-filing R&D lab notebooks: every post
  is auto-filed into a per-project directory before the conversational turn, with
  a `/privacy`·`/wisdom` per-chat model switch shown on the group title. See the
  [projects](../projects/) skill for the module (`projects_mode.py`) and details.
- **Resilience.** `start_telegram.sh` is single-instance (flock) and waits for
  DNS/network before launching (boot can fire the cron before DNS is up). Pair it
  with a `@reboot` cron and a `*/5` watchdog cron.

---

## Files

| File | Role |
|------|------|
| `src/gateway.py` | Long-polling bot loop: auth → route text to Claude, files to save+buttons. Main entry point. |
| `src/bridge.py` | Headless Claude driver. One session UUID per chat; `ask()` (blocking) and `ask_stream()` (live-editing). Prefixes every turn with a context line naming the chat and the message author (`This message is from: First Last (@username, id N)`) so Claude can tell group members apart. |
| `src/tg_api.py` | Minimal Telegram Bot API client + markdown→HTML / rich-message rendering. No webhook. |
| `src/tgconf.py` | All config: token, allowlist, paths, model, timeouts, feature flags. **Edit this first.** |
| `src/photo_reflex.py` | Optional sub-second image retrieval: intent detection → warm CLIP server → send via cached `file_id`s. |
| `src/reminders_reflex.py` | Optional ~5ms reminders list: SQLite SELECT → GFM table, **two columns only**. Telegram silently drops markdown image syntax inside a `sendRichMessage` table cell (probed live against the Bot API, 2026-08-07 — the cell returns with no text), so a Photo column renders as blanks. Instead each photo follows as its own message captioned with that reminder's time and text: one per message, never an album, since an album's single caption would sit under the wrong picture. Photos via `sendfile.py`; a missing file is skipped and the table always sends first. |
| `src/tasks_reflex.py` | Optional ~50ms running-task list. Does not build the table — asks the companion voice server's `progress` hook, which renders it from the live task registry (cron jobs, dev-side scripts, agent turns). Two copies of the wording would drift the first time a catalogue entry changed. Silent fall-through if the hook is unreachable. |
| `src/doc_reflex.py` | Optional ~1s document delivery: keyword match against a curated registry → `sendDocument` via cached `file_id`s. |
| `src/file_reflex.py` | Optional generic file reflex: "show/fetch/get/give me <thing>" resolved against the CLIP image index and a cached workspace walk (workspace files in DMs + Always-Nemotron private groups only); sends only a full-token-coverage match (docs via `sendDocument`, images via the photo path), everything else falls through to the LLM turn. |
| `src/scan_reflex.py` | Optional auto-scan reflex: an inbound PHOTO OF A DOCUMENT files itself, no LLM turn. Uses the companion `docscan` skill's `autoscan.py` (point `TG_AUTOSCAN_DIR` at it) to find EVERY sheet in the frame — a photo can hold a white sheet and a dark card, and both come out — decide whether each is really a document, rectify it, and let the paper choose the format: white stock → whitened PDF, coloured/dark stock → JPEG. Filed into `knowledge-base/from-scans/`; the caption becomes the name + annotation, and with no caption the vision model writes one (retried — a free-VL empty reply would otherwise file a document with no words). A photographed page has no text layer, so each PDF gets a `.md` sidecar carrying the annotation; that is what makes it findable by text search. CLIP indexing runs behind the answer. Question-shaped captions ("what does this say?") fall through to the LLM turn, and a photo with no document in it costs ~1s. |
| `src/qr_reflex.py` | Optional login-QR reflex: the owner asking for a QR ("make me a qr for the app", "show me my qr") runs your minting script (`TG_QR_SCRIPT`, called with `--chat <id>`) directly — no LLM turn. Owner-only, gated to `TG_QR_CHATS`, since such QRs typically carry live credentials; question-shaped messages fall through. Have your script render the QR large — e.g. `qrencode -s 24` (~1200px) — and send it via sendPhoto: small QRs come out soft after Telegram's photo compression. |
| `src/feedback_reply.py` | Optional reply loop for an app-feedback group: a **swipe-reply** to a feedback post (resolved through a `message_id → account` map your poster writes, `TG_DEVREPLY_MAP`) or an explicit `@acct-id <text>` is POSTed to your backend (`TG_DEVREPLY_URL`, bearer `TG_DEVREPLY_SECRET`) and delivered back inside the app. Owner-only, and a routed message never becomes an LLM turn — it was addressed to a user, not to the bot. Delivery is confirmed in the group; an unknown id fails loudly. Off unless `TG_FEEDBACK_CHAT` + `TG_DEVREPLY_URL` are set. |
| `src/personal_notes.py` | Optional owner-private note store: no-caption DM files auto-saved; strict delivery gate (owner DM / bot+owner-only group, fails closed). |
| `src/voice_mode.py` | Optional on-box voice conversation: whisper.cpp STT (auto language) + Piper TTS; used by `handle_voice()` for chats in `VOICE_CHATS`. |
| `src/qa_cache.py` | Semantic Q&A answer cache: repeat questions (even reworded) answered in ~0.1s from a local-embedding cache instead of an LLM turn; guards for product codes, TTL, and conversational fragments. |
| `src/projects_mode.py` | Optional project chats (symlink to [`../projects/src/projects_mode.py`](../projects/)): per-group auto-filing into a project directory + `/privacy`·`/wisdom` switch. |
| `doc_registry.example.json` | Template for `doc_registry.json` (curated docs the doc reflex may send). |
| `src/tg_whoami.py` | Onboarding helper — prints the user IDs of recent senders so you can fill the allowlist. |
| `src/start_telegram.sh` | Single-instance launcher (flock + network wait). Used by `@reboot` and watchdog crons. |
| `allowlist.example.json` | Template for `allowlist.json` (a JSON array of integer Telegram user IDs). |

Runtime dirs (auto-created, gitignored): `state/` (sessions + poll offset),
`inbox/<chat_id>/` (received files), `logs/`, `inject/` (optional email queue).
On KB ingest the original document is copied to `knowledge-base/uploads/` before
any conversion, so the knowledge base never depends on files in `telegram/inbox/`
(and the upload's filename is searchable immediately, even while a slow
document→markdown conversion is still running).

---

## Prerequisites

- **Python 3** with `requests` (`pip install requests`). Everything else is stdlib.
- **Claude Code CLI** on `PATH` (`claude`), logged in. The gateway runs it headless
  with `--dangerously-skip-permissions`, so it must already be authenticated.
- A Telegram account to create the bot.

---

## Install

> Installing with an agent? See [`AGENT-INSTALL.md`](AGENT-INSTALL.md) — the paste-in
> line is at the top of this README. The steps below are the manual path.

1. **Place the code.** Copy `src/*` into a working directory, e.g.
   `~/myproject/telegram/`. `tgconf.py` treats its **parent directory** as the
   Claude working directory (`CLAUDE_WORKDIR = PROJECT_ROOT`), i.e. the folder the agent
   operates in. So put `telegram/` inside the project you want Claude to work on.

2. **Create the bot.** In Telegram, message **@BotFather** → `/newbot` → follow the
   prompts → copy the token.

3. **Store the token** (kept out of git; read by `tgconf.py`):
   ```bash
   echo '<YOUR_BOT_TOKEN>' > telegram/bot_token && chmod 600 telegram/bot_token
   ```
   (Or set the `TG_BOT_TOKEN` env var instead.)

4. **Disable group privacy** so the bot sees every message in dedicated groups
   (not just ones that @-mention it): @BotFather → `/setprivacy` → pick the bot →
   **Disable**. Skip if you only use 1:1 DMs.

5. **Find your user ID and lock access.** Message your new bot once, then:
   ```bash
   python3 telegram/tg_whoami.py
   ```
   It prints the `user_id` of recent senders. Put yours in `allowlist.json`:
   ```bash
   cp allowlist.example.json telegram/allowlist.json
   # edit it to your real ID(s), e.g. [123456789]
   ```

6. **Run it:**
   ```bash
   ./telegram/start_telegram.sh
   # logs stream to telegram/logs/gateway.log
   ```
   Message the bot — you should get a reply. Send `/help` to see commands.

7. **Autostart + watchdog (recommended).** `crontab -e`:
   ```cron
   @reboot        /home/you/myproject/telegram/start_telegram.sh
   */5 * * * *    /home/you/myproject/telegram/start_telegram.sh   # watchdog: flock makes it a no-op if already up
   ```
   The launcher waits up to 5 min for DNS before giving up (so a slow boot doesn't
   leave it dead); the watchdog restarts it within 5 min of any crash or network drop.

---

## Configuration (`tgconf.py`)

| Setting | Default | Notes |
|---------|---------|-------|
| `CLAUDE_BIN` | `claude` (env `CLAUDE_BIN`) | Path to the Claude CLI. |
| `CLAUDE_WORKDIR` | parent of `telegram/` | The dir Claude operates in. |
| `CLAUDE_MODEL` | `opus` (env `TG_MODEL`) | Model for every turn. |
| `CLAUDE_TIMEOUT` | `900`s (env `TG_TIMEOUT`) | Per-turn hard timeout. |
| `STREAMING` | `False` | `True` = live-edit a placeholder as Claude generates (shows tool activity); `False` = wait for the full reply, send once. |
| `EDIT_INTERVAL` | `1.5`s | Min seconds between live edits while streaming. |
| `APPEND_SYSTEM` | (concise-reply prompt) | Passed via `--append-system-prompt`; biases Claude toward short, direct replies. **Customize this for your project** — the shipped text is deliberately generic. |
| `TG_MAX` / `RICH_MAX` | `4000` / `32768` | Message chunk size / rich-message payload cap. |
| `KB_REFLEX` | `1` (env `TG_KB_REFLEX`) | Optional tier-1 KB quick-answer (needs the companion KB tooling; set `0` to disable). |
| `DOC_REFLEX` | `1` (env `TG_DOC_REFLEX`) | Optional instant delivery of curated documents from `doc_registry.json` (set `0` to disable). |
| `OWNER_EMAIL` / `OWNER_PERSONAL_EMAIL` | empty (env `TG_OWNER_EMAIL`, `TG_OWNER_PERSONAL_EMAIL`) | Owner mailboxes for the email-injection flow (mail from these runs as a chat turn). |
| `FRIEND_EMAIL` / `FRIEND_NAME` | empty = off (env `TG_FRIEND_EMAIL`, `TG_FRIEND_NAME`) | Optional trusted outside collaborator: their emails also run agent turns, and every reply to them CCs the owner. |

**Switching the brain to a local/cheaper model:** every message currently spends
Claude subscription tokens. To route to a local or hybrid model, change
`bridge.ask()` (and `ask_stream()`) to call your model instead of the `claude` CLI.

---

## Optional integrations (companion tools — safe to remove)

These are wired into `gateway.py`/`tgconf.py` and expect companion tools that live
in the same workspace. Each is guarded so it no-ops if the underlying tool/module
is missing:

- **Chat archive** (`chatlog/chatdb`, `classify`) — logs every message + reply to
  SQLite/FTS5 and tags each with a project. If the module can't import, archiving
  silently disables.
- **KB reflex** (`email/kb/kb_answer.py`) — retrieves KB chunks and lets a fast
  grounded model answer instantly or escalate to the full turn. Toggle `KB_REFLEX`.
- **File ingest** (`local-ai/docpipe`, `local-ai/media`) — the **Ingest to KB**
  button pushes docs into a RAG pipeline / images into CLIP search.
- **Privacy gate** (`privacy_router.py` + the **privacy-router** skill) — messages
  whose intent touches private data (customer balances, refunds, invoices, PII) are
  answered by a private tool-calling model WITH the chat's recent history, instead
  of the cloud agent; fail-closed, `/cloud` bypasses. Toggle `PRIVACY_MODE`
  (`targeted`/`off`). The private turn returns `(answer, files)`: the agent can
  queue documents (via its `find_files`/`send_file` tools) and the gateway uploads
  them into the chat after the text reply — `sendPhoto` for images (with a
  `sendDocument` fallback), `sendDocument` otherwise, per-file error handling.
  Per-chat overrides: `ALWAYS_CLOUD_CHATS` (a group where every message is a cloud
  turn — e.g. a "Public" group) and `ALWAYS_PRIVATE_CHATS` (a group answered only
  by the private model, including file delivery — e.g. a "Private" group for
  confidential matters). The private agent also has `read_pdf`/`edit_pdf` tools
  (2026-07-27, PyMuPDF): value-level find/replace inside a PDF (quantities,
  prices, dates on an invoice) that redacts each matched value and reinserts the
  replacement at the same baseline/size/color, saving an edited COPY the agent
  delivers via `send_file` — for uploaded PDFs the gateway's file note points the
  model at these tools. See the privacy-router skill's README for the hard-won
  lessons (targeted-not-strict, full context, real tools not single-shot).
- **Email → chat injection** (`inject/` queue + `email/gmailer.py`) — a mail watcher
  drops emails (body + downloaded attachments) as JSON into `inject/`; the gateway runs each as a chat turn and can
  email the reply back in-thread. Replies are sent with gmailer's `--md` flag
  (markdown body → multipart/alternative HTML + plaintext fallback), so the model's
  markdown renders as real formatting in mail clients instead of literal `**` markers
  (owner feedback 2026-07-17).

To ship a **minimal** gateway, delete the `chatdb`/`classify` imports, the
`kb_reflex`/`_ingest` code paths and their commands, and the
`inject/` machinery from `gateway.py`, plus the corresponding paths in `tgconf.py`.
The core loop (`handle_text` → `bridge.ask` / file save+buttons) is all you need.

---

## Security notes

- The bot can run **arbitrary tools** in the working directory (email, files, bash).
  Keep the allowlist tight and the token secret.
- `bot_token` is chmod 600 and gitignored; never commit it or back it up in cleartext.
- `allowlist.json` is gitignored (it contains personal Telegram user IDs). Ship
  `allowlist.example.json` instead.
- Unknown senders are denied and logged; in a private chat the bot tells them their
  user ID (so a legitimate new user can send it to you), but never runs a turn.

---

## Architecture at a glance

```
Telegram  ──getUpdates(long-poll)──▶  gateway.py
                                        │  auth (allowlist)
                                        │  text ─▶ bridge.ask / ask_stream ─▶  `claude -p ...`  (one session per chat)
                                        │  file ─▶ inbox/  + [Ingest | Analyze | Hold] buttons
                                        ▼
                                   tg_api.py  (sendMessage / sendRichMessage / editMessageText / getFile)
```

State lives in `state/sessions.json` (`chat_id → {sid, init, held, title, ctype}`)
and `state/offset` (the getUpdates cursor).
