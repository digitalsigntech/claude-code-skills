# Reply Auto-Drafting

Auto-drafts replies to inbound **product inquiries** in an owner's Gmail mailbox,
in the owner's own voice, and **learns from the replies the owner actually sends**
so the drafts get closer over time. Each draft is grounded in a product knowledge
base (retrieved via a semantic index), threaded correctly, and — on the first reply
in a thread — can attach a document (price list, brochure, etc.). It **never sends**:
every draft lands in Gmail *Drafts* for the owner to review, edit, and send.

An optional **privacy layer** masks customer PII to opaque tokens before any text
reaches the cloud LLM, and routes anything too sensitive to a local model instead.

---

## What it does

On every scan (two phases per run, learn-then-draft):

**Phase A — draft.** Lists new unread INBOX mail in a recent window. For each message
it hasn't seen, the LLM classifies whether it's a genuine product inquiry (skipping
vendors, newsletters, invoices, internal chatter, automated mail). For real inquiries
it drafts a reply using: semantic top-k over the KB, facts learned from past replies,
the owner's writing-style profile, and the learned drafting instructions. It saves the
draft to Gmail Drafts — **To** = the customer, **CC** = the original CCs plus a fixed
teammate (owner and customer removed), **Subject** = `Re: …`, threaded via
`In-Reply-To`/`References`/`threadId`. On the first reply in a thread it attaches the
configured document. Then it pings a chat channel so the owner can review and send.

**Phase B — learn.** For every draft made earlier, it checks the thread by `threadId`:
- draft still in Drafts, unsent → do nothing;
- draft sent (possibly edited first — a draft sent from the Gmail UI keeps its draft
  id and just gains the `SENT` label, so this is detected by label, not by the draft
  disappearing) **or** the owner deleted it and sent their own reply → the LLM compares the
  draft to what the owner actually sent and extracts (a) durable KB facts appended to
  `reply-learnings.md`, and (b) drafting-behaviour lessons merged into the
  `LEARNED-INSTRUCTIONS` block in `SKILL.md` (fed verbatim into future drafts);
- draft gone **and** no reply sent → the owner deleted it → do nothing.

A safety cap limits drafts per run so activation can't flood Drafts. Nothing is ever
sent by the script.

---

## How it works — the files

| File | Role |
|------|------|
| `src/autodraft.py` | The whole pipeline: Gmail I/O, classify → draft → save, and the reconcile/learn phase. Main entry point. |
| `src/privacy.py` | Optional PII masking: deterministic regex rules + model NER → reversible `[[TYPE_N]]` tokens, a pre-send tripwire, and a local-model fallback for sensitive mail. |
| `src/run.sh` | Single-instance (flock) cron launcher; sets `AUTODRAFT_PRIVACY` and appends to `logs/`. |
| `src/watchdog.py` | Liveness watchdog (cron it every ~30 min): Telegram alert if the autodraft cron line disappears or no run completes for 35 min. Born from a real 3-day silent outage. |
| `src/SKILL.md` | Doc **and** runtime state — holds the auto-rewritten `LEARNED-INSTRUCTIONS` block (keep the two HTML-comment markers intact). Ships empty. |
| `autodraft.env.example` | Template for all configuration (copy to `autodraft.env`). |

Runtime artifacts (auto-created, gitignored): `state.db` (SQLite tracking every
inbound message's status — created **empty on first run**), `logs/autodraft.log`,
`.lock`.

---

## Prerequisites

- **Python 3** with the Google API client: `pip install google-api-python-client google-auth`.
  Everything else is stdlib.
- **A Gmail OAuth helper** you provide as `auth.py` on the import path (see below).
- **The main LLM as a CLI** on `PATH` that takes `-p <prompt> --model <m> --output-format json`
  and returns `{"result": "..."}`. (This is what `cloud_llm()` shells out to; swap it
  for any provider — see Caveats.)
- **Optional:** a semantic index module `kb_index` exposing
  `retrieve(query, k) -> [{"source":..., "text":...}, ...]`. Absent → it falls back to
  dumping every `knowledge-base/products/*.md` file.
- **Optional:** an OpenAI-compatible endpoint for the privacy layer's local model.
- **Optional:** a Telegram bot (or any webhook) for review pings.

### The two modules you bring

The script imports these from directories you point it at; they are **not** shipped
(they're environment-specific):

- `auth.get_credentials(account, interactive=False)` → returns Google OAuth
  credentials for the named mailbox profile, or a falsy value if not authorized.
  Location set by `AUTODRAFT_AUTH_DIR`.
- `kb_index.retrieve(query, k=10)` → optional semantic retrieval (see above).
  Location set by `AUTODRAFT_KB_INDEX_DIR`.

---

## Install / setup

1. **Place the code.** Copy `src/*` into a working directory inside your project,
   e.g. `~/myproject/autodraft/`. By default the script treats **two directories up**
   as the project root that holds `knowledge-base/` (override with
   `AUTODRAFT_PROJECT_ROOT`).

2. **Provide the KB layout** under the project root (all optional but recommended):
   ```
   knowledge-base/
     products/            # *.md product facts (authoritative; used as fallback dump)
     writing-styles/      # owner.md, learned-owner.md (voice/tone/sign-off)
     from-emails/         # reply-learnings.md (auto-appended; created on first learn)
   ```

3. **Wire up Gmail auth.** Put your `auth.py` where `AUTODRAFT_AUTH_DIR` points and
   authorize the mailbox once (e.g. `python auth.py owner`). Keep OAuth
   token/credential files out of git.

4. **Configure.** `cp autodraft.env.example autodraft.env`, edit the values, then
   `source autodraft.env` (or export them) before running. At minimum set
   `AUTODRAFT_OWNER_EMAIL`, `AUTODRAFT_CC_EMAIL`, `AUTODRAFT_INTERNAL_DOMAIN`, and —
   if you want an attachment on first replies — `AUTODRAFT_ATTACHMENT`.

5. **Run it once:**
   ```bash
   ./src/run.sh          # logs to src/logs/autodraft.log; creates state.db empty
   ```
   Check Gmail Drafts for any drafted inquiries.

6. **Schedule it — two cron lines.** `crontab -e`:
   ```cron
   */10 * * * *  /home/you/myproject/autodraft/run.sh
   */30 * * * *  /usr/bin/python3 /home/you/myproject/autodraft/watchdog.py >> /home/you/myproject/autodraft/logs/watchdog.log 2>&1
   ```
   `flock` makes an overlapping run.sh tick a no-op while a slow run is still going.
   The watchdog is not optional in spirit: it alerts you if the run.sh line ever
   disappears from the crontab or runs stop completing (learned the hard way — a
   crontab edit during unrelated maintenance once silently killed drafting for days).

---

## Configuration

All knobs are environment variables (full list in `autodraft.env.example`):

| Variable | Default | Purpose |
|----------|---------|---------|
| `AUTODRAFT_ACCOUNT` | `owner` | Auth profile name passed to `get_credentials`. |
| `AUTODRAFT_OWNER_EMAIL` | `owner@example.com` | The mailbox drafted for. |
| `AUTODRAFT_CC_EMAIL` | `teammate@example.com` | Address always added to CC. |
| `AUTODRAFT_INTERNAL_DOMAIN` | `example.com` | Mail from this domain is never drafted. |
| `AUTODRAFT_PROJECT_ROOT` | two dirs up from the script | Root holding `knowledge-base/`. |
| `AUTODRAFT_AUTH_DIR` | `PROJECT_ROOT` | Where `auth.py` lives. |
| `AUTODRAFT_KB_INDEX_DIR` | `PROJECT_ROOT/kb` | Where `kb_index` lives (optional). |
| `AUTODRAFT_ATTACHMENT` | (none) | File attached on the first reply in a thread. |
| `CLAUDE_BIN` / `AUTODRAFT_MODEL` | `claude` / `opus` | Main-LLM CLI path and model. |
| `AUTODRAFT_MAX_DRAFTS` | `5` | Max drafts per run. |
| `AUTODRAFT_SCAN_WINDOW` | `newer_than:4d` | Gmail query window for unread mail. |
| `AUTODRAFT_PRIVACY` | `1` (in `run.sh`) | Enable the PII masking layer. |
| `AUTODRAFT_LOCAL_ENDPOINT` / `AUTODRAFT_LOCAL_MODEL` | `localhost:8000` / `your-local-model` | Privacy layer's local model. |
| `AUTODRAFT_LOCAL_API_KEY` | (none) | Key for that endpoint, if any (read from env, never hardcoded). |
| `AUTODRAFT_TG_TOKEN_FILE` / `AUTODRAFT_TG_CHAT_FILE` / `AUTODRAFT_TG_CHAT` | (none) | Optional chat-ping destination. |

---

## Caveats

- **Swappable LLM backend.** `cloud_llm()` in `autodraft.py` shells out to a local CLI;
  the privacy layer's `_chat()` in `privacy.py` calls an OpenAI-compatible HTTP endpoint.
  Point either at whatever provider/model you like — that's the only change needed.
  **Bring your own credentials:** no API key is hardcoded anywhere; the local endpoint's
  key (if any) comes from `AUTODRAFT_LOCAL_API_KEY`.
- **Bring your own `auth.py` and (optionally) `kb_index`.** They're environment-specific
  and intentionally not shipped; expected signatures are documented above. Without
  `kb_index`, retrieval degrades to a full product-file dump.
- **Learned state is created empty.** `state.db`, `reply-learnings.md`, and the
  `LEARNED-INSTRUCTIONS` block in `SKILL.md` all start empty and populate as the owner
  sends replies. No learned data is shipped.
- **What was stripped for sharing.** The original ran against a specific company's
  mailbox, product line, price-list PDF, model endpoint, and chat group. All of that is
  genericized here (`example.com`, "your product", a placeholder local endpoint,
  placeholder chat IDs). The real learned instructions, the populated `state.db`, and
  runtime `logs/` were **not** copied.
- **Privacy layer is best-effort, not a guarantee.** Masking combines deterministic
  regex (emails/phones/cards/URLs/order numbers) with model NER (person/org/address),
  plus a pre-send tripwire that refuses to send if hard PII survived. If NER is down or
  the tripwire fires, the message is drafted entirely on the local model. Review drafts
  before sending.
