"""Audit the fast path: ask the main LLM the same question and show what CLIP missed.

the owner, 2026-08-16, after "show me the epson s3200 meteor manual" shipped a wiring
diagram and the real manual turned out not to be in the media index at all: "when you
fire the CLIP media index search on 'show me', it must be followed by a call to the
main LLM with the same prompt. If it returns more items, or different items, we must
show what was missing from the CLIP search."

The reflex still answers in ~0.4s and nothing here can slow it down — the audit runs
on a daemon thread AFTER the files are already sent. It speaks only when it has
something the reflex did not send; a confirmed-complete answer stays silent, because a
"nothing was missing" message after every request is noise that trains you to ignore it.

The LLM runs in its OWN session (a fresh --session-id), not the chat's: an audit
prompt inside the conversation would show up as something you said next turn. It runs
through bridge._run directly, so it never re-enters the gateway or the reflexes.

Every audit appends to logs/reflex_audit.jsonl — that log is the measurement of how
often the media index is wrong, and the queue of what to annotate or index next.
"""
import json
import os
import re
import threading
import time
import uuid

import bridge
import tgconf as C
import tg_api as TG

LOG = os.path.join(C.WORKSPACE_ROOT, "telegram", "logs", "reflex_audit.jsonl")
MAX_EXTRA = 4                  # never dump more than this many supplements

# The audit may only surface things the reflex itself was allowed to send. Personal
# notes have their own gate (personal_notes.allowed_chat) and are never routed through
# here; secrets and raw stores are not sendable at all.
BLOCKED = re.compile(r"/personal/|/mail|/chatlog/|/state/|token|secret|credential"
                     r"|password|api[_-]?key|\.env$|\.db$|\.key$|\.pem$", re.I)

PROMPT = """\
A user asked this in a Telegram chat: "{query}"

The fast path answered from the CLIP media index and sent these files:
{sent}

Your job is to check that answer for completeness, NOT to re-answer the user.

Search the knowledge base properly — `cd {root} && ./kb search "<terms>"` for text,
`ls`/`ug` over knowledge-base/ for files, and remember that a PDF's extracted .md in
knowledge-base/from-pdfs/ means the PDF itself sits in from-pdfs/_email_source/ or
uploads/. Consider synonyms the user did not type (manual/HUM/handbook,
SDS/MSDS/safety data sheet, diagram/schematic, spec/datasheet).

Then return STRICT JSON and nothing else:

{{"correct": ["<absolute path>", ...],
  "verdict": "complete" | "incomplete" | "wrong",
  "note": "<one short sentence, only if verdict is not complete>"}}

"correct" = every file that genuinely answers the request, best first, including any
the fast path already sent. Absolute paths that exist on disk — never invent one. If
the fast path sent exactly the right things, verdict is "complete" and note is "".
If it sent something that does not answer the request at all, verdict is "wrong".
Cap the list at 6 files. Prefer the real document over a scan or a duplicate copy.
"""


def _ask(query, sent_paths):
    """One standalone Claude turn, outside the chat's session. (text, error)."""
    prompt = PROMPT.format(
        query=query,
        root=C.WORKSPACE_ROOT,
        sent="\n".join(f"  - {p}" for p in sent_paths) or "  (nothing)")
    cmd = bridge._base_cmd(prompt) + ["--output-format", "json",
                                      "--session-id", str(uuid.uuid4())]
    return bridge._run(cmd)


def _parse(text):
    """Pull the JSON object out of the reply — models like to wrap it in prose."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    return d if isinstance(d, dict) else None


def _same_file(a, b):
    """Two paths for one document: the KB keeps a curated copy, an upload and a
    scan of the same thing, and only the timestamp prefix differs."""
    stem = lambda p: re.sub(r"^\d{8}-\d{6}[-_]|^[0-9a-f]{6,}[-_]", "",
                            os.path.splitext(os.path.basename(p))[0]).lower()
    return stem(a) == stem(b)


KB_DIR = os.path.join(C.WORKSPACE_ROOT, "knowledge-base") + os.sep
PRIVATE_KB = os.path.join(KB_DIR, "private") + os.sep


def _allowed(chat_id, path):
    """The audit may not widen who can see what.

    The file reflex hands out arbitrary workspace files only in DMs and the private
    group; everywhere else it is limited to the KB media index. An LLM picking the
    files must live inside the same fence, or "show me the price list" in a group with
    a guest in it becomes a way to pull any document on the box (the hard rule against
    company-private data reaching whitelisted outsiders is not overridable here).
    """
    ws_ok = chat_id > 0 or chat_id in C.ALWAYS_NEMOTRON_CHATS
    if path.startswith(PRIVATE_KB):        # knowledge-base/private = business-private
        return ws_ok
    return ws_ok or path.startswith(KB_DIR)


def _extras(correct, sent, chat_id=None):
    out = []
    for p in correct:
        if not isinstance(p, str) or not p.startswith("/"):
            continue
        if BLOCKED.search(p) or not os.path.isfile(p):
            continue
        if chat_id is not None and not _allowed(chat_id, os.path.abspath(p)):
            continue
        if any(_same_file(p, s) for s in sent) or any(_same_file(p, o) for o in out):
            continue
        out.append(p)
    return out[:MAX_EXTRA]


def _log(rec):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def _run(chat_id, query, sent_paths, log):
    t0 = time.time()
    text, err = _ask(query, sent_paths)
    d = _parse(text) if not err else None
    if not d:
        _log({"ts": int(t0), "chat": chat_id, "q": query, "sent": sent_paths,
              "error": (err or "unparseable")[:300]})
        log(f"reflex audit chat={chat_id} failed: {(err or 'unparseable')[:120]}")
        return
    correct = d.get("correct") or []
    extras = _extras(correct, sent_paths, chat_id)
    verdict = str(d.get("verdict") or "").lower()
    _log({"ts": int(t0), "chat": chat_id, "q": query, "sent": sent_paths,
          "correct": correct, "verdict": verdict, "note": d.get("note", ""),
          "extras": extras, "ms": int((time.time() - t0) * 1000)})
    if not extras:
        log(f"reflex audit chat={chat_id} '{query}' -> {verdict or 'complete'}, "
            f"nothing missing ({int((time.time()-t0)*1000)}ms)")
        return

    import file_reflex                      # local: avoids an import cycle at load
    note = (d.get("note") or "").strip()
    head = ("⚠️ The fast search sent the wrong thing. "
            if verdict == "wrong" else
            f"➕ The fast search missed {len(extras)} item"
            f"{'s' if len(extras) > 1 else ''}.")
    TG.send_message(chat_id, head + (f" {note}" if note else ""))
    sent_ok = []
    for p in extras:
        if file_reflex._send_doc(chat_id, p, time.time()):
            sent_ok.append(os.path.basename(p))
    log(f"reflex audit chat={chat_id} '{query}' -> {verdict}: sent {len(sent_ok)} "
        f"extra ({int((time.time()-t0)*1000)}ms): {', '.join(sent_ok)}")


def spawn(chat_id, query, sent_paths, log=lambda _m: None):
    """Fire the audit in the background. Never raises into the caller's path."""
    if not C.REFLEX_AUDIT or not query:
        return
    try:
        threading.Thread(target=_run, args=(chat_id, query, list(sent_paths), log),
                         daemon=True).start()
    except Exception as e:
        log(f"reflex audit spawn failed: {e}")


def dry_run(query, sent_paths, chat_id=C.OWNER_ID):
    """What the audit WOULD post, without touching Telegram.

        python3 reflex_audit.py "show me the s3200 manual" [already/sent.pdf ...]
    """
    text, err = _ask(query, sent_paths)
    if err:
        return {"error": err[:300]}
    d = _parse(text) or {}
    return {"verdict": d.get("verdict"), "note": d.get("note"),
            "correct": d.get("correct"),
            "extras": _extras(d.get("correct") or [], sent_paths, chat_id)}


def paths_from_summary(summary):
    """The reflex summaries carry the basenames they sent; the audit needs full
    paths, so the reflexes hand them over directly. This is the fallback for the
    doc reflex, whose summary is a workspace-relative path."""
    m = re.search(r"sent (\S+) in \d+ms", summary or "")
    if not m:
        return []
    p = os.path.join(C.WORKSPACE_ROOT, m.group(1))
    return [p] if os.path.isfile(p) else []


if __name__ == "__main__":
    import sys
    print(json.dumps(dry_run(sys.argv[1], sys.argv[2:]), indent=2))
