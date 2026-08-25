"""Voice conversation mode (2026-07-13, the owner: "Voice Claude" group).

A voice note in a VOICE_CHATS chat becomes a spoken conversation turn:
  ogg/opus -> ffmpeg 16k wav -> whisper.cpp large-v3-turbo (Vulkan iGPU, ~4x
  realtime, language autodetected) -> normal Claude turn -> Piper TTS (voice
  picked by detected language) -> ogg/opus voice note back, plus full text.

Everything runs on-box: whisper at ~/whisper.cpp (build-vulkan), Piper in
workspace/voice/venv with voices in workspace/voice/voices. All entry points raise on
failure — the gateway catches and falls back to a plain text reply.
"""
import os, re, json, subprocess, tempfile

import tgconf as C
import tg_api as TG


def transcribe(path):
    """Audio file (any ffmpeg-decodable format) -> (text, lang like 'en'/'ru')."""
    with tempfile.TemporaryDirectory(prefix="stt_") as td:
        wav = os.path.join(td, "a.wav")
        subprocess.run(["ffmpeg", "-y", "-i", path, "-ar", "16000", "-ac", "1", wav],
                       capture_output=True, timeout=120, check=True)
        out = os.path.join(td, "out")
        subprocess.run([C.WHISPER_BIN, "-m", C.WHISPER_MODEL, "-f", wav,
                        "-l", "auto", "-oj", "-of", out],
                       capture_output=True, timeout=600, check=True)
        d = json.load(open(out + ".json"))
        text = " ".join(s["text"].strip() for s in d.get("transcription", [])).strip()
        lang = (d.get("result") or {}).get("language") or "en"
        return text, lang


# Markdown reads terribly aloud; strip it and cap length at a sentence boundary
# (the full untruncated text is always sent alongside the voice note anyway).
def speakable(text, max_chars=1200):
    t = re.sub(r"```.*?```", " (code omitted) ", text, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"https?://\S+", "(link)", t)
    t = re.sub(r"^\s*[#>*+-]+\s*", "", t, flags=re.M)   # heading/list/quote markers
    t = re.sub(r"[*_~|]{1,3}", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > max_chars:
        cut = t[:max_chars]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        t = cut[:end + 1] if end > 200 else cut
    return t


def synthesize(text, lang="en"):
    """Reply text -> path of a Telegram-ready .ogg (opus) voice note, or None if
    nothing speakable is left after stripping markdown."""
    spoken = speakable(text)
    if not spoken:
        return None
    voice = C.PIPER_VOICES.get(lang[:2]) or C.PIPER_VOICES["en"]
    fd, ogg = tempfile.mkstemp(prefix="tts_", suffix=".ogg")
    os.close(fd)
    with tempfile.TemporaryDirectory(prefix="tts_") as td:
        wav = os.path.join(td, "a.wav")
        subprocess.run([C.PIPER, "-m", voice, "-f", wav], input=spoken, text=True,
                       capture_output=True, timeout=300, check=True)
        subprocess.run(["ffmpeg", "-y", "-i", wav, "-c:a", "libopus", "-b:a", "32k", ogg],
                       capture_output=True, timeout=120, check=True)
    return ogg


def send_voice(chat_id, ogg_path, reply_to=None):
    """Upload a voice note. Returns True on success; deletes the temp file either way."""
    try:
        params = {"chat_id": chat_id}
        if reply_to:
            params["reply_to_message_id"] = reply_to
        with open(ogg_path, "rb") as fh:
            r = TG._call("sendVoice", _files={"voice": fh}, _timeout=120, **params)
        return bool(r.get("ok"))
    finally:
        try:
            os.unlink(ogg_path)
        except OSError:
            pass
