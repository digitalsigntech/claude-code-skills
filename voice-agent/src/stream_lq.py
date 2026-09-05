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
import json
import math
import os
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
        if getattr(self, "_no_gpu", False):
            return False
        if not self.ensure():
            return False
        if self.backend in ("vulkan", "cuda", "metal"):
            return True
        self._no_gpu = True
        self.stop()
        return False

    def decode(self, pcm, lang=None, audio_ctx=None):
        """Text for PCM16/16 kHz mono bytes, via the resident server."""
        if not self.ensure():
            raise RuntimeError("recogniser not running")
        boundary = "----lq" + uuid.uuid4().hex
        wav_io = tempfile.SpooledTemporaryFile(max_size=1 << 20)
        with wave.open(wav_io, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(pcm)
        wav_io.seek(0)
        wav = wav_io.read()
        fields = {"response_format": "json", "temperature": "0",
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
        return " ".join(str(d.get("text") or "").split())

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
    """What the local row says about streaming — by proof, not by class."""
    r = recogniser()
    ok = ready()
    return {"stream": bool(ok),
            **({"stream_recogniser": os.path.basename(r.model),
                "stream_backend": r.backend,
                "stream_partial_ms": PARTIAL_MS} if ok else {})}


# --------------------------------------------------------------- session
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
        self.account = account
        self.on_transcript = on_transcript
        self.log = log or (lambda *a: print("[stream]", *a, file=sys.stderr))
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
        if not self.recog.ensure():
            self._agent({"type": "error", "message": "recogniser not running"})
            raise RuntimeError("recogniser not running")
        self._agent({"type": "hello",
                     "recogniser": os.path.basename(self.recog.model),
                     "backend": self.recog.backend,
                     "partial_every_ms": PARTIAL_MS,
                     "progress_every_s": PROGRESS_S,
                     "max_utterance_s": MAX_UTTERANCE_S,
                     "frame_ms": FRAME_MS})
        self.log(f"stream open: lang={self.lang or 'auto'} speaker={self.speaker or '-'} "
                 f"backend={self.recog.backend}")
        worker = threading.Thread(target=self._partials, daemon=True)
        worker.start()
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
            self.utt_id = str(c.get("id") or "")
        elif t == "utterance_end":
            self.ending.set()
            uid = str(c.get("id") or self.utt_id or f"u{self.utt_seq + 1}")
            self.utt_seq += 1
            pcm = bytes(self.buf)
            self.buf = bytearray()
            frames, self.frames = self.frames, 0
            self.dirty.clear()
            self.last_partial = ""
            self.utt_id = None
            try:
                self._finish(uid, pcm, frames, c)
            finally:
                self.ending.clear()
        elif t == "utterance_cancel":
            self.buf = bytearray()
            self.frames = 0
            self.dirty.clear()
            self.last_partial = ""
            self.utt_id = None
        elif t == "heard_out":
            self.heard_out = c.get("seconds")

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
                    text = self.recog.decode(tail, self.lang or "auto",
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
                self._agent({"type": "partial", "id": self.utt_id or f"u{self.utt_seq + 1}",
                             "text": text})

    def _finish(self, uid, pcm, frames, ctrl):
        t0 = time.time()
        secs_in = round(frames * FRAME_MS / 1000.0, 3)
        declared = ctrl.get("seconds")
        if isinstance(declared, (int, float)) and abs(declared - secs_in) > max(0.5, 0.25 * secs_in):
            self.log(f"utterance {uid}: phone says {declared}s, {secs_in}s of frames arrived")
        peak = pcm_peak_dbfs(pcm)
        if not pcm or peak < lv.SILENCE_PEAK_DBFS:
            self._agent({"type": "no_speech", "id": uid, "peak_dbfs": None if peak == float("-inf") else round(peak, 1)},
                        meter={"id": uid, "audio_seconds": secs_in, "audio_seconds_out": 0.0, "no_speech": True})
            return
        try:
            with self.decode_lock:
                heard = self.recog.decode(pcm, self.lang or "auto", audio_ctx_for(secs_in))
        except Exception as e:
            self._agent({"type": "error", "id": uid, "message": f"recogniser failed: {str(e)[:80]}"})
            return
        user_text = untangle(lv.speech_text(heard))
        t_stt = time.time() - t0
        if user_text and lv.phantom_gate(user_text, secs_in, ctrl.get("prefiltered"), peak):
            self.log(f"phantom dropped: {user_text!r} ({secs_in}s, prefiltered={ctrl.get('prefiltered')})")
            heard, user_text = user_text, ""
        if not user_text:
            self._agent({"type": "no_speech", "id": uid, "heard_marker": heard[:40]},
                        meter={"id": uid, "audio_seconds": secs_in, "audio_seconds_out": 0.0, "no_speech": True})
            return
        ts = time.time()
        self._agent({"type": "final", "id": uid, "text": user_text})
        if self.on_transcript:
            try:
                self.on_transcript(user_text, ts)
            except Exception as e:
                self.log(f"posting the transcript failed: {e}")
        lv.remember_lang(self.account, self.lang or "")
        # THE SOCKET IS NEVER SILENT WHILE THE MODEL THINKS (2026-09-05, 17:37
        # UTC): the phone closed a working stream 19 s after the final because
        # nothing had arrived since — a model turn on a real question runs
        # 10–30 s. A `progress` frame every few seconds says the reply is on
        # its way, and gives the app something to draw.
        box = {}

        def _think():
            try:
                box["answer"] = str(self.answer_fn(user_text) or "")
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
        answer = box.get("answer", "")
        answer, speak_all = lv.read_in_full(answer)
        t1 = time.time()
        spoken_line = "" if speak_all else lv.speech_for(answer or "")
        to_say = lv._speakable(spoken_line or answer or "")
        if not to_say.strip():
            to_say = "I do not have an answer for that."
        audio, secs_out, out_fmt, out_rate, spoke_by = lv.speak(to_say, self.lang or "en", self.speaker)
        reply = {"type": "reply", "id": uid, "text": answer,
                 **({"speech": spoken_line} if spoken_line else {}),
                 "user_text": user_text,
                 **({"lang": self.lang} if self.lang else {}),
                 **({"speaker": self.speaker} if self.speaker else {}),
                 "voice": {"format": out_fmt, "b64": base64.b64encode(audio).decode()},
                 "audio_seconds": secs_in, "audio_seconds_out": round(secs_out, 3),
                 "peak_dbfs": None if peak == float("-inf") else round(peak, 1),
                 "reply_format": f"{out_fmt} {out_rate} Hz {lv.REPLY_BITRATE} {spoke_by}",
                 "timing": {"stt_s": round(t_stt, 2), "think_s": round(t1 - t0 - t_stt, 2),
                            "tts_s": round(time.time() - t1, 2)},
                 "ts": ts}
        self.turns += 1
        self._agent(reply, meter={"id": uid, "audio_seconds": secs_in,
                                  "audio_seconds_out": round(secs_out, 3)})
        self.log(f"stream turn {uid}: {secs_in}s in, {secs_out:.1f}s out, stt {t_stt:.2f}s "
                 f"model {t1 - t0 - t_stt:.1f}s tts {time.time() - t1:.1f}s, "
                 f"{len(audio) // 1024} KB reply, lang={self.lang or 'auto'} "
                 f"speaker={self.speaker or '-'}, reply {reply['reply_format']}, "
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
        if isinstance(item, tuple) and item[0] == 2 and item[1][0] == KIND_AUDIO:
            time.sleep(self.pace)
        return item

    def send_text(self, s):
        self.sent.append(("text", s))

    def send_binary(self, b):
        self.sent.append(("binary", b))


def _selftest():
    """Drive a session with a synthesised utterance, no network."""
    import ws_min  # noqa: F401
    key = os.urandom(32)
    prefix = os.urandom(4)
    counter = [0]

    def phone(kind, payload):
        counter[0] += 1
        return (2, seal_frame(kind, key, prefix, counter[0], payload))

    text = "What time does the shipment leave the dock on Thursday?"
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
    frames.append(phone(KIND_CTRL, json.dumps({"type": "heard_out", "seconds": 1.2}).encode()))
    frames.append("END")
    ws = _FakeWS(frames)
    start = {"type": "start", "lang": "en", "speaker": "af_heart", "key_b64": base64.b64encode(key).decode(),
             "format": "pcm16", "rate": 16000, "frame_ms": 100, "tz": "America/Toronto"}
    sess = StreamSession(ws, lambda env: json.dumps(start),
                         lambda q: (time.sleep(5), f"You asked: {q} The dock opens at two thirty.")[1],
                         account="selftest", on_transcript=lambda t, ts: print("  transcript:", t))
    t0 = time.time()
    try:
        sess.run()
    except Exception as e:
        print("  session ended:", type(e).__name__, e)
    # decode what the "phone" got
    seen = []
    for kind, item in ws.sent:
        if kind == "text":
            meta = json.loads(item)
            frame = base64.b64decode(meta["frame"])
            k, nonce, payload = open_frame(key, frame)
            obj = json.loads(payload)
            seen.append((obj["type"], {kk: meta[kk] for kk in meta if kk != "frame"}, obj))
        else:
            k, nonce, payload = open_frame(key, item)
            obj = json.loads(payload)
            seen.append((obj["type"], None, obj))
    print(f"  frames from agent: {[s[0] for s in seen]}")
    for t, meter, obj in seen:
        if t == "partial":
            print(f"  partial: {obj['id']} {obj['text']!r}")
        if t == "final":
            print(f"  final:   {obj['id']} {obj['text']!r}")
        if t == "reply":
            print(f"  reply:   meter={meter} text={obj['text'][:60]!r} voice={len(obj['voice']['b64'])} b64 chars {obj['reply_format']} timing={obj['timing']}")
    print(f"  wall {time.time() - t0:.1f}s, utterance {n * 0.1:.1f}s, backend {sess.recog.backend}")
    ok = (any(s[0] == "final" for s in seen) and any(s[0] == "reply" for s in seen)
          and any(s[0] == "partial" for s in seen) and any(s[0] == "hello" for s in seen))
    print("SELFTEST", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if "--facts" in sys.argv:
        print(json.dumps(facts()))
        recogniser().stop()
