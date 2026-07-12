# KB Semantic Index

A small, dependency-light semantic search layer over a folder of Markdown knowledge-base
files, plus a grounded **tier-1 quick-answer** that retrieves the most relevant chunks and lets
a fast LLM answer *from just those* or explicitly escalate. Meant to sit in front of a bigger
reasoning model: cheap, local retrieval handles the easy questions; only genuinely hard ones
fall through.

## What it does

- **`kb search "..."`** — semantic top-k search over the KB, ranked by cosine similarity.
- **`kb index`** — (re)build the vector index. Incremental and idempotent: chunks are keyed by
  content hash, so a refresh only embeds new/changed chunks and drops deleted text. Safe on a cron.
- **`kb ask "..."`** — tier-1 *reflex*: if the top hit is a Q&A pair above a confidence
  threshold, print its stored answer verbatim with no LLM call; otherwise exit 2 ("escalate").
- **`kb stats`** — chunk counts by KB area.
- **`kb_answer.py "..."`** — retrieves top-k chunks and asks a fast, grounded LLM to answer
  using *only* those snippets, or reply with the single token `ESCALATE`. Exit 0 = answered,
  exit 2 = escalate (and it returns the retrieved snippets so the caller can answer without
  re-searching). `retrieve()` is also importable for use by other tools.

## How it works

| File | Role |
|------|------|
| `src/kb` | Bash wrapper: `./kb <cmd>`. Uses `$KB_PYTHON` if set, else `python3`. |
| `src/kb_index.py` | Chunking, embedding, storage, and the `search` / `ask` / `index` / `stats` commands. Also exposes `retrieve(query, k)`. |
| `src/kb_answer.py` | Tier-1 grounded quick-answer over retrieved chunks (imports `retrieve`). |

**Chunking.** Files ending in `qa.md` are split one chunk per `**Q:** ... **A:** ...` pair.
Other `.md` files are chunked heading-aware and size-bounded (`MAX_CHARS`); Markdown tables are
split **one chunk per data row** (with the header row prepended) so a single distinctive row
isn't averaged into a 20-row blob. Files/dirs starting with `_` and `*.bak` are skipped.

**Embeddings.** Each chunk is embedded via an OpenAI-compatible `/v1/embeddings` endpoint
(default a local nomic server) using nomic-style `search_document:` / `search_query:` prefixes.
Vectors are L2-normalized and stored as `vectors.npy` + `meta.json` under `<KB_ROOT>/.kb_index/`.
Search is a normalized dot product (cosine) in NumPy — no external vector DB.

**Why kb_answer over a bare threshold.** Cosine similarity is a good retrieval signal but a poor
correctness signal: a wrong, entity-twisted match can out-score the right one. So `kb_answer.py`
hands the retrieved snippets to a cheap LLM that actually reads them and decides whether they
*completely* answer this exact question — escalating on entity mismatches, missing details, or
conflicting snippets. A hard wall-clock deadline caps the call and degrades to escalate.

## Prerequisites

- Python 3 with **NumPy** (`pip install numpy`). Everything else is stdlib.
- An **embeddings endpoint** returning 768-dim vectors (any OpenAI-compatible
  `/v1/embeddings`). The default targets a local server at `127.0.0.1:18183`; embeddings are
  non-generative and cheap to self-host.
- For `kb_answer.py` only: a **chat-completions endpoint** and an API key (defaults to an
  OpenRouter URL, but any OpenAI-compatible endpoint works).

## Install / setup

1. Copy `src/` somewhere on your box and make the wrapper executable:
   ```
   chmod +x kb
   ```
2. Point it at your KB and embeddings server (see Config). Minimum:
   ```
   export KB_ROOT=~/myproject/knowledge-base
   export KB_EMBED_URL=http://127.0.0.1:18183/v1/embeddings
   ```
3. Build the index, then search:
   ```
   ./kb index
   ./kb search "how do I reset the widget?"
   ```
4. (Optional) For `kb_answer.py`, provide a key — either export `KB_LLM_KEY`, or copy
   `secrets.env.example` to `$KB_SECRETS_FILE` (default `~/.config/myproject/secrets.env`) and
   fill it in. Then:
   ```
   python3 kb_answer.py "how do I reset the widget?" --json
   ```
5. (Optional) Keep the index fresh on a cron:
   ```
   */30 * * * * KB_ROOT=~/myproject/knowledge-base /path/to/kb index >> /tmp/kb-index.log 2>&1
   ```

## Config

All configuration is via environment variables — no code edits needed.

| Variable | Default | Purpose |
|----------|---------|---------|
| `KB_ROOT` | `~/myproject/knowledge-base` | Root folder of the KB. |
| `KB_INDEX_DIRS` | `products,company,faq,technical,from-emails` | Comma-separated subdirs to index. |
| `KB_EMBED_URL` | `http://127.0.0.1:18183/v1/embeddings` | Embeddings endpoint. |
| `KB_ANSWER_THRESH` | `0.74` | Min cosine score for `kb ask` to answer without escalating. |
| `KB_PYTHON` | `python3` | Interpreter the `kb` wrapper uses (e.g. a venv path). |
| `KB_LLM_URL` | OpenRouter chat-completions URL | Grounded-answer endpoint. |
| `KB_LLM_MODEL` | a fast reasoning-off model id | Model for `kb_answer.py`. |
| `KB_LLM_KEY` | — | API key (preferred). |
| `KB_LLM_KEY_NAME` | `OPENROUTER_API_KEY` | Key name to look up in the secrets file. |
| `KB_SECRETS_FILE` | `~/.config/myproject/secrets.env` | Fallback secrets file for the key. |
| `KB_ANSWER_DEADLINE` | `12` | Hard wall-clock cap (s) for the grounded call. |

`DIM` (embedding dimension, 768), `BATCH`, and `MAX_CHARS` are constants at the top of
`kb_index.py` — change `DIM` to match your embedding model.

## Caveats

- **Bring your own backends.** Both the embeddings server and the chat LLM are swappable. The
  defaults (a local nomic embeddings server; an OpenRouter-hosted fast model) are just examples
  — point the env vars at any OpenAI-compatible endpoints. Change `DIM` if your embedding model
  isn't 768-dim.
- **Bring your own credentials.** No API key is bundled or hardcoded. `kb_answer.py` reads the
  key from `KB_LLM_KEY` or a `NAME=value` line in your secrets file; the search/index/ask/stats
  commands need no key at all.
- **What's stripped from this export.** This is a sanitized copy of an internal tool. Populated
  indexes/databases, logs, and the surrounding email-ingestion pipeline were **not** included.
  In particular, the original repo had `kbconf.py` and `db.py`, but those belong to a separate
  email-extraction/contacts pipeline and are **not imported** by the search/index/ask/answer
  code paths — so they're intentionally omitted here. Dev-scratch (benchmarks, one-off recon
  and cleanup scripts) is also omitted. All company-, person-, and product-specific identifiers
  have been genericized.
- **Q&A format.** `kb ask` only returns a reflex answer for chunks that parse as
  `[title] Q: ... \n A: ...` — i.e. content authored in Q&A Markdown. Prose chunks always
  escalate.
