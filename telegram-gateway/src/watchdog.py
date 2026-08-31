#!/usr/bin/env python3
"""Notice when the bot has gone quiet, and say so out of band.

    python3 watchdog.py            # from cron, every few minutes
    python3 watchdog.py --dry-run  # print the verdict, send nothing

The failure this exists for: the gateway process is up, `systemctl is-active` says
active, Telegram is delivering — and every message dies in the handler, so the bot
receives everything and answers nothing. Nothing on the machine notices, because
every component reports itself healthy. The only detector is a person who eventually
asks why they are being ignored, and by then it has been hours.

Two signals, both cheap:

  * an inbound message in the archive with no outbound after it, older than
    QUIET_MINUTES — the shape of a bot that is listening and not replying;
  * a traceback in the gateway log since the last check.

The alert goes straight to the Bot API from THIS process. Routing it through the
gateway would mean asking the broken thing to report that it is broken — the one
component known to be failing when this fires.
"""
import argparse, json, os, pathlib, re, sqlite3, sys, time, urllib.request

HERE = pathlib.Path(__file__).resolve().parent
STATE = HERE / "watchdog_state.json"
QUIET_MINUTES = int(os.environ.get("TG_WATCHDOG_QUIET_MIN", "10"))
REALERT_MINUTES = 60


def _cfg():
    sys.path.insert(0, str(HERE))
    import tgconf
    return tgconf


def _state():
    try:
        return json.loads(STATE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(d):
    STATE.write_text(json.dumps(d, indent=2) + "\n")


def telegram_chats(C):
    """The chat ids the Telegram gateway itself serves. chatlog/chat.db is shared
    with the voice app, which archives each app session under a synthetic chat id
    of its own — so "newest row is inbound" over the whole table answers a question
    this watchdog was not asked: it reports the app's conversations as a broken
    Telegram bot, and points at logs/gateway.log where there is nothing to find
    (the owner, 2026-08-15, after an hourly alert about an app message)."""
    ids = set()
    for attr in ("OWNER_ID", "SECOND_OWNER_ID"):
        val = getattr(C, attr, 0)
        if val:
            ids.add(int(val))
    for attr in ("ALWAYS_CLAUDE_CHATS", "ALWAYS_NEMOTRON_CHATS", "VOICE_CHATS", "QR_CHATS"):
        ids.update(int(x) for x in (getattr(C, attr, None) or ()))
    ids.update(int(x) for x in (getattr(C, "PROJECT_CHATS", None) or {}))
    allow = getattr(C, "allowlist", None)
    if callable(allow):
        try:
            ids.update(int(x) for x in (allow() or ()))
        except Exception:
            pass
    return ids


def unanswered(db_path, quiet_seconds, chat_ids=()):
    """The newest message in a chat the gateway serves is inbound and has been
    sitting there. Returns the text of the message nobody answered, or None.

    chat_ids scopes the question to the Telegram channel; an empty set means we
    could not work out which chats those are, and we say nothing rather than
    blame the gateway for another channel's silence."""
    chat_ids = [int(c) for c in chat_ids]
    if not chat_ids:
        return None
    try:
        cx = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
        row = cx.execute(
            "SELECT epoch, direction, text FROM messages "
            f"WHERE chat_id IN ({','.join('?' * len(chat_ids))}) "
            "ORDER BY epoch DESC LIMIT 1", chat_ids).fetchone()
        cx.close()
    except sqlite3.Error:
        return None
    if not row:
        return None
    epoch, direction, text = row
    if direction != "in":
        return None
    # A bracketed line is this archive's convention for a state marker, not
    # something a person said — "[cleared context — new conversation]" is written
    # inbound when the voice app clears the thread. Nobody is waiting on an answer
    # to it, so it is not evidence of a bot ignoring anyone (the owner, 2026-08-16:
    # an hourly alert quoting exactly that line).
    body = str(text or "").strip()
    if body.startswith("[") and body.endswith("]"):
        return None
    if time.time() - float(epoch or 0) < quiet_seconds:
        return None                     # still within a plausible thinking time
    return str(text or "")[:120]


def tracebacks(log_path, offset):
    """Exceptions in the bytes APPENDED since the last run, and the new offset.

    The first version gated on the file's mtime and then scanned the whole tail,
    so any write at all — a restart banner is enough — re-reported the oldest
    exceptions still in the window. It paged the owner about three errors that had
    been fixed hours earlier, which is how a watchdog teaches people to ignore it.

    A log is append-only: the honest question is "what is after where I stopped
    reading", and that is a byte offset, not a timestamp."""
    try:
        size = os.path.getsize(log_path)
    except OSError:
        return [], offset
    if size < offset:                   # rotated or truncated: start over
        offset = 0
    if size == offset:
        return [], offset
    try:
        with open(log_path, "r", errors="replace") as f:
            f.seek(offset)
            fresh = f.read()
    except OSError:
        return [], offset
    return re.findall(r"^(?:\w+Error|Exception|OSError)[^\n]*", fresh, re.M)[-3:], size


def notify(token, chat, text, dry=False):
    if dry:
        print(f"[watchdog] would alert {chat}: {text}")
        return True
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": str(chat), "text": text,
                         "parse_mode": "Markdown"}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r).get("ok", False)
    except Exception as e:
        print(f"[watchdog] could not send the alert: {e}", file=sys.stderr)
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    C = _cfg()
    owner = getattr(C, "OWNER_ID", 0)
    token = ""
    try:
        token = (HERE / "bot_token").read_text().strip()
    except OSError:
        pass
    if not (owner and token):
        raise SystemExit("watchdog needs bot_token and OWNER_ID to be able to speak")

    root = pathlib.Path(getattr(C, "WORKSPACE_ROOT", HERE.parent))
    db = root / "chatlog" / "chat.db"
    log = HERE / "logs" / "gateway.log"
    st = _state()
    first_run = "last_check" not in st
    if first_run:
        # A fresh install has a log full of history, and an alert about errors
        # from last week is the fastest way to teach someone to ignore this.
        st["last_check"] = time.time()
        try:
            st["log_offset"] = os.path.getsize(log)
        except OSError:
            st["log_offset"] = 0
        _save(st)
        print("[watchdog] first run — baseline set, watching from now")
        return 0

    problems = []
    quiet = unanswered(db, QUIET_MINUTES * 60, telegram_chats(C)) if db.exists() else None
    if quiet:
        problems.append(f"a message has gone unanswered for over {QUIET_MINUTES} "
                        f"minutes: _{quiet}_")
    errs, st["log_offset"] = tracebacks(str(log), int(st.get("log_offset", 0)))
    for err in errs:
        problems.append(f"`{err[:160]}`")

    st["last_check"] = time.time()
    if not problems:
        st.pop("alerted_at", None)
        _save(st)
        print("[watchdog] replying normally")
        return 0

    # One alert per incident, not one per run: a bot that is down stays down for a
    # while, and a watchdog that repeats itself every five minutes gets muted —
    # after which it is no longer a watchdog.
    last = st.get("alerted_at", 0)
    if time.time() - last < REALERT_MINUTES * 60:
        _save(st)
        print("[watchdog] problem persists, already alerted")
        return 1
    msg = ("🔕 *The bot is up but not answering.*\n\n" + "\n".join(f"• {p}" for p in problems)
           + "\n\nThe process is running, so nothing else will report this. "
             "Check `logs/gateway.log`.")
    if notify(token, owner, msg, a.dry_run):
        st["alerted_at"] = time.time()
    _save(st)
    return 1


if __name__ == "__main__":
    sys.exit(main())
