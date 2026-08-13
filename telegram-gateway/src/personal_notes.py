"""Personal notes — the owner's PRIVATE file store for the Telegram gateway.

the owner (2026-07-10): any file (photo, PDF, doc, anything) sent in his DM with NO
caption is a personal note. Notes live in their own store + SQLite db, separate
from the the workspace knowledge base, and are deliverable ONLY to:
  • his DM (chat C.OWNER_ID), or
  • a group whose only human member is the owner (bot + the owner, member_count == 2,
    verified live via getChatMemberCount + getChatMember, cached 10 min).
NEVER to the the workspace groups (Public / Wise / Private) or to any other user's DM.

Storage: workspace/personal/notes/<YYYYMMDD-HHMMSS>_<original-name>  (files)
         workspace/personal/notes.db                                  (metadata)
Notes may carry a `label` (human description, e.g. the email subject that came
with the file) and `keywords` (extracted from the file's content) — search()
matches both (the owner, 2026-07-11).
The personal/ tree is EXCLUDED from every shared index: file_reflex's workspace
walk, the private agent's find_files, docpipe/RAG, the CLIP media store and the
KB semantic index (which only sweeps knowledge-base/). Retrieval goes through
this module's search(), gated by allowed_chat().

CLI: python3 personal_notes.py list | search <words> | check <chat_id>
"""
import os, re, json, time, shutil, sqlite3, threading

import tgconf as C
import tg_api as TG

VLAD = C.OWNER_ID                       # the owner's Telegram user id == his DM chat id
PERSONAL_DIR = os.path.join(C.WORKSPACE_ROOT, "personal")
NOTES_DIR = os.path.join(PERSONAL_DIR, "notes")
DB = os.path.join(PERSONAL_DIR, "notes.db")
MAX_MB = 49

_LOCK = threading.Lock()
_CHAT_OK = {}                          # chat_id -> (verdict, checked_at); TTL below
CHAT_TTL = 600


def _db():
    os.makedirs(NOTES_DIR, exist_ok=True)
    con = sqlite3.connect(DB, timeout=10)
    con.execute("""CREATE TABLE IF NOT EXISTS notes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        orig_name TEXT,
        path TEXT NOT NULL,
        kind TEXT,
        size INTEGER,
        tg_file_id TEXT,
        label TEXT,
        keywords TEXT)""")
    # migrate a pre-2026-07-11 db (no label/keywords columns) in place
    cols = {r[1] for r in con.execute("PRAGMA table_info(notes)")}
    for c in ("label", "keywords"):
        if c not in cols:
            con.execute(f"ALTER TABLE notes ADD COLUMN {c} TEXT")
    return con


# ---- capture --------------------------------------------------------------------
def add(path, orig_name=None, tg_file_id=None, label=None, keywords=None):
    """Move a downloaded file into the personal store and record it. Returns note id.
    `label` is a human description (e.g. an email subject); `keywords` a list or
    comma-string of content keywords — both are matched by search()."""
    if isinstance(keywords, (list, tuple)):
        keywords = ", ".join(str(k).strip() for k in keywords if str(k).strip())
    orig = orig_name or os.path.basename(path)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^\w.\-]+", "_", orig)[:120]
    dest = os.path.join(NOTES_DIR, f"{stamp}_{safe}")
    n = 1
    while os.path.exists(dest):
        dest = os.path.join(NOTES_DIR, f"{stamp}_{n}_{safe}")
        n += 1
    with _LOCK:
        os.makedirs(NOTES_DIR, exist_ok=True)
        shutil.move(path, dest)
        con = _db()
        with con:
            cur = con.execute(
                "INSERT INTO notes(ts, orig_name, path, kind, size, tg_file_id, label, keywords) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (time.strftime("%Y-%m-%d %H:%M:%S"), orig, dest,
                 os.path.splitext(orig)[1].lstrip(".").lower() or "file",
                 os.path.getsize(dest), tg_file_id, label, keywords))
        con.close()
    return cur.lastrowid, dest


# ---- the privacy gate -------------------------------------------------------------
def allowed_chat(chat_id):
    """True only for the owner's DM or a live-verified bot+the owner-only group."""
    if chat_id == VLAD:
        return True
    if chat_id > 0:                    # someone else's DM
        return False
    hit = _CHAT_OK.get(chat_id)
    if hit and time.time() - hit[1] < CHAT_TTL:
        return hit[0]
    ok = False
    try:
        r = TG._call("getChatMemberCount", chat_id=chat_id, _timeout=10)
        if r.get("ok") and r.get("result") == 2:   # the bot + exactly one human
            m = TG._call("getChatMember", chat_id=chat_id, user_id=VLAD, _timeout=10)
            ok = bool(m.get("ok")) and (m.get("result", {}).get("status")
                                        in ("creator", "administrator", "member"))
    except Exception:
        ok = False                     # fail closed
    _CHAT_OK[chat_id] = (ok, time.time())
    return ok


def is_personal_path(path):
    real = os.path.realpath(path)
    return real == os.path.realpath(PERSONAL_DIR) or \
        real.startswith(os.path.realpath(PERSONAL_DIR) + os.sep)


# ---- retrieval --------------------------------------------------------------------
def _toks(s):
    return [t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if t]


def search(query, limit=8):
    """Notes whose original name / stored name / date / label / keywords matches
    every query token, newest first. Returns [(id, ts, orig_name, path)]."""
    qtoks = _toks(query)
    con = _db()
    rows = con.execute(
        "SELECT id, ts, orig_name, path, label, keywords FROM notes ORDER BY id DESC").fetchall()
    con.close()
    out = []
    for rid, ts, orig, path, label, keywords in rows:
        if not os.path.isfile(path):
            continue
        hay = (_toks(orig) + _toks(os.path.basename(path)) + _toks(ts[:10])
               + _toks(label) + _toks(keywords))
        if all(any(t == h or (len(t) >= 3 and t in h) for h in hay) for t in qtoks):
            out.append((rid, ts, orig, path))
        if len(out) >= limit:
            break
    return out


def recent(limit=10):
    con = _db()
    rows = con.execute("SELECT id, ts, orig_name, path FROM notes "
                       "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    con.close()
    return rows


# ---- delivery ---------------------------------------------------------------------
def send(chat_id, note_path, caption=""):
    """Send a personal note file — refuses anywhere the gate doesn't allow."""
    if not allowed_chat(chat_id):
        return None
    if not os.path.isfile(note_path) or os.path.getsize(note_path) > MAX_MB * 1024 * 1024:
        return None
    con = _db()
    row = con.execute("SELECT tg_file_id FROM notes WHERE path=?", (note_path,)).fetchone()
    con.close()
    params = {"chat_id": chat_id}
    if caption:
        params["caption"] = caption[:1000]
    r = {}
    if row and row[0]:
        r = TG._call("sendDocument", document=row[0], _timeout=30, **params)
    if not r.get("ok"):
        with open(note_path, "rb") as fh:
            r = TG._call("sendDocument", _files={"document": fh}, _timeout=120, **params)
    if not r.get("ok"):
        return None
    fid = (r.get("result", {}).get("document") or {}).get("file_id")
    if fid:
        con = _db()
        with con:
            con.execute("UPDATE notes SET tg_file_id=? WHERE path=?", (fid, note_path))
        con.close()
    return os.path.basename(note_path)


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "list":
        con = _db()
        for r in con.execute("SELECT id, ts, orig_name, label FROM notes "
                             "ORDER BY id DESC LIMIT 20"):
            print(f"#{r[0]}  {r[1]}  {r[2]}" + (f"  — {r[3]}" if r[3] else ""))
        con.close()
    elif cmd == "search":
        for r in search(" ".join(sys.argv[2:])):
            print(f"#{r[0]}  {r[1]}  {r[2]}  ->  {r[3]}")
    elif cmd == "check":
        cid = int(sys.argv[2])
        print(f"chat {cid} allowed: {allowed_chat(cid)}")
