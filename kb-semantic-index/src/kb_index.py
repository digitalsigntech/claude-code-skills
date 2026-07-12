#!/usr/bin/env python3
"""Unified semantic index over a whole knowledge base.

Chunks `<KB_ROOT>/**` (Q&A files as one chunk per Q/A pair; other .md heading-aware,
size-bounded), embeds each chunk on an embeddings server (default a LOCAL nomic server at
127.0.0.1:18183 — embeddings are non-generative, so they're cheap to run on-box), and stores
vectors + metadata in `<KB_ROOT>/.kb_index/`.

Incremental + idempotent: chunks are keyed by content hash, so a rebuild only embeds NEW or
CHANGED chunks and drops chunks whose text disappeared (edits/deletes handled). Safe on a cron.

  kb_index.py index                     # build / refresh the index
  kb_index.py search "how do I reset it" [-k 8] [--json]
  kb_index.py ask    "how do I reset it" [--json]
  kb_index.py stats

Config (environment):
  KB_ROOT            root folder of the knowledge base (default: ~/myproject/knowledge-base)
  KB_INDEX_DIRS      comma-separated subdirs to index (default: products,company,faq,technical,from-emails)
  KB_EMBED_URL       embeddings endpoint (default: http://127.0.0.1:18183/v1/embeddings)
  KB_ANSWER_THRESH   min cosine score for `ask` to answer without escalating (default: 0.74)

The embeddings backend is bring-your-own: any OpenAI-compatible /v1/embeddings endpoint that
returns 768-dim vectors works. Swap the model/URL and dimension (DIM) to match your server.
"""
import os, re, sys, json, glob, hashlib, argparse, urllib.request
import numpy as np

KB_ROOT = os.path.expanduser(os.environ.get("KB_ROOT", "~/myproject/knowledge-base"))
INDEX_DIRS = [d for d in os.environ.get(
    "KB_INDEX_DIRS", "products,company,faq,technical,from-emails").split(",") if d]
STORE = os.path.join(KB_ROOT, ".kb_index")
VECS_F, META_F = os.path.join(STORE, "vectors.npy"), os.path.join(STORE, "meta.json")
EMB_URL = os.environ.get("KB_EMBED_URL", "http://127.0.0.1:18183/v1/embeddings")
DIM, BATCH, MAX_CHARS = 768, 64, 900


def _embed(texts, kind):
    prefix = "search_document: " if kind == "document" else "search_query: "
    out = []
    for i in range(0, len(texts), BATCH):
        chunk = [prefix + " ".join(str(t).split())[:2000] for t in texts[i:i + BATCH]]
        body = json.dumps({"input": chunk}).encode()
        req = urllib.request.Request(EMB_URL, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            out.extend(it["embedding"] for it in json.load(r)["data"])
    a = np.asarray(out, dtype=np.float32)
    n = np.linalg.norm(a, axis=1, keepdims=True); n[n == 0] = 1.0
    return a / n


def _title(text, path):
    for l in text.splitlines():
        if l.startswith("# "):
            return l[2:].strip()
    return os.path.splitext(os.path.basename(path))[0]


def _chunks_qa(text, title):
    pairs = re.finditer(r"^\*\*Q:\*\*\s*(.+?)\n\*\*A:\*\*\s*(.+?)\s*$", text, re.M)
    return [f"[{title}] Q: {m.group(1).strip()}\nA: {m.group(2).strip()}" for m in pairs]


_TABLE_SEP = re.compile(r"^\s*\|[\s:|-]+\|\s*$")            # the |---|---| divider row


def _table_rows(p, ctx):
    """If paragraph `p` is a markdown table, return one chunk PER DATA ROW with the header
    row prepended (so each row carries its column meaning and embeds on its own signal —
    e.g. a single distinctive row stops getting averaged into a 20-row blob). Else None."""
    pipe = [l.strip() for l in p.splitlines() if l.strip().startswith("|")]
    if len(pipe) < 2 or not any(_TABLE_SEP.match(l) for l in pipe):
        return None
    header = pipe[0]
    rows = [f"[{ctx}] {header}\n{l}" for l in pipe[1:] if not _TABLE_SEP.match(l)]
    return rows or None


def _chunks_md(text, title):
    heading, buf, size, chunks = "", [], 0, []
    def ctx():
        return title + (f" — {heading}" if heading else "")
    def emit():
        nonlocal buf, size
        if buf:
            chunks.append(f"[{ctx()}]\n" + "\n\n".join(buf)); buf, size = [], 0
    for para in re.split(r"\n\s*\n", text):
        p = para.strip()
        if not p:
            continue
        if re.match(r"^#{1,6}\s", p):
            emit(); h = re.sub(r"^#{1,6}\s*", "", p).strip()
            heading = "" if h == title else h; continue   # don't echo the H1 (already the title)
        table = _table_rows(p, ctx())
        if table:                                          # split tables row-by-row
            emit(); chunks.extend(table); continue
        if size + len(p) > MAX_CHARS and buf:
            emit()
        buf.append(p); size += len(p)
    emit()
    return chunks


def iter_chunks():
    """Yield (hash, source_rel, text) for every chunk in the KB."""
    for d in INDEX_DIRS:
        for path in sorted(glob.glob(os.path.join(KB_ROOT, d, "**", "*.md"), recursive=True)):
            if ".bak" in path or os.path.basename(path).startswith("_"):
                continue
            text = open(path, encoding="utf-8").read()
            title = _title(text, path)
            parts = _chunks_qa(text, title) if path.endswith("qa.md") else _chunks_md(text, title)
            rel = os.path.relpath(path, KB_ROOT)
            for t in parts:
                if t.strip():
                    h = hashlib.md5(f"{rel}::{t}".encode()).hexdigest()
                    yield h, rel, t


def _load():
    if os.path.exists(VECS_F) and os.path.exists(META_F):
        return np.load(VECS_F), json.load(open(META_F))
    return np.zeros((0, DIM), np.float32), []


def _save(vecs, meta):
    os.makedirs(STORE, exist_ok=True)
    np.save(VECS_F + ".tmp.npy", vecs); os.replace(VECS_F + ".tmp.npy", VECS_F)
    json.dump(meta, open(META_F + ".tmp", "w")); os.replace(META_F + ".tmp", META_F)


def cmd_index(_):
    cur = {h: (src, txt) for h, src, txt in iter_chunks()}
    vecs, meta = _load()
    have = {m["hash"]: i for i, m in enumerate(meta)}
    keep = [i for i, m in enumerate(meta) if m["hash"] in cur]          # drop stale (edited/deleted)
    vecs, meta = (vecs[keep], [meta[i] for i in keep]) if len(vecs) else (vecs, meta)
    have = {m["hash"] for m in meta}
    new = [(h, s, t) for h, (s, t) in cur.items() if h not in have]
    if new:
        nv = _embed([t for _, _, t in new], "document")
        vecs = np.vstack([vecs, nv]) if len(vecs) else nv
        meta += [{"hash": h, "source": s, "text": t} for h, s, t in new]
        _save(vecs, meta)
    elif len(meta) != len(json.load(open(META_F)) if os.path.exists(META_F) else meta):
        _save(vecs, meta)                                              # persisted a pure prune
    srcs = len({m["source"] for m in meta})
    print(f"kb-index: {len(meta)} chunks from {srcs} files (+{len(new)} new, {len(cur)-len(new)} unchanged)")


def cmd_search(a):
    vecs, meta = _load()
    if not len(vecs):
        print("index empty — run `kb_index.py index` first"); return
    q = _embed([a.query], "query")[0]
    order = np.argsort(-(vecs @ q))[:a.k]
    hits = [{"score": round(float(vecs[i] @ q), 3), "source": meta[i]["source"],
             "text": meta[i]["text"]} for i in order]
    if a.json:
        print(json.dumps(hits, ensure_ascii=False, indent=2)); return
    for h in hits:
        snippet = " ".join(h["text"].split())
        print(f"\n[{h['score']}] {h['source']}\n  {snippet[:300]}{'…' if len(snippet)>300 else ''}")


ANSWER_THRESH = float(os.environ.get("KB_ANSWER_THRESH", "0.74"))


def cmd_ask(a):
    """Tier-1 reflex: answer a question DIRECTLY from a confident Q&A hit, no LLM.
    Prints the stored answer verbatim on a hit; prints nothing + exits 2 to signal
    the caller to escalate to a reasoning model."""
    vecs, meta = _load()
    if not len(vecs):
        sys.stderr.write("index empty\n"); sys.exit(2)
    q = _embed([a.query], "query")[0]
    sims = vecs @ q
    i = int(np.argmax(sims))
    top, score = meta[i], float(sims[i])
    m = re.match(r"^\[.*?\]\s*Q:\s*(.+?)\nA:\s*(.+)$", top["text"], re.S)
    if a.json:
        print(json.dumps({"hit": bool(m and score >= ANSWER_THRESH), "score": round(score, 3),
                          "source": top["source"],
                          "answer": m.group(2).strip() if m else None}, ensure_ascii=False))
        sys.exit(0 if (m and score >= ANSWER_THRESH) else 2)
    if m and score >= ANSWER_THRESH:
        sys.stderr.write(f"[reflex {score:.3f} {top['source']}]\n")
        print(m.group(2).strip()); sys.exit(0)
    sys.stderr.write(f"[no confident Q&A match: best {score:.3f} {top['source']} — escalate]\n")
    sys.exit(2)


def cmd_stats(_):
    _, meta = _load()
    from collections import Counter
    by = Counter(m["source"].split("/")[0] for m in meta)
    print(f"kb-index: {len(meta)} chunks | by area: {dict(by)}")


def retrieve(query, k=6):
    """Programmatic top-k retrieval for other tools. Returns list of
    {score, source, text}; [] if the index/embeddings are unavailable."""
    try:
        vecs, meta = _load()
        if not len(vecs):
            return []
        q = _embed([query], "query")[0]
        return [{"score": float(vecs[i] @ q), "source": meta[i]["source"], "text": meta[i]["text"]}
                for i in np.argsort(-(vecs @ q))[:k]]
    except Exception:
        return []


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("index").set_defaults(func=cmd_index)
    s = sub.add_parser("search"); s.add_argument("query"); s.add_argument("-k", type=int, default=8)
    s.add_argument("--json", action="store_true"); s.set_defaults(func=cmd_search)
    k = sub.add_parser("ask"); k.add_argument("query"); k.add_argument("--json", action="store_true")
    k.set_defaults(func=cmd_ask)
    sub.add_parser("stats").set_defaults(func=cmd_stats)
    a = ap.parse_args(); a.func(a)
