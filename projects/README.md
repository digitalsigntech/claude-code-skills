# projects — R&D project chats: a Telegram group that files itself

Turn a Telegram group into a **self-filing lab notebook** for one R&D project.
Everything the owner posts there — text, voice notes, photos, documents, albums —
is automatically filed into the project's directory, annotated, indexed, and made
retrievable, *before* the conversational turn runs. Questions asked in the group
are answered with the **project's own files as the first source of context**, not
the general knowledge base.

Built as an extension of the [telegram-gateway](../telegram-gateway/) skill
(`gateway.py` imports `projects_mode.py` from this skill — copy or symlink
`src/projects_mode.py` next to `gateway.py`).

## Directory layout (per project)

```
projects/<slug>/
  PROJECT.md          wiki-style overview: goals, current state, decisions, key files
                      (the assistant keeps it current as results/decisions land)
  REGISTRY.md         one table row per filed item — the quick-retrieval index
  files/YYYY-MM-DD/   raw files, one dir per day; each file gets a .meta.md sidecar
                      (annotation + keywords) so plain grep / semantic search finds it
  notes/YYYY-MM.md    chronological lab notebook: text posts + voice transcripts
```

Two retrieval paths by design: **REGISTRY.md** (grep one file, newest last) for
"where is that thing", and the wiki-style **PROJECT.md** for "what's the state of
this project". Filed photos are also pushed into the CLIP media index (see
[clip-media-search](../clip-media-search/)) so "show me the photo of X" works.

## What happens per post

| post | pipeline |
|------|----------|
| text | appended to `notes/YYYY-MM.md`, then answered (project files first) |
| voice note | transcribed on-box (whisper.cpp), audio + transcript filed, transcript answered |
| photo | filed; owner's caption = annotation, else **auto-annotated by the local-policy vision model**; CLIP-indexed |
| document | filed; summarized + keyworded by the local-policy model; `.meta.md` sidecar |
| album | every file filed as above, one summary message |

**Processing policy:** image/document analysis is done by the *local-policy* LLM
only (e.g. Nemotron; on OpenRouter as an interim until local hardware lands —
never the cloud assistant). Voice transcription is fully on-box.

## Privacy switch (per chat, shown on the group title)

- `/wisdom` — conversational turns run on the cloud assistant (Claude). Filing and
  file analysis still stay on the local-policy model.
- `/privacy` — conversational turns run on the private Nemotron path
  ([privacy-router](../privacy-router/)), which **fails closed** — never falls
  through to the cloud.

The current mode is appended to the **group title** ("… · 🧠 Wisdom" /
"… · 🔒 Privacy") so it's always visible at the top of the screen. Requires the
bot to be a group admin with *Change group info*; if it isn't, the toggle still
works and the bot tells you to grant admin.

## Commands

- `/project <slug>` — bind this chat to `projects/<slug>/` (scaffolds the
  directory). Static bindings go in `tgconf.PROJECT_CHATS`; runtime bindings
  persist in `state/projects.json`.
- `/project` — show the current binding + mode.
- `/privacy` / `/wisdom` — switch the answering model, update the title.

## Setup

1. Install [telegram-gateway](../telegram-gateway/) and copy/symlink
   `src/projects_mode.py` into its `src/`.
2. Create a Telegram group with the bot; make the bot admin (*Change group info*).
3. Bind it: add `{<chat_id>: "<slug>"}` to `PROJECT_CHATS` in `tgconf.py`, or just
   send `/project <slug>` in the group.
4. Optional: set `DST_PROJECTS_VISION_MODEL` (default: a Nemotron VL model on
   OpenRouter) for photo auto-annotation; `OPENROUTER_API_KEY` in
   `~/.config/dst/secrets.env`.

## Lessons baked in

- **File first, answer second.** Filing is deterministic code, not an LLM choice —
  a flaky model can never lose a lab result.
- **The registry is one flat file.** Grep beats a database for a single-owner
  project log; newest-last rows keep diffs append-only.
- **Sidecar `.meta.md` per file** makes binary files findable by text search
  without a separate index, and survives file moves within the tree.
- **Captions double as instructions.** A caption that's a question gets answered
  after filing; a purely descriptive one gets a one-line acknowledgement — the
  turn prompt says which is which, so the model doesn't chatter.
- **Title suffix must strip before re-append** or toggling stacks
  "· Wisdom · Privacy" endlessly (remember the base title).
