# CRM Contact Archive

A local, file-plus-SQLite CRM for a small operation. It keeps a **markdown
file per contact** (human-editable, git-friendly) alongside a **SQLite store**
that archives sent/received mail and maintains a merged, deduplicated contact
record with a rolling activity summary. It's designed to be the "who is this and
what have we said to them" memory an assistant/agent can query before drafting a
reply, and a cheap dedup key so the same person isn't logged twice.

## What it does

- **`contacts` table** — one row per email address, merged over time: name,
  company, role, phone, first/last seen, message count, an append-only
  `activity_log`, and a condensed `activity_summary`.
- **`emails` table** — a local archive of messages (Gmail-style records), with a
  `body_new` column holding only the sender's newest text (quoted reply chains
  stripped) so consumers don't re-read the whole thread every time.
- **Rolling-checkpoint summaries** — old message chunks are "sealed" into a
  `base_summary` and never re-summarized; only the recent tail is reprocessed on
  each update (keeps summarization cheap as history grows).
- **Markdown mirror** — a human directory of `customers/`, `leads/`,
  `partners/` folders, one file per relationship, indexed by `INDEX.md`, seeded
  from `_TEMPLATE-contact.md`.

## How it works

| File | Role |
|------|------|
| `src/db.py` | The whole data layer: schema, connection (WAL + migrations), and all upsert/query/summary/export functions. |
| `src/config.py` | Paths (DB + CSV, portable / env-overridable) and `strip_quoted()`, the quoted-reply stripper that produces `body_new`. |
| `src/_TEMPLATE-contact.md` | Blank per-contact markdown record. |
| `src/INDEX.md` | Explains the `customers/` `leads/` `partners/` folder layout and naming convention. |

The DB is created and migrated automatically the first time you call
`db.conn()` — there's no separate "build" step. `emails` is populated by
`upsert_email(c, rec)` where `rec` is a Gmail-style dict (`id`, `threadId`,
`from`, `to`, `subject`, `body`, …). `contacts` is grown by
`upsert_contact(c, email, when, name=…, company=…, …)`. Both are idempotent, so
you can re-run an ingest safely.

### Minimal usage

```python
import db

c = db.conn()                                  # creates/migrates ~/myproject/crm/contacts.db

# archive an email
db.upsert_email(c, {
    "id": "abc123", "threadId": "t1", "account": "me",
    "internalDate": 1720000000000, "date": "2026-07-06",
    "from": "user@example.com", "to": "owner@example.com",
    "subject": "Quote request", "body": "Hi, can you send pricing?\n\nOn ... wrote:\n> old",
})

# grow the contact record
db.upsert_contact(c, "user@example.com", when="2026-07-06",
                  name="A. User", company="Acme", activity_line="asked for pricing")

# read it back
print(db.get_contact(c, "user@example.com")["message_count"])
for m in db.emails_for_contact(c, "user@example.com"):
    print(m["date"], m["subject"], m["body_new"])

c.commit()
db.export_csv(c)                               # snapshot contacts -> contacts.csv
```

## Prerequisites

- Python 3.8+ — standard library only (`sqlite3`, `csv`, `json`, `re`). No pip
  installs required.
- An email source (optional) if you want to auto-populate `emails`. This package
  ships only the store + helpers; wiring a Gmail/IMAP fetch that yields the
  record dicts above is up to you.

## Install / setup

1. Copy this folder into your project (the two `.md` files under `src/` are your
   starting templates — copy them into a real `crm/` directory).
2. Set where the data lives. Either export env vars or edit the defaults in
   `src/config.py`:
   - `CRM_PROJECT_ROOT` (default: the folder above `src/`) — DB/CSV go in
     `<root>/crm/`.
   - `CRM_DB`, `CRM_CSV` — override the exact file paths if you prefer.
   See `config.example.json` for the shape (it's documentation; the code reads
   env vars, not the JSON).
3. `import db; c = db.conn()` — the SQLite file and schema are created on first
   call. Nothing else to bootstrap.
4. For the markdown side: create `crm/customers/`, `crm/leads/`,
   `crm/partners/`, drop in `INDEX.md`, and copy `_TEMPLATE-contact.md` per new
   contact (naming: `company-or-name-city.md`).

## Config

| Setting | Env var | Default | Meaning |
|---------|---------|---------|---------|
| Project root | `CRM_PROJECT_ROOT` | folder above `src/` | Base for the `crm/` data dir. |
| DB path | `CRM_DB` | `<root>/crm/contacts.db` | The SQLite archive. |
| CSV path | `CRM_CSV` | `<root>/crm/contacts.csv` | Where `export_csv()` writes. |
| Archive cap | arg to `prune_emails(c, n)` | unlimited (`0`) | Keep newest N emails per account; `0`/`None` = never prune. |

## Caveats

- **No data is shipped.** This is schema + code + blank templates only. The real
  populated database, CSV export, and the per-contact `customers/` `leads/`
  `partners/` folders are intentionally **excluded** — they contain private
  contact information.
- **Bring your own ingest.** `upsert_email` expects Gmail-style record dicts;
  you supply the fetch (Gmail API, IMAP, mbox import, …). The summary helpers
  (`set_summary`, `seal_base`) store text you generate — if you want automatic
  condensing you plug in your **own** LLM/summarizer; none is bundled and no
  model endpoint is hardcoded (bring-your-own model backend).
- **WAL mode** is on so a cron writer and interactive reader can coexist; expect
  `contacts.db-wal` / `-shm` sidecar files.
- `strip_quoted()` is deliberately conservative — if stripping would empty the
  body (bottom-posted or pure-quote replies) it keeps the original text.
