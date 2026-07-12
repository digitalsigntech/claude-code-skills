"""Doc reflex — deterministic ~1s document delivery for the Telegram gateway.

"fetch my labelexpo pass" sends the registered PDF straight via sendDocument —
no LLM turn at all. Unlike the photo reflex (which searches the CLIP index),
this only serves a small CURATED registry (doc_registry.json): each entry names
one file plus the keyword groups that must ALL appear in the message. Anything
that doesn't match every group falls through to the normal Claude turn, so a
miss costs microseconds and hijacks nothing.

Registry format (telegram/doc_registry.json), one entry per document:
  {"name": "labelexpo-pass",
   "path": "/abs/path/to/file.pdf",
   "caption": "shown under the document in Telegram",
   "keywords": [["labelexpo", "label expo"], ["pass", "ticket", "badge"]]}
The message matches when at least one alternative from EVERY keyword group
appears (case-insensitive, word-boundary). Add an entry, no restart needed —
the registry is re-read on every message.

file_id cache: state/doc_file_ids.json keyed by path — after the first upload,
re-sends reuse Telegram's cached copy (no re-upload).
"""
import os, re, json, time, threading

import tgconf as C
import tg_api as TG

REGISTRY_FILE = os.path.join(C.HERE, "doc_registry.json")
CACHE_FILE = os.path.join(C.STATE_DIR, "doc_file_ids.json")

_LOCK = threading.Lock()

# Messages that are really questions ABOUT a document ("how much was the
# labelexpo pass?") should reach Claude, not just get the file dumped on them.
QUESTION_WORD = re.compile(r"\b(how|why|when|who|which|what|where|should|could|would|"
                           r"do|does|did|is|are|was|were|cost|costs)\b", re.I)


def _registry():
    try:
        return json.load(open(REGISTRY_FILE))
    except Exception:
        return []


def detect(text):
    """Return the matching registry entry if this message asks for a registered
    document, else None."""
    t = (text or "").strip()
    if not t or "\n" in t or len(t) > 120 or QUESTION_WORD.search(t):
        return None
    for entry in _registry():
        groups = entry.get("keywords") or []
        if groups and all(
            any(re.search(rf"\b{re.escape(alt)}\b", t, re.I) for alt in group)
            for group in groups
        ):
            return entry
    return None


# ---- file_id cache -----------------------------------------------------------
def _cache():
    try:
        return json.load(open(CACHE_FILE))
    except Exception:
        return {}


def _remember(path, file_id):
    if not file_id:
        return
    with _LOCK:
        c = _cache()
        c[path] = file_id
        tmp = CACHE_FILE + ".tmp"
        json.dump(c, open(tmp, "w"), indent=1)
        os.replace(tmp, CACHE_FILE)


# ---- the reflex ---------------------------------------------------------------
def try_handle(chat_id, text):
    """Returns a short summary string if the message was fully handled (document
    sent), else None -> the gateway falls through to the normal Claude turn."""
    entry = detect(text)
    if not entry:
        return None
    path, caption = entry.get("path") or "", entry.get("caption") or ""
    if not os.path.exists(path):
        return None                      # stale registry entry -> let Claude handle it
    t0 = time.time()
    params = {"chat_id": chat_id}
    if caption:
        params["caption"] = caption[:1000]
    fid = _cache().get(path)
    if fid:                              # cached -> no re-upload
        r = TG._call("sendDocument", document=fid, _timeout=30, **params)
        if not r.get("ok"):              # cache went stale -> fall through to upload
            fid = None
    if not fid:
        with open(path, "rb") as fh:
            r = TG._call("sendDocument", _files={"document": fh}, _timeout=120, **params)
        if not r.get("ok"):
            return None                  # send failed -> let Claude try
        _remember(path, (r.get("result", {}).get("document") or {}).get("file_id"))
    ms = int((time.time() - t0) * 1000)
    return f"[doc reflex: sent {os.path.basename(path)} ({entry.get('name')}) in {ms}ms]"


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2:
        e = detect(" ".join(sys.argv[1:]))
        print("detect ->", e.get("name") if e else None)
