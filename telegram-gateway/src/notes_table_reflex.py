"""Listing notes as a table — business, personal, or both, with no model turn.

Asked for right after the two note stores went in: "I want the notes to
be output in form of table — business notes should be output with two columns:
date and note. Personal notes the same way. When I ask to output all notes,
output a three-column table with date, type of note (business or personal), and
the note."

Listing was the one case both note reflexes deliberately handed to Claude —
"show me the business notes" is not a save and not a lookup of one fact. That
was right when the answer had to be composed. It is not right now that the
answer has a fixed shape: a table of what is in the file. Same GFM the
reminders reflex emits, which the gateway renders natively in Telegram.

PRIVACY: the personal rows are the owner's private store, so every caller must
gate this the same way it gates personal_notes.send() — allowed_chat(), the
owner's DM or a live-verified bot+owner-only group. The reflex itself does not
know which chat it is in; it renders what it is asked for.
"""
import os
import re

import business_notes
import personal_notes

MAX_CELL = 120
# A note store grows; a table that is 200 rows long is not a table he can read
# on a phone. Newest first, and the count says what is below the cut.
MAX_ROWS = 25
# A note store fed by email carries the sender's furniture with it: logos,
# spacers, 1x1 tracking pixels, each one a note. They are still in the store
# and still retrievable by name — they are just not what "show me my notes"
# means. Eleven of the first fifteen rows were one hotel's letterhead.
BOILERPLATE = re.compile(r"\b(boilerplate|tracking pixel|spacer|email (logo|"
                         r"template|layout))\b", re.I)


def _cell(text):
    """One line, pipes escaped — a table cell cannot contain a newline."""
    s = " ".join((text or "").split()).replace("|", "\\|")
    return s if len(s) <= MAX_CELL else s[:MAX_CELL - 1] + "…"


def _personal_rows():
    """[(date, note)] newest first. A text note shows its text; a photo or a
    PDF has none, so it shows its label — the description the vision pass or
    the source email gave it — and only falls back to the filename. Straight
    from the db, because recent() does not carry the label."""
    con = personal_notes._db()
    got = con.execute("SELECT ts, orig_name, path, label FROM notes "
                      "ORDER BY id DESC").fetchall()
    con.close()
    rows = []
    for ts, orig, path, label in got:
        if not os.path.isfile(path):
            continue
        if BOILERPLATE.search(label or ""):
            continue
        body = personal_notes.body_of(path) or label or \
            os.path.basename(path).split("_", 1)[-1]
        # A filed document's first line is its title — the rest is the
        # document, and a table cell is not where anyone reads it.
        first = body.lstrip().split("\n", 1)[0]
        if first.startswith("#"):
            body = first.lstrip("# ").strip()
        rows.append((ts[:10], _cell(body)))
    return rows


def _business_rows():
    return [(d, _cell(t)) for d, t in reversed(business_notes.notes())]


def _tail(shown, total):
    return f"\n\n_Newest {shown} of {total}._" if total > shown else ""


def render(kind="all", limit=MAX_ROWS):
    """The table he asked for: two columns per store, three when both."""
    if kind in ("business", "personal"):
        rows = _business_rows() if kind == "business" else _personal_rows()
        if not rows:
            return f"No {kind} notes yet."
        head = ["| Date | Note |", "|---|---|"]
        body = [f"| {d} | {n} |" for d, n in rows[:limit]]
        return "\n".join(head + body) + _tail(len(body), len(rows))
    both = ([(d, "business", n) for d, n in _business_rows()]
            + [(d, "personal", n) for d, n in _personal_rows()])
    if not both:
        return "No notes yet."
    both.sort(key=lambda r: r[0], reverse=True)
    head = ["| Date | Type | Note |", "|---|---|---|"]
    body = [f"| {d} | {k} | {n} |" for d, k, n in both[:limit]]
    return "\n".join(head + body) + _tail(len(body), len(both))


LIST_ASK = re.compile(
    r"\b(show|list|output|display|print|give|read out|read me|what'?s in|"
    r"what is in|what do i have in|покажи|выведи|список|что в)\b", re.I)
NOTES = re.compile(r"\b(notes?|заметк\w+)\b", re.I)
BUSINESS = re.compile(r"\b(business|work|company|рабочи\w+|бизнес|деловы\w+)\b", re.I)
PERSONAL = re.compile(r"\b(personal|private|личны\w+|приватны\w+)\b", re.I)
EVERY = re.compile(r"\b(all|every|both|everything|все|всех|обе)\b", re.I)


def detect(text):
    """'business' | 'personal' | 'all' | None."""
    t = " ".join((text or "").split())
    if not t or len(t) > 160 or t.startswith("/"):
        return None
    if not (LIST_ASK.search(t) and NOTES.search(t)):
        return None
    biz, per = bool(BUSINESS.search(t)), bool(PERSONAL.search(t))
    if biz and not per:
        return "business"
    if per and not biz:
        return "personal"
    if biz and per:
        return "all"
    # "show me all notes" -> both stores. Bare "show my notes" has always meant
    # the personal ones, and changing that under him would be a surprise.
    return "all" if EVERY.search(t) else "personal"


def try_handle(chat_id, text, send):
    kind = detect(text)
    if not kind:
        return None
    send(chat_id, render(kind))
    return f"notes table reflex: listed {kind}"


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:])
    if q:
        k = detect(q)
        print(f"detect = {k}")
        if k:
            print(render(k))
    else:
        print(render("all"))
