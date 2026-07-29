"""Photo reflex — deterministic sub-second image retrieval for the Telegram gateway.

"show me the qs256 heads" goes straight to the warm CLIP server (127.0.0.1:8477)
and the photos are sent with cached Telegram file_ids (no re-upload) — no LLM turn
at all. Fires ONLY on a confident keyword match (a distinctive query token appearing
verbatim in an image's curated tags/annotation); anything fuzzy falls through to the
normal Claude turn, so a miss costs ~10ms and hijacks nothing.

file_id cache: state/media_file_ids.json, keyed by image basename. Populated three
ways: harvested when the user sends a photo in (its inbound file_id is reusable for
sending), harvested from every upload we make, and prewarmable via
  ./venv-less python3 photo_reflex.py --prewarm <chat_id>   (uploads + deletes)
"""
import os, re, json, time, threading, urllib.parse, urllib.request

import tgconf as C
import tg_api as TG

FIND_URL = "http://127.0.0.1:8477/find"
CACHE_FILE = os.path.join(C.STATE_DIR, "media_file_ids.json")
MAX_SEND = 4          # never spam more than this many photos per request
FIND_TIMEOUT = 3      # server is ~10ms warm; anything slow means it's down -> fall through

_LOCK = threading.Lock()
_CACHE = None

PHOTO_WORD = r"photos?|pics?|pictures?|images?|shots?"
# Words that mean the user wants a DOCUMENT, not KB photos — never reflex on these
# (e.g. "send me the qs256 datasheet" must reach Claude, not dump head photos).
DOC_WORD = re.compile(r"\b(datasheet|data\s+sheet|pdf|report|price|prices|pricing|list|"
                      r"spec|specs|manual|invoice|quote|email|draft|file|files|doc|docs|"
                      r"document|documents|schematic|drawing|order)\b", re.I)
QUESTION_WORD = re.compile(r"\b(how|why|when|where|who|which|should|can|could|would|"
                           r"do|does|did|is|are|was|were)\b", re.I)

PATTERNS = [
    # explicit command: /pic qs256   /photos voxeljet
    re.compile(r"^/(?:pic|pics|photo|photos|img)(?:@\w+)?\s+(?P<q>.+)$", re.I | re.S),
    # "photos of the qs256" / "picture of a voxeljet head"
    re.compile(rf"^(?:(?:show|send|get)\s+(?:me\s+)?)?(?:the\s+|a\s+|some\s+)?"
               rf"(?:{PHOTO_WORD})\s+of\s+(?:the\s+|a\s+)?(?P<q>.+?)[.!?]*$", re.I),
    # "send me the voxeljet head pics" — generic verb, but ONLY with an explicit
    # photo word: plain "show me X" belongs to the file reflex, which weighs docs
    # vs images (2026-07-10 'label expo pass' misfire: this pattern hijacked a PDF
    # request and CLIP matched a barcode-'label' tag).
    re.compile(rf"^(?:show|send|get|give|fetch|find|grab|pull\s+up|bring\s+up)\s+"
               rf"(?:me\s+|us\s+)?(?:the\s+|a\s+|some\s+)?"
               rf"(?P<q>.+?\s(?:{PHOTO_WORD}))[.!?]*$", re.I),
    # "what does a qs256 head look like"
    re.compile(r"^what\s+does\s+(?:the\s+|a\s+)?(?P<q>.+?)\s+look\s+like\??$", re.I),
]


def detect(text):
    """Return a search query if this message is an image request, else None."""
    t = (text or "").strip()
    if not t or "\n" in t or len(t) > 120:
        return None
    for i, pat in enumerate(PATTERNS):
        m = pat.match(t)
        if not m:
            continue
        q = m.group("q").strip()
        # The generic-verb form (pattern 2) is broad — keep it tight: short noun
        # phrases only, no question structure, no document words.
        if i in (2, 3) and (len(q.split()) > 7 or QUESTION_WORD.search(q)):
            return None
        if DOC_WORD.search(q) and not re.search(PHOTO_WORD, t, re.I):
            return None
        # strip a trailing photo-word: "qs256 head photos" -> "qs256 head"
        q = re.sub(rf"\s+(?:{PHOTO_WORD})$", "", q, flags=re.I).strip()
        return q or None
    return None


def _find(q, k=8):
    url = FIND_URL + "?" + urllib.parse.urlencode({"q": q, "k": k})
    with urllib.request.urlopen(url, timeout=FIND_TIMEOUT) as r:
        return json.load(r)


_STOP = {"the", "a", "an", "my", "our", "some", "me", "us", "please", "of", "for"}


def _covers(q, h):
    """EVERY distinctive query token must appear in the hit's own tags/annotation/
    filename — one shared token is not a match ('my label expo pass' once sent a
    printhead photo because 'label' matched a barcode-label tag; 'expo' and 'pass'
    matched nothing)."""
    hay = " ".join([h.get("tags") or "", h.get("annotation") or "",
                    os.path.basename(h.get("path") or "")]).lower()
    toks = [t for t in re.split(r"[^a-z0-9]+", q.lower()) if t and t not in _STOP]
    return bool(toks) and all(
        (t.rstrip("s") if len(t) > 3 else t) in hay for t in toks)


# ---- file_id cache -----------------------------------------------------------
def _cache():
    global _CACHE
    if _CACHE is None:
        try:
            _CACHE = json.load(open(CACHE_FILE))
        except Exception:
            _CACHE = {}
    return _CACHE


def remember(path, file_id):
    """Record a reusable Telegram file_id for an image (keyed by basename)."""
    if not file_id:
        return
    ext = os.path.splitext(path)[1].lower()
    if ext not in C.IMG_EXTS + C.VID_EXTS:
        return
    with _LOCK:
        c = _cache()
        c[os.path.basename(path)] = file_id
        tmp = CACHE_FILE + ".tmp"
        json.dump(c, open(tmp, "w"), indent=1)
        os.replace(tmp, CACHE_FILE)


def _file_id(path):
    with _LOCK:
        return _cache().get(os.path.basename(path))


def _harvest(resp_msgs, paths_in_order):
    """Store file_ids Telegram returned for the photos we just uploaded."""
    try:
        for m, p in zip(resp_msgs, paths_in_order):
            if not p:
                continue
            if m.get("photo"):
                remember(p, m["photo"][-1]["file_id"])
            elif m.get("video"):
                remember(p, m["video"]["file_id"])
    except Exception:
        pass


# ---- sending -----------------------------------------------------------------
def _send(chat_id, hits, footer):
    """Send hits as one media group (or single photo). Cached file_id when we have
    one, upload otherwise; uploads' new file_ids are harvested for next time."""
    media, files, uploaded = [], {}, []
    for i, h in enumerate(hits):
        cap = (h.get("annotation") or os.path.basename(h["path"]))[:200]
        if i == 0:
            cap = cap[:200 - len(footer) - 1] + "\n" + footer
        ext = os.path.splitext(h["path"])[1].lower()
        typ = "video" if ext in C.VID_EXTS else "photo"
        fid = _file_id(h["path"])
        if fid:
            media.append({"type": typ, "media": fid, "caption": cap})
            uploaded.append(None)
        else:
            key = f"p{i}"
            files[key] = open(h["path"], "rb")
            media.append({"type": typ, "media": f"attach://{key}", "caption": cap})
            uploaded.append(h["path"])
    try:
        if len(media) == 1:
            m0 = media[0]
            method, field = (("sendVideo", "video") if m0["type"] == "video"
                             else ("sendPhoto", "photo"))
            if files:
                r = TG._call(method, _files={field: files["p0"]},
                             chat_id=chat_id, caption=m0["caption"])
            else:
                r = TG._call(method, chat_id=chat_id, caption=m0["caption"],
                             **{field: m0["media"]})
            msgs = [r.get("result", {})] if r.get("ok") else []
        else:
            if files:
                r = TG._call("sendMediaGroup", _files=files, chat_id=chat_id,
                             media=json.dumps(media))
            else:
                r = TG._call("sendMediaGroup", chat_id=chat_id, media=media)
            msgs = r.get("result", []) if r.get("ok") else []
    finally:
        for f in files.values():
            f.close()
    if not r.get("ok"):
        return None
    _harvest(msgs, uploaded)
    return msgs


def try_handle(chat_id, text):
    """The reflex. Returns a short summary string if the message was fully handled
    (photos sent), else None -> the gateway falls through to the normal Claude turn."""
    q = detect(text)
    if not q:
        return None
    t0 = time.time()
    try:
        d = _find(q)
    except Exception:
        return None                      # CLIP server down -> Claude handles it
    # Confident = exact keyword match on curated tags/annotation AND full coverage
    # of the query (every distinctive token present). Embedding-only scores are NOT
    # trusted here (CLIP ranked an unrelated cable above the QS256 heads); a fuzzy
    # query falls through to Claude, which can search deliberately.
    hits = [h for h in d.get("results", [])
            if h.get("match") == "keyword" and _covers(q, h)][:MAX_SEND]
    if not hits:
        return None
    ms = int((time.time() - t0) * 1000)
    footer = f"⚡ {q} · {len(hits)} match{'es' if len(hits) > 1 else ''} · {ms}ms"
    sent = _send(chat_id, hits, footer)
    if not sent:
        return None                      # send failed -> let Claude try
    total = int((time.time() - t0) * 1000)
    return (f"[photo reflex: sent {len(hits)} photo(s) for '{q}' in {total}ms: "
            + ", ".join(os.path.basename(h["path"]) for h in hits) + "]")


# ---- prewarm: upload every KB image once (silently), cache ids, delete -------
def prewarm(chat_id):
    meta = json.load(open(os.path.join(C.DST_ROOT, "local-ai", "store", "media_meta.json")))
    todo = [m["path"] for m in meta
            if os.path.exists(m["path"]) and not _file_id(m["path"])]
    print(f"{len(todo)} image(s) need a file_id")
    for p in todo:
        with open(p, "rb") as f:
            r = TG._call("sendPhoto", _files={"photo": f}, chat_id=chat_id,
                         disable_notification=True)
        if not r.get("ok"):
            print("FAIL", os.path.basename(p), str(r.get("error"))[:120]); continue
        sizes = r["result"].get("photo") or []
        if sizes:
            remember(p, sizes[-1]["file_id"])
        TG.delete_message(chat_id, r["result"]["message_id"])
        print("cached", os.path.basename(p))


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "--prewarm":
        prewarm(int(sys.argv[2]))
    elif len(sys.argv) >= 2:
        print("detect ->", detect(" ".join(sys.argv[1:])))
