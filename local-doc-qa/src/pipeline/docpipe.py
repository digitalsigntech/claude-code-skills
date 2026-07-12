#!/usr/bin/env python3
"""docpipe — local, private document Q&A / extraction.

Pipeline (runs entirely against your configured model endpoints; local by default):
  parse (PDF/CSV/text) -> chunk -> embed -> store (numpy)
  ask: embed query -> retrieve top-k -> answer with the chat model, with citations.

With a local backend, document content never leaves the machine. An orchestrator
(e.g. a coding agent) can call this tool and read its *results* — not the raw files.

Usage:
  docpipe.py ingest <file|dir> [...]     add documents to the local index
  docpipe.py ask "question" [-k 6]       grounded answer over indexed docs
  docpipe.py summarize <file>            summarize a single document (no index)
  docpipe.py list                        show indexed files
  docpipe.py reset                       wipe the index
  docpipe.py health                      check the model servers
"""
import os, sys, argparse, glob
import parse as P
import llm
from store import VectorStore
from config import TOP_K, CHUNK_CHARS

SUPPORTED = (".pdf", ".csv", ".tsv", ".txt", ".md", ".json", ".log", ".yaml", ".yml", ".html", ".xml")


def _expand(paths):
    out = []
    for p in paths:
        if os.path.isdir(p):
            for root, _, files in os.walk(p):
                for fn in files:
                    if fn.lower().endswith(SUPPORTED):
                        out.append(os.path.join(root, fn))
        else:
            out.extend(glob.glob(p))
    return sorted(set(out))


def cmd_ingest(args):
    store = VectorStore()
    files = _expand(args.paths)
    if not files:
        print("No matching files."); return
    total = 0
    for path in files:
        # A PDF is processed ONCE into a .md (the source of truth); index that .md.
        if path.lower().endswith(".pdf"):
            import pdf2md
            md = pdf2md.convert(path, force=args.force)
            print(f"  pdf -> md: {path}  ->  {md}")
            path = md
        src = os.path.abspath(path)
        if store.has_file(src) and not args.force:
            print(f"  skip (already indexed): {path}"); continue
        if store.has_file(src):
            store.remove_file(src)
        chunks = P.parse_file(path)
        if not chunks:
            print(f"  empty: {path}"); continue
        # embed in batches to bound memory
        embs = []
        B = 32
        for i in range(0, len(chunks), B):
            embs.append(llm.embed([c["text"] for c in chunks[i:i+B]], kind="document"))
        import numpy as np
        store.add(chunks, np.vstack(embs))
        total += len(chunks)
        print(f"  indexed {len(chunks):4d} chunks  <- {path}")
    store.save()
    print(f"Done. {total} new chunks. Index now: {store.stats()}")


def _format_context(hits):
    blocks = []
    for h in hits:
        tag = f"[{os.path.basename(h['source'])} | {h['locator']}]"
        blocks.append(f"{tag}\n{h['text']}")
    return "\n\n---\n\n".join(blocks)


def cmd_ask(args):
    # Route exact numeric/value questions to the structured engine (deterministic,
    # reads the .md tables directly — no LLM arithmetic). Everything else -> RAG.
    import structured
    if not args.rag and structured.is_numeric_query(args.question):
        res = structured.answer(args.question)
        if res:
            print(res)
            print("\n(answered from structured .md tables — exact)")
            return
    store = VectorStore()
    if store.stats()["chunks"] == 0:
        print("Index is empty — run `ingest` first."); return
    from config import effective_k
    k = args.k if args.k else effective_k(args.question)
    qvec = llm.embed(args.question, kind="query")[0]
    hits = store.search(qvec, k=k)
    context = _format_context(hits)
    sys_prompt = (
        "You answer strictly from the provided document excerpts. "
        "Cite the source tag in [brackets] after each fact you use. "
        "If the answer is not in the excerpts, say you don't have it. "
        "For a simple lookup, answer concisely in one or two sentences. "
        "For a list/aggregation/numeric-threshold question, work in two steps internally: "
        "(1) gather EVERY relevant item with its exact value from ALL excerpts; "
        "(2) keep ONLY items that strictly satisfy the threshold, dropping any at or below it, "
        "re-checking each number — then output just the final filtered list.")
    user = f"Document excerpts:\n\n{context}\n\nQuestion: {args.question}\n\nAnswer with citations:"
    ans = llm.chat([{"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user}], temperature=0.0, max_tokens=args.max_tokens)
    print(ans)
    print("\nSources:")
    for h in hits:
        print(f"  [{os.path.basename(h['source'])} | {h['locator']}]  (score {h['score']:.2f})")


def cmd_summarize(args):
    chunks = P.parse_file(args.file)
    if not chunks:
        print("Nothing to summarize."); return
    # map-reduce summary so large docs fit the context window
    partials = []
    window, cur = [], 0
    for c in chunks:
        if cur + len(c["text"]) > CHUNK_CHARS * 6 and window:
            partials.append("\n".join(window)); window, cur = [], 0
        window.append(f"[{c['locator']}] {c['text']}"); cur += len(c["text"])
    if window:
        partials.append("\n".join(window))
    notes = []
    for i, part in enumerate(partials, 1):
        notes.append(llm.chat([
            {"role": "system", "content": "Summarize the key facts faithfully. Keep figures, names, dates."},
            {"role": "user", "content": part}], max_tokens=400))
    if len(notes) == 1:
        print(notes[0]); return
    final = llm.chat([
        {"role": "system", "content": "Combine these section summaries into one coherent summary."},
        {"role": "user", "content": "\n\n".join(notes)}], max_tokens=600)
    print(final)


def cmd_list(args):
    store = VectorStore()
    st = store.stats()
    print(f"{st['files']} file(s), {st['chunks']} chunk(s):")
    for f in store.files():
        print("  ", f)


def cmd_reset(args):
    from config import STORE_DIR
    for fn in ("vectors.npy", "meta.json"):
        p = os.path.join(STORE_DIR, fn)
        if os.path.exists(p): os.remove(p)
    print("Index wiped.")


def cmd_health(args):
    c, e = llm.health()
    print(f"chat server:      {'UP' if c else 'DOWN'}")
    print(f"embedding server: {'UP' if e else 'DOWN'}")
    sys.exit(0 if (c and e) else 1)


def main():
    ap = argparse.ArgumentParser(description="Local private document pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("ingest"); g.add_argument("paths", nargs="+"); g.add_argument("--force", action="store_true"); g.set_defaults(func=cmd_ingest)
    g = sub.add_parser("ask"); g.add_argument("question"); g.add_argument("-k", type=int, default=0); g.add_argument("--max-tokens", type=int, default=1024); g.add_argument("--rag", action="store_true", help="force the RAG path (skip structured)"); g.set_defaults(func=cmd_ask)
    g = sub.add_parser("summarize"); g.add_argument("file"); g.set_defaults(func=cmd_summarize)
    g = sub.add_parser("list"); g.set_defaults(func=cmd_list)
    g = sub.add_parser("reset"); g.set_defaults(func=cmd_reset)
    g = sub.add_parser("health"); g.set_defaults(func=cmd_health)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
