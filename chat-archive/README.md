# Chat archive + classifier

A searchable SQLite/FTS5 log of every chat message and assistant reply, with automatic
**project tagging** and **multi-label classification** driven by an LLM, plus keyword,
date, project, and meaning-based recall from the command line.

## What it does

Captures each inbound message and each assistant reply into a local SQLite database with
a full-text (FTS5) index. A background classifier reads the *conversation content* and
tags each message with one primary `project` and a short set of cross-cutting `labels`,
so you can later recall any thread by keyword, date range, project, label, or meaning —
even when a single chat room has covered many topics over time. The database is created
empty on first run; you point your chat gateway's capture hook at `record()` and/or
backfill from existing session transcripts.

## How it works

| File | Role |
|------|------|
| `src/chatdb.py` | Capture (`record()`, imported by your gateway) + the schema + the search CLI. FTS5 index over message text, kept in sync by triggers. |
| `src/chatdb` | Thin bash CLI wrapper (resolves `chatdb.py` next to itself). |
| `src/classify.py` | Groups unclassified rows into conversation *bursts* (same chat, <30 min gaps) and asks an LLM which project/labels each burst is about. Runs as a real-time in-process worker and/or a safety-net cron. |
| `src/recall.py` | Meaning-based recall using only the chat LLM: expand the query into related terms → FTS5 candidate pool → LLM reranks by intent. No embeddings needed. |
| `src/embed.py` | Optional vector recall: embeds messages into an on-disk store via an OpenAI-compatible embeddings endpoint, ranks by cosine similarity. |
| `src/backfill.py` | Imports existing chat-LLM JSONL session transcripts into the DB (idempotent). |
| `src/projects.json` | Primary project vocabulary (one tag per row). Auto-grows when the classifier meets a new topic. **Example slugs — edit for your world.** |
| `src/labels.json` | Multi-label vocabulary (several per row). Auto-grows. **Example labels — edit for your world.** |

Why classify from content, not the room? One chat room carries several projects over
time, so the room can't be the tag. Rows stay `project = NULL` until the classifier
tags them. `record()` is best-effort and never raises into the caller — a broken archive
must never break the chat.

### Two LLM roles (both swappable)

- **Worker** (classifier, `classify.py`): a cheap/metered model via an OpenAI-compatible
  `/chat/completions` endpoint. Default is an OpenRouter-hosted model; point `OR_URL` /
  `OR_MODEL` at any provider or a local llama.cpp/vLLM server. If it fails, it falls back
  to the chat LLM CLI and (optionally) fires a throttled alert.
- **Chat LLM** (`recall.py`, and the classifier fallback): a CLI that takes `-p <prompt>`
  and returns JSON on stdout. The defaults assume a `claude`-style CLI; swap via `CHAT_BIN`.
- **Embeddings** (`embed.py`, optional): any OpenAI-compatible `/v1/embeddings` endpoint;
  local is fine since embeddings aren't generative.

## Prerequisites

- Python 3.8+.
- `numpy` — only if you use `embed.py` (semantic vector recall). Everything else is stdlib.
- A worker LLM endpoint (OpenAI-compatible chat completions) for `classify.py`, **or**
  swap it to a local one. An API key if the endpoint needs one.
- A chat-LLM CLI on `PATH` for `recall.py` and the classifier fallback (optional if you
  don't use meaning-based recall and your worker never fails).
- An embeddings server for `embed.py` (optional).

## Install / setup

```bash
# 1. Put the src/ files where you want them; make the CLIs executable.
chmod +x src/chatdb src/chatdb.py src/classify.py src/recall.py src/embed.py src/backfill.py

# 2. (Optional) credentials for the default OpenRouter worker model:
mkdir -p ~/.config/chat-archive
cp secrets.env.example ~/.config/chat-archive/secrets.env   # then edit the key
#    ...or just: export OPENROUTER_API_KEY=sk-...

# 3. Edit src/projects.json and src/labels.json to your own project/label vocabularies
#    (they auto-grow, but a good starting set makes early tagging much better).

# 4. Wire capture into your chat gateway — import chatdb and call record() on each turn:
#      import chatdb
#      chatdb.record(text, "in",  sender="user",      chat_id=cid, chat_title=title)
#      chatdb.record(reply, "out", sender="assistant", chat_id=cid, chat_title=title)
#    Optionally start real-time tagging in-process:
#      import classify; classify.start_worker()

# 5. (Optional) backfill history from existing JSONL session transcripts:
TRANSCRIPT_DIR=~/.claude/projects/my-project python3 src/backfill.py --dry-run
python3 src/backfill.py

# 6. Tag whatever is unclassified (also run this on a cron as a safety net):
python3 src/classify.py

# 7. Search:
src/chatdb search "onboarding"
src/chatdb search "invoice" --since 2026-06-01 --project finance
src/chatdb recent --project research --limit 20
src/chatdb projects        # message counts per project
src/chatdb stats
python3 src/recall.py "on-prem deployment decision" --limit 10   # meaning-based
python3 src/embed.py index && python3 src/embed.py search "pricing thread"  # vector
```

The DB defaults to `src/chat.db` (created empty on first use); override with `CHATDB_PATH`.

## Config

All configuration is via environment variables; nothing else is hardcoded.

| Var | Used by | Default | Purpose |
|-----|---------|---------|---------|
| `CHATDB_PATH` | all | `<src>/chat.db` | SQLite DB location. |
| `OR_URL` | classify | OpenRouter chat-completions URL | Worker endpoint (OpenAI-compatible). |
| `OR_MODEL` | classify | a hosted model id | Worker model. Swap for any model/provider. |
| `OPENROUTER_API_KEY` | classify | — | Worker API key (or put it in `secrets.env`). |
| `SECRETS_ENV` | classify | `~/.config/chat-archive/secrets.env` | File to read `OPENROUTER_API_KEY=` from. |
| `CHAT_BIN` | classify, recall | `claude` | CLI for the chat LLM (prompt in, JSON out). |
| `FALLBACK_MODEL` | classify | `opus` | Model the classifier falls back to. |
| `RECALL_MODEL` | recall | `opus` | Model used for query expansion + rerank. |
| `EMBED_URL` | embed | `http://127.0.0.1:8080/v1/embeddings` | Embeddings endpoint. |
| `EMBED_DIM` | embed | `768` | Embedding dimensionality. |
| `EMBED_DOC_PREFIX` / `EMBED_QUERY_PREFIX` | embed | nomic-style prefixes | Set empty for models without task prefixes. |
| `TRANSCRIPT_DIR` | backfill | `~/.claude/projects/my-project` | Where JSONL session transcripts live. |
| `ALERT_BOT_TOKEN_FILE` / `ALERT_CHAT_ID_FILE` | classify | unset | Optional Telegram-style fallback alert; disabled unless set. |

Classifier burst tuning lives as constants at the top of `classify.py` (`GAP_SECS`,
`MAX_BURST_MSGS`, `MAX_CHARS`, `LOOKBACK_MSGS`, ...).

## Caveats

- **Swappable backends.** Worker LLM, chat LLM, and embeddings are all configurable; the
  shipped defaults are illustrative, not required. Don't hardcode a private endpoint/key.
- **Bring your own credentials.** No keys are included; provide your own via env or
  `secrets.env` (see `secrets.env.example`).
- **Empty DB by design.** No real chat data ships. `chat.db` (plus its `-wal`/`-shm`) is
  created on first run. The vector `store/` and `logs/` are created as needed.
- **Vocabularies are examples.** `projects.json` / `labels.json` contain generic example
  slugs. Replace them; they self-grow as the classifier encounters new topics.
- **Backfill format.** `backfill.py` assumes a Claude Code / claude-style JSONL session
  transcript and a gateway that prepends a context header to human turns. Adjust the
  regexes and `TRANSCRIPT_DIR` for your own gateway, or skip backfill and capture live.
- **What was stripped for this release.** The original tool shipped with a real populated
  `chat.db`, its WAL/SHM sidecars, a populated vector `store/`, classifier `logs/`, and a
  gateway-restart helper script; none of those are included. Identifiers, project names,
  paths, and endpoints were genericized.
