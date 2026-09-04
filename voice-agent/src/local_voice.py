#!/usr/bin/env python3
"""LQ, the local voice tier: speech in, speech out, on this machine only.

    python3 local_voice.py --selftest [clip.wav]   # the real pipeline, timed
    python3 local_voice.py --probe                 # is this install capable?

WHAT THIS IS FOR (#390-#393). A turn arrives as a sealed envelope carrying
`{"voice": {"format": "aac", "b64": …}, "lang": …}`. This module turns that into
text, hands the text to the agent's ordinary ask path, speaks the answer, and
returns `{"text", "user_text", "voice": {"format", "b64"}}` — sealed again on the
way out. **No audio ever leaves this machine.** The relay carries ciphertext and
a duration; the speech provider is not in the path at all, because on this tier
there is no speech provider.

WHY DURATIONS TRAVEL IN THE CLEAR. The product bills seconds of audio, in
plus out, and you cannot measure what you cannot read. So the numbers ride
beside the envelope rather than inside it — the only plaintext in a turn, and
deliberately the most boring quantity in it.

THE AGENT IS THE HONEST METER, and that is not a compliment to the agent: it is
the only party that decodes BOTH directions. The phone knows what it recorded
and nothing about the reply; the relay knows neither. So this end reports both
and the phone's own figure becomes a cross-check rather than the source.

CAPABILITY, DECLARED ONLY WHEN TRUE. `probe()` decides — the binaries, the model
and a voice file all have to exist. A tier that advertises itself and then fails
is worse than one that says it is unavailable, and this project has now been
bitten four times by things that quietly did nothing.
"""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))

# Where the pieces live. Overridable per install, because the box has an iGPU
# build and a modest VPS will not.
WHISPER_BIN = os.environ.get(
    "LQ_WHISPER_BIN", os.path.expanduser("~/whisper.cpp/build-vulkan/bin/whisper-cli"))
WHISPER_MODEL = os.environ.get(
    "LQ_WHISPER_MODEL",
    os.path.expanduser("~/whisper.cpp/models/ggml-large-v3-turbo-q5_0.bin"))
PIPER_BIN = os.environ.get(
    "LQ_PIPER_BIN", os.path.join(HERE, "venv", "bin", "piper"))
PIPER_VOICE = os.environ.get(
    "LQ_PIPER_VOICE",
    os.path.join(HERE, "voices", "en_US-lessac-medium.onnx"))
FFMPEG = os.environ.get("LQ_FFMPEG", "ffmpeg")

# A turn is one push of a button. Longer than this is not a question, it is an
# accident — a pocket, a stuck button, or someone testing the meter.
MAX_AUDIO_S = float(os.environ.get("LQ_MAX_AUDIO_S", "120"))


def probe():
    """(ok, reasons) — is this install actually able to serve the tier?"""
    missing = []
    for label, path in (("whisper binary", WHISPER_BIN),
                        ("whisper model", WHISPER_MODEL),
                        ("piper binary", PIPER_BIN),
                        ("piper voice", PIPER_VOICE)):
        if not os.path.exists(path):
            missing.append(f"{label} not at {path}")
    if not shutil.which(FFMPEG):
        missing.append(f"{FFMPEG} is not on PATH")
    return (not missing), missing


def _duration(wav_path):
    with wave.open(wav_path) as w:
        return w.getnframes() / float(w.getframerate())


# SILENCE DOES NOT COME BACK AS A MARKER. It was supposed to: whisper emits
# [BLANK_AUDIO] and friends, and filtering those was the whole plan. Then I fed
# it two seconds of literal digital zero and it returned the word "you" —
# confidently, with no marker at all. Transcribers hallucinate on silence, so a
# LEXICAL test can only catch the polite half of this.
#
# The physical test cannot be fooled the same way: audio with no energy in it
# contains no speech whatever the model says about it. -50 dBFS peak is far
# below any real microphone in any real room, so a whisper still passes and a
# muted mic, a dead input or a stream of zeroes does not.
SILENCE_PEAK_DBFS = float(os.environ.get("LQ_SILENCE_PEAK_DBFS", "-50"))


def _peak_dbfs(wav_path):
    """Loudest sample in the clip, in dBFS. -inf for pure digital silence."""
    import array
    import math
    with wave.open(wav_path) as w:
        if w.getsampwidth() != 2:
            return 0.0                     # not 16-bit: do not judge it
        a = array.array("h")
        a.frombytes(w.readframes(w.getnframes()))
    peak = max((abs(x) for x in a), default=0)
    return -math.inf if peak == 0 else 20 * math.log10(peak / 32768.0)


def _to_wav16k(src, dst):
    """Whatever the phone sent -> 16 kHz mono WAV, which is what whisper eats."""
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", src,
                    "-ar", "16000", "-ac", "1", dst],
                   check=True, timeout=120)
    return _duration(dst)


# WHAT WHISPER SAYS WHEN NOBODY SPOKE. It does not return an empty string: it
# returns a bracketed token — [BLANK_AUDIO], [SILENCE], (music), *coughs* — and
# a token is text, so it was posted as the user's own words and answered by a
# model turn. Nine of those reached a real chat before anyone saw it (2026-09-04). A non-speech marker is the transcriber saying "there was
# nothing here", which is the opposite of something to answer.
_NON_SPEECH = re.compile(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*|♪+|\.{2,}")
# After the markers are gone: punctuation and stray single letters are not a
# question either. Two characters is the shortest thing worth relaying — "no",
# "ok", "да" — and anything under it is noise the recogniser could not resolve.
MIN_SPEECH_CHARS = 2


def speech_text(raw):
    """The words in a transcript, or "" when it contains none.

    Deliberately conservative in one direction: a line that is PART marker and
    part speech keeps the speech ("[BLANK_AUDIO] what were our sales" is a real
    question with a marker glued to it), and only a line with nothing left is
    treated as silence.
    """
    t = _NON_SPEECH.sub(" ", raw or "")
    t = " ".join(t.split()).strip(" .,!?-—…")
    return t if len(t) >= MIN_SPEECH_CHARS else ""


def transcribe(audio_bytes, suffix=".m4a"):
    """(text, seconds_of_audio, peak_dbfs). Raises on an absurdly long clip."""
    d = tempfile.mkdtemp(prefix="lq-")
    try:
        raw = os.path.join(d, "in" + suffix)
        wav = os.path.join(d, "in16k.wav")
        with open(raw, "wb") as f:
            f.write(audio_bytes)
        seconds = _to_wav16k(raw, wav)
        if seconds > MAX_AUDIO_S:
            raise ValueError(f"{seconds:.0f}s of audio is not a question "
                             f"(limit {MAX_AUDIO_S:.0f}s)")
        peak = _peak_dbfs(wav)
        if peak < SILENCE_PEAK_DBFS:
            # Do not even run the recogniser: there is nothing in the file, and
            # asking a model what a silence says is how you get "you".
            return "", seconds, peak
        out = subprocess.run(
            [WHISPER_BIN, "-m", WHISPER_MODEL, "-f", wav, "-nt", "-np"],
            capture_output=True, text=True, timeout=600)
        return " ".join(out.stdout.split()), seconds, peak
    finally:
        shutil.rmtree(d, ignore_errors=True)


# Piper writes 22 kHz 16-bit mono WAV — 44 KB per second, which base64 inflates
# to 59. MEASURED, NOT ASSUMED: my first working turn returned 490 KB of base64
# for eight and a half seconds of speech. That is eight times the whole turn
# budget the contract was written around and a quarter of the body ceiling I had
# just set, so "no transcode on the way out" was a decision made before anyone
# looked at the number. The reply is compressed.
REPLY_FORMAT = os.environ.get("LQ_REPLY_FORMAT", "aac")
REPLY_BITRATE = os.environ.get("LQ_REPLY_BITRATE", "32k")


def speak(text):
    """(audio_bytes, seconds, format) — spoken, then compressed for the wire."""
    d = tempfile.mkdtemp(prefix="lq-")
    try:
        wav = os.path.join(d, "out.wav")
        subprocess.run([PIPER_BIN, "-m", PIPER_VOICE, "-f", wav],
                       input=text, capture_output=True, text=True,
                       check=True, timeout=600)
        seconds = _duration(wav)
        if REPLY_FORMAT == "wav":
            with open(wav, "rb") as f:
                return f.read(), seconds, "wav"
        ext = {"aac": ".m4a", "opus": ".ogg"}.get(REPLY_FORMAT, ".m4a")
        codec = {"aac": ["-c:a", "aac"],
                 "opus": ["-c:a", "libopus"]}.get(REPLY_FORMAT, ["-c:a", "aac"])
        enc = os.path.join(d, "out" + ext)
        subprocess.run([FFMPEG, "-v", "error", "-y", "-i", wav, "-ac", "1",
                        "-ar", "16000", *codec, "-b:a", REPLY_BITRATE, enc],
                       check=True, timeout=120)
        with open(enc, "rb") as f:
            # THE DURATION COMES FROM THE WAV, not the encoded file: the meter
            # must not move because someone changed a bitrate.
            return f.read(), seconds, REPLY_FORMAT
    finally:
        shutil.rmtree(d, ignore_errors=True)


def turn(payload, answer_fn, on_transcript=None):
    """One LQ turn: the opened plaintext in, the plaintext reply out.

    `payload` is what was inside the envelope: {"voice": {"format", "b64"},
    "lang"}. `answer_fn(text) -> str` is the agent's ordinary ask path, so a
    spoken question and a typed one are answered by the same code and cannot
    drift apart.

    `on_transcript(text, ts)` fires THE MOMENT STT FINISHES and before the model
    is asked anything — the owner's rule: their own words belong on the screen
    while the answer is still being thought about, not after it arrives. It is
    called with the same timestamp the reply will carry, so the two halves of
    one turn sort together however they race. A failure in it is swallowed: a
    turn must not die because writing it down did.
    """
    voice = (payload or {}).get("voice") or {}
    b64 = voice.get("b64")
    if not b64:
        raise ValueError("no audio in the turn")
    fmt = (voice.get("format") or "aac").lower()
    suffix = {"aac": ".m4a", "m4a": ".m4a", "wav": ".wav",
              "ogg": ".ogg", "opus": ".ogg"}.get(fmt, ".m4a")
    t0 = time.time()
    ts = time.time()
    heard, secs_in, peak = transcribe(base64.b64decode(b64), suffix)
    user_text = speech_text(heard)
    t_stt = time.time() - t0
    if not user_text:
        # NOTHING WAS SAID: no bubble, no model turn, no speech back, and
        # nothing billed. The transcriber has told us there were no words, and
        # every step after this one would be work done on that non-answer —
        # including putting it in front of the person as their own sentence.
        return {"text": "", "user_text": "", "no_speech": True,
                "heard_marker": (heard or "")[:40],
                "peak_dbfs": (None if peak == float("-inf")
                              else round(peak, 1)),
                "audio_seconds_in": round(secs_in, 3),
                "audio_seconds_out": 0.0, "ts": ts,
                "timing": {"stt_s": round(t_stt, 2), "think_s": 0.0,
                           "tts_s": 0.0}}
    if on_transcript:
        try:
            on_transcript(user_text, ts)
        except Exception as e:
            print(f"[lq] posting the transcript failed: {e}", file=sys.stderr)
    answer = answer_fn(user_text)
    t1 = time.time()
    audio, secs_out, out_fmt = speak(answer or "")
    return {
        "text": answer,
        "user_text": user_text,
        "voice": {"format": out_fmt, "b64": base64.b64encode(audio).decode()},
        # Beside the envelope, in the clear, because the meter cannot read the
        # envelope. Rounded to milliseconds: a bill does not need more and a
        # float with sixteen digits invites someone to diff two of them.
        "audio_seconds_in": round(secs_in, 3),
        "audio_seconds_out": round(secs_out, 3),
        # The turn's own stamp, shared with the transcript posted above so the
        # two halves of one exchange sort together whichever arrives first.
        "ts": ts,
        "timing": {"stt_s": round(t_stt, 2),
                   "think_s": round(t1 - t0 - t_stt, 2),
                   "tts_s": round(time.time() - t1, 2)},
    }


def selftest(clip=None):
    ok, missing = probe()
    print("probe:", "ready" if ok else "NOT READY")
    for m in missing:
        print("  missing:", m)
    if not ok:
        return 1
    clip = clip or os.path.join(HERE, "test_en.wav")
    if not os.path.exists(clip):
        print("no clip to test with:", clip)
        return 1
    with open(clip, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    payload = {"voice": {"format": os.path.splitext(clip)[1].lstrip("."),
                         "b64": b64}, "lang": "en"}
    r = turn(payload, lambda t: f"You said: {t}")
    print(f"heard      : {r['user_text'][:90]}")
    print(f"answered   : {r['text'][:90]}")
    print(f"audio in   : {r['audio_seconds_in']}s   out: "
          f"{r['audio_seconds_out']}s")
    print(f"billable   : {r['audio_seconds_in'] + r['audio_seconds_out']:.2f}s "
          f"-> {(r['audio_seconds_in'] + r['audio_seconds_out']) / 60:.4f} min")
    print(f"timing     : {r['timing']}")
    print(f"reply audio: {len(r['voice']['b64']) // 1024} KB base64, "
          f"{r['voice']['format']}")
    return 0


def main():
    args = sys.argv[1:]
    if args and args[0] == "--probe":
        ok, missing = probe()
        print(json.dumps({"voice_local": ok, "missing": missing}, indent=1))
        return 0 if ok else 1
    if args and args[0] == "--selftest":
        return selftest(args[1] if len(args) > 1 else None)
    sys.exit(__doc__.strip())


if __name__ == "__main__":
    sys.exit(main())
