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
import os, re, json, time, shutil, sqlite3, threading, subprocess

import tgconf as C
import tg_api as TG

OWNER = C.OWNER_ID                       # the owner's Telegram user id == his DM chat id
PERSONAL_DIR = os.path.join(C.WORKSPACE_ROOT, "personal")
NOTES_DIR = os.path.join(PERSONAL_DIR, "notes")
DB = os.path.join(PERSONAL_DIR, "notes.db")
# Meaning-vectors for the read-back search, inside the private tree with the
# notes they describe — never in a shared cache (2026-08-16).
VECTORS = os.path.join(PERSONAL_DIR, ".note_vectors.json")
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
        keywords TEXT,
        owner INTEGER)""")
    # migrate a pre-2026-07-11 db (no label/keywords columns) in place
    cols = {r[1] for r in con.execute("PRAGMA table_info(notes)")}
    for c in ("label", "keywords"):
        if c not in cols:
            con.execute(f"ALTER TABLE notes ADD COLUMN {c} TEXT")
    # 2026-08-15 (the owner: "The personal notes should be accessible only to the
    # User who created them"). Every row already here is his — the store has
    # only ever had one writer — so backfilling to OWNER is the true answer,
    # not a guess.
    if "owner" not in cols:
        con.execute("ALTER TABLE notes ADD COLUMN owner INTEGER")
        con.execute("UPDATE notes SET owner=? WHERE owner IS NULL", (OWNER,))
        con.commit()
    return con


NK = os.path.join(C.WORKSPACE_ROOT, "nk")


def _reindex():
    """Refresh the personal semantic index behind the answer (the owner 2026-08-15:
    "The personal knowledge base should be indexed too"). Incremental and ~0.1s,
    but it is still work the saver should not wait on, and a failed index must
    never turn a successful save into an error."""
    def run():
        try:
            subprocess.run([NK, "personal", "index"], capture_output=True, timeout=300)
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


# ---- capture --------------------------------------------------------------------
def add(path, orig_name=None, tg_file_id=None, label=None, keywords=None,
        owner=None):
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
                "INSERT INTO notes(ts, orig_name, path, kind, size, tg_file_id, label, "
                "keywords, owner) VALUES(?,?,?,?,?,?,?,?,?)",
                (time.strftime("%Y-%m-%d %H:%M:%S"), orig, dest,
                 os.path.splitext(orig)[1].lstrip(".").lower() or "file",
                 os.path.getsize(dest), tg_file_id, label, keywords,
                 int(owner) if owner else OWNER))
        con.close()
    _reindex()
    return cur.lastrowid, dest


def add_text(body, name=None, owner=None):
    """Store a spoken/typed note as a text file. Returns (note_id, path).

    the owner, 2026-08-15: "saving and retrieving passwords should be much faster".
    A one-line fact does not need a download, a vision call or a model turn —
    it needs a file. The slug skips bare numbers so the filename stays a
    description ("big-ipad-password") and not the secret itself."""
    body = (body or "").strip()
    if not body:
        return None, None
    words = [w for w in _toks(body) if not w.isdigit()][:5]
    slug = "-".join(words) or "note"
    tmp = os.path.join("/tmp", f"{slug[:60]}.txt")
    with open(tmp, "w") as fh:
        fh.write(body + "\n")
    return add(tmp, orig_name=name or f"{slug[:60]}.txt", owner=owner,
               label=body[:120], keywords=", ".join(w for w in _toks(body)
                                                    if not w.isdigit()))


# ---- the privacy gate -------------------------------------------------------------
def allowed_chat(chat_id, viewer=None):
    """True only for VIEWER's own DM or a live-verified bot+viewer-only group.

    2026-08-15, the owner: "The personal notes should be accessible only to the User
    who created them." The chat gate answers WHERE a note may be delivered; the
    owner column answers WHOSE notes those are. Both are needed — his DM is the
    right room for his notes and the wrong room for anyone else's."""
    viewer = int(viewer) if viewer else OWNER
    if chat_id == viewer:
        return True
    if chat_id > 0:                    # someone else's DM
        return False
    hit = _CHAT_OK.get((chat_id, viewer))
    if hit and time.time() - hit[1] < CHAT_TTL:
        return hit[0]
    ok = False
    try:
        r = TG._call("getChatMemberCount", chat_id=chat_id, _timeout=10)
        if r.get("ok") and r.get("result") == 2:   # the bot + exactly one human
            m = TG._call("getChatMember", chat_id=chat_id, user_id=viewer, _timeout=10)
            ok = bool(m.get("ok")) and (m.get("result", {}).get("status")
                                        in ("creator", "administrator", "member"))
    except Exception:
        ok = False                     # fail closed
    _CHAT_OK[(chat_id, viewer)] = (ok, time.time())
    return ok



def known_user(tg_id):
    """Is this a person the deployment knows? The registry is the answer — a
    guest's DM should not start a private store on this box."""
    try:
        import sys
        sys.path.insert(0, os.path.join(C.WORKSPACE_ROOT, "operations", "accounts"))
        import accounts
        return bool(accounts.get(int(tg_id)))
    except Exception:
        return False


def may_create(chat_id, sender):
    """True where a no-caption file becomes THAT person's personal note: their
    own DM, and only if the deployment knows them. the second owner dictating in her own
    chat files under the second owner (2026-08-15) — before this, the store had exactly
    one writer and everyone else's DM fell through to the ingest keyboard."""
    if not sender or int(chat_id) != int(sender):
        return False
    return int(sender) == OWNER or known_user(sender)


def is_personal_path(path):
    real = os.path.realpath(path)
    return real == os.path.realpath(PERSONAL_DIR) or \
        real.startswith(os.path.realpath(PERSONAL_DIR) + os.sep)


# ---- retrieval --------------------------------------------------------------------
def _toks(s):
    """Words, in any alphabet — see business_notes._toks (2026-08-16). The
    ASCII-only split made every Cyrillic question tokenise to nothing."""
    return [t for t in re.split(r"[^\w]+", (s or "").lower(), flags=re.U) if t]


def search(query, limit=8, viewer=None):
    """Notes whose original name / stored name / date / label / keywords matches
    every query token, newest first. Returns [(id, ts, orig_name, path)].
    Only the VIEWER's own notes — this store is per-creator (2026-08-15)."""
    qtoks = _toks(query)
    con = _db()
    rows = con.execute(
        "SELECT id, ts, orig_name, path, label, keywords FROM notes "
        "WHERE owner=? ORDER BY id DESC", (int(viewer) if viewer else OWNER,)).fetchall()
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


TEXT_KINDS = ("txt", "md", "text")
MAX_READBACK = 4000


def body_of(path):
    """The text of a text note, or None. Anything else (a photo, a PDF) has no
    body to read aloud and must go back as a file, through send()."""
    if os.path.splitext(path)[1].lstrip(".").lower() not in TEXT_KINDS:
        return None
    try:
        if os.path.getsize(path) > MAX_READBACK:
            return None
        return open(path, errors="replace").read().strip() or None
    except OSError:
        return None


SPOKEN_MAX = 300


def search_text(query, limit=4, spoken=False, viewer=None):
    """Text notes matching every query token in name, label, keywords OR body.

    search() deliberately never opens a note; this one does, because a spoken
    question ("what's my iPad password") names the CONTENT, and the content is
    the only place the answer lives. Returns [(id, ts, name, path, body)].

    spoken=True is the read-aloud contract and is deliberately stricter: only
    short dictated facts, and at least one query word must hit the note's
    DESCRIPTION. Without that second rule "what is my email address" matched a
    hotel receipt that merely contained both words."""
    qtoks = _toks(query)
    if not qtoks:
        return []
    con = _db()
    rows = con.execute("SELECT id, ts, orig_name, path, label, keywords "
                       "FROM notes WHERE owner=? ORDER BY id DESC",
                       (int(viewer) if viewer else OWNER,)).fetchall()
    con.close()
    out, candidates = [], []
    for rid, ts, orig, path, label, keywords in rows:
        if not os.path.isfile(path):
            continue
        body = body_of(path)
        if body is None:
            continue
        if spoken and len(body) > SPOKEN_MAX:
            continue
        named = (_toks(orig) + _toks(os.path.basename(path))
                 + _toks(label) + _toks(keywords))
        hay = named + _toks(ts[:10]) + _toks(body)

        def hit(t, where):
            return any(t == h or (len(t) >= 3 and t in h) for h in where)

        candidates.append((rid, ts, orig, path, body, named, hay))
        if not all(hit(t, hay) for t in qtoks):
            continue
        if spoken and not any(hit(t, named) for t in qtoks):
            continue
        out.append((rid, ts, orig, path, body))
        if len(out) >= limit:
            break
    if out:
        return out
    # Nothing matched word for word. Rank by MEANING instead of giving up —
    # the same fix as business_notes.search (2026-08-16): he asks "membership
    # no", the note says "number", and one unmatched word should not be the
    # difference between an answer and "I don't have it".
    # The read-aloud contract still holds here: a note only answers a spoken
    # question if the question names what the note IS, not merely something its
    # body happens to contain. Meaning replaces "every word matched" — it does
    # not replace the description rule.
    pool = candidates
    if spoken:
        pool = [c for c in candidates if any(hit(t, c[5]) for t in qtoks)]
    try:
        import note_search
        texts = [f"{c[2]} {' '.join(c[5])} {c[4]}" for c in pool]
        ranked = note_search.rank(query, texts, note_search.VectorCache(VECTORS))
        return [pool[i][:5] for _, i in ranked[:limit]]
    except Exception:
        return []


def recent(limit=10, viewer=None):
    con = _db()
    rows = con.execute("SELECT id, ts, orig_name, path FROM notes WHERE owner=? "
                       "ORDER BY id DESC LIMIT ?",
                       (int(viewer) if viewer else OWNER, limit)).fetchall()
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
