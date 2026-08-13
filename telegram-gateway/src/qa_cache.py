"""Semantic Q&A answer cache (2026-07-13, the owner: "cache our Q&A for faster retrieval",
voice + text).

Every question answered by a full Claude turn is stored with a LOCAL embedding
(nomic-embed server, per the embeddings-stay-local policy). A repeat question —
even reworded — is served from the cache in ~0.1s instead of a 10-30s LLM turn.

Safety guards, because a wrong instant answer is worse than a slow right one:
  - cosine >= THRESHOLD (0.90) against the stored QUESTION (not the answer);
  - digit-bearing tokens (product codes: phd-12, pg-17, qs256, quantities) must
    match EXACTLY between the two questions — embeddings alone rank "price of
    PHD-12" vs "price of PHD-18" nearly identical, this guard makes that a miss;
  - entries expire after TTL_DAYS (prices/specs drift);
  - answers that look like errors are never stored.

Used by: voice/realtime/server.py (ask_claude bridge — questions arrive restated
self-contained, cache always applies), gateway.py handle_text and handle_voice
(conversational, so both store and serve only question-shaped standalone
messages — see cacheable()).

CLI: python3 qa_cache.py stats | list | clear [--all | --like SUBSTR]
"""
import os, re, json, sqlite3, threading, time, urllib.request

import numpy as np

DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(DIR, "state", "qa_cache.db")
EMB_URL = os.environ.get("TG_EMBED_URL", "http://127.0.0.1:18183/v1/embeddings")
QUERY_PREFIX = "search_query: "     # nomic task prefix; symmetric Q-vs-Q comparison
THRESHOLD = 0.90
DEDUP = 0.97                        # near-identical stored question -> update in place
TTL_DAYS = 30
MAX_Q = 500                         # embed cap; questions are short

_lock = threading.Lock()
_cx_local = threading.local()


def _cx():
    c = getattr(_cx_local, "c", None)
    if c is None:
        os.makedirs(os.path.dirname(DB), exist_ok=True)
        c = _cx_local.c = sqlite3.connect(DB, timeout=10)
        c.execute("""CREATE TABLE IF NOT EXISTS qa(
            id INTEGER PRIMARY KEY, question TEXT, answer TEXT, vec BLOB,
            source TEXT, created REAL, hits INTEGER DEFAULT 0, last_hit REAL)""")
        c.commit()
    return c


def _embed(text):
    body = json.dumps({"input": [QUERY_PREFIX + " ".join(text.split())[:MAX_Q]]}).encode()
    req = urllib.request.Request(EMB_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        v = np.asarray(json.load(r)["data"][0]["embedding"], dtype=np.float32)
    n = np.linalg.norm(v)
    return v / (n or 1.0)


# Tokens carrying a digit are load-bearing identifiers (model numbers, quantities,
# years) — require exact set equality, don't trust embedding proximity on them.
_code_re = re.compile(r"\b(?=\w*\d)[\w.-]{2,}\b")


def _codes(q):
    return {m.lower().strip(".-") for m in _code_re.findall(q)}


_qword = re.compile(
    r"^(what|when|where|which|who|whose|why|how|is|are|does|do|can|could|will|"
    r"what's|how's|сколько|что|как|какой|какая|какие|когда|где|кто|почему|есть)\b", re.I)


def cacheable(text):
    """True for standalone question-shaped messages. Conversational fragments
    ('yes, do that', 'and the other one?') must NOT be cached or served — their
    meaning depends on chat context the cache can't see."""
    t = text.strip()
    if len(t) < 15 or len(t) > 400 or t.startswith("/"):
        return False
    if "?" not in t and not _qword.match(t):
        return False
    # Context-dependent openers/pronoun-only asks: don't risk it.
    if re.match(r"^(and|also|what about|а |и |же |it|its|they|them|this|that|these|those)\b",
                t, re.I):
        return False
    return t.count(" ") >= 2


def lookup(question, threshold=THRESHOLD):
    """Cached answer for a semantically-equal question, or None. Never raises."""
    try:
        cutoff = time.time() - TTL_DAYS * 86400
        with _lock:
            rows = _cx().execute(
                "SELECT id, question, answer, vec FROM qa WHERE created > ?",
                (cutoff,)).fetchall()
        if not rows:
            return None
        v = _embed(question)
        mat = np.frombuffer(b"".join(r[3] for r in rows), dtype=np.float32
                            ).reshape(len(rows), -1)
        sims = mat @ v
        i = int(np.argmax(sims))
        if sims[i] < threshold or _codes(question) != _codes(rows[i][1]):
            return None
        with _lock:
            _cx().execute("UPDATE qa SET hits = hits + 1, last_hit = ? WHERE id = ?",
                          (time.time(), rows[i][0]))
            _cx().commit()
        return rows[i][2]
    except Exception:
        return None


def store(question, answer, source="text"):
    """Remember a served answer. Best-effort; never raises."""
    try:
        q, a = question.strip(), (answer or "").strip()
        if len(q) < 15 or not a or a.startswith(("⚠️", "🔒")) or len(a) > 4000:
            return
        v = _embed(q)
        with _lock:
            c = _cx()
            cutoff = time.time() - TTL_DAYS * 86400
            rows = c.execute("SELECT id, question, vec FROM qa WHERE created > ?",
                             (cutoff,)).fetchall()
            for rid, rq, rv in rows:
                if (np.frombuffer(rv, dtype=np.float32) @ v) >= DEDUP and _codes(q) == _codes(rq):
                    c.execute("UPDATE qa SET answer = ?, created = ?, source = ? WHERE id = ?",
                              (a, time.time(), source, rid))
                    c.commit()
                    return
            c.execute("INSERT INTO qa(question, answer, vec, source, created) VALUES(?,?,?,?,?)",
                      (q, a, v.tobytes(), source, time.time()))
            c.commit()
    except Exception:
        pass


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["stats", "list", "clear"])
    p.add_argument("--all", action="store_true")
    p.add_argument("--like", default=None)
    a = p.parse_args()
    c = _cx()
    if a.cmd == "stats":
        n, hits = c.execute("SELECT COUNT(*), COALESCE(SUM(hits),0) FROM qa").fetchone()
        print(f"{n} cached Q&A, {hits} total hits")
    elif a.cmd == "list":
        for r in c.execute("SELECT id, hits, source, question FROM qa ORDER BY created DESC"):
            print(f"#{r[0]} hits={r[1]} [{r[2]}] {r[3][:90]}")
    elif a.cmd == "clear":
        if a.like:
            c.execute("DELETE FROM qa WHERE question LIKE ?", (f"%{a.like}%",))
        elif a.all:
            c.execute("DELETE FROM qa")
        else:
            p.error("clear needs --all or --like SUBSTR")
        c.commit()
        print("cleared")
