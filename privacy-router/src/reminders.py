#!/usr/bin/env python3
"""Shared reminder/scheduled-job queue for chat agents.

Why: an agent that cannot schedule will either hand-roll one-shot crontab
scripts, or worse, just *say* "I'll ping you at 5pm" with nothing behind it
(a hallucinated capability — the model does not run between messages). This
is one auditable SQLite queue every agent writes to, drained by a per-minute
cron:

    * * * * * /usr/bin/python3 /path/to/reminders.py fire >> fire.log 2>&1

Kinds:
  ping — at fire time, send `text` verbatim to `chat_id` via the Telegram bot.
  task — at fire time, run `text` as an INSTRUCTION through the private agent
         loop (private_agent.run — with its CRM/email/KB tools) and post the
         answer. Use for conditional reminders like "ping only if we haven't
         replied to X" — the agent checks at fire time, then reports.

CLI:
  reminders.py add "YYYY-MM-DD HH:MM" <chat_id> <ping|task> "<text>" [--by NAME]
  reminders.py list [--all]
  reminders.py cancel <id>
  reminders.py fire

Config (env):
  TG_BOT_TOKEN or TG_BOT_TOKEN_FILE   Telegram bot credentials
  REMINDERS_DB                        sqlite path (default: reminders.db next to this file)

Stdlib only. Failed fires retry (3 attempts) then post a warning to the chat.
"""
import argparse, json, os, sqlite3, sys, time, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("REMINDERS_DB", os.path.join(HERE, "reminders.db"))
MAX_ATTEMPTS = 3
TIME_FMT = "%Y-%m-%d %H:%M"


def _token():
    t = os.environ.get("TG_BOT_TOKEN")
    if t:
        return t.strip()
    path = os.environ.get("TG_BOT_TOKEN_FILE", "")
    if path and os.path.exists(path):
        return open(path).read().strip()
    raise RuntimeError("set TG_BOT_TOKEN or TG_BOT_TOKEN_FILE")


def _db():
    c = sqlite3.connect(DB, timeout=30)
    c.execute("""CREATE TABLE IF NOT EXISTS reminders(
        id INTEGER PRIMARY KEY,
        created_ts TEXT NOT NULL,
        created_by TEXT NOT NULL,
        chat_id INTEGER NOT NULL,
        when_local TEXT NOT NULL,          -- 'YYYY-MM-DD HH:MM' local time
        when_epoch REAL NOT NULL,
        kind TEXT NOT NULL,                -- 'ping' | 'task'
        text TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',  -- pending | done | failed | cancelled
        attempts INTEGER NOT NULL DEFAULT 0,
        fired_ts TEXT,
        result TEXT)""")
    return c


def _tg_send(chat_id, text):
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text[:4000]}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{_token()}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.loads(r.read())
    if not out.get("ok"):
        raise RuntimeError(f"telegram: {out}")


def add(when_local, chat_id, kind, text, created_by="agent"):
    """Queue a reminder. Returns (id, when_local). Raises ValueError on bad input."""
    when_local = (when_local or "").strip()
    try:
        when_epoch = time.mktime(time.strptime(when_local, TIME_FMT))
    except ValueError:
        raise ValueError(f"bad time {when_local!r} — use 'YYYY-MM-DD HH:MM' (24h, local)")
    if kind not in ("ping", "task"):
        raise ValueError("kind must be 'ping' or 'task'")
    if not (text or "").strip():
        raise ValueError("text is empty")
    c = _db()
    cur = c.execute(
        "INSERT INTO reminders(created_ts, created_by, chat_id, when_local, when_epoch, kind, text) "
        "VALUES(?,?,?,?,?,?,?)",
        (time.strftime("%Y-%m-%d %H:%M:%S"), created_by, int(chat_id),
         when_local, when_epoch, kind, text.strip()))
    c.commit(); c.close()
    return cur.lastrowid, when_local


def cancel(rid):
    c = _db()
    n = c.execute("UPDATE reminders SET status='cancelled' WHERE id=? AND status='pending'",
                  (int(rid),)).rowcount
    c.commit(); c.close()
    return n == 1


def list_rows(all_rows=False):
    c = _db()
    q = "SELECT id, when_local, chat_id, kind, status, created_by, text FROM reminders"
    if not all_rows:
        q += " WHERE status='pending'"
    rows = [dict(zip(("id", "when", "chat_id", "kind", "status", "by", "text"), r))
            for r in c.execute(q + " ORDER BY when_epoch")]
    c.close()
    return rows


def _fire_one(row):
    chat_id, kind, text = row["chat_id"], row["kind"], row["text"]
    if kind == "ping":
        _tg_send(chat_id, f"⏰ Reminder: {text}")
        return "ping sent"
    # task: run the instruction through the private agent loop, post its answer
    sys.path.insert(0, HERE)
    import private_agent
    answer, files = private_agent.run(text)
    answer = (answer or "").strip()
    if not answer:
        raise RuntimeError("agent gave no answer")
    if files:
        answer += "\n\n(Note: the agent located files; ask in chat to have them sent.)"
    _tg_send(chat_id, f"⏰ Scheduled task result:\n\n{answer}")
    return "task ran: " + answer[:500]


def fire():
    now = time.time()
    c = _db()
    due = [dict(zip(("id", "chat_id", "kind", "text", "attempts"), r)) for r in c.execute(
        "SELECT id, chat_id, kind, text, attempts FROM reminders "
        "WHERE status='pending' AND when_epoch<=? ORDER BY when_epoch", (now,))]
    c.close()
    for row in due:
        c = _db()
        c.execute("UPDATE reminders SET attempts=attempts+1 WHERE id=?", (row["id"],))
        c.commit(); c.close()
        try:
            result = _fire_one(row)
            status = "done"
        except Exception as e:
            result = f"attempt {row['attempts'] + 1} failed: {e}"
            status = "failed" if row["attempts"] + 1 >= MAX_ATTEMPTS else "pending"
            if status == "failed":
                try:
                    _tg_send(row["chat_id"],
                             f"⚠️ Reminder #{row['id']} failed {MAX_ATTEMPTS} times and was dropped: "
                             f"{row['text'][:200]}")
                except Exception:
                    pass
        print(f"{time.strftime('%F %T')} #{row['id']} {status}: {result}", flush=True)
        c = _db()
        c.execute("UPDATE reminders SET status=?, fired_ts=?, result=? WHERE id=?",
                  (status, time.strftime("%Y-%m-%d %H:%M:%S"), result[:1000], row["id"]))
        c.commit(); c.close()


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("when"); a.add_argument("chat_id", type=int)
    a.add_argument("kind", choices=("ping", "task")); a.add_argument("text")
    a.add_argument("--by", default="agent")
    l = sub.add_parser("list"); l.add_argument("--all", action="store_true")
    ca = sub.add_parser("cancel"); ca.add_argument("id", type=int)
    sub.add_parser("fire")
    ns = ap.parse_args()
    if ns.cmd == "add":
        rid, when = add(ns.when, ns.chat_id, ns.kind, ns.text, created_by=ns.by)
        print(f"queued #{rid} for {when}")
    elif ns.cmd == "list":
        for r in list_rows(ns.all):
            print(f"#{r['id']} {r['when']} [{r['status']}] {r['kind']} chat={r['chat_id']} "
                  f"by={r['by']}: {r['text'][:100]}")
    elif ns.cmd == "cancel":
        print("cancelled" if cancel(ns.id) else "not found / not pending")
    elif ns.cmd == "fire":
        fire()


if __name__ == "__main__":
    main()
