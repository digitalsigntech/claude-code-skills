"""small on CPU, small on Vulkan, large-v3-turbo on Vulkan — whole turn.

the owner's ask (#465 from the Mac side): does the box's AMD iGPU earn its place
in the LQ tier? The rule he set is the right one — pick the largest model whose
WHOLE TURN beats small on CPU, not the one with the best STT number, because a
person waits for the whole turn.
"""
import os, subprocess, sys, time, wave
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import local_voice as lv

HERE = os.path.dirname(os.path.abspath(__file__))


H = os.path.expanduser("~/whisper.cpp")
CPU = f"{H}/build/bin/whisper-cli"
GPU = f"{H}/build-vulkan/bin/whisper-cli"
SMALL = f"{H}/models/ggml-small.bin"
LARGE = f"{H}/models/ggml-large-v3-turbo-q5_0.bin"

CLIPS = [("short", "Yes, go ahead."),
         ("normal", "What were the sales on Tuesday this week?"),
         ("long", "Can you give me the full equipment list, every press we "
                  "run, with the web width and the top speed for each one, "
                  "and tell me which of them is booked tomorrow morning.")]
VOICE = os.environ.get("LQ_PIPER_VOICE",
                       os.path.join(HERE, "voices", "en_US-lessac-medium.onnx"))
PIPER = os.environ.get("LQ_PIPER_BIN",
                       os.path.join(HERE, "venv", "bin", "piper"))
ANSWER = ("Tuesday came in at 49,060 dollars, which is up on Monday and the "
          "best day of the week so far.")


def clip(tag, text):
    w = f"/tmp/gb_{tag}.wav"
    if not os.path.exists(w):
        subprocess.run([PIPER, "-m", VOICE, "-f", "/tmp/gb.wav"], input=text,
                       text=True, capture_output=True, check=True)
        subprocess.run([lv.FFMPEG, "-y", "-v", "error", "-i", "/tmp/gb.wav",
                        "-ar", "16000", "-ac", "1", w], check=True)
    with wave.open(w) as f:
        return w, f.getnframes() / float(f.getframerate())


def stt(binary, model, wav):
    t = time.time()
    r = subprocess.run([binary, "-m", model, "-f", wav, "-nt", "-np", "-l", "en"],
                       capture_output=True, text=True)
    return time.time() - t, " ".join((r.stdout or "").split())


def tts():
    t = time.time()
    subprocess.run([PIPER, "-m", VOICE, "-f", "/tmp/gb_out.wav"], input=ANSWER,
                   text=True, capture_output=True, check=True)
    return time.time() - t


print(f"  {'clip':<7} {'audio':>6}  {'small CPU':>18}  {'small GPU':>18}  "
      f"{'large-v3 GPU':>18}")
for tag, text in CLIPS:
    wav, secs = clip(tag, text)
    row = []
    for label, b, m in (("cpu", CPU, SMALL), ("gpu", GPU, SMALL),
                        ("gpu-l", GPU, LARGE)):
        best = min(stt(b, m, wav)[0] for _ in range(2))
        row.append(best)
    t_tts = min(tts() for _ in range(2))
    print(f"  {tag:<7} {secs:5.1f}s  "
          + "  ".join(f"{s:6.2f}s -> {s + t_tts:6.2f}s" for s in row)
          + f"   (tts {t_tts:.2f}s)")
