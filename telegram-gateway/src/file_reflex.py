"""File reflex — deterministic fast delivery of ANY file for the Telegram gateway.

the owner (2026-07-10): "show me / fetch / get / give me ... PDF, images, docs and other
types of files — must be fast, must be the closely matching item, not something
random." One verb surface, three sources, strict matching, no LLM turn:

 1. curated registry (doc_registry.json) — handled UPSTREAM by doc_reflex; this
    module is the net under it for everything not curated (and for phrasings the
    doc reflex rejects, e.g. "could you send ...").
 2. KB images — the warm CLIP server (127.0.0.1:8477), keyword-confident hits only,
    and EVERY distinctive query token must appear in the hit's tags/annotation/name
    (the old reflex sent a printhead photo for "label expo pass" because one token,
    'label', matched a barcode-label tag).
 3. workspace files — a cached walk of the workspace tree (~4k files, refreshed every
    2 min); a file qualifies only when every distinctive query token appears in its
    name or parent folders. Best precision wins; ties go to the newest file.
    DM chats + the Always-Nemotron private group ONLY (the owner 2026-07-27: that group
    is a private channel whose agent may send workspace files anyway, so the reflex
    may too) — other group chats never get arbitrary workspace files, just registry
    docs + KB images. Secrets (token/credential/key/.env), databases and the
    mail/telegram/chatlog trees are never indexed.

If a query plausibly matches BOTH a document and KB images (no type hint either
way), or several unrelated files match equally, it falls through to the normal
Claude turn — a miss costs ~10ms and hijacks nothing. Toggle: TG_FILE_REFLEX=0.
"""
import os, re, time, threading

import tgconf as C
import tg_api as TG
import doc_reflex
import photo_reflex
import personal_notes

MAX_MB = 49                    # Telegram bot upload cap is 50 MB
WALK_TTL = 120                 # seconds the workspace file list stays cached
GENERIC_CAP = 5                # >= this many equally-scored distinct names -> too vague

VERB = r"(?:show|send|get|give|fetch|find|grab|share|attach|forward|pull\s+up|bring\s+up)"
LEAD = r"(?:please\s+|pls\s+)?(?:can\s+you\s+|could\s+you\s+|would\s+you\s+)?"
ASK = re.compile(
    rf"^{LEAD}{VERB}\s+(?:me\s+|us\s+)?(?:my\s+|the\s+|a\s+|an\s+|some\s+|our\s+"
    rf"|that\s+|this\s+)?(?P<q>.+?)(?:\s+(?:please|pls))?\s*[.!?]*$", re.I)
# The object must be a noun phrase, not a question ("get me whatever the friend account said" and
# "show me how the pass looks" belong to Claude).
QUESTION_WORD = re.compile(r"\b(how|why|when|who|whom|which|what|where|should|could|"
                           r"would|do|does|did|is|are|was|were)\b", re.I)

STOP = {"me", "my", "the", "a", "an", "some", "our", "your", "us", "please", "pls",
        "of", "for", "to", "in", "on", "from", "and", "or", "that", "this",
        "latest", "current", "newest", "recent", "copy", "version", "one"}
IMG_HINT = {"photo", "photos", "pic", "pics", "picture", "pictures", "image",
            "images", "shot", "shots"}
NOTE_HINT = {"note", "notes"}          # -> the owner's personal notes ONLY (gated)
FILE_HINT = {"file", "files", "doc", "docs", "document", "documents", "scan",
             "scans", "spreadsheet"}
EXT_HINT = {"pdf": ".pdf", "csv": ".csv", "xlsx": ".xlsx"}

# Never indexed: VCS/cache noise, raw mail stores, gateway internals (bot_token!),
# chat archive, "media" (KB images are the CLIP index's domain, source 2) and
# "from-pdfs" (email-KB extraction artifacts — hash-prefixed .md mirrors of every
# emailed PDF that would out-tie the real document).
EXCLUDE_DIRS = {".git", "__pycache__", "node_modules", "state", "logs", "log",
                "store", "cache", "tmp", "inbox", "media", "from-pdfs"
                } | set(C.FILE_REFLEX_EXCLUDE_DIRS)   # the private-note dir is in there,
# and it is served ONLY via the personal_notes gate
EXCLUDE_DIR_PREFIXES = ("venv", ".")
SENSITIVE = re.compile(r"token|secret|credential|password|api[_-]?key|id_rsa"
                       r"|\.pem$|\.key$|\.env$", re.I)
EXCLUDE_EXTS = {".db", ".sqlite", ".sqlite3", ".pyc", ".lock", ".tmp", ".part",
                ".faiss", ".npy", ".jsonl", ".log"}

_LOCK = threading.Lock()
_WALK = {"ts": 0.0, "files": []}   # [(path, name_tokens, dir_tokens, mtime)]


def _toks(s):
    return [t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if t]


def detect(text):
    """Return the object phrase if this is a fetch-verb request, else None."""
    t = (text or "").strip()
    if not t or "\n" in t or len(t) > 120:
        return None
    m = ASK.match(t)
    if not m:
        return None
    q = m.group("q").strip()
    if not q or len(q.split()) > 8 or QUESTION_WORD.search(q):
        return None
    return q


# ---- source 3: workspace file index -------------------------------------------
def _index():
    with _LOCK:
        if time.time() - _WALK["ts"] < WALK_TTL:
            return _WALK["files"]
    files = []
    for root, dirs, names in os.walk(C.WORKSPACE_ROOT):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS
                   and not d.startswith(EXCLUDE_DIR_PREFIXES)]
        rel = os.path.relpath(root, C.WORKSPACE_ROOT)
        dtoks = _toks(" ".join(rel.split(os.sep)[-2:])) if rel != "." else []
        for n in names:
            if n.startswith(".") or SENSITIVE.search(n):
                continue
            if os.path.splitext(n)[1].lower() in EXCLUDE_EXTS:
                continue
            p = os.path.join(root, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if not st.st_size or st.st_size > MAX_MB * 1024 * 1024:
                continue
            files.append((p, _toks(n), dtoks, st.st_mtime))
    with _LOCK:
        _WALK.update(ts=time.time(), files=files)
    return files


def _hit(tok, toks):
    """Query token matches a file token exactly or as a >=3-char substring either
    way ('expo' in 'labelexpo'; file token 'label' in query 'labelexpo')."""
    base = tok.rstrip("s") if len(tok) > 3 else tok
    for f in toks:
        if tok == f or base == f:
            return True
        if len(base) >= 3 and base in f:
            return True
        if len(f) >= 4 and f in tok:
            return True
    return False


def _files(qtoks, ext=None):
    """Full-coverage candidates, best first. Score: a token matched in the NAME is
    worth 2, matched only in parent dirs 1; ties -> tighter name, then newest."""
    out = []
    for p, ntoks, dtoks, mtime in _index():
        if ext and not p.lower().endswith(ext):
            continue
        n_match = sum(1 for t in qtoks if _hit(t, ntoks))
        if n_match < len(qtoks) and not all(
                _hit(t, ntoks) or _hit(t, dtoks) for t in qtoks):
            continue
        score = n_match * 2 + (len(qtoks) - n_match)
        out.append((score, n_match / max(1, len(ntoks)), mtime, p))
    out.sort(key=lambda x: (-x[0], -x[1], -x[2]))
    return out


# ---- source 2: KB images (CLIP) ------------------------------------------------
def _images(qtoks):
    q = " ".join(qtoks)
    try:
        d = photo_reflex._find(q)
    except Exception:
        return []                        # CLIP server down -> other sources / Claude
    return [h for h in d.get("results", [])
            if h.get("match") == "keyword" and photo_reflex._covers(q, h)
            ][:photo_reflex.MAX_SEND]


# ---- decision -------------------------------------------------------------------
def resolve(chat_id, text):
    """('doc', path) | ('imgs', hits, query) | None (-> normal Claude turn)."""
    q = detect(text)
    if not q:
        return None
    raw = _toks(q)
    img_hint = any(t in IMG_HINT for t in raw)
    file_hint = any(t in FILE_HINT or t in EXT_HINT for t in raw)
    note_hint = any(t in NOTE_HINT for t in raw)
    ext = next((EXT_HINT[t] for t in raw if t in EXT_HINT), None)
    qtoks = [t for t in raw
             if t not in STOP and t not in IMG_HINT and t not in FILE_HINT
             and t not in EXT_HINT and t not in NOTE_HINT]
    if not qtoks or not any(len(t) >= 3 or t.isdigit() for t in qtoks):
        return None
    # Personal notes (the owner 2026-07-10): searched only where the privacy gate allows;
    # "note(s)" in the request scopes the search to them exclusively.
    nhits = (personal_notes.search(" ".join(qtoks))
             if personal_notes.allowed_chat(chat_id) else [])
    if note_hint:
        if not nhits or len(nhits) >= GENERIC_CAP:
            return None                  # none or too vague -> Claude decides
        return ("note", nhits[0][3])     # newest match
    imgs = [] if file_hint else _images(qtoks)
    ws_ok = chat_id > 0 or chat_id in C.ALWAYS_NEMOTRON_CHATS
    files = [] if (img_hint or not ws_ok) else _files(qtoks, ext)
    if sum(1 for s in (nhits, imgs, files) if s) > 1:
        return None                      # plausible several ways -> Claude decides
    if nhits:
        if len(nhits) >= GENERIC_CAP:
            return None
        return ("note", nhits[0][3])
    if imgs:
        return ("imgs", imgs, q)
    if files:
        top = files[0]
        peers = {os.path.basename(f[3]) for f in files
                 if f[0] == top[0] and abs(f[1] - top[1]) < 1e-6}
        if len(peers) >= GENERIC_CAP:    # "get me the invoice" -> too vague
            return None
        return ("doc", top[3])
    return None


# ---- sending --------------------------------------------------------------------
def _send_doc(chat_id, path, t0):
    rel = os.path.relpath(path, C.WORKSPACE_ROOT)
    params = {"chat_id": chat_id, "caption": rel[:1000]}
    fid = doc_reflex._cache().get(path)
    if fid:                              # cached -> no re-upload
        r = TG._call("sendDocument", document=fid, _timeout=30, **params)
        if not r.get("ok"):
            fid = None
    if not fid:
        with open(path, "rb") as fh:
            r = TG._call("sendDocument", _files={"document": fh}, _timeout=120, **params)
        if not r.get("ok"):
            return None                  # send failed -> let Claude try
        doc_reflex._remember(path, (r.get("result", {}).get("document") or {}).get("file_id"))
    ms = int((time.time() - t0) * 1000)
    return f"[file reflex: sent {rel} in {ms}ms]"


def _send_imgs(chat_id, hits, q, t0):
    ms = int((time.time() - t0) * 1000)
    footer = f"⚡ {q} · {len(hits)} match{'es' if len(hits) > 1 else ''} · {ms}ms"
    if not photo_reflex._send(chat_id, hits, footer):
        return None
    total = int((time.time() - t0) * 1000)
    return (f"[file reflex: sent {len(hits)} media item(s) for '{q}' in {total}ms: "
            + ", ".join(os.path.basename(h["path"]) for h in hits) + "]")


def try_handle(chat_id, text):
    """Returns a short summary string if the message was fully handled (file or
    photos sent), else None -> the gateway falls through to the normal Claude turn."""
    t0 = time.time()
    r = resolve(chat_id, text)
    if not r:
        return None
    if r[0] == "note":
        sent = personal_notes.send(chat_id, r[1])   # re-checks the gate itself
        if not sent:
            return None
        return f"[file reflex: sent personal note {sent} in {int((time.time()-t0)*1000)}ms]"
    if r[0] == "doc":
        return _send_doc(chat_id, r[1], t0)
    return _send_imgs(chat_id, r[1], r[2], t0)


if __name__ == "__main__":
    import sys
    r = resolve(C.OWNER_ID, " ".join(sys.argv[1:]))
    if not r:
        print("-> fall through to Claude")
    elif r[0] == "doc":
        print("-> sendDocument:", os.path.relpath(r[1], C.WORKSPACE_ROOT))
    else:
        print(f"-> send {len(r[1])} photo(s):",
              ", ".join(os.path.basename(h["path"]) for h in r[1]))
