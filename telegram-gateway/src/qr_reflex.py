"""QR reflex — deterministic Agent Voice Mode login-QR minting for the gateway.

"create a qr code for voice mode app" runs voice/hosted/make_account_qr.py
directly (mint bearer on the VPS -> qrencode -> sendPhoto -> schedule the
chat-message deletion) with no LLM turn: ~3s instead of a full Claude turn.

The QR carries a LIVE CREDENTIAL for the owner's hosted account (acct-owner),
so the reflex is deliberately narrow:
  * only the owner himself can trigger it (OWNER sender id), and
  * only in chats where nobody else would see the QR (his DM + "Voice Claude");
  * the message must contain an action verb + "qr" + a voice-app context word,
    and must not be an informational question ("how does the qr login work?").
Anything else falls through to the normal Claude turn, which can still run the
script by hand (e.g. for a different account or chat).
"""
import tgconf as C   # identity from config
import os, re, subprocess, sys, time

SCRIPT = os.path.join(C.WORKSPACE_ROOT, "voice/hosted/make_account_qr.py")
ACCOUNT, NAME = "acct-owner", "the owner"
OWNER = C.OWNER_ID                          # the owner — it is HIS account credential
ALLOWED_CHATS = {C.OWNER_ID, C.EXAMPLE_CHAT_ID}   # his DM + "Voice Claude" (bot+the owner only)

ACTION = re.compile(r"\b(create|make|generate|mint|send|give|get|need|want|"
                    r"show|display|see|another|fresh|new|resend|re-send)\b", re.I)
QR = re.compile(r"\bqr\b", re.I)
CONTEXT = re.compile(r"\b(voice|app|login|log in|sign[- ]?in|account|phone)\b", re.I)
# Informational questions about the QR system should reach Claude, not get a
# QR dumped on them. Narrower than doc_reflex's guard on purpose: "can you
# make me a qr for the voice app" must still fire.
QUESTION = re.compile(r"\b(how|why|what|when|where|which|who|explain)\b", re.I)


def detect(text, chat_id=OWNER, sender_id=OWNER):
    t = (text or "").strip()
    return (chat_id in ALLOWED_CHATS and sender_id == OWNER
            and 0 < len(t) <= 160 and "\n" not in t
            and bool(QR.search(t)) and bool(ACTION.search(t))
            and bool(CONTEXT.search(t)) and not QUESTION.search(t))


def try_handle(chat_id, text, sender_id):
    """Returns a short summary string if fully handled (QR minted + sent),
    else None -> the gateway falls through to the normal Claude turn."""
    if not detect(text, chat_id, sender_id):
        return None
    t0 = time.time()
    r = subprocess.run(
        [sys.executable, SCRIPT, "--account", ACCOUNT, "--name", NAME,
         "--chat", str(chat_id)],
        capture_output=True, text=True, timeout=90)
    if r.returncode != 0:                  # mint/send failed -> log + let Claude try
        raise RuntimeError(f"make_account_qr rc={r.returncode}: "
                           f"{(r.stderr or '').strip()[-300:]}")
    ms = int((time.time() - t0) * 1000)
    return f"[qr reflex: minted+sent {ACCOUNT} login QR in {ms}ms]"


if __name__ == "__main__":
    print("detect ->", detect(" ".join(sys.argv[1:]) or "create a qr code for voice mode app"))
