#!/usr/bin/env python3
"""Semantic recall for the chat archive — search by meaning, via an OpenAI-compatible
embedding server (default: a local nomic-embed server, 768-dim). Embeddings aren't
generative, so a local GPU/CPU embedding model is a good fit here; only the generative
reasoning stays on your chat LLM. The backend is swappable — point EMBED_URL/EMBED_DIM
at any OpenAI-compatible /v1/embeddings endpoint (see README).

Each message is embedded into a small on-disk vector store next to chat.db. Search
embeds the query and ranks by cosine similarity (vectors are L2-normalized on store, so
cosine == dot product; ranking is one numpy matmul — instant at this scale).

Indexing is incremental and idempotent — only new message ids are embedded — so it's
safe on a cron next to classify.py.

  embed.py index                         # embed any not-yet-embedded messages
  embed.py search "on-prem deployment"   # rank by meaning
  embed.py search "invoice" --project finance --since 2026-06-01 --limit 10
"""
import os, sys, json, argparse, urllib.request
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import chatdb

EMB_URL = os.environ.get("EMBED_URL", "http://127.0.0.1:8080/v1/embeddings")
STORE = os.path.join(HERE, "store")
VECS = os.path.join(STORE, "vectors.npy")
IDS = os.path.join(STORE, "ids.npy")
DIM = int(os.environ.get("EMBED_DIM", "768"))
BATCH = 64
MAX_CHARS = 2000            # keep well within the embed model's context
# nomic-embed asks for task prefixes; they measurably improve retrieval. Blank them
# (EMBED_DOC_PREFIX="" / EMBED_QUERY_PREFIX="") for models that don't use prefixes.
DOC_PREFIX = os.environ.get("EMBED_DOC_PREFIX", "search_document: ")
QUERY_PREFIX = os.environ.get("EMBED_QUERY_PREFIX", "search_query: ")


def _embed(texts, prefix):
    """Return an (n, DIM) float32 array of L2-normalized embeddings."""
    out = []
    for i in range(0, len(texts), BATCH):
        chunk = [prefix + " ".join(str(t).split())[:MAX_CHARS] for t in texts[i:i + BATCH]]
        body = json.dumps({"input": chunk}).encode()
        req = urllib.request.Request(EMB_URL, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.load(r)
        out.extend(item["embedding"] for item in d["data"])
    a = np.asarray(out, dtype=np.float32)
    n = np.linalg.norm(a, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return a / n


def _load():
    if os.path.exists(VECS) and os.path.exists(IDS):
        return np.load(VECS), np.load(IDS)
    return np.zeros((0, DIM), np.float32), np.zeros((0,), np.int64)


def _save(vecs, ids):
    os.makedirs(STORE, exist_ok=True)
    np.save(VECS + ".tmp.npy", vecs); os.replace(VECS + ".tmp.npy", VECS)
    np.save(IDS + ".tmp.npy", ids); os.replace(IDS + ".tmp.npy", IDS)


def index(verbose=True):
    vecs, ids = _load()
    have = set(int(x) for x in ids.tolist())
    c = chatdb._get()
    rows = c.execute("SELECT id, text FROM messages ORDER BY id").fetchall()
    todo = [(i, t) for i, t in rows if i not in have]
    if not todo:
        if verbose:
            print(f"nothing to embed ({len(have)} already indexed).")
        return 0
    new_ids = np.asarray([i for i, _ in todo], np.int64)
    new_vecs = _embed([t for _, t in todo], DOC_PREFIX)
    vecs = np.vstack([vecs, new_vecs]) if len(vecs) else new_vecs
    ids = np.concatenate([ids, new_ids]) if len(ids) else new_ids
    _save(vecs, ids)
    if verbose:
        print(f"embedded {len(todo)} new messages ({len(ids)} total).")
    return len(todo)


def search(query, limit=10, project=None, since=None, until=None, pool=400):
    """Return chatdb-shaped rows (id,ts,sender,direction,kind,project,chat_title,text),
    ranked by cosine similarity, after metadata filtering."""
    vecs, ids = _load()
    if not len(ids):
        return []
    qv = _embed([query], QUERY_PREFIX)[0]
    sims = vecs @ qv
    order = np.argsort(-sims)[:pool]
    ranked = [(int(ids[i]), float(sims[i])) for i in order]
    score = {i: s for i, s in ranked}
    c = chatdb._get()
    qmarks = ",".join("?" * len(score))
    rows = c.execute(
        f"SELECT id,ts,sender,direction,kind,project,chat_title,text,epoch "
        f"FROM messages WHERE id IN ({qmarks})", list(score)).fetchall()
    out = []
    for rid, ts, sender, direction, kind, proj, title, text, epoch in rows:
        if project and proj != project:
            continue
        if since and epoch < chatdb._parse_day(since):
            continue
        if until and epoch >= chatdb._parse_day(until, end=True):
            continue
        out.append((score[rid], rid, ts, sender, direction, kind, proj, title, text))
    out.sort(key=lambda r: -r[0])
    return [(rid, ts, sender, direction, kind, proj, title, text)
            for _s, rid, ts, sender, direction, kind, proj, title, text in out[:limit]]


def main():
    ap = argparse.ArgumentParser(prog="embed")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("index")
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--project"); s.add_argument("--since"); s.add_argument("--until")
    s.add_argument("--limit", type=int, default=10)
    a = ap.parse_args()
    if a.cmd == "index":
        index()
    elif a.cmd == "search":
        chatdb._print_rows(search(a.query, a.limit, a.project, a.since, a.until))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
