# email-knowledge-extract

A cron pipeline that turns a mailbox archive into a living **knowledge base + lightweight
CRM** using an LLM. For every new email it extracts contacts, durable product knowledge,
operational facts (orders/POs/quotes/shipping), a one-line activity entry, and a rolling
per-contact relationship summary with inline source citations. It also learns the writing
style of your own senders. Contact rosters are built deterministically (from headers/body)
so the LLM can only *enrich* contacts, never *invent* them.

## What it does

- **Contacts / CRM** — merges every real person/org into `contacts.db` (+ `contacts.csv`),
  with company, role, phone, a dated activity log, and an LLM relationship summary.
- **Product knowledge** — durable technical facts from *your outbound* mail →
  `product-knowledge.md` (marketing/pricing deliberately excluded).
- **Operational knowledge** — order/PO/quote/invoice numbers, shipping, channel, decisions,
  next steps from *any* mail → `operational-knowledge.md`.
- **Forwarded mail** — recovers the *original* sender hidden inside an internally-forwarded
  message and extracts them too.
- **Writing-style profiles** — per-sender learned style (`learned-<name>.md`), bounded so it
  never grows without limit.
- **Hallucination guard** — any address the model returns that isn't in a header or body is
  logged and dropped.

## How it works

| File | Role |
|------|------|
| `src/process_emails.py` | Orchestrator. Reads unprocessed rows from the `emails` table, runs extraction, upserts contacts/KB, refreshes summaries. Idempotent (tracks processed IDs). Day = recent mail gently; night = whole backlog. |
| `src/extract.py` | All LLM calls. One focused prompt per field; JSON parsing; rolling-checkpoint contact summaries with inline `[msg-id]` citations + a deterministic citation sanitizer. Primary HTTP model + optional CLI fallback. |
| `src/kbconf.py` | Config: your domain/addresses, output paths, noise-sender filter, day/night budgets, forwarded/quoted-reply parsing. |
| `src/db.py` | SQLite schema + all DB access (contacts, email archive, processed set, summary checkpoints). Schema only — no data ships. |
| `src/conv_clean.py` | Thread-level cleaner: drops internal-agent chatter, dedupes repeated signatures/disclaimers across a thread, repairs HTML→text spacing, cuts quoted history. |
| `src/run.sh` | Cron entry point: single-instance lock, sets day/night mode by hour, runs the processor. |

**Data flow:** a downloader (not included) fills the `emails` table → `process_emails.py`
reads it → `extract.py` calls the LLM → results land in `contacts.db` + the markdown KB files
under your data dir.

## Prerequisites

- **Python 3** with `requests` (`pip install requests`). `sqlite3` is stdlib.
- **An LLM endpoint** — any OpenAI-compatible `/chat/completions` (a hosted router, or a local
  `llama.cpp` / vLLM / Ollama server). For the per-contact summaries a large context window
  (tens of thousands of tokens) helps but isn't required.
- **A populated `emails` table.** This skill *processes* an archive; it does not fetch mail.
  Supply your own mailbox sync (e.g. a Gmail/IMAP downloader) that inserts rows via
  `db.upsert_email(conn, record)` — see `db._EMAIL_COLS` for the expected record shape
  (Gmail-style keys: `id`, `threadId`, `from`, `to`, `cc`, `subject`, `body`, `internalDate`, …).

## Install / setup

1. `pip install requests`
2. Copy `secrets.env.example` to a private path (e.g. `~/.config/myproject/secrets.env`),
   fill in your endpoint/key/model, and `chmod 600` it. Either export the vars, or point the
   code at the file with `EKB_SECRETS_FILE=~/.config/myproject/secrets.env`.
3. Edit `src/kbconf.py`: set `OWN_DOMAIN` (or `EKB_OWN_DOMAIN`), the `INTERNAL_ADDRESSES` /
   `AGENT_ADDRESSES` / `WRITERS` sets, and the `NOISE_SENDER_DOMAINS` list for your org. Match
   the agent-address prefixes in `conv_clean.py` (`DROP_ADDRS`).
4. Populate the `emails` table (your downloader).
5. Run once: `EKB_SECRETS_FILE=... python3 src/process_emails.py`
6. Schedule `src/run.sh` on cron (e.g. every 15 min). Outputs appear under your data dir
   (`EKB_DATA_DIR`, default `data/` next to `src/`).

## Config

All knobs are env vars (see `secrets.env.example`) or constants in `kbconf.py`:

| Setting | Default | Meaning |
|---------|---------|---------|
| `EKB_LLM_API_KEY` / `EKB_LLM_URL` / `EKB_MODEL` | — | Primary model endpoint (required). |
| `EKB_SUM_MODEL` | = `EKB_MODEL` | Model for per-contact summaries (use a big-context one if you have it). |
| `EKB_PROVIDER` | unset | Optional router-style provider pin, comma-separated. |
| `EKB_OWN_DOMAIN` | `example.com` | Your domain — decides inbound vs outbound. |
| `EKB_BUSINESS_DESC` | generic | One line describing your business; injected into the prompts for context. |
| `EKB_DATA_DIR` | `../data` | Where DB + KB markdown are written. |
| `EKB_FALLBACK_CMD` / `EKB_FALLBACK_MODEL` | unset | Optional CLI LLM used only if the primary fails. |
| `EKB_ALERT_BOT_TOKEN_FILE` / `EKB_ALERT_CHAT_ID` | unset | Optional throttled ops alert when the primary is down. |
| `EKB_DOCPIPE` | unset | Optional external "`<cmd> ingest <file>`" attachment indexer. |
| `EKB_MODE` | `day` | `day` (recent, gentle) vs `night` (backlog catch-up); `run.sh` sets it by hour. |
| `DAY_MAX/NIGHT_MAX`, `DAY_BUDGET/NIGHT_BUDGET`, `RECENT_DAYS`, `BODY_CHARS`, `MAX_EXTRACT_RETRIES` | in `kbconf.py` | Throughput/time budgets and truncation. |

## Caveats

- **Bring your own model.** The LLM backend is fully swappable — nothing is hardcoded to a
  specific vendor, and no endpoint or key ships in the code. Point `EKB_LLM_URL`/`EKB_MODEL`
  at whatever you run. The summary prompt asks for inline `[msg-id]` citations; a deterministic
  sanitizer then drops any fabricated id, and an optional editor pass relocates document-level
  id-lists inline — so citation fidelity holds even on models that cite loosely.
- **Bring your own mailbox sync.** No mail is fetched here; you populate the `emails` table.
- **What was stripped for this shareable copy:** all real company/product/person/email
  identifiers were replaced with neutral placeholders; secrets (API keys, bot tokens) are
  env-only with a `.example` template; no populated database is included.
- **Omitted from the original:** the optional **Q&A-harvest** pass (`qa_kb.py`) and the
  **semantic KB index** step — both belong to a separate embeddings-based skill. The import is
  guarded (`try/except`), so the pipeline runs fine without them; if you drop a compatible
  `qa_kb.py` into `src/`, it will be used automatically. Dev-scratch (benchmarks, account-recon
  probes, ad-hoc rebuild scripts), logs, and customer-data files were not exported.
- **Also omitted: ingest-time attachment-content extraction** (`attach_text.py` in the
  original, added 2026-07-09). It extracts every attachment's text the moment mail arrives
  (pdftotext; RapidOCR for scanned PDFs/images; openpyxl/python-docx for office files) into
  an `attachments` table + FTS5 index in the same SQLite DB, so query-time reads are instant
  instead of fetch-and-extract per question. It is coupled to the mailbox downloader (it
  pulls attachment bytes from the mail API), which this skill deliberately leaves to you —
  if you have your own sync, replicate the pattern as a post-download step in `run.sh`
  (extract → store text + FTS row keyed by email id, small per-cycle `--limit` so scanned
  PDFs can't block the cron cadence).

## Multiple mailboxes

List any number of accounts in `ACCOUNTS_TO_PROCESS`; per-writer style learning
comes from the `WRITERS` map (each person gets `learned-<person>.md`). When
mailboxes overlap (colleagues CC each other), have your downloader store the
RFC `Message-ID` header as `rec["rfcMsgid"]` — `db.upsert_email` then keeps a
single copy per message across mailboxes, preferring the sender's own SENT copy.
