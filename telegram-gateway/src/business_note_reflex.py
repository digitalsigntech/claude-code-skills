"""Dictating a business note, and asking for it back, without a model turn.

Asked for right after the personal-note reflex shipped: "save this note in
the business notes. The code for the room is 9815." — a phrasing the personal
fast path did not know.

Two things the personal reflex did not have to handle:

  • The body arrives in the NEXT SENTENCE. Speech-to-text writes "save this
    note in the business notes. The code for the room is 9815." — a period,
    not a colon. So the separator set includes '.' and the body is whatever
    follows the phrase, even across a sentence boundary.
  • Nothing to save is a real case ("save this in business notes" and then
    nothing). That returns None and falls through to Claude rather than
    writing an empty bullet.

PRIVACY: business_notes lives outside the KB index, but the callers still gate
this the same way they gate the personal store — the owner's DM or a live
verified bot+owner-only group. Company-private is not public.
"""
import re

import business_notes

NOUN = r"business\s+notes?"
SAVE_VERB = r"(?:save|store|keep|add|put|write|note|record|file)"
BIZ_SAVE = re.compile(
    r"^\s*(?:please\s+|can you\s+|could you\s+)*"
    rf"(?:{SAVE_VERB}\s+)?(?:this\s+|that\s+|it\s+)?(?:note\s+)?"
    rf"(?:in|to|into|for)?\s*(?:the\s+|my\s+|our\s+)?{NOUN}\b"
    r"\s*[:,.\-–—]*\s*(?P<body>.*)$", re.I)
# The other order, spoken just as often: "add to business notes" reversed —
# "the room code is 9815, put that in the business notes."
BIZ_TRAILING = re.compile(
    rf"^\s*(?P<body>.+?)[,.;]?\s*(?:{SAVE_VERB})\s+(?:this|that|it)?\s*"
    rf"(?:in|to|into)\s*(?:the\s+|my\s+|our\s+)?{NOUN}\.?\s*$", re.I)
RU_SAVE = re.compile(
    r"^\s*(?:сохрани\w*\s+|запиши\w*\s+|добавь\s+)?(?:в\s+)?(?:наши\s+|мои\s+)?"
    r"(?:рабочи\w+|бизнес|деловы\w+)[\s-]*заметк\w+\s*[:,.\-–—]*\s*(?P<body>.*)$",
    re.I)
ASKING = re.compile(r"\b(what|what's|whats|which|where|remind|tell|give|say|"
                    r"какой|какая|что|где|напомни|скажи)\b", re.I)
LISTING = re.compile(r"\b(list|show|all|everything|read out|open|search|find)\b", re.I)
STOP = {"what", "whats", "what's", "which", "where", "is", "the", "a", "an",
        "my", "our", "me", "again", "tell", "remind", "give", "say", "was",
        "of", "for", "to", "do", "does", "i", "we", "have", "it", "s", "please",
        "business", "note", "notes", "какой", "какая", "какие", "что", "где",
        "напомни", "скажи", "у", "нас", "мне", "от", "наш", "наши"}


def text_of(text):
    """The note body in a 'business notes' line, or None if this isn't one —
    or is one with nothing to save yet."""
    t = " ".join((text or "").split())
    if not t or len(t) > 600 or t.startswith("/"):
        return None
    if LISTING.search(t):
        return None
    for pat in (BIZ_TRAILING, BIZ_SAVE, RU_SAVE):
        m = pat.match(t)
        if m:
            body = m.group("body").strip().strip("\"'“”")
            if len(body) >= 3 and not body.endswith("?"):
                return body
    return None


def save(body):
    """(answer, line) — writes the bullet and answers in the same breath."""
    line = business_notes.add(body)
    if not line:
        return None, None
    return "Saved to the business notes.", line


# The owner dictates in English and asks in Russian. Same trick as the personal
# reflex: a short map of the words that actually end up in a business note.
RU_EN = {"код": "code", "комната": "room", "комнаты": "room", "кабинет": "office",
         "офис": "office", "офиса": "office", "склад": "warehouse",
         "склада": "warehouse", "дверь": "door", "двери": "door",
         "ключ": "key", "ключа": "key", "вайфай": "wifi", "пароль": "password",
         "сигнализация": "alarm", "сигнализации": "alarm", "банк": "bank",
         "счёт": "account", "счет": "account", "телефон": "phone"}


def _query_terms(text):
    toks = [w for w in re.split(r"[^\w']+", (text or "").lower()) if w]
    return [RU_EN.get(w, w) for w in toks if w not in STOP and len(w) > 1]


def _spoken(text):
    """The bullet without its markdown — this gets read out loud."""
    return re.sub(r"\*\*|__|`", "", text or "").strip()


def detect_lookup(text):
    t = " ".join((text or "").split())
    if not t or len(t) > 160 or t.startswith("/"):
        return False
    return bool(ASKING.search(t)) and not bool(text_of(t))


def lookup(text):
    """The note, or None to let the model answer. Callers gate the chat."""
    if not detect_lookup(text):
        return None
    terms = _query_terms(text)
    if len(terms) < 2:                   # one word is a topic, not a name
        return None
    hits = business_notes.search(" ".join(terms), limit=4)
    if not hits or len(hits) > 2:        # ambiguous: let the model sort it out
        return None
    if len(hits) == 2:
        return (f"{_spoken(hits[0][1])}\n\n"
                f"(Also, from {hits[1][0]}: {_spoken(hits[1][1])})")
    return _spoken(hits[0][1])


def try_handle(chat_id, text, send):
    body = text_of(text)
    if body:
        answer, line = save(body)
        if not answer:
            return None
        send(chat_id, answer)
        return f"business note reflex: saved {line!r}"
    found = lookup(text)
    if found:
        send(chat_id, found)
        return "business note reflex: read back a note"
    return None


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:])
    if q:
        print(f"text_of  = {text_of(q)!r}")
        print(f"lookup   = {lookup(q)!r}")
