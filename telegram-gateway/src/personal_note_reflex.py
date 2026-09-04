"""Saving something to personal notes, without waiting for the description.

the owner, 2026-08-07, having waited 11:37 → 11:39 for "Saved to your personal
notes": "it should take just a few seconds."

The two minutes were a vision call. The reply that came back — *a close-up of a
MacBook keyboard, labelled and tagged so you can find it by macbook or
keyboard* — is exactly where the time went, and the useful content of it, to
him, was the word SAVED.

SAME SPLIT AS #115: the file is RECEIVED, then it is FILED. Only the first half
needs him present. The bytes are stored and answered for immediately; the
label and keywords are produced behind that and written into the note when they
arrive. He can already search his notes by date and filename — tags improve
that search, they do not gate it.

PRIVACY: this is the owner's private store. Nothing here reads a note back, and
the follow-up line ("that was the MacBook keyboard") goes only to a chat
personal_notes.allowed_chat() approves — the same gate that guards sending.
"""


import tgconf as C   # identity from config
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time

import note_body

HOME = os.path.expanduser("~")
CAMERA = f"{C.WORKSPACE_ROOT}/voice/realtime/camera"
# The DB lives under workspace/personal, not ~/personal — getting this wrong would
# have written labels into a database nobody reads.
NOTES_DB = os.environ.get("NOTES_DB", f"{C.WORKSPACE_ROOT}/personal/notes.db")
FRESH_S = 1800
CLAUDE = f"{HOME}/.local/bin/claude"

_PROMPT = (
    "Look at the image at {path}. Reply with exactly two lines and nothing "
    "else:\nLABEL: <one short phrase naming what it is>\n"
    "KEYWORDS: <5-10 comma-separated words someone might search by>"
)


def _label_later(note_id, path, chat_id=None):
    """Describe the picture and write it into the note. Runs AFTER the answer.

    Failure here is silent on purpose: the note is already saved and findable
    by date and filename. A missing label is a slightly worse search, not a
    lost file — so this must never be able to turn a successful save into an
    error message."""
    try:
        out = subprocess.run(
            [CLAUDE, "-p", _PROMPT.format(path=path),
             "--dangerously-skip-permissions"],
            capture_output=True, text=True, timeout=300)
        txt = out.stdout or ""
        label = (re.search(r"LABEL:\s*(.+)", txt) or [None, None])[1]
        keys = (re.search(r"KEYWORDS:\s*(.+)", txt) or [None, None])[1]
        if not label:
            return
        con = sqlite3.connect(NOTES_DB, timeout=10)
        with con:
            con.execute("UPDATE notes SET label=?, keywords=? WHERE id=?",
                        (label.strip(), (keys or "").strip(), int(note_id)))
        con.close()
        print(f"[personal_note_reflex] note {note_id} labelled: {label.strip()}",
              flush=True)
        if chat_id:
            # The promised second line, and only to a chat the gate approves.
            import personal_notes
            if personal_notes.allowed_chat(chat_id):
                import tg_api as TG
                TG.send_message(chat_id,
                                f"That note was {label.strip().lower()} — "
                                f"searchable by {keys or 'its date'}.")
    except Exception as e:
        print(f"[personal_note_reflex] labelling failed: {e}", flush=True)


def _newest_upload(max_age=FRESH_S):
    try:
        files = [(os.path.getmtime(os.path.join(CAMERA, n)),
                  os.path.join(CAMERA, n))
                 for n in os.listdir(CAMERA) if not n.startswith(".")]
    except OSError:
        return None
    if not files:
        return None
    ts, path = max(files)
    return path if time.time() - ts <= max_age else None


def save_it(path=None, chat_id=None, viewer=None):
    """(answer, note_id). Stores and answers; labels behind the answer."""
    src = path or _newest_upload()
    if not src or not os.path.exists(src):
        return "I do not have a recent photo to save — send it first.", None
    import personal_notes
    # add() MOVES the file; the camera copy is the app's record of what was
    # sent, so hand over a copy rather than emptying that folder.
    tmp = os.path.join("/tmp", os.path.basename(src))
    shutil.copy2(src, tmp)
    note_id, dest = personal_notes.add(tmp, orig_name=os.path.basename(src),
                                       owner=viewer)
    threading.Thread(target=_label_later, args=(note_id, dest, chat_id),
                     daemon=True).start()
    return ("Saved to your personal knowledge base. I am looking at it now and will "
            "tag it so you can find it by what it shows."), note_id


# His words, not ours: "notes" and "knowledge base" name the same thing, so
# both phrasings reach the same store.
KB_NOUN = r"(?:knowledge\s?base|kb|база\s?знаний)"
NOTE_NOUN = re.compile(rf"\b(personal notes?|my notes?|private notes?|"
                      rf"(?:personal|private|my)\s+{KB_NOUN})\b", re.I)
SAVE_ISH = re.compile(r"\b(save|store|keep|add|put|file)\b", re.I)
NOT_SAVE = re.compile(r"\b(search|find|look up|show|list|send|read|what|"
                      r"remove|delete)\b", re.I)


def detect(text):
    t = (text or "").strip()
    if not t or len(t) > 160 or t.startswith("/"):
        return False
    if NOT_SAVE.search(t):
        return False
    if text_of(t):                       # "my notes: X is Y" is a TEXT note,
        return False                     # not a request to file the last photo
    return bool(NOTE_NOUN.search(t)) and bool(SAVE_ISH.search(t))


# ------------------------------------------------------------------ TEXT NOTES
# the owner, 2026-08-15, on the phone: "saving and retrieving passwords should be
# much faster." Dictating a password and asking for it back were both full
# model turns — a file write and a filename match, waited on for seconds. The
# save half is the same shape as the photo half above; the READ half is new,
# and it is the first thing in this module that ever says a note out loud, so
# it runs behind allowed_chat() exactly like personal_notes.send() does.
_SAVE_VERB = r"(?:save|store|keep|add|put|write|note|remember|record)"
TEXT_SAVE = re.compile(
    r"^\s*(?:please\s+|can you\s+|could you\s+)*"
    rf"(?:{_SAVE_VERB}\s+)?(?:this\s+|that\s+|it\s+)?(?:in|to|into)?\s*"
    rf"(?:my\s+)?(?:personal\s+|private\s+)?(?:notes?|{KB_NOUN})\b"
    r"\s*[:,\-–—]+\s*(?P<body>.+)$", re.I)
NOTE_TO_SELF = re.compile(r"^\s*note to self\s*[:,\-–—]*\s*(?P<body>.+)$", re.I)
RU_SAVE = re.compile(
    r"^\s*(?:сохрани\w*\s+|запиши\w*\s+)?(?:в\s+)?(?:мои\s+|моих\s+)?"
    r"(?:личны\w+\s+|приватны\w+\s+)?(?:заметк\w+|база\s?знаний)"
    r"\s*[:,\-–—]+\s*(?P<body>.+)$", re.I)
# Unanchored twins of the above, for an ask spoken in the middle of the note.
# The verb is required here — unanchored, "my notes" alone is a topic, not a
# request, and cutting it out of a sentence would mangle the fact.
TEXT_PHRASE = re.compile(
    r"(?:please\s+|can\s+you\s+|could\s+you\s+)*"
    rf"{_SAVE_VERB}\s+(?:this\s+|that\s+|it\s+)?(?:in|to|into|for)?\s*"
    rf"(?:my\s+|our\s+)?(?:personal\s+|private\s+)?(?:notes?|{KB_NOUN})\b"
    r"\s*[:,.\-–—]*\s*", re.I)
RU_PHRASE_ANY = re.compile(
    r"(?:сохрани\w*|запиши\w*|добавь)\s+(?:это\s+|эту\s+)?(?:в\s+)?(?:мои\s+|моих\s+)?"
    r"(?:личны\w+\s+|приватны\w+\s+)?(?:заметк\w+|базу?\s?знаний)"
    r"\s*[:,.\-–—]*\s*", re.I)
ANY_VERB = re.compile(rf"\b{_SAVE_VERB}\b", re.I)
ANY_NOUN = re.compile(r"\b(?:notes?|kb|knowledge\s?base|заметк\w*|базу?\s?знаний)\b", re.I)


def text_of(text):
    """The note body of a save request, or None.

    The DECISION is intent.classify (2026-08-15: understand the request, do not
    react to keywords); the patterns below only find where the body starts once
    the answer is yes. A question is never a save — "my notes: what did I put
    there?" is someone searching out loud."""
    t = (text or "").strip()
    if not t or len(t) > 600 or t.startswith("/"):
        return None
    try:
        import intent as _intent
        if _intent.classify(t)[0] != "kb.save.personal":
            return None
    except Exception:
        return None
    for pat in (NOTE_TO_SELF, TEXT_SAVE, RU_SAVE):
        m = pat.match(t)
        if m:
            body = m.group("body").strip().strip("\"'“”")
            if len(body) >= 3 and not body.endswith("?"):
                return body
    return _body_after_the_ask(t)


def _body_after_the_ask(t):
    """Understanding said SAVE; the patterns did not recognise the wording.

    "keep this for me: parking spot B4" is a save in any language, and no
    regex here matches it. First try cutting the ask out wherever it sits —
    the anchored patterns above miss an instruction spoken between two facts,
    and taking only what follows a colon then loses the fact in front of it
    (2026-08-16, business side, same code). Colon and next-sentence stay as
    the last resort: better a partial note than dropping a request we already
    understood."""
    body = note_body.without(t, TEXT_PHRASE, RU_PHRASE_ANY)
    if body is None:
        body = note_body.without_instruction_sentences(t, ANY_VERB, ANY_NOUN)
    if body:
        body = body.strip().strip("\"'“”")
        if len(body) >= 3 and not body.endswith("?"):
            return body
    if ":" in t:
        body = t.split(":", 1)[1].strip().strip("\"'“”")
        if len(body) >= 3 and not body.endswith("?"):
            return body
    parts = re.split(r"(?<=[.!])\s+", t)
    if len(parts) > 1:
        body = " ".join(parts[1:]).strip()
        if len(body) >= 3 and not body.endswith("?"):
            return body
    return None


def save_text(body, chat_id=None, viewer=None):
    """(answer, note_id). Writes the note and answers in the same breath.

    The note is filed under the person who dictated it (2026-08-15): a store
    that is "only accessible to the User who created them" has to record who
    that was at the moment of writing, not infer it later."""
    import personal_notes
    # AN UNKNOWN CALLER WRITES NOTHING (2026-09-04). A hosted account whose
    # name matched nobody in the registry arrived here with chat_id=None,
    # skipped the gate below, and add_text() filed its sentence under the
    # store's default owner — a stranger's words in the owner's private notes.
    # The read gate in lookup() already refuses chat_id=None; the write gate
    # now does the same, so the two answer the same question the same way.
    if chat_id is None:
        return None, None
    if not personal_notes.allowed_chat(chat_id, viewer=viewer):
        return None, None                # never write into a store from
    note_id, _ = personal_notes.add_text(body, owner=viewer)   # someone else's chat
    if not note_id:
        return None, None
    return "Saved to your personal knowledge base.", note_id


# Read-back. Two gates before a private note is ever spoken: the chat must pass
# allowed_chat(), and the question must actually NAME a note — a query that
# matches nothing returns None and falls through to the model, which is the
# behaviour we had before this reflex existed.
SECRET_NOUN = re.compile(
    r"\b(password|passcode|passphrase|pin|code|login|username|key|account|"
    r"пароль|пин|код|логин)\b", re.I)
MINE = re.compile(r"\b(my|mine|мой|моя|мои|моего|моих|у меня)\b", re.I)
ASKING = re.compile(r"\b(what|what's|whats|which|where|remind|tell|give|"
                    r"say|какой|какая|что|где|напомни|скажи)\b", re.I)
STOP = {"what", "whats", "what's", "which", "where", "is", "the", "a", "an",
        "my", "mine", "me", "again", "tell", "remind", "give", "say", "was",
        "of", "for", "to", "do", "does", "i", "have", "it", "s", "please",
        "какой", "какая", "какие", "что", "где", "мой", "моя", "мои", "моего",
        "моих", "напомни", "скажи", "у", "меня", "мне", "от"}


def detect_lookup(text):
    """Is he asking for something he saved? Meaning, not keywords — "what is
    the room code" and "напомни какой код от мастерской" are the same request
    and share no word."""
    t = (text or "").strip()
    if not t or len(t) > 200 or t.startswith("/"):
        return False
    try:
        import intent as _intent
        return _intent.classify(t)[0] == "kb.recall"
    except Exception:
        return False


# He dictates in English and asks in Russian, or the other way round — the note
# is stored in whichever language he spoke it. A dozen words cover the things
# people actually keep in a private note; anything else falls through to Claude.
RU_EN = {"пароль": "password", "пин": "pin", "код": "code", "логин": "login",
         "ключ": "key", "айпад": "ipad", "айпада": "ipad", "айфон": "iphone",
         "айфона": "iphone", "телефон": "phone", "телефона": "phone",
         "ноутбук": "laptop", "ноутбука": "laptop", "макбук": "macbook",
         "макбука": "macbook", "вайфай": "wifi", "гараж": "garage",
         "гаража": "garage", "сейф": "safe", "сейфа": "safe",
         "почта": "email", "почты": "email", "банк": "bank", "банка": "bank",
         "карта": "card", "карты": "card", "большой": "big", "большого": "big"}
ABBREV = {"no": "number", "num": "number", "nr": "number", "#": "number",
          "acct": "account", "pw": "password", "pwd": "password",
          "tel": "phone", "номер": "number", "тел": "phone"}


def _query_terms(text):
    """Tokens to match a note by — abbreviations spelled out.

    Same fix as the business side (2026-08-16): he asks "membership no?" and
    the note says "membership number", so an all-tokens-must-match search found
    nothing."""
    toks = [w.rstrip(".") for w in re.split(r"[^\w'#]+", (text or "").lower()) if w]
    out = []
    for w in toks:
        w = ABBREV.get(w, RU_EN.get(w, w))
        if w not in STOP and len(w) > 1:
            out.append(w)
    return out


def lookup(text, chat_id, viewer=None):
    """The note's text, or None to let the model answer. Private-store read —
    refuses outside the same chats personal_notes.send() will deliver to."""
    if not detect_lookup(text):
        return None
    import personal_notes
    if chat_id is None or not personal_notes.allowed_chat(chat_id, viewer=viewer):
        return None
    terms = _query_terms(text)
    if len(terms) < 2:                   # one word is not a name, it is a topic
        return None
    hits = personal_notes.search_text(" ".join(terms), limit=4, spoken=True,
                                      viewer=viewer)
    if not hits or len(hits) > 2:        # ambiguous: let the model disambiguate
        return None
    body = hits[0][4]
    if len(hits) == 2:
        return f"{body}\n\n(You have another note matching that too.)"
    return body


def try_handle(chat_id, text, send, viewer=None):
    body = text_of(text)
    if body:
        answer, note_id = save_text(body, chat_id=chat_id, viewer=viewer)
        if not answer:
            return None
        send(chat_id, answer)
        return f"personal note reflex: text note {note_id}"
    found = lookup(text, chat_id, viewer=viewer)
    if found:
        send(chat_id, found)
        return "personal note reflex: read back a note"
    if not detect(text):
        return None
    answer, note_id = save_it(chat_id=chat_id, viewer=viewer)
    send(chat_id, answer)
    return f"personal note reflex: note {note_id}" if note_id else \
        "personal note reflex: nothing to save"


if __name__ == "__main__":
    q = " ".join(_sys.argv[1:])
    if q:
        print(f"detect({q!r}) = {detect(q)}")
