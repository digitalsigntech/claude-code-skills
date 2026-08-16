"""Business notes — dictated company facts, in one dated markdown file.

the owner, 2026-08-15, on a call: "save this note in the business notes. The code
for the room is 9815." There was no such store — his private note store on one
side, the customer-facing knowledge base on the other, and nothing in between
for company information that simply isn't for customers.

Storage: knowledge-base/private/business-notes.md — one dated bullet per note.
A flat file on purpose: he reads it, edits it and deletes lines from it without
me, which is not true of a database.

PRIVACY: `private/` is NOT in the KB semantic index (TG_KB_DIRS lists
products, company, faq, technical, from-emails, from-pdfs, from-scans), so
nothing here can surface in a customer answer or a public-group reply. This is
company-private, not owner-private: it is a different store from personal/,
and the callers gate it the same strict way until someone asks otherwise.

CLI: python3 business_notes.py list | search <words> | add <text>
"""
import os
import re
import subprocess
import threading
import time

import tgconf as C

FILE = os.path.join(C.WORKSPACE_ROOT, "knowledge-base", "private", "business-notes.md")
HEADER = """# Business notes

## About this file

Facts the owner dictates for the business — the business-private knowledge
base, dated, newest at the bottom.

It lives in `knowledge-base/private/`, which is NOT in the company KB semantic
index, so nothing here can surface in a customer-facing answer or a public-group
reply. It has its OWN index instead — `./nk business search`.

The notes below are the indexed part. Keep any explanation above the `## Notes`
heading, so the prose never outranks a fact in a search.

## Notes

"""
BULLET = re.compile(r"^- (\d{4}-\d{2}-\d{2}) — (.+)$")


def _toks(s):
    """Words, in any alphabet.

    This split was [^a-z0-9]+ until 2026-08-16, so a question asked in Russian
    tokenised to nothing and search() returned before it began — the store was
    silently English-only, in a house where half the questions are not."""
    return [t for t in re.split(r"[^\w]+", (s or "").lower(), flags=re.U) if t]


# ---- who may read this ------------------------------------------------------------
# the owner, 2026-08-15: "The business private knowledge base should be accessible to
# the company owners. in our case it's me and the second owner."
#
# "In our case" is the important half: the RULE is company owners, the pair is
# just today's answer. So the set is read from the accounts registry by
# position, not written here — adding a third owner there grants access, and
# nobody has to remember this file exists.
def owners():
    """Telegram ids whose registry position says owner. Empty on any failure,
    which denies rather than grants — a registry we cannot read is not a
    reason to open the door."""
    try:
        import sys
        sys.path.insert(0, os.path.join(C.WORKSPACE_ROOT, "operations", "accounts"))
        import accounts, json
        users = json.load(open(os.path.join(
            os.path.dirname(accounts.__file__), "users.json")))
        return {int(uid) for uid, u in users.items()
                if "owner" in (u.get("position") or "").lower()}
    except Exception:
        return set()


def allowed_chat(chat_id, viewer=None):
    """True where a company-private note may be read: an owner's own DM, or a
    group whose only human is an owner. Same shape as the personal gate, a
    wider set of people — and it still fails closed."""
    import personal_notes
    who = owners()
    if not who:
        return False
    if viewer is not None and int(viewer) not in who:
        return False                     # not an owner, whatever room this is
    if chat_id > 0:
        # A DM belongs to exactly one person, so it must be the ASKER's own:
        # an owner is still not entitled to be answered inside another
        # owner's private chat.
        return int(chat_id) in who and (viewer is None or int(chat_id) == int(viewer))
    return any(personal_notes.allowed_chat(chat_id, viewer=o) for o in who)


NK = os.path.join(C.WORKSPACE_ROOT, "nk")


def _reindex():
    """Refresh the business index behind the answer — same reason as the
    personal one: a note is saved the moment it is written, and searching it
    is a service that catches up a beat later."""
    def run():
        try:
            subprocess.run([NK, "business", "index"], capture_output=True, timeout=300)
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


def add(body):
    """Append a dated bullet. Returns the line written, or None."""
    body = " ".join((body or "").split()).strip().rstrip(".")
    if len(body) < 3:
        return None
    os.makedirs(os.path.dirname(FILE), exist_ok=True)
    if not os.path.exists(FILE):
        with open(FILE, "w") as fh:
            fh.write(HEADER)
    line = f"- {time.strftime('%Y-%m-%d')} — {body}.\n"
    with open(FILE, "a") as fh:
        fh.write(line)
    _reindex()
    return line.strip()


def notes():
    """[(date, text)] oldest first."""
    if not os.path.exists(FILE):
        return []
    out = []
    for ln in open(FILE, errors="replace"):
        m = BULLET.match(ln.rstrip())
        if m:
            out.append((m.group(1), m.group(2)))
    return out


VECTORS = os.path.join(os.path.dirname(FILE), ".note_vectors.json")


def search(query, limit=4):
    """Notes that answer the question, best first.

    Two passes, and the second one is why this is not a keyword search
    (2026-08-16 — "what is my marriott membership no?" returned nothing because
    the note says "number"):

      1. every query token present — instant, no model, and the common case;
      2. otherwise rank by MEANING against the local embedding server, so
         "my marriott membership", "my hotel loyalty id" and the Russian for it
         all reach the same bullet.

    A question that matches nothing well returns nothing: the ranker has a
    floor, and the caller treats an empty result as "let the model answer"."""
    qtoks = _toks(query)
    if not qtoks:
        return []
    rows = list(reversed(notes()))                 # newest first
    hits = []
    for date, text in rows:
        hay = _toks(text) + _toks(date)
        if all(any(t == h or (len(t) >= 3 and t in h) for h in hay) for t in qtoks):
            hits.append((date, text))
        if len(hits) >= limit:
            break
    if hits:
        return hits
    try:
        import note_search
        ranked = note_search.rank(query, [t for _, t in rows],
                                  note_search.VectorCache(VECTORS))
        return [rows[i] for _, i in ranked[:limit]]
    except Exception:
        return []


if __name__ == "__main__":
    import sys
    cmd = (sys.argv[1:] or ["list"])[0]
    rest = " ".join(sys.argv[2:])
    if cmd == "add":
        print(add(rest) or "nothing to add")
    elif cmd == "search":
        for d, t in search(rest):
            print(f"{d}  {t}")
    else:
        for d, t in notes():
            print(f"{d}  {t}")
