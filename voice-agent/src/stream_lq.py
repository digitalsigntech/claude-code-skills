"""stream_lq — real-time speech to a GPU agent over one WebSocket (request 487).

What the clip path does per sentence, this does while the sentence is spoken:
the phone streams 100 ms frames of PCM16/16 kHz, sealed under a key that
travels only inside the sealed `start` frame; a RESIDENT recogniser on the
GPU re-decodes the open utterance every PARTIAL_MS for `partial`, decodes it
once more with full context after `utterance_end` for `final`, and then the
turn is the ordinary one — the agent's answer path, the spoken cap, the
synthesiser — delivered as a `reply` shaped exactly like a clip reply.

WIRE (the app, build 346). First frame: a sealed TEXT frame (today's
envelope) whose plaintext is {"type":"start","lang","speaker","tz","key_b64",
"format":"pcm16","rate":16000,"frame_ms":100, …}. Every later frame is
BINARY: 1 kind byte (1 audio, 2 control JSON, 3 agent JSON) ‖ 12-byte nonce
(4 random ‖ 8-byte big-endian counter) ‖ AES-256-GCM ciphertext ‖ 16-byte tag,
under the stream key. The agent uses kind 3 with its own nonce prefix.

THE PLANE IS OPAQUE TO WORDS, NOT TO SECONDS (#548, amendment A): a metered
agent frame is sent to the plane as TEXT JSON {"frame": <base64 of the binary
frame>, "id", "audio_seconds", "audio_seconds_out"}; the plane bills from the
clear fields and forwards the binary frame to the phone unchanged. Unmetered
agent frames (hello, partial, final, no_speech, error) go as binary directly.

WHY A RESIDENT RECOGNISER. Measured on the box's iGPU (2026-09-05): whisper's
cost is a padded 30 s encoder window plus a model load per invocation, not the
audio length — `large-v3-turbo` 2.39 s per call, 1.96 s resident, 0.5 s
resident with the audio context sized to the utterance. The two changes turn a
2.5 s recogniser leg into half a second and make partials possible at all.
"""
import base64
import inspect
import json
import math
import os
import queue
import re
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import local_voice as lv                                        # noqa: E402

KIND_AUDIO, KIND_CTRL, KIND_AGENT = 1, 2, 3
RATE = 16000
FRAME_MS = 100
PARTIAL_MS = int(os.environ.get("LQ_STREAM_PARTIAL_MS", "700"))
PROGRESS_S = float(os.environ.get("LQ_STREAM_PROGRESS_S", "4"))
MAX_UTTERANCE_S = int(os.environ.get("LQ_STREAM_MAX_UTTERANCE_S", "60"))
STREAM_PORT = int(os.environ.get("LQ_STREAM_PORT", "8098"))
STREAM_THREADS = int(os.environ.get("LQ_STREAM_THREADS", "4"))


# ---------------------------------------------------------------- frames
def seal_frame(kind, key, prefix, counter, payload):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = prefix + counter.to_bytes(8, "big")
    return bytes([kind]) + nonce + AESGCM(key).encrypt(nonce, payload, None)


def open_frame(key, frame):
    """(kind, nonce, payload) or raises."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if len(frame) < 1 + 12 + 16:
        raise ValueError("frame too short")
    kind, nonce = frame[0], bytes(frame[1:13])
    return kind, nonce, AESGCM(key).decrypt(nonce, bytes(frame[13:]), None)


def pcm_peak_dbfs(pcm):
    n = len(pcm) // 2
    if n == 0:
        return float("-inf")
    peak = 0
    for (v,) in struct.iter_unpack("<h", pcm[:n * 2]):
        a = -v if v < 0 else v
        if a > peak:
            peak = a
            if peak >= 32767:
                break
    return float("-inf") if peak == 0 else 20 * math.log10(peak / 32768.0)


def audio_ctx_for(seconds):
    """Whisper's encoder context for an utterance of this length: 50 frames a
    second plus headroom, never below 512 and never above the full 1500 (30 s).

    The floor is measured, not chosen: at 320 a 2 s clip decoded to dots, at
    384 a 3 s question came back twice over ("What is 2 plus 2 …? What is 2
    plus 2 …"); 512 was clean on 2, 4 and 8 s clips at ≈0.5 s a decode."""
    return max(512, min(1500, int(50 * seconds) + 192))


def untangle(text):
    """Whisper's two habits on a short or cut-off buffer, undone: a sentence
    repeated whole ("A. A"), and a phrase stuttered ("What is 2 plus? What is
    2 plus? …"). Only exact repeats are folded; nothing is paraphrased."""
    t = " ".join((text or "").split())
    if not t:
        return t
    # whole-text halves
    n = len(t)
    for cut in range(n // 2 - 1, n // 2 + 2):
        if 8 < cut < n and t[:cut].strip().rstrip(".?!") == t[cut:].strip().rstrip(".?!"):
            return t[:cut].strip()
    # the same clause three or more times in a row
    parts = [p.strip() for p in __import__("re").split(r"(?<=[.?!])\s+", t) if p.strip()]
    out = []
    for p in parts:
        if len(out) >= 2 and out[-1] == p and out[-2] == p:
            continue
        out.append(p)
    return " ".join(out)


# SCRIPTS, for two checks whisper cannot do for itself (2026-09-05, 21:24 UTC:
# an English sentence came back as "the,amel, Adam, little, 향, ٰس …" with
# English at p=0.91, and was then SPOKEN in that non-language). A transcript
# whose letters span several scripts is a garbled decode, not a sentence; and
# the voice that reads an answer is chosen from the answer's own script, not
# from a recogniser's guess about the question.
import unicodedata as _ud


scripts_in, mixed_scripts = lv.scripts_in, lv.mixed_scripts   # the one rule, shared with the clip path


LATIN_LANGS = {"en", "fr", "es", "de", "it", "pt", "pl", "sv", "nl", "tr"}


def voice_lang_for(text, heard=None, heard_p=0.0, pinned=None, last=None, ui=None):
    """The language a voice should read `text` in. Pinned wins. Otherwise the
    text's own script decides the family, and within a family the best
    witness: a confidently heard language, then the account's last, then the
    app's language, then English. Never a language this install cannot speak."""
    can = lv._voice_locales()
    if pinned and pinned in can:
        return pinned
    sc = scripts_in(text)
    if "kana" in sc and "cjk" in sc:
        sc["cjk"] = sc.pop("kana") + sc["cjk"]
    top = max(sc, key=sc.get) if sc else "latn"
    family = {"cyrl": ("ru", "uk"), "cjk": ("zh", "ja"), "kana": ("ja",),
              "latn": tuple(sorted(LATIN_LANGS))}.get(top, ())
    if top == "kana" or ("kana" in scripts_in(text) and top == "cjk"):
        family = ("ja", "zh")
    witnesses = [(heard if heard_p >= 0.6 else None), last, ui, "en"]
    for w in witnesses:
        if w and w in family and w in can:
            return w
    for w in family:
        if w in can:
            return w
    return last if last in can else "en"


# whisper names its languages in English; the roster speaks in codes
WHISPER_LANG = {"english": "en", "russian": "ru", "ukrainian": "uk", "german": "de",
                "french": "fr", "spanish": "es", "italian": "it", "portuguese": "pt",
                "polish": "pl", "swedish": "sv", "dutch": "nl", "turkish": "tr",
                "chinese": "zh", "japanese": "ja", "korean": "ko", "arabic": "ar",
                "hindi": "hi", "czech": "cs", "greek": "el", "hebrew": "he",
                "finnish": "fi", "norwegian": "no", "danish": "da", "hungarian": "hu",
                "romanian": "ro", "vietnamese": "vi", "thai": "th", "indonesian": "id"}


# ---------------------------------------------------------- recogniser
class Recogniser:
    """One resident whisper-server on the GPU, started on first use."""

    def __init__(self, binary=None, model=None, port=STREAM_PORT):
        self.binary = binary or os.path.join(
            os.path.dirname(lv.WHISPER_BIN), "whisper-server")
        self.model = model or lv.WHISPER_MODEL
        self.port = port
        self.proc = None
        self.backend = "unknown"
        self.log_path = os.path.join(tempfile.gettempdir(), "lq-stream-server.log")
        self._lock = threading.Lock()

    def _listening(self):
        import socket
        with socket.socket() as s:
            s.settimeout(0.3)
            return s.connect_ex(("127.0.0.1", self.port)) == 0

    def ensure(self, timeout=60):
        with self._lock:
            if self.proc and self.proc.poll() is None and self._listening():
                return True
            if self.proc is None and self._listening():
                # A server from a previous life of this process (the agent
                # restarts, its child does not): adopt it, and read the
                # backend from the log it left behind.
                try:
                    txt = open(self.log_path, "rb").read()[-20000:].decode("utf-8", "replace")
                except OSError:
                    txt = ""
                self.backend = ("vulkan" if "ggml_vulkan" in txt else
                                "cuda" if ("CUDA" in txt or "ggml_cuda" in txt) else
                                "metal" if "ggml_metal" in txt else "cpu")
                self.proc = _Adopted()
                return True
            if not (os.path.exists(self.binary) and os.path.exists(self.model)):
                return False
            log = open(self.log_path, "ab")
            self.proc = subprocess.Popen(
                [self.binary, "-m", self.model, "--host", "127.0.0.1",
                 "--port", str(self.port), "-t", str(STREAM_THREADS), "-nt"],
                stdout=log, stderr=subprocess.STDOUT)
            t0 = time.time()
            while time.time() - t0 < timeout:
                if self._listening():
                    break
                if self.proc.poll() is not None:
                    return False
                time.sleep(0.25)
            else:
                return False
            try:
                txt = open(self.log_path, "rb").read()[-20000:].decode("utf-8", "replace")
            except OSError:
                txt = ""
            if "ggml_vulkan" in txt:
                self.backend = "vulkan"
            elif "CUDA" in txt or "cuBLAS" in txt or "ggml_cuda" in txt:
                self.backend = "cuda"
            elif "Metal" in txt or "ggml_metal" in txt:
                self.backend = "metal"
            else:
                self.backend = "cpu"
            return True

    def ready(self):
        """A GPU backend, resident and answering: the proof `stream: true` needs.

        A CPU backend is remembered and its server STOPPED: a resident model
        that will never stream is 500 MB of RAM doing nothing on a two-core
        box, and every capability probe must not pay a model load to learn
        the same answer again."""
        if getattr(self, "_no_gpu", False) or os.environ.get("LQ_STREAM_NO_GPU"):
            return False
        if not self.ensure():
            return False
        if self.backend in ("vulkan", "cuda", "metal"):
            return True
        self._no_gpu = True
        self.stop()
        return False

    def cli_available(self):
        """A one-shot decoder exists (whisper-cli + model): enough to hold a
        stream and transcribe each utterance once when it ends. That is the
        clip path's cost with the upload wait removed — no GPU in it."""
        return bool(lv.WHISPER_BIN and os.path.exists(lv.WHISPER_BIN)
                    and self.model and os.path.exists(self.model))

    def decode(self, pcm, lang=None, audio_ctx=None):
        """Text for PCM16/16 kHz mono bytes, via the resident server."""
        return self.decode_full(pcm, lang, audio_ctx)[0]

    @staticmethod
    def _wav(pcm):
        wav_io = tempfile.SpooledTemporaryFile(max_size=1 << 20)
        with wave.open(wav_io, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(pcm)
        wav_io.seek(0)
        return wav_io.read()

    def decode_cpu(self, pcm, lang=None, hint=""):
        """(text, code, probability) through the clip path's one-shot decoder
        (request 495: CPU agents stream too). whisper-cli reports the language
        it auto-detected without a probability; a named language counts as
        heard, an unnamed one as unknown."""
        heard, _secs, _peak, code = lv.transcribe(self._wav(pcm), ".wav",
                                                  (lang if lang and lang != "auto" else None), hint)
        text = " ".join(str(heard or "").split())
        code = (code or "").strip().lower()[:2]
        return text, code, (1.0 if code else 0.0)

    def decode_full(self, pcm, lang=None, audio_ctx=None, hint=""):
        """(text, language code heard, probability). With `auto` the server
        names the language it decoded in — one pass, no second model. Without
        a GPU server the one-shot decoder answers instead."""
        if not self.ready():
            return self.decode_cpu(pcm, lang, hint)
        if not self.ensure():
            raise RuntimeError("recogniser not running")
        boundary = "----lq" + uuid.uuid4().hex
        wav = self._wav(pcm)
        fields = {"response_format": "verbose_json", "temperature": "0",
                  "language": (lang or "auto")}
        if audio_ctx:
            fields["audio_ctx"] = str(int(audio_ctx))
        body = b""
        for k, v in fields.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                     f"name=\"{k}\"\r\n\r\n{v}\r\n").encode()
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                 f"filename=\"u.wav\"\r\nContent-Type: audio/wav\r\n\r\n").encode()
        body += wav + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/inference", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.loads(r.read() or b"{}")
        text = " ".join(str(d.get("text") or "").split())
        name = str(d.get("detected_language") or d.get("language") or "").strip().lower()
        code = WHISPER_LANG.get(name, name[:2] if name else "")
        try:
            prob = float(d.get("detected_language_probability") or 0)
        except (TypeError, ValueError):
            prob = 0.0
        return text, code, prob

    def stop(self):
        with self._lock:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc = None


class _Adopted:
    """Stands in for a Popen we did not start: alive as long as the port is."""
    def poll(self):
        return None

    def terminate(self):
        subprocess.run(["fuser", "-k", f"{STREAM_PORT}/tcp"], capture_output=True)

    def wait(self, t=None):
        return 0

    def kill(self):
        self.terminate()


_RECOG = None
_RECOG_LOCK = threading.Lock()


def recogniser():
    global _RECOG
    with _RECOG_LOCK:
        if _RECOG is None:
            _RECOG = Recogniser()
        return _RECOG


def ready():
    try:
        return recogniser().ready()
    except Exception as e:
        print(f"[stream] recogniser not ready: {e}", file=sys.stderr)
        return False


def facts():
    """What the local row says about streaming — by proof, not by class.

    Request 495 (2026-09-06): two capabilities. `stream` — the agent holds the
    socket, decodes each utterance once when it ends and answers in
    `reply_chunk` frames; any install with a decoder has it. `stream_partials`
    — live partial transcripts while the person speaks; only a resident GPU
    recogniser can afford them."""
    r = recogniser()
    gpu = ready()
    ok = gpu or r.cli_available()
    return {"stream": bool(ok),
            "stream_partials": bool(gpu),
            **({"stream_recogniser": os.path.basename(r.model),
                "stream_backend": r.backend if gpu else "cpu"} if ok else {}),
            **({"stream_partial_ms": PARTIAL_MS} if gpu else {})}


# --------------------------------------------------------------- session
# ------------------------------------------------------- streamed reply ---
# WHY THE REPLY WAS ONE BLOB, AND WHAT THIS CHANGES (2026-09-05). Measured on
# the afternoon's turns: recogniser under a second, model 6–25 s, synthesis
# 0.5–7 s — and the phone heard nothing until all of it was done, because the
# whole answer was synthesised as one file after the model's last token. The
# model's text arrives as deltas (bridge._stream_run → on_text); this class
# cuts that text at sentence ends as it grows, speaks each sentence the moment
# it is complete, and sends it down the stream as a `reply_chunk`. The first
# sentence is in the phone's ear while the model is still writing the third.
#
# What it deliberately does NOT do: split inside a number ("1.5 s"), speak a
# fragment shorter than MIN_SENT chars (it waits for the next boundary), or
# keep going once a table row or a code fence appears — those belong on the
# screen, and the remainder after the model finishes goes through the same
# table-stripping as the single-blob path. Order is guaranteed by ONE worker
# thread and a sequence number; the app plays in seq order and treats the
# `final` chunk (or the `reply` frame that follows it) as the end of the turn.
_SENT_END = re.compile(r'[.!?…。！？]+["”’)\]]*(?=\s)|\n')
_TABLE_OR_FENCE = re.compile(r'(^|\n)\s*(\|.*\||```|\[tool_call\])', re.S)   # a tool-call line is never spoken
MIN_SENT = int(os.environ.get("LQ_STREAM_MIN_SENT", "24"))
TOOL_WAIT_S = float(os.environ.get("LQ_STREAM_TOOL_WAIT_S", "30"))   # request 499: how long a tool result may take


def _strip_tables(text):
    """The remainder after a table appeared: prose lines only."""
    out, fence = [], False
    for ln in (text or "").splitlines():
        if lv._CODE_FENCE.match(ln):
            fence = not fence
            continue
        if fence or lv._TABLE_ROW.match(ln):
            continue
        out.append(ln)
    return "\n".join(out).strip()


class _ChunkSpeaker:
    def __init__(self, session, uid, lang, speaker, t0, seq_start=0):
        self.s, self.uid, self.lang, self.speaker, self.t0 = session, uid, lang, speaker, t0
        self.seen = ""              # the model's text so far
        self.consumed = 0           # chars of `seen` already handed to the worker
        self.halted = False         # a table/fence appeared: stop cutting
        self.interrupted = False    # the phone stopped playing: send nothing more
        self.seq = int(seq_start or 0)      # a continuation after a tool call carries on counting
        self.secs = 0.0
        self.bytes = 0
        self.first_audio = None     # seconds from t0 to the first chunk sent
        self.spoke_by = ""
        self.err = None
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()

    # called from the model thread on every delta with the FULL text so far
    def feed(self, full):
        full = str(full or "")
        with self.lock:
            self.seen = full
            if self.halted or self.err or self.interrupted:
                return
            if self.consumed == 0:
                lead = len(full) - len(full.lstrip())
                if full.lstrip()[:len(lv.READ_IN_FULL)].lower() == lv.READ_IN_FULL:
                    self.consumed = lead + len(lv.READ_IN_FULL)
            if _TABLE_OR_FENCE.search(full[self.consumed:]):
                self.halted = True
                return
            pos = self.consumed
            while True:
                m = _SENT_END.search(full, pos)
                if not m:
                    break
                end = m.end()
                sent = full[self.consumed:end].strip()
                if len(sent) < MIN_SENT and m.group(0) != "\n":
                    pos = end                       # too short alone: extend
                    continue
                if sent:
                    self.q.put((sent, False))
                self.consumed = end
                pos = end

    def interrupt(self):
        """The phone stopped playing this answer — the person spoke over it
        (control `interrupt`, build 356+). What went out stays; nothing else is
        synthesised or sent for this utterance, and finish() will not fall back
        to the single blob. Returns the number of chunks that had gone out."""
        with self.lock:
            if not self.interrupted:
                self.interrupted = True
                try:
                    while True:
                        self.q.get_nowait()
                except queue.Empty:
                    pass
                self.q.put(None)
            return self.seq

    def _stats(self):
        return {"chunks": self.seq, "audio_seconds_out": round(self.secs, 3),
                "bytes": self.bytes, "first_audio_s": self.first_audio,
                "spoke_by": self.spoke_by,
                **({"interrupted": True} if self.interrupted else {})}

    def close(self):
        """Nothing more to say from this speaker (a tool call with no lead-in)."""
        with self.lock:
            self.q.put(None)
        self.worker.join(timeout=30)
        return self._stats()

    def finish(self, answer, spoken_line, final=True):
        """The model is done. Speak whatever was not cut yet, flag it final.
        Returns None when the stream cannot be trusted (the final text does not
        start with what was already spoken) and NO chunk went out — the caller
        then takes the single-blob path. If chunks did go out, the rest is
        spoken from the final text on a best-effort basis. An interrupted
        answer is never completed and never falls back: the person has moved on."""
        if self.interrupted:
            self.worker.join(timeout=30)
            return self._stats()
        with self.lock:
            prefix = self.seen[:self.consumed]
            base = spoken_line or answer or ""
            if base.startswith(prefix):
                rem = base[len(prefix):]
            elif (answer or "").startswith(prefix):
                rem = answer[len(prefix):]
            elif self.seq == 0 and self.q.empty():
                self.q.put(None)
                return None
            else:
                self.s.log(f"stream {self.uid}: final text does not extend the spoken "
                           f"prefix ({self.consumed} chars) — remainder skipped")
                rem = ""
            rem = _strip_tables(rem) if (self.halted or lv._TABLE_ROW.search(rem or "")) else rem
            if rem.strip() or final:
                self.q.put((rem.strip(), final))
            self.q.put(None)
        self.worker.join(timeout=120)
        return self._stats()

    def _run(self):
        while True:
            item = self.q.get()
            if item is None or self.interrupted:
                return
            text, final = item
            try:
                say = lv._speakable(text) if text else ""
                audio, secs, fmt, rate, who = (b"", 0.0, lv.REPLY_FORMAT, 0, "") if not say.strip() \
                    else lv.speak(say, voice_lang_for(say, pinned=self.s.lang or None,
                                                      last=self.lang), self.speaker)
                if not audio and not final:
                    continue
                if self.interrupted:            # the phone moved on while this was synthesised
                    return
                self.seq += 1
                frame = {"type": "reply_chunk", "id": self.uid, "turn_id": self.s.turn_id(self.uid), "seq": self.seq,
                         "final": bool(final), "text": text,
                         "voice": {"format": fmt, "b64": base64.b64encode(audio).decode()},
                         "audio_seconds_out": round(secs, 3)}
                self.s._agent(frame)
                if self.first_audio is None and audio:
                    self.first_audio = round(time.time() - self.t0, 2)
                self.secs += secs
                self.bytes += len(audio)
                if who:
                    self.spoke_by = who
            except Exception as e:                                # noqa: BLE001
                self.err = e
                self.s.log(f"stream {self.uid}: chunk {self.seq + 1} failed: {e}")
                if final:
                    return


class StreamSession:
    """One phone's socket, from the sealed `start` to the close.

    `ws` has recv() -> (op, bytes), send_text(), send_binary() (ws_min.WS).
    `open_envelope(env_dict) -> plaintext str` opens today's sealed envelope
    for this account. `answer_fn(text) -> str` is the agent's ordinary ask
    path. `on_transcript(text, ts)` posts the person's words the moment the
    final decode exists, as the clip path does.
    """

    def __init__(self, ws, open_envelope, answer_fn, account=None,
                 on_transcript=None, log=None):
        self.ws = ws
        self.open_envelope = open_envelope
        self.answer_fn = answer_fn
        # Request 497 (2026-09-06): ONE IDENTITY FOR ONE TURN. The phone's
        # utterance id restarts with every stream, so the turn id is the
        # stream's own tag plus the utterance id — the same string on the
        # final, the chunks, the reply, and the archive rows the agent writes
        # for that turn, so the app can pair a voiced line with its history
        # row by identity rather than by text (the texts differ: the archive
        # keeps paragraph breaks, the chunks are sentences).
        self.sid = uuid.uuid4().hex[:8]
        # Request 499: the app's declared tools (from `start`), and the
        # results it sends back, keyed by call id.
        self.tools = None
        self.tool_q = {}
        self.account = account
        self.on_transcript = on_transcript
        self.log = log or (lambda *a: print("[stream]", *a, file=sys.stderr))
        self.speaking = {}          # str(utterance id) -> _ChunkSpeaker while its answer is spoken
        self.key = None
        self.prefix = os.urandom(4)
        self.counter = 0
        self.lang = ""
        self.speaker = ""
        self.start = {}
        self.buf = bytearray()
        self.frames = 0
        self.utt_id = None
        self.utt_seq = 0
        self.seen_nonces = set()
        self.dirty = threading.Event()
        self.stop = threading.Event()
        self.decode_lock = threading.Lock()
        self.last_partial = ""
        self.ending = threading.Event()
        self.heard_out = None
        self.turns = 0
        self.partial_count = 0
        self.recog = recogniser()
        # RECOGNISE NOW, ANSWER IN ORDER (2026-09-05, 20:25 UTC). Two sentences
        # back to back: the second one's transcript waited behind the first
        # one's whole model turn, because one thread did both. Recognition is
        # half a second and happens in the reading thread the moment the
        # utterance ends; the model turn and the voice go through this queue,
        # one at a time, in the order the sentences were spoken.
        self.answer_q = queue.Queue()
        # Peaks of the utterances that were answered, newest last: the level
        # a filler fragment is judged "quiet" against (lv.quiet_for).
        self.levels = []
        self.answer_thread = None

    # ---- sending
    def _agent(self, obj, meter=None):
        payload = json.dumps(obj, ensure_ascii=False).encode()
        self.counter += 1
        frame = seal_frame(KIND_AGENT, self.key, self.prefix, self.counter, payload)
        if meter:
            self.ws.send_text(json.dumps({"frame": base64.b64encode(frame).decode(),
                                          **meter}))
        else:
            self.ws.send_binary(frame)

    # ---- lifecycle
    def run(self):
        op, data = self.ws.recv(timeout=30)
        try:
            env = json.loads(data.decode("utf-8"))
            start = json.loads(self.open_envelope(env))
        except Exception as e:
            raise ValueError(f"start frame did not open: {str(e)[:120]}")
        if start.get("type") != "start":
            raise ValueError("first frame is not a start")
        key = base64.b64decode(str(start.get("key_b64") or ""))
        if len(key) != 32:
            raise ValueError("stream key is not 32 bytes")
        if str(start.get("format") or "pcm16") != "pcm16" or int(start.get("rate") or RATE) != RATE:
            raise ValueError("only pcm16 at 16000 Hz is streamed")
        self.key = key
        self.start = start
        self.lang = str(start.get("lang") or "").strip().lower()[:5]
        if self.lang == "auto":
            self.lang = ""
        self.speaker = str(start.get("speaker") or "").strip().lower()[:64]
        # STREAMED REPLY AUDIO is opt-in per stream (2026-09-05, the owner:
        # "organise streaming back"). An app that sends reply_stream:true gets
        # the answer as it is produced — a `reply_chunk` frame per sentence,
        # synthesised the moment the sentence is complete — and a final `reply`
        # frame with the text and no voice. An app that does not ask gets the
        # single-blob reply exactly as before; nothing changes under it.
        self.reply_stream = bool(start.get("reply_stream"))
        self.tools = start.get("tools") if isinstance(start.get("tools"), list) and start.get("tools") else None
        # Request 495: partials need the resident GPU recogniser; the socket
        # itself needs only a decoder. A CPU install streams without partials.
        self.partials = bool(self.recog.ready())
        if not self.partials and not self.recog.cli_available():
            self._agent({"type": "error", "message": "recogniser not running"})
            raise RuntimeError("recogniser not running")
        backend = self.recog.backend if self.partials else "cpu"
        self._agent({"type": "hello",
                     "recogniser": os.path.basename(self.recog.model),
                     "backend": backend,
                     "partials": self.partials,
                     **({"partial_every_ms": PARTIAL_MS} if self.partials else {}),
                     "progress_every_s": PROGRESS_S,
                     "max_utterance_s": MAX_UTTERANCE_S,
                     "frame_ms": FRAME_MS,
                     "reply_stream": self.reply_stream,
                     "tools": len(self.tools) if self.tools else 0})
        self.log(f"stream open: lang={self.lang or 'auto'} speaker={self.speaker or '-'} reply_stream={self.reply_stream} "
                 f"backend={backend} partials={self.partials} tools={len(self.tools) if self.tools else 0}")
        if start.get("greet"):
            # Request 489: the greeting is an id-0 reply right after the hello,
            # in the app's language, by first name, in the picked voice —
            # unmetered, so it goes as a plain binary frame the plane cannot bill.
            try:
                g = lv.greeting_reply(str(start.get("ui_lang") or self.lang or "en"),
                                      self.speaker, start.get("name"))
                self._agent({"type": "reply", "id": 0, **g})
                self.log(f"greeting: {g['text']!r} ({g['audio_seconds_out']}s, {g['reply_format']})")
            except Exception as e:
                self.log(f"greeting failed: {str(e)[:100]}")
        worker = threading.Thread(target=self._partials if self.partials else (lambda: None), daemon=True)
        worker.start()
        self.answer_thread = threading.Thread(target=self._answers, daemon=True)
        self.answer_thread.start()
        try:
            while True:
                op, data = self.ws.recv(timeout=900)
                if op != 2:                              # BINARY only after start
                    self.log(f"text frame after start ignored ({len(data)} bytes)")
                    continue
                try:
                    kind, nonce, payload = open_frame(self.key, data)
                except Exception as e:
                    self._agent({"type": "error", "message": f"frame refused: {str(e)[:80]}"})
                    continue
                if nonce in self.seen_nonces:
                    continue                             # a replayed frame
                self.seen_nonces.add(nonce)
                if kind == KIND_AUDIO:
                    if len(self.buf) < MAX_UTTERANCE_S * RATE * 2:
                        self.buf += payload
                        self.frames += 1
                        self.dirty.set()
                elif kind == KIND_CTRL:
                    self._control(payload)
                else:
                    self.log(f"unexpected kind {kind} from the phone")
        finally:
            self.stop.set()

    def _control(self, payload):
        try:
            c = json.loads(payload.decode("utf-8"))
        except Exception:
            return
        t = c.get("type")
        if t == "utterance_start":
            self.utt_id = c.get("id")            # the phone's id, its own type
        elif t == "utterance_end":
            self.ending.set()
            # THE ID IS ECHOED AS SENT — an int stays an int, a string a string
            # — because the app matches frames to sentences by it, and a
            # stringified 3 is not the 3 it is waiting for. Only when the phone
            # sent none does the agent name the utterance itself ("u<n>").
            uid = c.get("id") if c.get("id") is not None else (
                self.utt_id if self.utt_id is not None else f"u{self.utt_seq + 1}")
            self.utt_seq += 1
            pcm = bytes(self.buf)
            self.buf = bytearray()
            frames, self.frames = self.frames, 0
            self.dirty.clear()
            self.last_partial = ""
            self.utt_id = None
            try:
                self._recognise(uid, pcm, frames, c)
            finally:
                self.ending.clear()
        elif t == "utterance_cancel":
            self.buf = bytearray()
            self.frames = 0
            self.dirty.clear()
            self.last_partial = ""
            self.utt_id = None
        elif t == "interrupt":
            # build 356+: the person spoke over the answer; the phone stopped
            # playing and says so, so the worker stops synthesising the rest.
            sp = self.speaking.get(str(c.get("id")))
            if sp:
                n = sp.interrupt()
                self.log(f"stream {c.get('id')}: interrupted by the phone after {n} chunk(s) — the rest is not synthesised")
            else:
                self.log(f"stream: interrupt for {c.get('id')!r} — nothing being spoken under that id")
        elif t == "heard_out":
            self.heard_out = c.get("seconds")
        elif t == "tool_result":
            # request 499: the phone ran the tool; the turn waiting on it continues
            q = self.tool_q.get(str(c.get("call_id") or ""))
            if q is not None:
                q.put(c.get("output"))
            else:
                self.log(f"stream: tool_result for {c.get('call_id')!r} — no turn is waiting on it")

    def _partials(self):
        while not self.stop.is_set():
            if not self.dirty.wait(timeout=PARTIAL_MS / 1000.0):
                continue
            time.sleep(PARTIAL_MS / 1000.0)
            if self.stop.is_set():
                return
            self.dirty.clear()
            pcm = bytes(self.buf)
            secs = len(pcm) / (RATE * 2)
            if secs < 0.6:
                continue
            tail = pcm[-(15 * RATE * 2):]                # the last 15 s at most
            try:
                with self.decode_lock:
                    text = self.recog.decode(tail, self.lang or lv.recent_lang(self.account) or "auto",
                                             audio_ctx_for(len(tail) / (RATE * 2)))
            except Exception as e:
                self.log(f"partial decode failed: {str(e)[:80]}")
                continue
            if self.ending.is_set():
                continue                                 # the final has the floor
            text = untangle(lv.speech_text(text))
            if text and text != self.last_partial:
                self.last_partial = text
                self.partial_count += 1
                self._agent({"type": "partial",
                             "id": self.utt_id if self.utt_id is not None else f"u{self.utt_seq + 1}",
                             "text": text})

    def turn_id(self, uid):
        return f"{self.sid}:{uid}"

    @staticmethod
    def _kw(fn, **kw):
        """Only the keywords `fn` accepts — an older callback keeps working."""
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            return {}
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return kw
        return {k: v for k, v in kw.items() if k in params}

    def _no_speech(self, uid, secs_in, peak, reason, heard=""):
        """Every no-speech says why (2026-09-05: utterance 4 of a stream
        vanished from the phone with no line here to explain it)."""
        self.log(f"no speech ({reason}): utterance {uid}, {secs_in}s, peak "
                 f"{'-inf' if peak == float('-inf') else round(peak, 1)} dBFS"
                 + (f", heard {heard[:40]!r}" if heard else ""))
        self._agent({"type": "no_speech", "id": uid, "turn_id": self.turn_id(uid),
                     "peak_dbfs": None if peak == float("-inf") else round(peak, 1),
                     **({"heard_marker": heard[:40]} if heard else {})},
                    meter={"id": uid, "audio_seconds": secs_in,
                           "audio_seconds_out": 0.0, "no_speech": True})

    def _recognise(self, uid, pcm, frames, ctrl):
        """The reading thread's half of an utterance: decode, gate, `final`,
        then hand the sentence to the answer queue — half a second, so the
        next sentence's frames are read the moment this returns."""
        t0 = time.time()
        secs_in = round(frames * FRAME_MS / 1000.0, 3)
        declared = ctrl.get("seconds")
        if isinstance(declared, (int, float)) and abs(declared - secs_in) > max(0.5, 0.25 * secs_in):
            self.log(f"utterance {uid}: phone says {declared}s, {secs_in}s of frames arrived")
        peak = pcm_peak_dbfs(pcm)
        if not pcm:
            return self._no_speech(uid, secs_in, peak, "no frames")
        if peak < lv.SILENCE_PEAK_DBFS:
            return self._no_speech(uid, secs_in, peak, "silence")
        try:
            with self.decode_lock:
                heard, heard_code, heard_p = self.recog.decode_full(
                    pcm, self.lang or "auto", audio_ctx_for(secs_in),
                    hint=lv.recent_lang(self.account))
        except Exception as e:
            self._agent({"type": "error", "id": uid, "message": f"recogniser failed: {str(e)[:80]}"})
            return
        # THE VOICE FOLLOWS THE LANGUAGE HEARD (build 355 sends lang=auto on
        # every stream, because a pinned language translated Russian into
        # English). A pinned `lang` wins; otherwise the code the recogniser
        # decoded in, if this install can speak it; otherwise what this
        # account spoke last; otherwise English.
        user_text = untangle(lv.speech_text(heard))
        t_stt = time.time() - t0
        if user_text and mixed_scripts(user_text):
            self.log(f"garbled decode (mixed scripts, heard {heard_code or '?'} p={heard_p:.2f}): "
                     f"{user_text[:60]!r} ({secs_in}s, peak {peak:.1f} dBFS)")
            return self._no_speech(uid, secs_in, peak, "mixed scripts", heard=user_text)
        _why = user_text and lv.hallucination_gate(
            user_text, secs_in, ctrl.get("prefiltered"), peak,
            heard=(heard_code if heard_p >= 0.6 else None),
            phone_lang=str(self.start.get("ui_lang") or "")[:2] or None,
            quiet=lv.quiet_for(peak, self.levels),
            known_langs=(self.lang, lv.recent_lang(self.account)))
        if _why:
            self.log(f"phantom dropped ({_why}): {user_text!r} ({secs_in}s, peak {peak:.1f} dBFS, "
                     f"prefiltered={ctrl.get('prefiltered')})")
            return self._no_speech(uid, secs_in, peak, _why, heard=user_text)
        if not user_text:
            return self._no_speech(uid, secs_in, peak, "no words", heard=heard)
        lang = voice_lang_for(user_text, heard_code, heard_p, pinned=self.lang,
                              last=lv.recent_lang(self.account),
                              ui=str(self.start.get("ui_lang") or "")[:2] or None)
        ts = time.time()
        self._agent({"type": "final", "id": uid, "turn_id": self.turn_id(uid), "text": user_text})
        if self.on_transcript:
            try:
                self.on_transcript(user_text, ts, **self._kw(self.on_transcript, turn_id=self.turn_id(uid)))
            except Exception as e:
                self.log(f"posting the transcript failed: {e}")
        lv.remember_lang(self.account, lang)
        self.levels = (self.levels + [peak])[-8:]
        if not self.lang:
            self.log(f"utterance {uid}: heard {heard_code or '?'} (p={heard_p:.2f}) -> speaking {lang}")
        self.answer_q.put((uid, user_text, secs_in, peak, ctrl, t0, t_stt, ts, lang))

    def _answers(self):
        """One model turn at a time, in the order the sentences ended."""
        while not self.stop.is_set():
            try:
                item = self.answer_q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._answer(*item)
            except Exception as e:
                self.log(f"answer failed for utterance {item[0]}: {str(e)[:120]}")
                try:
                    self._agent({"type": "error", "id": item[0], "message": str(e)[:160]})
                except Exception:
                    return

    def _answer(self, uid, user_text, secs_in, peak, ctrl, t0, t_stt, ts, lang=None):
        lang = lang or self.lang or "en"
        # THE SOCKET IS NEVER SILENT WHILE THE MODEL THINKS (2026-09-05, 17:37
        # UTC): the phone closed a working stream 19 s after the final because
        # nothing had arrived since — a model turn on a real question runs
        # 10–30 s. A `progress` frame every few seconds says the reply is on
        # its way, and gives the app something to draw.
        box = {}
        speaker = None
        try:
            takes_on_text = "on_text" in inspect.signature(self.answer_fn).parameters
        except (TypeError, ValueError):
            takes_on_text = False
        if self.reply_stream:
            if takes_on_text:
                speaker = _ChunkSpeaker(self, uid, lang, self.speaker, t0)
                self.speaking[str(uid)] = speaker
            # An answer path without on_text (the skill agent's ask() returns
            # the whole text at once) is chunked AFTER the model returns: the
            # first sentence is synthesised and sent while the rest is still
            # being made — on a CPU install that is the difference between the
            # first sound at 1.5 s and at 7 s (request 495, measured on the second install).

        def _ask_model(prompt, spk):
            """The model, with a progress frame every few seconds while it thinks."""
            box = {}

            def _think():
                try:
                    kw = self._kw(self.answer_fn, turn_id=self.turn_id(uid), tools=self.tools)
                    if spk and takes_on_text:
                        box["answer"] = str(self.answer_fn(prompt, on_text=spk.feed, **kw) or "")
                    else:
                        box["answer"] = str(self.answer_fn(prompt, **kw) or "")
                except Exception as e:                                 # noqa: BLE001
                    box["error"] = e

            th = threading.Thread(target=_think, daemon=True)
            th.start()
            t_think = time.time()
            while th.is_alive():
                th.join(PROGRESS_S)
                if th.is_alive():
                    try:
                        self._agent({"type": "progress", "id": uid,
                                     "elapsed_s": round(time.time() - t_think, 1)})
                    except Exception:
                        break                       # the socket is gone; the turn ends below
            if "error" in box:
                raise box["error"]
            return box.get("answer", "")

        answer = _ask_model(user_text, speaker)
        # REQUEST 499: THE MODEL MAY CALL ONE OF THE PHONE'S TOOLS. The call is
        # the last line of its answer; the lead-in before it is spoken first,
        # the call goes to the phone, the phone's result comes back as a
        # control frame, and the model continues the SAME turn — more chunks
        # under the same id, one closing reply at the end.
        tool_calls, texts, parts = [], [], []
        hops = 0
        while True:
            clean, call = lv.split_tool_call(answer)
            if call is None:
                break
            if not (self.tools and self.reply_stream) or hops >= lv.MAX_TOOL_HOPS:
                self.log(f"stream {uid}: tool call {call['name']} dropped "
                         f"({'no tools declared' if not self.tools else 'no reply_stream' if not self.reply_stream else 'hop limit'})")
                answer = clean
                break
            hops += 1
            call_id = uuid.uuid4().hex[:8]
            if speaker is None and self.reply_stream and clean.strip():
                # no streaming answer path: the lead-in is chunked now, before
                # the call goes out, so it is heard before the app acts
                speaker = _ChunkSpeaker(self, uid, lang, self.speaker, t0,
                                        seq_start=sum(p_["chunks"] for p_ in parts))
                self.speaking[str(uid)] = speaker
                speaker.feed(clean)
            if speaker:
                st = (speaker.finish(clean, "", final=False) if clean.strip() else speaker.close())
                if st:
                    parts.append(st)
                self.speaking.pop(str(uid), None)
            if clean.strip():
                texts.append(clean)
            q = queue.Queue()
            self.tool_q[call_id] = q
            self._agent({"type": "tool_call", "id": uid, "turn_id": self.turn_id(uid),
                         "call_id": call_id, "name": call["name"], "arguments": call["arguments"]})
            self.log(f"stream {uid}: tool_call {call['name']} {json.dumps(call['arguments'], ensure_ascii=False)[:120]} -> waiting for the phone")
            output, waited, timed_out = None, 0.0, False
            while True:
                try:
                    output = q.get(timeout=PROGRESS_S)
                    break
                except queue.Empty:
                    waited += PROGRESS_S
                    if waited >= TOOL_WAIT_S:
                        timed_out = True
                        break
                    try:
                        self._agent({"type": "progress", "id": uid, "elapsed_s": round(waited, 1), "waiting": "tool_result"})
                    except Exception:
                        timed_out = True
                        break
            self.tool_q.pop(call_id, None)
            tool_calls.append({"call_id": call_id, "name": call["name"], "arguments": call["arguments"],
                               **({"output": output} if not timed_out else {"timed_out": True})})
            if timed_out:
                self.log(f"stream {uid}: no tool_result for {call['name']} within {TOOL_WAIT_S:.0f}s — the turn ends")
                answer = ""
                speaker = None
                break
            self.log(f"stream {uid}: tool_result {call['name']} -> {json.dumps(output, ensure_ascii=False)[:120]}")
            # The continuation: a streaming answer path feeds a new speaker as
            # it writes; one without on_text (the skill's ask()) gets its whole
            # answer chunked afterwards, exactly like the first pass — passing
            # on_text to a callback that has no such parameter closed the second install's
            # socket on the first live tool call (2026-09-07 00:49 UTC).
            speaker = None
            if takes_on_text:
                speaker = _ChunkSpeaker(self, uid, lang, self.speaker, t0,
                                        seq_start=sum(p_["chunks"] for p_ in parts))
                self.speaking[str(uid)] = speaker
            answer = _ask_model(lv.tool_result_prompt(call, output), speaker)
        answer, speak_all = lv.read_in_full(answer)
        t1 = time.time()
        spoken_line = "" if speak_all else lv.speech_for(answer or "")
        if self.reply_stream and speaker is None and not spoken_line and (answer or "").strip():
            speaker = _ChunkSpeaker(self, uid, lang, self.speaker, t0,
                                    seq_start=sum(p_["chunks"] for p_ in parts))
            self.speaking[str(uid)] = speaker
            speaker.feed(answer)               # every sentence at once; spoken one by one
        to_say = lv._speakable(spoken_line or answer or "")
        if not to_say.strip():
            to_say = "I do not have an answer for that." if not tool_calls else ""
        streamed = speaker.finish(answer, spoken_line) if speaker else None
        self.speaking.pop(str(uid), None)
        if parts:
            # the lead-in chunks before a tool call count towards the turn
            if not streamed:
                streamed = {"chunks": 0, "audio_seconds_out": 0.0, "bytes": 0, "first_audio_s": None, "spoke_by": ""}
            # a continuation speaker counts from where the lead-in stopped, so
            # its `chunks` is already the turn's total; only a turn that ended
            # without one (a timeout) has to add the lead-in's chunks itself
            streamed = {**streamed,
                        "chunks": streamed["chunks"] if streamed["chunks"] else sum(p_["chunks"] for p_ in parts),
                        "audio_seconds_out": round(streamed["audio_seconds_out"] + sum(p_["audio_seconds_out"] for p_ in parts), 3),
                        "bytes": streamed["bytes"] + sum(p_["bytes"] for p_ in parts),
                        "first_audio_s": next((p_["first_audio_s"] for p_ in parts if p_.get("first_audio_s") is not None), streamed.get("first_audio_s")),
                        "spoke_by": streamed.get("spoke_by") or parts[0].get("spoke_by") or ""}
            if not streamed["chunks"]:
                streamed = None
        if texts:
            answer = "\n\n".join(texts + ([answer] if (answer or "").strip() else []))
        if streamed:
            audio, secs_out, out_fmt, out_rate = b"", streamed["audio_seconds_out"], lv.REPLY_FORMAT, 0
            spoke_by = streamed["spoke_by"] or "-"
            voice = None                       # every byte of audio went as chunks
        elif not to_say.strip():
            audio, secs_out, out_fmt, out_rate, spoke_by, voice = b"", 0.0, lv.REPLY_FORMAT, 0, "-", None
        else:
            if speaker:
                self.log(f"stream {uid}: chunking fell back to the single blob")
            lang = voice_lang_for(to_say, pinned=self.lang or None, last=lang)
            audio, secs_out, out_fmt, out_rate, spoke_by = lv.speak(to_say, lang, self.speaker)
            voice = {"format": out_fmt, "b64": base64.b64encode(audio).decode()}
        reply = {"type": "reply", "id": uid, "turn_id": self.turn_id(uid), "text": answer,
                 **({"speech": spoken_line} if spoken_line else {}),
                 "user_text": user_text,
                 "lang": lang,
                 **({"speaker": self.speaker} if self.speaker else {}),
                 "voice": voice,
                 **({"streamed": streamed} if streamed else {}),
                 **({"tool_calls": tool_calls} if tool_calls else {}),
                 "audio_seconds": secs_in, "audio_seconds_out": round(secs_out, 3),
                 "peak_dbfs": None if peak == float("-inf") else round(peak, 1),
                 "reply_format": (f"{out_fmt} streamed {streamed['chunks']} chunks"
                                  f"{' (interrupted)' if streamed.get('interrupted') else ''} {lv.REPLY_BITRATE} {spoke_by}"
                                  if streamed else f"{out_fmt} {out_rate} Hz {lv.REPLY_BITRATE} {spoke_by}"),
                 "timing": {"stt_s": round(t_stt, 2), "think_s": round(t1 - t0 - t_stt, 2),
                            "tts_s": round(time.time() - t1, 2)},
                 "ts": ts}
        self.turns += 1
        self._agent(reply, meter={"id": uid, "audio_seconds": secs_in,
                                  "audio_seconds_out": round(secs_out, 3)})
        self.log(f"stream turn {uid}: {secs_in}s in, {secs_out:.1f}s out, stt {t_stt:.2f}s "
                 f"model {t1 - t0 - t_stt:.1f}s tts {time.time() - t1:.1f}s, "
                 + (f"first_audio {streamed['first_audio_s']}s in {streamed['chunks']} chunks, "
                    f"{streamed['bytes'] // 1024} KB" if streamed else f"{len(audio) // 1024} KB reply")
                 + f", lang={lang}{'' if self.lang else ' (heard)'} "
                 f"speaker={self.speaker or '-'}, peak {peak:.1f} dBFS, "
                 f"prefiltered={ctrl.get('prefiltered')}, reply {reply['reply_format']}, "
                 f"{self.partial_count} partials")
        self.partial_count = 0


# --------------------------------------------------------------- selftest
class _FakeWS:
    """A phone in a box: what the session would receive, and what it sent.
    Frames are handed over at REAL TIME — one audio frame per 100 ms — because
    a phone cannot deliver a sentence faster than it is spoken, and the partial
    worker only has something to do while the sentence is still arriving."""
    def __init__(self, frames, pace_s=FRAME_MS / 1000.0):
        self.inbox = list(frames)
        self.sent = []
        self.pace = pace_s

    def recv(self, timeout=None):
        if not self.inbox:
            import ws_min
            raise ws_min.ConnectionClosed("done")
        item = self.inbox.pop(0)
        if item == "END":
            import ws_min
            raise ws_min.ConnectionClosed("done")
        if item == "WAIT":
            # let both queued answers finish — 16 s with a resident GPU
            # recogniser, longer on a CPU install where each decode is a
            # one-shot whisper-cli run — handing over anything injected meanwhile
            for _ in range(int(getattr(self, "wait_s", 16) * 2)):
                time.sleep(0.5)
                inj = getattr(self, "injected", None)
                if inj:
                    self.inbox.insert(0, "WAIT")
                    return inj.pop(0)
            return self.recv(timeout)
        if isinstance(item, tuple) and item[0] == 2 and item[1][0] == KIND_AUDIO:
            time.sleep(self.pace)
        return item

    def send_text(self, s):
        self.sent.append(("text", s))

    def send_binary(self, b):
        self.sent.append(("binary", b))
        if getattr(self, "on_agent", None):
            try:
                self.on_agent(b)
            except Exception as e:
                print(f"[selftest] on_agent: {e}", file=sys.stderr)

    def inject(self, item):
        """A frame the phone sends in REACTION to the agent (a tool result)."""
        self.injected = getattr(self, "injected", []) + [item]


def _selftest():
    """Drive a session with a synthesised utterance, no network."""
    import ws_min  # noqa: F401
    key = os.urandom(32)
    prefix = os.urandom(4)
    counter = [0]

    def phone(kind, payload):
        counter[0] += 1
        return (2, seal_frame(kind, key, prefix, counter[0], payload))

    tools_mode = bool(os.environ.get("LQ_SELFTEST_TOOLS"))
    text = ("Please switch the app to dark mode." if tools_mode
            else "What time does the shipment leave the dock on Thursday?")
    a, secs, fmt, rate, who = lv.speak(text, "en", "af_heart")
    d = tempfile.mkdtemp()
    src = os.path.join(d, "u.m4a")
    open(src, "wb").write(a)
    wav = os.path.join(d, "u.wav")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-ar", "16000", "-ac", "1", wav], check=True)
    with wave.open(wav) as w:
        pcm = w.readframes(w.getnframes())
    step = RATE * 2 * FRAME_MS // 1000
    frames = [(1, json.dumps({"start": "sealed"}).encode())]      # opened by the fake opener
    frames.append(phone(KIND_CTRL, json.dumps({"type": "utterance_start", "id": "utt-1"}).encode()))
    n = 0
    for i in range(0, len(pcm), step):
        frames.append(phone(KIND_AUDIO, pcm[i:i + step].ljust(step, b"\0")))
        n += 1
    frames.append(phone(KIND_CTRL, json.dumps({"type": "utterance_end", "id": "utt-1", "seconds": round(n * 0.1, 1)}).encode()))
    # a second sentence straight after the first, while the first is being answered — in RUSSIAN,
    # so the language heard, not the language pinned, has to pick the voice
    # Long enough for the one-shot decoder to IDENTIFY it (SHORT_CLIP_S):
    # a CPU install continues a shorter sentence in the last language heard.
    ru_a, _, _, _, _ = lv.speak("Покажи мне, пожалуйста, отчёт о продажах за прошлую неделю и за этот месяц.", "ru", None)
    ru_src = os.path.join(d, "ru.m4a"); open(ru_src, "wb").write(ru_a)
    ru_wav = os.path.join(d, "ru.wav")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", ru_src, "-ar", "16000", "-ac", "1", ru_wav], check=True)
    with wave.open(ru_wav) as w:
        ru_pcm = w.readframes(w.getnframes())
    frames.append(phone(KIND_CTRL, json.dumps({"type": "utterance_start", "id": "utt-2"}).encode()))
    for i in range(0, len(ru_pcm), step):
        frames.append(phone(KIND_AUDIO, ru_pcm[i:i + step].ljust(step, b"\0")))
    frames.append(phone(KIND_CTRL, json.dumps({"type": "utterance_end", "id": "utt-2", "seconds": round(n * 0.1, 1)}).encode()))
    frames.append(phone(KIND_CTRL, json.dumps({"type": "heard_out", "seconds": 1.2}).encode()))
    frames.append("WAIT")
    frames.append("END")
    ws = _FakeWS(frames)
    ws.wait_s = float(os.environ.get("LQ_SELFTEST_WAIT_S") or (16 if ready() else 50))
    calls_seen = []

    def _on_agent(b):
        try:
            k, _n, payload = open_frame(key, b)
            o = json.loads(payload)
        except Exception:
            return
        if o.get("type") == "tool_call":
            calls_seen.append(o)
            ws.inject(phone(KIND_CTRL, json.dumps({"type": "tool_result", "turn_id": o.get("turn_id"),
                                                   "call_id": o["call_id"], "output": {"ok": True, "mode": "dark"}}).encode()))
    ws.on_agent = _on_agent
    start = {"type": "start", "lang": "auto", "speaker": "af_heart", "key_b64": base64.b64encode(key).decode(),
             "format": "pcm16", "rate": 16000, "frame_ms": 100, "tz": "America/Toronto",
             "greet": True, "ui_lang": "en", "name": "Alex",
             **({"tools": [{"type": "function", "name": "set_appearance",
                            "description": "Switch the app between light and dark mode.",
                            "parameters": {"type": "object", "properties": {"mode": {"type": "string", "enum": ["light", "dark"]}},
                                           "required": ["mode"]}}]} if tools_mode else {})}
    def answer_fn(q, on_text=None):
        # The model, as the bridge delivers it: the full text so far on each
        # delta, a few words at a time, with a think before the first one.
        # LQ_SELFTEST_NO_ON_TEXT=1 makes it the skill agent's shape instead:
        # the whole answer at once, nothing in between.
        if q.startswith(lv.TOOL_RESULT_MARK):
            full = "Done. The app is in dark mode now."
        elif tools_mode and "dark mode" in q.lower():
            full = ('Switching to dark mode.\n[tool_call] {"name": "set_appearance", "arguments": {"mode": "dark"}}')
        elif scripts_in(q).get("cyrl"):
            # The model answers in the language it was asked in; the voice follows the answer.
            full = (f"Вы спросили: {q} Док открывается в половине третьего. "
                    "Паллеты уходят дневным грузовиком, так что будьте там к двум. "
                    "Я добавил это в ваш список.")
        else:
            full = (f"You asked: {q} The dock opens at two thirty. "
                    "The pallets go out on the afternoon truck, so be there by two. "
                    "I have put it on your list.")
        time.sleep(4)          # long enough that the second sentence is decoded before this answer ends
        if on_text:
            words = full.split(" ")
            for i in range(1, len(words) + 1):
                on_text(" ".join(words[:i]))
                time.sleep(0.12)
        return full

    start["reply_stream"] = True
    if os.environ.get("LQ_SELFTEST_NO_ON_TEXT"):
        _full_fn = answer_fn
        answer_fn = lambda q: _full_fn(q)      # noqa: E731 — the skill agent's shape: no on_text at all

    sess = StreamSession(ws, lambda env: json.dumps(start), answer_fn,
                         account="selftest", on_transcript=lambda t, ts: print("  transcript:", t))
    t0 = time.time()
    try:
        sess.run()
    except Exception as e:
        print("  session ended:", type(e).__name__, e)
    # decode what the "phone" got
    seen = []
    stamps = []
    for kind, item in ws.sent:
        if kind == "text":
            meta = json.loads(item)
            frame = base64.b64decode(meta["frame"])
            k, nonce, payload = open_frame(key, frame)
            obj = json.loads(payload)
            seen.append((obj["type"], {kk: meta[kk] for kk in meta if kk != "frame"}, obj))
            stamps.append(round(time.time() - t0, 2))
        else:
            k, nonce, payload = open_frame(key, item)
            obj = json.loads(payload)
            seen.append((obj["type"], None, obj))
            stamps.append(round(time.time() - t0, 2))
    print(f"  frames from agent: {[s[0] for s in seen]}")
    for t, meter, obj in seen:
        if t == "partial":
            print(f"  partial: {obj['id']} {obj['text']!r}")
        if t == "final":
            print(f"  final:   {obj['id']} {obj['text']!r}")
        if t == "reply_chunk":
            print(f"  chunk {obj['seq']}{' FINAL' if obj.get('final') else ''}: {obj['text'][:50]!r} "
                  f"{obj['audio_seconds_out']}s audio")
        if t == "reply":
            v = obj.get("voice")
            print(f"  reply:   meter={meter} text={obj['text'][:60]!r} voice={len(v['b64']) if v else None} "
                  f"b64 chars {obj['reply_format']} timing={obj.get('timing')} streamed={obj.get('streamed')}")
    print(f"  wall {time.time() - t0:.1f}s, utterance {n * 0.1:.1f}s, backend {sess.recog.backend}")
    kinds = [s[0] for s in seen]
    order = [(s[0], s[2].get("id")) for s in seen if s[0] in ("final", "reply")]
    print(f"  order of finals and replies: {order}")
    print("  timeline:", [(k, o.get("id"), st) for (k, _m, o), st in zip(seen, stamps) if k in ("final", "reply", "reply_chunk", "partial")][:16])
    # With the resident recogniser the second sentence is transcribed before
    # the first reply (the #586 split). A CPU install decodes serially at the
    # clip path's speed, so there only the ORDER of the replies is promised.
    replies_in_order = (("final", "utt-2") in order and ("reply", "utt-1") in order and ("reply", "utt-2") in order
                        and order.index(("final", "utt-2")) < order.index(("reply", "utt-2"))
                        and order.index(("reply", "utt-1")) < order.index(("reply", "utt-2")))
    early = replies_in_order and order.index(("final", "utt-2")) < order.index(("reply", "utt-1"))
    in_order = early if sess.partials else replies_in_order
    print(f"  second sentence transcribed before the first reply: {early}; replies in order: {replies_in_order}"
          f" -> {'ok' if in_order else 'FAIL'} ({'partials' if sess.partials else 'no partials'})")
    r2 = [s[2] for s in seen if s[0] == "reply" and s[2].get("id") == "utt-2"]
    lang_ok = bool(r2) and r2[0].get("lang") == "ru" and "ru_RU" in (r2[0].get("reply_format") or "")
    print(f"  Russian sentence heard as ru and spoken by a Russian voice: {lang_ok} -> {r2[0].get('reply_format') if r2 else None} | final: {[s[2]['text'] for s in seen if s[0]=='final' and s[2].get('id')=='utt-2']}")
    greet_ok = (len(seen) > 1 and kinds[0] == "hello" and kinds[1] == "reply"
                and seen[1][2].get("id") == 0 and seen[1][2].get("greeting") is True
                and seen[1][1] is None)          # unmetered: no wrapper
    print(f"  greeting first, id 0, unmetered: {greet_ok} -> {seen[1][2].get('text')!r}" if len(seen) > 1 else "  no greeting")
    # judged on the FIRST sentence: the second queues behind it by design
    chunks = [o for k, _m, o in seen if k == "reply_chunk" and o.get("id") == "utt-1"]
    last = [o for k, _m, o in seen if k == "reply" and o.get("id") == "utt-1"]
    stream_ok = (len(chunks) >= 2 and chunks[-1].get("final") is True
                 and [c["seq"] for c in chunks] == list(range(1, len(chunks) + 1))
                 and bool(last) and last[-1].get("voice") is None
                 and (last[-1].get("streamed") or {}).get("chunks") == len(chunks))
    print(f"  streamed: {len(chunks)} chunk(s), first audio at "
          f"{(last[-1].get('streamed') or {}).get('first_audio_s') if last else None}s "
          f"after the utterance end -> {'ok' if stream_ok else 'FAIL'}")
    # request 497: every frame of a turn carries the same turn id, ending in the utterance id
    tids = [(o.get("id"), o.get("turn_id")) for k, _m, o in seen if k in ("final", "reply", "reply_chunk") and o.get("id") != 0]
    tid_ok = bool(tids) and all(t and str(t).endswith(f":{i}") for i, t in tids) and len({t.split(":")[0] for _i, t in tids}) == 1
    print(f"  turn ids on final/reply/reply_chunk: {tid_ok} -> {sorted({t for _i, t in tids})}")
    tools_ok = True
    if tools_mode:
        r1 = [o for k, _m, o in seen if k == "reply" and o.get("id") == "utt-1"]
        c1 = [o for k, _m, o in seen if k == "reply_chunk" and o.get("id") == "utt-1"]
        tools_ok = (len(calls_seen) == 1 and calls_seen[0].get("name") == "set_appearance"
                    and bool(r1) and (r1[-1].get("tool_calls") or [{}])[0].get("output") == {"ok": True, "mode": "dark"}
                    and "dark mode now" in (r1[-1].get("text") or "")
                    and "[tool_call]" not in (r1[-1].get("text") or "")
                    and [c["seq"] for c in c1] == list(range(1, len(c1) + 1))
                    and not any("[tool_call]" in (c.get("text") or "") for c in c1)
                    and sum(1 for c in c1 if c.get("final")) == 1)
        print(f"  phone tool: call seen {[(c.get('name'), c.get('arguments')) for c in calls_seen]}, "
              f"chunks {[ (c['seq'], c.get('final'), (c.get('text') or '')[:30]) for c in c1]}, "
              f"reply text {(r1[-1].get('text') if r1 else None)!r}, tool_calls {(r1[-1].get('tool_calls') if r1 else None)} -> {'ok' if tools_ok else 'FAIL'}")
    ok = (greet_ok and stream_ok and in_order and lang_ok and tid_ok and tools_ok and kinds.count("reply") >= 3
          and (any(s[0] == "partial" for s in seen) or not sess.partials)
          and (not any(s[0] == "partial" for s in seen) or sess.partials))
    print("SELFTEST", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if "--facts" in sys.argv:
        print(json.dumps(facts()))
        recogniser().stop()
