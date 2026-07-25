"""Extract text from audio/video for indexing — 100% on-box.

ffmpeg -> 16 kHz mono wav -> whisper.cpp (large-v3-turbo on the Vulkan iGPU,
same stack as Telegram voice mode). Returns '' when the file has no audio
stream or whisper finds no speech, so callers can index nothing gracefully.
"""
import os, json, subprocess, tempfile

WHISPER_BIN = os.environ.get(
    "DOCPIPE_WHISPER_BIN",
    os.path.expanduser("~/whisper.cpp/build-vulkan/bin/whisper-cli"))
WHISPER_MODEL = os.environ.get(
    "DOCPIPE_WHISPER_MODEL",
    os.path.expanduser("~/whisper.cpp/models/ggml-large-v3-turbo-q5_0.bin"))


def available():
    return os.path.exists(WHISPER_BIN) and os.path.exists(WHISPER_MODEL)


def has_audio(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                        "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
                       capture_output=True, text=True)
    return "audio" in (r.stdout or "")


def transcribe(path, timeout=900):
    """Any ffmpeg-decodable audio/video file -> transcript text ('' if silent)."""
    if not available():
        print(f"  whisper not available at {WHISPER_BIN}; skipping transcript")
        return ""
    if not has_audio(path):
        return ""
    with tempfile.TemporaryDirectory(prefix="docpipe_stt_") as td:
        wav = os.path.join(td, "a.wav")
        subprocess.run(["ffmpeg", "-y", "-i", path, "-vn", "-ar", "16000", "-ac", "1", wav],
                       capture_output=True, timeout=300, check=True)
        out = os.path.join(td, "out")
        subprocess.run([WHISPER_BIN, "-m", WHISPER_MODEL, "-f", wav,
                        "-l", "auto", "-oj", "-of", out],
                       capture_output=True, timeout=timeout, check=True)
        d = json.load(open(out + ".json"))
        return " ".join(s["text"].strip() for s in d.get("transcription", [])).strip()
