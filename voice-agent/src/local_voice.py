#!/usr/bin/env python3
"""LQ, the local voice tier: speech in, speech out, on this machine only.

    python3 local_voice.py --selftest [clip.wav]   # the real pipeline, timed
    python3 local_voice.py --probe                 # is this install capable?
    python3 local_voice.py --verify-voices         # does every voice SPEAK?

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
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
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
# LANGUAGE ID IS NOT TRANSCRIPTION, and paying transcription prices for it is
# what `-l auto` does: a second full encoder pass, 13.4s against 7.2s on a
# two-core agent. A smaller model can say WHICH language without being able to
# write down what was said — measured over ten languages on Max:
#
#     tiny   9/10   0.84s      base  10/10  1.82s      small  10/10  6.59s
#
# BASE, NOT TINY, AND THE REASON IS UKRAINIAN. Tiny called it Russian — the one
# confusion in this set that matters most, and on this pipeline a wrong `-l`
# does not fail, it translates (#419). It was also barely confident when right:
# Turkish at p=0.39, Dutch at p=0.49. Base was right ten times out of ten and
# never below p=0.97, for four seconds less than the full model.
WHISPER_DETECT_MODEL = os.environ.get(
    "LQ_WHISPER_DETECT_MODEL",
    os.path.join(os.path.dirname(WHISPER_MODEL), "ggml-base.bin"))
# Below this we do not believe the detector and pay for the full two-pass
# `auto`. Base's worst CORRECT answer was 0.97 and tiny's wrong one was 0.61,
# so this sits in the gap the measurement left rather than a round number.
DETECT_MIN_P = float(os.environ.get("LQ_DETECT_MIN_P", "0.85"))
# ENGLISH GETS THE ENGLISH MODEL, on CPU-only agents (2026-09-04).
# `small.en` spends its whole vocabulary on one language, so it is both faster
# and better at it — but ONLY for a turn we already know is English. It is an
# optimisation hanging off the multilingual model, never a replacement for it:
# the tier's language count still comes from WHISPER_MODEL, because a machine
# that also has small.en installed did not become English-only.
WHISPER_MODEL_EN = os.environ.get(
    "LQ_WHISPER_MODEL_EN",
    os.path.join(os.path.dirname(WHISPER_MODEL), "ggml-small.en.bin"))
FFMPEG = os.environ.get("LQ_FFMPEG", "ffmpeg")

# A turn is one push of a button. Longer than this is not a question, it is an
# accident — a pocket, a stuck button, or someone testing the meter.
MAX_AUDIO_S = float(os.environ.get("LQ_MAX_AUDIO_S", "120"))


# WHAT THIS INSTALL CAN ACTUALLY HEAR AND SAY (2026-09-04). The picker showed
# "14 languages" for the local tier while this machine ran `ggml-base.en` and
# one English voice — which is one language. The 14 was never a claim anyone
# made: the plane OMITS a count it does not know (#307, so a tier is never given
# a borrowed number) and the app then falls back to the interface languages we
# ship. Right for a cloud tier, wrong here, because a local tier's languages are
# a property of the FILES ON THIS DISK and of nothing else.
#
# A turn needs both halves. A model that only transcribes English cannot serve a
# Spanish question however many voices are installed, and a voice we do not have
# cannot answer one we transcribed. So the count is the INTERSECTION of what the
# transcriber hears and what the voices speak — the languages a whole turn can
# actually be had in.
WHISPER_MULTILINGUAL_LANGS = 99          # whisper.cpp's own documented figure


def has_gpu():
    """Is there an accelerator worth loading a large model onto?

    Deliberately crude and cheap: a Vulkan/CUDA whisper build present, or an
    NVIDIA or Metal device visible. A wrong YES costs a slow first turn; a wrong
    NO costs accuracy nobody notices. Neither is worth probing hardware for.
    """
    if "vulkan" in WHISPER_BIN or "cuda" in WHISPER_BIN.lower():
        return True
    if shutil.which("nvidia-smi"):
        return True
    return sys.platform == "darwin"


def preferred_model_name():
    """The recogniser this machine should run (2026-09-04).

    A CPU-only agent gets `small` MULTILINGUAL rather than `small.en`: the
    languages are worth more than the second or two the bigger vocabulary costs,
    and the English-only model was never a decision — it was the smallest thing
    that made the first turn work. An agent with an accelerator gets the
    99-language `large-v3-turbo`.

    This is the PRIMARY model, the one the language count is read from. A
    CPU-only agent also keeps `small.en` beside it and `model_for()` reaches for
    it on a turn already known to be English — an optimisation for one language,
    not a change of tier.
    """
    return "ggml-large-v3-turbo-q5_0.bin" if has_gpu() else "ggml-small.bin"


# A SHORT SENTENCE IN EACH, because "does this voice work" cannot be asked in
# English. Japanese and Chinese route through their own phonemizers, and those
# are exactly the two that failed.
_HELLO = {
    "en": "Hello.", "fr": "Bonjour.", "es": "Hola.", "de": "Hallo.",
    "it": "Ciao.", "pt": "Ol\u00e1.", "pl": "Cze\u015b\u0107.",
    "sv": "Hej.", "nl": "Hallo.", "tr": "Merhaba.",
    "ru": "\u0417\u0434\u0440\u0430\u0432\u0441\u0442\u0432\u0443\u0439\u0442\u0435.",
    "uk": "\u0412\u0456\u0442\u0430\u044e.",
    "zh": "\u4f60\u597d\u3002", "ja": "\u3053\u3093\u306b\u3061\u306f\u3002",
}


def _voice_files():
    d = os.path.dirname(PIPER_VOICE)
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(".onnx"))
    except OSError:
        names = [os.path.basename(PIPER_VOICE)]
    return d, names


def _code_of(name):
    code = name.split("-", 1)[0].split("_", 1)[0].lower()
    return code if len(code) == 2 else ""


def verify_voices(force=False):
    """{filename: {ok, err}} — proven by SYNTHESISING one word with each.

    WHY A FILE ON DISK IS NOT A LANGUAGE (2026-09-04, the third time this shape
    has cost a day). The row said fourteen because fourteen `.onnx` files were
    installed. Two of them could not speak: Japanese needs `pyopenjtalk` and
    Chinese needs `g2pW`, neither of which ships with piper, and both failed at
    the phonemizer with the audio file already opened and empty. Nothing in the
    count could ever have noticed — it was reading filenames.

    So the count is now the result of running each voice once. Cached against
    each file's size and mtime, because it costs a few seconds and the answer
    only changes when the files do.
    """
    d, names = _voice_files()
    cache_path = os.path.join(d, ".spoken.json")
    try:
        with open(cache_path) as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    out, changed = {}, False
    for n in names:
        full = os.path.join(d, n)
        try:
            st = os.stat(full)
            stamp = [int(st.st_size), int(st.st_mtime)]
        except OSError:
            continue
        old = cache.get(n)
        if old and not force and old.get("stamp") == stamp:
            out[n] = old
            continue
        changed = True
        tmp = tempfile.mkdtemp(prefix="lq-verify-")
        try:
            wav = os.path.join(tmp, "v.wav")
            r = subprocess.run(
                [PIPER_BIN, "-m", full, "-f", wav],
                input=_HELLO.get(_code_of(n), "Hello."), text=True,
                capture_output=True, timeout=120)
            ok = (r.returncode == 0 and os.path.exists(wav)
                  and os.path.getsize(wav) > 2000)
            err = ""
            if not ok:
                lines = [x for x in r.stderr.strip().splitlines() if x.strip()]
                for x in lines:
                    if "Error" in x or "error" in x:
                        err = x.strip()[:160]
                # The LAST line is `wave.Error: # channels not specified` every
                # time — piper opened the output before it failed. The useful
                # line is the phonemizer's, further up.
                if not err and lines:
                    err = lines[-1][:160]
        except (OSError, subprocess.SubprocessError) as e:
            ok, err = False, str(e)[:160]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        out[n] = {"ok": bool(ok), "err": err, "stamp": stamp}
    if changed:
        try:
            with open(cache_path, "w") as f:
                json.dump(out, f, indent=1, sort_keys=True)
        except OSError as e:
            print(f"[lq] voice check not cached: {e}", file=sys.stderr)
    return out


def _spoken_cache():
    """The cached verdicts, if they cover today's files. Never synthesises."""
    d, names = _voice_files()
    try:
        with open(os.path.join(d, ".spoken.json")) as f:
            cache = json.load(f)
    except (OSError, ValueError):
        return None
    for n in names:
        entry = cache.get(n)
        try:
            st = os.stat(os.path.join(d, n))
        except OSError:
            continue
        if not entry or entry.get("stamp") != [int(st.st_size),
                                               int(st.st_mtime)]:
            return None      # stale: a voice was added or replaced
    return cache


def _voice_locales(verified_only=True):
    """The languages this machine can actually answer in, as ISO codes.

    NOT the .onnx files on disk any more. Once a language could be spoken by
    Kokoro instead of Piper, counting filenames undercounted the box by four —
    it reported ten languages while offering voices in fourteen, because
    Japanese, Chinese, Italian and Portuguese have no Piper file there and never
    will. A language is available when some engine here has a voice for it,
    which is exactly what speakers_for() answers.
    """
    out = set()
    for code in _roster():
        if code.startswith("_") or len(code) != 2:
            continue
        if speakers_for(code):
            out.add(code)
    if out:
        return out
    # No roster at all: fall back to the filenames, which is what an install
    # with voices and no roster.json has always meant.
    d, names = _voice_files()
    cache = _spoken_cache() if verified_only else None
    for n in names:
        code = _code_of(n)
        if code and not (cache is not None
                         and not (cache.get(n) or {}).get("ok")):
            out.add(code)
    return out


def understands():
    """How many languages the RECOGNISER can hear, whatever we can answer in."""
    return (1 if os.path.basename(WHISPER_MODEL).endswith(".en.bin")
            else WHISPER_MULTILINGUAL_LANGS)


def languages():
    """(count, codes, why) — what a whole turn can be had in, on this install.

    Honest by construction: it reads the model filename and the voice files
    rather than a constant, so an install that adds a multilingual model and
    more voices starts reporting the truth without anyone editing a number.
    """
    voices = _voice_locales()
    english_only = os.path.basename(WHISPER_MODEL).endswith(".en.bin")
    if english_only:
        codes = sorted(voices & {"en"})
        why = (f"{os.path.basename(WHISPER_MODEL)} transcribes English only, "
               f"so English is the only language a turn can complete in")
        return len(codes), codes, why
    codes = sorted(voices)
    # TWO HONEST NUMBERS, and they are different. A multilingual recogniser
    # hears up to 99; a turn also has to be ANSWERED, and only the installed
    # voices can do that. Reporting the 99 alone would be the borrowed-number
    # mistake again, one tier along: true of half the pipeline and false of the
    # thing the user experiences.
    proven = _spoken_cache() is not None
    why = (f"{os.path.basename(WHISPER_MODEL)} understands up to "
           f"{WHISPER_MULTILINGUAL_LANGS} languages; a turn also needs a voice "
           f"to answer in, and {len(codes)} "
           f"{'is' if len(codes) == 1 else 'are'} "
           # INSTALLED IS NOT SPOKEN. Two of the fourteen were installed and
           # mute, so the word in the published reason now says which claim
           # this number is: one that was run, or one that was listed.
           + ("proven to speak" if proven else "installed but unverified"))
    return len(codes), codes, why


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
# model turn. Nine of those reached a real person's Telegram before anyone saw
# it (2026-09-04). A non-speech marker is the transcriber saying "there was
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


# THE ROSTER: which named speaker is which file, per language (#417).
#
# `anna` is not a file. It is a ROLE the user picked once, and every language
# fills it with a different model — because Piper's inventory is uneven and the
# person who chose "anna" in English should not lose their voice by asking a
# question in German. So the app sends an id, and this map, not the code,
# decides what speaks.
#
# It lives on disk beside the voices for the same reason the language count
# does: a roster in the source is a second copy of the truth, and the copies
# drift. Absent the file, behaviour is exactly what it was — one voice per
# language, picked by prefix.
ROSTER = os.environ.get("LQ_ROSTER", "")


def _roster():
    """{lang: {speaker_id: {"file", "speaker"?, "gender"?}}} — or {} if none."""
    path = ROSTER or os.path.join(os.path.dirname(PIPER_VOICE), "roster.json")
    try:
        with open(path) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def speakers_for(lang):
    """The ids offerable in a language, in roster order. [] when unrostered.

    OFFERABLE MEANS INSTALLED. The roster is the catalogue, not the inventory:
    it names a file for every language, and an install that fetched five voices
    has five. Listing an id whose file is absent puts a row in the picker that
    plays the wrong voice — the same shape as advertising fourteen languages
    and answering all of them in English.
    """
    code = (lang or "").strip().lower()[:2]
    r = _roster().get(code) or {}
    d = os.path.dirname(PIPER_VOICE)
    cache = _spoken_cache()
    kokoro = engine_for(code) == "kokoro"
    out = []
    for vid, entry in r.items():
        # WHICHEVER ENGINE SPEAKS THIS LANGUAGE HERE. Kokoro brought two ids
        # Piper never had — ja/maria and zh/leo — and a check that only looked
        # for an .onnx would have hidden them while their greetings sat on the
        # plane, which is a picker disagreeing with its own samples.
        if kokoro:
            # ONE ENGINE PER LANGUAGE, not one per voice. Offering `maria` in
            # Spanish from Piper while the other three come from Kokoro would
            # put two different synthesisers in one picker and leave her with
            # no sample, since the greetings are regenerated per language. If
            # Kokoro speaks this language, its voices are the whole list.
            if entry.get("kokoro"):
                out.append(vid)
            continue
        f = entry.get("file", "")
        if not f:
            continue
        f = f if os.path.isabs(f) else os.path.join(d, f)
        if not os.path.exists(f):
            continue
        # ...and mute is not installed either, which is how Japanese got counted.
        if cache is not None and not (cache.get(os.path.basename(f))
                                      or {}).get("ok"):
            continue
        out.append(vid)
    return out


def voices():
    """{"ids": [...], "by_lang": {lang: [ids]}} — what this install can offer.

    The app's picker shows `ids`; `by_lang` is what stops it offering `maria` in
    Turkish, where Piper's entire inventory is one male voice.
    """
    by_lang = {}
    for code in sorted(_roster()):
        if code.startswith("_"):
            continue
        ids = speakers_for(code)
        if ids:
            by_lang[code] = ids
    order = ["anna", "maria", "tom", "leo"]
    seen = {v for ids in by_lang.values() for v in ids}
    ids = [v for v in order if v in seen] + sorted(seen - set(order))
    # A PARTIAL INSTALL AND A THIN LANGUAGE LOOK IDENTICAL from the outside.
    # Mid-copy, this row said French had no voices and English had three;
    # finished, it says four each — and nothing in the numbers distinguished
    # "still arriving" from "this is all the engine has". Turkish really does
    # have one voice in the whole of Piper. So the row says which it is.
    have = sum(len(v) for v in by_lang.values())
    # WHAT THE ENGINES CAN FILL, not what the roster lists. Moving a language
    # to Kokoro strands the roles Kokoro has no voice for — Spanish maria,
    # three quarters of French — and counting those as missing said "42 of 47,
    # rises as the rest arrive" about voices that are never arriving.
    want = 0
    for code, entries in _roster().items():
        if code.startswith("_"):
            continue
        if engine_for(code) == "kokoro":
            want += sum(1 for e in entries.values() if e.get("kokoro"))
        else:
            want += sum(1 for e in entries.values() if e.get("file"))
    source = (f"{have} of {want} rostered voices installed on this machine"
              + ("" if have >= want else
                 " — a language offers only what is on this disk, so this "
                 "count rises as the rest arrive"))
    return {"ids": ids, "by_lang": by_lang, "source": source}


def voice_for(lang, speaker=None):
    """(onnx_path, speaker_index) for a language and an optional named speaker.

    Fourteen voices on disk and one hard-coded path is a tier that ADVERTISES
    fourteen languages and answers every one of them in English — which is what
    the first Russian turn did (2026-09-04). The count and the pipeline have to
    read the same directory.
    """
    code = (lang or "").strip().lower()[:2]
    d = os.path.dirname(PIPER_VOICE)
    who = (speaker or "").strip().lower()
    if code and who:
        roster_here = _roster().get(code) or {}
        entry = roster_here.get(who)
        if entry is None:
            # AN ID WE DO NOT KNOW IS NOT A DEFAULT, it is a disagreement. The
            # app began sending `speaker` on every turn with build 338; if its
            # ids ever stop matching this roster — a rename, a new voice, a
            # different case convention — every user quietly gets the language
            # default and nothing anywhere says why. Falling back is right;
            # falling back in silence is how it goes unnoticed for a week.
            print(f"[lq] no voice named {who!r} for {code} "
                  f"(roster has: {' '.join(roster_here) or 'nothing'}) — "
                  f"using the language default", file=sys.stderr)
        if entry and entry.get("file"):
            f = entry["file"]
            f = f if os.path.isabs(f) else os.path.join(d, f)
            if os.path.exists(f):
                return f, entry.get("speaker")
            # A ROSTERED VOICE THAT IS NOT INSTALLED IS A BROKEN PROMISE, and a
            # silent fallback to another voice is the same bug as answering
            # Russian in English. Say so, then fall back audibly in the log.
            print(f"[lq] rostered voice missing: {who}/{code} -> {f}",
                  file=sys.stderr)
    if not code:
        return PIPER_VOICE, None
    # The configured voice wins for its own language, so installing en_GB
    # alongside en_US does not silently re-cast the English one.
    if os.path.basename(PIPER_VOICE).lower().startswith(code + "_"):
        return PIPER_VOICE, None
    # An unnamed turn in a rostered language still gets the roster's first
    # voice, so the default and the picker's top entry are the same voice.
    first = (_roster().get(code) or {})
    for entry in first.values():
        f = entry.get("file", "")
        f = f if os.path.isabs(f) else os.path.join(d, f)
        if os.path.exists(f):
            return f, entry.get("speaker")
        break
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(".onnx"))
    except OSError:
        return PIPER_VOICE, None
    for n in names:
        if n.lower().startswith(code + "_"):
            return os.path.join(d, n), None
    return PIPER_VOICE, None


def model_for(code):
    """The recogniser for a turn in this language.

    English on a CPU-only agent gets `small.en`; everything else, and every
    accelerated agent, gets the model this install was configured with. A GPU
    agent runs large-v3-turbo, which beats small.en at English too, so swapping
    there would trade accuracy for nothing.
    """
    if (code == "en" and not has_gpu() and WHISPER_MODEL_EN
            and os.path.exists(WHISPER_MODEL_EN)
            and not os.path.basename(WHISPER_MODEL).endswith(".en.bin")):
        return WHISPER_MODEL_EN
    return WHISPER_MODEL


# THE LANGUAGE OF THE LAST TURN, per account, briefly.
#
# Hands-free (2026-09-04) turns one long question into a stream of short ones,
# and short audio is not reliably identifiable BY ANY MODEL HERE: 1.8s of
# Ukrainian is read as Russian by base at p=0.94 AND by small at p=0.95 — both
# wrong, both confident, so no threshold and no bigger model saves it. Half a
# second of "Да" is read as English, or Portuguese.
#
# But a conversation does not change language every utterance. The first turn is
# long enough to identify; the "yes" that follows it is not, and does not need
# to be. So a detection is remembered for a few minutes and a clip too short to
# trust inherits it.
SHORT_CLIP_S = float(os.environ.get("LQ_SHORT_CLIP_S", "2.5"))
RECENT_LANG_S = float(os.environ.get("LQ_RECENT_LANG_S", "600"))
_recent_lang = {}


def remember_lang(account, code):
    if code:
        _recent_lang[str(account or "default")] = (time.time(), code)


def recent_lang(account):
    ts, code = _recent_lang.get(str(account or "default"), (0, ""))
    return code if time.time() - ts < RECENT_LANG_S else ""


# ONE SPEECH JOB AT A TIME, per agent.
#
# The app began overlapping turns (2026-09-04): it keeps listening while a turn
# is out, and this server is a ThreadingHTTPServer, so two clips arrive and run
# at once on two cores that ONE whisper pass already saturates. Measured on Max:
#
#     1 at once   7.4s
#     2 at once  18.9s each      (serial would be 7.4 and 14.8)
#     3 at once  34.1s           (serial would be 22.2)
#
# Concurrency is not just no faster here, it is WORSE for everyone in the queue:
# the person who spoke first waits 18.9s instead of 7.4s so that the second
# person can also wait 18.9s. The lock covers the CPU-bound work only —
# recognising and speaking — and never the model call between them, which is
# somebody else's machine and may overlap freely.
_SPEECH_CPU = threading.Lock()


def detect_language(wav):
    """Which language is this, per the small model — or "" if we cannot say.

    Returns "" rather than a guess when the detector is absent or UNSURE, and
    the caller then falls back to whisper's own `-l auto`. A detector that
    invents a language is worse than none: forcing the wrong `-l` does not
    fail, it TRANSLATES (#419), which is the bug this whole path exists to
    avoid. Unsure is therefore a first-class answer, not a failure.
    """
    if not os.path.exists(WHISPER_DETECT_MODEL):
        return ""
    try:
        with _SPEECH_CPU:
            r = subprocess.run(
                [WHISPER_BIN, "-m", WHISPER_DETECT_MODEL, "-f", wav,
                 "-dl", "-nt"],
                capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[lq] language detection skipped: {e}", file=sys.stderr)
        return ""
    blob = (r.stdout or "") + (r.stderr or "")
    m = re.search(r"auto-detected language:\s*([a-z]{2})\s*"
                  r"\(p\s*=\s*([0-9.]+)\)", blob)
    if not m:
        return ""
    code, p = m.group(1), float(m.group(2))
    if p < DETECT_MIN_P:
        print(f"[lq] detector unsure ({code} p={p:.2f}) — falling back to the "
              f"full model", file=sys.stderr)
        return ""
    return code


def transcribe(audio_bytes, suffix=".m4a", lang=None, hint=""):
    """(text, seconds, peak_dbfs, language_heard). Raises on an absurd clip.

    The FOURTH value is not decoration. When the app sends no `lang` the
    recogniser auto-detects, and the answer still has to be SPOKEN in something:
    without carrying the detected code back out, an auto-detected Russian turn
    gets a Russian transcript and an English voice reading it.
    """
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
            return "", seconds, peak, (lang or "")[:2].lower()
        # -l MATTERS AND ITS DEFAULT IS "en". Without it whisper.cpp assumes
        # English, and a Russian clip came back TRANSLATED into English rather
        # than transcribed — "I can speak both Russian and Russian" — which
        # reads like a bad transcription and is actually the wrong task. The
        # turn's own language when the app sends one, `auto` otherwise.
        want = (lang or "").strip().lower()
        code = want[:2]
        if want == "auto" or code not in _voice_locales():
            # `auto` IS PART OF THE CONTRACT, not a value that happens to fall
            # through. A pinned language is a claim about what the person is
            # ABOUT to say, made from a setting; when it is wrong whisper does
            # not fail, it TRANSLATES — `-l en` on the Russian clip returns
            # "What were the sales last month?", fluent and plausible and not
            # what anyone said. Detection costs about seven seconds on a CPU
            # agent and is the price of never doing that silently.
            #
            # A language we cannot answer in also lands here: forcing one we
            # have no voice for buys a transcript we cannot reply to.
            code = "auto"
        if code == "auto":
            # TOO SHORT TO IDENTIFY: inherit the session's language rather than
            # ask a model a question it cannot answer. Measured on Max — 1.8s of
            # Ukrainian reads as Russian at p=0.94, and 0.53s of Russian reads
            # as English at p=0.61 — so this is not a threshold that needs
            # tuning, it is audio that does not contain the answer.
            if seconds < SHORT_CLIP_S and hint:
                print(f"[lq] {seconds:.2f}s is too short to identify — "
                      f"continuing in {hint}", file=sys.stderr)
                code = hint
            else:
                code = detect_language(wav) or "auto"
        model = model_for(code)
        with _SPEECH_CPU:
            out = subprocess.run(
                [WHISPER_BIN, "-m", model, "-f", wav, "-nt", "-np",
                 "-l", code, "-oj", "-of", os.path.join(d, "res")],
                capture_output=True, text=True, timeout=600)
        heard = code
        if code == "auto":
            try:
                with open(os.path.join(d, "res.json")) as f:
                    heard = str(json.load(f)["result"]["language"])[:2].lower()
            except (OSError, ValueError, KeyError, TypeError):
                heard = ""
        return " ".join(out.stdout.split()), seconds, peak, heard
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
# EMPTY MEANS PIPER'S OWN RATE, which is what #448 fixed and what should stay.
# Set LQ_REPLY_RATE=16000 to resample deliberately — asked for as an A/B when
# the phone's in-session playback was the leading suspect for the gibberish.
# It stays available because "is it the rate" should be answerable by flipping
# one variable rather than by argument, even though #484 found the cause
# elsewhere.
REPLY_RATE = os.environ.get("LQ_REPLY_RATE", "").strip()


# NOBODY SPEAKS FASTER THAN THIS. Above roughly forty characters a second the
# audio is not fast speech, it is a voice DROPPING most of the text: a Ukrainian
# model handed "Sales were 1,048,200." produced 0.44s where the English voice
# produced 2.91s, and the failure is silence rather than an accent. Normal
# speech, in every language here, sits near twelve to eighteen.
MAX_CHARS_PER_SECOND = float(os.environ.get("LQ_MAX_CPS", "40"))
# Above this it is not speech any more, it is a voice hurrying. Ordinary speech
# in every language here measured 9.6 to 18.9 characters a second.
RUSHED_CPS = float(os.environ.get("LQ_RUSHED_CPS", "22"))


# LONG INPUT IS SPOKEN IN PIECES, because one voice rushes it otherwise.
#
# 2026-09-04, from a real reply he called gibberish: `en_US-ryan-medium` given a
# 290-character sentence produced 12.1s of audio — 27 characters a second, where
# ordinary speech runs 12 to 18 — and the recogniser could read back 23% of it.
# The SAME text through `en_US-lessac-medium` came back 87%, and the same voice
# on the first sentence alone came back 94%. So it is not the text, not the
# encode and not the voice in general: it is this model given one very long
# phrase. Ending the phrase earlier fixed it (86% at 18 ch/s) and the rate is the
# tell every time.
#
# So Piper is called with pieces no longer than this, split where a sentence or
# a clause already ends, and the audio is joined.
MAX_PHRASE_CHARS = int(os.environ.get("LQ_MAX_PHRASE_CHARS", "180"))


def _phrases(text, limit=None):
    """Split into speakable pieces at the latest boundary that fits."""
    limit = limit or MAX_PHRASE_CHARS
    out, rest = [], " ".join((text or "").split())
    while len(rest) > limit:
        window = rest[:limit]
        cut = max(window.rfind(". "), window.rfind("? "), window.rfind("! "))
        if cut < limit // 3:
            cut = max(window.rfind("; "), window.rfind(": "),
                      window.rfind(", "))
        if cut < limit // 3:
            cut = window.rfind(" ")
        if cut <= 0:
            break
        out.append(rest[:cut + 1].strip())
        rest = rest[cut + 1:].lstrip()
    if rest:
        out.append(rest)
    return out


def _encode(d, wav, seconds, rate):
    """One wire format, whichever synthesiser produced the audio."""
    if REPLY_FORMAT == "wav":
        with open(wav, "rb") as f:
            return f.read(), seconds, "wav", rate
    ext = {"aac": ".m4a", "opus": ".ogg"}.get(REPLY_FORMAT, ".m4a")
    codec = {"aac": ["-c:a", "aac"],
             "opus": ["-c:a", "libopus"]}.get(REPLY_FORMAT, ["-c:a", "aac"])
    enc = os.path.join(d, "out" + ext)
    # THE SYNTHESISER'S OWN RATE, NOT THE RECOGNISER'S. This resampled every
    # reply to 16 kHz — the rate whisper wants on the way IN, applied by habit
    # on the way OUT — while the greeting clips kept 22.05 kHz. Same voice, same
    # 32 kbps, half the top of the band, and exactly the "replies sound worse
    # than the samples" that was reported (2026-09-04).
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", wav, "-ac", "1",
                    *(["-ar", REPLY_RATE] if REPLY_RATE else []),
                    *codec, "-b:a", REPLY_BITRATE, enc],
                   check=True, timeout=120)
    with open(enc, "rb") as f:
        # THE DURATION COMES FROM THE WAV, not the encoded file: the meter must
        # not move because someone changed a bitrate.
        return f.read(), seconds, REPLY_FORMAT, rate


# THE SYNTHESISER FOLLOWS THE HARDWARE, as the recogniser already does
# (2026-09-04). A CPU-only or AMD agent speaks Kokoro's languages with
# Kokoro and the rest with Piper; an NVIDIA agent is a later branch and not this
# one. Which language goes to which engine is DATA in the roster, not a rule
# here, because it is a judgement about voices and it will change.
KOKORO_MODEL = os.environ.get(
    "LQ_KOKORO_MODEL", os.path.expanduser("~/kokoro/kokoro-v1.0.onnx"))
KOKORO_VOICES = os.environ.get(
    "LQ_KOKORO_VOICES", os.path.expanduser("~/kokoro/voices-v1.0.bin"))
_kokoro = None


def kokoro_ready():
    if not (os.path.exists(KOKORO_MODEL) and os.path.exists(KOKORO_VOICES)):
        return False
    try:
        _kokoro_engine()
    except Exception as e:
        print(f"[lq] kokoro not usable here: {e}", file=sys.stderr)
        return False
    return True


# WHERE KOKORO IS INSTALLED, when it is not on the agent's own path. The agent
# runs the system python; kokoro-onnx pulls onnxruntime and its friends, which
# is not something to force into a distribution's site-packages on a box that is
# serving somebody. A venv beside it and one path entry keeps both clean.
KOKORO_SITE = os.environ.get("LQ_KOKORO_SITE", "")


def _kokoro_engine():
    global _kokoro
    if _kokoro is None:
        if KOKORO_SITE and KOKORO_SITE not in sys.path:
            sys.path.insert(0, KOKORO_SITE)
        from kokoro_onnx import Kokoro
        _kokoro = Kokoro(KOKORO_MODEL, KOKORO_VOICES)
    return _kokoro


def engine_for(lang):
    """Which synthesiser speaks this language on this machine."""
    code = (lang or "").strip().lower()[:2]
    want = (_roster().get("_engines") or {}).get(code, "piper")
    if want == "kokoro" and not kokoro_ready():
        return "piper"          # not installed here; the roster is a wish
    return want


def _kokoro_voice(lang, speaker):
    code = (lang or "").strip().lower()[:2]
    who = (speaker or "").strip().lower()
    here = _roster().get(code) or {}
    entry = here.get(who)
    if entry and entry.get("kokoro"):
        return entry["kokoro"]
    if who:
        # THE SAME DISAGREEMENT #446 MADE AUDIBLE FOR PIPER: an id this roster
        # lacks (or one Kokoro has no voice for) falls back to the language
        # default, and it must say so in the log or the picker "does nothing".
        print(f"[lq] no kokoro voice named {who!r} for {code} "
              f"(roster has: {' '.join(k for k, e in here.items() if e.get('kokoro')) or 'nothing'})"
              " — using the language default", file=sys.stderr)
    for vid, e in (_roster().get(code) or {}).items():
        if e.get("kokoro"):
            return e["kokoro"]
    return None


def _speak_kokoro(text, lang, speaker, wav):
    """Synthesise with Kokoro. Returns the voice id that spoke, or None if
    this turn cannot use it — ONE lookup, so a fallback is logged once."""
    voice = _kokoro_voice(lang, speaker)
    if not voice:
        return None
    import soundfile as sf
    code = (lang or "en").strip().lower()[:2]
    tag = {"en": "en-us", "ja": "ja", "pt": "pt-br", "zh": "cmn"}.get(code, code)
    with _SPEECH_CPU:
        samples, sr = _kokoro_engine().create(text, voice=voice, speed=1.0,
                                              lang=tag)
    sf.write(wav, samples, sr)
    return voice


def speak(text, lang=None, speaker=None):
    """(audio_bytes, seconds, format, sample_rate, voice) — spoken, compressed.

    THE LAST FIELD IS WHO SPOKE — "kokoro:am_michael" or a Piper file name —
    reported by the code that chose it, because the label used to be a second
    roster lookup that always named the Piper file whichever engine had spoken.

    THE RATE IS RETURNED because it took a side-by-side ffprobe of two files to
    discover that replies were resampled to 16 kHz while the greeting clips kept
    Piper's 22.05 kHz (#448). The parameter was in nobody's log, so "the replies
    sound worse than the samples" had nothing to check itself against.
    """
    d = tempfile.mkdtemp(prefix="lq-")
    try:
        wav = os.path.join(d, "out.wav")
        # THE ENGINE IS CHOSEN BEFORE ANY VOICE FILE IS LOOKED FOR. Resolving
        # the Piper roster first, "just in case", is what made the box's first
        # English turn log "rostered voice missing: leo/en" twice and then
        # label a Kokoro reply as en_US-lessac-medium.onnx (request 475): the
        # Piper files for Kokoro's languages are absent ON PURPOSE, and a
        # warning about them is not a warning, it is noise that hides one.
        if engine_for(lang) == "kokoro":
            try:
                kv = _speak_kokoro(text, lang, speaker, wav)
                if kv:
                    seconds = _duration(wav)
                    with wave.open(wav) as _w:
                        rate = int(REPLY_RATE) if REPLY_RATE else _w.getframerate()
                    return _encode(d, wav, seconds, rate) + ("kokoro:" + kv,)
            except Exception as e:
                # NEVER LOSE THE TURN TO A NEW ENGINE. Piper is installed,
                # proven and one line away; a synthesiser that fails should cost
                # a log line, not an answer.
                print(f"[lq] kokoro failed ({e}) — speaking with piper",
                      file=sys.stderr)
        model, sid = voice_for(lang, speaker)
        cmd = [PIPER_BIN, "-m", model, "-f", wav]
        if sid is not None:
            # A multi-speaker model is ONE file holding several people; without
            # -s it is always speaker 0, which would make two rostered ids the
            # same voice and nobody would hear the difference as a bug.
            cmd += ["-s", str(sid)]
        spoke_by = os.path.basename(model) + ("" if sid is None else f"#{sid}")
        pieces = _phrases(text)
        with _SPEECH_CPU:
            if len(pieces) == 1:
                subprocess.run(cmd, input=pieces[0], capture_output=True,
                               text=True, check=True, timeout=600)
            else:
                parts = []
                for n, piece in enumerate(pieces):
                    part = os.path.join(d, f"p{n}.wav")
                    subprocess.run(cmd[:-1] + [part], input=piece,
                                   capture_output=True, text=True,
                                   check=True, timeout=600)
                    parts.append(part)
                listing = os.path.join(d, "parts.txt")
                with open(listing, "w") as f:
                    for part in parts:
                        f.write(f"file '{part}'\n")
                subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "concat",
                                "-safe", "0", "-i", listing, "-c", "copy",
                                wav], check=True, timeout=120)
        seconds = _duration(wav)
        with wave.open(wav) as _w:
            rate = int(REPLY_RATE) if REPLY_RATE else _w.getframerate()
        # RUSHED IS NOT MUTE, AND IT HAS ITS OWN NUMBER. The gibberish of
        # 2026-09-04 was a voice speaking 27 characters a second where ordinary
        # speech runs 12 to 18 — audible, wrong, and invisible to every check
        # here because something WAS produced. The rate was the tell in every
        # experiment that found it, so it is now watched rather than
        # rediscovered. Logged only: the phrase splitting is the cure, and a
        # second guess at the voice would be a worse turn than a fast one.
        if (text.strip() and seconds > 0
                and MAX_CHARS_PER_SECOND > len(text) / seconds > RUSHED_CPS):
            print(f"[lq] {os.path.basename(model)} spoke {len(text)} chars in "
                  f"{seconds:.1f}s — {len(text) / seconds:.0f} a second, which "
                  f"is faster than speech", file=sys.stderr)
        # A VOICE THAT READ ALMOST NONE OF IT GETS ONE SECOND CHANCE. This
        # happens when the answer is in a different script from the voice —
        # the agent replying in English to a Ukrainian question — and the user
        # would otherwise receive half a second of nothing and no error
        # anywhere. The default voice is not necessarily right for the
        # language, but it is certainly better than silence.
        if (text.strip() and model != PIPER_VOICE
                and seconds > 0 and len(text) / seconds > MAX_CHARS_PER_SECOND):
            print(f"[lq] {os.path.basename(model)} read {len(text)} chars in "
                  f"{seconds:.2f}s — speaking it with the default voice",
                  file=sys.stderr)
            subprocess.run([PIPER_BIN, "-m", PIPER_VOICE, "-f", wav],
                           input=text, capture_output=True, text=True,
                           check=True, timeout=600)
            seconds = _duration(wav)
        return _encode(d, wav, seconds, rate) + (spoke_by,)
    finally:
        shutil.rmtree(d, ignore_errors=True)


# WHAT TO SAY OUT LOUD WHEN THE ANSWER IS A TABLE (2026-09-04). Asked for a
# sales table, LQ recited the whole thing — every month, every figure, aloud,
# while the same table sat drawn on the screen. That is the mistake `show_chart`
# already names from the other direction: a table exists so the numbers do NOT
# have to be listened to.
#
# The spoken line is DERIVED, never invented. Tables are removed and what is
# left is the model's own prose; if that is nothing, the fallback names the
# shape and points at the screen. No figure is ever summarised into speech that
# the model did not itself write, because a spoken number nobody wrote is the
# fabrication shape with a microphone.
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_CODE_FENCE = re.compile(r"^\s*```")
SPEECH_MAX_CHARS = 400


# WHAT A VOICE SHOULD NOT READ ALOUD. Markdown is punctuation for the eye:
# `**Needs a decision:**` is emphasis on screen and, spoken, either two stray
# asterisks or nothing at all where a person would have changed their tone.
# Bullets are worse — a dash at the start of eight lines is a list to look at
# and a stutter to listen to.
_MD_NOISE = re.compile(r"\*\*|__|`+|^\s*[-*+]\s+|^\s*#{1,6}\s+", re.M)
# SINGLE-ASTERISK EMPHASIS IS ALSO MARKUP. `*Your reminders* (3 pending)`
# is how the reminders table is headed, and on the box's first reminders turn
# the synthesiser said "asterisk" twice (request 477). Paired, tight against
# the words, not inside a word: "2 * 3" and snake_case stay as they are.
_MD_EMPH = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])"
                      r"|(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])")


# HOW A PERSON WOULD SAY IT (2026-09-04, from the owner: LQ pronounced the
# slash in "and/or" and read "Wed-Fri" as "wed fry"). A recogniser's output and
# a screen's text are written for the EYE; every one of these is punctuation
# that a reader resolves silently and a synthesiser cannot.
#
# I cannot verify the PRONUNCIATION from here — there is no espeak CLI on either
# machine and I have no ears — so what is tested is the TEXT handed to Piper.
# The bet, and it is a fair one, is that a synthesiser says ordinary words
# correctly and symbols unpredictably; this turns the second into the first.
_DAYS = {"mon": "Monday", "tue": "Tuesday", "tues": "Tuesday",
         "wed": "Wednesday", "thu": "Thursday", "thur": "Thursday",
         "thurs": "Thursday", "fri": "Friday", "sat": "Saturday",
         "sun": "Sunday"}
_MONTHS = {"jan": "January", "feb": "February", "mar": "March",
           "apr": "April", "jun": "June", "jul": "July", "aug": "August",
           "sep": "September", "sept": "September", "oct": "October",
           "nov": "November", "dec": "December"}
_PER_UNITS = {"h", "hr", "hrs", "hour", "hours", "min", "mins", "minute",
              "minutes", "s", "sec", "secs", "second", "seconds", "day",
              "days", "week", "weeks", "month", "months", "year", "years",
              "km", "mi", "kg", "lb", "lbs", "ft", "sqft", "unit", "units",
              "page", "pages", "sheet", "sheets"}
# TWO OR THREE CAPITALS, not any word with a number after it. The first
# version took 2-5 letters and turned COVID-19 into "C O V I D, 1 9".
_CODE = re.compile(r"\b([A-Z]{2,3})-(\d[\d-]*)\b")
# LEAVE THESE ALONE ENTIRELY. A URL, a path and an address are full of the
# very characters every rule below claims, and none of them mean what the rule
# thinks: "spacerigs.io/bavaria/" came out as "spacerigs.io or bavaria or".
# Masked before the rules run and restored after, which is the only way a rule
# cannot reach inside them by accident.
_OPAQUE = re.compile(r"(?:https?://\S+|www\.\S+|\S+@\S+\.\S+"
                     r"|/[\w.-]+(?:/[\w.-]+)+/?|\b[\w-]+\.[a-z]{2,}/\S*)")
# More codes than this in one answer is a list, not a reference to write down.
SPELL_MAX_CODES = 2
_FRACTION = {"1/2": "one half", "1/3": "one third", "2/3": "two thirds",
             "1/4": "one quarter", "3/4": "three quarters"}
_NUM_SLASH = re.compile(r"(?<![\w/])(\d+)\s*/\s*(\d+)(?![\w/])")
# The trailing period goes WITH the abbreviation: "Aug. 18" expanded to
# "August. 18" and a full stop in the middle of a date is a sentence break to a
# synthesiser.
_ABBR = re.compile(r"\b([A-Za-z]{3,5})\.(?=\s|$)|\b([A-Za-z]{3,5})\b")
_RANGE_WORD = re.compile(r"\b(" + "|".join(sorted(
    set(list(_DAYS.values()) + list(_MONTHS.values())))) +
    r")\s*-\s*(" + "|".join(sorted(
        set(list(_DAYS.values()) + list(_MONTHS.values())))) + r")\b")
_RANGE_NUM = re.compile(r"(?<![\w-])(\d[\d,.]*)\s*-\s*(\d[\d,.]*)(?![\w-])")
_SLASH = re.compile(r"(?<=\w)\s*/\s*(?=\w)")
_MONEY = re.compile(r"\$\s*(\d[\d,]*)(?:\.(\d{2}))?")
_TIME24 = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")


def _spell(token):
    """`PO-26-0412` -> `P O, 2 6, 0 4 1 2`. A code is heard, not understood."""
    out = []
    for group in token.split("-"):
        if not group:
            continue
        out.append(" ".join(group.upper() if group.isalpha() else group))
    return ", ".join(out)


def _say_slash(m, text):
    return " per " if False else " or "


def say_text(text):
    """Turn what is written for the eye into what a voice can read aloud."""
    t = _MD_NOISE.sub(" ", text or "")
    t = _MD_EMPH.sub(lambda m: m.group(1) or m.group(2), t)
    # Park the things no rule should touch, and put them back at the end.
    kept = []

    def _park(m):
        kept.append(m.group(0))
        return f"\x00{len(kept) - 1}\x00"
    t = _OPAQUE.sub(_park, t)
    t = re.sub(r"\b24\s*/\s*7\b", "twenty-four seven", t)
    t = _NUM_SLASH.sub(
        lambda m: _FRACTION.get(f"{m.group(1)}/{m.group(2)}",
                                f"{m.group(1)} over {m.group(2)}"), t)
    # Codes first: they contain hyphens and digits that every later rule would
    # otherwise claim as a range or a quantity.
    #
    # BUT SPELLING IS FOR ONE CODE, NOT FOR A CATALOGUE. Reading "PO-26-0412"
    # as characters is right when someone has to write it down. Applied to an
    # answer listing six presses — PR-01 through PR-06, each named twice — it
    # produced "P R, 0 1 ... P R, 0 2 ..." for four hundred characters, which
    # the owner heard as gibberish and was right to (2026-09-04). Above a
    # handful, the hyphen simply becomes a space and the code is read as words
    # and numbers, which is how a person reads a list aloud.
    codes = _CODE.findall(t)
    if len(codes) > SPELL_MAX_CODES:
        t = _CODE.sub(lambda m: m.group(0).replace("-", " "), t)
    else:
        t = _CODE.sub(lambda m: _spell(m.group(0)), t)

    def _abbr(m):
        w = m.group(1) or m.group(2)
        # CAPITALISED ONLY. "Sun" is a day and "sun" is a noun; "Mar" is a month
        # and "mar" is a verb. Expanding either without the capital turns "the
        # sun is out" into "the Sunday is out", which is a worse sentence than
        # the one this rule exists to fix.
        if not w[:1].isupper():
            return m.group(0)
        k = w.lower()
        return _DAYS.get(k) or _MONTHS.get(k) or m.group(0)
    t = _ABBR.sub(_abbr, t)
    t = _RANGE_WORD.sub(r"\1 to \2", t)
    t = _RANGE_NUM.sub(r"\1 to \2", t)

    # A SLASH IS TWO DIFFERENT WORDS. Between plain words it means "or";
    # after a quantity it means "per". And when the word on the right IS the
    # connector — "and/or" — saying it again gives "and or or", which is how
    # the first version of this read aloud.
    def _slash(m):
        after = t[m.end():].split()
        nxt = (after[0].strip(".,;:").lower() if after else "")
        if nxt in ("or", "and"):
            return " "
        return " per " if nxt in _PER_UNITS else " or "
    t = _SLASH.sub(_slash, t)
    # "per h" is not something anyone says.
    t = re.sub(r"\bper h\b", "per hour", t)
    t = re.sub(r"\bper hr\b", "per hour", t)
    t = re.sub(r"\bper min\b", "per minute", t)
    t = re.sub(r"\bper sec?\b", "per second", t)
    t = _MONEY.sub(lambda m: (f"{m.group(1)} dollars {m.group(2)}"
                              if m.group(2) else f"{m.group(1)} dollars"), t)

    def _time(m):
        h, mi = int(m.group(1)), m.group(2)
        if h == 0:
            return f"12:{mi} AM"
        if h > 12:
            return f"{h - 12}:{mi} PM"
        return m.group(0)
    t = _TIME24.sub(_time, t)
    t = re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], t)
    # A SPELLED CODE ENDS IN A COMMA and the sentence usually had one of its
    # own: "the PR-01, a Performance Series" became "the P R, 0 1 , a
    # Performance Series" — a space before a comma, which is an extra breath
    # where there should be none.
    t = re.sub(r"\s+([,.;:!?])", r"\1", t)
    t = re.sub(r"([,.;:!?])\1+", r"\1", t)
    return " ".join(t.split())


def _speakable(text):
    return say_text(text)


# The model's own signal that this one is to be heard entire (#460).
READ_IN_FULL = "[read-in-full]"


def read_in_full(text):
    """(stripped_text, speak_it_all) — the marker never reaches the screen."""
    t = (text or "").lstrip()
    if t[:len(READ_IN_FULL)].lower() == READ_IN_FULL:
        return t[len(READ_IN_FULL):].lstrip(), True
    return text, False


def speech_for(text):
    """A short line to SAY for an answer whose body belongs on the screen.

    Returns "" when the whole answer is already speakable — the caller then
    speaks the answer itself and nothing changes for ordinary turns.
    """
    lines = (text or "").splitlines()
    kept, in_fence, had_table = [], False, False
    for ln in lines:
        if _CODE_FENCE.match(ln):
            in_fence = not in_fence
            had_table = True
            continue
        if in_fence:
            continue
        if _TABLE_ROW.match(ln):
            had_table = True
            continue
        kept.append(ln)
    if not had_table:
        # NO TABLE IS NOT THE SAME AS SHORT (2026-09-04, from the first
        # build-338 turns in the field). A 957-character answer — bold headings, eight
        # bullets, no table anywhere — was read aloud for EIGHTY-SIX SECONDS,
        # longer than the fifty this whole mechanism was built to stop. The
        # structure that triggered it was never the point; the LENGTH was.
        #
        # Nothing is lost: the full answer is on the screen either way, and the
        # spoken version says so rather than stopping mid-thought.
        prose = _speakable(text)
        if len(prose) <= SPEECH_MAX_CHARS:
            return ""                 # short enough to say as it stands
        cut = prose[:SPEECH_MAX_CHARS]
        stop = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
        if stop > SPEECH_MAX_CHARS // 3:
            cut = cut[:stop + 1]
        return cut + " The rest is on your screen."
    prose = _speakable(" ".join(kept))
    if len(prose) >= MIN_SPEECH_CHARS:
        return prose[:SPEECH_MAX_CHARS]
    return "It is on your screen."


# THE SAME CLIP TWICE IS A RETRY, NOT A SECOND QUESTION.
#
# 2026-09-04, from his own session: one 31.3-second clip arrived twice, sixteen
# seconds apart, identical to the tenth of a decibel in peak level. It was
# transcribed twice, answered twice — with two DIFFERENT answers — and both
# questions and both answers landed in his chat. On a two-core box that is
# thirteen seconds of recogniser spent to produce a duplicate, while the lock
# holds everyone else up behind it.
#
# Byte-identical audio from one account inside two minutes is a resend. The
# answer already given is returned again, which is also the only response that
# keeps the two chats consistent.
REPLAY_WINDOW_S = float(os.environ.get("LQ_REPLAY_WINDOW_S", "120"))
_recent_turns = {}
# AND A RETRY THAT ARRIVES BEFORE THE FIRST HAS FINISHED. The cache above only
# helps once there is an answer to replay; the app now retries a turn whose
# network vanished mid-flight (2026-09-04), and such a retry lands while the
# original is still being recognised — nothing cached, both run, two answers.
# Measured before this existed: two identical clips 0.3s apart produced two
# model calls and two different answers, which is the duplicate of #482 all
# over again by another route.
#
# So a turn in progress is announced and a duplicate waits for it. The wait
# never holds the speech lock: it happens before any of the work.
_inflight = {}
_inflight_guard = threading.Lock()
INFLIGHT_WAIT_S = float(os.environ.get("LQ_INFLIGHT_WAIT_S", "180"))


def _replay_key(account, audio):
    return f"{account or 'default'}\x00{hashlib.sha256(audio).hexdigest()}"


def turn(payload, answer_fn, on_transcript=None, account=None):
    """One LQ turn, answered once however many times it is asked."""
    voice = (payload or {}).get("voice") or {}
    b64 = voice.get("b64")
    if not b64:
        raise ValueError("no audio in the turn")
    raw_audio = base64.b64decode(b64)
    key = _replay_key(account, raw_audio)

    seen_at, seen_out = _recent_turns.get(key, (0.0, None))
    if seen_out is not None and time.time() - seen_at < REPLAY_WINDOW_S:
        print(f"[lq] the same clip again after {time.time() - seen_at:.0f}s — "
              f"returning the first answer rather than recognising it twice",
              file=sys.stderr)
        return seen_out

    with _inflight_guard:
        waiting = _inflight.get(key)
        first = waiting is None
        if first:
            _inflight[key] = waiting = threading.Event()
    if not first:
        print("[lq] the same clip is already being answered — waiting for that "
              "turn rather than starting a second", file=sys.stderr)
        waiting.wait(INFLIGHT_WAIT_S)
        seen_at, seen_out = _recent_turns.get(key, (0.0, None))
        if seen_out is not None:
            return seen_out
        # It failed or timed out. Answer rather than leave them with nothing:
        # a slow answer beats a dropped one.
        with _inflight_guard:
            _inflight[key] = waiting = threading.Event()
    try:
        out = _run_turn(payload, answer_fn, on_transcript, account, key,
                        raw_audio)
        _recent_turns[key] = (time.time(), out)
        return out
    finally:
        with _inflight_guard:
            _inflight.pop(key, None)
        waiting.set()


def _run_turn(payload, answer_fn, on_transcript, account, key, raw_audio):
    """One LQ turn: the opened plaintext in, the plaintext reply out.

    `payload` is what was inside the envelope: {"voice": {"format", "b64"},
    "lang"}. `answer_fn(text) -> str` is the agent's ordinary ask path, so a
    spoken question and a typed one are answered by the same code and cannot
    drift apart.

    `on_transcript(text, ts)` fires THE MOMENT STT FINISHES and before the model
    is asked anything — the owner's rule: his own words belong on the screen
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
    lang = str((payload or {}).get("lang") or "").strip().lower()[:5]
    # "auto" IS A REQUEST, NOT A LANGUAGE, and it is truthy — which is how it
    # reached `speak(text, lang or heard_lang)` and won, so `voice_for("auto")`
    # found no voice for "au" and answered a Ukrainian question in English.
    # That is #419 exactly, reintroduced by the contract that fixed it. Found by
    # running a WHOLE TURN rather than the recogniser on its own.
    if lang == "auto":
        lang = ""
    speaker = str((payload or {}).get("speaker") or "").strip().lower()[:32]
    heard, secs_in, peak, heard_lang = transcribe(
        raw_audio, suffix, lang, recent_lang(account))
    # Remember what this account is speaking, so the one-word answers that
    # follow a question do not each have to be identified from scratch.
    remember_lang(account, lang or heard_lang)
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
    # THE MODEL ASKED FOR THE WHOLE THING. The cap is for conversation; when the
    # person said "read me the email", cutting it at four hundred characters and
    # saying "the rest is on your screen" answers a question nobody asked.
    answer, speak_all = read_in_full(answer)
    t1 = time.time()
    # SPEAK THE SUMMARY, SHOW THE TABLE. `speech` travels beside `text` so the
    # app can mute the karaoke when the two differ — the highlight follows the
    # voice, and the voice is no longer reading the thing on screen.
    spoken_line = "" if speak_all else speech_for(answer or "")
    # STRIPPED WHETHER OR NOT IT WAS SUMMARISED. `speech_for()` cleans the
    # line it builds, but a short answer returns "" from it and went to Piper
    # RAW — so "**Total** 226,220" kept its asterisks and the voice read them.
    # The cleaning belongs at the point of speaking, which is the only place
    # every path passes through.
    # NOTHING TO SAY IS NOT NOTHING TO DO. piper exits non-zero on empty input,
    # which reached the app as a bare `voice_turn_failed` 400 — so a model that
    # returned no text turned into "the voice service is broken" on the phone.
    # The person did speak; their transcript is already up; the agent owes them
    # a sentence about its own silence rather than an error code. These are the
    # agent's words, never the model's, and they are the only words in this
    # file that were not said by somebody.
    to_say = _speakable(spoken_line or answer or "")
    if not to_say.strip():
        to_say = "I do not have an answer for that."
    audio, secs_out, out_fmt, out_rate, spoke_by = speak(
        to_say, lang or heard_lang, speaker)
    out = {
        "text": answer,
        **({"speech": spoken_line} if spoken_line else {}),
        "user_text": user_text,
        # The language the turn actually RAN IN — the app's `lang` when it sent
        # one, otherwise what the recogniser detected. It is the field that says
        # which voice you are hearing, and it is how a caller can tell that a
        # language it asked for was not one this install can answer in.
        **({"lang": lang or heard_lang} if (lang or heard_lang) else {}),
        # The voice that actually spoke, which is not always the one asked for:
        # an id this install has no file for falls back, and the app should be
        # able to see that rather than infer it from the sound.
        **({"speaker": speaker} if speaker else {}),
        "voice": {"format": out_fmt, "b64": base64.b64encode(audio).decode()},
        # Beside the envelope, in the clear, because the meter cannot read the
        # envelope. Rounded to milliseconds: a bill does not need more and a
        # float with sixteen digits invites someone to diff two of them.
        "audio_seconds_in": round(secs_in, 3),
        "audio_seconds_out": round(secs_out, 3),
        # The turn's own stamp, shared with the transcript posted above so the
        # two halves of one exchange sort together whichever arrives first.
        "ts": ts,
        # HOW LOUD IT WAS, on a turn that DID produce words. Reported only for
        # the silent ones until now, which is the wrong way round: silence is
        # already explained. The unexplained case is a short transcript out of
        # very quiet audio — whisper's documented habit of hallucinating a bare
        # "you" or "Thank you." on near-silence (#433). A bare word is not a
        # marker, so no filter here catches it; this at least makes it
        # IDENTIFIABLE in the log the first time someone reports a phantom.
        "peak_dbfs": (None if peak == float("-inf") else round(peak, 1)),
        # What the reply is, in the clear, so a complaint about how it sounds
        # has something to check itself against.
        # WHICH FILE SPOKE, not only how it was packaged. Asked "which voice
        # model spoke and what is its native rate" I could answer the second
        # from the log and had to go and look for the first (2026-09-04).
        "reply_format": f"{out_fmt} {out_rate} Hz {REPLY_BITRATE} {spoke_by}",
        "timing": {"stt_s": round(t_stt, 2),
                   "think_s": round(t1 - t0 - t_stt, 2),
                   "tts_s": round(time.time() - t1, 2)},
    }
    return out


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
    if args and args[0] == "--verify-voices":
        res = verify_voices(force="--force" in args)
        bad = 0
        for n in sorted(res):
            ok = res[n]["ok"]
            bad += not ok
            print(("ok   " if ok else "MUTE "), f"{n:<38}",
                  "" if ok else res[n]["err"])
        n_lang, codes, why = languages()
        print(f"\n{len(res)} voices, {bad} mute -> {n_lang} languages: "
              f"{' '.join(codes)}")
        return 1 if bad else 0
    if args and args[0] == "--selftest":
        return selftest(args[1] if len(args) > 1 else None)
    sys.exit(__doc__.strip())


if __name__ == "__main__":
    sys.exit(main())
