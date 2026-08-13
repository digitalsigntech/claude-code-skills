"""Filing a scan into the knowledge base, without a model turn.

the owner, 2026-08-07, after scanning a document from the app: "the round trip of
saving the PDF in knowledge base was too long, we need to shorten it as much as
possible."

MEASURED FIRST, because the obvious suspect was innocent: an incremental KB
index takes **0.75s** over 34,655 chunks, and the upload is a few hundred
kilobytes. Neither explains half a minute. The cost was a full model turn
deciding where the file should go and what to call it — the same shape as the
reminders amendment that went from 31s to 0.03s.

WHAT MACLAUDE GOT RIGHT, and it is the better half of the fix: filing is not
one operation to the person doing it. It is *received*, then it is *filed*, and
only the first half needs him present. So this answers the moment the bytes are
copied, and does the text extraction and the index refresh behind that answer.

WHAT IT DELIBERATELY DOES NOT DO: choose a clever name or a clever folder. It
files by date and original filename into knowledge-base/from-scans/, says so,
and leaves renaming to a later turn if anyone wants one. A wrong name filed
instantly is easy to fix; thirty seconds of silence is not.
"""


import tgconf as C   # identity from config
import os
import re
import shutil
import subprocess
import threading
import time

HOME = os.path.expanduser("~")
CAMERA = f"{C.WORKSPACE_ROOT}/voice/realtime/camera"
DEST = os.environ.get("KB_SCAN_DIR", f"{C.WORKSPACE_ROOT}/knowledge-base/from-scans")
KB_CLI = f"{C.WORKSPACE_ROOT}/email/kb/kb"
FRESH_S = 1800          # a scan older than this is not "this document"


def _newest_upload(max_age=FRESH_S):
    """The most recent scan or photo, or None. Filing acts on what he just
    sent; if nothing arrived recently there is nothing to file, and saying so
    beats filing last week's picture."""
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


def _slug(name):
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.basename(name)).strip("-")
    return base or "scan"


def _index_later(dest_path):
    """Extraction and indexing, behind the answer. Nothing here is allowed to
    fail loudly: the document is already filed, and a failed index is a search
    that misses it, not a document that was lost."""
    try:
        if dest_path.lower().endswith(".pdf"):
            md = os.path.splitext(dest_path)[0] + ".md"
            txt = subprocess.run(["pdftotext", "-layout", dest_path, "-"],
                                 capture_output=True, text=True, timeout=120)
            if txt.returncode == 0 and txt.stdout.strip():
                with open(md, "w") as fh:
                    fh.write(f"# {os.path.basename(dest_path)}\n\n"
                             f"Source file: {os.path.basename(dest_path)}\n\n"
                             f"_Scanned from the app and converted on "
                             f"{time.strftime('%Y-%m-%d')}._\n\n"
                             + txt.stdout)
        subprocess.run([KB_CLI, "index"], capture_output=True, timeout=600)
    except Exception as e:
        print(f"[kb_file_reflex] background step failed: {e}", flush=True)


def file_it(path=None):
    """(answer, filed_path). Copies, answers, and indexes afterwards."""
    src = path or _newest_upload()
    if not src or not os.path.exists(src):
        return ("I do not have a recent scan to file — send or scan the "
                "document first."), None
    os.makedirs(DEST, exist_ok=True)
    dest = os.path.join(DEST, time.strftime("%Y%m%d-") + _slug(src))
    n = 1
    while os.path.exists(dest):
        root, ext = os.path.splitext(dest)
        dest, n = f"{root}-{n}{ext}", n + 1
    shutil.copy2(src, dest)
    threading.Thread(target=_index_later, args=(dest,), daemon=True).start()
    rel = dest.replace(HOME + "/workspace/", "")
    return (f"Filed as {os.path.basename(dest)} in {os.path.dirname(rel)}. "
            f"Reading the text out of it and indexing now — it will be "
            f"searchable in a moment."), dest


# "save this to the knowledge base", "file this in the KB", "add this document
# to the knowledge base". NOT "what is in the knowledge base" — that is a
# search, and answering it by filing something would be absurd.
KB_NOUN = re.compile(r"\b(knowledge[- ]?base|\bkb\b)\b", re.I)
SAVE_ISH = re.compile(r"\b(save|file|add|put|store|keep|upload)\b", re.I)
NOT_SAVE = re.compile(r"\b(search|find|look up|what is|what's in|show|list|"
                      r"remove|delete)\b", re.I)


def detect(text):
    t = (text or "").strip()
    if not t or len(t) > 160 or t.startswith("/"):
        return False
    if NOT_SAVE.search(t):
        return False
    return bool(KB_NOUN.search(t)) and bool(SAVE_ISH.search(t))


def try_handle(chat_id, text, send):
    if not detect(text):
        return None
    answer, dest = file_it()
    send(chat_id, answer)
    return f"kb file reflex: {os.path.basename(dest) if dest else 'nothing to file'}"


if __name__ == "__main__":
    q = " ".join(_sys.argv[1:])
    if q:
        print(f"detect({q!r}) = {detect(q)}")
