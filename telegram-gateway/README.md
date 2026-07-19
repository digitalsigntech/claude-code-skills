# Telegram Gateway for Headless Claude Code

Chat with a headless **Claude Code** agent over Telegram. Every message you send
runs a **real Claude turn** with full tools (bash, file I/O, and whatever else is
wired into the working directory), and each Telegram chat/group keeps its **own
persistent conversation**. Send files and the bot offers to ingest, analyze, or
hold them for the conversation.

It long-polls the Telegram Bot API (no webhook), so it runs fine on a box behind
NAT with no public IP or inbound ports.

> This is the gateway that runs on **Mercury** (the DST appliance). Some features
> below (KB reflex, docpipe ingest, CLIP media search, email injection, chat
> archive) are DST-specific integrations. They **degrade
> gracefully** — if the referenced tools/modules aren't present, that feature just
> no-ops and the core chat gateway runs unaffected. See
> [Optional integrations](#optional-integrations) for how to strip or keep them.

---

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
  question and answered directly; otherwise the bot posts inline buttons:
  **📚 Ingest to KB · 🔍 Analyze · 📎 Hold for chat**.
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
  and content `keywords` — both searchable via `search()`. See `src/personal_notes.py` (set `VLAD` to
  your owner user id).
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
| `src/doc_reflex.py` | Optional ~1s document delivery: keyword match against a curated registry → `sendDocument` via cached `file_id`s. |
| `src/file_reflex.py` | Optional generic file reflex: "show/fetch/get/give me <thing>" resolved against the CLIP image index and a cached workspace walk; sends only a full-token-coverage match (docs via `sendDocument`, images via the photo path), everything else falls through to the LLM turn. |
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

---

## Prerequisites

- **Python 3** with `requests` (`pip install requests`). Everything else is stdlib.
- **Claude Code CLI** on `PATH` (`claude`), logged in. The gateway runs it headless
  with `--dangerously-skip-permissions`, so it must already be authenticated.
- A Telegram account to create the bot.

---

## Install

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
| `APPEND_SYSTEM` | (concise-reply prompt) | Passed via `--append-system-prompt`; biases Claude toward short, direct replies. **Customize this for your project** — it currently mentions DST specifics. |
| `TG_MAX` / `RICH_MAX` | `4000` / `32768` | Message chunk size / rich-message payload cap. |
| `KB_REFLEX` | `1` (env `TG_KB_REFLEX`) | Optional tier-1 KB quick-answer (DST-specific; set `0` to disable). |
| `DOC_REFLEX` | `1` (env `TG_DOC_REFLEX`) | Optional instant delivery of curated documents from `doc_registry.json` (set `0` to disable). |

**Switching the brain to a local/cheaper model:** every message currently spends
Claude subscription tokens. To route to a local or hybrid model, change
`bridge.ask()` (and `ask_stream()`) to call your model instead of the `claude` CLI.

---

## Optional integrations (DST-specific — safe to remove)

These are wired into `gateway.py`/`tgconf.py` for the DST appliance. Each is
guarded so it no-ops if the underlying tool/module is missing:

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
  confidential matters). See the privacy-router skill's README for the hard-won
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
