# privacy-router — label your data, route private queries to a private LLM

Keeps sensitive data away from the cloud LLM in a chat-with-your-agent setup. The
cloud agent (e.g. Claude) is great, but every file it reads enters cloud context —
so a question like *"what balance does customer X have with us?"* would ship the
customer's ledger to the cloud. This skill routes such queries to a **private
model** (a tool-calling LLM you trust — e.g. hosted Nemotron, or fully local via
llama.cpp/vLLM) that looks the answer up itself with **on-box, read-only tools**.

Battle-tested lessons baked in (learned the hard way):

- **Targeted beats strict.** Routing *every* message to the private model (strict,
  label-decides-everything) breaks the assistant: the private model lacks the cloud
  agent's context and tooling. Route only queries whose *intent* touches private
  data; let everything else stay with the cloud agent. (`intent.py`)
- **The private model needs full context.** Without recent chat history it can't
  resolve "that customer" / "the adapter we discussed" and is helpless. Pass the
  last ~20 messages.
- **A single-shot call will hallucinate actions.** Given context but no tools, a
  capable model says "let me check the database…" — and nothing happens, because
  nothing runs after its reply. Give it a real tool loop. (`private_agent.py`)
- **Fail closed.** Any router/answerer error must degrade to "kept it on-box,
  couldn't answer" — never silently fall through to the cloud. A false positive
  costs quality; a false negative is a leak.
- **Embeddings miss proper nouns.** Semantic retrieval won't find rare customer
  names; the agent's keyword tools (`search_contacts`, `search_emails`) do.

## Files

| File | What it does |
|------|--------------|
| `src/intent.py` | Deterministic query-intent classifier (regex, no network): balances/owed, refunds/complaints/disputes, invoices+party, hard PII → private. Customize the product terms so catalog questions stay public. |
| `src/data_labels.py` | Source-of-truth labels for *data* (public vs private), driven by a JSON manifest; default private, web-scraped = public, fail closed. Use in ingest pipelines and label-aware routing. |
| `src/data-classification.example.json` | Manifest template — list only documents you'd publish. |
| `src/private_agent.py` | Tool-calling loop for the private model: `search_contacts`, `search_emails`, `read_email` (CRM sqlite), optional `kb_search` (semantic index), plus `find_files`/`send_file` for document delivery. Read-only lookups, ≤6 model calls, ≤120s wall, forced final answer. `run()` returns `(answer, files)`. *(The original has since gained `read_attachment`/`search_attachments` tools serving pre-extracted attachment text from an FTS5 table filled at mail-ingest time — see the email-knowledge-extract skill's notes; not ported here because it's coupled to the mailbox downloader.)* Also: `schedule_reminder`/`list_reminders`/`cancel_reminder` (2026-07-23) — an agent that cannot schedule will *pretend* to ("I'll ping you at 5pm") and nothing happens; these write to the shared queue in `src/reminders.py`, and the system prompt forbids promising a future ping without a successful `schedule_reminder` call in the same turn. |
| `src/reminders.py` | Shared reminder/scheduled-job queue (SQLite, stdlib-only). Kinds: `ping` (send text verbatim at fire time via the Telegram bot) and `task` (run the text as an instruction through `private_agent.run` at fire time — with its lookup tools — and post the result; enables conditional reminders like "ping only if we haven't replied to X"). Drained by a per-minute cron: `* * * * * python3 /path/to/reminders.py fire >> fire.log 2>&1`. 3 attempts, then a ⚠️ notice to the chat. Env: `TG_BOT_TOKEN`/`TG_BOT_TOKEN_FILE`, `REMINDERS_DB`. |

## Wiring (gateway side)

In your chat gateway, before the cloud-agent turn:

```python
import intent, private_agent
priv, why = intent.is_private(text)
if priv:
    history = last_n_messages(chat_id, 20)          # full context — essential
    files = []
    try:
        answer, files = private_agent.run(text, history)   # files: [{path, caption}]
        reply = "🔒 Private — answered on-box:\n\n" + answer
    except Exception as e:
        reply = f"Kept it on-box; the private answerer failed ({e}). Try again."
    send(reply)
    upload_to_chat(files)   # documents the agent queued via send_file
    return                                           # NEVER fall through to cloud
# ...normal cloud-agent turn...
```

Add a `/cloud <msg>` escape-hatch command that bypasses the gate for when the
classifier misfires. See the **telegram-gateway** skill for a full integration.

**File delivery.** The agent can hand over actual documents, not just talk about
them: `find_files` searches the workspace by filename; `send_file` queues a path
(the agent never uploads anything itself — the *gateway* does the upload after the
loop). Guardrails: paths must resolve inside `WORKSPACE_ROOT`, ≤49 MB, and
credential-like paths (`token`, `secret`, `password`, `.git/`, `venv/`, …) are
denied outright. The equivalent CLI contract (`privacy_route.py --json`) returns
`{"decision": "private", "answer": "...", "files": [{path, caption}, ...]}`.

## Config

| Env | Meaning |
|-----|---------|
| `PRIVATE_LLM_URL` / `PRIVATE_LLM_MODEL` | OpenAI-compatible chat endpoint + model (must support tool calling). Point at localhost for full privacy. |
| `PRIVATE_LLM_KEY` / `PRIVATE_LLM_KEY_FILE` | API key (or KEY=value file). |
| `CONTACTS_DB` | sqlite CRM database (`contacts` + `emails` tables — see **crm-contacts**). |
| `WORKSPACE_ROOT` | Directory `find_files`/`send_file` are confined to (default: cwd). |
| `DATA_CLASSIFICATION` | Path to the labels manifest. |

Honest caveat: if the private model is a hosted endpoint (OpenRouter etc.), "private"
means "not sent to your primary cloud provider" — the query still leaves the box.
Full privacy arrives when you point `PRIVATE_LLM_URL` at local hardware; the routing
logic doesn't change.
