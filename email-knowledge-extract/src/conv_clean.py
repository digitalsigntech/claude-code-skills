#!/usr/bin/env python3
"""Strip a whole email conversation down to just the substantive new text.

This is the conversation-level cleaner used by the KB pipeline (extract.py packs
threads through `clean_conversation` before summarizing). It goes further than the
per-email `strip_quoted` in kbconf.py, because it sees the WHOLE thread at once:

  * drops internal agent/assistant chatter (messages to/from the agent accounts),
  * removes signatures / disclaimers that repeat across the thread -- matched at
    the WORD level, so it catches both the wrapped ">"-quoted form AND the single
    giant line Gmail produces by joining words with non-breaking spaces,
  * collapses newline runs to single spaces and repairs the missing spaces HTML
    ->text rendering leaves behind ("regards,AcmeSupport-Accounts"),
  * cuts the trailing "On <date>, <x> wrote:" quoted-reply history off each body.

Also runnable standalone on a conversation JSON:  python conv_clean.py in.json out.json
"""
import json, sys, re, collections

MIN_MSGS = 3      # a shingle must appear in this many messages to be boilerplate
K        = 10     # shingle length in words (length gate: short repeats survive)
MARKER   = " "    # deleted blocks become a single space (never fuse neighbours)

# Prefixes of internal agent/assistant accounts. Messages where any of these appear
# in from/to/cc are dropped from the conversation before summarizing. Match the local
# part(s) you use for automation accounts (see AGENT_ADDRESSES in kbconf.py).
DROP_ADDRS = ("agent@", "peer-agent@")

_WORD = re.compile(r"[^\s>|*\xa0​]+")       # a "word": run of non-separator chars
_URLish = re.compile(r"(https?://\S+|www\.\S+|<[^>\s]+>|\S+@\S+\.\S+)")

# Reply-attribution / forwarded-header markers that introduce quoted history.
# Dedup often strips the repeated "On <weekday>, <date> at" prefix, leaving a bare
# "<time> <Name> <email> wrote:" tail -- so we also anchor on "<email> wrote:".
_QUOTE_START = re.compile(
    r"\bOn\s+(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,.*?\bwrote:"     # "On Tue, 16 Jun 2026 ... wrote:"
    r"|(?:\d{1,2}:\d{2}\s*(?:AM|PM)\s+)?"                           # optional leading time "1:04 PM"
    r"(?:[A-Z][\w.'-]*\s+){0,4}"                                    # optional name words
    r"(?:&lt;|<)\s*[\w.+-]+@[\w.-]+\s*(?:&gt;|>)\s*wrote:"          # "<person@example.com> wrote:"
    r"|-+\s*Forwarded message\s*-+"                                 # "---------- Forwarded message ---------"
    r"|\bBegin forwarded message:",
    re.DOTALL,
)


def fix_spacing(text):
    """Re-insert spaces at glue boundaries left by HTML->text rendering, e.g.
    'Best regards,AcmeSupport-AccountsOn Wed' -> 'Best regards, Acme
    Support-Accounts On Wed'. URLs/emails are left untouched."""
    parts = _URLish.split(text)
    for i in range(0, len(parts), 2):          # even indices = non-URL segments
        s = parts[i]
        s = re.sub(r"(?<=[,.!?;:])(?=[A-Z])", " ", s)  # punctuation glue (caps only, keeps 20:41)
        s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)     # camel/word glue
        s = re.sub(r"(?<=[0-9])(?=[A-Z])", " ", s)     # digit->word glue, e.g. 2026Join
        parts[i] = s
    return "".join(parts)


def strip_trailing_quote(body):
    """Truncate at the earliest quoted-email marker so dangling fragments like
    'On Tue, 16 Jun ... wrote:Hi' are removed."""
    m = _QUOTE_START.search(body)
    return body[:m.start()].rstrip() if m else body


def words(body):
    """Return (normalized_word, start, end) spans over the original body."""
    return [(m.group().lower(), m.start(), m.end()) for m in _WORD.finditer(body)]


def find_boilerplate(bodies):
    """Set of boilerplate shingle keys (K-word tuples seen in >= MIN_MSGS bodies)."""
    seen = collections.defaultdict(set)
    for i, b in enumerate(bodies):
        toks = [w for w, _, _ in words(b)]
        for j in range(len(toks) - K + 1):
            seen[tuple(toks[j:j + K])].add(i)
    return {k for k, idxs in seen.items() if len(idxs) >= MIN_MSGS}


def dedupe_body(body, boiler, already_seen):
    """Delete spans of boilerplate shingles already seen in earlier messages;
    the first occurrence of each block is preserved. `already_seen` is updated."""
    toks = words(body)
    keys = [k for k, _, _ in toks]
    delete = [False] * len(body)
    this_keys = set()
    for j in range(len(toks) - K + 1):
        key = tuple(keys[j:j + K])
        if key not in boiler:
            continue
        this_keys.add(key)
        if key in already_seen:
            for c in range(toks[j][1], toks[j + K - 1][2]):
                delete[c] = True
    already_seen |= this_keys

    out, i, n = [], 0, len(body)
    while i < n:
        if delete[i]:
            out.append(MARKER)
            while i < n and delete[i]:
                i += 1
        else:
            out.append(body[i]); i += 1
    return "".join(out)


def clean_text(body):
    """The per-body finish pass (no cross-message dedup): newline collapse, HTML
    glue repair, whitespace tidy, trailing-quote removal."""
    body = re.sub(r"[\r\n]+", " ", body or "")
    body = fix_spacing(body)
    body = re.sub(r" {2,}", " ", body).strip()
    return strip_trailing_quote(body)


def _is_dropped(msg):
    blob = " ".join(str(msg.get(k, "")) for k in ("from", "to", "cc")).lower()
    return any(a in blob for a in DROP_ADDRS)


def _raw_body(msg):
    return msg.get("body_new") or msg.get("body") or ""


def clean_conversation(msgs, body_key="body_new"):
    """Strip a whole conversation. `msgs` is a list of records (oldest->newest)
    with from/to/cc and a body under 'body_new' or 'body'. Returns a NEW list with
    internal-agent messages dropped and each record's `body_key` set to cleaned text.
    Original records are not mutated."""
    kept = [m for m in msgs if not _is_dropped(m)]
    bodies = [_raw_body(m) for m in kept]
    boiler = find_boilerplate(bodies)
    seen, out = set(), []
    for m, raw in zip(kept, bodies):                 # in order -> first copy kept
        cleaned = clean_text(dedupe_body(raw, boiler, seen))
        rec = dict(m)
        rec[body_key] = cleaned
        out.append(rec)
    return out


def _main(inp, outp):
    with open(inp, encoding="utf-8") as f:
        msgs = json.load(f)
    n0 = len(msgs)
    before = sum(len(m.get("body", "")) for m in msgs)
    cleaned = clean_conversation(msgs, body_key="body")
    after = sum(len(m.get("body", "")) for m in cleaned)
    with open(outp, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, ensure_ascii=False, indent=2)
    print(f"messages: {n0} -> {len(cleaned)} (dropped {n0 - len(cleaned)} internal-agent)")
    print(f"body chars: {before} -> {after}  ({100*(before-after)//max(before,1)}% smaller)")


if __name__ == "__main__":
    a = sys.argv
    _main(a[1] if len(a) > 1 else "conversation.json",
          a[2] if len(a) > 2 else "conversation_deduped.json")
