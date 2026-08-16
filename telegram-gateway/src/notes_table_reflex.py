"""Listing notes as a table — business, personal, or both, with no model turn.

Asked for 2026-08-15: "I want the notes to be output in form of table." Then, an
hour later: "they should be similar to reminders whereas we have three columns,
date, note and picture. There may be more than one picture per note and there
may be PDFs as well as pictures per note."

That second sentence is the design. A note is not a file — a forwarded booking
is a summary AND the ticket AND the confirmation PDF, filed in the same breath
under the same subject. The store keeps one row per FILE, so the first version
of this table printed the summary and its attachments as separate, equal lines,
which is exactly the shape the owner was objecting to when it happened to email
letterhead. Rows are grouped back together here (see _group).

Two clients, two tables, the same split the reminders reflex arrived at the
hard way:

  • THE APP (client="ios") renders markdown, so the Picture column carries
    ![](vb-token:TOKEN) thumbnails — several per cell when a note has several.
  • TELEGRAM silently DROPS image syntax inside a sendRichMessage table cell
    (probed live 2026-08-07 — the cell comes back empty), so the third column
    there NAMES what is attached ("2 photos, 1 PDF") and the files follow as
    their own captioned messages, one per message, never an album.

PRIVACY: the personal rows are the owner's private store, so every caller must
gate this the same way it gates personal_notes.send() — allowed_chat(), the
owner's DM or a live-verified bot+owner-only group. The reflex itself does not
know which chat it is in; it renders what it is asked for.
"""
import os
import re
import subprocess
import sys

import business_notes
import personal_notes

HERE = os.path.dirname(os.path.abspath(__file__))
SENDFILE = os.path.join(HERE, "sendfile.py")

MAX_CELL = 120
# A note store grows; a table 200 rows long is not a table the owner can read on a
# phone. Newest first, and the count says what is below the cut.
MAX_ROWS = 25
# A note store fed by email used to carry the sender's furniture with it: logos,
# spacers, 1x1 tracking pixels, each one its own note. idle_watcher no longer
# saves them (2026-08-15), but the ones already filed are still in the store —
# hidden from the list, never from search, never deleted.
BOILERPLATE = re.compile(r"\b(boilerplate|tracking pixel|spacer|email (logo|"
                         r"template|layout))\b", re.I)
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".bmp")
DOC_EXT = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv")


def _cell(text):
    """One line, pipes escaped — a table cell cannot contain a newline."""
    s = " ".join((text or "").split()).replace("|", "\\|")
    return s if len(s) <= MAX_CELL else s[:MAX_CELL - 1] + "…"


def _is_image(path):
    return path.lower().endswith(IMAGE_EXT)


def _title_of(path, label, orig):
    """What this note SAYS. A text note says its text (its first heading if it
    has one — the rest is the document, and a cell is not where it is read); a
    photo or a PDF says the label it was filed under; failing both, its name."""
    body = personal_notes.body_of(path)
    if body:
        first = body.lstrip().split("\n", 1)[0]
        return first.lstrip("# ").strip() if first.startswith("#") else body
    return label or os.path.basename(path).split("_", 1)[-1]


def _group(rows):
    """[(date, text, [file paths])] newest first.

    The grouping key is the STORED FILENAME STAMP — every file of one email is
    written by the same add() sweep and lands as 20260725-163313_<name>, to the
    second. The label looked like the obvious key and is not: each file of that
    hotel email was labelled individually by the vision pass ("the hotel's
    email template graphic - icon 48x48"), so no two shared a label while all
    twelve shared a stamp.

    The text note in the group is the row's words; everything else hangs off it
    as an attachment. A note filed on its own is a group of one, and if it is a
    photo or a PDF it is its own attachment: "show me my notes" should put the
    picture in front of him, not the word "IMG_2136.PNG".
    """
    groups, order = {}, []
    for ts, orig, path, label in rows:
        stamp = os.path.basename(path).split("_", 1)[0]
        key = stamp if re.fullmatch(r"\d{8}-\d{6}", stamp) else ts[:19]
        if key not in groups:
            groups[key] = {"date": ts[:10], "text": None, "files": [],
                           "from_body": False}
            order.append(key)
        g = groups[key]
        body = personal_notes.body_of(path)
        if body:
            # The words of the group always come from its text note, whichever
            # order the files arrive in. Getting this wrong titled a hotel
            # booking "the hotel's email template graphic - icon 64x64",
            # because the icon was simply written first.
            if not g["from_body"]:
                g["text"], g["from_body"] = _title_of(path, label, orig), True
        else:
            g["files"].append(path)
            if not g["text"]:
                g["text"] = label or os.path.basename(path).split("_", 1)[-1]
    out = []
    for key in order:
        g = groups[key]
        out.append((g["date"], g["text"], g["files"]))
    return out


def _personal_rows():
    con = personal_notes._db()
    got = con.execute("SELECT ts, orig_name, path, label FROM notes "
                      "ORDER BY id DESC").fetchall()
    con.close()
    rows = [(ts, orig, path, label or "") for ts, orig, path, label in got
            if os.path.isfile(path) and not BOILERPLATE.search(label or "")]
    return _group(rows)


def _business_rows():
    # A business note is a line in a markdown file; it has no attachments yet,
    # and the column stays so the two tables read the same way.
    return [(d, t, []) for d, t in reversed(business_notes.notes())]


def _files_cell(files, ios):
    """The Picture column. Thumbnails in the app, a count in Telegram."""
    if not files:
        return ""
    if ios:
        import reminders_reflex                     # its minter, not a copy
        cells = []
        for p in files:
            if _is_image(p):
                tok = reminders_reflex._mint(p)
                if tok:
                    cells.append(f"![](vb-token:{tok})")
                    continue
            cells.append(f"📄 {os.path.basename(p).split('_', 1)[-1]}")
        return " ".join(cells)
    pics = sum(1 for p in files if _is_image(p))
    docs = len(files) - pics
    bits = []
    if pics:
        bits.append(f"{pics} photo" + ("s" if pics > 1 else ""))
    if docs:
        bits.append(f"{docs} file" + ("s" if docs > 1 else ""))
    return ", ".join(bits)


def _tail(shown, total, files, ios):
    out = f"\n\n_Newest {shown} of {total}._" if total > shown else ""
    if not ios and files:
        out += (f"\n\n_{files} attachment{'s' if files != 1 else ''} — "
                f"sending below._")
    return out


def render(kind="all", limit=MAX_ROWS, client="telegram"):
    """The table: Date | Note | Picture, plus a Type column across both stores."""
    ios = client == "ios"
    if kind in ("business", "personal"):
        rows = _business_rows() if kind == "business" else _personal_rows()
        if not rows:
            return f"No {kind} notes yet."
        head = ["| **Date** | **Note** | **Picture** |", "|---|---|---|"]
        shown = rows[:limit]
        body = [f"| {d} | {_cell(t)} | {_files_cell(f, ios)} |"
                for d, t, f in shown]
    else:
        both = ([(d, "business", t, f) for d, t, f in _business_rows()]
                + [(d, "personal", t, f) for d, t, f in _personal_rows()])
        if not both:
            return "No notes yet."
        both.sort(key=lambda r: r[0], reverse=True)
        head = ["| **Date** | **Type** | **Note** | **Picture** |", "|---|---|---|---|"]
        shown = [(d, t, f) for d, _k, t, f in both[:limit]]
        body = [f"| {d} | {k} | {_cell(t)} | {_files_cell(f, ios)} |"
                for d, k, t, f in both[:limit]]
        rows = both
    attached = sum(len(f) for _d, _t, f in shown)
    return "\n".join(head + body) + _tail(len(body), len(rows), attached, ios)


def send_files(chat_id, kind="all", limit=MAX_ROWS):
    """Telegram only: each attachment as its own message, captioned with its
    note's date and words — the reminders rule, for the same reason. An album
    carries ONE caption for the whole group, which would put the wrong words
    under the wrong picture. Through sendfile.py, so every file lands in the
    app's attachment feed and the chat archive. Returns how many were sent."""
    if kind == "business":
        rows = _business_rows()
    elif kind == "personal":
        rows = _personal_rows()
    else:
        rows = sorted(_business_rows() + _personal_rows(),
                      key=lambda r: r[0], reverse=True)
    sent = 0
    for date, text, files in rows[:limit]:
        for path in files:
            caption = f"{date} — {text}"[:1000]
            try:
                out = subprocess.run(
                    [sys.executable, SENDFILE, str(chat_id), path, caption],
                    capture_output=True, text=True, timeout=120)
                if "sent" in (out.stdout or "").lower():
                    sent += 1
            except Exception:
                pass          # a file that fails to send must not lose the table
    return sent


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
    n = send_files(chat_id, kind)
    return f"notes table reflex: listed {kind}" + (f", {n} file(s)" if n else "")


if __name__ == "__main__":
    q = " ".join(sys.argv[1:])
    if q:
        k = detect(q)
        print(f"detect = {k}")
        if k:
            print(render(k))
    else:
        print(render("all"))
