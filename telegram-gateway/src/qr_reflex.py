"""QR reflex — deterministic login-QR minting for the Telegram gateway.

"create a qr code for the voice app" runs your QR-minting script directly
(TG_QR_SCRIPT, called as `<script> --chat <chat_id>`) with no LLM turn: a few
seconds instead of a full model round trip. Built for setups where the QR
carries a short-lived login credential (e.g. a companion app's account QR),
so the reflex is deliberately narrow:
  * only the owner (tgconf OWNER_ID) can trigger it, and
  * only in chats listed in TG_QR_CHATS (comma-separated ids — keep this to
    chats nobody else can read);
  * the message must contain an action verb + "qr" + a context word, and must
    not be an informational question ("how does the qr login work?").
Anything else falls through to the normal Claude turn. Disabled unless
TG_QR_SCRIPT is set (see tgconf.QR_REFLEX).
"""
import re, shlex, subprocess, time

import tgconf as C

ACTION = re.compile(r"\b(create|make|generate|mint|send|give|get|need|want|"
                    r"another|fresh|new|resend|re-send)\b", re.I)
QR = re.compile(r"\bqr\b", re.I)
CONTEXT = re.compile(r"\b(voice|app|login|log in|sign[- ]?in|account|phone)\b", re.I)
# Informational questions about the QR system should reach Claude, not get a
# QR dumped on them. Narrower than doc_reflex's guard on purpose: "can you
# make me a qr for the app" must still fire.
QUESTION = re.compile(r"\b(how|why|what|when|where|which|who|explain)\b", re.I)


def detect(text, chat_id, sender_id):
    t = (text or "").strip()
    return (chat_id in C.QR_CHATS and sender_id == C.OWNER_ID
            and 0 < len(t) <= 160 and "\n" not in t
            and bool(QR.search(t)) and bool(ACTION.search(t))
            and bool(CONTEXT.search(t)) and not QUESTION.search(t))


def try_handle(chat_id, text, sender_id):
    """Returns a short summary string if fully handled (QR minted + sent),
    else None -> the gateway falls through to the normal Claude turn."""
    if not C.QR_SCRIPT or not detect(text, chat_id, sender_id):
        return None
    t0 = time.time()
    r = subprocess.run(shlex.split(C.QR_SCRIPT) + ["--chat", str(chat_id)],
                       capture_output=True, text=True, timeout=90)
    if r.returncode != 0:                  # mint/send failed -> log + let Claude try
        raise RuntimeError(f"qr script rc={r.returncode}: "
                           f"{(r.stderr or '').strip()[-300:]}")
    ms = int((time.time() - t0) * 1000)
    return f"[qr reflex: minted+sent login QR in {ms}ms]"
