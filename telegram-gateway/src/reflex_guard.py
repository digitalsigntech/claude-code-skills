#!/usr/bin/env python3
"""Shared test for "is this a REQUEST, or is somebody talking about the answer?"

Every keyword reflex has the same failure, and it has now been reported three
times about three different reflexes:

  2026-08-11, backups — "You said the backups are running well, but the last
  message here shows 2 failures" got the backup table again.
  2026-08-14, reminders — twice. A bug report about ANOTHER agent's reminders
  ("here is what I got: …") and a refusal ("I don't need to see reminders that I
  have here") both came back with the reader's own table, so a report about a
  different machine was answered with a list of their own rows. The owner had to
  censor the keyword — "why does the remind*** word trigger…" — to get a sentence
  through to the model at all.

The shape is always the same: NOUN plus a common verb, matched anywhere in the
sentence. But "show", "see", "any" and "all" are English filler — nearly every
sentence that mentions reminders contains one, so the noun alone is effectively
the trigger, and a reflex that fires on the noun cannot tell a question from a
complaint about its own last answer.

Fixing it once per reflex is how it came back twice. These are the guards, in
one place, for all of them.
"""
import re

# Somebody is discussing an exchange that already happened — quoting it, citing
# it, complaining about it, or reporting what a different agent did. Re-printing
# the table answers nobody and talks over the person.
META = re.compile(
    r"\byou (said|told|reported|claimed|showed|sent|gave)\b|"
    r"\b(last|previous|above|earlier|this|that) (message|table|reply|answer|"
    r"report|one|thing)\b|"
    r"\bhere('s| is| are)? what\b|\bwhat i got\b|\bi got\b|"
    r"\bi asked\b|\bi told\b|\basked (him|her|them|max|it|you)\b|"
    r"\bi am reporting\b|\bi'm reporting\b|\breporting (you|that)\b|"
    r"\binstead of\b|\bwithout reading\b|\btriggers?\b|\btrigger(ing|ed)\b",
    re.I)

# Explicitly NOT asking for it. A reflex that fires on "I don't need to see
# reminders" has read the words and missed the sentence.
NEGATED = re.compile(
    r"\b(do ?n'?t|does ?n'?t|did ?n'?t|no|not|never|stop|quit|avoid)\b[^.?!]{0,24}"
    r"\b(need|want|ask|asking|show|showing|send|sending|display|see|give)\b|"
    r"\bno need\b|\bnot asking\b|\bwithout\b",
    re.I)

# A question ABOUT the mechanism rather than a request for its output.
ABOUT_MECHANISM = re.compile(
    r"\b(why|how do|how does|how to|explain|what makes|what causes|"
    r"who wrote|where is|which file|what triggers)\b", re.I)


def talking_about_it(text):
    """True when the message discusses the reflex, its output, or another agent's
    output — rather than asking for this one's."""
    t = text or ""
    return bool(META.search(t) or NEGATED.search(t) or ABOUT_MECHANISM.search(t))


def near(text, noun_pattern, verb_pattern, words=3):
    """The verb has to be ABOUT the noun: within a few words either side, not
    merely somewhere in the same paragraph."""
    n, v = noun_pattern, verb_pattern
    gap = r"\W+(\w+\W+){0,%d}?" % words
    return bool(re.search(f"(?:{n}){gap}(?:{v})|(?:{v}){gap}(?:{n})", text or "", re.I))


def opens_with(text, verb_pattern):
    """The sentence starts like a request: optionally polite, then the verb.
    "Show me all pending reminders" yes; "…to show me reminders" no."""
    return bool(re.match(r"^\s*(please\s+|pls\s+|can you\s+|could you\s+|"
                         r"would you\s+|now\s+)*(?:%s)\b" % verb_pattern,
                         text or "", re.I))
