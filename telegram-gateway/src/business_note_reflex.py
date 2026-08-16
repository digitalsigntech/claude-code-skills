"""Dictating a business note, and asking for it back, without a model turn.

the owner, 2026-08-15, immediately after the personal-note reflex shipped: "save
this note in the business notes. The code for the room is 9815." — then, told
that phrasing missed the new fast path, "yes, please" to teaching it.

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
import note_body

# "business notes", "business knowledge base", "biz KB" are one drawer — his
# vocabulary is the knowledge bases, and every wording must reach it.
NOUN = (r"(?:business|biz|company|corporate|private|work)\s*"
        r"(?:notes?|knowledge\s?base|kb)")
SAVE_VERB = r"(?:save|store|keep|add|put|write|note|record|file)"
_LEAD = r"(?:please\s+|can\s+you\s+|could\s+you\s+)*"
_OBJ = r"(?:this\s+|that\s+|it\s+)?(?:note\s+)?"
_PREP = r"(?:in|to|into|for)\s+"
_DET = r"(?:the\s+|my\s+|our\s+)?"
# Unanchored: the ask can lead, trail, or sit between two facts (2026-08-16).
# Whatever it matches gets cut out; the rest of the message is the note.
BIZ_PHRASE = re.compile(
    rf"{_LEAD}(?:{SAVE_VERB}\s+{_OBJ}(?:{_PREP})?|{_PREP}{_OBJ})"
    rf"{_DET}{NOUN}\b\s*[:,.\-–—]*\s*", re.I)
RU_PHRASE = re.compile(
    r"(?:сохрани\w*\s+|запиши\w*\s+|добавь\s+)?(?:в\s+)?(?:наши\s+|мои\s+)?"
    r"(?:рабочи\w+|бизнес|деловы\w+)[\s-]*(?:заметк\w+|базу?\s?знаний)"
    r"\s*[:,.\-–—]*\s*", re.I)
# For the coarse fallback: a sentence with both of these is the ask, not a fact.
ANY_VERB = re.compile(rf"\b{SAVE_VERB}\b", re.I)
ANY_NOUN = re.compile(r"\b(?:notes?|kb|knowledge\s?base|заметк\w*|базу?\s?знаний)\b", re.I)
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
    try:
        import intent as _intent
        if _intent.classify(t)[0] != "kb.save.business":
            return None
    except Exception:
        return None
    body = note_body.without(t, BIZ_PHRASE, RU_PHRASE)
    if body is None:
        body = note_body.without_instruction_sentences(t, ANY_VERB, ANY_NOUN)
    if body:
        body = body.strip().strip("\"'“”")
        if len(body) >= 3 and not body.endswith("?"):
            return body
    # Understood as a save, worded in a way neither rule above recognises: the
    # content is after the colon, or in the sentence after the asking one.
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


def save(body):
    """(answer, line) — writes the bullet and answers in the same breath."""
    line = business_notes.add(body)
    if not line:
        return None, None
    return "Saved to the private business knowledge base.", line


# He dictates in English and asks in Russian. Same trick as the personal
# reflex: a short map of the words that actually end up in a business note.
RU_EN = {"код": "code", "комната": "room", "комнаты": "room", "кабинет": "office",
         "офис": "office", "офиса": "office", "склад": "warehouse",
         "склада": "warehouse", "дверь": "door", "двери": "door",
         "ключ": "key", "ключа": "key", "вайфай": "wifi", "пароль": "password",
         "сигнализация": "alarm", "сигнализации": "alarm", "банк": "bank",
         "счёт": "account", "счет": "account", "телефон": "phone"}


# He asks in abbreviations and the note is written out in full. "membership
# no?" found nothing against "membership number" (2026-08-16) because search
# requires every token to appear.
ABBREV = {"no": "number", "num": "number", "nr": "number", "#": "number",
          "acct": "account", "pw": "password", "pwd": "password",
          "tel": "phone", "номер": "number", "тел": "phone"}


def _query_terms(text):
    toks = [w for w in re.split(r"[^\w'#]+", (text or "").lower()) if w]
    toks = [t.rstrip(".") for t in toks]
    out = []
    for w in toks:
        w = ABBREV.get(w, RU_EN.get(w, w))
        if w not in STOP and len(w) > 1:
            out.append(w)
    return out


def _spoken(text):
    """The bullet without its markdown — this gets read out loud."""
    return re.sub(r"\*\*|__|`", "", text or "").strip()


def detect_lookup(text):
    """Asking for a company-private fact he saved — decided by meaning."""
    t = " ".join((text or "").split())
    if not t or len(t) > 200 or t.startswith("/"):
        return False
    try:
        import intent as _intent
        return _intent.classify(t)[0] == "kb.recall"
    except Exception:
        return False


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
