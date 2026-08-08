"""Reminders reflex — "show me my reminders" answered without an LLM turn.

The owner, 2026-08-07: "when the user wants to see all reminders, present them as a
table; if a reminder has a photo attached there must be a thumbnail in the row,
and tapping it shows the photo full screen at full resolution."

Third of the same shape (tasks, backups, reminders): a question with a definite
answer should not cost a model turn. This one is a SELECT and, for rows with a
picture, one token mint per row.

THE PHOTO CELL is `![](vb-token:TOKEN)` — markdown image syntax with a file
token where a URL would go (Maclaude's shape). It survives any parser as an
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
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request

HOME = os.environ.get("DST_ROOT_HOME", os.path.expanduser("~"))
DB = f"{HOME}/DST/operations/reminders/reminders.db"
REALTIME = f"{HOME}/DST/voice/realtime"
MINT_URL = "http://127.0.0.1:8478/{secret}/mint-token"
SENDFILE = f"{HOME}/DST/telegram/sendfile.py"
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
        sys.path.insert(0, f"{HOME}/DST/operations/reminders")
        import reminders
        return reminders.summarize(text)
    except Exception:
        return text


def _cell(text):
    """No pipes, no newlines, nothing cut — a pipe ends the column early and
    shifts every later value under the wrong heading."""
    return " ".join(str(text or "").replace("|", "/").split())


def pending(limit=LIMIT):
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
            "WHERE status='pending' ORDER BY when_epoch LIMIT ?",
            (limit,)).fetchall()
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
                    "token": tok, "overdue": overdue,
                    "photo": photo if (photo and os.path.exists(photo)) else None})
    return out


def completed(limit=LIMIT):
    """Reachable, not default (#104 amended). "Did I already order those?" is
    a real question and the answer lives here — but a list of things already
    done costs one of the three or four rows a phone shows at once."""
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = c.execute(
            "SELECT id, when_local, when_epoch, "
            "COALESCE(NULLIF(TRIM(label), ''), text), photo, status FROM "
            "reminders WHERE status NOT IN ('pending') "
            "ORDER BY when_epoch DESC LIMIT ?", (limit,)).fetchall()
    finally:
        c.close()
    out = []
    for rid, wl, we, text, photo, status in rows:
        tok = _mint(photo) if (photo and os.path.exists(photo)) else None
        out.append({"id": rid, "when": _when(wl, we) + (
            ", cancelled" if status == "cancelled" else ""),
            "epoch": we, "text": text, "token": tok, "status": status,
            "photo": photo if (photo and os.path.exists(photo)) else None})
    return out


def render(rows=None, title="Reminders", empty="No reminders set.",
           noun="pending"):
    """Two columns, never three.

    2026-08-07, tested live against the Bot API: markdown image syntax inside a
    `sendRichMessage` table cell is SILENTLY DROPPED — the cell comes back with
    no text at all, so the Photo column rendered as a strip of blanks in
    Telegram while looking correct in the voice app. The owner's fix, and it is
    the better one: no Photo column here, and each photo goes out as its own
    message captioned with that reminder's time and text (see send_photos).
    One message per photo, not an album — an album carries a single caption for
    the whole group, which would put the wrong words under the wrong picture.
    """
    rows = pending() if rows is None else rows
    if not rows:
        return empty
    out = ["| When | Reminder |", "|---|---|"]
    for r in rows:
        out.append(f"| {_cell(r['when'])} | {_cell(r['text'])} |")
    n = len(rows)
    late = sum(1 for r in rows if r.get("overdue"))
    # The count word is a parameter because it was not: the completed list
    # rendered as "(20 pending)", a header that contradicted its own title.
    count = f"{n} {noun}" + (f", {late} overdue" if late else "")
    photos = sum(1 for r in rows if r.get("photo"))
    tail = ""
    if photos:
        tail = f"\n\n_{photos} of these {'has' if photos == 1 else 'have'} a photo — sending below._"
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
LIST_ISH = re.compile(r"\b(show|list|what|which|any|all|see|view|display|"
                      r"upcoming|pending|do i have|have i got)\b", re.I)
CREATE = re.compile(r"\b(remind me|remind us|set a reminder|add a reminder|"
                    r"create a reminder|cancel|delete|remove|snooze|"
                    r"how do|how to|why)\b", re.I)


# Deliberately narrow. "Did I already order those filters?" is the question
# Maclaude cited, and it is NOT matched here on purpose: it never says
# "reminder", and the honest answer needs the agent searching mail and the CRM,
# not a list of reminder rows. A reflex that swallowed it would answer a
# purchasing question with a to-do list.
DONE_ISH = re.compile(r"\b(completed|finished|done|past|previous|history|"
                      r"already)\b", re.I)


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


def try_handle(chat_id, text, send):
    # Completed first: "show me completed reminders" matches BOTH patterns,
    # and the more specific reading is the one the user typed.
    if detect_done(text):
        try:
            rows = completed()
        except Exception:
            return None
        send(chat_id, render(rows, title="Reminders — completed",
                             empty="Nothing completed yet.", noun="done"))
        n = send_photos(chat_id, rows)
        return f"reminders reflex: {len(rows)} completed, {n} photo(s)"
    if not detect(text):
        return None
    try:
        rows = pending()
    except Exception:
        return None          # invisible on failure; Claude can still answer
    send(chat_id, render(rows))
    # After the table, never inside it: the photos, one message each, so the
    # caption under a picture is that reminder's own time and words.
    n = send_photos(chat_id, rows)
    return f"reminders reflex: {len(rows)} pending, {n} photo(s)"


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:])
    if q:
        print(f"detect({q!r}) = {detect(q)}")
    t0 = time.time()
    print(render())
    print(f"\n({(time.time() - t0) * 1000:.0f}ms)")
