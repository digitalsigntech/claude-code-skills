"""Reminders reflex — "show me my reminders" answered without an LLM turn.

the owner, 2026-08-07: "when the user wants to see all reminders, present them as a
table; if a reminder has a photo attached there must be a thumbnail in the row,
and tapping it shows the photo full screen at full resolution."

Third of the same shape (tasks, backups, reminders): a question with a definite
answer should not cost a model turn. This one is a SELECT and, for rows with a
picture, one token mint per row.

THE PHOTO CELL is `![](vb-token:TOKEN)` — markdown image syntax with a file
token where a URL would go (the app developer's agent's shape). It survives any parser as an
image, cannot be mistaken for prose, and `GET /api/file/<token>` already speaks
that token, so nothing new is needed to fetch the bytes.

DURABILITY, since it was asked before it was promised: the token is derived
deterministically from the file's path and persisted, so it does not expire —
but it only resolves while the FILE is still there. Nothing prunes the camera
directory today. Rather than trust that, every row checks the file exists at
render time: a photo that has gone leaves an EMPTY cell, not a token that will
draw a broken-picture glyph on someone's phone. A promise is checked at the
moment it is made.
"""


import tgconf as C   # identity from config
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request

HOME = os.path.expanduser("~")
# A test must never be able to touch the live queue. Twice tonight I ran an
# amend test against real rows and cancelled the owner's reminders — #32 and
# then #34 — because the module simply pointed at the live database and my
# discipline was the only thing standing between a regex test and his data.
# Discipline lost both times, so it is an env var now: point REMINDERS_DB at a
# copy and every read AND write follows it.
DB = os.environ.get("REMINDERS_DB",
                    f"{C.WORKSPACE_ROOT}/operations/reminders/reminders.db")
REALTIME = f"{C.WORKSPACE_ROOT}/voice/realtime"
MINT_URL = "http://127.0.0.1:8478/{secret}/mint-token"
SENDFILE = f"{C.WORKSPACE_ROOT}/telegram/sendfile.py"
LIMIT = 20


def _mint(path):
    """File token from the RUNNING server — its token map lives there, and a
    second derivation here would be a second copy of the same secret rule."""
    try:
        secret = open(os.path.join(REALTIME, ".secret")).read().strip()
        bearer = open(os.path.join(REALTIME, ".hook_secret")).read().strip()
        rq = urllib.request.Request(
            MINT_URL.format(secret=secret),
            data=json.dumps({"path": os.path.abspath(path)}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {bearer}"})
        with urllib.request.urlopen(rq, timeout=5) as rp:
            return json.load(rp).get("token")
    except Exception:
        return None


def _when(when_local, epoch):
    """'tomorrow 10:00', 'Mon 09:15', 'Sat 10 Aug 09:15' — the shortest form
    that is still unambiguous. A bare date makes the reader do arithmetic."""
    try:
        t = time.localtime(epoch)
    except Exception:
        return str(when_local)
    now = time.localtime()
    hhmm = time.strftime("%H:%M", t)
    days = (int(epoch) // 86400) - (int(time.time()) // 86400)
    if days == 0:
        return f"today {hhmm}"
    if days == 1:
        return f"tomorrow {hhmm}"
    if 1 < days < 7:
        return f"{time.strftime('%a', t)} {hhmm}"
    if t.tm_year == now.tm_year:
        return f"{time.strftime('%-d %b', t)} {hhmm}"
    return f"{time.strftime('%-d %b %Y', t)} {hhmm}"


def _summarize(text):
    """reminders.summarize — imported, never re-implemented: two copies of a
    shortening rule drift, and then the list and the store disagree about what
    a reminder says."""
    try:
        import sys
        sys.path.insert(0, f"{C.WORKSPACE_ROOT}/operations/reminders")
        import reminders
        return reminders.summarize(text)
    except Exception:
        return text


def _cell(text):
    """No pipes, no newlines, nothing cut — a pipe ends the column early and
    shifts every later value under the wrong heading."""
    return " ".join(str(text or "").replace("|", "/").split())


# "per user" (the owner 2026-08-10, refined the same day): ONE list at a time.
# "show me MY reminders" is the asker's own; "show me OUR reminders" is the
# shared ones. Never a union — a table mixing his, hers and shared would need an
# owner column to be readable, and he does not want one. The title says whose,
# so the rows do not have to.
OURS = re.compile(r"\b(our|ours|shared|both of us|team)\b|"
                  r"\bнаш\w*\b|\bобщ\w+\b", re.I)


def _owner_sql(owner):
    """owner is now a single key, or None for unfiltered (CLI, cron)."""
    if not owner:
        return "", []
    return " AND COALESCE(owner,C.PRIMARY_OWNER_KEY) = ?", [owner]


def scope_of(text, viewer):
    """Which single list a sentence is asking for."""
    return "shared" if OURS.search(text or "") else viewer


def pending(limit=LIMIT, owner=None):
    osql, oargs = _owner_sql(owner)
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = c.execute(
            "SELECT id, when_local, when_epoch, "
            # #104 again (the owner): a reminder has two audiences and only one
            # of them reads this table. `label` is the human form; `text` is
            # whatever the agent needs at fire time, runbook and all. Three of
            # his seven rows were 583, 680 and 715 bytes of procedure written
            # to me — 70% of the table, none of it addressed to him.
            "COALESCE(NULLIF(TRIM(label), ''), text), photo FROM reminders "
            # #110: kind='delmsg' rows are HOUSEKEEPING — each one's "text" is
            # a Telegram message_id, queued by qr-login to delete an expired QR
            # picture. the owner saw one as a reminder reading "5756" and
            # the app developer's agent read it as a spoken reminder that had lost its words.
            # Nothing was lost: it never had any. They are not his reminders
            # and do not belong in a list of them.
            "WHERE status='pending' AND kind != 'delmsg'" + osql +
            " ORDER BY when_epoch LIMIT ?",
            (*oargs, limit)).fetchall()
    finally:
        c.close()
    out = []
    now = time.time()
    for rid, wl, we, text, photo in rows:
        # Rows written before labels existed still hold a runbook here. The
        # same summariser the writer now uses, applied on the way out.
        if len(text or "") > 120:
            text = _summarize(text)
        tok = None
        if photo and os.path.exists(photo):
            tok = _mint(photo)
        # #104 amended: the filter is DONE-versus-NOT-DONE, never time. An
        # overdue reminder is the most important row on the screen — its time
        # passed and nobody acted on it — so it stays, and says so.
        overdue = bool(we and we < now)
        when = _when(wl, we) + (", overdue" if overdue else "")
        out.append({"id": rid, "when": when, "epoch": we, "text": text,
                    "token": tok, "overdue": overdue, "state": "pending",
                    "photo": photo if (photo and os.path.exists(photo)) else None})
    return out


def completed(limit=LIMIT, owner=None):
    """Reachable, not default (#104 amended). "Did I already order those?" is
    a real question and the answer lives here — but a list of things already
    done costs one of the three or four rows a phone shows at once."""
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = c.execute(
            "SELECT id, when_local, when_epoch, "
            "COALESCE(NULLIF(TRIM(label), ''), text), photo, status FROM "
            "reminders WHERE status NOT IN ('pending') AND kind != 'delmsg'"
            + _owner_sql(owner)[0] +
            " ORDER BY when_epoch DESC LIMIT ?",
            (*_owner_sql(owner)[1], limit)).fetchall()
    finally:
        c.close()
    out = []
    for rid, wl, we, text, photo, status in rows:
        tok = _mint(photo) if (photo and os.path.exists(photo)) else None
        # #110: THREE states, three suffixes. A blank suffix used to mean both
        # "pending" and "already fired", so the reader had to know the time of
        # day to tell them apart — which is the work the table exists to save.
        suffix = {"cancelled": ", cancelled"}.get(status, ", done")
        out.append({"id": rid, "when": _when(wl, we) + suffix,
                    "state": "cancelled" if status == "cancelled" else "done",
            "epoch": we, "text": text, "token": tok, "status": status,
            "photo": photo if (photo and os.path.exists(photo)) else None})
    return out


def done_only(limit=LIMIT):
    """#111: "what have I finished" is a question about things that HAPPENED.
    A cancelled reminder never fired, so counting it as done answers the
    opposite of what was asked."""
    return [r for r in completed(limit * 2) if r.get("state") == "done"][:limit]


def everything(limit=LIMIT):
    """#111: "show me ALL reminders, even completed ones" ADDS to the default
    rather than replacing it. The word "even" is the tell — he was widening
    the set, and collapsing that into the completed-only view is the one
    reading that loses information instead of adding it.

    Order: pending first (soonest first — it is the actionable part), then
    done, then cancelled, both newest first."""
    p = pending(limit)
    rest = completed(limit * 2)
    done = [r for r in rest if r.get("state") == "done"]
    canc = [r for r in rest if r.get("state") == "cancelled"]
    return (p + done + canc)[:limit * 2]


def by_ids(ids):
    """#129: specific rows, whatever their state — for "here is the one you
    just made". Ordered as asked for, not by time: a single row has no order
    and a caller passing two means those two, in that order.

    Deliberately state-blind. A reminder created for a moment that has already
    passed is still the row to show back, and hiding it because it is not
    pending would answer a creation with an empty table."""
    ids = [int(i) for i in ids if str(i).strip().lstrip("-").isdigit()]
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = c.execute(
            "SELECT id, when_local, when_epoch, "
            "COALESCE(NULLIF(TRIM(label), ''), text), photo, status "
            f"FROM reminders WHERE id IN ({q})", ids).fetchall()
    finally:
        c.close()
    found = {}
    now = time.time()
    for rid, wl, we, text, photo, status in rows:
        if len(text or "") > 120:
            text = _summarize(text)
        tok = _mint(photo) if (photo and os.path.exists(photo)) else None
        if status == "pending":
            overdue = bool(we and we < now)
            when = _when(wl, we) + (", overdue" if overdue else "")
            state = "pending"
        else:
            overdue = False
            when = _when(wl, we) + ({"cancelled": ", cancelled"}
                                    .get(status, ", done"))
            state = "cancelled" if status == "cancelled" else "done"
        found[rid] = {"id": rid, "when": when, "epoch": we, "text": text,
                      "token": tok, "overdue": overdue, "state": state,
                      "photo": photo if (photo and os.path.exists(photo))
                      else None}
    return [found[i] for i in ids if i in found]


# ---------------------------------------------------------------- #135
# the owner asked "Show me ALL reminders FOR TODAY" and got the everything view:
# three pending, eleven done, eight cancelled, including a done row from two
# days earlier. His sentence carried both words and only the first one was read.
#
# "All reminders for today" is not the everything view — it is all of TODAY'S,
# and "all" there is doing no work beyond politeness. So A DATE QUALIFIER WINS
# OVER THE BREADTH WORD whenever both appear, and the breadth word decides only
# whether the day's CLOSED rows come too.
_TODAY = r"today|tonight|сегодня|segodnya"
_TOMORROW = r"tomorrow|завтра|zavtra"
_DAYPARTS = {"morning": (5, 12), "afternoon": (12, 17), "evening": (17, 24),
             "tonight": (17, 24)}
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday")
DATE_PHRASE = re.compile(
    rf"\b(?P<today>{_TODAY})\b|\b(?P<tomorrow>{_TOMORROW})\b|"
    r"\bthis\s+(?P<part>morning|afternoon|evening)\b|"
    r"\b(?:on\s+|next\s+|this\s+)?(?P<wday>" + "|".join(_WEEKDAYS) + r")\b",
    re.I)


def _midnight(ts):
    t = time.localtime(ts)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def date_window(text, now=None):
    """(start, end, label) for a date qualifier in `text`, or None.

    Deliberately small. It covers the phrasings someone actually says out loud
    about a day — today, tonight, this afternoon, tomorrow, Thursday, and the
    Russian for the first two — and returns None for everything else so the
    question reaches the model instead of being answered with a wrong window.
    """
    m = DATE_PHRASE.search(text or "")
    if not m:
        return None
    now = now or time.time()
    day0 = _midnight(now)
    if m.group("part") or (m.group("today") or "").lower() == "tonight":
        part = (m.group("part") or "tonight").lower()
        h0, h1 = _DAYPARTS[part]
        return (day0 + h0 * 3600, day0 + h1 * 3600,
                "tonight" if part == "tonight" else f"this {part}")
    if m.group("today"):
        return day0, day0 + 86400, "today"
    if m.group("tomorrow"):
        return day0 + 86400, day0 + 172800, "tomorrow"
    wday = m.group("wday").lower()
    # The NEXT one, today included: "what have I got on Friday" asked on Friday
    # means today, not a week away.
    delta = (_WEEKDAYS.index(wday) - time.localtime(now).tm_wday) % 7
    start = day0 + delta * 86400
    return start, start + 86400, wday.capitalize()


def window(start, end, include_closed=False, limit=LIMIT * 2):
    """Reminders whose time falls inside [start, end)."""
    states = ("", ) if include_closed else ("AND status='pending' ", )
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = c.execute(
            "SELECT id, when_local, when_epoch, "
            "COALESCE(NULLIF(TRIM(label), ''), text), photo, status "
            "FROM reminders WHERE kind != 'delmsg' "
            f"AND when_epoch >= ? AND when_epoch < ? {states[0]}"
            "ORDER BY when_epoch LIMIT ?",
            (int(start), int(end), limit)).fetchall()
    finally:
        c.close()
    out = []
    now = time.time()
    for rid, wl, we, text, photo, status in rows:
        if len(text or "") > 120:
            text = _summarize(text)
        tok = _mint(photo) if (photo and os.path.exists(photo)) else None
        if status == "pending":
            overdue = bool(we and we < now)
            when = _when(wl, we) + (", overdue" if overdue else "")
            state = "pending"
        else:
            overdue = False
            when = _when(wl, we) + ({"cancelled": ", cancelled"}
                                    .get(status, ", done"))
            state = "cancelled" if status == "cancelled" else "done"
        out.append({"id": rid, "when": when, "epoch": we, "text": text,
                    "token": tok, "overdue": overdue, "state": state,
                    "photo": photo if (photo and os.path.exists(photo))
                    else None})
    return out


# The neighbours the app developer's agent listed — "what is due today", "anything today", "what
# have I got this afternoon", "zavtra" — carry a date and no reminder noun, so
# the ordinary detectors refuse them. This is the question they all are.
DUE_ISH = re.compile(r"\b(due|scheduled|planned|booked|on my plate|coming up|"
                     r"agenda|got|have i got|do i have|anything|what'?s on|"
                     r"what have i|what do i have)\b", re.I)
# ...and the topics that are NOT this question, however dated they are. Without
# these, "what is the weather today" and "what did I email him today" would
# both be answered with a table of reminders.
OTHER_TOPIC = re.compile(r"\b(weather|e-?mails?|inbox|mails?|news|prices?|"
                         r"quotes?|invoices?|orders?|backups?|tasks?|"
                         r"meetings?|calendars?|temperature|forecast|traffic)\b",
                         re.I)


def detect_dated(text):
    """Is this a question about what is on a particular day?"""
    t = (text or "").strip()
    if not t or len(t) > 120 or "\n" in t or t.startswith("/"):
        return False
    if CREATE.search(t) or OTHER_TOPIC.search(t):
        return False
    if not date_window(t):
        return False
    # A reminder noun settles it. Otherwise the sentence has to be ASKING —
    # "tomorrow" on its own is a question about the day; "I'll do it tomorrow"
    # is not, and neither is anything with a topic of its own above.
    if NOUN.search(t):
        return True
    return bool(DUE_ISH.search(t) or LIST_ISH.search(t)
                or len(t.split()) <= 2)


def dated(text, client="telegram"):
    """The rendered table for a date question, or None if there is no date in
    it. `all`/`even completed` widens this to the day's closed rows too — the
    breadth word still means something, it just no longer replaces the day."""
    win = date_window(text)
    if not win:
        return None
    start, end, label = win
    rows = window(start, end, include_closed=bool(ALL_ISH.search(text or "")))
    return render(rows, title=f"Reminders — {label}",
                  empty=f"Nothing {label}." if label in ("today", "tonight")
                  else f"Nothing for {label}.",
                  noun="pending", client=client)


def _counts(rows):
    """Per-state counts, because a single number over three states makes the
    reader open the table to find out what it is a number OF."""
    order = ("pending", "done", "cancelled")
    n = {k: sum(1 for r in rows if r.get("state") == k) for k in order}
    parts = [f"{n[k]} {k}" for k in order if n[k]]
    return " · ".join(parts) or f"{len(rows)}"


def render(rows=None, title="Reminders", empty="No reminders set.",
           noun="pending", client="telegram"):
    """The two clients get DIFFERENT tables, because they are different media.

    THE APP (client="ios"): When | Reminder | Photo, with the photo cell
    carrying ![](vb-token:TOKEN). It renders markdown natively, draws the
    thumbnail inline and opens it full screen on a tap. This is the layout
    the owner asked for and the one he lost when I made both clients share a
    shape — three narrow text columns truncating every reminder, and no
    thumbnails at all.

    TELEGRAM (client="telegram"): When | Reminder, and each photo follows as
    its own captioned message. Tested live against the Bot API: markdown image
    syntax inside a sendRichMessage table cell is SILENTLY DROPPED, so a Photo
    column there is a strip of blanks. One message per photo, never an album —
    an album carries a single caption for the whole group, which would put the
    wrong words under the wrong picture.

    THE ID IS INVISIBLE (#106). the app developer's agent needs it to name a row on
    edit/delete; the owner does not need to read it. It rides as an HTML
    comment inside the When cell — no column, no width, nothing on screen,
    and a two-character regex on his side.
    """
    rows = pending() if rows is None else rows
    if not rows:
        return empty
    ios = client == "ios"
    # Bold header cells (the owner 2026-08-13: he wanted the header row to read
    # differently from the body; Telegram draws the cell backgrounds itself and
    # exposes no styling, so weight is the only lever we have — and he approved
    # it on sight).
    head = (["| **When** | **Reminder** | **Photo** |", "|---|---|---|"] if ios
            else ["| **When** | **Reminder** |", "|---|---|"])
    out = list(head)
    for r in rows:
        # The comment goes to the APP only. Telegram has no HTML comments in
        # rich messages — it would render the marker as literal text, which is
        # the same class of mistake as the vb-token cell it silently dropped.
        when = _cell(r["when"]) + (f"<!--id:{r['id']}-->" if ios else "")
        cells = [when, _cell(r["text"])]
        if ios:
            cells.append(f"![](vb-token:{r['token']})" if r.get("token") else "")
        out.append("| " + " | ".join(cells) + " |")
    late = sum(1 for r in rows if r.get("overdue"))
    # #111: name each state. "20 done" over a set whose first row was
    # cancelled was wrong twice — the number and the word.
    count = _counts(rows) + (f", {late} overdue" if late else "")
    tail = ""
    if not ios:
        photos = sum(1 for r in rows if r.get("photo"))
        if photos:
            tail = (f"\n\n_{photos} of these "
                    f"{'has' if photos == 1 else 'have'} a photo — sending below._")
    return (f"*{title}* ({count})\n\n" + "\n".join(out) + tail)


def send_photos(chat_id, rows):
    """One message per photo, captioned with that reminder's own time and text.

    Goes through sendfile.py rather than the Bot API directly, so each photo
    lands in the app's attachment feed and the chat archive like every other
    file we send. Returns the number sent."""
    sent = 0
    for r in rows:
        path = r.get("photo")
        if not path:
            continue
        caption = f"{r['when']} — {r['text']}"[:1000]
        try:
            out = subprocess.run(
                [sys.executable, SENDFILE, str(chat_id), path, caption],
                capture_output=True, text=True, timeout=60)
            if "sent" in (out.stdout or "").lower():
                sent += 1
        except Exception:
            pass          # a photo that fails to send must not lose the table
    return sent


# "show me my reminders", "what reminders do I have", "list reminders", "any
# reminders?". Must NOT fire on "remind me to ..." — that is a request to
# CREATE one, and answering it with a list would swallow the instruction.
NOUN = re.compile(r"\breminders?\b", re.I)
# Russian too: he asks in both languages, and "покажи мои напоминания" used to
# fall through to the model while its English twin was instant. The date and
# state words already had Russian; the LIST VERB was the gap.
LIST_ISH = re.compile(r"\b(show|list|what|which|any|all|see|view|display|"
                      r"upcoming|pending|do i have|have i got)\b|"
                      r"\bпокажи\b|\bпоказать\b|\bсписок\b|\bкакие\b|"
                      r"\bчто\b|\bесть ли\b|\bвыведи\b", re.I)
CREATE = re.compile(r"\b(remind me|remind us|set a reminder|add a reminder|"
                    r"create a reminder|cancel|delete|remove|snooze|"
                    r"how do|how to|why)\b", re.I)


# Deliberately narrow. "Did I already order those filters?" is the question
# the app developer's agent cited, and it is NOT matched here on purpose: it never says
# "reminder", and the honest answer needs the agent searching mail and the CRM,
# not a list of reminder rows. A reflex that swallowed it would answer a
# purchasing question with a to-do list.
DONE_ISH = re.compile(r"\b(completed|finished|done|past|previous|history|"
                      r"already)\b", re.I)


# "show me ALL reminders", "even completed", "including done", "everything".
ALL_ISH = re.compile(r"\b(all|every|everything|even (the )?(completed|done|"
                     r"finished)|includ\w*\s+(the\s+)?(completed|done|"
                     r"finished|cancelled))\b", re.I)


def detect_all(text):
    t = (text or "").strip()
    if not t or len(t) > 120 or "\n" in t or t.startswith("/"):
        return False
    if CREATE.search(t):
        return False
    return bool(NOUN.search(t)) and bool(ALL_ISH.search(t))


def detect_done(text):
    t = (text or "").strip()
    if not t or len(t) > 120 or "\n" in t or t.startswith("/"):
        return False
    if CREATE.search(t):
        return False
    return bool(NOUN.search(t)) and bool(DONE_ISH.search(t))


def detect(text):
    t = (text or "").strip()
    if not t or len(t) > 120 or "\n" in t or t.startswith("/"):
        return False
    if CREATE.search(t):
        return False
    return bool(NOUN.search(t)) and bool(LIST_ISH.search(t))


def after_amend(rid, owner=None):
    """#114: the table to append after an amendment. The pending list when the
    row is in it; otherwise a ONE-ROW table of that row, because the sheet
    rewrites itself from whatever table comes back and a done row has no
    business in the pending list."""
    try:
        rows = pending(owner=owner)
        if any(r["id"] == rid for r in rows):
            return render(rows, title=_whose(owner), client="ios")
        one = [r for r in completed(200, owner=owner) if r["id"] == rid]
        if one:
            return render(one, title="Reminder", noun="row", client="ios")
    except Exception:
        pass
    return ""


# ============================================================== #139
# ONE SELECTOR, replacing three special cases.
#
# the owner, twice in one day: "Show me all reminders for today" gave him the
# everything view, and so did "Can you show me all PENDING reminders?" — 2
# pending among 12 done and 8 cancelled. #135 fixed the first by special-casing
# dates, which was the wrong shape and left the second broken. the app developer's agent's
# diagnosis is the right one: ANY qualifier was being dropped in favour of the
# breadth word, because three branches each read a different word and whichever
# matched first won.
#
# His rule, verbatim: "ONLY SHOW WHAT YOU WERE ASKED."
#
#   a STATE word  (pending / done / cancelled / overdue)   -> that is the filter
#   a DATE word   (today / tomorrow / this week / Friday)  -> that is the filter
#   "all"         -> POLITENESS, unless it is the only qualifier in the sentence
STATE_WORDS = (
    ("pending", r"pending|outstanding|open|active|upcoming|unfinished|"
                r"not done|still to do|to-?do|"
                r"невыполненн\w*|ожидающ\w*|активн\w*|предстоящ\w*|"
                r"незавершённ\w*|незавершенн\w*"),
    ("done", r"completed|finished|done|closed|"
             r"выполненн\w*|завершённ\w*|завершенн\w*|сделанн\w*|готов\w*"),
    ("cancelled", r"cancell?ed|dropped|abandoned|"
                  r"отменённ\w*|отмененн\w*|отменён\w*|отменен\w*"),
)
OVERDUE_WORDS = re.compile(r"\b(overdue|late|missed|past due|"
                           r"просроченн\w*|опоздавш\w*|пропущенн\w*)\b", re.I)
WEEK_WORDS = re.compile(r"\bthis week\b|\bthe week\b|"
                        r"на этой неделе|эт[ао][йя] недел\w*", re.I)
RU_NOUN = re.compile(r"напоминани\w*", re.I)
# "все"/"всё" is the Russian politeness word. Without it, "покажи ВСЕ
# напоминания на сегодня" returned today's pending only while its English twin
# returned every state.
RU_ALL = re.compile(r"\bвс[её]\b|\bвсех\b|\bвсе[хм]?\b", re.I)
# "even the completed ones" / "including done" WIDENS — it adds a state to the
# default rather than replacing it (#111). "Show me completed reminders" names
# the filter. Same words, opposite jobs, and the difference is the "even".
WIDENING = re.compile(r"\b(even|including|includes?|as well as|plus|and also)\b"
                      r"[^.]{0,20}\b(completed|done|finished|cancell?ed|closed)\b|"
                      r"даже\s+\w*|включая\s+\w*", re.I)


def _states_in(text):
    found = []
    for name, pat in STATE_WORDS:
        if re.search(rf"\b({pat})\b", text or "", re.I):
            found.append(name)
    return found


def interpret(text, now=None):
    """What was actually asked for, or None if this is not that question.

    Returns {states, overdue, window, label}. Nothing in here widens a request
    the user made narrow — that was the whole bug.
    """
    t = " ".join((text or "").split())
    if not t or "\n" in t or t.startswith("/"):
        return None
    # #117 (the owner): "when the voice app asks you the same, return the proper
    # table to it". The app does not forward what he SAID — it forwards a
    # rewritten instruction, and "show me the reminders for this week" arrived
    # as 141 characters of "…including their times and titles. If possible,
    # include whether any are already done or cancelled." One character over the
    # cap, so the reflex declined and the model answered in spoken prose — right
    # content, wrong shape, and the app's sheet redraws from a table.
    #
    # The QUESTION is the first sentence; the rest is elaboration the caller
    # added. Read that, exactly as amend() does, rather than raising a cap that
    # exists to keep paragraphs of unrelated text from looking like a query.
    if len(t) > 140:
        t = re.split(r"(?<=[.!?])\s", t, 1)[0]
        if not t or len(t) > 140:
            return None
    if CREATE.search(t) or OTHER_TOPIC.search(t):
        return None
    now = now or time.time()

    states = _states_in(t)
    if states and WIDENING.search(t):
        # Named as an ADDITION, not as the filter. And when he ALSO said a bare
        # "all", the named state is an example of what he meant by it rather
        # than a narrowing — which is what #111 shipped and he accepted.
        #
        # A BARE breadth word, not ALL_ISH: ALL_ISH itself matches "including
        # done", so testing it here made the widening phrase its own excuse to
        # widen further, and "reminders including done" quietly returned
        # cancelled rows nobody mentioned.
        if re.search(r"\b(all|every|everything)\b", t, re.I) or RU_ALL.search(t):
            states = ["pending", "done", "cancelled"]
        else:
            states = sorted(set(states) | {"pending"},
                            key=["pending", "done", "cancelled"].index)
    overdue = bool(OVERDUE_WORDS.search(t))
    win = date_window(t, now)
    if not win and WEEK_WORDS.search(t):
        day0 = _midnight(now)
        # The week AHEAD, not the calendar week: "what have I got this week"
        # asked on Friday is about Friday to Sunday, and a Monday-anchored
        # window would answer with four days that have already happened.
        win = (day0, day0 + 7 * 86400, "this week")

    named = bool(states or overdue or win)
    # Is this a reminders question at all? A qualifier alone is not enough —
    # "cancelled" appears in plenty of sentences that are not this one.
    if not (NOUN.search(t) or RU_NOUN.search(t)):
        if not named:
            return None
        if not (LIST_ISH.search(t) or DUE_ISH.search(t) or len(t.split()) <= 2):
            return None

    if not named:
        if ALL_ISH.search(t) or RU_ALL.search(t):
            return {"states": ["pending", "done", "cancelled"],
                    "overdue": False, "window": None, "label": "everything"}
        if not (LIST_ISH.search(t) or DUE_ISH.search(t)):
            return None
        return {"states": ["pending"], "overdue": False, "window": None,
                "label": ""}

    if not states:
        # A date with no state named: a bare "all" decides whether the day's
        # closed rows come too. Overdue is inherently a pending condition.
        states = (["pending", "done", "cancelled"]
                  if ((ALL_ISH.search(t) or RU_ALL.search(t)) and not overdue)
                  else ["pending"])
    bits = []
    if overdue:
        bits.append("overdue")
    elif len(states) == 1 and states[0] != "pending":
        bits.append(states[0])
    elif len(states) > 1 and not win:
        bits.append(" and ".join(states))
    elif states == ["pending"] and not win:
        bits.append("pending")
    if win:
        bits.append(win[2])
    return {"states": states, "overdue": overdue, "window": win,
            "label": " · ".join(bits)}


def rows_for(spec, limit=LIMIT * 2, owner=None):
    """The rows a spec names. ONE query, so the table cannot disagree with the
    sentence that asked for it."""
    where = ["kind != 'delmsg'"]
    args = []
    osql, oargs = _owner_sql(owner)
    if osql:
        where.append(osql.replace(" AND ", "", 1))
        args += oargs
    where.append("status IN (%s)" % ",".join("?" * len(spec["states"])))
    args += spec["states"]
    if spec["window"]:
        where.append("when_epoch >= ? AND when_epoch < ?")
        args += [int(spec["window"][0]), int(spec["window"][1])]
    if spec["overdue"]:
        where.append("when_epoch < ?")
        args.append(int(time.time()))
    order = ("when_epoch" if (spec["window"] or "pending" in spec["states"])
             else "when_epoch DESC")
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = c.execute(
            "SELECT id, when_local, when_epoch, "
            "COALESCE(NULLIF(TRIM(label), ''), text), photo, status "
            f"FROM reminders WHERE {' AND '.join(where)} "
            f"ORDER BY {order} LIMIT ?", args + [limit]).fetchall()
    finally:
        c.close()
    out = []
    now = time.time()
    for rid, wl, we, text, photo, status in rows:
        if len(text or "") > 120:
            text = _summarize(text)
        tok = _mint(photo) if (photo and os.path.exists(photo)) else None
        if status == "pending":
            od = bool(we and we < now)
            when = _when(wl, we) + (", overdue" if od else "")
        else:
            od = False
            when = _when(wl, we) + ({"cancelled": ", cancelled"}
                                    .get(status, ", done"))
        out.append({"id": rid, "when": when, "epoch": we, "text": text,
                    "token": tok, "overdue": od,
                    "state": ("cancelled" if status == "cancelled"
                              else ("done" if status != "pending"
                                    else "pending")),
                    "photo": (photo if (photo and os.path.exists(photo))
                              else None)})
    return out


def _whose(owner):
    """'Shared reminders' / 'the owner's reminders' — the heading that makes an
    owner column unnecessary."""
    if not owner:
        return "Reminders"
    if owner == "shared":
        return "Shared reminders"
    return owner.capitalize() + "'s reminders"


def answer(text, client="telegram", owner=None):
    """(rendered_table, rows), or (None, []) when this is not that question.

    owner is the VIEWER. The sentence decides whether they are asking for their
    own list or the shared one; the two are never merged.
    """
    spec = interpret(text)
    if not spec:
        return None, []
    scope = scope_of(text, owner)
    rows = rows_for(spec, owner=scope)
    label = spec["label"]
    return render(rows, title=_whose(scope) + (f" — {label}" if label else ""),
                  empty=(f"No {label} reminders." if label
                         else "No reminders set."),
                  noun=(spec["states"][0] if len(spec["states"]) == 1
                        else "matching"), client=client), rows


def try_handle(chat_id, text, send, owner=None):
    """#139: ONE reading of the sentence. The three special-cased branches that
    used to live here — all / done / pending — each answered a different part of
    it, and whichever matched first won, which is how "all PENDING reminders"
    became the everything view."""
    table, rows = answer(text, client="telegram", owner=owner)
    if table is None:
        return None
    send(chat_id, table)
    n = send_photos(chat_id, rows)
    return f"reminders reflex: {len(rows)} row(s), {n} photo(s)"


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:])
    if q:
        print(f"detect({q!r}) = {detect(q)}")
    t0 = time.time()
    print(render())
    print(f"\n({(time.time() - t0) * 1000:.0f}ms)")


# ---------------------------------------------------------------- #109 -----
# AMENDING a reminder, with no model turn. Listing was already instant; the
# CHANGE is the moment the user is watching a sheet and waiting for it to
# update, so it is the one that should not cost a round trip.
#
# WHAT THIS WILL DO, and nothing else:
#   delete/cancel reminder id N
#   move it to an explicit time — today/tomorrow/weekday/date + clock time
#   replace the wording literally — "change the text to X", "rename it to X"
#
# WHAT IT DELIBERATELY WILL NOT DO. the app developer's agent's own example is the reason:
# "The change: make it 25." means "Order more filters" becomes "Order 25 more
# filters" — that is reasoning about the sentence, not editing it, and a regex
# that tried would produce a reminder saying something nobody asked for. Same
# for "an hour later" and "move it earlier": computable, but only if I am sure
# which anchor is meant. Those fall through to the model exactly as now, which
# costs a turn and gets it right. THE VERB HERE IS SOMETIMES `delete`, so the
# bar for acting without reasoning is "the sentence says the id and says the
# operation", not "I can probably work out what was meant".
_ID = re.compile(r"\breminder\s+(?:id\s*)?#?(\d+)\b", re.I)
_DELETE = re.compile(r"\b(delete|cancel|remove|drop)\b", re.I)
# The value ends at the first sentence end, not at end-of-string: the buried
# format puts "Confirm in one short sentence." after the clause, and anchoring
# to $ made a perfectly clear instruction unmatchable.
_RELABEL = re.compile(
    r"\b(?:change|set|replace|make)\s+(?:the\s+)?(?:text|label|wording|title)"
    r"\s*:?\s*(?:to|as)\s*:?\s*[\"“']?(.+?)[\"”']?(?:\.\s|\.$|$)"
    r"|\brename\s+it\s+to\s*:?\s*[\"“']?(.+?)[\"”']?(?:\.\s|\.$|$)", re.I)
_CLOCK = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\b", re.I)
_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
         "sunday")
# Anything in here means the time is relative or fuzzy: hand it to the model.
# #113: "seven in the morning" is EXACT — the day-part disambiguates the hour,
# it does not blur it. Only genuinely open-ended words stay fuzzy.
_FUZZY = re.compile(r"\b(later|earlier|in\s+\w+\s+(hour|minute|day)|next week|"
                    r"sometime|around|about|asap|soon|whenever)\b", re.I)
_WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
             "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
             "twelve": 12, "midnight": 0, "noon": 12}
_WORD_MIN = {"o'clock": 0, "oclock": 0, "fifteen": 15, "thirty": 30,
             "forty-five": 45, "forty five": 45, "quarter": 15, "half": 30}


def _spoken_to_clock(phrase):
    """'seven in the morning' -> '7:00 am'; 'half past nine' -> '9:30';
    'quarter to eight' -> '7:45'. The app sends what was SPOKEN, so the
    numerals often are not there."""
    p = " " + phrase.lower() + " "
    for w, n in _WORD_MIN.items():
        # "half past nine", "quarter past/to eight"
        m = re.search(rf"\b{re.escape(w)}\s+(past|to)\s+(\w+)\b", p)
        if m and m.group(2) in _WORD_NUM:
            hh = _WORD_NUM[m.group(2)]
            mins = n if m.group(1) == "past" else -n
            if mins < 0:
                hh, mins = (hh - 1) % 24, 60 + mins
            return re.sub(rf"\b{re.escape(w)}\s+(past|to)\s+\w+\b",
                          f" {hh}:{mins:02d} ", p, count=1)
    for w, n in _WORD_NUM.items():
        m = re.search(rf"\b(?:at\s+)?{w}\b(\s+(thirty|fifteen|forty[- ]five|"
                      rf"o'?clock))?", p)
        if m:
            mins = _WORD_MIN.get((m.group(2) or "").strip(), 0)
            return p[:m.start()] + f" {n}:{mins:02d} " + p[m.end():]
    return phrase


def _daypart_ampm(phrase):
    low = phrase.lower()
    if re.search(r"\bin the morning\b|\bthis morning\b|\bam\b", low):
        return "am"
    if re.search(r"\bin the (afternoon|evening)\b|\btonight\b|\bat night\b"
                 r"|\bpm\b", low):
        return "pm"
    return ""


_MONTHS = {m: i + 1 for i, m in enumerate(
    ("january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"))}
_MONTHS.update({m[:3]: i + 1 for m, i in
                [(k, v - 1) for k, v in list(_MONTHS.items())]})


def _find_date(phrase, now):
    """(date_tuple, phrase_without_the_date) or (None, phrase).

    The date is REMOVED from the phrase before any clock parsing, because
    "August 20" left "20" behind and my first version read it as 20:00 —
    silently moving a reminder to eight in the evening today. A wrong time is
    worse than no match: no match costs a model turn, a wrong one costs him
    the reminder.
    """
    low = " " + phrase.lower() + " "
    # 2026-08-20
    m = re.search(r"\b(20\d\d)-(\d{1,2})-(\d{1,2})\b", low)
    if m:
        return ((int(m.group(1)), int(m.group(2)), int(m.group(3))),
                low.replace(m.group(0), " "))
    # "20 August", "20th of August", "August 20", "Aug 20th"
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]{3,9})\b", low)
    if m and m.group(2) in _MONTHS:
        return ((now.tm_year, _MONTHS[m.group(2)], int(m.group(1))),
                low.replace(m.group(0), " "))
    m = re.search(r"\b([a-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?\b", low)
    if m and m.group(1) in _MONTHS:
        return ((now.tm_year, _MONTHS[m.group(1)], int(m.group(2))),
                low.replace(m.group(0), " "))
    if "today" in low:
        return ((now.tm_year, now.tm_mon, now.tm_mday), low.replace("today", " "))
    if "tomorrow" in low:
        t = time.localtime(time.mktime(now) + 86400)
        return ((t.tm_year, t.tm_mon, t.tm_mday),
                low.replace("tomorrow", " "))
    for i, d in enumerate(_DAYS):
        m = re.search(rf"\b(next\s+)?({d}|{d[:3]})\b", low)
        if m:
            ahead = (i - now.tm_wday) % 7
            ahead = ahead or 7                    # "Tuesday" on a Tuesday = next
            if m.group(1):                        # "next Friday" — the one after
                ahead = ahead if ahead >= 3 else ahead + 7
            t = time.localtime(time.mktime(now) + ahead * 86400)
            return ((t.tm_year, t.tm_mon, t.tm_mday), low.replace(m.group(0), " "))
    return None, phrase


def _parse_when(phrase, base=None, keep_epoch=None):
    """An explicit local time from a phrase, or None.

    None is the safe answer: it means the model handles it, which costs a turn
    and cannot be wrong. #114: a DATE with no clock time is not ambiguous — it
    means the same time on a different day — so the row's own hour is kept
    rather than refusing or guessing.
    """
    if not phrase or _FUZZY.search(phrase):
        return None
    now = time.localtime(base or time.time())
    date, rest = _find_date(phrase, now)
    part = _daypart_ampm(rest)
    rest = _spoken_to_clock(rest)
    m = _CLOCK.search(rest)
    hh = mm = None
    if m:
        hh, mm = int(m.group(1)), int(m.group(2) or 0)
        ampm = ((m.group(3) or part) or "").lower().replace(".", "")
        if ampm.startswith("p") and hh < 12:
            hh += 12
        if ampm.startswith("a") and hh == 12:
            hh = 0
        if not ampm and hh <= 7:      # "at 3" on a work reminder is a guess
            return None
        if hh > 23 or mm > 59:
            return None
    if hh is None:
        if date is None:
            return None               # neither a day nor a time: nothing said
        if keep_epoch:                # a new day, the same hour
            k = time.localtime(keep_epoch)
            hh, mm = k.tm_hour, k.tm_min
        else:
            return None
    if date is None:
        # #116, the mirror of #114: a TIME with no day, on a row that already
        # has a day, means the same day at a different hour. "Move the William
        # one to 9am" on a Thursday reminder is Thursday 09:00 — anchoring it to
        # today instead made it a past time and refused a clear instruction.
        anchor = time.localtime(keep_epoch) if keep_epoch else now
        cand = time.struct_time((anchor.tm_year, anchor.tm_mon, anchor.tm_mday,
                                 hh, mm, 0, 0, 0, -1))
        if time.mktime(cand) <= time.time():
            return None               # a bare past time: which day is a guess
        date = (anchor.tm_year, anchor.tm_mon, anchor.tm_mday)
    return f"{date[0]:04d}-{date[1]:02d}-{date[2]:02d} {hh:02d}:{mm:02d}"


# ------------------------------------------------------------ TARGET RESOLUTION
# #116 (the owner, on a call): "changing a reminder takes about a minute". The
# database write is a millisecond; the minute was the model turn, and the turn
# happened because _ID demands the literal words "reminder 34". Nobody speaks
# that. He says "change this one to order three boards" and "change this one
# from 8 a.m. to 10 a.m." — both perfectly determinate, both previously falling
# through.
#
# Every rule below resolves to EXACTLY ONE row or gives up. Two candidates is
# not a coin flip, it is a question for the model: the verb here is sometimes
# `cancel`, and a fast wrong answer costs more than a slow right one.
_PRONOUN = re.compile(r"\b(this|that|the)\s+(one|reminder)\b|\bit\b", re.I)
# "from 8 a.m. to 10 a.m." — the FIRST time names the row, the second is the new
# value. Without this split, _parse_when grabs whichever it sees first and
# "moves" the reminder to the time it already has.
_FROM_TO = re.compile(
    r"\bfrom\s+(?P<sel>\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?)\s+"
    r"to\s+(?P<new>\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?)\b", re.I)
# Any determiner, not just "the": he says "move OUR crate one" and "my 8 a.m.
# one". Missing those left the selector words in the instruction, where
# _spoken_to_clock read the "one" of "crate one" as 1 o'clock and the whole
# amendment failed.
_THE_X_ONE = re.compile(
    r"\b(?:the|our|my|your|his|her|that|this)\s+(?P<what>[\w:.' -]{2,30}?)"
    r"\s+one\b", re.I)
_STOP = {"the", "a", "an", "to", "of", "for", "and", "or", "my", "our", "this",
         "that", "it", "one", "ones", "change", "set", "make", "move", "edit",
         "update", "please", "reminder", "reminders", "am", "pm", "at", "on",
         "in", "from", "with", "order", "buy", "get", "more", "some"}


def _all_rows(owner=None):
    """Pending first — an amendment is overwhelmingly about a live reminder,
    and preferring them keeps a stale done row from stealing a keyword.

    Scoped to the viewer: you cannot reschedule, rename or cancel a reminder
    you are not allowed to see. Resolution silently skipping the other owner's
    rows is the point — it must not even be a candidate.
    """
    return pending(LIMIT * 2, owner=owner) + completed(LIMIT * 2, owner=owner)


def _hhmm(row):
    if not row.get("epoch"):
        return None
    t = time.localtime(row["epoch"])
    return t.tm_hour, t.tm_min


def _only(matches):
    """One match resolves; zero or many do not."""
    return matches[0] if len(matches) == 1 else None


def _pick(rows, keep):
    """Live rows get first refusal, then the whole set — one match or nothing.

    Every selector needs this. "from 10 am to 11 am" was ambiguous only because
    a reminder completed two days ago had also been at 10:00; the pending one is
    obviously the one being rescheduled."""
    return _only([r for r in rows if r.get("state") == "pending" and keep(r)]) \
        or _only([r for r in rows if keep(r)])


def _by_clock(rows, phrase):
    """'the 8 a.m. one', 'from 10 am to ...' — match on the hour we hold.

    Deliberately NOT _parse_when: that function answers "when should this fire",
    so it refuses a time earlier today as unschedulable. Here the time is a
    NAME, not a schedule — the 10 a.m. reminder is still called that at noon.
    """
    m = _CLOCK.search(_spoken_to_clock(phrase))
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2) or 0)
    ampm = ((m.group(3) or _daypart_ampm(phrase)) or "").lower().replace(".", "")
    if ampm.startswith("p") and hh < 12:
        hh += 12
    if ampm.startswith("a") and hh == 12:
        hh = 0
    if hh > 23 or mm > 59:
        return None
    hit = _pick(rows, lambda r: _hhmm(r) == (hh, mm))
    # An unqualified "the 10 one" could be either half of the day; only accept
    # the 12-hour reading when it is the only row that fits.
    if not hit and not ampm:
        hit = _pick(rows, lambda r: _hhmm(r) == ((hh + 12) % 24, mm))
    return hit


def _by_day(rows, q):
    for i, day in enumerate(_DAYS):
        if re.search(rf"\bthe\s+{day}\s+one\b", q, re.I):
            return _pick(rows, lambda r: r.get("epoch") and
                         time.localtime(r["epoch"]).tm_wday == i)
    return None


def _by_keyword(rows, q):
    """'the William one', 'change this one to order three boards' — a content
    word that appears in exactly one reminder names it. Stop-words are stripped
    so the shared scaffolding of every command ('change', 'order', 'to') cannot
    match anything."""
    words = [w for w in re.findall(r"[\w']{3,}", q.lower()) if w not in _STOP]
    if not words:
        return None

    # Without the pending-first rule, "order three boards" matched a cancelled
    # Epson-driver row and a done one about salmon fillets, and the two of them
    # together made a perfectly clear instruction ambiguous.
    return _pick(rows, lambda r: any(w in (r.get("text") or "").lower()
                                     for w in words))


def _strip_selector(q):
    """Remove the phrase that identified the row, keeping the instruction."""
    q = _THE_X_ONE.sub(" ", q)
    q = _FROM_TO.sub(lambda mm: " to " + mm.group("new"), q)
    q = _PRONOUN.sub(" ", q)
    return " ".join(q.split())


def resolve(q, owner=None):
    """The row this sentence is about, or None to let the model decide."""
    rows = _all_rows(owner)
    if not rows:
        return None
    sel = _FROM_TO.search(q)
    if sel:
        hit = _by_clock(rows, sel.group("sel"))
        if hit:
            return hit
    x = _THE_X_ONE.search(q)
    if x:
        hit = _by_clock(rows, x.group("what")) or _by_day(rows, q) \
            or _by_keyword(rows, x.group("what"))
        if hit:
            return hit
    hit = _by_keyword(rows, q)
    if hit:
        return hit
    # Bare "change it" with a single live reminder is not ambiguous.
    if _PRONOUN.search(q):
        live = [r for r in rows if r.get("state") == "pending"]
        if len(live) == 1:
            return live[0]
    return None
# -------------------------------------------------------- END TARGET RESOLUTION


AUDIT = os.environ.get("AMEND_AUDIT",
                       f"{C.WORKSPACE_ROOT}/voice/realtime/amend_audit.log")


def _audit(question, rid, note):
    """#138 (the app developer's agent): "if your reflex has any way to log which id it acted on
    versus which id the app sent, that would settle whose side it is on."

    It did not, so here it is. One line per amendment attempt, whatever the
    outcome — a refusal is as diagnostic as a change, and the case he cannot
    reproduce is one where the WRONG row moved, which only shows up as a
    difference between the id in the sentence and the id in the write.
    """
    try:
        ids = re.findall(r"\b(?:reminder\s*#?\s*)?(\d{1,6})\b", question or "")
        with open(AUDIT, "a") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t"
                     f"sent={question[:120]!r}\tnumbers_in_text={ids}\t"
                     f"acted_on={rid}\t{note}\n")
    except Exception:
        pass


def amend(question, owner=None):
    """(answer, changed) for an amendment we can do exactly, else (None, False).

    The asker can only touch what they can see, and the sentence picks WHICH
    list that is: "move my dentist one" is theirs, "move our pickup one" is the
    shared one. Same rule as the listings, so an edit can never reach across.
    """
    scope = scope_of(question, owner)
    q = " ".join((question or "").split())
    m = _ID.search(q)
    if not m:
        # No "reminder 34" in the sentence: try to work out which row it is
        # from the words themselves. Only an unambiguous hit proceeds, and only
        # when the sentence actually asks for a change — "show me the William
        # one" is a LIST question and belongs to the renderers below.
        if not re.search(r"\b(change|move|set|make|edit|update|reschedul\w+|"
                         r"rename|push|delay|cancel|delete|remove|drop)\b", q, re.I):
            return None, False
        row0 = resolve(q, scope)
        if not row0:
            _audit(question, None, "no row matched the words — falls to model")
            return None, False
        rid = row0["id"]
        _audit(question, rid, f"resolved BY NAME -> {row0.get('text', '')[:60]!r}")
        # The words that NAMED the row must not be read as the instruction.
        # "Move the William one to 9am" parsed as 1:00, because _spoken_to_clock
        # turns the "one" of "the William one" into an hour. Excise the selector
        # before anything downstream reads a time out of it.
        q = _strip_selector(q)
        # Everything downstream slices the sentence at the id match. With no id
        # in the text, the whole sentence is the instruction.
        class _Span:                       # noqa: D401 - tiny shim
            def start(self): return 0
            def end(self): return 0
        m = _Span()
    else:
        rid = int(m.group(1))
        _audit(question, rid, "id taken FROM THE SENTENCE")
    sys.path.insert(0, f"{C.WORKSPACE_ROOT}/operations/reminders")
    import reminders
    row = None
    # Scoped to the asker: naming a number must not reach past ownership. A row
    # belonging to the other owner is reported exactly as a missing one — the
    # answer tells the asker nothing about someone else's list.
    for r in reminders.list_rows(all_rows=True, owner=scope):
        if r["id"] == rid:
            row = r
            break
    if not row:
        _audit(question, rid, "no such row in the database (or not the asker's)")
        return f"There is no reminder {rid}.", False
    was = row["status"]
    # The clause AFTER the quoted row is the instruction; the row itself is
    # context the app pasted in, and it contains words like "Reminder:" that
    # would otherwise look like commands.
    # #113: the OPERATION lives in the leading sentence; everything after the
    # first full stop is prose for the model (row context, "confirm in one
    # short sentence"). Matching the whole paragraph is how a perfectly good
    # instruction cost 31 seconds — mine parsed it, then found the clause
    # buried behind a bracketed row and gave up.
    lead = re.split(r"(?<=[.!?])\s", q, 1)[0]
    tail = lead[m.end():] if m.end() < len(lead) else q[m.end():]
    tail = re.sub(r"^\s*\([^)]*\)\s*", "", tail).strip(" .:—-")
    # An explicit "The change: X" marker beats position — it is the one place
    # the old buried format still names its own instruction, and honouring it
    # costs nothing and keeps older app builds fast.
    marked = re.search(r"\bthe change\s*:\s*(.+)", q, re.I)
    if marked and not tail:
        tail = marked.group(1).strip(" .:—-")
    if _DELETE.search(q[:m.start()] + " " + tail):
        if was == "cancelled":
            return f"Reminder {rid} was already cancelled.", False
        ok = reminders.cancel(rid)
        return (f"Deleted reminder {rid} — {row['text'][:60]}." if ok
                else f"Could not delete reminder {rid}."), ok
    # "Change reminder id 34: change the text to X" nests his verb inside mine;
    # "The change: change the text to: X" nests it twice. Try the clause as it
    # arrives, then with one leading verb peeled off, rather than demanding a
    # shape the caller has no reason to know about.
    rel = _RELABEL.search(tail) or _RELABEL.search(
        re.sub(r"^(?:the\s+)?change\s*:?\s*", "", tail, flags=re.I))
    if rel:
        new = (rel.group(1) or rel.group(2) or "").strip(" :")
        if new:
            reminders.edit(rid, label=new)
            note = f" It is still marked {was}." if was != "pending" else ""
            _audit(question, rid, f"WROTE text -> {new[:60]!r}")
            return f"Reminder {rid} now reads: {new}.{note}", True
    # "from 8 a.m. to 10 a.m.": the first time SELECTED the row, so parsing the
    # clause whole would read 8 as the destination and move it where it already
    # is. The destination is the second half, explicitly.
    ft = _FROM_TO.search(tail)
    when = (_parse_when(ft.group("new"), keep_epoch=row.get("when_epoch") or None)
            if ft else None)
    when = when or _parse_when(tail, keep_epoch=row.get("when_epoch") or None)
    if when:
        ok, rearmed = reminders.edit(rid, when_local=when)
        pretty = time.strftime("%A %-d %B at %H:%M",
                               time.strptime(when, "%Y-%m-%d %H:%M"))
        # Re-arming is stated, never silent: a reminder that quietly went from
        # "done" back to "will fire" is a surprise waiting to happen.
        tailnote = (" It was already " + was + ", so it is set to fire again."
                    if rearmed else "")
        _audit(question, rid, f"WROTE time -> {pretty}")
        return f"Reminder {rid} moved to {pretty}.{tailnote}", True
    # "change this one to order three boards" — a rewrite with no "the text to"
    # scaffolding. Only reached once a time reading has been ruled out, so the
    # clause cannot be a reschedule wearing the same words, and only when the
    # sentence targeted a row without naming its number (the id form has always
    # had the explicit _RELABEL shape available and using it is unambiguous).
    plain = re.search(r"\b(?:change|make|set|rename)\b.{0,40}?\bto\s+(?P<new>.+)$",
                      tail, re.I)
    if plain:
        new = plain.group("new").strip(" .:—-\"'")
        # A clause that names a day or a clock time is a RESCHEDULE whose
        # phrasing _parse_when could not read — "to Friday at 2pm" renamed the
        # reminder to the words "Friday at 2pm" in testing. When a rewrite is
        # indistinguishable from a botched reschedule, neither is safe: hand it
        # to the model.
        timey = re.search(r"\b(" + "|".join(_DAYS) + r"|today|tomorrow|tonight|"
                          r"o'?clock|noon|midnight)\b", new, re.I) or \
            re.search(r"\b\d{1,2}(:\d{2})?\s*(am|pm|a\.m\.|p\.m\.)\b", new, re.I)
        if len(new.split()) >= 2 and not _FUZZY.search(new) and not timey:
            reminders.edit(rid, label=new)
            note = f" It is still marked {was}." if was != "pending" else ""
            _audit(question, rid, f"WROTE text -> {new[:60]!r}")
            return f"Reminder {rid} now reads: {new}.{note}", True
    _audit(question, rid, "understood the row but not the instruction — "
                          "falls to the model")
    return None, False        # not something to do without thinking
