"""Business notes — dictated company facts, in one dated markdown file.

The owner asked for a business-notes store. There was none — his private note store on one
side, the customer-facing knowledge base on the other, and nothing in between
for company information that simply isn't for customers.

Storage: knowledge-base/private/business-notes.md — one dated bullet per note.
A flat file on purpose: he reads it, edits it and deletes lines from it without
me, which is not true of a database.

PRIVACY: `private/` is NOT in the KB semantic index (KB_INDEX_DIRS lists
products, company, faq, technical, from-emails, from-pdfs, from-scans), so
nothing here can surface in a customer answer or a public-group reply. This is
company-private, not owner-private: it is a different store from personal/,
and the callers gate it the same strict way until someone asks otherwise.

CLI: python3 business_notes.py list | search <words> | add <text>
"""
import os
import re
import time

import tgconf as C

FILE = os.path.join(C.WORKSPACE_ROOT, "knowledge-base", "private", "business-notes.md")
HEADER = """# Business notes

Facts the owner dictates for the business — dated, newest at the bottom.

This file lives in `knowledge-base/private/`, which is NOT in the KB semantic
index (`KB_INDEX_DIRS` = products, company, faq, technical, from-emails,
from-pdfs, from-scans). Nothing here can surface in a customer-facing answer or
a public-group reply. It is separate from `personal/` — that store is
the owner's private one and is gated per chat; this one is company information
that simply isn't for customers.

"""
BULLET = re.compile(r"^- (\d{4}-\d{2}-\d{2}) — (.+)$")


def _toks(s):
    return [t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if t]


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


def search(query, limit=4):
    """Notes matching every query token, newest first."""
    qtoks = _toks(query)
    if not qtoks:
        return []
    hits = []
    for date, text in reversed(notes()):
        hay = _toks(text) + _toks(date)
        if all(any(t == h or (len(t) >= 3 and t in h) for h in hay) for t in qtoks):
            hits.append((date, text))
        if len(hits) >= limit:
            break
    return hits


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
