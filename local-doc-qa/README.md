# Local Doc Q&A (docpipe)

Private, on-box document ingest + retrieval-augmented Q&A. Point it at your PDFs,
CSVs, and text files; it parses, chunks, embeds, and indexes them locally, then
answers questions with citations back to the source file and page/row. With a local
model backend, document content never leaves the machine — an orchestrator (e.g. a
coding agent) calls this tool and reads only the *derived* answer, never the raw docs.

## What it does

- **`ingest`** — parse PDF/CSV/TSV/text/markdown/json → chunk → embed → store.
- **`ask`** — grounded answer over the index, with `[source | locator]` citations.
- **`summarize`** — map-reduce summary of a single document (no index needed).
- **`list` / `reset` / `health`** — inspect the index, wipe it, check the servers.

Two answer paths, auto-routed by `ask`:

1. **Structured (exact).** Numeric/value questions ("list every item > $300",
   "cheapest X", "between $A and $B") are answered **in code** by `structured.py`,
   which parses the Markdown tables directly. Deterministic, no LLM, sub-second —
   the right tool for filtering a table by a number, which semantic RAG can't rank.
2. **Semantic (RAG).** Everything else embeds the query, retrieves the top-k chunks,
   and answers with the chat model. Force this path with `--rag`.

PDFs are converted **once** to Markdown (`pdf2md.py`, real tables via PyMuPDF table
detection); the `.md` becomes the source of truth and the PDF is never re-read.

## How it works

| File | Role |
|------|------|
| `src/docpipe` | shell wrapper — runs `pipeline/docpipe.py` from anywhere via the project venv |
| `src/pipeline/docpipe.py` | CLI entry point; the ask router (structured vs RAG) lives here |
| `src/pipeline/config.py` | all knobs (endpoints, model names, chunking, retrieval); env-overridable |
| `src/pipeline/parse.py` | PDF (PyMuPDF) / CSV (pandas) / text parsing → citable chunks |
| `src/pipeline/pdf2md.py` | one-time PDF → Markdown conversion (tables + prose) |
| `src/pipeline/llm.py` | thin OpenAI-compatible clients for the chat + embedding servers |
| `src/pipeline/store.py` | tiny numpy cosine vector store (`vectors.npy` + `meta.json`) |
| `src/pipeline/structured.py` | exact numeric/value queries over Markdown tables |
| `src/start_servers.sh` / `stop_servers.sh` | example llama.cpp launchers (localhost) |

Vector store: L2-normalized embeddings in a numpy matrix; search is a single
matrix-vector product. No external database.

## Prerequisites

- **Python 3.9+** with: `pymupdf` (fitz), `pandas`, `numpy`, `requests`.
- **Two OpenAI-compatible model endpoints**: one chat/instruct model and one
  embedding model. Any backend works — `llama.cpp` (`llama-server`), vLLM, Ollama,
  or a hosted OpenAI-compatible API. Local backends keep documents fully on-box.
- (Only if you use `start_servers.sh`) a built `llama-server` binary and GGUF model
  files.

## Install / setup

```bash
cd local-doc-qa/src

# 1) create the project venv the wrapper expects
python3 -m venv venv
./venv/bin/pip install pymupdf pandas numpy requests

# 2) configure endpoints/models (copy the template, edit, source it)
cp ../config.example.env config.env
$EDITOR config.env
source config.env

# 3) bring up your model servers (or point config at endpoints you already run)
#    edit the model paths / LLAMA_BIN_DIR first, then:
./start_servers.sh
./docpipe health

# 4) index some documents and ask
./docpipe ingest /path/to/docs
./docpipe ask "what does the warranty cover?"
./docpipe ask "list every item more than $300"
```

If you already run your endpoints elsewhere, skip `start_servers.sh` and just set
`DOCPIPE_CHAT_URL` / `DOCPIPE_EMB_URL` to point at them.

## Config

Set via environment (see `config.example.env`) or edit `src/pipeline/config.py`:

| Var | Default | Meaning |
|-----|---------|---------|
| `DOCPIPE_CHAT_URL` | `http://127.0.0.1:18182/v1` | chat model base URL |
| `DOCPIPE_EMB_URL` | `http://127.0.0.1:18183/v1` | embedding model base URL |
| `DOCPIPE_CHAT_MODEL` | `local-chat` | chat model name/alias |
| `DOCPIPE_EMB_MODEL` | `local-embed` | embedding model name/alias |
| `DOCPIPE_EMB_DIM` | `768` | embedding dimension — **must** match the model |
| `DOCPIPE_EMB_TASK_PREFIX` | `1` | prepend nomic-style `search_document/query:` prefixes |
| `DOCPIPE_KB_DIR` | `<src>/knowledge-base` | where `.md` docs + PDF conversions live |

Non-env knobs in `config.py`: `CHUNK_CHARS` (1200), `CHUNK_OVERLAP` (150), `TOP_K`
(10, point lookups), `AGG_TOP_K` (48, aggregation/numeric-filter queries). The
`CATEGORY_MAP` in `structured.py` optionally routes a category question to the right
`.md` file — the shipped entries are generic placeholders; customize them for your
own document categories or leave the list to search across every `.md`.

## Caveats

- **Bring your own model backend.** The chat and embedding backends are fully
  swappable — anything OpenAI-compatible. No endpoint URL or API key is baked in;
  set them via env. If your backend needs an API key, add it to your server/client
  setup (this tool talks plain OpenAI-compatible HTTP).
- **`EMB_DIM` must match your embedding model**, and if you re-index with a different
  embedding model you must `reset` first (dimensions/space won't match).
- **Structured path assumes `$`-style currency** in Markdown tables; adjust
  `PRICE_RE` in `structured.py` for other currencies/number formats.
- **`start_servers.sh` is an example** (llama.cpp with flags tuned for an iGPU-class
  box). Adjust `-ngl`, `-b`, `-ub`, `-t`, context size, and ports for your hardware,
  or ignore it entirely and use your own server.
- **Ships empty.** The vector store (`src/store/`) is schema-only; run `ingest` to
  populate it. No documents, models, or venv are included.
- **Omitted from the original tool** (dev-scratch / out of scope): benchmarks,
  A/B and probe scripts, evaluation harnesses, CLIP image-search and grammar-check
  modules, model weights, and test data. Only the ingest/ask/summarize path is here.
