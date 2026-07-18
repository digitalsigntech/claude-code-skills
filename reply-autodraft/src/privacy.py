#!/usr/bin/env python3
"""Privacy layer for the auto-draft pipeline.

Goal: the cloud LLM never sees raw customer PII. Everything that touches unmasked
text lives here and runs on components you can move fully on-box later (rules =
local always; NER = a model endpoint you point wherever you like). Swapping the
NER/model backend is a config change, not a redesign.

Flow:  raw text --detect--> entities --mask--> [[TYPE_N]] tokens --> cloud LLM
       cloud reply --unmask--> real values --> Gmail draft

Design rules:
  * Reversible: a per-message Masker holds placeholder<->real maps; unmask is a
    plain string substitution, so it cannot hallucinate.
  * Stable: same surface form -> same placeholder across classify/draft/learn.
  * Tripwire: before ANY payload leaves for the cloud, re-scan with the structured
    rules. If high-confidence PII (email/phone/card) survived detection, we
    REFUSE to send (caller falls back to local generation). Belt for NER misses.
"""
import os, re, json, time, urllib.request, urllib.error

# --------------------------------------------------------------------------- rules
# Deterministic, zero-false-negative on structured PII. These run locally forever.
RULES = [
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("PHONE", re.compile(r"(?<!\w)(?:\+?\d[\d ()\-]{7,}\d)(?!\w)")),
    ("CARD",  re.compile(r"\b(?:\d[ -]?){13,16}\b")),
    ("URL",   re.compile(r"https?://[^\s]+")),
    # order / invoice / serial numbers: letter-prefixed or long digit runs
    ("ORDER", re.compile(r"\b(?:PO|INV|ORD|SN|S/N)[-# ]?\w{3,}\b", re.I)),
]
# Patterns that MUST be gone before we send anything to the cloud (tripwire).
HARD_PII = [RULES[0][1], RULES[1][1], RULES[2][1]]


# Your own product/brand vocabulary is never PII. NER occasionally reads product
# names as people or hardware brands as the customer's company, and masking them
# breaks KB retrieval and draft quality. Applied ONLY to fuzzy NER entities —
# rule matches (emails/phones/cards/orders) are always masked regardless.
# Adjust to your catalogue.
PROTECTED = re.compile(
    os.environ.get("AUTODRAFT_PROTECTED_TERMS",
                   r"print\s*head|head\s*(?:doctor|tester)|ink\s*tester|"
                   r"\b(?:ricoh|kyocera|seiko|epson|xaar|dimatix)\b"), re.I)


def _token(kind, n):
    return f"[[{kind}_{n}]]"


class Masker:
    """Bidirectional PII masker, one per inbound message/thread."""

    def __init__(self):
        self.rev = {}          # placeholder -> real value
        self.fwd = {}          # real value  -> placeholder (dedupe/stability)
        self._n = {}           # per-kind counter
        self.ner_ok = True     # False if the NER backend errored during seed()

    def _assign(self, kind, value):
        value = value.strip()
        if not value:
            return value
        if value in self.fwd:
            return self.fwd[value]
        self._n[kind] = self._n.get(kind, 0) + 1
        ph = _token(kind, self._n[kind])
        self.fwd[value] = ph
        self.rev[ph] = value
        return ph

    def seed(self, *texts):
        """Detect entities across all given texts and register placeholders so
        the mapping is consistent no matter which text is masked first."""
        for text in texts:
            if not text:
                continue
            for kind, rx in RULES:
                for m in rx.finditer(text):
                    self._assign(kind, m.group(0))
            try:
                ents = ner(text)                   # model NER (may raise)
            except Exception:
                self.ner_ok = False                # backend down -> caller routes local
                continue
            for kind, value in ents:
                if PROTECTED.search(value):        # product terms are not PII
                    continue
                self._assign(kind, value)
        return self

    def register(self, kind, value):
        """Explicitly register a known entity (e.g. the sender's name from a
        CRM) so it is masked even though NER never saw it in the email text."""
        if value and value.strip():
            self._assign(kind, value)
        return self

    def mask(self, text):
        if not text:
            return text
        # longest real values first so substrings don't corrupt a longer match
        for value, ph in sorted(self.fwd.items(), key=lambda kv: -len(kv[0])):
            text = text.replace(value, ph)
        return text

    def unmask(self, text):
        if not text:
            return text
        for ph, value in self.rev.items():
            text = text.replace(ph, value)
        return text

    def density(self, text):
        """Fraction of chars covered by placeholders after masking — used to
        decide when a message is too sensitive to send (route local instead)."""
        masked = self.mask(text or "")
        hit = sum(len(m.group(0)) for m in re.finditer(r"\[\[[A-Z]+_\d+\]\]", masked))
        return hit / max(len(masked), 1)


def leak_check(text):
    """Tripwire: return the list of HARD_PII strings still present. Empty == safe.
    Caller MUST NOT send to the cloud LLM if this is non-empty."""
    hits = []
    for rx in HARD_PII:
        hits += [m.group(0) for m in rx.finditer(text or "")]
    return hits


# Words that mark a text as confidently English. Chosen to be English-specific
# (no "in"/"is", which are also German/Dutch); English prose scores 20-30%.
_EN_HINTS = re.compile(
    r"\b(the|and|you|your|for|with|this|that|please|thank|thanks|we|are|will|"
    r"have|would|could|any|questions|regards)\b", re.I)


def inflection_fix(text, had_placeholders):
    """Repair grammar around unmasked names in inflected languages.

    Substituting real names back into [[TYPE_N]] slots is seamless in English, but
    in inflected languages (Slavic, Baltic, German, ...) a name dropped into a slot
    can break case/declension/agreement. Only runs when the reply actually contained
    placeholders and doesn't look confidently English; runs on the private model, so
    the repaired text never leaves the privacy boundary."""
    if not text or not had_placeholders:
        return text
    words = re.findall(r"[^\W\d_]+", text, re.UNICODE)
    if words and len(_EN_HINTS.findall(text)) / len(words) >= 0.08:
        return text                       # confidently English -> names slot in cleanly
    fixed = local_chat(
        "You are a careful copy editor. The text below had personal and company names "
        "inserted into placeholder slots, which can break grammatical case, declension, "
        "conjugation or agreement in inflected languages. Fix ONLY such grammar issues "
        "around the names — do not reword, add, remove or reformat anything else. If the "
        "text is already grammatically correct, return it unchanged. Return only the "
        "corrected text.",
        text, max_tokens=2000)
    return fixed or text


# ----------------------------------------------------------------------- local/NER backend
# ONE local model serves BOTH roles (entity extraction + fallback draft generation).
# Point LOCAL_ENDPOINT at any OpenAI-compatible chat endpoint (a local server, or a
# hosted one during testing). The API key is read from an env var — never hardcode it.
# Making this fully on-box later is just a change of LOCAL_ENDPOINT.
LOCAL_MODEL = os.environ.get("AUTODRAFT_LOCAL_MODEL", "your-local-model")
LOCAL_ENDPOINT = os.environ.get("AUTODRAFT_LOCAL_ENDPOINT",
                                "http://localhost:8000/v1/chat/completions")
LOCAL_API_KEY_ENV = "AUTODRAFT_LOCAL_API_KEY"      # name of the env var holding the key
_NER_SYS = (
    "Extract PERSON, ORG (company), and ADDRESS entities from the text. "
    "Return ONLY a JSON array of {\"type\":\"PERSON|ORG|ADDRESS\",\"text\":\"...\"}. "
    "Use the exact surface substring. No commentary.")


def _api_key():
    return os.environ.get(LOCAL_API_KEY_ENV, "")   # empty is fine for a local server


def _chat(system, user, max_tokens, temperature, timeout):
    """One OpenAI-compatible chat call to the local/private model. Raises on
    transport/response error. Add provider-specific fields to `payload` as needed."""
    payload = {
        "model": LOCAL_MODEL, "temperature": temperature, "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}]}
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    key = _api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(LOCAL_ENDPOINT, data=body, headers=headers)
    # Hosted endpoints get intermittently rate-limited (429s that clear in
    # seconds) — retry briefly, else one blip downgrades the whole message off
    # the masked-cloud path (or kills the draft entirely on the fallback path).
    for attempt in range(3):
        try:
            raw = urllib.request.urlopen(req, timeout=timeout).read().decode()
            return json.loads(raw)["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 2:
                raise
            time.sleep(3 * (attempt + 1))


def ner(text):
    """Return [(TYPE, surface), ...] for fuzzy entities (PERSON/ORG/ADDRESS).

    RAISES if the backend is unreachable/errors — the caller (Masker.seed) treats
    that as 'NER down' and routes the whole message to local generation instead of
    sending a partially-masked payload to the cloud. A successful-but-empty result
    returns []."""
    content = _chat(_NER_SYS, text[:6000], max_tokens=800, temperature=0, timeout=60)
    m = re.search(r"\[.*\]", content, re.S)
    if not m:
        return []
    arr = json.loads(m.group(0))
    return [(e["type"], e["text"]) for e in arr
            if e.get("type") in ("PERSON", "ORG", "ADDRESS") and e.get("text")]


def local_chat(system, user, max_tokens=1500, timeout=200):
    """Full generation on the private model — the fallback path when a message is
    too sensitive for the cloud (NER down, or hard PII survived masking). Raw text is
    fine here: it never leaves the local boundary. Returns '' on failure so the caller
    degrades exactly like a failed cloud_llm() call."""
    try:
        return (_chat(system, user, max_tokens=max_tokens,
                      temperature=0.4, timeout=timeout) or "").strip()
    except Exception:
        return ""
