"""«Any urgent emails?» — answered from a file, in milliseconds.

the owner, 2026-08-10: *"If I ask 'do I have any hot emails' or 'do I have any
urgent emails?', the answer should be on the box instant, cached… This answer
should be regenerated upon checking emails."* And: *"a table that will show who
it's from, time stamp, and a brief summary. Three columns."*

The cache is built by `email/urgent.py` at the end of every digest pass. This is
only the reading half — it never computes, never opens the archive, and is
therefore as fast as the disk.

THREE COLUMNS IS A CONSTRAINT, NOT A STYLE. Up to three, the app wraps the cells
inside the bubble and the table is readable at a glance; at four it keeps its
shape and scrolls sideways, which is the opposite of a glance. So how long a
thread has been waiting rides in the summary cell rather than earning a column,
and the urgency flags do not appear at all — they are ranking signals, and a
"(today)" printed next to a four-day-old thread reads as a claim rather than as
the reason it sorted where it did.

WHAT IT PROMISES, EXACTLY. Every row is a thread whose newest message came from
outside the workspace — they wrote last, so they are waiting on us. That is a fact about
the archive, not a judgement about importance, and the wording says so. Calling
it "important" would be a claim nobody here is in a position to make.
"""


import tgconf as C   # identity from config
import json
import os
import re
import time

HOME = os.path.expanduser("~")
CACHE = os.environ.get("URGENT_JSON", f"{C.WORKSPACE_ROOT}/email/urgent.json")
STALE_S = 3600          # the digest runs every 15 min through the day


def _cell(text):
    """No pipes, no newlines, nothing cut. A pipe ends the column early and
    shifts every later value under the wrong heading."""
    return " ".join(str(text or "").replace("|", "/").split())


def _when(ts):
    """The timestamp column. HH:MM for today (the app rewrites it into the
    phone's own 12/24-hour convention — that only happens in a column whose
    heading is time-ish, which is why this one is called When), and a date
    plus the time for anything older."""
    t = time.localtime(ts)
    days = (int(time.time()) // 86400) - (int(ts) // 86400)
    if days == 0:
        return time.strftime("%H:%M", t)
    if days == 1:
        return f"yesterday {time.strftime('%H:%M', t)}"
    return f"{time.strftime('%-d %b', t)} {time.strftime('%H:%M', t)}"


def _summary(row, client):
    """The whole thing for the app, one sentence for Telegram."""
    full = row.get("summary_full") or row.get("summary") or row.get("subject")
    if client == "ios":
        return full
    short = row.get("summary") or full or ""
    return short


def _waited(hours):
    if hours < 24:
        return f"waiting {int(hours)}h"
    return f"waiting {int(hours / 24)}d"


def load():
    try:
        with open(CACHE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def render(data=None, client="ios"):
    """#131: the app truncates the third column at three lines and a tap opens
    the whole thing, so a long summary costs nothing there and shortening it
    here would throw away text the reader can already reach. Telegram has no
    tap-to-expand, so that client still gets the short form."""
    data = data if data is not None else load()
    if data is None:
        # Never silently answer "nothing urgent" when the truth is "I could not
        # look". A missing cache and an empty inbox are opposite states and only
        # one of them is good news.
        return ("I cannot read the urgent-email cache right now, so I do not "
                "know — it is rebuilt every time the mail is checked.")
    rows = data.get("rows") or []
    age = time.time() - (data.get("generated") or 0)
    if not rows:
        return "*Nothing waiting on a reply* — every thread this week has our answer on it."
    # #131, one word, the owner looking at the live table: Summary.
    # Bold header, same reason as the reminders table (the owner 2026-08-13).
    out = ["| **From** | **When** | **Summary** |", "|---|---|---|"]
    for r in rows:
        # The urgency FLAGS stay out of the cell. They are ranking signals —
        # the word "today" found in a body — and printed beside a four-day-old
        # thread they read as a claim about the thread rather than as the
        # reason it sorted where it did. He asked for who, when, and what.
        out.append(f"| {_cell(r.get('from'))} | {_when(r.get('ts', 0))} | "
                   f"{_cell(_summary(r, client))} · "
                   f"{_waited(r.get('waiting_h', 0))} |")
    head = (f"*Waiting on a reply* — {len(rows)} thread"
            f"{'s' if len(rows) != 1 else ''}")
    body = f"{head}\n\n" + "\n".join(out)
    if age > STALE_S:
        body += (f"\n\n_Last checked {int(age / 3600)}h ago — the mail checker "
                 f"may not have run._")
    return body


# "any urgent emails", "do I have hot emails", "anything waiting on me",
# "what needs answering". NOT "reply to the urgent email" — that is an
# instruction, and answering it with a table would swallow the request.
NOUN = re.compile(r"\b(e-?mails?|inbox|messages?|threads?|correspondence)\b", re.I)
URGENT = re.compile(r"\b(urgent|hot|pressing|burning|critical|waiting|"
                    r"unanswered|outstanding|overdue|need(?:s|ing)? (?:a )?"
                    r"(?:reply|answer|response)|awaiting)\b", re.I)
ACTION = re.compile(r"\b(reply|respond|answer|draft|write|send|forward|"
                    r"delete|archive|open|read me|show me the (?:body|text))\b",
                    re.I)
BARE = re.compile(r"^\s*(anything|what)(?:'s|\s+is|\s+are)?\s+"
                  r"(urgent|hot|pressing|waiting|burning|critical|"
                  r"needs?\s+(?:my\s+)?(?:attention|answer(?:ing)?|"
                  r"repl(?:y|ying)))\b", re.I)
# The question forms that CONTAIN an action word without being one. "Anything I
# need to answer" is the most natural way to ask this, and a bare \banswer\b in
# ACTION refused it — the filter was eating the question it exists to serve.
ASKING = re.compile(
    r"^\s*(?:is\s+there\s+|do\s+i\s+have\s+|have\s+i\s+got\s+)?anything\s+"
    r"(?:i\s+)?(?:still\s+)?(?:need(?:s)?\s+|have\s+|got\s+)?(?:to\s+)?"
    r"(?:answer|reply|respond)\b|"
    r"^\s*who(?:'s|\s+is|\s+are)?\s+waiting\b|"
    r"^\s*what(?:'s|\s+is)?\s+waiting\b", re.I)


def detect(text):
    t = (text or "").strip()
    if not t or len(t) > 120 or "\n" in t or t.startswith("/"):
        return False
    # Asked BEFORE the action filter, or the filter refuses the question.
    if ASKING.match(t):
        return True
    if ACTION.search(t):
        return False
    if BARE.match(t):
        return True
    return bool(NOUN.search(t)) and bool(URGENT.search(t))


def try_handle(chat_id, text, send):
    if not detect(text):
        return None
    send(chat_id, render(client="telegram"))
    data = load() or {}
    return f"urgent reflex: {len(data.get('rows') or [])} waiting"


if __name__ == "__main__":
    q = " ".join(_sys.argv[1:])
    if q:
        print(f"detect({q!r}) = {detect(q)}")
    t0 = time.time()
    print(render())
    print(f"\n({(time.time() - t0) * 1000:.1f}ms)")
