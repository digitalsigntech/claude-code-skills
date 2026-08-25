"""Listing a knowledge base as a table — personal, private business, or
both, with no model turn.

the owner, 2026-08-15: "I want the notes to be output in form of table." Then, an
hour later: "they should be similar to reminders whereas we have three columns,
date, note and picture. There may be more than one picture per note and there
may be PDFs as well as pictures per note."

That second sentence is the design. A note is not a file — a forwarded booking
is a summary AND the ticket AND the confirmation PDF, filed in the same breath
under the same subject. The store keeps one row per FILE, so the first version
of this table printed the summary and its attachments as separate, equal lines,
which is exactly the shape he was objecting to when it happened to email
letterhead. Rows are grouped back together here (see _group).

Two clients, two tables, the same split the reminders reflex arrived at the
hard way:

  • THE APP (client="ios") renders markdown, so the Picture column carries
    ![](vb-token:TOKEN) thumbnails — several per cell when a note has several.
  • TELEGRAM silently DROPS image syntax inside a sendRichMessage table cell
    (probed live 2026-08-07 — the cell comes back empty), so the third column
    there NAMES what is attached ("2 photos, 1 PDF") and the files follow as
    their own captioned messages, one per message, never an album.

VOCABULARY (the owner, 2026-08-15): "we do not need note stores. We already have
private business knowledge base, public knowledge base and personal knowledge
bases." There are three knowledge bases, not stores-plus-a-KB — so "show me my
personal knowledge base" and "show my notes" are the same request, and both
phrasings are matched here. The public KB is the company one (./kb) and is not
listed by this reflex; it is not a private drawer to page through.

PRIVACY: the personal rows belong to ONE person — the viewer — and the private
business rows to the company owners. Callers gate the room
(personal_notes.allowed_chat / business_notes.allowed_chat) and pass a viewer;
this module renders what it is asked for and filters rows by that viewer.
"""
import tgconf as C   # identity from config
import os
import time
import re
import subprocess
import sys

import business_notes
import personal_notes

HOME = os.path.expanduser("~")
SENDFILE = f"{C.WORKSPACE_ROOT}/telegram/sendfile.py"

MAX_CELL = 120
# the owner, 2026-08-15: "If a user wants you to output a lot of data, output only
# the first 40 entries. We don't want to spam the chat." Newest first, and the
# count below the cut says how much was not shown.
MAX_ROWS = 40
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
    """[(date, text, [attachments], [every path in the group])] newest first.

    The grouping key is the STORED FILENAME STAMP — every file of one email is
    written by the same add() sweep and lands as 20260725-163313_<name>, to the
    second. The label looked like the obvious key and is not: each file of that
    hotel email was labelled individually by the vision pass ("Booking.com
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
                           "src": [], "from_body": False}
            order.append(key)
        g = groups[key]
        g["src"].append(path)
        body = personal_notes.body_of(path)
        if body:
            # The words of the group always come from its text note, whichever
            # order the files arrive in. Getting this wrong titled a hotel
            # booking "Booking.com email template graphic - icon 64x64",
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
        out.append((g["date"], g["text"], g["files"], g["src"]))
    return out


def _personal_rows(viewer=None):
    con = personal_notes._db()
    got = con.execute("SELECT ts, orig_name, path, label FROM notes "
                      "WHERE owner=? ORDER BY id DESC",
                      (int(viewer) if viewer else personal_notes.OWNER,)).fetchall()
    con.close()
    rows = [(ts, orig, path, label or "") for ts, orig, path, label in got
            if os.path.isfile(path) and not BOILERPLATE.search(label or "")]
    return _group(rows)


def _business_rows():
    # A business note is a line in a markdown file; it has no attachments yet,
    # and the column stays so the two tables read the same way.
    return [(d, t, [], []) for d, t in reversed(business_notes.notes())]



SCANS_DIR = os.path.join(C.WORKSPACE_ROOT, "knowledge-base/from-scans")


def _scan_rows():
    """Scanned documents: filed pages, each with the document itself attached.

    the owner, 2026-08-15: "When I ask to show all the knowledge base, the app
    shows me only a few notes. Our knowledge base is bigger. It contains
    pictures, documents, text notes." He asked for all of it and got the note
    stores — the scans were a third store nothing listed. A scan is a `.md`
    summary beside the page it was made from; the summary is the note and the
    page is the attachment, which is exactly the shape of every other row.
    """
    if not os.path.isdir(SCANS_DIR):
        return []
    rows = []
    for name in sorted(os.listdir(SCANS_DIR), reverse=True):
        if not name.endswith(".md"):
            continue
        stem = name[:-3]
        path = os.path.join(SCANS_DIR, name)
        try:
            raw = open(path, encoding="utf-8").read()
        except OSError:
            continue
        # The filename heading and the "Source file:" line are bookkeeping, not
        # the document. Reading them aloud — or putting them in a cell he is
        # scanning for content — says nothing he did not already know.
        text = " ".join(
            l for l in raw.splitlines()
            if l.strip()
            and not l.lstrip().startswith("#")
            and not l.lstrip().lower().startswith(("source file:", "scanned:",
                                                   "extracted:"))
        )
        text = " ".join(text.split()) or " ".join(raw.split())
        # The page(s) this summary was written from: same stem, any extension.
        files = [os.path.join(SCANS_DIR, f) for f in os.listdir(SCANS_DIR)
                 if f.startswith(stem.split("-scan")[0]) and not f.endswith(".md")]
        # A scan's date is in its filename: 20260809-180628-scan.md
        d = stem[:8]
        d = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if d.isdigit() else ""
        rows.append((d, text[:400], sorted(files), []))
    return rows

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


def _tail(shown, total, files, ios, everything=False):
    out = f"\n\n_Newest {shown} of {total}._" if total > shown else ""
    # AN INCOMPLETE ANSWER MUST NOT LOOK COMPLETE. "Show me all the knowledge
    # base" returns notes, scans and their pictures — but the product library
    # is thousands of files and belongs to search, not to a table. Saying so is
    # the difference between an answer and a wrong answer (2026-08-15).
    if everything:
        out += ("\n\n_Notes, scanned documents and their pictures. The product "
                "library is searched, not listed — ask for a product or a "
                "document by name._")
    if not ios and files:
        out += (f"\n\n_{files} attachment{'s' if files != 1 else ''} — "
                f"sending below._")
    return out


def render(kind="all", limit=MAX_ROWS, client="telegram", viewer=None):
    """The table: Date | Note | Picture, plus a trailing Type column across
    both stores."""
    ios = client == "ios"
    if kind in ("business", "personal"):
        rows = _business_rows() if kind == "business" else _personal_rows(viewer)
        if not rows:
            return ("Your personal knowledge base is empty." if kind == "personal"
                else "The private business knowledge base is empty.")
        head = ["| **Date** | **Note** | **Picture** |", "|---|---|---|"]
        shown = rows[:limit]
        body = [f"| {d} | {_cell(t)} | {_files_cell(f, ios)} |"
                for d, t, f, _s in shown]
    else:
        both = ([(d, "business", t, f) for d, t, f, _s in _business_rows()]
                + [(d, "personal", t, f) for d, t, f, _s in _personal_rows(viewer)]
                + [(d, "document", t, f) for d, t, f, _s in _scan_rows()])
        if not both:
            return "Both knowledge bases are empty."
        both.sort(key=lambda r: r[0], reverse=True)
        # Type last (the owner, 2026-08-15): the note is what he reads, and a column
        # of "personal / business" between the date and the words pushed every
        # note off the right edge of the phone.
        head = ["| **Date** | **Note** | **Picture** | **Type** |", "|---|---|---|---|"]
        shown = [(d, t, f, []) for d, _k, t, f in both[:limit]]
        body = [f"| {d} | {_cell(t)} | {_files_cell(f, ios)} | {k} |"
                for d, k, t, f in both[:limit]]
        rows = both
    attached = sum(len(r[2]) for r in shown)
    return "\n".join(head + body) + _tail(len(body), len(rows), attached, ios,
                                            everything=(kind not in ("business",
                                                                     "personal")))


def send_files(chat_id, kind="all", limit=MAX_ROWS, viewer=None):
    """Telegram only: each attachment as its own message, captioned with its
    note's date and words — the reminders rule, for the same reason. An album
    carries ONE caption for the whole group, which would put the wrong words
    under the wrong picture. Through sendfile.py, so every file lands in the
    app's attachment feed and the chat archive. Returns how many were sent."""
    if kind == "business":
        rows = _business_rows()
    elif kind == "personal":
        rows = _personal_rows(viewer)
    else:
        rows = sorted(_business_rows() + _personal_rows(viewer),
                      key=lambda r: r[0], reverse=True)
    sent = 0
    for date, text, files, *_ in rows[:limit]:
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


# ------------------------------------------------------------ SEMANTIC SEARCH
# the owner, 2026-08-15: "there is no difference between the notes and knowledge
# base ... The private knowledge base should also be indexed for quick search.
# The personal knowledge base should be indexed too." Each tier has its own
# index under its own root (./nk personal|business) — separate stores, because
# a shared index with a filter is one bug away from answering a customer with a
# door code. Search returns the same table, narrowed to what matched.
ROOTS = {"personal": f"{C.WORKSPACE_ROOT}/personal",
         "business": f"{C.WORKSPACE_ROOT}/knowledge-base/private"}
SEARCH_ASK = re.compile(r"\b(search|find|look up|lookup|anything about|"
                        r"what do i have (on|about)|найди|поищи)\b", re.I)


# Every query returns its k nearest chunks whether or not any of them is about
# the question — "ipad password" scored 0.81 on the right note and 0.53, 0.51,
# 0.49 on a hotel, an order and a flight. Below this line a hit is just the
# least-distant thing in a small store, and printing it as a match is worse
# than printing nothing.
MIN_SCORE = 0.60


def _semantic(tier, query, k=8):
    """Note paths matching `query`, best first, weak neighbours dropped.

    In-process (kb_query), not `./nk` — measured 2026-08-15 after the owner said
    retrieval was too slow: the CLI spent 13ms embedding and 0.1ms on the
    maths, then ~120ms starting a python that could do it. Same engine, same
    index; the process spawn was the latency. 18ms warm.

    Empty on any failure — the caller then says nothing matched, never guesses.
    """
    try:
        import kb_query
        hits = kb_query.search(tier, query, k=k, min_score=MIN_SCORE)
    except Exception:
        return []
    root = ROOTS[tier]
    return [os.path.join(root, h["source"]) for h in hits if h.get("source")]


def search_rows(kind, query, limit=MAX_ROWS, viewer=None):
    """The listing rows whose note matched, best first.

    A row owns EVERY path filed with it — its text note and its attachments —
    so a hit maps back by path identity, not by comparing words. Rank comes
    from the index: the row holding the best-scoring file leads.

    The personal index is ONE index over a store with several creators, and
    that is safe here only because the rows are fetched per viewer first: a
    hit on someone else's note matches no row of theirs and is dropped. Any
    future caller that reads the index directly must apply that filter itself
    (2026-08-15: "personal notes should be accessible only to the User who
    created them")."""
    tiers = [kind] if kind in ("personal", "business") else ["personal", "business"]
    out = []
    for tier in tiers:
        if tier == "business":
            # The whole store is ONE file, so the index can only say "something
            # in business notes is relevant" — which was true of a hotel query
            # and dumped three door codes under it. The line has to match.
            out += [(d, t, [], []) for d, t in business_notes.search(query, limit)]
            continue
        ranked = [os.path.realpath(p) for p in _semantic(tier, query)]
        scored = []
        for row in _personal_rows(viewer):
            best = min((ranked.index(os.path.realpath(p))
                        for p in row[3] if os.path.realpath(p) in ranked),
                       default=None)
            if best is not None:
                scored.append((best, row))
        out += [r for _b, r in sorted(scored, key=lambda x: x[0])]
    return out[:limit]


LIST_ASK = re.compile(
    r"\b(show|list|output|display|print|give|read out|read me|what'?s in|"
    r"what is in|what do i have in|покажи|выведи|список|что в)\b", re.I)
NOTES = re.compile(r"\b(notes?|knowledge\s?bases?|kbs?|заметк\w+|баз\w+ знаний)\b", re.I)
BUSINESS = re.compile(r"\b(business|work|company|рабочи\w+|бизнес|деловы\w+)\b", re.I)
PERSONAL = re.compile(r"\b(personal|private|личны\w+|приватны\w+)\b", re.I)
EVERY = re.compile(r"\b(all|every|both|everything|все|всех|обе)\b", re.I)



KB_ROOT = os.path.join(C.WORKSPACE_ROOT, "knowledge-base")
# What each area IS, in his words rather than the directory's. A folder name is
# not an answer: "from-pdfs" tells him nothing about what is inside it.
KB_AREAS = [
    ("products", "Product sheets, price lists, photos"),
    ("from-pdfs", "Everything read out of PDFs — pages, text, pictures"),
    ("from-emails", "Facts and files extracted from mail"),
    ("from-scans", "Documents scanned with the phone"),
    ("company", "Company details, logos, letterhead"),
    ("faq", "Answers to questions customers repeat"),
    ("technical", "Technical notes"),
    ("uploads", "Files sent in and kept"),
    ("writing-styles", "How each of us writes"),
    ("private", "Private business notes"),
]
_IMG = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".bmp")
_DOC = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv")


def _count_area(path):
    """(documents, pictures, text) under a directory, at any depth."""
    doc = pic = txt = 0
    for root, _d, files in os.walk(path):
        for f in files:
            e = os.path.splitext(f)[1].lower()
            if e in _IMG:
                pic += 1
            elif e in _DOC:
                doc += 1
            elif e in (".md", ".txt", ".json", ".csv"):
                txt += 1
    return doc, pic, txt


def _kb_item_rows():
    """Every item in the knowledge base, newest first, as table rows.

    the owner, 2026-08-16: "I already instructed you to limit the output to just
    40 items. If a user wants to see the whole KB, you will output only 40
    items." So the whole KB is ITEMS, capped — not a summary of areas. He asked
    to see what is in it; a count of what is in it is a different answer.

    A file's date is when it was filed, its words are its first heading or its
    name, and a picture is its own thumbnail. The area it sits in is its type,
    so a row says where it came from without a second table.
    """
    rows = []
    for name, _what in KB_AREAS:
        base = os.path.join(KB_ROOT, name)
        if not os.path.isdir(base):
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                if f.startswith("."):
                    continue
                p = os.path.join(root, f)
                try:
                    ts = os.path.getmtime(p)
                except OSError:
                    continue
                ext = os.path.splitext(f)[1].lower()
                title = f
                if ext in (".md", ".txt"):
                    try:
                        with open(p, encoding="utf-8", errors="replace") as fh:
                            for line in fh:
                                line = line.strip().lstrip("#").strip()
                                if line:
                                    title = line
                                    break
                    except OSError:
                        pass
                rows.append((time.strftime("%Y-%m-%d", time.localtime(ts)),
                             title, [p] if ext in IMAGE_EXT else [], name, ts))
    return rows


def render_kb(client="telegram", viewer=None, limit=MAX_ROWS):
    """THE WHOLE KNOWLEDGE BASE — every store, newest first, capped at `limit`.

    2026-08-16: the first version answered with a table of AREAS and their
    counts. He had already set the rule — forty items — and a summary is not a
    listing. Every area is in here now, including the note stores, and the tail
    says how many were left behind rather than implying there were none.
    """
    ios = client == "ios"
    rows = _kb_item_rows()
    for d, t, f, _s in _personal_rows(viewer):
        rows.append((d, t, f, "personal", _epoch(d)))
    for d, t, f, _s in _business_rows():
        rows.append((d, t, f, "business", _epoch(d)))
    if not rows:
        return "The knowledge base is empty."
    rows.sort(key=lambda r: r[4], reverse=True)
    shown = rows[:limit]
    head = ["| **Date** | **Item** | **Picture** | **Type** |", "|---|---|---|---|"]
    body = [f"| {d} | {_cell(t)} | {_files_cell(f, ios)} | {k} |"
            for d, t, f, k, _ts in shown]
    attached = sum(len(r[2]) for r in shown)
    return "\n".join(head + body) + _tail(len(body), len(rows), attached, ios)


def _epoch(d):
    """A YYYY-MM-DD back to a sortable number; unknown dates sort last."""
    try:
        return time.mktime(time.strptime(d[:10], "%Y-%m-%d"))
    except (ValueError, TypeError):
        return 0.0



# A request that NAMES a document is not a request for a list. 2026-08-16:
# "show me my writing style doc" returned the notes table, because the intent
# model reads it as kb.list and the listing had no idea a name was in it.
_DOC_STOP = {"show", "me", "my", "mine", "our", "the", "a", "an", "please",
             "doc", "docs", "document", "documents", "file", "files", "open",
             "get", "give", "send", "pull", "up", "for", "of", "in", "to",
             "see", "view", "read", "display", "want", "i", "you", "it",
             "purchase", "knowledge", "base", "kb", "note", "notes",
             # Quantifiers name no document. "Show me ALL Maria's flights"
             # sent the lightbox installation guide, because "all" was the only
             # surviving word and it lives inside "instALLation" (2026-08-24).
             "all", "any", "every", "each", "both", "some", "list", "everything"}
# Words that say what KIND of document, not which one. Only these may be
# dropped when they match nothing — see the comment in find_doc.
_SHAPE = {"datasheet", "spec", "specs", "sheet", "manual", "guide", "brochure",
          "catalog", "catalogue", "pdf", "drawing", "diagram", "template",
          "invoice", "quote", "report", "letter", "photo", "picture", "image"}


def find_doc(question, viewer=None, root=None):
    """The one file this sentence names, or None.

    Scored on the WORDS, not on a keyword table: every token that is not
    scaffolding has to appear in the file's path, and the best match wins only
    if it is a clear winner. Two files tied means the sentence was ambiguous,
    and a listing is the honest answer to an ambiguous name.
    """
    t = " ".join((question or "").lower().split())
    words = [re.sub(r"['\u2019]s$", "", w.strip(",.?!;:'\"")) for w in t.split()]
    terms = [w for w in words if w and w not in _DOC_STOP and len(w) > 2]
    if not terms:
        return None
    # The word he dropped still says what SHAPE he wants. "Datasheet" matches
    # no filename here, but it does mean a document rather than a photograph of
    # the product — without this, "show me a meteor datasheet" answered with a
    # picture of the back panel (2026-08-16).
    wants_doc = any(w in ("doc", "docs", "document", "datasheet", "spec",
                          "specs", "sheet", "manual", "guide", "brochure",
                          "catalog", "catalogue", "pdf", "drawing", "diagram")
                    for w in words)
    mine = "my" in words or "mine" in words
    who = ""
    if mine:
        # Whose document "my" means. Read from the agent profile, never
        # written here: a name in the code is how the second owner's questions once came
        # back under the owner's.
        try:
            import sys as _sys
            _sys.path.insert(0, os.path.join(C.WORKSPACE_ROOT, "lib"))
            import agentprofile
            who = str((agentprofile.person("owner") or {}).get("name") or "").lower()
        except Exception:
            who = ""
    base = root or KB_ROOT
    corpus = []
    for dirpath, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.startswith("."):
                continue
            p = os.path.join(dirpath, f)
            corpus.append((p, f,
                           p[len(base):].lower().replace("-", " ")
                           .replace("_", " ")))
    # A WORD THAT MATCHES NOTHING CANNOT DISCRIMINATE, so it is dropped rather
    # than allowed to refuse the whole request. 2026-08-16: "show me a meteor
    # datasheet" found nothing, because no file in the knowledge base has the
    # word "datasheet" in its name — he was saying what KIND of document he
    # wanted, and the requirement that every word appear turned that into a
    # refusal. Words like "datasheet", "spec" or "manual" name a shape; the
    # word that identifies the thing is the one that matches something.
    # WORD BOUNDARIES, NOT SUBSTRINGS. "all" inside "installation" is not the
    # word "all", and matching it there is how a question about flights was
    # answered with a lightbox installation guide (2026-08-24). A term may
    # still match a longer word it STARTS — "flight" finds "flights" — because
    # that is the same word, not a different one that happens to contain it.
    def _hits(term, hay):
        return re.search(r"(?<![a-z0-9])" + re.escape(term), hay) is not None

    # A SHAPE word that matches nothing is dropped; an IDENTIFYING word that
    # matches nothing means the document is not here.
    #
    # This rule used to drop any unmatched word, which inverted its own
    # purpose: asked for "all Maria's flights", it threw away "maria's" and
    # "flights" — the two words that say what he wants — kept "all", and
    # matched that against every path containing those letters. Dropping the
    # specific words and keeping the generic one can only ever answer the
    # wrong question, so now an unmatched identifying word is a NO.
    unmatched = [t for t in terms if not any(_hits(t, hay) for _p, _f, hay in corpus)]
    if [t for t in unmatched if t not in _SHAPE]:
        return None
    terms = [t for t in terms if t not in unmatched] or terms
    required = [t for t in terms if t not in _SHAPE] or terms
    best, best_score, best_key = None, 0, (0, 0, 0)
    for p, f, hay in corpus:
        if True:
            if True:
                # Identifying words select the document; shape words only
                # describe it. "The lightbox installation GUIDE" must not be
                # refused because no file is named "guide" — but it must still
                # prefer one that is (2026-08-24).
                if not all(_hits(term, hay) for term in required):
                    continue
                score = sum(1 for term in terms if _hits(term, hay))
                # "my" prefers the file that carries the asker's own name, so
                # each owner's writing-style file wins for its own owner,
                # without either name appearing here.
                if who and who in hay:
                    score += 1
                # A learned/derived copy is not the document he means.
                if "learned" in hay or "index" in hay:
                    score -= 1
                ext = os.path.splitext(f)[1].lower()
                if wants_doc:
                    if ext in DOC_EXT or ext in (".md", ".txt"):
                        score += 1
                    elif ext in IMAGE_EXT:
                        score -= 1
                # A tie on words is broken by shape, not abandoned: the file
                # whose NAME is the thing asked for beats one that merely
                # mentions it in a folder, and a short path beats a deep one.
                exact = sum(1 for term in terms
                            if _hits(term, os.path.splitext(f)[0].lower()
                                     .replace("-", " ").replace("_", " ")))
                key = (score, exact, -len(p))
                if best is None or key > best_key:
                    best, best_key, best_score = p, key, score
                elif key == best_key:
                    best = None       # genuinely indistinguishable: not a name
    return best if best_score > 0 else None

def detect(text):
    """'business' | 'personal' | 'all' | None.

    WHETHER this is a listing request is decided by meaning (intent.classify),
    not by words (the owner, 2026-08-15: "We should not hard code some reaction to
    keywords. We should always understand the user request."). The patterns
    below survive for a different job: once the intent is known, they read
    WHICH knowledge base he named. That is parameter extraction, and a word
    like "business" there cannot start an action on its own."""
    t = " ".join((text or "").split())
    if not t or t.startswith("/"):
        return None
    try:
        import intent as _intent
        if _intent.classify(t)[0] != "kb.list":
            return None
    except Exception:
        # Understanding unavailable: do nothing rather than guess from words.
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


QUERY_AFTER = re.compile(r"\b(?:for|about|on|regarding|про|о|об)\s+(?P<q>.+)$", re.I)
STRIP = re.compile(r"\b(search|find|look ?up|in|my|our|the|personal|private|"
                   r"business|work|company|notes?|knowledge ?base|kb|please|"
                   r"anything|everything|найди|поищи|в|моих|мои|заметк\w+)\b", re.I)


# A REFLEX MUST NOT ANSWER A PARAGRAPH (2026-08-15). the owner wrote "Our knowledge
# base retrieval is too slow. We must optimize the speed. Is everything indexed?
# ... You can dry the airline tickets or hotel bookings search." — an
# instruction with work in it. It contained "knowledge base" and "search", so
# this reflex printed a table and his message was never read.
#
# The tell is shape, not vocabulary: a request to look something up is ONE short
# sentence that opens with the asking. An instruction is several sentences and
# arrives with its own subject. Both tests below are cheap and neither can be
# satisfied by an accident of wording.
MAX_ASK_CHARS = 120
SENTENCE_END = re.compile(r"[.!?](?:\s|$)")


def _is_a_request(text, ask_re):
    """One short sentence whose first words are the asking — or not ours."""
    t = " ".join((text or "").split())
    if len(t) > MAX_ASK_CHARS:
        return False
    if len(SENTENCE_END.findall(t)) > 1:      # more than one sentence: prose
        return False
    m = ask_re.search(t)
    if not m:
        return False
    # "search my notes for X" opens with the verb; "we should test the search"
    # buries it. Four words of grace covers "please", "can you", "hey".
    return len(t[:m.start()].split()) <= 4


def detect_search(text):
    """(kind, query) for "search my notes for X", or None."""
    t = " ".join((text or "").split())
    if not t or t.startswith("/"):
        return None
    try:
        import intent as _intent
        if _intent.classify(t)[0] != "kb.search":
            return None
    except Exception:
        return None
    biz, per = bool(BUSINESS.search(t)), bool(PERSONAL.search(t))
    kind = "business" if (biz and not per) else "personal" if (per and not biz) else "all"
    m = QUERY_AFTER.search(t)
    q = m.group("q") if m else STRIP.sub(" ", t)
    q = " ".join(q.split()).strip(" ?.,")
    return (kind, q) if len(q) >= 3 else None


def render_rows(rows, title, client="telegram"):
    ios = client == "ios"
    if not rows:
        return f"_{title}: nothing matched._"
    head = ["| **Date** | **Note** | **Picture** |", "|---|---|---|"]
    body = [f"| {d} | {_cell(t)} | {_files_cell(f, ios)} |" for d, t, f, _s in rows]
    return f"*{title}*\n\n" + "\n".join(head + body)


# "the whole/entire/complete knowledge base" — not one store, all of it.
# "all the knowledge base" is his own wording and has to be in here: it was the
# sentence that produced twelve notes and nothing else. Naming a store still
# wins — detect() resolves that first, and this only ever sees `all`.
WHOLE = re.compile(r"\b(entire|whole|complete|everything in|all of|all)\b"
                   r"[^.?!]{0,20}\b(kb|knowledge ?base)\b|"
                   r"\b(kb|knowledge ?base)\b[^.?!]{0,20}\b(entirely|in full)\b",
                   re.I)


def try_handle(chat_id, text, send, viewer=None):
    found = detect_search(text)
    if found:
        kind, q = found
        rows = search_rows(kind, q, viewer=viewer)
        send(chat_id, render_rows(rows, f"Knowledge base — “{q}”"))
        return f"notes table reflex: searched {kind} for {q!r}, {len(rows)} hit(s)"
    kind = detect(text)
    if not kind:
        return None
    # "the entire KB" names no single store and means all of it — the areas
    # table rather than the notes table (the owner, 2026-08-16: "You are showing
    # me only 2 business notes. I am asking about the entire KB.").
    # Not gated on `kind`: "show me the entire knowledge base" names no store,
    # and the default for a bare "knowledge base" is his personal one — which
    # is how asking for everything returned twelve personal notes. Naming a
    # store still wins, because those words are checked here first.
    named_store = bool(BUSINESS.search(text or "")) or \
        bool(PERSONAL.search(text or ""))
    if WHOLE.search(text or "") and not named_store:
        send(chat_id, render_kb(viewer=viewer))
        return "notes table reflex: whole-KB inventory"
    send(chat_id, render(kind, viewer=viewer))
    n = send_files(chat_id, kind, viewer=viewer)
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
