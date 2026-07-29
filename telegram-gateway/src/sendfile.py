#!/usr/bin/env python3
"""Send a file into a Telegram chat THROUGH tg_api — the one supported way.

    python3 ~/DST/telegram/sendfile.py <chat_id> <path> [caption]

Why this exists (2026-07-29): Claude turns were improvising raw Bot API calls
with the bot token. Those sends work, but they bypass tg_api._call — no
attachment spool (the voice app's GET /attachments feed misses the file), no
[sent file:] archive marker in chat.db. Going through tg_api gives both.
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tg_api


def _pdf_thumb(path):
    """First-page JPEG thumbnail for a PDF (Bot API: <=320px, <=200KB).
    Telegram often shows NO preview for bot-sent PDFs unless the bot attaches
    one explicitly. Returns a temp path or None."""
    try:
        out = tempfile.mktemp(suffix=".jpg")
        subprocess.run(["pdftoppm", "-jpeg", "-f", "1", "-l", "1",
                        "-scale-to", "320", "-singlefile", path, out[:-4]],
                       check=True, timeout=20, capture_output=True)
        if os.path.exists(out) and os.path.getsize(out) <= 200_000:
            return out
    except Exception:
        pass
    return None


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    chat_id, path = int(sys.argv[1]), sys.argv[2]
    caption = " ".join(sys.argv[3:])[:1000]
    if not os.path.exists(path):
        sys.exit(f"no such file: {path}")
    name = os.path.basename(path)
    as_photo = path.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
    attempts = ([("sendPhoto", "photo"), ("sendDocument", "document")]
                if as_photo else [("sendDocument", "document")])
    thumb = _pdf_thumb(path) if path.lower().endswith(".pdf") else None
    for method, field in attempts:
        files = {field: open(path, "rb")}
        if thumb and method == "sendDocument":
            files["thumbnail"] = open(thumb, "rb")
        try:
            r = tg_api._call(method, _files=files, _timeout=120,
                             chat_id=chat_id,
                             **({"caption": caption} if caption else {}))
        finally:
            for fh in files.values():
                fh.close()
        if r.get("ok"):
            try:                # archive marker, best-effort like the gateway
                sys.path.insert(0, os.path.expanduser("~/DST/chatlog"))
                import chatdb
                chatdb.record(f"[sent file: {name}]"
                              + (f" {caption}" if caption else ""),
                              "out", sender="claude", chat_id=chat_id,
                              kind="file")
            except Exception:
                pass
            print(f"sent {name} to {chat_id} via {method}")
            return
    sys.exit(f"send FAILED: {r.get('error')}")


if __name__ == "__main__":
    main()
