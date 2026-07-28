#!/usr/bin/env python3
"""Strict privacy router (Gate #3, strict mode).

Decide whether a query is PUBLIC (safe for the Anthropic cloud / Claude) or PRIVATE
(must be processed on-box by Nemotron). Policy: DEFAULT PRIVATE — a query is public
only if the knowledge it needs comes exclusively from public-labeled sources (see
data-classification.json). Fail closed: no clear public match, or any private source
in play, or any error -> PRIVATE.

If PRIVATE, we answer it HERE with Nemotron (retrieval-augmented over the local KB) so
the data never reaches the cloud. Nemotron is on OpenRouter today and moving on-box
(DGX Spark) — the routing logic doesn't change when it does.

  privacy_route.py "<query>" --json
Output JSON: {decision: "public"|"private", reason, answer?, sources?}
"""
import os, sys, json, argparse

KB_DIR = os.path.dirname(os.path.abspath(__file__))
DST_ROOT = os.path.dirname(os.path.dirname(KB_DIR))
sys.path.insert(0, DST_ROOT)                       # data_labels
sys.path.insert(0, os.path.join(DST_ROOT, "telegram"))  # privacy_router (intent regex)

import data_labels
from kb_index import retrieve
from kb_answer import _nemotron            # reuse the Nemotron (OpenRouter) client

try:
    import privacy_router as _pr           # query-intent classifier (financial/PII)
    _intent = _pr.is_private
except Exception:
    _intent = lambda t: (False, "no-intent-module")

# A retrieved chunk counts as "relevant" (actually about this query) at/above this score.
RELEVANT = float(os.environ.get("PRIVACY_RELEVANT_SCORE", "0.55"))

PRIVATE_SYSTEM = (
    "You are the PRIVATE assistant of Digital Sign Technologies (DST), makers of the "
    "Print Head Doctor and Print Head Tester. You run on-box and are trusted with "
    "confidential company data — customer records, balances, internal notes. You are "
    "mid-conversation with Vladimir (DST co-owner) over Telegram: the recent chat "
    "history is provided — use it to understand what he's referring to, exactly as if "
    "you had been present for the whole conversation. Messages may also come from "
    "Marina (DST co-owner) — each is labeled with its sender; address the person who "
    "sent the current message. Knowledge-base excerpts and CRM "
    "records are also provided; ground factual claims in them. Be concise, direct and "
    "factual.\n"
    "IMPORTANT: You have NO tools IN THIS DEGRADED TURN (the normal tool-calling agent "
    "failed; you are the fallback). You cannot look anything up, run queries, check "
    "databases, fetch pages, or take any action — this reply is your ONLY output and "
    "nothing runs after it. NEVER say you will check/do/look into something. If the "
    "provided context doesn't settle the answer, state plainly what's missing and stop. "
    "If the request needed an ACTION (fetching a web page or image, sending a file, "
    "scheduling), say a temporary problem prevented it and to please resend the request "
    "— NEVER claim you lack the ability in general; the normal agent has tools for it."
)


def _label(source):
    """Label a retrieved chunk's source. `source` is relative to knowledge-base/."""
    return data_labels.label_for(os.path.join(DST_ROOT, "knowledge-base", source))


def decide(text):
    """Return (decision, reason, hits). DEFAULT PRIVATE / fail closed."""
    # 1) Query intent that is inherently sensitive (balances owed, PII) -> private.
    try:
        if _intent(text)[0]:
            return "private", "sensitive-intent", []
    except Exception:
        pass
    # 2) Retrieval: what knowledge does this query actually pull in?
    try:
        hits = retrieve(text, k=6)
    except Exception as e:
        return "private", f"retrieval-error:{e}", []
    relevant = [h for h in hits if h.get("score", 0) >= RELEVANT]
    if not relevant:
        # Nothing clearly public matches -> could need private/other data -> fail closed.
        return "private", "no-public-match", hits
    if all(_label(h["source"]) == "public" for h in relevant):
        return "public", "public-only", hits
    return "private", "private-source", hits


_STOP = {"what", "why", "how", "when", "where", "who", "did", "does", "do", "the",
         "a", "an", "is", "are", "was", "were", "with", "from", "about", "have",
         "has", "had", "want", "wanted", "will", "would", "our", "their", "them",
         "they", "this", "that", "much", "many", "get", "got", "can", "could",
         "please", "tell", "show", "give", "know", "need", "customer", "client"}


def _terms(text):
    """Content words worth exact-matching in the CRM (names like 'Snuggle' that
    semantic embeddings miss)."""
    import re as _re
    return [w for w in _re.findall(r"[a-zA-Z][a-zA-Z0-9&'-]{3,}", text.lower())
            if w not in _STOP][:6]


def crm_context(text, max_chars=5000):
    """On-box keyword lookup over the CRM: per-contact rolling summaries + excerpts of
    the most relevant archived emails. Complements semantic KB retrieval, which is weak
    on rare proper nouns. Best-effort: returns '' on any failure."""
    import sqlite3, re as _re
    terms = _terms(text)
    if not terms:
        return ""
    out = []
    try:
        db = sqlite3.connect(os.path.join(DST_ROOT, "crm", "contacts.db"))
        # 1) Contact summaries: match name/company/email against each term.
        seen = set()
        for t in terms:
            for email, name, company, base, act in db.execute(
                    "SELECT email, name, company, base_summary, activity_summary FROM contacts "
                    "WHERE lower(coalesce(name,'')||' '||coalesce(company,'')||' '||email) "
                    "LIKE ? LIMIT 3", (f"%{t}%",)):
                if email in seen:
                    continue
                seen.add(email)
                summ = " ".join(s for s in (base, act) if s).strip()
                if summ:
                    out.append(f"[CRM contact {name or email} ({company or 'n/a'})]\n{summ[:1800]}")
        # 2) Email excerpts: rank archived mail by how many terms it matches, newest first.
        score = "+".join(
            f"(CASE WHEN instr(lower(coalesce(subject,'')||' '||coalesce(body,'')), ?) > 0 THEN 1 ELSE 0 END)"
            for _ in terms)
        rows = db.execute(
            f"SELECT date, from_addr, subject, body, ({score}) AS m FROM emails "
            f"WHERE m > 0 ORDER BY m DESC, internal_date DESC LIMIT 3",
            terms).fetchall()
        for date, frm, subj, body, m in rows:
            body = body or ""
            # window around the first matched term so the excerpt is on-topic
            pos = min((p for p in (body.lower().find(t) for t in terms) if p >= 0), default=0)
            lo = max(0, pos - 200)
            out.append(f"[Email {date} from {frm} — {subj}]\n…{body[lo:lo + 800].strip()}…")
    except Exception:
        return "\n\n".join(out)[:max_chars]
    return "\n\n".join(out)[:max_chars]


def answer_private(text, hits, history="", sender="Vladimir", chat_id=None):
    """Answer on Nemotron with FULL context. Returns (answer, files) where files is a
    list of {path, caption} for the gateway to upload to the chat. Preferred path: the
    TOOL-CALLING agent loop (private_agent.py) — Nemotron looks things up itself (CRM,
    email archive, KB) and can queue documents to deliver. Fallback: single-shot with
    pre-retrieved context (text only). Either way it never touches the cloud/Claude."""
    try:
        import private_agent
        return private_agent.run(text, history, sender=sender, chat_id=chat_id)
    except Exception as e:
        print(f"[privacy_route] agent loop failed, falling back to single-shot: {e}",
              file=sys.stderr, flush=True)
    ctx_hits = hits if hits is not None else retrieve(text, k=6)
    context = "\n\n".join(f"[{h['source']}]\n{h['text']}" for h in ctx_hits[:6]) or "(no KB match)"
    crm = crm_context(text)
    user = ""
    if history.strip():
        user += f"Recent conversation (oldest first):\n{history.strip()}\n\n"
    try:
        mem_f = os.path.join(DST_ROOT, "knowledge-base", "private", "nemotron-memory.md")
        with open(mem_f) as f:
            mem = f.read().strip()[-4000:]
        if mem:
            user += f"Private memory (facts previously saved on request):\n{mem}\n\n"
    except OSError:
        pass
    user += f"Knowledge-base excerpts:\n{context}\n\n"
    if crm:
        user += f"CRM records (contact summaries + archived email excerpts):\n{crm}\n\n"
    user += f"Message from {sender}: {text}"
    out = _nemotron(PRIVATE_SYSTEM, user, max_tokens=700, timeout=(5, 60), retry429=True)
    return (out or "").strip(), []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--answer", action="store_true",
                    help="skip the public/private decision; just answer privately on Nemotron")
    ap.add_argument("--history-stdin", action="store_true",
                    help="read recent conversation history (plain text) from stdin")
    ap.add_argument("--sender", default="Vladimir",
                    help="first name of the person who sent the message (Vladimir/Marina)")
    ap.add_argument("--chat-id", type=int, default=None,
                    help="Telegram chat the question came from (reminders fire there)")
    a = ap.parse_args()
    history = sys.stdin.read() if a.history_stdin else ""
    if a.answer:
        ans, files = answer_private(a.query, None, history, sender=a.sender,
                                    chat_id=a.chat_id)
        out = {"decision": "private", "reason": "forced-answer",
               "answer": ans, "files": files}
    else:
        decision, reason, hits = decide(a.query)
        out = {"decision": decision, "reason": reason,
               "sources": sorted({h["source"] for h in (hits or [])})}
        if decision == "private":
            out["answer"], out["files"] = answer_private(a.query, hits, history,
                                                         sender=a.sender,
                                                         chat_id=a.chat_id)
    print(json.dumps(out) if a.json else json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
