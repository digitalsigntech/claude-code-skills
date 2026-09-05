#!/usr/bin/env python3
"""Does the spoken-text normaliser say what a person would?

    python3 test_say_text.py

WHY THIS EXISTS (2026-09-04, from the owner). LQ pronounced the slash in
"and/or" and read "Wed-Fri" as "wed fry". Both are punctuation a reader resolves
in silence and a synthesiser cannot, and the list of them is long enough that
fixing one by hand invites breaking another: the first version of the slash rule
turned "and/or" into "and or or", and expanding day names without checking the
capital would turn "the sun is out" into "the Sunday is out".

WHAT IS AND IS NOT TESTED. There is no espeak CLI on either machine and no ears
here, so this tests the TEXT handed to Piper, never the sound. The bet is that a
synthesiser says ordinary words correctly and symbols unpredictably; the job is
to hand it the first kind.
"""
import os
import sys

os.environ.setdefault("LQ_PIPER_VOICE", "/tmp/none.onnx")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import local_voice as lv                                    # noqa: E402

CASES = [
    # (written, spoken, why this line is here)
    ("and/or", "and or", "the reported case; must not become 'and or or'"),
    ("*Your reminders* (3 pending)", "Your reminders (3 pending)",
     "request 477: single-asterisk emphasis was spoken as 'asterisk'"),
    ("_note_ in snake_case_name, 2 * 3", "note in snake_case_name, 2 * 3",
     "paired emphasis only: a word-internal underscore or a bare star stays"),
    ("Tue/Thu", "Tuesday or Thursday", "slash between words is 'or'"),
    ("60 km/h", "60 km per hour", "slash after a quantity is 'per'"),
    ("3 sheets/min", "3 sheets per minute", "and the unit is spoken in full"),
    ("Wed-Fri", "Wednesday to Friday", "the reported case: not 'wed fry'"),
    ("Mon-Wed and Sat", "Monday to Wednesday and Saturday", "a range and a day"),
    ("Sep 4 to Oct 2", "September 4 to October 2", "months, already ranged"),
    ("10-15 units", "10 to 15 units", "a numeric range"),
    ("PO-26-0412", "P O, 2 6, 0 4 1 2", "a code is heard, not understood"),
    ("job PR-01 is finishing", "job P R, 0 1 is finishing", "code in a sentence"),
    ("$1,048,200", "1,048,200 dollars", "currency as a person says it"),
    ("$5.50 each", "5 dollars 50 each", "and with cents"),
    ("14:30", "2:30 PM", "24-hour clock"),
    ("00:15", "12:15 AM", "midnight"),
    ("09:45 start", "09:45 start", "a 12-hour time is left alone"),
    ("**Total** for Sep", "Total for September", "markdown goes too"),
    ("a well-known problem", "a well-known problem", "not every hyphen is a range"),
    ("the sun is out", "the sun is out", "lower case stays a noun"),
    ("we may ship it", "we may ship it", "'may' is a word before it is a month"),

    # EVERYTHING BELOW WAS A FALSE POSITIVE IN THE FIRST SHIPPED VERSION. The
    # table above covered the forms I imagined; these are the forms an actual
    # answer contains, and every one of them came out wrong an hour after the
    # rules went live.
    ("see spacerigs.io/bavaria/ for details",
     "see spacerigs.io/bavaria/ for details", "a URL is not two words with 'or'"),
    ("the file is at /opt/voice-agent/config.json",
     "the file is at /opt/voice-agent/config.json", "nor is a path"),
    ("https://example.com/a/b", "https://example.com/a/b", "nor a full URL"),
    ("email someone@example.com", "email someone@example.com",
     "an address survives untouched"),
    ("COVID-19 rules", "COVID-19 rules", "five capitals is a word, not a code"),
    ("about 1/2 of the run", "about one half of the run", "a fraction is spoken"),
    ("10/15 ratio", "10 over 15 ratio", "and any other number pair is 'over'"),
    ("24/7 support", "twenty-four seven support", "the idiom, not '24 or 7'"),
    ("the AB-1 and CD-2 plates", "the A B, 1 and C D, 2 plates",
     "two capitals and a number is still a code"),
]


def main():
    bad = 0
    for src, want, why in CASES:
        got = lv.say_text(src)
        ok = got == want
        bad += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {src!r:<26} -> {got!r:<34} {why}")
        if not ok:
            print(f"       wanted {want!r}")
    print(f"\n{len(CASES)} cases, {bad} failing")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
