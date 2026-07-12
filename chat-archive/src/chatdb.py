#!/usr/bin/env python3
"""Chat archive — every inbound message + assistant reply in a searchable SQLite DB.

One row per message: inbound from a person, outbound from the assistant, or a relayed
email. Full-text search runs over the message text via FTS5. Searchable by keyword, by
date range, and by project.

The `project` column is filled in *later* by classify.py (from the conversation
content), because a single chat room carries several projects over time — so the
room can't be the project tag. It stays NULL until classified.

record() is called from the gateway hot path: it is cheap, self-contained, and never
raises into the caller (a broken archive must never break the chat).

CLI:
  chatdb.py search "onboarding"                     # keyword (FTS5)
  chatdb.py search "invoice" --since 2026-06-01 --project billing
  chatdb.py recent --project research --limit 20
  chatdb.py projects                                # counts per project
  chatdb.py stats
"""
import os, sys, sqlite3, threading, datetime, time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("CHATDB_PATH", os.path.join(HERE, "chat.db"))

_lock = threading.Lock()
_conn = None

# Set by record() after every insert; the in-process classify worker (classify.py,
# started by the gateway) waits on it and tags the new row within ~1-2s. If no worker
# is running (CLI / cron context) setting it is a harmless no-op. This is what makes
# tagging real-time instead of waiting for a polling cron.
_new_msg = threading.Event()

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
  id         INTEGER PRIMARY KEY,
  ts         TEXT NOT NULL,          -- 'YYYY-MM-DD HH:MM:SS' local time
  epoch      REAL NOT NULL,          -- unix seconds (range/sort)
  chat_id    INTEGER,
  chat_title TEXT,
  sender     TEXT,                   -- 'user','assistant','peer',...
  direction  TEXT,                   -- 'in' | 'out'
  kind       TEXT,                   -- 'text','command','email','file'
  project    TEXT,                   -- NULL until classify.py tags it
  session_id TEXT,
  text       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_epoch   ON messages(epoch);
CREATE INDEX IF NOT EXISTS idx_msg_project ON messages(project);
CREATE INDEX IF NOT EXISTS idx_msg_chat    ON messages(chat_id);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  text, project, chat_title,
  content='messages', content_rowid='id',
  tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS msg_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, text, project, chat_title)
  VALUES (new.id, new.text, coalesce(new.project,''), coalesce(new.chat_title,''));
END;
CREATE TRIGGER IF NOT EXISTS msg_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text, project, chat_title)
  VALUES ('delete', old.id, old.text, coalesce(old.project,''), coalesce(old.chat_title,''));
END;
CREATE TRIGGER IF NOT EXISTS msg_au AFTER UPDATE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text, project, chat_title)
  VALUES ('delete', old.id, old.text, coalesce(old.project,''), coalesce(old.chat_title,''));
  INSERT INTO messages_fts(rowid, text, project, chat_title)
  VALUES (new.id, new.text, coalesce(new.project,''), coalesce(new.chat_title,''));
END;
"""


def _get():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA busy_timeout=5000")
        _conn.executescript(SCHEMA)
        _conn.commit()
    return _conn


def record(text, direction, sender=None, chat_id=None, chat_title=None,
           kind="text", session_id=None, project=None, epoch=None):
    """Insert one message. Best-effort: never raises into the caller."""
    if not text or not str(text).strip():
        return
    try:
        ep = float(epoch) if epoch is not None else time.time()
        ts = datetime.datetime.fromtimestamp(ep).strftime("%Y-%m-%d %H:%M:%S")
        with _lock:
            c = _get()
            c.execute(
                "INSERT INTO messages(ts,epoch,chat_id,chat_title,sender,direction,"
                "kind,project,session_id,text) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (ts, ep, chat_id, chat_title, sender, direction, kind, project,
                 session_id, str(text)))
            c.commit()
        _new_msg.set()  # wake the real-time classify worker (no-op if none running)
    except Exception:
        pass  # archiving is best-effort; never propagate into the gateway


# ---- read side (used by the CLI and by classify.py) -------------------------
def _parse_day(s, end=False):
    """Accept 'YYYY-MM-DD' (or an int epoch). end=True -> end of that day."""
    if s is None:
        return None
    s = str(s).strip()
    if s.isdigit() and len(s) >= 6:
        return float(s)
    d = datetime.datetime.strptime(s, "%Y-%m-%d")
    if end:
        d += datetime.timedelta(days=1)
    return d.timestamp()


def search(query=None, project=None, since=None, until=None, chat_id=None,
           sender=None, limit=50):
    c = _get()
    args = []
    if query:
        sql = ("SELECT m.id,m.ts,m.sender,m.direction,m.kind,m.project,m.chat_title,m.text "
               "FROM messages_fts f JOIN messages m ON m.id=f.rowid "
               "WHERE messages_fts MATCH ? ")
        args.append(query)
    else:
        sql = ("SELECT m.id,m.ts,m.sender,m.direction,m.kind,m.project,m.chat_title,m.text "
               "FROM messages m WHERE 1=1 ")
    if project:
        sql += "AND m.project = ? "; args.append(project)
    if sender:
        sql += "AND m.sender = ? "; args.append(sender)
    if since:
        sql += "AND m.epoch >= ? "; args.append(_parse_day(since))
    if until:
        sql += "AND m.epoch < ? "; args.append(_parse_day(until, end=True))
    if chat_id:
        sql += "AND m.chat_id = ? "; args.append(int(chat_id))
    sql += "ORDER BY m.epoch DESC LIMIT ?"; args.append(int(limit))
    return c.execute(sql, args).fetchall()


# ---- CLI --------------------------------------------------------------------
def _print_rows(rows):
    if not rows:
        print("(no matches)"); return
    for rid, ts, sender, direction, kind, project, title, text in reversed(rows):
        arrow = "→" if direction == "out" else "←"
        who = sender or ("assistant" if direction == "out" else "?")
        proj = f"[{project}]" if project else "[unclassified]"
        room = f" ({title})" if title else ""
        body = " ".join(str(text).split())
        if len(body) > 280:
            body = body[:280] + " …"
        print(f"{ts}  {proj:<22}{room}\n  {arrow} {who}: {body}\n")


def _cli(argv):
    import argparse
    p = argparse.ArgumentParser(prog="chatdb", description="Search the chat archive.")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("search", help="full-text keyword search")
    s.add_argument("query")
    s.add_argument("--project"); s.add_argument("--since"); s.add_argument("--until")
    s.add_argument("--sender"); s.add_argument("--chat-id", type=int)
    s.add_argument("--limit", type=int, default=50)

    r = sub.add_parser("recent", help="most recent messages (optionally filtered)")
    r.add_argument("--project"); r.add_argument("--since"); r.add_argument("--until")
    r.add_argument("--sender"); r.add_argument("--chat-id", type=int)
    r.add_argument("--limit", type=int, default=30)

    m = sub.add_parser("semantic", help="meaning-based search (vector embeddings), not keywords")
    m.add_argument("query")
    m.add_argument("--project"); m.add_argument("--since"); m.add_argument("--until")
    m.add_argument("--limit", type=int, default=10)
    m.add_argument("--rerank", action="store_true",
                   help="add an LLM rerank pass over the vector hits (higher quality, slower)")

    sub.add_parser("projects", help="message counts per project")
    sub.add_parser("stats", help="archive summary")

    a = p.parse_args(argv)
    if a.cmd == "search":
        _print_rows(search(a.query, a.project, a.since, a.until, a.chat_id, a.sender, a.limit))
    elif a.cmd == "recent":
        _print_rows(search(None, a.project, a.since, a.until, a.chat_id, a.sender, a.limit))
    elif a.cmd == "semantic":
        import embed  # vector embeddings for retrieval; reasoning stays on the chat LLM
        rows = embed.search(a.query, max(a.limit, 30) if a.rerank else a.limit,
                            a.project, a.since, a.until)
        if a.rerank:
            import recall  # the chat LLM reranks the vector hits by intent
            rows = recall.rerank(a.query, rows, a.limit)
        _print_rows(rows)
    elif a.cmd == "projects":
        c = _get()
        rows = c.execute(
            "SELECT coalesce(project,'(unclassified)') p, count(*) n FROM messages "
            "GROUP BY p ORDER BY n DESC").fetchall()
        for proj, n in rows:
            print(f"  {n:>6}  {proj}")
    elif a.cmd == "stats":
        c = _get()
        n = c.execute("SELECT count(*) FROM messages").fetchone()[0]
        unc = c.execute("SELECT count(*) FROM messages WHERE project IS NULL").fetchone()[0]
        span = c.execute("SELECT min(ts), max(ts) FROM messages").fetchone()
        print(f"messages: {n}  |  unclassified: {unc}\nspan: {span[0]} .. {span[1]}\ndb: {DB_PATH}")
    else:
        p.print_help()


if __name__ == "__main__":
    _cli(sys.argv[1:])
