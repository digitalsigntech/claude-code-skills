"""Saving something to personal notes, without waiting for the description.

the owner, 2026-08-07, having waited 11:37 → 11:39 for "Saved to your personal
notes": "it should take just a few seconds."

The two minutes were a vision call. The reply that came back — *a close-up of a
MacBook keyboard, labelled and tagged so you can find it by macbook or
keyboard* — is exactly where the time went, and the useful content of it, to
him, was the word SAVED.

SAME SPLIT AS #115: the file is RECEIVED, then it is FILED. Only the first half
needs him present. The bytes are stored and answered for immediately; the
label and keywords are produced behind that and written into the note when they
arrive. He can already search his notes by date and filename — tags improve
that search, they do not gate it.

PRIVACY: this is the owner's private store. Nothing here reads a note back, and
the follow-up line ("that was the MacBook keyboard") goes only to a chat
personal_notes.allowed_chat() approves — the same gate that guards sending.
"""


import tgconf as C   # identity from config
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time

HOME = os.path.expanduser("~")
CAMERA = f"{C.WORKSPACE_ROOT}/voice/realtime/camera"
# The DB lives under workspace/personal, not ~/personal — getting this wrong would
# have written labels into a database nobody reads.
NOTES_DB = os.environ.get("NOTES_DB", f"{C.WORKSPACE_ROOT}/personal/notes.db")
FRESH_S = 1800
CLAUDE = f"{HOME}/.local/bin/claude"

_PROMPT = (
    "Look at the image at {path}. Reply with exactly two lines and nothing "
    "else:\nLABEL: <one short phrase naming what it is>\n"
    "KEYWORDS: <5-10 comma-separated words someone might search by>"
)


def _label_later(note_id, path, chat_id=None):
    """Describe the picture and write it into the note. Runs AFTER the answer.

    Failure here is silent on purpose: the note is already saved and findable
    by date and filename. A missing label is a slightly worse search, not a
    lost file — so this must never be able to turn a successful save into an
    error message."""
    try:
        out = subprocess.run(
            [CLAUDE, "-p", _PROMPT.format(path=path),
             "--dangerously-skip-permissions"],
            capture_output=True, text=True, timeout=300)
        txt = out.stdout or ""
        label = (re.search(r"LABEL:\s*(.+)", txt) or [None, None])[1]
        keys = (re.search(r"KEYWORDS:\s*(.+)", txt) or [None, None])[1]
        if not label:
            return
        con = sqlite3.connect(NOTES_DB, timeout=10)
        with con:
            con.execute("UPDATE notes SET label=?, keywords=? WHERE id=?",
                        (label.strip(), (keys or "").strip(), int(note_id)))
        con.close()
        print(f"[personal_note_reflex] note {note_id} labelled: {label.strip()}",
              flush=True)
        if chat_id:
            # The promised second line, and only to a chat the gate approves.
            import personal_notes
            if personal_notes.allowed_chat(chat_id):
                import tg_api as TG
                TG.send_message(chat_id,
                                f"That note was {label.strip().lower()} — "
                                f"searchable by {keys or 'its date'}.")
    except Exception as e:
        print(f"[personal_note_reflex] labelling failed: {e}", flush=True)


def _newest_upload(max_age=FRESH_S):
    try:
        files = [(os.path.getmtime(os.path.join(CAMERA, n)),
                  os.path.join(CAMERA, n))
                 for n in os.listdir(CAMERA) if not n.startswith(".")]
    except OSError:
        return None
    if not files:
        return None
    ts, path = max(files)
    return path if time.time() - ts <= max_age else None


def save_it(path=None, chat_id=None):
    """(answer, note_id). Stores and answers; labels behind the answer."""
    src = path or _newest_upload()
    if not src or not os.path.exists(src):
        return "I do not have a recent photo to save — send it first.", None
    import personal_notes
    # add() MOVES the file; the camera copy is the app's record of what was
    # sent, so hand over a copy rather than emptying that folder.
    tmp = os.path.join("/tmp", os.path.basename(src))
    shutil.copy2(src, tmp)
    note_id, dest = personal_notes.add(tmp, orig_name=os.path.basename(src))
    threading.Thread(target=_label_later, args=(note_id, dest, chat_id),
                     daemon=True).start()
    return ("Saved to your personal notes. I am looking at it now and will "
            "tag it so you can find it by what it shows."), note_id


NOTE_NOUN = re.compile(r"\b(personal notes?|my notes?|private notes?)\b", re.I)
SAVE_ISH = re.compile(r"\b(save|store|keep|add|put|file)\b", re.I)
NOT_SAVE = re.compile(r"\b(search|find|look up|show|list|send|read|what|"
                      r"remove|delete)\b", re.I)


def detect(text):
    t = (text or "").strip()
    if not t or len(t) > 160 or t.startswith("/"):
        return False
    if NOT_SAVE.search(t):
        return False
    return bool(NOTE_NOUN.search(t)) and bool(SAVE_ISH.search(t))


def try_handle(chat_id, text, send):
    if not detect(text):
        return None
    answer, note_id = save_it(chat_id=chat_id)
    send(chat_id, answer)
    return f"personal note reflex: note {note_id}" if note_id else \
        "personal note reflex: nothing to save"


if __name__ == "__main__":
    q = " ".join(_sys.argv[1:])
    if q:
        print(f"detect({q!r}) = {detect(q)}")
