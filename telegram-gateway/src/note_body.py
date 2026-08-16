"""Which part of a dictated note is the note.

the owner, 2026-08-16: "My Marriott Bonvoy membership number is 000000000. Save to
our biz KB. Postal code used: A1A1A1" — and what got saved was "A1A1A1". Two
faults of the same shape. The phrase patterns were anchored to the start or the
end of the message, so an instruction sitting BETWEEN two facts matched
neither; the fallback then took "everything after the first colon", which here
was the last four words of a three-fact note. The number he actually asked me
to keep was the one thing thrown away.

The rule that survives every position — instruction first, instruction last,
instruction in the middle — is: find the request to save, cut it out, keep
everything else in the order it was spoken. A note is what is left when you
remove the asking.

Shared by the personal and business note reflexes; each supplies its own
vocabulary, because "my notes" and "our biz KB" are different drawers.
"""
import re

SENTENCE = re.compile(r"(?<=[.!?])\s+")
# Trailing punctuation that only joined the fact to the instruction. A period
# is NOT stripped: it separates two facts that are about to become neighbours.
_DANGLING = " ,;:-–—"


def without(text, *patterns):
    """`text` minus the first save-instruction any pattern finds, or None.

    The patterns are unanchored on purpose — .search, not .match."""
    for pat in patterns:
        m = pat.search(text)
        if not m:
            continue
        head = text[:m.start()].rstrip(_DANGLING)
        tail = text[m.end():].lstrip()
        return " ".join(f"{head} {tail}".split())
    return None


def without_instruction_sentences(text, verb_re, noun_re):
    """Same idea, one level coarser: drop whole sentences that are the ask.

    For wordings no phrase pattern knows ("this one belongs in the company
    notes, please"). A sentence naming both a save verb and the drawer is the
    request; every other sentence is the note. Returns None if that rule
    changes nothing or eats everything — then the caller's last resort runs."""
    parts = SENTENCE.split(text)
    if len(parts) < 2:
        return None
    kept = [p for p in parts if not (verb_re.search(p) and noun_re.search(p))]
    if not kept or len(kept) == len(parts):
        return None
    return " ".join(" ".join(kept).split())
