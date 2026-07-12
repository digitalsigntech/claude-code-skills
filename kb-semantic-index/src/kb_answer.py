#!/usr/bin/env python3
"""Tier-1 quick answer: retrieve a few KB chunks, let a FAST grounded LLM answer from JUST
those chunks — or say ESCALATE.

Why this beats a bare score-threshold reflex: cosine similarity is a good *retrieval* signal
but a bad *correctness* signal. A wrong, entity-twisted match can out-score the right one (e.g.
a near-duplicate about the wrong model scoring 0.80 while the correct snippet sits at 0.747).
So we don't trust the top stored answer on score alone — we hand the retrieved snippets to a
cheap LLM that actually reads them and reasons about whether they answer THIS specific question
(right subject? complete?).

Context stays TINY: top-k chunks only (~a few KB), no boilerplate, no full system prompt. One
metered fast-model call (fractions of a cent); the heavy reasoning model is only touched if this
escalates. Grounded + bounded = fast and safe.

  kb_answer.py "how do I reset the widget?" [-k 6] [--json]
Exit 0 = answered; exit 2 = ESCALATE (snippets don't cover it) -> caller runs the full LLM.

Model backend is bring-your-own. This ships with an OpenAI-compatible chat-completions client
pointed at an OpenRouter endpoint by default, but any compatible endpoint works — set the env
vars below. The API key is read from the environment / a local secrets file and is NEVER
hardcoded.

Config (environment):
  KB_LLM_URL     chat-completions endpoint (default: https://openrouter.ai/api/v1/chat/completions)
  KB_LLM_MODEL   model id (default: a fast, reasoning-off grounded model)
  KB_LLM_KEY     API key for the endpoint (preferred). If unset, falls back to reading
                 <secrets file> for a line `<KB_LLM_KEY_NAME>=...`.
  KB_LLM_KEY_NAME  key name to look up in the secrets file (default: OPENROUTER_API_KEY)
  KB_SECRETS_FILE  path to the secrets file (default: ~/.config/myproject/secrets.env)
  KB_ANSWER_DEADLINE  hard wall-clock cap in seconds for the grounded call (default: 12)
"""
import os, sys, json, argparse, urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FTimeout
from kb_index import retrieve

# Hard wall-clock cap for the whole grounded call. urllib's socket timeout only bounds a
# single read, so a slow-drip response can run for 40s+; this deadline actually kills it.
HARD_DEADLINE = float(os.environ.get("KB_ANSWER_DEADLINE", "12"))

LLM_URL = os.environ.get("KB_LLM_URL", "https://openrouter.ai/api/v1/chat/completions")
LLM_MODEL = os.environ.get("KB_LLM_MODEL", "nvidia/nemotron-3-super-120b-a12b")
KEY_NAME = os.environ.get("KB_LLM_KEY_NAME", "OPENROUTER_API_KEY")
SECRETS_FILE = os.path.expanduser(os.environ.get("KB_SECRETS_FILE", "~/.config/myproject/secrets.env"))
ESCALATE = "ESCALATE"

SYSTEM = (
    "You are a knowledge-base assistant answering a user's question over chat. You are given a "
    "few knowledge-base snippets. Answer using ONLY those snippets — never outside knowledge, "
    "never a guess.\n"
    "Rules:\n"
    "1. Answer ONLY if the snippets clearly and COMPLETELY cover THIS exact question — the "
    "right subject/item, the specific detail asked, no ambiguity. Then reply with the answer "
    "alone: concise, plain, no preamble.\n"
    "2. If the snippets are about a different item, miss a detail the question needs, or the "
    "question asks something the snippets don't settle (e.g. cross-compatibility, a value not "
    "listed), reply with EXACTLY the single token " + ESCALATE + " and nothing else.\n"
    "3. Watch entity mismatches: a snippet about item A does NOT answer a question about using "
    "item A with item B. When unsure, " + ESCALATE + ".\n"
    "4. If MULTIPLE snippets give different answers for the same thing the question names (e.g. "
    "different variants at different values), present ALL of them with what distinguishes each "
    "— never silently pick one."
)


def _load_key():
    """API key from the environment first, else a `NAME=value` line in the secrets file.
    Never hardcoded; returns None if neither is present."""
    k = os.environ.get("KB_LLM_KEY")
    if k:
        return k.strip()
    try:
        return next((l.split("=", 1)[1].strip()
                     for l in open(SECRETS_FILE)
                     if l.startswith(KEY_NAME + "=")), None)
    except Exception:
        return None


def _llm(system, user, max_tokens=400, timeout=(5, 12)):
    """Fast, reasoning-OFF grounded call. Returns content ('' on any failure)."""
    key = _load_key()
    if not key:
        return ""
    payload = {"model": LLM_MODEL, "temperature": 0.0, "max_tokens": max_tokens,
               "reasoning": {"enabled": False},
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}]}
    try:
        req = urllib.request.Request(LLM_URL, data=json.dumps(payload).encode(),
                                     headers={"Authorization": f"Bearer {key}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout[1]) as r:
            return (json.load(r)["choices"][0]["message"].get("content") or "").strip()
    except Exception:
        return ""


def quick_answer(question, k=6):
    """Return {answer, escalate, score, sources}. escalate=True means the caller should run
    the full LLM. Never raises — any failure degrades to escalate."""
    hits = retrieve(question, k=k)
    if not hits:
        return {"answer": None, "escalate": True, "score": 0.0, "sources": []}
    context = "\n\n".join(f"[{i+1}] {' '.join(h['text'].split())}" for i, h in enumerate(hits))
    user = f"User question: {question}\n\nKnowledge-base snippets:\n{context}"
    # Enforce a hard total deadline: if the grounded call overruns, we escalate rather than
    # make the user wait — the snippets are already in hand for the caller to answer from.
    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            out = ex.submit(_llm, SYSTEM, user).result(timeout=HARD_DEADLINE)
    except FTimeout:
        out = ""
    top = round(float(hits[0]["score"]), 3)
    srcs = sorted({h["source"] for h in hits})
    # On escalate we still return the retrieved snippets so the caller can answer straight from
    # them instead of re-searching the KB from scratch.
    if not out or out.strip().upper().startswith(ESCALATE):
        return {"answer": None, "escalate": True, "score": top, "sources": srcs,
                "snippets": context}
    return {"answer": out.strip(), "escalate": False, "score": top, "sources": srcs,
            "snippets": context}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("-k", type=int, default=6)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    res = quick_answer(a.query, k=a.k)
    if a.json:
        print(json.dumps(res, ensure_ascii=False))
    elif res["escalate"]:
        sys.stderr.write(f"[escalate — top {res['score']:.3f}]\n")
    else:
        print(res["answer"])
    sys.exit(2 if res["escalate"] else 0)
