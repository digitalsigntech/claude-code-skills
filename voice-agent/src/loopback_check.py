"""An ear of sorts: speak an answer, then let the recogniser read it back.

I cannot hear the replies. But whisper can — and if Piper's output were garbled,
sped up, or fed nonsense text, the recogniser would not read back the words that
went in. It is not a judgement of how PLEASANT the voice is; it is an objective
check that the audio still contains the sentence.
"""
import difflib
import os
import re
import sys

sys.path.insert(0, os.environ.get("LQ_AGENT_DIR", os.path.dirname(os.path.abspath(__file__))))
import local_voice as lv

# Run as:  python3 loopback_check.py ["a sentence to try"]
CASES = [
    ("plain", "Wednesday September 2 had sales of 49,280 dollars."),
    ("codes, as it speaks them now",
     "Six presses PR-01 through PR-06. PR-01 Mark Andy Performance Series P5."),
    ("normalised forms",
     "Job PR-01 runs Wed-Fri at 14:30, and/or Sat. Total $1,048,200."),
]


def words(s):
    return re.findall(r"[a-z0-9]+", s.lower())


if len(sys.argv) > 1:
    CASES = [("given on the command line", " ".join(sys.argv[1:]))]

for label, answer in CASES:
    spoken = lv.say_text(answer)
    audio, secs, fmt, rate, _who = lv.speak(spoken, "en")
    heard, _, _, _ = lv.transcribe(audio, ".m4a", "en")
    ratio = difflib.SequenceMatcher(None, words(spoken), words(heard)).ratio()
    print(f"  {label}")
    print(f"    spoke : {spoken[:74]!r}")
    print(f"    heard : {heard[:74]!r}")
    print(f"    match : {ratio:.0%}   ({rate} Hz, {secs:.1f}s)")
