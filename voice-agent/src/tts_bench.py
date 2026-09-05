"""Kokoro against Piper: synthesis seconds, and can the recogniser read it back?

the owner, request 467: Piper is not good enough — what runs better? The two
questions that matter for this tier are what it COSTS on the agent's own CPU and
whether the words survive, so both are measured the way Piper already was.
"""
import difflib, os, re, subprocess, sys, time, wave
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import local_voice as lv
import soundfile as sf
from kokoro_onnx import Kokoro

HERE = os.path.dirname(os.path.abspath(__file__))
BENCH = os.environ.get("TTS_BENCH_DIR", os.path.join(HERE, "bench"))
TEXTS = [
    ("short", "Yes, go ahead."),
    ("normal", "Tuesday came in at 49,060 dollars, the best day of the week."),
    ("long", "No, every supplier note sitting in the inbox right now already "
             "has your reply logged behind it: Sanjay Bhatt at Innovia, Rhonda "
             "Estes at Mactac, and Cheryl Nakagawa at Durst, all answered same "
             "day. Nothing from a supplier is sitting open."),
]
w = lambda s: re.findall(r"[a-z0-9]+", s.lower())
PIPER = os.environ.get("LQ_PIPER_BIN", os.path.join(HERE, "venv", "bin", "piper"))
VOICE = os.environ.get("LQ_PIPER_VOICE", os.path.join(HERE, "voices", "en_US-lessac-medium.onnx"))
kok = Kokoro(os.environ.get("LQ_KOKORO_MODEL", f"{BENCH}/kokoro-v1.0.onnx"),
             os.environ.get("LQ_KOKORO_VOICES", f"{BENCH}/voices-v1.0.bin"))

print(f"  {'clip':<7} {'engine':<7} {'synth':>7} {'audio':>7} {'ch/s':>6} {'read back':>10}")
for tag, text in TEXTS:
    t = time.time()
    subprocess.run([PIPER, "-m", VOICE, "-f", f"{BENCH}/p_{tag}.wav"],
                   input=text, text=True, capture_output=True, check=True)
    p_synth = time.time() - t
    with wave.open(f"{BENCH}/p_{tag}.wav") as f:
        p_secs = f.getnframes() / float(f.getframerate())
    h, _, _, _ = lv.transcribe(open(f"{BENCH}/p_{tag}.wav", "rb").read(), ".wav", "en")
    p_score = difflib.SequenceMatcher(None, w(text), w(h)).ratio()

    t = time.time()
    samples, sr = kok.create(text, voice="af_heart", speed=1.0, lang="en-us")
    k_synth = time.time() - t
    sf.write(f"{BENCH}/k_{tag}.wav", samples, sr)
    k_secs = len(samples) / sr
    h, _, _, _ = lv.transcribe(open(f"{BENCH}/k_{tag}.wav", "rb").read(), ".wav", "en")
    k_score = difflib.SequenceMatcher(None, w(text), w(h)).ratio()

    for name, synth, secs, score in (("piper", p_synth, p_secs, p_score),
                                     ("kokoro", k_synth, k_secs, k_score)):
        print(f"  {tag:<7} {name:<7} {synth:6.2f}s {secs:6.2f}s "
              f"{len(text)/secs:6.1f} {score:9.0%}")
