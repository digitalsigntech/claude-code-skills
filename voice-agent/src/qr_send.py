#!/usr/bin/env python3
"""Send the pairing QR into a Telegram chat, and take it back out again.

A login QR is a credential with a clock on it. Posting one into a chat is the
fastest way to get it onto a phone, and leaving it there afterwards is the whole
reason it has an expiry in the first place — the code dies server-side, but the
image stays in the chat history looking exactly like a live one.

Deleting it was previously a line of instructions telling the installing agent to
schedule the deletion itself. Instructions are not a mechanism: the agent that
forgets, or finishes its session, leaves the credential sitting there. So the send
records what it posted, and the deletion is swept by whatever runs next — the
adapter's own background thread, or the next pair.py run. Both are idempotent, so
neither has to know about the other.

    python3 qr_send.py --chat <id> --png pairing-qr.png --expires <epoch>
    python3 qr_send.py --sweep          # delete anything now past its expiry

The bot token is found, not configured: $TELEGRAM_BOT_TOKEN / $TG_BOT_TOKEN, then
a `telegram/bot_token` file in the agent's workdir — where an agent that already
talks to its user over Telegram keeps it.
"""
import argparse, json, os, pathlib, sys, time, urllib.error, urllib.request

HERE = pathlib.Path(__file__).resolve().parent
PENDING = HERE / "pending_qr.json"
API = "https://api.telegram.org/bot{token}/{method}"


def _cfg():
    try:
        return json.loads((HERE / "config.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def bot_token():
    for env in ("TELEGRAM_BOT_TOKEN", "TG_BOT_TOKEN"):
        if os.environ.get(env):
            return os.environ[env].strip()
    workdir = os.path.expanduser(str(_cfg().get("workdir") or ""))
    for cand in ([pathlib.Path(workdir) / "telegram" / "bot_token"] if workdir else []) + \
                [pathlib.Path.home() / ".config" / "telegram" / "bot_token"]:
        try:
            tok = cand.read_text().strip()
            if tok:
                return tok
        except OSError:
            continue
    return ""


def _call(token, method, params=None, files=None):
    url = API.format(token=token, method=method)
    if files:
        boundary = "----voiceagent" + str(int(time.time() * 1000))
        body = b""
        for k, v in (params or {}).items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n"
                     f"{v}\r\n").encode()
        for k, path in files.items():
            name = os.path.basename(path)
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; "
                     f"filename=\"{name}\"\r\nContent-Type: image/png\r\n\r\n").encode()
            body += pathlib.Path(path).read_bytes() + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type":
                                              f"multipart/form-data; boundary={boundary}"})
    else:
        req = urllib.request.Request(url, data=json.dumps(params or {}).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def _pending():
    try:
        return json.loads(PENDING.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_pending(rows):
    PENDING.write_text(json.dumps(rows, indent=2) + "\n")
    try:
        PENDING.chmod(0o600)
    except OSError:
        pass


def send(chat_id, png, expires, caption=""):
    """Post the QR and write down what has to be taken back."""
    token = bot_token()
    if not token:
        raise SystemExit(
            "no Telegram bot token found. Set $TELEGRAM_BOT_TOKEN, or show the PNG "
            "to your user another way — but then delete it yourself at expiry.")
    r = _call(token, "sendPhoto", {"chat_id": str(chat_id), "caption": caption},
              {"photo": str(png)})
    if not r.get("ok"):
        raise SystemExit(f"Telegram refused the photo: {r}")
    mid = r["result"]["message_id"]
    rows = _pending()
    rows.append({"chat_id": chat_id, "message_id": mid, "expires": float(expires)})
    _save_pending(rows)
    print(f"[qr] sent to chat {chat_id} (message {mid}); deletes at "
          + time.strftime("%H:%M", time.localtime(expires)))
    return mid


def sweep(now=None):
    """Delete every posted QR that is past its expiry. Safe to run from anywhere,
    as often as anything likes: a message already gone counts as deleted."""
    rows, now = _pending(), now or time.time()
    if not rows:
        return 0
    token = bot_token()
    keep, gone = [], 0
    for row in rows:
        if row.get("expires", 0) > now:
            keep.append(row)
            continue
        if not token:
            keep.append(row)          # cannot act; do not forget it either
            continue
        try:
            _call(token, "deleteMessage", {"chat_id": str(row["chat_id"]),
                                           "message_id": row["message_id"]})
            gone += 1
        except urllib.error.HTTPError as e:
            # Already deleted, too old to delete, or the bot lost access: the
            # record has no future either way, so drop it rather than retry it
            # forever. Anything else is transient — keep it for the next sweep.
            if e.code in (400, 403):
                gone += 1
            else:
                keep.append(row)
        except (urllib.error.URLError, OSError):
            keep.append(row)
    if gone:
        _save_pending(keep)
        print(f"[qr] deleted {gone} expired QR message(s)", flush=True)
    return gone


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat", help="Telegram chat id to send the QR to")
    ap.add_argument("--png", default=str(HERE / "pairing-qr.png"))
    ap.add_argument("--expires", type=float, help="epoch seconds when it dies")
    ap.add_argument("--caption", default="")
    ap.add_argument("--sweep", action="store_true", help="delete anything expired")
    a = ap.parse_args()
    if a.sweep:
        sys.exit(0 if sweep() >= 0 else 1)
    if not (a.chat and a.expires):
        raise SystemExit("need --chat and --expires (or --sweep)")
    send(a.chat, a.png, a.expires, a.caption)
