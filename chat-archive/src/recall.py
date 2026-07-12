#!/usr/bin/env python3
"""Meaning-based recall for the chat archive — using the CHAT LLM only (no local model,
no embeddings). The LLM expands the query into related terms/synonyms/jargon, those run
against the FTS5 index to gather a candidate pool, then the LLM reranks the pool by true
relevance to the original intent. So "the pricing for that cleaning gadget" still finds
the right pricing thread even with no shared keywords.

Returns chatdb-shaped rows, so the CLI prints them with chatdb's formatter.

  recall.py "on-prem deployment decision" --limit 10
"""
import os, sys, json, subprocess, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import chatdb

# A CLI-driven chat LLM (default assumes a `claude`-style CLI). Swap via CHAT_BIN.
CHAT_BIN = os.environ.get("CHAT_BIN", "claude")
MODEL = os.environ.get("RECALL_MODEL", "opus")
POOL = 60                      # candidates pulled from FTS before rerank


def chat_json(prompt, timeout=120):
    r = subprocess.run(
        [CHAT_BIN, "-p", prompt, "--model", MODEL,
         "--dangerously-skip-permissions", "--output-format", "json"],
        capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "chat LLM failed")[:200])
    d = json.loads(r.stdout)
    return (d.get("result") or "").strip()


def _arr(raw):
    s = raw.index("["); e = raw.rindex("]") + 1
    return json.loads(raw[s:e])


def expand(query):
    """Ask the chat LLM for search terms a relevant message would likely contain."""
    prompt = (
        f'A user is searching a business chat archive for messages about: "{query}".\n'
        "List 6-12 search terms — single words or short phrases — that such messages would "
        "likely contain: synonyms, domain jargon, product names, and closely related "
        "concepts. Respond with ONLY a JSON array of strings, no prose.")
    try:
        terms = [t for t in _arr(chat_json(prompt)) if isinstance(t, str) and t.strip()]
    except Exception:
        terms = []
    return terms


def _fts_expr(terms):
    # Quote EVERY term as an FTS5 phrase literal, so hyphens/punctuation in terms like
    # "on-prem" or "air-gapped" are treated as text, not as MATCH operators.
    seen, parts = set(), []
    for t in terms:
        t = " ".join(str(t).replace('"', "").split()).strip()
        if t and t.lower() not in seen:
            seen.add(t.lower()); parts.append(f'"{t}"')
    return " OR ".join(parts)


def rerank(query, rows, limit):
    """Have the chat LLM pick the rows truly relevant to the original query."""
    listing = "\n".join(
        f"{i}. [{r[5] or '?'}] {r[2] or '?'}: " + " ".join(str(r[7]).split())[:200]
        for i, r in enumerate(rows))
    prompt = (
        f'Original search intent: "{query}"\n\nCandidate messages:\n{listing}\n\n'
        f"Return ONLY a JSON array of the indices (numbers) of the messages genuinely "
        f"relevant to the intent, most relevant first, at most {limit}. No prose.")
    try:
        idxs = [i for i in _arr(chat_json(prompt)) if isinstance(i, int) and 0 <= i < len(rows)]
        if idxs:
            return [rows[i] for i in idxs[:limit]]
    except Exception:
        pass
    return rows[:limit]


def search(query, limit=10, project=None, since=None, until=None):
    terms = expand(query)
    expr = _fts_expr(terms + [query]) or query
    try:
        rows = chatdb.search(query=expr, project=project, since=since, until=until, limit=POOL)
    except Exception:
        # if the built MATCH expression is somehow invalid, fall back to the raw query
        rows = chatdb.search(query=query, project=project, since=since, until=until, limit=POOL)
    if not rows:
        return []
    return rerank(query, rows, limit)


def main():
    ap = argparse.ArgumentParser(prog="recall")
    ap.add_argument("query")
    ap.add_argument("--project"); ap.add_argument("--since"); ap.add_argument("--until")
    ap.add_argument("--limit", type=int, default=10)
    a = ap.parse_args()
    chatdb._print_rows(search(a.query, a.limit, a.project, a.since, a.until))


if __name__ == "__main__":
    main()
