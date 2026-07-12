"""SQLite contacts DB with merge + per-contact activity summary.

Tables:
  contacts(email PK, name, company, role, phone, first_seen, last_seen,
           message_count, activity_log, activity_summary, ...)
  emails(id PK, thread_id, account, internal_date, date, from_addr, to_addr, cc,
         subject, labels, has_attachment, attachments, body, body_new, fetched)
  processed(msg_id PK, processed_at)
  meta(key PK, value)

Ships schema + code only — the populated DB is never included. A separate downloader
(not part of this skill) is expected to fill the `emails` table from your mailbox.
"""
import sqlite3, os, csv, json
from kbconf import CONTACTS_DB, CONTACTS_CSV, strip_quoted

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
  email TEXT PRIMARY KEY, name TEXT, company TEXT, role TEXT, phone TEXT,
  first_seen TEXT, last_seen TEXT, message_count INTEGER DEFAULT 0,
  activity_log TEXT DEFAULT '', activity_summary TEXT DEFAULT '',
  summarized_ids TEXT DEFAULT '',
  -- rolling-checkpoint summarization: base_summary condenses everything up to the
  -- last SEALED chunk boundary; base_ids are the msg ids folded into it. The tail
  -- (msgs not in base_ids) is fed raw alongside base_summary on each update, so old
  -- chunks are never re-summarized. When the tail grows past a budget it is sealed
  -- into base_summary and the boundary advances.
  base_summary TEXT DEFAULT '', base_ids TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS emails (
  id TEXT PRIMARY KEY, thread_id TEXT, account TEXT, internal_date INTEGER,
  date TEXT, from_addr TEXT, to_addr TEXT, cc TEXT, subject TEXT,
  labels TEXT, has_attachment INTEGER DEFAULT 0, attachments TEXT,
  body TEXT, body_new TEXT, fetched TEXT
);
CREATE INDEX IF NOT EXISTS idx_emails_acct_date ON emails(account, internal_date DESC);
CREATE TABLE IF NOT EXISTS processed (msg_id TEXT PRIMARY KEY, processed_at TEXT);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
-- emails whose extraction LLM call timed out: deferred for retry, capped so a
-- genuinely un-processable email can't block the oldest-first queue forever.
CREATE TABLE IF NOT EXISTS extract_attempts (msg_id TEXT PRIMARY KEY, n INTEGER DEFAULT 0, last_at TEXT);
"""


def conn():
    os.makedirs(os.path.dirname(CONTACTS_DB), exist_ok=True)
    c = sqlite3.connect(CONTACTS_DB, timeout=60)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=60000")   # wait on locks (cron + other writers may overlap)
    c.execute("PRAGMA journal_mode=WAL")     # concurrent readers + 1 writer; avoids 'database is locked'
    c.executescript(SCHEMA)
    # Lightweight migrations for tables that predate a column.
    cols = {r[1] for r in c.execute("PRAGMA table_info(emails)").fetchall()}
    if "body_new" not in cols:
        c.execute("ALTER TABLE emails ADD COLUMN body_new TEXT")
    if "thread_id" not in cols:
        c.execute("ALTER TABLE emails ADD COLUMN thread_id TEXT")
    # thread_id index lives here (not in SCHEMA): the column may have just been added.
    c.execute("CREATE INDEX IF NOT EXISTS idx_emails_thread ON emails(thread_id, internal_date)")
    ccols = {r[1] for r in c.execute("PRAGMA table_info(contacts)").fetchall()}
    if "summarized_ids" not in ccols:
        c.execute("ALTER TABLE contacts ADD COLUMN summarized_ids TEXT DEFAULT ''")
    if "base_summary" not in ccols:
        c.execute("ALTER TABLE contacts ADD COLUMN base_summary TEXT DEFAULT ''")
    if "base_ids" not in ccols:
        c.execute("ALTER TABLE contacts ADD COLUMN base_ids TEXT DEFAULT ''")
    c.commit()
    return c


def is_processed(c, msg_id):
    return c.execute("SELECT 1 FROM processed WHERE msg_id=?", (msg_id,)).fetchone() is not None


def mark_processed(c, msg_id, when):
    c.execute("INSERT OR IGNORE INTO processed(msg_id, processed_at) VALUES (?,?)", (msg_id, when))


def bump_extract_attempt(c, msg_id, when):
    """Record one more timed-out extraction attempt for this email; return the new count."""
    c.execute("""INSERT INTO extract_attempts(msg_id, n, last_at) VALUES (?,1,?)
                 ON CONFLICT(msg_id) DO UPDATE SET n=n+1, last_at=excluded.last_at""",
              (msg_id, when))
    return c.execute("SELECT n FROM extract_attempts WHERE msg_id=?", (msg_id,)).fetchone()[0]


# --- Email archive (the local corpus) ---------------------------------------
# Mapping between the corpus record dict (Gmail-style keys) and DB columns.
_EMAIL_COLS = [
    ("id", "id"), ("threadId", "thread_id"), ("account", "account"),
    ("internalDate", "internal_date"), ("date", "date"), ("from", "from_addr"),
    ("to", "to_addr"), ("cc", "cc"), ("subject", "subject"), ("labels", "labels"),
    ("has_attachment", "has_attachment"), ("attachments", "attachments"),
    ("body", "body"), ("fetched", "fetched"),
]


def known_email_ids(c):
    return {r[0] for r in c.execute("SELECT id FROM emails").fetchall()}


def email_count(c):
    return c.execute("SELECT COUNT(*) FROM emails").fetchone()[0]


def upsert_email(c, rec):
    """Insert/replace one corpus record (dict with Gmail-style keys).

    Computes body_new (quoted reply chain stripped) so extraction reads only the
    sender's new text. The full body is still stored for forwarded-original parsing.
    """
    vals = []
    for rk, _ in _EMAIL_COLS:
        v = rec.get(rk)
        if rk in ("labels", "attachments"):
            v = json.dumps(v or [], ensure_ascii=False)
        elif rk == "has_attachment":
            v = 1 if v else 0
        vals.append(v)
    cols = [col for _, col in _EMAIL_COLS] + ["body_new"]
    vals.append(strip_quoted(rec.get("body") or ""))
    c.execute(f"INSERT OR REPLACE INTO emails({','.join(cols)}) VALUES ({','.join('?' * len(vals))})", vals)


def _email_row_to_rec(row):
    """DB row -> corpus record dict (the shape process_emails.py expects)."""
    return {
        "id": row["id"], "threadId": row["thread_id"],
        "account": row["account"], "internalDate": row["internal_date"],
        "date": row["date"], "from": row["from_addr"], "to": row["to_addr"], "cc": row["cc"],
        "subject": row["subject"], "labels": json.loads(row["labels"] or "[]"),
        "has_attachment": bool(row["has_attachment"]),
        "attachments": json.loads(row["attachments"] or "[]"),
        "body": row["body"], "body_new": row["body_new"] or row["body"],
        "fetched": row["fetched"],
    }


def all_emails(c):
    rows = c.execute("SELECT * FROM emails ORDER BY internal_date DESC").fetchall()
    return [_email_row_to_rec(r) for r in rows]


def thread(c, thread_id):
    """All archived messages in one conversation, oldest-first."""
    rows = c.execute("SELECT * FROM emails WHERE thread_id=? ORDER BY internal_date",
                     (thread_id,)).fetchall()
    return [_email_row_to_rec(r) for r in rows]


def prune_emails(c, n):
    """Keep only the newest N emails per account. n<=0 (or falsy) means UNLIMITED —
    no pruning, the archive grows without bound."""
    if not n or n <= 0:
        return                                   # unlimited archive: never delete
    for (acct,) in c.execute("SELECT DISTINCT account FROM emails").fetchall():
        c.execute("""DELETE FROM emails WHERE id IN (
                       SELECT id FROM emails WHERE account=?
                       ORDER BY internal_date DESC LIMIT -1 OFFSET ?)""", (acct, n))


def _pick(old, new):
    """Prefer an existing non-empty value; otherwise take the new one."""
    old = (old or "").strip()
    return old if old else (new or "").strip()


def upsert_contact(c, email, when, name="", company="", role="", phone="", activity_line=""):
    email = email.lower().strip()
    row = c.execute("SELECT * FROM contacts WHERE email=?", (email,)).fetchone()
    if row is None:
        log = f"- {when[:10]}: {activity_line}" if activity_line else ""
        c.execute("""INSERT INTO contacts(email,name,company,role,phone,first_seen,last_seen,
                     message_count,activity_log,activity_summary) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                  (email, name, company, role, phone, when, when, 1, log, ""))
    else:
        log = row["activity_log"] or ""
        if activity_line:
            log = (log + f"\n- {when[:10]}: {activity_line}").strip()
        c.execute("""UPDATE contacts SET name=?, company=?, role=?, phone=?,
                     last_seen=?, message_count=message_count+1, activity_log=? WHERE email=?""",
                  (_pick(row["name"], name), _pick(row["company"], company),
                   _pick(row["role"], role), _pick(row["phone"], phone),
                   when, log, email))


def set_summary(c, email, summary, summarized_ids=None):
    """Store the contact's summary; optionally record which msg ids it now folds in."""
    email = email.lower().strip()
    if summarized_ids is None:
        c.execute("UPDATE contacts SET activity_summary=? WHERE email=?", (summary, email))
    else:
        c.execute("UPDATE contacts SET activity_summary=?, summarized_ids=? WHERE email=?",
                  (summary, json.dumps(sorted(set(summarized_ids))), email))


def summarized_ids(c, email):
    row = c.execute("SELECT summarized_ids FROM contacts WHERE email=?",
                    (email.lower().strip(),)).fetchone()
    return set(json.loads(row["summarized_ids"] or "[]")) if row else set()


def get_base(c, email):
    """Return (base_summary, base_ids set) — the sealed checkpoint for this contact."""
    row = c.execute("SELECT base_summary, base_ids FROM contacts WHERE email=?",
                    (email.lower().strip(),)).fetchone()
    if not row:
        return "", set()
    return (row["base_summary"] or ""), set(json.loads(row["base_ids"] or "[]"))


def seal_base(c, email, base_summary, base_ids):
    """Advance the sealed checkpoint: fold the current tail into base_summary."""
    c.execute("UPDATE contacts SET base_summary=?, base_ids=? WHERE email=?",
              (base_summary, json.dumps(sorted(set(base_ids))), email.lower().strip()))


def emails_for_contact(c, addr):
    """All archived emails where this address appears in From/To/Cc, oldest-first."""
    addr = addr.lower().strip()
    like = f"%{addr}%"
    rows = c.execute(
        """SELECT * FROM emails
           WHERE lower(from_addr) LIKE ? OR lower(to_addr) LIKE ? OR lower(cc) LIKE ?
           ORDER BY internal_date""", (like, like, like)).fetchall()
    return [_email_row_to_rec(r) for r in rows]


def get_contact(c, email):
    return c.execute("SELECT * FROM contacts WHERE email=?", (email.lower().strip(),)).fetchone()


def export_csv(c):
    rows = c.execute("""SELECT email,name,company,role,phone,first_seen,last_seen,
                        message_count,activity_summary FROM contacts ORDER BY last_seen DESC""").fetchall()
    os.makedirs(os.path.dirname(CONTACTS_CSV), exist_ok=True)
    with open(CONTACTS_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["email", "name", "company", "role", "phone", "first_seen",
                    "last_seen", "message_count", "activity_summary"])
        for r in rows:
            w.writerow([r[k] for k in r.keys()])
    return len(rows)
