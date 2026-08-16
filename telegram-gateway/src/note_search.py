"""Finding a note by what it MEANS, not by every word appearing in it.

the owner, 2026-08-16, after "what is my marriott membership no?" found nothing and
my fix was a table mapping "no" to "number": "You should understand the meaning
of 'no'. You are not a dumb hard-coded system. Moreover, I could have asked
'my marriott membership' without the word 'number' and you should have found
it."

Both halves are the same fault. The search required EVERY query token to appear
in the note, so one word he did not phrase the way I stored it — "no", "id",
"loyalty", nothing at all — dropped the only correct hit to zero. An
abbreviation table just adds a fourth spelling to a rule that will keep
breaking on the fifth.

So ranking here is by meaning, using the embedding server that is already warm
on the box (~45ms), with literal token overlap as a tie-break rather than as a
gate. Nothing scores zero for missing a word; the best-matching note wins and
must clear a floor to be answered at all.

Vectors are cached beside the store, keyed by the note's text — a note is
embedded once, and a query costs one round trip. If the server is down the
ranking degrades to token overlap, which still beats all-or-nothing: two words
of three is a hit, not a miss.

PRIVACY: the cache holds vectors of private notes, so it is written next to the
notes it came from — inside the tier's own directory, never a shared one.
Hidden and non-.md, so no tier's semantic index picks it up.
"""
import hashlib
import json
import math
import os
import re
import urllib.request

EMB_URL = os.environ.get("TG_EMBED_URL", "http://127.0.0.1:18183/v1/embeddings")
# nomic asks for asymmetric prefixes: the question and the note are not the
# same kind of text, and saying so is worth a few points of accuracy.
Q_PREFIX = "search_query: "
D_PREFIX = "search_document: "
TIMEOUT = 6
MAX_CHARS = 500
# Below this, "the best match" is just the least bad one. Answering there is how
# a store of three notes confidently returns the wrong one — and being wrong is
# worse than being slow, because falling short of the floor just means the
# model answers instead of the reflex.
FLOOR = 0.58
# How far the best note must be ahead of the runner-up to count as the answer.
MARGIN = 0.06
_WORD = re.compile(r"[^\w']+", re.U)
# Words that carry no topic. Without this "what's the weather" scored a hit on
# a note about the workshop alarm, on the strength of sharing the word "the".
_NOISE = {"the", "a", "an", "is", "are", "was", "were", "my", "our", "me", "we",
          "i", "it", "its", "of", "for", "to", "in", "on", "at", "and", "or",
          "what", "what's", "whats", "which", "who", "where", "when", "how",
          "do", "does", "did", "please", "tell", "give", "say", "again",
          "мой", "моя", "мои", "наш", "наши", "что", "как", "какой", "какая",
          "где", "когда", "мне", "меня", "нас", "это", "от", "для"}


def _toks(s, drop_noise=False):
    out = [w for w in _WORD.split((s or "").lower()) if len(w) > 1]
    return [w for w in out if w not in _NOISE] if drop_noise else out


def _embed(texts, prefix):
    body = json.dumps({"input": [prefix + " ".join(t.split())[:MAX_CHARS]
                                 for t in texts]}).encode()
    req = urllib.request.Request(EMB_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = json.load(r)["data"]
    out = []
    for row in data:
        v = row["embedding"]
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append([x / n for x in v])
    return out


def _key(text):
    return hashlib.sha1(" ".join((text or "").split()).encode()).hexdigest()[:16]


class VectorCache:
    """One note embedded once. `path` lives inside the tier it belongs to."""

    def __init__(self, path):
        self.path = path
        try:
            with open(path) as fh:
                self.vecs = json.load(fh)
        except Exception:
            self.vecs = {}

    def get(self, texts):
        """Vectors for `texts`, embedding and storing whatever is new."""
        missing = [t for t in texts if _key(t) not in self.vecs]
        if missing:
            try:
                for t, v in zip(missing, _embed(missing, D_PREFIX)):
                    self.vecs[_key(t)] = v
                self._save()
            except Exception:
                return None
        return [self.vecs.get(_key(t)) for t in texts]

    def _save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(self.vecs, fh)
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)
        except Exception:
            pass


# He asks in Russian about notes dictated in English, and the names inside them
# are the same names either way — "марриотт" IS marriott. The embedding model is
# weak across alphabets (0.48 for a question it answers at 0.62 in English), so
# names get transliterated before the literal comparison. A letter map, not a
# word list: it works on the next brand he writes in Cyrillic too.
_CYR = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
        "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
        "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya"}


def _translit(tok):
    if not any(c in _CYR for c in tok):
        return tok
    out = "".join(_CYR.get(c, c) for c in tok)
    # Russian inflects; the stem is what matches an English note.
    return re.sub(r"(ov|a|u|e|y|i)$", "", out) or out


def _forms(tok):
    t = _translit(tok)
    return {tok, t} if t != tok else {tok}


def _overlap(qtoks, text):
    """Share of the question's words the note actually contains (0..1).

    Substring on either side, so "membership" matches "memberships" and "no"
    matches nothing it shouldn't — a two-letter token cannot carry a hit on its
    own because scoring is a fraction, not a gate."""
    if not qtoks:
        return 0.0
    hay = set()
    for h in _toks(text):
        hay |= _forms(h)
    hits = 0
    for t in qtoks:
        forms = _forms(t)
        if any(f == h or (len(f) >= 3 and (f in h or h in f))
               for f in forms for h in hay):
            hits += 1
    return hits / len(qtoks)


def rank(query, texts, cache=None, floor=FLOOR):
    """[(score, index)] best first, only what clears the floor.

    score = meaning (cosine) nudged by literal overlap, so an exact word match
    breaks ties between two notes that mean roughly the same thing. With no
    embedding server it IS the overlap — degraded, never all-or-nothing."""
    qtoks = _toks(query, drop_noise=True)
    if not texts or not qtoks:
        return []
    doc_vecs = cache.get(texts) if cache else None
    q_vecs = []
    if doc_vecs and all(doc_vecs):
        # The question asked in Cyrillic about a note dictated in English gets
        # embedded both ways and keeps whichever understands it better: this
        # model reads "nomer marriott" (0.55) far better than "номер марриотт"
        # (0.48) against an English note.
        forms = [query]
        latin = " ".join(_translit(t) for t in qtoks)
        if latin != " ".join(qtoks):
            forms.append(latin)
        try:
            q_vecs = _embed(forms, Q_PREFIX)
        except Exception:
            q_vecs = []
    scored = []
    for i, text in enumerate(texts):
        lex = _overlap(qtoks, text)
        if q_vecs:
            cos = max(sum(a * b for a, b in zip(qv, doc_vecs[i])) for qv in q_vecs)
            score = cos + 0.15 * lex
        else:
            # No server: overlap alone, on the same 0..1 scale as cosine so one
            # floor governs both paths. Two words of three clears it; one of
            # three does not.
            score = lex
        if score >= floor:
            scored.append((round(score, 4), i))
    scored.sort(key=lambda s: -s[0])
    # A near-tie is not an answer. "what is my locker number" scores 0.656 on
    # the Marriott note and 0.609 on the room code — it resembles both because
    # it is neither, and the honest result is nothing, which sends the question
    # to the model. A real hit wins outright: the same store answers "my hotel
    # loyalty id" at 0.62 against 0.53.
    if len(scored) > 1 and scored[0][0] - scored[1][0] < MARGIN:
        return []
    return scored
