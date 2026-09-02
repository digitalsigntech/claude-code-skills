#!/usr/bin/env python3
"""The fabrication invariant: numbers spoken that the agent never gave.

    python3 fabrication.py --selftest     # includes the 16:42 specimen

THE CASE THIS EXISTS FOR (2026-08-31, 16:41-16:42 UTC, one demo account). The
user asked for six months of sales; the relay worked, the agent answered
6,294,100 with months around a million each, and the real text was drawn on the
screen. The follow-up — "full details" — was never relayed, and the model
narrated a different universe: total 475,000, July 95,000, August 60,000. It had
the true figures in its own context and spoke different ones.

WHY THIS SHAPE AND NOT THE OTHER ONE. The invariant we first agreed was
"asserted his-world specifics with no tool call at all", which is the keystone
class. This is one layer in and much sharper: asserted specifics AGAINST a call
it already has. That version needs no guess about what the model could
legitimately know, because the agent's own answer is the ground truth sitting
right there — so the check is a comparison rather than a judgement, and a
comparison is the only kind of alarm worth waking anyone for.

WHAT IT DELIBERATELY DOES NOT DO:

  * It does not fire without a baseline. A model speaking numbers in a session
    where the agent gave none may be reading the manual, quoting the user, or
    counting something on the phone. No ground truth, no verdict.
  * It does not punish rounding. "About 6.3 million" for 6,294,100 is a correct
    summary, and an invariant that calls it a lie is an invariant people switch
    off. Anything within TOLERANCE of a figure the agent gave is supported.
  * It does not fire on one stray number. Fabrication of the kind seen here
    arrives as a whole invented table; a single unmatched value is far more
    likely to be arithmetic the model did on the real figures.

The cost of a false positive here is real: it accuses a working system of
lying, in the group, in front of the person deciding whether to trust it. So
every threshold below is set to miss some real cases rather than invent one.
"""
import argparse
import json
import os
import re
import sys
import time

STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "figure_ledger.json")

# Within 2% of a figure the agent gave counts as that figure: "6.3 million"
# for 6,294,100 is a summary, not a fabrication.
TOLERANCE = 0.02
# How long an answer stays usable as ground truth. A voice session is minutes;
# an hour is generous and keeps a stale morning figure from indicting an
# afternoon one.
WINDOW = 3600
# Below this, a number is a date, a count, a month index, a rating — not a
# quantity anyone fabricates a report out of.
FLOOR = 1000
# One unmatched number is arithmetic. Two or more is a different universe.
MIN_UNSUPPORTED = 2

_MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "mm": 1e6,
         "b": 1e9, "billion": 1e9}

# A DATE IS NOT A QUANTITY, and this cost a false positive nobody would have
# forgiven (2026-09-02, found from the app's own figure detector being stricter
# than mine). "Comparing 2025 and 2026 quarter by quarter" parsed as two
# numbers above the floor, neither of them in the baseline, which is exactly
# the shape this module announces as an invented table — an innocent sentence,
# accused in the group, by an alarm built to miss rather than invent.
#
# So: nothing glued to a date or time separator, and no bare four-digit year.
# The cost is a real quantity of exactly 2,026 units going unseen, which is the
# trade this whole module is written to prefer.
_NUM = re.compile(
    r"(?<![\w.])"
    r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(k|m|mm|b|thousand|million|billion)?\b",
    re.I)
_DATEY = re.compile(r"[-/:]")
YEAR_LO, YEAR_HI = 1900, 2100


def _is_datey(text, start, end):
    """Is this number glued to a date or time separator on either side?"""
    before = text[max(0, start - 1):start]
    after = text[end:end + 1]
    return bool((before and _DATEY.match(before))
                or (after and _DATEY.match(after)))


def figures(text):
    """Every quantity in a piece of text, normalised to a float.

    Thousands separators, currency and magnitude words all collapse here so
    that 6,294,100 / $6.29M / "6.29 million" are one number and not three.
    """
    text = text or ""
    out = []
    for m in _NUM.finditer(text):
        raw, suffix = m.group(1), m.group(2)
        try:
            val = float(raw.replace(",", ""))
        except ValueError:
            continue
        if suffix:
            val *= _MULT[suffix.lower()]
        elif ("," not in raw and "." not in raw
              and YEAR_LO <= val <= YEAR_HI):
            continue                       # a bare year, not a quantity
        if _is_datey(text, m.start(1), m.end(0)):
            continue                       # 2026-08-15, 14:00, 2026/0042
        if val >= FLOOR:
            out.append(val)
    return out


def _load():
    try:
        with open(STORE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(d):
    tmp = STORE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=1, sort_keys=True)
    os.replace(tmp, STORE)


def record_answer(account, question, answer, now=None):
    """Remember the figures in an answer this agent actually gave.

    Only the NUMBERS and the question are kept, never the answer body: the
    ledger's whole job is comparison, and a store of past answers on disk is a
    second copy of the conversation for no extra power.
    """
    vals = figures(answer)
    if not vals:
        return []
    now = now or time.time()
    d = _load()
    rows = [r for r in d.get(account, []) if now - r.get("at", 0) < WINDOW]
    rows.append({"at": now, "q": (question or "")[:120], "figures": vals})
    d[account] = rows[-20:]
    _save(d)
    return vals


def baseline(account, now=None):
    """Every figure this agent gave this account inside the window."""
    now = now or time.time()
    vals = []
    for row in _load().get(account, []):
        if now - row.get("at", 0) < WINDOW:
            vals.extend(row.get("figures") or [])
    return vals


def supported(value, base):
    """Is this number one the agent gave, allowing for a rounded retelling?"""
    return any(abs(value - b) <= TOLERANCE * max(abs(b), 1.0) for b in base)


def check_spoken(account, spoken, now=None, base=None):
    """A verdict on one model-spoken line from a turn that called nothing.

    Returns None when there is nothing to say — which is the common case and
    must stay cheap — or a dict describing the contradiction.
    """
    base = baseline(account, now) if base is None else base
    if not base:
        return None                       # no ground truth, no verdict
    spoken_vals = figures(spoken)
    if not spoken_vals:
        return None
    bad = [v for v in spoken_vals if not supported(v, base)]
    if len(bad) < MIN_UNSUPPORTED or len(bad) < len(spoken_vals):
        # Any supported figure at all means the model is working from the real
        # answer; a mixed line is a summary with a slip in it, not an invention,
        # and the two deserve different words.
        return None
    return {"account": account, "spoken": spoken_vals, "unsupported": bad,
            "baseline": sorted(set(base), reverse=True)[:8]}


def describe(verdict):
    """The announcement text — the numbers side by side and nothing else."""
    def n(v):
        return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.2f}"
    return (
        "🚨 *The model spoke numbers the agent never gave.*\n\n"
        f"*It said:* {', '.join(n(v) for v in verdict['unsupported'])}\n"
        f"*The agent's own answer, this session:* "
        f"{', '.join(n(v) for v in verdict['baseline'])}\n\n"
        "No tool call was made in that turn, and not one figure it spoke "
        "matches a figure it was given. The true answer was already on the "
        "screen above it.")


def selftest():
    """The real specimen, plus the cases that must stay silent."""
    now = time.time()
    real = ("Sales for the last six months came to 6,294,100 in total. "
            "March 1,048,200, April 1,102,600, May 987,400, June 1,051,900, "
            "July 1,043,500, August 1,060,500.")
    base = figures(real)
    cases = [
        ("THE SPECIMEN — invented table",
         "The total was 475,000. July came in at 95,000 and August at 60,000.",
         True),
        ("a rounded retelling is not a lie",
         "About 6.3 million over the six months, with July near 1.04 million.",
         False),
        ("exact retelling", "The total was 6,294,100.", False),
        ("one odd number among real ones",
         "6,294,100 in total, and 88,000 of that was returns.", False),
        ("no numbers at all", "I will pull that up for you.", False),
        ("small numbers are not quantities",
         "That covers 6 months across 3 regions.", False),
        ("two years are not two invented figures",
         "Comparing 2025 and 2026 quarter by quarter.", False),
        ("a date beside a real figure does not become one",
         "Total 6,294,100 as of 2026-08-15 at 14:00.", False),
        ("a reference number in a date shape is not a quantity",
         "Invoice 2026/0042 was raised on 2026-08-15.", False),
        ("one invented number alone is arithmetic, not invention",
         "That works out to 512,300.", False),
    ]
    ok = True
    print(f"baseline from the agent's real answer: {[int(b) for b in base]}\n")
    for name, spoken, want in cases:
        v = check_spoken("acct-test", spoken, now=now, base=base)
        got = v is not None
        print(f"{'FLAG ' if got else 'quiet'}  {name:<46} "
              f"{'ok' if got == want else 'WRONG'}")
        ok = ok and got == want
    print()
    v = check_spoken("acct-test", cases[0][1], now=now, base=base)
    print(describe(v))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--show", metavar="ACCOUNT")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.show:
        for row in _load().get(a.show, []):
            print(f"{time.strftime('%H:%M:%S', time.localtime(row['at']))}  "
                  f"{[int(v) for v in row['figures']]}  {row['q']}")
        return 0
    sys.exit(__doc__.strip())


if __name__ == "__main__":
    sys.exit(main())
