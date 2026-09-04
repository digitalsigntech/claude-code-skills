#!/usr/bin/env python3
"""Voice agent adapter — connect a Claude Code machine to the voice plane.

The plane POSTs protocol messages to this server; each one is answered from the
machine the agent actually runs on. A spoken question becomes a real Claude turn
in your project directory, so the answer comes out of your own files.

    python3 voice_agent.py            # serve (reads config.json beside this file)
    python3 voice_agent.py --check    # print health as the plane would see it

Protocol (POST /, JSON, `Authorization: Bearer <secret>`):

    {"v":1, "account":"…", "account_name":"…", "type":"capabilities"}
        -> {"capabilities": ["ask", "health", "progress", "branding", "file"]}
    {"v":1, …, "type":"health"}
        -> {"ok": true}                       agent up and signed in
        -> {"ok": false, "signed_out": true, "detail": "…"}
    {"v":1, …, "type":"ask", "question":"…"}
        -> {"answer": "…"}
        -> {"answer": "", "agent_error": "signed_out", "detail": "…"}

`branding` is the app's identity panel — the user's name, the company, the agent's
own name and logo. It is configuration, not code: an agent that does not set it gets
the app's generic assistant, which is why an install for a company must.

`progress` says what this agent is working on right now, and the app polls it to
decide whether an agent is there at all — refusing it draws the connect button
crossed out on a machine that is answering fine.

`health` must never cost a model turn — it is a file read, so a connection test
stays instant. Unknown types get HTTP 400, which the plane reads as "this agent
speaks ask only" rather than as a failure.

Sessions are per account: the first turn opens one, later turns resume it, so a
conversation over voice keeps its thread.
"""
import argparse, base64, calendar, hashlib, json, mimetypes, os, pathlib, re, secrets, \
    shutil, subprocess, sys, threading, time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
STATE = HERE / "state.json"

DEFAULTS = {
    "workdir": str(pathlib.Path.home()),
    "port": 8787,
    "bind": "127.0.0.1",
    "model": "",                 # empty = whatever `claude` defaults to
    "turn_timeout": 870,         # the plane gives up at 900
    "secret": "",                # bearer the plane must present; generated if absent
}
_lock = threading.Lock()


def load(path, default):
    try:
        return {**default, **json.loads(path.read_text())} if isinstance(default, dict) \
            else json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(default) if isinstance(default, dict) else default


def save(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def config():
    cfg = load(CONFIG, DEFAULTS)
    if not cfg.get("secret"):
        cfg["secret"] = secrets.token_urlsafe(32)
        save(CONFIG, cfg)
    return cfg


def claude_bin():
    """Absolute path to the CLI. A service manager does not inherit a login PATH."""
    cfg = load(CONFIG, DEFAULTS)
    if cfg.get("claude_bin"):
        return cfg["claude_bin"]
    found = shutil.which("claude")
    if found:
        return found
    for p in ("~/.local/bin/claude", "/usr/local/bin/claude", "/usr/bin/claude"):
        p = pathlib.Path(p).expanduser()
        if p.exists():
            return str(p)
    return ""


# ---------------------------------------------------------------- health
SIGNED_OUT = re.compile(
    r"(oauth (token|session) (expired|revoked)|please run .?/login|not logged in|"
    r"authentication[_ ]error|invalid api key|credit balance is too low)", re.I)


def health():
    """File reads only — a connection test must not cost a model turn."""
    exe = claude_bin()
    if not exe:
        return {"ok": False, "signed_out": True,
                "detail": "the claude CLI was not found on this machine"}

    cred = pathlib.Path.home() / ".claude" / ".credentials.json"
    if not cred.exists():
        # An API-key install is legitimate and has no credentials file.
        if os.environ.get("ANTHROPIC_API_KEY"):
            return {"ok": True}
        return {"ok": False, "signed_out": True,
                "detail": "no Claude credentials on this machine — run `claude` "
                          "in a terminal here and log in"}
    try:
        oauth = json.loads(cred.read_text()).get("claudeAiOauth", {})
    except (json.JSONDecodeError, OSError) as e:
        return {"ok": False, "signed_out": True, "detail": f"credentials unreadable: {e}"}

    # The access token refreshes itself; only the refresh token expiring is fatal.
    refresh_exp = int(oauth.get("refreshTokenExpiresAt") or 0) / 1000
    if refresh_exp and refresh_exp < time.time():
        return {"ok": False, "signed_out": True,
                "detail": "the Claude login on this machine has expired — run "
                          "`claude` in a terminal here and log in again"}
    return {"ok": True}


# ---------------------------------------------------------------- turns
# Turns running right now, by id. The app polls `progress` to decide whether the
# agent is reachable at all, so this is also what keeps the Connect button honest.
INFLIGHT = {}
_inflight_lock = threading.Lock()


def progress(account=None):
    """What this agent is working on, answered without a model.

    The app probes this to decide whether an agent is there — a 400 here is read
    as unreachable and the connect button is drawn crossed out, on a machine that
    is answering questions perfectly well. So an adapter that cannot report work
    must still report NO work rather than refuse the question.

    `covers_all_origins` is false and stays false: this sees turns that arrived
    through the app, and nothing about a cron job or a terminal session on the
    same machine. Claiming otherwise would have the voice say "nothing is
    running" while something is."""
    # THE QUESTION DOES NOT GO OUT IN THE CLEAR ON A SEALED ACCOUNT (#260).
    #
    # The app seals the question so the plane cannot read it, and then polls
    # this endpoint every few seconds while the turn runs — and this handed
    # back the first 120 characters of that same question, in plaintext,
    # through the same relay. Sealing the body and narrating its contents
    # beside it is not partial protection, it is theatre: the plane would have
    # learned every question anyway, a few seconds later, from us.
    #
    # Omitted rather than sealed, because the app already HAS the plaintext —
    # it wrote it. The id and the clock are what the progress card needs; the
    # words were only ever a convenience, and they are not ours to broadcast.
    hide = False
    try:
        hide = e2ee_locked(account) if account else False
    except Exception:
        hide = bool(account)          # unsure -> say less, never more
    with _inflight_lock:
        tasks = [{"id": tid,
                  **({"question_sealed": True} if hide
                     else {"question": t["question"][:120]}),
                  "state": "running",
                  "started": t["started"], "elapsed": round(time.time() - t["started"], 1),
                  "waiting": 0, "origin": "app"}
                 for tid, t in INFLIGHT.items()]
    return {"busy": bool(tasks),
            "tool": None,
            "elapsed": max([t["elapsed"] for t in tasks], default=0),
            "serialized": False,
            "covers_all_origins": False,
            "coverage_note": "I can only see what was asked through the app on this "
                             "machine — not anything else it may be running.",
            "tasks": tasks}


def _finish_turn(turn_id):
    """Drop a turn from the in-flight list. Every exit from ask() goes through
    here: a turn that fails and stays listed makes the app narrate work that
    stopped minutes ago, and `busy` never falls back to false."""
    with _inflight_lock:
        INFLIGHT.pop(turn_id, None)


# ---------------------------------------------------------------- history
HISTORY_TAIL_BYTES = 512 * 1024         # per session file, newest first
HISTORY_MAX_FILES = 6


def archive_dir():
    """A message archive on this machine, if it has one.

    Transcripts are what EVERY Claude Code machine has, so they are the floor.
    A machine that also archives its chats has something better: one timeline
    across every channel the agent talks on, so a conversation that happened in
    chat this morning is there when the phone asks about it tonight."""
    cfg = config()
    if cfg.get("archive_dir"):
        d = pathlib.Path(os.path.expanduser(cfg["archive_dir"]))
        return d if (d / "chatdb.py").exists() else None
    d = pathlib.Path(os.path.expanduser(cfg["workdir"])) / "chatlog"
    return d if (d / "chatdb.py").exists() else None


def _chatdb():
    d = archive_dir()
    if not d:
        return None
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))
    try:
        import chatdb
        return chatdb
    except ImportError:
        return None


# ---------------------------------------------------------------- guests ---
# One machine, more than one caller. The account this agent BELONGS to is its
# owner; anyone else — a demo unlock, a colleague trying it — is a guest, and a
# guest must not read the owner's conversation or write into the owner's chat.
#
# 2026-08-14, found while building the demo endpoint: a freshly minted demo
# account asked for its history and was handed the OWNER'S — their words and
# the agent's replies — because history is per MACHINE and nothing asked whose
# it was.
_WHO = threading.local()


def set_caller(account):
    _WHO.account = str(account or "") or None


def caller():
    return getattr(_WHO, "account", None)


def owner_accounts():
    """Every account that is THIS person. Usually one; more when the same human
    reaches the agent under a second id — a demo account they own, a second
    phone. Unset means single-user, which is what every existing install
    expects.

    2026-08-17: the demo account was a guest because guests must not read the
    owner's conversation. When the only user of the demo IS the owner, that
    protection costs a chat, a delivery tick and a persistent thread and buys
    nothing — so the demo is named here and treated as what it is.
    """
    cfg = config()
    named = cfg.get("owner_accounts")
    if isinstance(named, list):
        out = [str(a) for a in named if a]
    else:
        out = []
    one = str(cfg.get("owner_account") or "").strip()
    if one and one not in out:
        out.insert(0, one)
    return out


def owner_account():
    """The primary one, for anything that needs a single name."""
    a = owner_accounts()
    return a[0] if a else None


def is_guest():
    own = owner_accounts()
    who = caller()
    return bool(own and who and who not in own)


def guest_chat_id(account=None):
    """A private, stable chat id for a guest, well away from Telegram's range."""
    a = account or caller() or "guest"
    return -(int(hashlib.sha256(a.encode()).hexdigest()[:12], 16) % 10**9) - 10**12

# ---------------------------------------------------------------- telegram
# 2026-08-13, from the owner of an install that had both: "it has a telegram
# gateway. Messages must be synched to telegram, too."
#
# So this machine has the same two channels the box has, and the same rule
# follows: a voice conversation is not a separate conversation. What is said to
# the phone appears in the chat, what is answered appears in the chat, and a
# photo sent by voice arrives there as a photo. Otherwise the same person
# talking to the same agent has two half-records and neither is the truth.
#
# Best-effort in one direction only: a failed Telegram send must never fail a
# voice turn, but it MUST be visible — `posted` goes false, so the app draws no
# tick, and the archive row says the same thing.
_TG_CACHE = {}


def telegram():
    """The gateway's own send API, if this machine has one installed."""
    if "mod" in _TG_CACHE:
        return _TG_CACHE["mod"]
    _TG_CACHE["mod"] = None
    d = os.path.join(os.path.expanduser(config()["workdir"]), "telegram")
    if os.path.isfile(os.path.join(d, "tg_api.py")):
        if d not in sys.path:
            sys.path.insert(0, d)
        try:
            import tg_api
            _TG_CACHE["mod"] = tg_api
        except Exception:
            pass
    return _TG_CACHE["mod"]


def archive_chat_id():
    """Which chat this caller's lines belong to: the owner's Telegram chat, or
    the guest's own private one."""
    return guest_chat_id() if is_guest() else (telegram_chat() or 0)


def telegram_chat():
    """Which chat voice traffic mirrors into.

    `telegram_chat` in config wins. Failing that the gateway's own owner id,
    and failing THAT a single-entry allowlist — an install with exactly one
    permitted user has no ambiguity about whose chat this is. More than one and
    it stays unset rather than guessing, because guessing here posts somebody's
    conversation into somebody else's window.
    """
    if "chat" in _TG_CACHE:
        return _TG_CACHE["chat"]
    chat = 0
    cfg = config()
    try:
        chat = int(cfg.get("telegram_chat") or 0)
    except (TypeError, ValueError):
        chat = 0
    d = os.path.join(os.path.expanduser(cfg["workdir"]), "telegram")
    if not chat and os.path.isdir(d):
        if d not in sys.path:
            sys.path.insert(0, d)
        try:
            import tgconf
            chat = int(getattr(tgconf, "OWNER_ID", 0) or 0)
        except Exception:
            chat = 0
        if not chat:
            try:
                ids = json.loads(open(os.path.join(d, "allowlist.json")).read())
                if isinstance(ids, list) and len(ids) == 1:
                    chat = int(ids[0])
            except Exception:
                chat = 0
    _TG_CACHE["chat"] = chat
    return chat


def _chat_title():
    if is_guest():
        return caller() or "Guest"
    """The archive's name for that chat. Derived, never hardcoded: an upstream
    install spent a week filing one person's words under another's name, because
    a name was written into the code back when only one person used it."""
    if not telegram_chat():
        return "Voice"
    return branding().get("user_name") or "Chat"


def tg_text(text, who=None):
    """Mirror one spoken line. `who` names the speaker; the agent's own words go
    in unlabelled, exactly as they do when it answers in the chat itself."""
    api, chat = telegram(), telegram_chat()
    if not api or not chat or not text:
        return False
    body = f"🎙 {who}: {text}" if who else text
    try:
        res = api.send_message(chat, body[:3900])
        return bool(res and res.get("ok"))
    except Exception:
        return False


def tg_file(path, caption=None):
    """Mirror one upload. Photos go as photos so they render in the chat;
    anything else as a document, which is what the box does and for the same
    reason — a PDF sent as a photo is a PDF nobody can open."""
    api, chat = telegram(), telegram_chat()
    if not api or not chat:
        return False
    method = "sendPhoto" if _att_kind(path) == "photo" else "sendDocument"
    field = "photo" if method == "sendPhoto" else "document"
    try:
        with open(path, "rb") as fh:
            res = api._call(method, _files={field: fh}, _timeout=90,
                            chat_id=chat, caption=(caption or "")[:1000] or None)
        return bool(res and res.get("ok"))
    except Exception:
        return False



# How long the app is asked to wait for a mirror before it is told the send is
# still in flight. Long enough that the normal case answers plainly, short
# enough that a stuck send never holds a checkmark hostage.
#
# 2026-08-18: this constant was added by a replace whose anchor no longer
# matched, so it silently did not land — and my own probe set it by hand,
# which is why the test passed and the server raised NameError on the first
# real call. A test that supplies the missing thing proves nothing.
MIRROR_DEADLINE_S = 2.5


def person_name(fallback=""):
    """WHOSE line this is — the human, not the account.

    2026-08-17: a mirrored line arrived in an owner's own chat prefixed with
    his COMPANY's name instead of his own. The app sends no name at all, so the
    prefix was composed here from the account name — and an account is named
    after a company. Branding already knows the person; that is what
    attribution means.

    Company at most alongside, never instead: a chat is between people.
    """
    try:
        who = str(branding().get("user_name") or "").strip()
    except Exception:
        who = ""
    return who or str(fallback or "").strip() or "you"


def _mirror_state_db():
    d = archive_dir()
    if not d:
        return None
    import sqlite3
    cx = sqlite3.connect(f"{d / 'chat.db'}", timeout=5)
    cx.execute("CREATE TABLE IF NOT EXISTS mirror_state("
               "epoch REAL, chat_id INTEGER, mirrored INTEGER, "
               "PRIMARY KEY(epoch, chat_id))")
    return cx


def _record_mirror(chat_id, mirrored):
    """Remember whether the line just archived actually reached the chat.

    2026-08-19: the app ticks whatever history hands back, and history could
    not say which lines were only WRITTEN DOWN. Two lines he could see ticked
    were never in Telegram at all. A sidecar table rather than a column,
    because the gateway writes this database too and a schema it does not know
    about is a schema it cannot break.
    """
    try:
        cx = _mirror_state_db()
        if not cx:
            return
        row = cx.execute("SELECT epoch FROM messages WHERE chat_id=? "
                         "ORDER BY epoch DESC LIMIT 1", (chat_id,)).fetchone()
        if row:
            cx.execute("INSERT OR REPLACE INTO mirror_state VALUES(?,?,?)",
                       (row[0], chat_id, 1 if mirrored else 0))
            cx.commit()
        cx.close()
    except Exception:
        pass

def archive(text, direction, sender, account_name="", mirror=True):
    """Record a voice turn in the machine's archive, and mirror it to Telegram.

    The archive write is best-effort by design — a voice turn must never fail
    because writing it down did. The row is filed under the TELEGRAM chat when
    there is one, not under a separate "Voice" pseudo-chat: one person talking
    to one agent should read back as one conversation, whichever way the words
    arrived."""
    chat = telegram_chat()
    db = _chatdb()
    if db and text:
        # CHECK AND WRITE UNDER ONE LOCK. 2026-08-19: a typed message arrives on
        # two paths at once — the line is logged and the same words are asked —
        # and both threads checked for a duplicate before either had written.
        # The rows were 180 MICROSECONDS apart, so the check was correct and
        # simply too early. A dedupe that is not atomic is a dedupe that works
        # in testing and fails on the one case it exists for.
        with _lock:
            if direction == "in" and _already_archived(text, within=15,
                                                       direction="in"):
                print(f"[voice-agent] duplicate line not archived: "
                      f"{str(text)[:40]!r}", file=sys.stderr)
                return "duplicate"
            try:
                db.record(text, direction,            # no cap (#275)
                          sender=sender, chat_id=archive_chat_id(),
                          chat_title=_chat_title(), kind="text")
            except Exception:
                pass
    # A guest's words go nowhere near the owner's Telegram.
    #
    # AND THE CALLER IS TOLD WHICH HAPPENED. 2026-08-14: this returned nothing,
    # so `log` answered `mirrored: true` for every line — including a demo
    # account's, which has no chat at all — and the app drew a delivery tick
    # for a delivery that could not happen. A tick has to mean something.
    if not text:
        return "empty"
    if not mirror:
        _record_mirror(archive_chat_id(), False)
        return "archived_only"
    if is_guest():
        _record_mirror(archive_chat_id(), False)
        return "guest_no_chat"
    if not telegram_chat():
        _record_mirror(archive_chat_id(), False)
        return "no_chat"
    # A SLOW MIRROR SHOULD NOT BE A SLOW TICK. The app waits on this call to
    # decide the checkmark, so a Telegram send that takes ten seconds — a
    # retry, a rate limit — held the answer for ten seconds and then drew a
    # tick nobody was watching for any more. The send runs on its own thread
    # and gets a short deadline: normally it finishes well inside it and the
    # answer is a plain yes, and when it does not, the honest answer is
    # "queued" rather than a boolean pretending to know (2026-08-18).
    box = {}

    def _send():
        box["ok"] = tg_text(text, who=(sender if direction == "in" else None))

    t = threading.Thread(target=_send, daemon=True)
    t.start()
    t.join(MIRROR_DEADLINE_S)
    if "ok" not in box:
        # Still in flight: not yet a delivery, and the echo will settle it.
        _record_mirror(archive_chat_id(), False)
        return "queued"
    _record_mirror(archive_chat_id(), bool(box["ok"]))
    return True if box["ok"] else "send_failed"


# ------------------------------------------------------------- attachments
# 2026-08-13: a user sent three photos with the caption "Remind me at 5:30 p.m.
# to analyze this sample." All three failed — this adapter answered HTTP 400 to
# `photo`, because it had no idea what one was — and the app showed "internal
# error" with delivery ticks beside them. The ask that followed: an agent must
# be as capable as the one it was extracted from, and it must store attachments
# to the messages.
#
# WHAT "DELIVERED" MEANS, which depends on what this machine has. With a chat
# gateway installed the answer is that chat: `posted` is true once the gateway
# accepts the picture, exactly as upstream. WITHOUT one, the app's own window is
# the only place the user can look, so a photo is delivered when it is (a)
# stored, (b) written into the archive as a message, and (c) fetchable by token
# so history renders it. All three, or `posted` is false — a tick that means
# less than that is a tick that will eventually lie, and did.
UPLOAD_DIR = "voice-uploads"            # under workdir; created on first upload
# Push banners are DERIVED copies made by the reminder firing loop, and they
# live outside the uploads tree. Tokens for them must still resolve here or
# the notification extension fetches a 404 and draws no picture.
BANNER_DIR = os.environ.get("REMINDER_BANNER_DIR", "/tmp/reminder-banners")
UPLOAD_MAX = 36 * 1024 * 1024           # matches the plane's own ceiling
_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/heic": ".heic",
        "image/webp": ".webp", "application/pdf": ".pdf"}


def upload_dir():
    d = os.path.join(os.path.expanduser(config()["workdir"]), UPLOAD_DIR,
                     time.strftime("%Y-%m-%d"))
    os.makedirs(d, exist_ok=True)
    return d


def save_upload(blob, content_type="image/jpeg", stem="photo"):
    """Bytes to a file nobody else will collide with, and its stable token."""
    ext = _EXT.get((content_type or "").split(";")[0].strip().lower())
    if not ext:
        ext = mimetypes.guess_extension(content_type or "") or ".bin"
    name = f"{stem}-{time.strftime('%H%M%S')}-{secrets.token_hex(3)}{ext}"
    path = os.path.join(upload_dir(), name)
    with open(path, "wb") as f:
        f.write(blob)
    return path, media_token(path)


def _att_kind(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif"):
        return "photo"
    if ext == ".pdf":
        return "pdf"
    return "file"


# The marker the archive stores, and the one thing that has to agree between
# the writer and the reader. Same convention the box uses, so a row means the
# same in both archives.
_MARKER = re.compile(r"^\[(camera photo|camera photos|file): ([^\]]+)\]\s*(.*)$",
                     re.S)


def archive_file(paths, caption, sender):
    """One archive row for an upload, carrying the filenames in its text.

    The schema has no attachment column and inventing one would fork the
    archive away from the box's. The marker convention costs nothing and
    survives anything that reads the table as plain messages."""
    db = _chatdb()
    if not db or not paths:
        return False
    names = ",".join(os.path.basename(p) for p in paths)
    tag = "camera photos" if len(paths) > 1 else "camera photo"
    text = f"[{tag}: {names}]" + (f" {caption}" if caption else "")
    # The chat gets the PICTURES, not the marker: a line reading
    # "[camera photo: photo-210115.png]" in somebody's Telegram is a filename
    # where a photograph should be. The marker is the archive's business.
    sent = True
    if telegram_chat() and not is_guest():
        for i, p in enumerate(paths):
            sent = tg_file(p, caption if i == 0 else None) and sent
    try:
        db.record(text + ("" if sent else " [NOT delivered to the chat]"),
                  "in", sender=sender or "you", chat_id=archive_chat_id(),
                  chat_title=_chat_title(), kind="photo")
    except Exception:
        return False
    # Delivered means it is somewhere the user can actually see it. With a
    # Telegram gateway installed that is Telegram, and a picture only this
    # machine can see is not a delivered picture.
    return sent


def _resolve_upload(name):
    """A filename from an archive row back to a path on disk.

    Newest day first: the same picture is never stored twice, and a scan that
    walks every day for every history poll would grow into the poll budget."""
    root = os.path.join(os.path.expanduser(config()["workdir"]), UPLOAD_DIR)
    if not os.path.isdir(root):
        return None
    for day in sorted(os.listdir(root), reverse=True):
        p = os.path.join(root, day, name)
        if os.path.exists(p):
            return p
    return None


def list_attachments(since=0.0, limit=30):
    """Files this agent holds, newest last — the feed the app reads back.

    Only files that made it into the archive appear here. An upload that failed
    to record is not listed, because the app treats presence in this feed as
    presence in the conversation, and it is right to.
    """
    d = archive_dir()
    if not d:
        return []
    try:
        import sqlite3
        cx = sqlite3.connect(f"file:{d / 'chat.db'}?mode=ro", uri=True, timeout=3)
        if is_guest():
            rows = cx.execute(
                "SELECT epoch, text FROM messages WHERE epoch > ? AND "
                "chat_id = ? AND (text LIKE '[camera photo%' OR "
                "text LIKE '[file: %') ORDER BY epoch DESC LIMIT ?",
                (since, guest_chat_id(), limit)).fetchall()
        else:
            rows = cx.execute(
                "SELECT epoch, text FROM messages WHERE epoch > ? AND "
                "(text LIKE '[camera photo%' OR text LIKE '[file: %') "
                "ORDER BY epoch DESC LIMIT ?", (since, limit)).fetchall()
        cx.close()
    except Exception:
        return []
    items = []
    for ep, text in reversed(rows):
        m = _MARKER.match(str(text).strip())
        if not m:
            continue
        caption = m.group(3).strip()
        for nm in m.group(2).split(","):
            path = _resolve_upload(nm.strip())
            if not path:
                continue                # deleted since: not an attachment now
            items.append({"token": media_token(path), "ts": float(ep),
                          "kind": _att_kind(path),
                          "filename": os.path.basename(path),
                          "caption": caption})
    return items


def _norm(t):
    """For comparison only: case and punctuation removed.

    2026-08-14: "Visual sign." and "visual sign?" are one thing somebody said,
    recorded by two writers a second apart — the app posts the transcript and
    the relayed question is archived when it arrives. Comparing the raw strings
    called them different sentences, so both went into the chat and the agent
    answered a fragment twice.
    """
    return re.sub(r"[^\w\s]", "", (t or "").lower()).strip()


def _already_archived(text, within=180, direction=None):
    """Has this line just been written? Used only to stop a double entry.

    Compared on the first 200 characters with case and punctuation removed: the
    voice model's `log` call and the archived `ask` are the same sentence, but
    one of them can arrive truncated, re-punctuated, or rewritten in passing by
    the speech pipeline.

    `direction` narrows it to one side of the conversation, and a SHORT window
    goes with it: somebody saying "yes" twice a minute apart means it twice.
    """
    d = archive_dir()
    if not d or not text:
        return False
    try:
        import sqlite3
        cx = sqlite3.connect(f"file:{d / 'chat.db'}?mode=ro", uri=True, timeout=3)
        if direction:
            rows = cx.execute("SELECT text FROM messages WHERE epoch > ? AND "
                              "direction = ? ORDER BY epoch DESC LIMIT 4",
                              (time.time() - within, direction)).fetchall()
        else:
            rows = cx.execute("SELECT text FROM messages WHERE epoch > ? "
                              "ORDER BY epoch DESC LIMIT 4",
                              (time.time() - within,)).fetchall()
        cx.close()
    except Exception:
        return False
    head = _norm(text)[:200]
    return bool(head) and any(_norm(r[0])[:200] == head for r in rows)


def _archive_history(limit, since):
    """The tail of the archive, newest-last, across every chat it holds."""
    db = _chatdb()
    if not db:
        return None
    d = archive_dir()
    try:
        import sqlite3
        cx = sqlite3.connect(f"file:{d / 'chat.db'}?mode=ro", uri=True, timeout=3)
        if is_guest():
            rows = cx.execute(
                "SELECT epoch, sender, text, direction FROM messages "
                "WHERE epoch > ? AND chat_id = ? ORDER BY epoch DESC LIMIT ?",
                (since, guest_chat_id(), limit)).fetchall()
        else:
            rows = cx.execute(
                "SELECT epoch, sender, text, direction FROM messages "
                "WHERE epoch > ? ORDER BY epoch DESC LIMIT ?",
                (since, limit)).fetchall()
        cx.close()
    except Exception:
        return None
    name = branding().get("bot_name") or "agent"
    # What we KNOW about delivery, per row. Absent means unknown — an older
    # line from before this was recorded — and the app keeps its own behaviour
    # there rather than being told a guess.
    state = {}
    try:
        import sqlite3
        cx2 = sqlite3.connect(f"file:{d / 'chat.db'}?mode=ro", uri=True,
                              timeout=3)
        state = {float(e): bool(m) for e, m in cx2.execute(
            "SELECT epoch, mirrored FROM mirror_state")}
        cx2.close()
    except Exception:
        state = {}
    msgs = []
    for ep, sender, text, direction in reversed(rows):
        if not isinstance(ep, (int, float)) or ep <= 0 or not text:
            continue
        # Direction is the authoritative field: keying on the sender's name puts
        # the agent's own words in the user's bubble the first time anything
        # else writes to the archive.
        role = "agent" if direction == "out" else "user"
        m = {"role": role,
             "sender": name if role == "agent" else (sender or "you"),
             # NO CAP (#275): this 2000 cut his answer mid-word at exactly
             # 2000 characters — "…over a channel the voi" — while the
             # archive held all 2293. A message the agent wrote in full and
             # the app shows in part is worse than a failure: it reads as
             # the whole answer. The owner's rule: no caps on message
             # content anywhere in the sealed path.
             "text": _strip_injected_prefix(str(text)),
             "ts": float(ep)}
        if float(ep) in state:
            m["mirrored"] = state[float(ep)]
        # A row that names files carries their tokens, so a restored chat shows
        # the picture instead of the words "[camera photo: …]". Same fields the
        # box sends: `token` flat for one file, `tokens` for an album.
        mk = _MARKER.match(str(text).strip())
        if mk:
            paths = [p for p in (_resolve_upload(n.strip())
                                 for n in mk.group(2).split(",")) if p]
            if paths:
                toks = [media_token(p) for p in paths]
                m.update(token=toks[0], tokens=toks,
                         kind=_att_kind(paths[0]),
                         filename=os.path.basename(paths[0]))
        msgs.append(m)
    return msgs


def _tail_lines(path, max_bytes=HISTORY_TAIL_BYTES):
    """Last lines of a JSONL file without reading the whole thing.

    A working session log runs to megabytes and the app polls history every few
    seconds; reading from the front would spend the poll budget on the parts of
    the conversation nobody is asking for."""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()            # discard the partial line we landed in
            return f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []


def _entry_text(entry):
    """The human-visible words of a transcript entry, or "" if it has none.

    Tool calls, tool results and thinking are the agent working, not the agent
    talking. Replaying them into a phone's chat would bury the conversation in
    machinery the user never saw the first time."""
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                          if isinstance(b, dict) and b.get("type") == "text").strip()
    return ""


def _strip_injected_prefix(text):
    """Drop a leading bracketed context line from a user message.

    Gateways prepend one — who is speaking, which chat, which channel — and this
    adapter adds its own "[Voice turn from …]". None of it is anything the user
    typed, and replaying it into their phone shows them machinery they never saw,
    chat ids included. The convention is a single bracketed line followed by the
    real message, so that is exactly what comes off: no bracket, no newline after
    it, nothing removed."""
    first, sep, rest = text.partition("\n")
    first = first.strip()
    if sep and first.startswith("[") and first.endswith("]") and rest.strip():
        return rest.strip()
    return text


def history(limit=50, since=0.0):
    """The conversation with this agent, from its own session transcripts.

    Two sources, in order. A machine with a message archive (the chat-archive
    component) has one timeline across every channel its agent talks on, voice
    turns included — that is what the box this was extracted from served, and it
    is the better answer whenever it exists.

    Failing that, Claude Code writes every session to ~/.claude/projects/<project>/
    and voice turns are resumed sessions in that same project, so the app's own
    conversation is in there too. Either way it is the real thread, not a copy
    kept in parallel."""
    archived = _archive_history(limit, since)
    if archived is not None:
        return archived

    cfg = config()
    root = pathlib.Path.home() / ".claude" / "projects" / _project_slug(
        os.path.expanduser(cfg["workdir"]))
    if not root.is_dir():
        return []
    name = (branding().get("bot_name") or "agent")
    msgs = []
    files = sorted(root.glob("*.jsonl"), key=lambda p: p.stat().st_mtime,
                   reverse=True)[:HISTORY_MAX_FILES]
    for f in files:
        for line in _tail_lines(f):
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("type") not in ("user", "assistant") or e.get("isSidechain"):
                continue                # sidechains are subagents talking to themselves
            if e.get("isMeta") or e.get("isCompactSummary"):
                continue
            text = _entry_text(e)
            if not text or "identity panel of a voice app" in text:
                continue                # our own derivation turn is not conversation
            try:
                # Transcript stamps are UTC. timegm, not mktime-minus-timezone:
                # that hack is wrong by an hour under summer time, which is
                # enough to make `since` skip the messages it was asked for.
                ts = float(calendar.timegm(
                    time.strptime(e["timestamp"][:19], "%Y-%m-%dT%H:%M:%S")))
            except (KeyError, ValueError, TypeError):
                continue                # no usable clock: sorting it to 1970 is worse
            if ts <= since:
                continue
            role = "user" if e["type"] == "user" else "agent"
            if role == "user":
                text = _strip_injected_prefix(text)
            msgs.append({"role": role, "sender": "you" if role == "user" else name,
                         "text": text, "ts": ts})       # no cap (#275)
        if len(msgs) >= limit * 3:
            break
    msgs.sort(key=lambda m: m["ts"])
    return msgs[-limit:]


# ---------------------------------------------------------------- turns
def session_id(account):
    st = load(STATE, {})
    return st.get("sessions", {}).get(account)


def remember_session(account, sid):
    with _lock:
        st = load(STATE, {})
        st.setdefault("sessions", {})[account] = sid
        save(STATE, st)



# "that picture" means the NEWEST one, not the last one discussed.
#
# 2026-08-19: a photo arrived at 00:57:35, he typed "ignore that picture" at
# 00:57:53, and the agent confidently disregarded an app icon from four hours
# earlier — the last image it had actually LOOKED at, rather than the last one
# received. It was not a race: the photo was in the archive eighteen seconds
# before the question. The model simply reached for the image it knew.
#
# A demonstrative has one honest referent — the most recent image — and if that
# image has not been examined yet, the answer is to open it or to ask, never to
# name a different one with confidence.
# One bracketed machine marker and nothing else: `[manual-miss q="…" v=abc]`.
# The whole message must be the marker — a sentence that happens to contain a
# bracket is a sentence, and belongs in the chat like any other.
_MARKER_ONLY = re.compile(r"\A\[[a-z][a-z0-9._-]{1,40}(?:\s[^\]]*)?\]\Z", re.S)


_THAT_PICTURE = re.compile(
    r"\b(that|this|the|it|last|latest|previous)\b[^.?!]{0,20}"
    r"\b(picture|photo|photograph|image|screenshot|scan|shot)\b|"
    r"\b(picture|photo|photograph|image|screenshot)\b\s*$", re.I)
_IMAGE_WINDOW_S = 6 * 3600


def newest_image(within=_IMAGE_WINDOW_S):
    """(path, when) of the most recent image in this chat, or None."""
    d = archive_dir()
    if not d:
        return None
    try:
        import sqlite3
        cx = sqlite3.connect(f"file:{d / 'chat.db'}?mode=ro", uri=True, timeout=3)
        rows = cx.execute(
            "SELECT epoch, text FROM messages WHERE epoch > ? AND "
            "text LIKE '[camera photo%' ORDER BY epoch DESC LIMIT 1",
            (time.time() - within,)).fetchall()
        cx.close()
    except Exception:
        return None
    for ep, text in rows:
        m = _MARKER.match(str(text).strip())
        if not m:
            continue
        for nm in m.group(2).split(","):
            p = _resolve_upload(nm.strip())
            if p:
                return p, ep
    return None


def picture_context(question):
    """A line naming the picture a demonstrative refers to, or ''."""
    if not question or not _THAT_PICTURE.search(question):
        return ""
    hit = newest_image()
    if not hit:
        return ("The user refers to a picture, and there is no recent "
                "image in this chat. Ask which one rather than guessing.")
    path, ep = hit
    when = time.strftime("%H:%M", time.localtime(ep))
    return (f"\"That picture\" is the MOST RECENT image in this chat: "
            f"{path}, received at {when}. It is the one meant — not any image "
            f"discussed earlier. Open it if you need to see it.")


# An agent cannot reach the app's switches (2026-08-19, an owner's rule: "if a
# user asks to change some setting in the app via TEXT INPUT, the agent must
# respond that those are VOICE COMMANDS ONLY").
#
# The manual states the reason; this states the reflex. A manual section only
# helps if the agent goes looking, and the phrasings people use for this are
# endless — so the turn carries the rule whenever the question smells like one,
# and the agent answers in its own words rather than from a script.
_SETTING_VERB = re.compile(
    r"\b(change|switch|set|turn|make|enable|disable|use|put|activate|"
    r"increase|decrease|raise|lower|mute|unmute)\b", re.I)
_SETTING_NOUN = re.compile(
    r"\b(dark|light) mode\b|\bappearance\b|\bfont\b|\btext size\b|"
    r"\bbigger text\b|\bvoice\b|\blanguage\b|\bquality\b|\bhq\b|"
    r"\bstandard\b|\bnotification\w*\b|\bkeyboard\b|\bauto[- ]?connect\b|"
    r"\bconnect automatically\b|\breplies\b|\bverbosity\b|\bshorter\b|"
    r"\blonger\b|\bbubbles?\b|\btranspar\w+\b|\bsetting\w*\b", re.I)



def time_context(tz):
    """The caller's clock, stated for the turn.

    2026-08-19: he said "4pm" and got 5:00 PM, in the table and spoken aloud.
    The reminder was CREATED in the machine's timezone and DISPLAYED in the
    caller's — two different clocks, one hour apart, each correct on its own
    terms. A time somebody says belongs to the zone they are standing in, and
    the app tells us which that is on every ask.
    """
    if not tz:
        return ""
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime
        now = datetime.now(ZoneInfo(str(tz)))
    except Exception:
        return ""
    return (f"The person you are answering is in {tz}, where it is now "
            f"{now.strftime('%H:%M on %a %d %b')}. Any time they name — "
            f"\"4pm\", \"tomorrow morning\" — is in THAT zone, and anything "
            f"you schedule or read back must be too. This machine's own clock "
            f"is not theirs.")

# THE MANUAL RIDES INTO THE TURN (2026-08-20, #252).
#
# Asked about the app's privacy and security, an agent that had the manual on
# disk — synced 20 minutes earlier, the sync logged, the file 27KB and correct —
# answered that it "isn't in the company files" and that it has "no visibility
# into the app's architecture". Both halves false, and the second is the kind of
# sentence that ends a conversation: it does not invite the user to ask again.
#
# The lesson is the one this project keeps relearning in new costumes: a
# document the model COULD open is not a document the model WILL open. A
# pointer in a prompt is a step to forget, and the busiest turn is the one that
# forgets it. So the relevant sections travel with the question, already read.
#
# Sections, not the whole file: 27KB of manual in front of every app question
# buries the answer it contains, and the excerpt keeps the door open for the
# model to say which section it is quoting.
_APP_QUESTION = re.compile(
    r"\b(this|the|your)\s+app\b|\bapp\'?s\b|\bin the app\b|"
    r"\bagent voice mode\b|\bvoice mode\b|\btestflight\b|\bthis build\b|"
    r"\bend[- ]to[- ]end\b|\be2ee\b|\bencrypt\w*\b|\bfingerprint\b|"
    r"\bsafety number\b|\bpairing\b|\bqr code\b|"
    r"\bprivacy\b|\bsecurity\b|\bsecure\b|\brecord(ed|ing)\b|"
    r"\bretention\b|\bretained\b|\bwho can (read|see|hear)\b|"
    r"\btranscript\w*\b|\bdark mode\b|\bbubbles?\b|\bverbosity\b|"
    r"\bnotification\w*\b|\bminutes? (left|remaining)\b|\bmy balance\b",
    re.I)

_DOC_WORD = re.compile(r"[a-z][a-z0-9'-]{3,}", re.I)


def _manual_sections(text):
    """(heading, body) pairs, split on markdown headings 1-3 deep."""
    out, head, cur = [], "", []
    for line in text.splitlines():
        if re.match(r"^#{1,3} ", line):
            if cur:
                out.append((head, "\n".join(cur)))
            head, cur = line.lstrip("# ").strip(), [line]
        else:
            cur.append(line)
    if cur:
        out.append((head, "\n".join(cur)))
    return out


def app_doc_context(question, budget=4000):
    """The manual sections that answer THIS question, or ''."""
    q = question or ""
    if not _APP_QUESTION.search(q):
        return ""
    try:
        d = _docs_dir()
        with open(os.path.join(d, "manual.md"), encoding="utf-8") as f:
            manual = f.read()
        try:
            with open(os.path.join(d, ".manual-version"), encoding="utf-8") as f:
                ver = f.read().strip()[:32]
        except OSError:
            ver = "unknown"
    except OSError:
        return ""
    if not manual.strip():
        return ""
    words = {w.lower() for w in _DOC_WORD.findall(q)}
    if not words:
        return ""
    scored = []
    for head, body in _manual_sections(manual):
        low = body.lower()
        hits = sum(1 for w in words if w in low)
        hits += 3 * sum(1 for w in words if w in head.lower())
        if hits:
            scored.append((hits, len(body), head, body))
    if not scored:
        return ""
    scored.sort(key=lambda r: (-r[0], r[1]))
    picked, used = [], 0
    for _hits, _n, _head, body in scored:
        chunk = body.strip()
        if used + len(chunk) > budget:
            chunk = chunk[:max(0, budget - used)]
        if not chunk:
            break
        picked.append(chunk)
        used += len(chunk)
        if used >= budget:
            break
    return ("The user is asking about the voice app itself. The app ships a "
            "manual and it is on this machine, current as of version "
            f"{ver}; these are the parts that bear on the question:\n\n"
            + "\n\n---\n\n".join(picked)
            + "\n\nAnswer from that text. It is authoritative and it is "
              "today's — where it speaks, it outranks anything you remember "
              "about the app. NEVER say the app is undocumented, that its "
              "workings are not in your files, or that you have no visibility "
              "into it: the document is quoted above. If these sections do "
              "not cover the specific thing asked, say what they DO establish "
              "and offer to look further — that is a gap in the manual worth "
              "reporting, not a limit of yours.")


def app_setting_context(question):
    """A line telling the agent it cannot change an app setting, or ''."""
    q = question or ""
    if not (_SETTING_VERB.search(q) and _SETTING_NOUN.search(q)):
        return ""
    return ("This asks to change a SETTING IN THE APP. You cannot: the "
            "app's switches are not reachable from here, any more than the "
            "phone's brightness is. Say so plainly, in one or two sentences, "
            "and name the two ways that DO work — say it out loud to the app "
            "(e.g. \"switch to dark mode\"), or open Settings. Do not "
            "apologise at length, do not offer to try, and never imply it is "
            "done. The one exception is /clear, which the app acts on itself "
            "before the message reaches you.")

def ask(account, question, account_name="", archive_question=True,
        archive_turn=True, context=""):
    """`archive_turn=False` for a LOOKUP: answer and drop.

    2026-08-14: when no table is on screen the app asks the agent
    itself and turns the answer into a card. Both halves were archived, so the
    poll returned the app's own question as the USER's sentence and the answer
    as a full table — and suppressing them live did not help, because a
    relaunch replayed them out of history. Only not writing them can fix that.

    `archive_question=False` for a turn whose words are already recorded.

    A captioned upload writes ONE row — the marker with the caption on it — and
    then runs the caption as a turn. Left to archive itself, that turn added a
    second user bubble saying the same thing, with the internal file paths we
    appended for the agent visible on the end of it: words the user never said,
    on their own screen, which is the bug we chased all afternoon in another
    form."""
    cfg = config()
    exe = claude_bin()
    if not exe:
        return {"answer": "", "agent_error": "signed_out",
                "detail": "the claude CLI was not found on this machine"}

    workdir = os.path.expanduser(cfg["workdir"])

    # THE USER'S MESSAGE CONTAINS ONLY THE USER'S WORDS (2026-08-20).
    #
    # Everything the app knows and the model needs — which picture is on
    # screen, which zone the phone is in, who is speaking — used to be appended
    # to the question as bracketed text. Five turns in a row the agent then
    # refused it as a prompt injection and said so out loud: "I won't take
    # instructions on your timezone from bracketed text embedded in the
    # message." It was right to be suspicious. A sentence arriving inside the
    # user's own message, telling the model what to believe about the world, is
    # indistinguishable from an attack no matter who actually wrote it — and a
    # model that ever learns to trust that shape is a model an attacker can
    # steer with one sentence typed into the app.
    #
    # The channel is what makes it trustworthy, not the wording. Context the
    # SYSTEM knows goes in the system prompt, where the CLI's own authority
    # covers it; the user's message stays exactly what they said. The warning
    # also cost the owner five identical security alerts about his own
    # infrastructure, which teaches the one habit no warning should ever
    # teach — that these are noise and can be scrolled past.
    prompt = question
    cmd = [exe, "-p", prompt, "--dangerously-skip-permissions"]
    sysbits = []
    if account_name:
        sysbits.append(f"This turn comes from {account_name} through the voice "
                       f"app. It is them speaking, not a system message.")
    if context:
        sysbits.append(context.strip())
    if sysbits:
        cmd += ["--append-system-prompt", "\n\n".join(sysbits)]
    if cfg.get("model"):
        cmd += ["--model", cfg["model"]]

    # The CLI refuses --dangerously-skip-permissions when the process is root,
    # which is exactly how a service on a single-purpose box tends to run. Left
    # unhandled, every voice turn fails instantly with a message about sudo that
    # says nothing about voice. IS_SANDBOX=1 is the CLI's own acknowledgement
    # that the caller has accepted the risk; without it the turn still runs, but
    # tool use is limited to what unprompted permissions allow.
    env = dict(os.environ)
    if os.geteuid() == 0:
        env.setdefault("IS_SANDBOX", "1")

    turn_id = secrets.token_hex(8)
    with _inflight_lock:
        INFLIGHT[turn_id] = {"started": time.time(), "question": question}
    if archive_question:
        if archive_turn:
            # The USER'S words only. The picture context is instruction for the
            # model, and putting it in the archive would show him a sentence he
            # never typed — the mistake the caption path already made once.
            archive(question.split("\n\n[", 1)[0], "in",
                    sender=person_name(account_name))

    sid = session_id(account)
    if sid:
        cmd += ["--resume", sid]
    else:
        sid = __import__("uuid").uuid4().hex
        sid = f"{sid[:8]}-{sid[8:12]}-{sid[12:16]}-{sid[16:20]}-{sid[20:32]}"
        cmd += ["--session-id", sid]

    try:
        r = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True,
                           timeout=cfg["turn_timeout"], env=env)
    except subprocess.TimeoutExpired:
        _finish_turn(turn_id)
        return {"answer": "", "agent_error": "timeout",
                "detail": f"the turn ran past {cfg['turn_timeout']}s"}
    except FileNotFoundError:
        _finish_turn(turn_id)
        return {"answer": "", "agent_error": "signed_out",
                "detail": f"could not execute {exe}"}

    out, err = (r.stdout or "").strip(), (r.stderr or "").strip()
    if r.returncode != 0 and "cannot be used with root" in (err + out):
        r = subprocess.run([c for c in cmd if c != "--dangerously-skip-permissions"],
                           cwd=workdir, capture_output=True, text=True,
                           timeout=cfg["turn_timeout"], env=env)
        out, err = (r.stdout or "").strip(), (r.stderr or "").strip()
    if r.returncode != 0:
        _finish_turn(turn_id)
        blob = f"{err}\n{out}"
        if SIGNED_OUT.search(blob):
            return {"answer": "", "agent_error": "signed_out", "detail": err[:400]}
        return {"answer": "", "agent_error": "failed", "detail": (err or out)[:400]}

    # A resumed session that the CLI has forgotten: retry once, fresh.
    if not out and sid and "No conversation found" in err:
        with _lock:
            st = load(STATE, {})
            st.get("sessions", {}).pop(account, None)
            save(STATE, st)
        _finish_turn(turn_id)
        # The retry is the same turn: it must carry the same context, or the
        # second attempt answers with less than the first one knew.
        return ask(account, question, account_name, archive_question=False,
                   archive_turn=archive_turn, context=context)

    remember_session(account, sid)
    _finish_turn(turn_id)
    if archive_turn:
        archive(out, "out", sender=branding().get("bot_name") or "agent")
    # Whole answers, always (#275). A truncated reply is indistinguishable
    # from a short one at every point downstream.
    return {"answer": out}


# ---------------------------------------------------------------- identity
MEDIA = {}                              # token -> absolute path, minted here only
IDENTITY_TTL = 7 * 86400                # re-derive weekly; names change slowly
_identity_lock = threading.Lock()

LOGO_DIRS = ("brand", "branding", "assets", "static", "public", "img", "images",
             "media", "knowledge-base/company/assets", "docs")
LOGO_EXT = (".png", ".jpg", ".jpeg", ".webp")

IDENTITY_PROMPT = """\
Output ONE JSON object and nothing else. No prose, no code fence.

You are being asked to describe yourself for the identity panel of a voice app
that speaks with your user. Answer ONLY from files inside this project directory —
CLAUDE.md, README, company or brand files. Not from your own account, not from
global configuration, not from any conversation you remember, not from general
knowledge. If this project does not state a fact, it is null, even if you believe
you know it: the wrong person's name on a stranger's phone is the failure here.

ONE EXCEPTION, and only for agent_name: your own name is something users say rather
than write down, so the addresses listed at the end of this prompt (taken from this
project's own conversation logs) count as evidence for it. Nothing else may come
from them.

{"agent_name": "the name your user calls YOU. If the candidates listed below are
                present, choose the one that is actually your name and not a person
                you work with; otherwise take it from this project's files. null if
                you have no name of your own here",
 "company_name": "the company or organisation whose work lives here; null if none",
 "user_name": "the full name of the person you work for here; null if unknown",
 "user_email": "their email address; null if unknown",
 "logo_path": "absolute path to this company's logo image on this machine
               (png/jpg/webp, not svg); null if there is none"}

Use null, never a guess or a placeholder. It is better to say null than to invent
a name a real person will see on their phone."""


def scan_logo(workdir):
    """A logo file, found rather than configured. Preferred directories first,
    then anything named like a logo — the file is nearly always sitting in the
    project already, and asking a human to type its path is how it stays unset."""
    root = pathlib.Path(workdir)
    for sub in LOGO_DIRS:
        d = root / sub
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if p.suffix.lower() in LOGO_EXT and "logo" in p.name.lower():
                return str(p.resolve())
    try:
        for p in sorted(root.rglob("*logo*")):
            if p.suffix.lower() in LOGO_EXT and ".git" not in p.parts:
                return str(p.resolve())
    except OSError:
        pass
    return ""


SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".cache",
             "dist", "build", ".next", "target"}

# Words that open a sentence and look exactly like a name in vocative position.
NOT_A_NAME = {
    "yes", "no", "ok", "okay", "also", "and", "but", "so", "then", "now", "well",
    "please", "thanks", "thank", "good", "great", "sorry", "actually", "just",
    "still", "next", "first", "second", "last", "times", "note", "todo", "done",
    "fix", "add", "make", "run", "check", "read", "write", "send", "use", "let",
    "can", "could", "would", "should", "did", "does", "is", "are", "was", "the",
    "this", "that", "there", "here", "what", "why", "how", "when", "where", "who",
    "placeholder", "example", "test", "hi", "hey", "hello", "morning", "evening",
    # Header-ish words: pasted mail and logs are full of "Subject:", "From:",
    # "For, ..." and they sit in exactly the position a name sits in.
    "subject", "from", "to", "date", "re", "fwd", "cc", "bcc", "summary", "for",
    "mobile", "phone", "email", "sent", "sincerely", "regards", "best", "dear",
}
VOCATIVE = (
    re.compile(r"^\s*(?:hi|hey|hello|thanks|thank you|ok|okay)[,! ]+([A-Z][a-z]{1,15})\b", re.M),
    # Comma only, never a colon: "Subject: …" and "Note: …" sit in exactly the
    # position a name sits in, and a pasted email would out-vote the real answer.
    re.compile(r"^\s*([A-Z][a-z]{1,15}),\s", re.M),
    re.compile(r"\b(?:thanks|thank you|cheers)[,! ]+([A-Z][a-z]{1,15})\b", re.I),
)
# Being told outright beats being addressed: rare, but decisive when present.
NAMED = (
    re.compile(r"\byou(?:'re| are)\s+(?:called\s+)?([A-Z][a-z]{1,15})\b"),
    re.compile(r"\byour name is\s+([A-Z][a-z]{1,15})\b", re.I),
    re.compile(r"\bwe(?:'ll| will)? call you\s+([A-Z][a-z]{1,15})\b", re.I),
)


def _project_slug(workdir):
    """Claude Code stores a project's sessions under a path-derived directory."""
    return "-" + re.sub(r"[^A-Za-z0-9]+", "-", os.path.abspath(workdir)).strip("-")


def address_candidates(workdir, max_messages=600):
    """Names the USER uses to address this agent, counted from its own sessions.

    An agent's name is the one identity fact that is never written down: it is
    established by being used. "You are <name>" appears in no file on the machine
    of an agent everyone calls by that name,
    and asking the operator to add it is asking them to configure the thing we
    said would configure itself. The transcripts are where it does exist.

    Counted here rather than judged by a model: a count is evidence, and it is
    the difference between reading a name and inventing one."""
    root = pathlib.Path.home() / ".claude" / "projects" / _project_slug(workdir)
    if not root.is_dir():
        return {}
    hits, seen = {}, 0
    for f in sorted(root.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            for line in f.open(errors="ignore"):
                if seen >= max_messages:
                    break
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") != "user":
                    continue
                content = (d.get("message") or {}).get("content")
                if isinstance(content, list):
                    content = " ".join(c.get("text", "") for c in content
                                       if isinstance(c, dict))
                if not isinstance(content, str) or not content.strip():
                    continue
                if "identity panel of a voice app" in content:
                    # Our own derivation turn, logged like any other user message.
                    # Left in, the names it lists become evidence for the next run
                    # citing itself — a fact that gets truer every week without
                    # anyone ever saying it again.
                    continue
                seen += 1
                for rx, weight in [(r, 1) for r in VOCATIVE] + [(r, 10) for r in NAMED]:
                    for name in rx.findall(content):
                        name = name.strip().title()
                        if name.lower() in NOT_A_NAME:
                            continue
                        h = hits.setdefault(name, {"count": 0, "sessions": set(),
                                                   "samples": []})
                        h["count"] += weight
                        h["sessions"].add(f.name)
                        if len(h["samples"]) < 2:
                            h["samples"].append(" ".join(content.split())[:120])
        except OSError:
            continue
        if seen >= max_messages:
            break
    for h in hits.values():
        h["sessions"] = len(h["sessions"])
    # Spread across sessions before raw count: a name the user comes back to is
    # the agent's; a name that spikes inside one pasted email is a colleague's.
    return dict(sorted(hits.items(),
                       key=lambda kv: (-kv[1]["sessions"], -kv[1]["count"])))


def attested(value, workdir, max_files=2000, max_bytes=2_000_000):
    """Does this string actually appear in the project's own files?

    The check that turns a model's answer into evidence. Cheap, and it only ever
    removes: a name the project never writes down does not go on the panel."""
    needle = value.strip().lower()
    if len(needle) < 2:
        return False
    seen = 0
    for dp, dn, fn in os.walk(workdir):
        dn[:] = [d for d in dn if d not in SKIP_DIRS and not d.startswith(".")]
        for f in fn:
            if seen >= max_files:
                return False
            p = os.path.join(dp, f)
            try:
                if os.path.getsize(p) > max_bytes:
                    continue
                seen += 1
                with open(p, "r", errors="ignore") as fh:
                    if needle in fh.read().lower():
                        return True
            except OSError:
                continue
    return False


def derive_identity(timeout=180):
    """Ask the agent who it is, once, and cache the answer.

    Everything here is knowable on this machine — the agent's own name lives in
    CLAUDE.md, the company in its files, the user in the work they do together —
    so requiring an operator to type any of it is asking for the one thing they
    will skip. The agent reads its own project and answers.

    NEVER on the request path: the plane relays `branding` with a 15s timeout and
    a cold model turn takes longer, so this runs at startup and from pair.py, and
    the panel is served from cache."""
    cfg = config()
    exe = claude_bin()
    if not exe:
        return {}
    workdir = os.path.expanduser(cfg["workdir"])
    env = dict(os.environ)
    if os.geteuid() == 0:
        env.setdefault("IS_SANDBOX", "1")

    # Names the user has actually used to address this agent, counted from its own
    # sessions. Offered as candidates, never as the answer: the model picks which
    # one is its name, and can only pick one that was really said.
    cands = address_candidates(workdir)
    prompt = IDENTITY_PROMPT
    if cands:
        listed = "\n".join(
            f"- {n}: addressed {h['count']}x across {h['sessions']} session(s), "
            f"e.g. {' | '.join(repr(x) for x in h['samples'])}"
            for n, h in list(cands.items())[:5])
        prompt += (
            "\n\nEvidence for agent_name — names used in an address position in this "
            f"project's own conversation logs:\n{listed}\n\n"
            "Every message in those logs was written TO you, so a name in the "
            "greeting position is usually yours. It is NOT yours when the line is "
            "quoted or forwarded text, an email pasted in, or a message about a "
            "colleague rather than to you — read the samples and judge. Recurring "
            "across several sessions is the strongest sign; a single mention inside "
            "pasted content is the weakest. Put your choice in agent_name; this "
            "evidence outranks the files-only rule above. If none of them is you, "
            "agent_name is null.")

    cmd = [exe, "-p", prompt, "--dangerously-skip-permissions"]
    if cfg.get("model"):
        cmd += ["--model", cfg["model"]]
    try:
        r = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True,
                           timeout=timeout, env=env)
        if r.returncode != 0 and "cannot be used with root" in (r.stdout or "") + (r.stderr or ""):
            r = subprocess.run([c for c in cmd if c != "--dangerously-skip-permissions"],
                               cwd=workdir, capture_output=True, text=True,
                               timeout=timeout, env=env)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return {}

    out = (r.stdout or "").strip()
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return {}
    try:
        got = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}

    ident = {}
    for k in ("agent_name", "company_name", "user_name", "user_email"):
        v = got.get(k)
        if isinstance(v, str) and v.strip() and v.strip().lower() not in ("null", "none", "unknown"):
            v = v.strip()[:120]
            # Attested by the project or dropped. Asked who its user is, an agent
            # with nothing to read will answer from the account the CLI is signed
            # in as — which on a shared build is a real person with no connection
            # to this install. A fact the project cannot show in writing is not a
            # fact about this project.
            #
            # Its own name is the exception, and it has to be: nobody writes down
            # what they call their agent, they just call it that. The transcripts
            # are the record, so a name the user has actually used counts as
            # written down.
            if k == "agent_name" and v in cands:
                ident[k] = v
            elif attested(v, workdir):
                ident[k] = v
            else:
                print(f"[voice-agent] identity: dropping {k}={v!r} — not found in "
                      f"{workdir}", file=sys.stderr, flush=True)
    logo = got.get("logo_path")
    logo = logo if isinstance(logo, str) and os.path.exists(os.path.expanduser(logo or "")) else ""
    # The filesystem is the more reliable witness of its own contents: a model
    # asked for a path will occasionally produce a plausible one that is not there.
    ident["logo"] = os.path.expanduser(logo) if logo else scan_logo(workdir)
    return {k: v for k, v in ident.items() if v}


def cached_identity():
    st = load(STATE, {})
    return st.get("identity") or {}


def ensure_identity(force=False, timeout=180):
    """Derive if missing or stale. Returns what the panel should show."""
    st = load(STATE, {})
    age = time.time() - float(st.get("identity_ts") or 0)
    if not force and st.get("identity") and age < IDENTITY_TTL:
        return st["identity"]
    if not _identity_lock.acquire(blocking=False):
        return st.get("identity") or {}       # a refresh is already running
    try:
        ident = derive_identity(timeout=timeout)
        if ident:
            with _lock:
                st = load(STATE, {})
                st["identity"] = ident
                st["identity_ts"] = time.time()
                save(STATE, st)
            return ident
        return st.get("identity") or {}
    finally:
        _identity_lock.release()


def _remint(token):
    """A token from before the last restart, back to its path.

    MEDIA is memory only, and the app caches history rows containing tokens on
    disk — so without this every restart quietly kills every picture in the
    conversation, and the older the chat the more of it is dead. Tokens are
    derived from the path, so the map can be rebuilt by walking the uploads.
    """
    if not token:
        return None
    root = os.path.join(os.path.expanduser(config()["workdir"]), UPLOAD_DIR)
    dirs = [os.path.join(root, d)
            for d in (sorted(os.listdir(root), reverse=True)
                      if os.path.isdir(root) else [])]
    # A reminder banner is minted in the CRON process, not this one, so a push
    # carried a token this server had never seen and every extension fetch
    # 404'd: derived correctly, named correctly, unreachable.
    dirs.append(BANNER_DIR)
    for dd in dirs:
        for nm in os.listdir(dd) if os.path.isdir(dd) else []:
            p = os.path.join(dd, nm)
            if media_token(p) == token:      # mints into MEDIA as a side effect
                return p
    logo = os.path.expanduser(str(branding().get("logo") or ""))
    if logo and os.path.exists(logo) and media_token(logo) == token:
        return logo
    return None


def media_token(path):
    """Stable token for a file: same path, same token, across restarts.

    The plane never receives bytes it did not ask for — it gets a token in the
    branding panel and fetches it back through `file`, like any attachment."""
    cfg = config()
    tok = hashlib.sha256((cfg["secret"] + "|" + os.path.abspath(path))
                         .encode()).hexdigest()[:32]
    MEDIA[tok] = os.path.abspath(path)
    return tok


def branding():
    """The identity panel the app shows: who is speaking, for whom, and the logo.

    Derived, not configured. Without it the app shows a blank name and a generic
    assistant — the honest look for a machine nobody has set up, and the wrong one
    for an install that had every fact available on disk the whole time. config.json
    still wins where it is set, for the operator who wants a different answer than
    the true one."""
    cfg = config()
    ident = cached_identity()
    b = {}
    for key, field in (("agent_name", "bot_name"), ("company_name", "company_name"),
                       ("user_name", "user_name"), ("user_email", "user_email")):
        val = str(cfg.get(key) or ident.get(key) or "").strip()
        if val:
            b[field] = val
    logo = os.path.expanduser(str(cfg.get("logo") or ident.get("logo") or "").strip())
    if logo and os.path.exists(logo):
        b["logo_token"] = media_token(logo)
    return b


# Sentences that ASK for a change, and answers that CLAIM one. Both are
# deliberately loose: a false confirmation is expensive and a needless snapshot
# costs one SELECT.
_AMEND_SHAPE = re.compile(
    r"\b(change|move|set|make|edit|update|reschedul\w+|rename|push|delay|"
    r"cancel|delete|remove|drop|snooze)\b.{0,80}\breminder\b|"
    r"\breminder\b.{0,80}\b(to|for|at)\b", re.I | re.S)
# ...unless the sentence is about a DIFFERENT system. The guard fired on my own
# maintenance request ("delete both cloud routines") and called a true answer a
# lie, because the local store had correctly not moved. A guard that polices
# claims about a store it cannot see is worse than none.
_ELSEWHERE = re.compile(r"\b(routine|remotetrigger|cloud|cron|calendar)\b", re.I)
_CLAIMS_DONE = re.compile(
    r"\b(done|updated|changed|moved|rescheduled|set for|now set|cancelled|"
    r"canceled|deleted|removed)\b", re.I)


def reminders_snapshot():
    """Every reminder's id, time, text and status — the thing a confirmation is
    supposed to be about. Compared before and after a model turn, so "done" has
    to correspond to something that actually moved."""
    db = os.environ.get("REMINDERS_DB") or os.path.join(
        os.path.expanduser(config()["workdir"]),
        "operations/reminders/reminders.db")
    try:
        import sqlite3
        cx = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        rows = cx.execute("SELECT id, when_epoch, text, status FROM reminders"
                          ).fetchall()
        cx.close()
        return sorted(rows)
    except Exception:
        return None            # unknown state: never used to accuse the model



# "Give me the agent key" means the ENCRYPTION fingerprint (2026-08-19). Asked
# for it, an agent went looking and answered with a signing key it found on
# disk — plausible, adjacent, and wrong in the one place where a wrong number
# is worse than no number: the digits exist so a substituted key is caught, and
# a confident irrelevant answer defeats that as thoroughly as silence.
#
# So it is answered from the key itself, with no model in the path.
_KEY_ASK = re.compile(
    r"\b(fingerprint|safety number)\b|"
    r"\b(encryption|agent|public|pairing)\s+key\b|"
    r"\bkey\b[^.?!]{0,20}\b(verify|verification|compare|encryption)\b|"
    r"\b(verify|check|compare)\b[^.?!]{0,20}\bkey\b", re.I)


def fingerprint_answer(question):
    """The twenty digits, or None if this is not that question."""
    if not question or not _KEY_ASK.search(question):
        return None
    try:
        _priv, pub = agent_keys()
    except Exception as e:
        return (f"I have no encryption key yet, so there is nothing to "
                f"compare ({str(e)[:80]}).")
    return (f"{key_fingerprint(pub)}\n\nThat is this agent's encryption "
            f"fingerprint. Compare it with the number under Settings → "
            f"Encryption in the app: they must match exactly, digit for digit. "
            f"It is not a signing key or a login token — it is the safety "
            f"number for the key your messages would be sealed to.")

def reflex_answer(question, tz=None):
    """A deterministic answer for questions that never needed a model, or None.

    One reflex today — reminders — and it exists because the app renders a GRID
    when it receives a markdown table and prose when it does not. The table has
    to be byte-shaped: a `<!--id:N-->` marker inside the When cell is what makes
    a row tappable, and no model reproduces that reliably turn after turn.
    """
    # The safety number is answered from the key, before anything else: it is
    # the one question where a plausible neighbouring answer is worse than no
    # answer at all.
    fp = fingerprint_answer(question)
    if fp:
        return fp
    d = os.path.join(os.path.expanduser(config()["workdir"]), "telegram")
    if not os.path.isfile(os.path.join(d, "reminders_reflex.py")):
        return None
    if d not in sys.path:
        sys.path.insert(0, d)
    os.environ.setdefault("REMINDERS_DB", os.path.join(
        os.path.expanduser(config()["workdir"]),
        "operations/reminders/reminders.db"))
    try:
        import reminders_reflex
        # The READER's zone, forwarded by the plane from the phone. A
        # reminder due Saturday read "Tomorrow" because this VPS runs
        # UTC and the reader does not.
        if hasattr(reminders_reflex, "set_viewer_tz"):
            reminders_reflex.set_viewer_tz(tz)
        # AMEND FIRST, and this ordering is not cosmetic. 2026-08-13: an owner
        # opened a reminder card and asked to move it. This function
        # only tried `answer()`, which is the LISTING reader — the sentence
        # contained "today 19:30" from the card, so it matched as a request for
        # today's list, and the change fell through to the model. The model
        # said "Done — fix the monitor is now set for 7:30 PM" and changed
        # nothing: it confirmed the time it had been TOLD, which was the old
        # one. Nobody was lying and nothing was edited.
        #
        # A confirmation must come from the store. `amend()` returns the answer
        # it can prove and (None, False) for anything it cannot do exactly, and
        # only then is this a listing question.
        ans, changed = reminders_reflex.amend(question)
        if ans:
            if changed:
                rid = re.search(r"\breminder\s+(\d+)", ans, re.I)
                extra = (reminders_reflex.after_amend(int(rid.group(1)))
                         if rid else
                         reminders_reflex.render(reminders_reflex.pending(),
                                                 client="ios"))
                if extra:
                    ans += "\n\n" + extra
            return ans
        out = reminders_reflex.answer(question, client="ios")
    except Exception as e:
        print(f"[voice-agent] reminders reflex: {e}", file=sys.stderr)
        return None
    if not out:
        return None
    return out[0] if isinstance(out, tuple) else out



def capabilities():
    """Declared from what this install can actually answer. `branding` is claimed
    whenever a panel exists — including one derived a moment ago, which is why the
    derivation runs before pair.py registers rather than after."""
    # `progress` is unconditional: it costs nothing, and the app treats its absence
    # as an agent that is not there.
    caps = ["ask", "health", "progress", "history"]
    try:                       # only claimed when a key really exists
        agent_keys()
        caps.append("pubkey")
    except Exception:
        pass
    b = branding()
    if b:
        caps.append("branding")
    # `file` used to be claimed only when a logo existed, which was true when
    # the only servable file WAS the logo. Uploads are servable by the same
    # route, so it is now unconditional — and the plane checks this list before
    # relaying, so an unclaimed capability is a feature that silently is not
    # there rather than one that fails loudly.
    caps.append("file")
    # LQ, DECLARED ONLY WHEN IT WOULD ACTUALLY WORK. probe() checks the
    # transcriber, its model, the speech binary and a voice file; a tier that
    # advertises itself and then answers HTTP 400 is worse than one that says
    # plainly it is unavailable, and the app is specified to show the row with
    # a reason rather than hide it.
    #
    # BOTH SPELLINGS, on purpose. Every other capability here uses an
    # underscore and none use a hyphen, so `voice_local` is the consistent
    # name — but the client may gate its tier row on either, and a naming
    # mismatch would present as a tier that simply never appears, which is the
    # one failure shape this project keeps paying for. Two strings cost
    # nothing; a silent absence costs a day.
    try:
        import local_voice
        if local_voice.probe()[0]:
            caps += ["voice_local", "voice-local"]
    except Exception:
        pass
    # Claimed only if there is somewhere to record an upload. Without an
    # archive a photo could be stored but never appear in the conversation,
    # and the honest answer to "can you take a photo" is then no.
    if _chatdb():
        caps += ["photo", "photos", "attachments", "log", "reset"]
    # The app starts sealing the moment it sees this, so it is claimed only
    # when a key exists, the library imports, and the operator turned it on.
    if e2ee_ready(caller()):
        caps.append("e2ee-v1")
    return caps



# ------------------------------------------------------------------ keys
# End-to-end encryption, phone <-> agent (2026-08-19). The agent half of the
# key exchange lives here so the PLANE never chooses a key: whoever composes
# the pairing QR chooses the public key in it, and if that were the relay the
# whole scheme would be theatre it could silently defeat.
#
# The private half is written to the agent's own disk, 0600, and never leaves
# it — not in a relay, not in a QR, not in a log line. What travels is the
# public key and a fingerprint short enough for two people to read aloud.
KEY_FILE = "agent-x25519.key"


def _key_path():
    return os.path.join(str(HERE), KEY_FILE)


RETIRED_KEYS_FILE = "agent-x25519.retired"


def rotate_agent_key():
    """New key for sealing; the old one KEPT for opening.

    A rotation that destroys the previous private key destroys the archive
    sealed to it — the failure is silent and total, and it arrives weeks later
    when somebody scrolls back. Retired keys are appended, never dropped, and
    `key_id` in an envelope says which one to reach for.
    """
    from cryptography.hazmat.primitives import serialization
    p = _key_path()
    if os.path.exists(p):
        with open(p, "rb") as f:
            old_raw = f.read()
        with open(os.path.join(str(HERE), RETIRED_KEYS_FILE), "ab") as f:
            f.write(old_raw)                      # 32 bytes per retired key
        os.chmod(os.path.join(str(HERE), RETIRED_KEYS_FILE), 0o600)
        os.remove(p)
    priv, pub = agent_keys()
    print(f"[voice-agent] key rotated; fingerprint now {key_fingerprint(pub)}",
          file=sys.stderr)
    return pub


def retired_keys():
    """Every private key this agent has retired, oldest first."""
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey)
    p = os.path.join(str(HERE), RETIRED_KEYS_FILE)
    out = []
    try:
        raw = open(p, "rb").read()
    except OSError:
        return out
    for i in range(0, len(raw) - 31, 32):
        try:
            out.append(X25519PrivateKey.from_private_bytes(raw[i:i + 32]))
        except Exception:
            continue
    return out


def agent_keys():
    """(private, public) X25519 keys, generated once and kept."""
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey)
    from cryptography.hazmat.primitives import serialization
    p = _key_path()
    if os.path.exists(p):
        with open(p, "rb") as f:
            priv = X25519PrivateKey.from_private_bytes(f.read())
    else:
        priv = X25519PrivateKey.generate()
        raw = priv.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption())
        with open(p, "wb") as f:
            f.write(raw)
        os.chmod(p, 0o600)
        print(f"[voice-agent] generated an X25519 key pair at {p}",
              file=sys.stderr)
    pub = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)
    return priv, pub


def key_fingerprint(pub=None):
    """A short safety number, said aloud to detect a substituted key.

    Four groups of five digits, from SHA-256 of the public key. The fingerprint
    is the ONLY defence against a relay that swaps the key in transit, so it
    has to be short enough that somebody actually reads it out.
    """
    if pub is None:
        _priv, pub = agent_keys()
    # THE DERIVATION, stated so another implementation can reproduce it exactly:
    #
    #   1. take the RAW 32-byte X25519 public key (not base64, not hex)
    #   2. SHA-256 it
    #   3. take the FIRST 10 bytes of the digest
    #   4. read them as a BIG-ENDIAN unsigned integer
    #   5. reduce modulo 10**20
    #   6. render as decimal, ZERO-PADDED on the left to exactly 20 digits
    #   7. group in fours of five, separated by single spaces
    #
    # 2026-08-19: the first version left-padded to 25 digits and kept the
    # leading 20 — reproducible only by reading this code, which is the one
    # thing a safety number must not require. The app must COMPUTE this from
    # the key it stored; a fingerprint read off the wire verifies nothing,
    # because a substituting party sends a matching pair.
    h = hashlib.sha256(pub).digest()
    n = int.from_bytes(h[:10], "big") % (10 ** 20)
    digits = str(n).zfill(20)
    return " ".join(digits[i:i + 5] for i in range(0, 20, 5))


# ------------------------------------------------------- e2ee (wire v1)
# The agent half of docs/feature-e2e-encryption.md, matching
# MessageCrypto.swift byte for byte. Every constant here is load-bearing: a
# derivation that differs by one byte produces a key that decrypts nothing,
# and the failure looks like a corrupted message rather than a mismatch.
#
#   key   = HKDF-SHA256(ikm=X25519(priv, their_pub),
#                       salt="voicebridge-e2ee-v1",
#                       info=b"voicebridge/v1/" + direction + 0x00
#                            + sorted([my_pub, their_pub])[0]
#                            + sorted([my_pub, their_pub])[1],
#                       length=32)
#   ct    = AES-256-GCM(key, nonce=12 random bytes, plaintext); the envelope's
#           `ct` carries ciphertext||tag, `nonce` carries the 12 bytes, because
#           CryptoKit splits its combined box exactly there.
#   key_id = sha256(recipient_public_key)[:8] as hex — WHICH KEY this was
#           sealed to, so a rotated key is a readable failure, not a mystery.
#   dedupe = HMAC-SHA256(normalised_text) with a key derived under the
#           direction string "dedupe", first 16 bytes as hex. Separate key, so
#           a tag can never leak anything about the one that hides the words.
E2EE_ALG = "x25519-hkdf-sha256-aes256gcm"
E2EE_SALT = b"voicebridge-e2ee-v1"
DIR_TO_AGENT = "phone->agent"
DIR_TO_PHONE = "agent->phone"


def _hkdf(shared, direction, my_pub, their_pub):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    pair = sorted([my_pub, their_pub])
    info = (b"voicebridge/v1/" + direction.encode() + b"\x00"
            + pair[0] + pair[1])
    return HKDF(algorithm=hashes.SHA256(), length=32,
                salt=E2EE_SALT, info=info).derive(shared)


def _shared(priv, their_pub):
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
    return priv.exchange(X25519PublicKey.from_public_bytes(their_pub))


def e2ee_key(direction, priv, my_pub, their_pub):
    return _hkdf(_shared(priv, their_pub), direction, my_pub, their_pub)


def e2ee_normalised(text):
    """Whitespace-collapsed, exactly as the app normalises before its HMAC."""
    return " ".join((text or "").split())


def e2ee_dedupe_tag(text, priv, my_pub, their_pub):
    import hmac as _hmac
    k = _hkdf(_shared(priv, their_pub), "dedupe", my_pub, their_pub)
    mac = _hmac.new(k, e2ee_normalised(text).encode(), hashlib.sha256).digest()
    return mac[:16].hex()


def e2ee_key_id(pub):
    return hashlib.sha256(pub).digest()[:8].hex()


def v2_reader_keys():
    """Raw X25519 pubkeys of every device this agent has accepted (#352).

    One accepted device stays on v1 — every phone in the field speaks it. Two
    or more cannot be served by v1 at all, which is the entire reason v2
    exists: the second device's history is a wall of placeholders until the
    message is wrapped for its key as well.
    """
    try:
        import base64 as _b64
        import devices as _dev
        rows = _dev.rows()
        out = []
        for dev in _dev.accepted_ids():
            try:
                raw = _b64.b64decode((rows.get(dev) or {}).get("pubkey") or "")
            except Exception:
                continue
            if len(raw) == 32:
                out.append(raw)
        return out
    except Exception:
        return []


def seal_for_devices(text, direction=None):
    """v2 when there are two readers, None when there are not — caller falls
    back to v1 so a single-device account is byte-identical to yesterday."""
    fleet = v2_reader_keys()
    if len(fleet) < 2:
        return None
    import e2ee_v2
    return e2ee_v2.seal(text, fleet, direction or e2ee_v2.DIR_TO_PHONE)


def e2ee_seal(text, priv, my_pub, their_pub, direction=DIR_TO_PHONE):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import base64 as _b64
    nonce = os.urandom(12)
    body = AESGCM(e2ee_key(direction, priv, my_pub, their_pub)).encrypt(
        nonce, (text or "").encode(), None)
    return {"v": 1, "alg": E2EE_ALG,
            # Sealed TO the phone, so the phone's key names it.
            "key_id": e2ee_key_id(their_pub),
            "nonce": _b64.b64encode(nonce).decode(),
            "ct": _b64.b64encode(body).decode(),
            "dedupe": e2ee_dedupe_tag(text, priv, my_pub, their_pub)}


def e2ee_open(env, priv, my_pub, their_pub, direction=DIR_TO_AGENT):
    """Plaintext, or raises. NEVER returns a placeholder on failure: a message
    that silently becomes empty is indistinguishable from one nobody sent."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import base64 as _b64
    if not isinstance(env, dict):
        raise ValueError("no envelope")
    if str(env.get("alg") or E2EE_ALG) != E2EE_ALG:
        raise ValueError(f"unknown alg {env.get('alg')!r}")
    want = e2ee_key_id(my_pub)
    got = str(env.get("key_id") or "")
    if got and got != want:
        # Sealed to a key this agent no longer uses (or never had). Say which,
        # because "cannot decrypt" and "sealed to yesterday's key" have
        # different remedies and only one of them is a bug.
        raise ValueError(f"sealed to key_id {got}, this agent is {want}")
    nonce = _b64.b64decode(env["nonce"])
    body = _b64.b64decode(env["ct"])
    return AESGCM(e2ee_key(direction, priv, my_pub, their_pub)).decrypt(
        nonce, body, None).decode()


def public_key_b64():
    import base64 as _b64
    _priv, pub = agent_keys()
    return _b64.b64encode(pub).decode()

# ---- who the far end is, and whether we are sealing at all
#
# THE HOLE THE CONTRACT DOES NOT CLOSE (2026-08-20, #254): X25519 needs the
# PEER's public key, and nothing on the wire carries the phone's. The app
# computes `devicePublicKeyBase64` and never sends it, so an agent has no key
# to derive against and open() cannot even be attempted. Handled two ways, so
# whichever the app adopts, this side already works:
#
#   * `pk` (base64 raw 32 bytes) beside `sealed`, or at the top of the body;
#   * anything previously pinned for that account.
#
# PINNED ON FIRST SIGHT, per account, and a CHANGE IS REFUSED. Trust-on-first-
# use is not paranoia here: whoever relays the first message could substitute a
# key of their own and read everything after it. Pinning means that attack has
# to win the very first message and can never be applied to a live pairing —
# and a refusal is visible, where a silent re-pin is not.
PEERS_FILE = "peer-keys.json"


class PeerKeyChanged(ValueError):
    """A different device key for an account that already pinned one.

    Its own type because it is the one refusal with a REMEDY: everything else
    that fails to open is a bug or an attack, and this one is usually a person
    holding a new phone. The app needs to tell those apart to know whether to
    show "re-pair" or "something is wrong", so it gets its own error code on
    the wire rather than a prose detail nobody can branch on.
    """


def unpin_peer(account):
    """Forget the pinned device key, so the next one is accepted.

    THE PIN MUST HAVE A WAY OUT, and one a person can reach (2026-08-20).
    Restore a phone from a backup that excluded the Keychain — a failure mode
    the design document lists — and the device generates a new key that this
    agent then refuses forever. Without this, the remedy is an operator with
    ssh, which is not a remedy for anybody who does not have one. Clearing on
    a fresh pairing scan makes re-pairing the act that authorises it: a person
    holding the agent's own QR code is the same evidence the first pin had.
    """
    with _lock:
        store = load(_peers_path(), {})
        if account in store:
            store.pop(account, None)
            save(_peers_path(), store)
            print(f"[voice-agent] device key unpinned for {account} — the "
                  f"next sealed message will pin a new one", file=sys.stderr)
            return True
    return False


def _peers_path():
    # A pathlib.Path: load()/save() call .read_text()/.write_text() on it, and
    # a str here fails only at the moment a sealed message arrives — which is
    # exactly the moment nothing may quietly fall back to plaintext.
    return HERE / PEERS_FILE


def push_preview_envelope(account, text):
    """The sealed banner preview: {"from": ..., "text": ...}, or None.

    A push carried a sealed preview only for the MESSAGE
    family, and only ever as a bare string — so the family he actually sees,
    the answer to his own question, showed "Your agent has replied." forever,
    and even the message banner could not name who was speaking.

    THE NAME GOES INSIDE THE SEAL. It is as private as the words: a plaintext
    `from` beside `aps` hands it to Apple and to anyone reading a plane log,
    which is the rule #271 set for `account` and this is one level stricter.

    Wrapped for every approved device when there is more than one, so a second
    phone's banner is not blank for the reason its conversation used to be.
    """
    if not e2ee_locked(account):
        return None
    body = " ".join(str(text or "").split())[:300]
    if not body:
        return None
    payload = json.dumps(
        {"from": branding().get("bot_name") or "agent", "text": body},
        ensure_ascii=False, separators=(",", ":"))
    multi = seal_for_devices(payload)
    if multi:
        return multi
    priv, mine = agent_keys()
    theirs = peer_key(account)
    if theirs is None:
        return None
    return e2ee_seal(payload, priv, mine, theirs, direction=DIR_TO_PHONE)


def approved_device_pubkeys():
    """Base64 pubkeys of every device the registry has ACCEPTED and not revoked.

    The inbound counterpart to `v2_reader_keys()`: that one decides who may read
    what this agent sends, this one decides whose sealed asks it will open. Same
    source of truth, so a device cannot be a legitimate reader and an
    illegitimate writer at the same time.
    """
    try:
        import devices as _dev
        return {r.get("pubkey") for r in _dev.rows().values()
                if r.get("accepted") and not r.get("revoked") and r.get("pubkey")}
    except Exception:
        return set()


SPOKEN_NO_CALL = "[spoken-no-call]"


def _check_fabrication(log_message, text, account):
    """Run the fabrication invariant over a line the model spoke with no call.

    The app marks such a line `[spoken-no-call] <what was said>`; anything else
    on the diagnostic channel passes straight through. The verdict is LOGGED,
    not announced from here — one announcement path, already tested, lives in
    the watcher, and an agent that talks to Telegram from inside a request
    handler is an agent that can block a turn on a network call.
    """
    text = (text or "").strip()
    if not text.startswith(SPOKEN_NO_CALL):
        return
    try:
        import fabrication
        verdict = fabrication.check_spoken(account or "default",
                                           text[len(SPOKEN_NO_CALL):])
    except Exception as e:
        log_message("fabrication check skipped: %.80s", e)
        return
    if verdict:
        log_message("FABRICATION %s", json.dumps(verdict))


def _voice_turn(plaintext):
    """The opened payload if this is an LQ turn, else None."""
    t = (plaintext or "").lstrip()
    if not t.startswith("{") or '"voice"' not in t[:400]:
        return None
    try:
        p = json.loads(t)
    except ValueError:
        return None
    return p if isinstance(p, dict) and isinstance(p.get("voice"), dict) else None


def peer_key(account, offered_b64=None):
    """The phone's public key for this account, pinning a new one once.

    A KEY THE REGISTRY HAS APPROVED IS NOT A CHANGED KEY (2026-08-31, from the
    ledger). Multi-device landed on the OUTBOUND leg — v2 wraps one answer for
    every approved device — and this, the inbound leg, stayed pinned to
    whichever device sealed first. So the second device's every question died
    here: `PeerKeyChanged` -> HTTP 400 -> the plane's 502, three times per turn,
    while the phone showed a promise to check that nothing was ever made for.

    The pin defends against an UNKNOWN key substituted by the relay. A key in
    the registry is not unknown: it is a device this agent accepted, announced,
    and (outside a sandbox install) a human approved by name. Approval is the
    gate; the pin stays as the first device's fallback for v1 sealing.
    """
    import base64 as _b64
    with _lock:
        store = load(_peers_path(), {})
        cur = store.get(account)
        if offered_b64:
            try:
                raw = _b64.b64decode(offered_b64, validate=True)
            except Exception:
                raise ValueError("peer key is not valid base64")
            if len(raw) != 32:
                raise ValueError(f"peer key is {len(raw)} bytes, expected 32")
            if cur and cur != offered_b64:
                if offered_b64 in approved_device_pubkeys():
                    print(f"[voice-agent] second approved device sealed to "
                          f"{account}: {key_fingerprint(raw)}", file=sys.stderr)
                    return raw
                raise PeerKeyChanged(
                    "this account is pinned to a different device key and that "
                    "key is not an approved device — scan a fresh pairing code "
                    "to re-pair, which clears the old pin deliberately instead "
                    "of accepting a new key mid-conversation")
            if not cur:
                store[account] = offered_b64
                save(_peers_path(), store)
                print(f"[voice-agent] pinned device key for {account}: "
                      f"{key_fingerprint(raw)}", file=sys.stderr)
            return raw
        if cur:
            return _b64.b64decode(cur)
    return None


def e2ee_ready(account=None):
    """True when this agent can seal AT ALL — the capability declaration.

    NOT conditional on knowing the phone's key, and that was a deadlock I built
    and had to take back out (#255): the phone only sends its key once it
    starts sealing, and it only starts sealing once it sees this capability.
    Requiring the key here meant the switch could never be thrown.
    """
    if not config().get("e2ee", False):
        return False
    try:
        import cryptography                      # noqa: F401
        agent_keys()
    except Exception:
        return False
    return True


def e2ee_locked(account):
    """True once this account has PROVED it can seal, i.e. a pinned key.

    This — not the declaration — is what makes plaintext a refusal. The
    contract says a declared agent refuses plaintext, and taken literally that
    bricks any phone on an older build the moment the flag goes on: it cannot
    seal, so every message it sends is refused and the person is simply cut
    off with an error they cannot act on. Keyed on evidence instead, the
    downgrade is still impossible where it matters — once a device has sealed
    even once, plaintext claiming to be that device is refused forever — and an
    old build keeps working until its user updates.
    """
    return e2ee_ready(account) and peer_key(account) is not None


# ---------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    server_version = "voice-agent/1.0"

    def log_message(self, fmt, *a):
        sys.stderr.write("[voice-agent] %s\n" % (fmt % a))

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # A liveness probe that needs no secret, for tunnels and load balancers.
        if self.path.rstrip("/") in ("", "/health"):
            return self._send(200, {"service": "voice-agent", "ok": health()["ok"]})
        self._send(404, {"error": "not found"})

    def _voice_answer(self, payload, account, name, d, priv, mine, theirs):
        """One LQ turn: audio in, the agent's own answer, audio out.

        THE ANSWER COMES FROM THE ORDINARY ASK PATH, not from a voice-shaped
        copy of it. A spoken question and a typed one are the same question, and
        two code paths answering them is how they drift — the reminders reflex,
        the fabrication ledger and the archive all apply here because they are
        the same call.
        """
        import local_voice
        t0 = time.time()
        try:
            def answer_fn(text):
                self.log_message("voice ask from %s: %.60s",
                                 name or account, text)
                res = ask(account, text, name,
                          archive_turn=(d.get("archive") is not False),
                          context=time_context(d.get("tz")))
                return str(res.get("answer") or "")

            out = local_voice.turn(payload, answer_fn)
        except Exception as e:
            # A voice turn that fails must fail LOUDLY and in the clear: the app
            # can show "I could not hear that", and a SEALED error is a message
            # the person cannot read about a message they could not hear.
            self.log_message("VOICE TURN FAILED: %.200s", e)
            return self._send(400, {"error": "voice_turn_failed",
                                    "detail": str(e)[:300]})
        self.log_message(
            "voice turn: %.1fs in, %.1fs out, stt %.1fs tts %.1fs, %d KB reply",
            out["audio_seconds_in"], out["audio_seconds_out"],
            out["timing"]["stt_s"], out["timing"]["tts_s"],
            len(out["voice"]["b64"]) // 1024)
        LAST_APP_TURN[account] = time.time()
        body = json.dumps({"text": out["text"], "user_text": out["user_text"],
                           "voice": out["voice"]}, ensure_ascii=False)
        sealed = seal_for_devices(body) or e2ee_seal(
            body, priv, mine, theirs, direction=DIR_TO_PHONE)
        # DURATIONS IN THE CLEAR, beside the envelope, because the meter cannot
        # read the envelope. Both from this end — the only party that decodes
        # both directions — with the phone's own figure arriving separately as a
        # cross-check rather than as the source.
        return self._send(200, {
            "sealed": sealed,
            "audio_seconds_in": out["audio_seconds_in"],
            "audio_seconds_out": out["audio_seconds_out"],
            "engine": "local",
            "took_s": round(time.time() - t0, 2)})

    def do_POST(self):
        cfg = config()
        auth = self.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not secrets.compare_digest(token, cfg["secret"]):
            self.log_message("rejected: bad or missing bearer")
            return self._send(401, {"error": "unauthorized"})

        try:
            n = int(self.headers.get("Content-Length") or 0)
            d = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"error": "bad json"})

        kind = str(d.get("type") or "")
        account = str(d.get("account") or "default")
        set_caller(account)
        name = str(d.get("account_name") or "")

        if kind == "pubkey":
            # The agent's public key and its fingerprint. The plane relays this
            # blindly; it cannot substitute a key without the fingerprint the
            # two humans compare failing to match.
            try:
                return self._send(200, {"alg": "x25519",
                                        "public_key": public_key_b64(),
                                        "fingerprint": key_fingerprint()})
            except Exception as e:
                return self._send(200, {"error": str(e)[:120]})
        if kind in ("device_register", "device_revoke", "devices"):
            # MULTI-DEVICE E2EE (#347 veto 1, ported from the box). A device in
            # the registry is a new pair of eyes on everything sealed, so the
            # relay may record and ask but never grant: this end keeps its own
            # list, tells the owner what asked to link, and answers pending
            # until a human approves. Without these three the plane's device
            # screen shows a permanent "pending" — which is what it did.
            sys.path.insert(0, str(HERE))
            try:
                import devices as _dev
            except Exception as e:
                return self._send(200, {"accepted": False,
                                        "error": f"device store unavailable: {str(e)[:80]}"})
            if kind == "devices":
                return self._send(200, {"accepted": _dev.accepted_ids(),
                                        "rows": _dev.rows()})
            _id = str(d.get("device_id") or "")[:32]
            if kind == "device_revoke":
                return self._send(200, {"revoked": bool(_dev.revoke(_id))})
            # A SANDBOX HAS NO OWNER TO ASK. `auto_approve_devices` is off
            # unless an install sets it, and the demo sets it: long-press the
            # guide button, type the password, and you are a stranger with no
            # one to approve you — a gate there is a locked door with nobody
            # behind it. Real installs are unchanged, and the relay still
            # approves nothing either way.
            _auto = bool(config().get("auto_approve_devices"))
            _dev.register(_id, str(d.get("pubkey") or "")[:200],
                          str(d.get("label") or "")[:60],
                          str(d.get("type_") or "")[:24],
                          str(d.get("os") or "")[:40],
                          str(d.get("location") or "")[:60],
                          auto_approve=_auto)
            ok = _id in _dev.accepted_ids()
            if _auto:
                self.log_message("device %s auto-approved (sandbox install): "
                                 "%s", _id, d.get("label") or "unnamed")
            if not ok and not _auto:
                try:                       # tell the owner, in his own chat
                    # AND IN TELEGRAM, not only the transcript (#355). A device
                    # asking to read everything is what someone must see when
                    # they are not holding the app, and if a registration is
                    # ever hostile the announcement IS the alarm. An install
                    # with no chat behind it (a demo account) still gets the
                    # transcript line, which is all it has.
                    # archive() here takes (text, direction, sender=…) — the
                    # box's takes (who, text). Same job, different signature,
                    # and the first port called the box's shape into this file.
                    archive("[a new device asked to link: "
                            f"{d.get('label') or 'unnamed'} "
                            f"({d.get('type_') or '?'}, {d.get('os') or '?'}) "
                            f"from {d.get('location') or 'an unknown place'} — "
                            f"id {_id}. It can read NOTHING until you approve "
                            f"it — and approving lets it read this "
                            f"conversation's HISTORY as well as new messages. "
                            f"To allow that: reply \"approve device "
                            f"{d.get('label') or _id}\"]", "in",
                            sender=person_name(name))
                except Exception as e:
                    # An un-announced request is still pending, so this is not
                    # fatal — but a device nobody was told about is a device
                    # nobody will approve, so say it in the log.
                    self.log_message("device announce failed: %s", str(e)[:90])
            return self._send(200, {"accepted": ok,
                                    "state": "linked" if ok else "pending"})
        if kind == "capabilities":
            return self._send(200, {"capabilities": capabilities()})
        if kind == "progress":
            return self._send(200, progress(account))
        if kind == "qr_spent":
            # The plane says a login QR was just redeemed. The picture in the
            # chat is now a code that already worked, so it goes immediately
            # rather than at the expiry it no longer needs.
            sys.path.insert(0, str(HERE))
            try:
                import qr_send
                gone = qr_send.spend(str(d.get("sha256") or "")[:64])
            except Exception as e:
                return self._send(200, {"ok": True, "deleted": False,
                                        "reason": str(e)[:120]})
            return self._send(200, {"ok": True, "deleted": bool(gone)})
        if kind == "history":
            try:
                limit = min(int(d.get("limit") or 50), 100)
            except (TypeError, ValueError):
                limit = 50
            try:
                since = float(d.get("since") or 0)
            except (TypeError, ValueError):
                since = 0.0
            # `chat` is false when this caller has nowhere for a line to be
            # delivered TO — a guest, or an agent with no chat linked. The app
            # draws its ticks from it: an archived line is not a delivered one.
            rows = history(limit, since)
            # THE WIRE IS THE BOUNDARY, NOT THE DISK (#255, the app side's call).
            # The agent legitimately reads its own conversation — chat, search,
            # every reflex — so the archive stays plaintext. But handing the
            # plane a plaintext history on every sync would give it the whole
            # conversation anyway, and sealing single messages while shipping
            # the transcript in clear is theatre. Each row is re-sealed on the
            # way out, to the key this account has pinned.
            if e2ee_locked(account):
                try:
                    priv, mine = agent_keys()
                    theirs = peer_key(account)
                    for row in rows:
                        if not isinstance(row, dict) or "text" not in row:
                            continue
                        row["sealed"] = (
                            seal_for_devices(str(row.get("text") or ""))
                            or e2ee_seal(str(row.get("text") or ""),
                                         priv, mine, theirs,
                                         direction=DIR_TO_PHONE))
                        row.pop("text", None)
                except Exception as e:
                    # A history that cannot be sealed is not served in the
                    # clear: refuse, and say why. Silent plaintext is the one
                    # outcome the whole scheme exists to prevent.
                    self.log_message("HISTORY SEAL FAILED: %s", e)
                    return self._send(500, {"error": "seal_failed",
                                            "detail": str(e)[:300]})
            return self._send(200, {"messages": rows,
                                    "chat": bool(telegram_chat())
                                            and not is_guest()})
        if kind == "health":
            return self._send(200, health())
        if kind == "branding":
            b = branding()
            if not b:
                # No identity configured. Answering with an empty panel would
                # have the app render blanks; the plane's 404 makes it fall back
                # to its own plain panel, which is the honest look for "unset".
                return self._send(404, {"error": "no branding"})
            return self._send(200, b)
        if kind == "file":
            # Only paths this process minted a token for are servable — the
            # plane asking for a file is not authority to read arbitrary ones.
            tok = str(d.get("token") or "")
            path = MEDIA.get(tok) or _remint(tok)
            if not path or not os.path.exists(path):
                return self._send(404, {"error": "no such token"})
            if os.path.getsize(path) > 25 * 1024 * 1024:
                return self._send(413, {"error": "file too large for relay"})
            with open(path, "rb") as f:
                blob = f.read()
            return self._send(200, {
                "b64": base64.b64encode(blob).decode(),
                "content_type": mimetypes.guess_type(path)[0] or "application/octet-stream",
                "filename": os.path.basename(path)})
        if kind == "log":
            # The app calls this beside every turn to mirror what was SPOKEN.
            # It answered 400 until now — visible in the plane's log as
            # `POST /log 400` on every exchange — so anything the voice said
            # that did not come back through `ask` was never written down.
            #
            # DEDUP IS THE WHOLE JOB. ask() already archives its own answer, so
            # logging the same words again puts the agent's reply on screen
            # twice. Compare with the last row before writing.
            who = str(d.get("who") or "you")
            text = str(d.get("text") or "").strip()
            sealed_log = d.get("sealed")
            if sealed_log:
                # THE MIRROR MOVES HERE (contract #254): the plane forwards the
                # envelope, this end holds both the key and the Telegram
                # credentials, so it opens the line, posts it, and reports the
                # outcome the plane passes through as `mirrored`.
                try:
                    priv, mine = agent_keys()
                    theirs = peer_key(account,
                                      sealed_log.get("pk") or d.get("pk"))
                    if theirs is None:
                        raise ValueError("no device key for this account")
                    text = e2ee_open(sealed_log, priv, mine, theirs,
                                     direction=DIR_TO_AGENT).strip()
                except PeerKeyChanged as e:
                    self.log_message("DEVICE KEY CHANGED for %s", account)
                    return self._send(400, {"error": "device_key_changed",
                                            "detail": str(e)[:300]})
                except Exception as e:
                    self.log_message("SEALED LOG REFUSED: %s", e)
                    return self._send(400, {"error": "sealed_open_failed",
                                            "detail": str(e)[:300]})
            elif (text and e2ee_locked(account) and not d.get("diagnostic")
                  and not _MARKER_ONLY.match(text)):
                self.log_message("PLAINTEXT LOG REFUSED (e2ee declared)")
                return self._send(400, {
                    "error": "e2ee_required",
                    "detail": "this account is sealed — send a `sealed` "
                              "envelope, not a plaintext line"})
            if not text:
                return self._send(200, {"ok": True, "mirrored": False})
            # A DIAGNOSTIC IS NOT A SPOKEN LINE (2026-08-20, #251).
            #
            # The app began sending `[manual-miss q="…" v=…]` through this
            # channel on the assumption that the agent suppresses markers. It
            # did not: every one of them would have been archived as the user's
            # own words and mirrored into his chat, so a bug report about a
            # failed search would arrive looking like a sentence he had said.
            # The assumption was reasonable — the box's own code comment claims
            # markers are "never posted by design" — and it was wrong in both
            # agents, which is the shape of failure that only shows up in
            # somebody else's chat.
            #
            # Two ways to say it, because a build already in the field cannot
            # be recalled: an explicit `diagnostic: true`, or a whole message
            # that is one bracketed marker. Anchored, so a sentence that merely
            # contains a bracket is still a sentence.
            if d.get("diagnostic") or _MARKER_ONLY.match(text):
                self.log_message("DIAGNOSTIC %s", text[:300])
                _check_fabrication(self.log_message, text, account)
                return self._send(200, {
                    "ok": True, "mirrored": False, "suppressed": "diagnostic",
                    "reason": "recorded in the agent's log, not the chat"})
            if _already_archived(text):
                # ALREADY IN THE CHAT IS DELIVERED. 2026-08-19: the agent posts
                # its own answer, the app mirrors the same words a second
                # later, and this refused the copy — correctly — but answered
                # `mirrored: false`, so a line sitting in his Telegram was
                # drawn with no tick. The duplicate is the strongest possible
                # confirmation that the words are there: it is why we refused.
                return self._send(200, {"ok": True, "mirrored": True,
                                        "suppressed": "already_archived",
                                        "reason": "these words are already in "
                                                  "the chat — not sent twice"})
            nm = branding().get("bot_name") or "agent"
            outcome = archive(text, "out" if who != "you" else "in",
                              sender=nm if who != "you" else person_name(name))
            # `ok` means recorded — it is in the conversation the app shows.
            # `mirrored` means it reached the user's OTHER chat, and it is only
            # true when a send actually succeeded. A demo account has no chat
            # behind it by design, so it gets a reason rather than a tick.
            body = {"ok": True, "mirrored": outcome is True}
            if outcome == "queued":
                # `mirrored` stays a BOOLEAN: a string there reads as nil on
                # every build already in the field and degrades silently.
                # `queued` is an unknown key to an old build —
                # ignored, tick absent, reconciled later, which is exactly
                # today's behaviour — and an honest pending state to a new one.
                body["queued"] = True
            if isinstance(outcome, str):
                body["suppressed"] = outcome
                body["reason"] = {
                    "guest_no_chat": "this account has no chat of its own — "
                                     "the line is in the app's conversation",
                    "no_chat": "no chat is linked to this agent",
                    "send_failed": "the chat refused the message",
                    "archived_only": "recorded, not mirrored by design",
                    "duplicate": "the same line was already recorded",
                    "queued": "the send is still in flight — it will appear",
                }.get(outcome, outcome)
            return self._send(200, body)
        if kind == "reset":
            # A new conversation: the next turn opens a fresh session instead
            # of resuming. The transcript stays — this ends a thread, it does
            # not erase one.
            with _lock:
                st = load(STATE, {})
                st.setdefault("sessions", {}).pop(account, None)
                save(STATE, st)
            # THE CHAT IS TOLD, VISIBLY (2026-08-16, an owner's requirement:
            # the clear button must clear the context window, leave a short
            # line in the chat confirming it, and send the clear to the chat
            # the agent is reading). Clearing the agent's thread without
            # saying so in the chat leaves the person reading that chat with a
            # conversation that has silently lost its memory — and the next
            # answer looks like forgetfulness rather than a fresh start.
            # #272: CLEAR IS AN APP ACTION, so the line it writes must not
            # push back at the app that made it. He tapped Clear and got a
            # banner while the app was open — the agent posted "context
            # cleared" to the chat, the notifier saw a new line, and the phone
            # was notified about its own tap. Same quiet window as an answered
            # turn, for the same reason: the app already knows.
            LAST_APP_TURN[account] = time.time()
            archive("[cleared context — new conversation]", "in",
                    sender=person_name(name), mirror=False)
            told = tg_text("🧹 Context cleared from the voice app — fresh "
                           "conversation from here.") if not is_guest() else False
            self.log_message("reset: session cleared for %s%s", account,
                             " (chat told)" if told else "")
            return self._send(200, {"ok": True, "chat_notified": bool(told)})
        if kind == "attachments":
            try:
                since = float(d.get("since") or 0)
            except (TypeError, ValueError):
                since = 0.0
            items = list_attachments(since)
            # #261: the FILE is stage 3, but the CAPTION is the sentence a
            # person typed beside it and the filename is often just as telling
            # (`invoice-<customer>.pdf`). Both are text, and stage 2 claims to
            # seal text. `token`/`ts`/`kind` stay readable because the app
            # needs them to fetch and render; the words go in an envelope.
            #
            # WITHHOLDING WOULD BE WRONG HERE, unlike progress (#260): an
            # attachment can originate on the agent side — a file posted into
            # the chat, an upload that arrived through Telegram — so the app
            # has no local copy to fall back on.
            #
            # OFF BY DEFAULT AND GATED SEPARATELY, because sending an envelope
            # to a build that cannot open it blanks captions on a live phone:
            # exactly the regression this fix exists to avoid. The flag goes on
            # when the app side confirms the opening build is installed.
            if items and config().get("e2ee_attachments") and e2ee_locked(account):
                try:
                    priv, mine = agent_keys()
                    theirs = peer_key(account)
                    for it in items:
                        meta = json.dumps({"filename": it.get("filename", ""),
                                           "caption": it.get("caption", "")})
                        it["meta_sealed"] = e2ee_seal(meta, priv, mine, theirs,
                                                      direction=DIR_TO_PHONE)
                        it.pop("filename", None)
                        it.pop("caption", None)
                except Exception as e:
                    self.log_message("ATTACHMENT META SEAL FAILED: %s", e)
                    return self._send(500, {"error": "seal_failed",
                                            "detail": str(e)[:300]})
            self.log_message("attachments -> %d item(s)", len(items))
            return self._send(200, {"items": items})
        if kind in ("photo", "photos"):
            # ONE PATH FOR BOTH, because the difference is only how many files
            # arrived: a single photo is an album of one, and splitting them
            # gave the box two code paths that drifted.
            raw = d.get("items") if kind == "photos" else [
                {"b64": d.get("b64"), "content_type": d.get("content_type")}]
            saved = []
            for it in (raw or [])[:10]:
                b64 = str((it or {}).get("b64") or "")
                if not b64:
                    continue
                try:
                    blob = base64.b64decode(b64)
                except Exception:
                    continue
                if not blob or len(blob) > UPLOAD_MAX:
                    continue            # empty or over the ceiling: not stored
                saved.append(save_upload(
                    blob, str((it or {}).get("content_type") or "image/jpeg")))
            if not saved:
                return self._send(400, {"error": "no photos"})
            paths = [p for p, _ in saved]
            toks = [t for _, t in saved]
            cap = str(d.get("caption") or "").strip() or None
            # `posted` is the whole contract: TRUE only once the upload is in
            # the archive, which is what makes it show up in history and in the
            # attachments feed. Stored-but-unrecorded is a file nobody can
            # reach, and reporting that as delivered is the exact lie the app
            # spent this afternoon drawing on his screen.
            posted = archive_file(paths, cap, person_name(name))
            self.log_message("%s: %d file(s) %s", kind, len(paths),
                             "archived" if posted else "STORED BUT NOT ARCHIVED")
            answer = None
            if cap:
                # The caption is a real instruction — "Remind me at 5:30 p.m. to
                # analyze this sample" was one, and it died with the upload.
                # It runs as a turn, with the filenames named so the agent can
                # open them.
                try:
                    where = ", ".join(paths)
                    answer = ask(account, cap, name,
                                 archive_question=False,
                                 context=f"The user just sent {len(paths)} "
                                         f"file(s) with this message, saved "
                                         f"at: {where}. Open them if the "
                                         f"message refers to them.").get("answer")
                except Exception as e:
                    self.log_message("caption turn failed: %s", e)
            body = {"ok": True, "posted": posted,
                    "posted_to": "Telegram" if telegram_chat() else "your chat",
                    "count": len(paths),
                    **({"answer": answer} if answer else {})}
            if kind == "photos":
                body["tokens"] = toks
            else:
                body.update(token=toks[0], name=os.path.basename(paths[0]))
            return self._send(200, body)
        if kind == "ask":
            q = str(d.get("question") or "").strip()
            # --- sealed request (contract #254). The envelope REPLACES the
            # plaintext fields; the reply is sealed back the same way.
            sealed_in = d.get("sealed")
            seal_reply = False
            if sealed_in:
                try:
                    priv, mine = agent_keys()
                    theirs = peer_key(account,
                                      sealed_in.get("pk") or d.get("pk"))
                    if theirs is None:
                        raise ValueError(
                            "no device key for this account — the envelope "
                            "must carry `pk` (base64 raw X25519 public key) "
                            "at least once so the agent can derive")
                    q = e2ee_open(sealed_in, priv, mine, theirs,
                                  direction=DIR_TO_AGENT).strip()
                    seal_reply = True
                    # LQ: THE ENVELOPE SAYS WHAT IT IS. A voice turn arrives as
                    # JSON inside the seal — {"voice": {...}, "lang": ...} —
                    # rather than as a question string. Detected from the
                    # PLAINTEXT rather than from a `kind` field beside the
                    # envelope, deliberately: the relay forwards what it is
                    # given and a turn that lied about its kind would be opened
                    # anyway. What is inside decides what this is.
                    vt = _voice_turn(q)
                    if vt is not None:
                        return self._voice_answer(
                            vt, account, name, d, priv, mine, theirs)
                except PeerKeyChanged as e:
                    self.log_message("DEVICE KEY CHANGED for %s", account)
                    return self._send(400, {"error": "device_key_changed",
                                            "detail": str(e)[:300]})
                except Exception as e:
                    # LOUD, never a fallback to plaintext: a message that
                    # cannot be opened has not been received.
                    self.log_message("SEALED ASK REFUSED: %s", e)
                    return self._send(400, {"error": "sealed_open_failed",
                                            "detail": str(e)[:300]})
            elif e2ee_locked(account) and not d.get("diagnostic"):
                # Downgrade rule: once declared, plaintext is refused rather
                # than served. Accepting it "for compatibility" is exactly how
                # stripping the envelope defeats the whole scheme.
                self.log_message("PLAINTEXT ASK REFUSED (e2ee declared)")
                return self._send(400, {
                    "error": "e2ee_required",
                    "detail": "this account is sealed — send a `sealed` "
                              "envelope, not a plaintext question"})
            if not q:
                return self._send(400, {"error": "no question"})
            # 2026-08-13: "what reminders do I have" came back as a bulleted
            # list here and as a grid on the box, because the box answers it
            # WITHOUT a model — a deterministic reflex emits the markdown table
            # the app turns into a tappable grid. A model asked to produce a
            # table produces one shaped however it feels that turn, and the row
            # ids that make a row openable cannot survive that. So the same
            # reflex answers here, from this machine's own reminder store.
            # A CONFIRMATION MUST COME FROM THE STORE. An amend that falls
            # through to the model gets answered from the REQUEST — "now
            # set for 7:30 PM" while nothing moved — so the store is
            # photographed first and the claim checked against it below.
            before = (reminders_snapshot()
                      if _AMEND_SHAPE.search(q) and not _ELSEWHERE.search(q)
                      else None)
            quick = reflex_answer(q, tz=str(d.get("tz") or "") or None)
            if quick:
                self.log_message("reflex answered: %.40s", q)
                if d.get("archive") is not False:
                    archive(q, "in", sender=person_name(name))
                    archive(quick, "out",
                            sender=branding().get("bot_name") or "agent",
                            mirror=False)   # the table is for the app's grid
                return self._send(200, {"answer": quick})
            self.log_message("ask from %s: %.60s", name or account, q)
            t0 = time.time()
            # {"archive": false} — a lookup the app makes on the user's
            # behalf. Answer it and leave no trace: no row, no mirror, nothing
            # for history restore to replay.
            keep = d.get("archive")
            # A demonstrative about a picture is resolved before the turn, so
            # the model is told WHICH image rather than picking the one it
            # happens to remember.
            res = ask(account, q, name, archive_turn=(keep is not False),
                      context="\n\n".join(
                          c for c in (picture_context(q),
                                      app_setting_context(q),
                                      app_doc_context(q),
                                      time_context(d.get("tz"))) if c))
            self.log_message("answered in %.1fs (%s)", time.time() - t0,
                             res.get("agent_error") or "ok")
            # GROUND TRUTH FOR THE FABRICATION INVARIANT. The figures in what
            # this agent actually said are kept so a later turn that speaks
            # different ones can be told apart from a later turn that rounds
            # them. Numbers only, never the answer body.
            try:
                import fabrication
                fabrication.record_answer(account, q,
                                          str(res.get("answer") or ""))
            except Exception as e:
                self.log_message("figure ledger skipped: %.80s", e)
            # The phone has this answer in its hand; the notifier must not
            # announce the same exchange as news a minute later (#270).
            LAST_APP_TURN[account] = time.time()
            if before is not None and reminders_snapshot() == before:
                ans = str(res.get("answer") or "")
                if _CLAIMS_DONE.search(ans):
                    self.log_message("BLOCKED a false confirmation: "
                                     "nothing in the reminder store changed")
                    res["answer"] = (
                        "I could not change that reminder — nothing in the "
                        "store moved, so it is still set as it was. Say the "
                        "new time again and I will try once more.")
            # #370: THE BANNER FOR THE FAMILY HE ACTUALLY SEES. The plane
            # fires the answer push and cannot read anything; the preview has
            # to be sealed here and carried through. A separate key from
            # `sealed`, because that one is the whole reply and this one is two
            # lines for a lock screen — and because the app is specified to
            # distrust readable text beside an envelope, so it may not ride
            # inside it.
            try:
                _prev = push_preview_envelope(account,
                                              str(res.get("answer") or ""))
                if _prev:
                    res["push_preview"] = _prev
            except Exception as e:
                self.log_message("push preview skipped: %.80s", e)
            if seal_reply:
                # Sealed in, sealed out — and the plaintext `answer` is
                # REMOVED, not left beside it. The app is specified to ignore
                # readable text next to an envelope precisely because only the
                # plane could have written it; sending both would make that
                # rule fire on our own reply.
                try:
                    priv, mine = agent_keys()
                    theirs = peer_key(account)
                    out = dict(res)
                    _multi = seal_for_devices(str(res.get("answer") or ""))
                    if _multi:
                        self.log_message("sealed v2 to %d devices",
                                         len(_multi.get("wraps") or []))
                    out["sealed"] = _multi or e2ee_seal(
                        str(res.get("answer") or ""), priv, mine, theirs,
                        direction=DIR_TO_PHONE)
                    out.pop("answer", None)
                    return self._send(200, out)
                except Exception as e:
                    self.log_message("SEALING THE REPLY FAILED: %s", e)
                    return self._send(500, {"error": "seal_failed",
                                            "detail": str(e)[:300]})
            return self._send(200, res)

        # Everything else: the plane treats HTTP 400 as "ask-only agent".
        self._send(400, {"error": f"unsupported type: {kind}"})



# ---------------------------------------------------- new-message notifier
# An owner asked for a push when a message arrives in the chat while the app is
# not in front of them (2026-08-18), OFF unless they turn it on. The app owns
# the switch and the foreground case; this owns "something arrived".
#
# It watches the ARCHIVE rather than hooking a send path, because a message can
# reach that chat from several directions — somebody typing in the chat, a
# reminder firing, a scheduled job posting — and the archive is the one place
# all of them land. What it must NOT do is announce the person's own words back
# to them, so lines the app itself just produced are skipped.
NOTIFY_POLL_S = 20
NOTIFY_KIND = "message"


# The app already knows about a turn it just made (#270): it has the answer in
# its hand and drew it on screen. A "new message" push for the same exchange is
# the second of two banners for one event, and the person cannot tell which is
# which. The plane dedupes as a backstop; this stops the second sender.
LAST_APP_TURN = {}
APP_TURN_QUIET_S = 120


def _notify_plane(account, kind=NOTIFY_KIND, **extra):
    """Ask the plane to nudge this account's phones. Authenticated with this
    agent's own secret, which the plane scopes to this account alone."""
    try:
        cfg = config()
        body = json.dumps({"kind": kind, "account": account, **extra}).encode()
        rq = urllib.request.Request(
            (os.environ.get("VOICE_PLANE", "https://app.agentvoicemode.ai")
             + "/api/notify"),
            data=body,
            headers={"Authorization": f"Bearer {cfg['secret']}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(rq, timeout=15) as rp:
            return json.loads(rp.read() or b"{}")
    except Exception as e:
        return {"error": str(e)[:120]}


def _message_watcher():
    """Poll the archive; nudge the phone when a line it has not seen appears."""
    d = archive_dir()
    accounts = owner_accounts()
    if not d or not accounts:
        return
    st = load(STATE, {})
    last = float(st.get("notify_seen_epoch") or 0)
    if not last:                      # first run: start from now, never a flood
        last = time.time()
    while True:
        time.sleep(NOTIFY_POLL_S)
        try:
            import sqlite3
            cx = sqlite3.connect(f"file:{d / 'chat.db'}?mode=ro", uri=True,
                                 timeout=3)
            rows = cx.execute(
                # `text` joined the projection for #270's sealed preview. It
                # is read here and sealed to the phone's key before it goes
                # anywhere — the plane still never sees a word of it.
                "SELECT epoch, kind, direction, text FROM messages WHERE epoch > ? "
                "ORDER BY epoch", (last,)).fetchall()
            cx.close()
        except Exception:
            continue
        if not rows:
            continue
        newest = max(r[0] for r in rows)
        # The app's own voice lines are already on his screen; announcing them
        # would be the notification equivalent of the duplicate bubble.
        worth = [r for r in rows if (r[1] or "") != "voice"]
        last = newest
        st = load(STATE, {})
        st["notify_seen_epoch"] = newest
        save(STATE, st)
        if not worth:
            continue
        for acct in accounts:
            quiet = time.time() - LAST_APP_TURN.get(acct, 0)
            if quiet < APP_TURN_QUIET_S:
                print(f"[voice-agent] new-message push skipped for {acct}: "
                      f"an app turn finished {quiet:.0f}s ago — the phone "
                      f"already has these words", file=sys.stderr)
                continue
            # #270 stage 4: the preview travels SEALED. The plane cannot read
            # it, the notification extension on the phone can, and a phone that
            # fails to decrypt keeps the generic wording. Capped, because a
            # banner shows two lines and a whole answer in a push is a copy of
            # the conversation living in Apple's queue.
            env = None
            try:
                newest_text = str((worth[-1][3] if len(worth[-1]) > 3
                                   else "") or "")
                env = push_preview_envelope(acct, newest_text)
            except Exception as e:
                print(f"[voice-agent] preview seal failed for {acct}: {e}",
                      file=sys.stderr)
                env = None
            res = _notify_plane(acct, count=len(worth),
                                **({"sealed": env} if env else {}))
            print(f"[voice-agent] new-message push for {acct}: "
                  f"{len(worth)} line(s) -> {res}", file=sys.stderr)


# ------------------------------------------------------ the app's own docs
# 2026-08-19, an owner: "our agents, wherever they are, should have access to
# this app's documentation and release notes. So if a user asks the agent
# anything about this app, the agent should be able to answer it."
#
# What prompted it: he asked how the keyboard behaves after sending, the
# question reached the agent, and the agent said — correctly and uselessly —
# that it could not see the app's UI. An agent that cannot answer a question
# about the thing it is speaking through is a gap the user experiences as
# ignorance, not as a boundary.
#
# So the manual and the release notes are kept ON DISK beside the agent's own
# knowledge, refreshed in the background. A file the agent can read beats a
# fetch it has to remember to make, and it keeps working when the plane does
# not.
DOCS_REFRESH_S = 900        # a version check, not a download
DOCS_DIRNAME = "agent-voice-mode"


def _docs_dir():
    base = os.path.expanduser(config()["workdir"])
    kb = os.path.join(base, "knowledge-base")
    root = kb if os.path.isdir(kb) else base
    d = os.path.join(root, DOCS_DIRNAME)
    os.makedirs(d, exist_ok=True)
    return d


def _plane(path):
    url = (os.environ.get("VOICE_PLANE", "https://app.agentvoicemode.ai")
           + "/api" + path)
    with urllib.request.urlopen(url, timeout=20) as rp:
        return rp.read()


def sync_app_docs(force=False):
    """Manual + release notes to disk. Returns what changed.

    The manual is fetched only when its VERSION changes: `/api/manual/version`
    returns an opaque string to compare for equality, so the usual case costs a
    few hundred bytes instead of a document. A stale copy is worse than none —
    an agent confidently describing last week's app is a wrong answer nobody
    can tell from a right one — so the version is what the cache is keyed on,
    never a timer.
    """
    changed = []
    d = _docs_dir()
    stamp = os.path.join(d, ".manual-version")
    try:
        seen = open(stamp).read().strip()
    except OSError:
        seen = ""
    try:
        now_v = str(json.loads(_plane("/manual/version") or b"{}")
                    .get("version") or "")
    except Exception:
        now_v = ""
    # ONE FLAG ANSWERED TWO QUESTIONS (2026-08-20, #253). This early return
    # used to be `return []` — and it stood in front of the RELEASE NOTES
    # fetch as well as the manual's. So release notes were only ever refreshed
    # in a cycle where the manual happened to change too: two builds shipped
    # this morning and the agent's copy stayed on the older one, while the
    # sync reported "nothing to fetch" and looked healthy doing it. The
    # manual's version says nothing about the release notes; it is not their
    # cache key and never was.
    fresh = (now_v and now_v == seen and not force
             and os.path.exists(os.path.join(d, "manual.md")))
    try:
        man = None if fresh else _plane("/manual")
        p = os.path.join(d, "manual.md")
        if man is not None and (not os.path.exists(p)
                                or open(p, "rb").read() != man):
            with open(p, "wb") as f:
                f.write(man)
            changed.append("manual.md")
        if now_v and man is not None:
            with open(stamp, "w") as f:
                f.write(now_v)
    except Exception as e:
        print(f"[voice-agent] manual sync failed: {e}", file=sys.stderr)
    try:
        rel = json.loads(_plane("/releases") or b"{}").get("builds", {})
        lines = ["# Agent Voice Mode — release notes",
                 "",
                 "Newest first. Each heading is the build number the app "
                 "reports in Settings.", ""]
        for b in sorted(rel, key=lambda x: int(x), reverse=True):
            lines += [f"## Build {b}", "", str(rel[b]).strip(), ""]
        body = "\n".join(lines).encode()
        p = os.path.join(d, "release-notes.md")
        if not os.path.exists(p) or open(p, "rb").read() != body:
            with open(p, "wb") as f:
                f.write(body)
            changed.append("release-notes.md")
    except Exception as e:
        print(f"[voice-agent] release-note sync failed: {e}", file=sys.stderr)
    if changed:
        print(f"[voice-agent] app docs updated: {', '.join(changed)} in {d}",
              file=sys.stderr)
    return changed


def _docs_worker():
    while True:
        try:
            sync_app_docs()
        except Exception:
            pass
        time.sleep(DOCS_REFRESH_S)

def _identity_worker():
    """Keep the panel current in the background. It costs a model turn, so it
    never runs while the plane is waiting on a request — the panel is always
    served from cache, and this is what fills the cache."""
    ident = ensure_identity()
    if ident:
        print(f"[voice-agent] identity: {ident.get('agent_name') or '?'} "
              f"at {ident.get('company_name') or '?'} "
              f"for {ident.get('user_name') or '?'}"
              + (f", logo {os.path.basename(ident['logo'])}" if ident.get("logo") else ""),
              flush=True)
    else:
        print("[voice-agent] identity: could not derive one — the app will show its "
              "generic panel. Set agent_name/company_name/user_name in config.json "
              "to override.", flush=True)


def _qr_sweeper():
    """Take posted QR codes back out of the chat when they expire.

    The adapter is the one thing on this machine that is definitely still running
    a quarter of an hour after an install — the installing agent has moved on and
    pair.py exited long ago. So the deletion belongs here, not in whatever posted
    the image."""
    sys.path.insert(0, str(HERE))
    try:
        import qr_send
    except ImportError:
        return
    while True:
        try:
            qr_send.sweep()
        except Exception as e:
            print(f"[voice-agent] qr sweep failed: {e}", file=sys.stderr, flush=True)
        time.sleep(30)


def serve():
    cfg = config()
    srv = ThreadingHTTPServer((cfg["bind"], int(cfg["port"])), Handler)
    print(f"[voice-agent] listening on {cfg['bind']}:{cfg['port']}  "
          f"workdir={os.path.expanduser(cfg['workdir'])}", flush=True)
    h = health()
    if not h["ok"]:
        print(f"[voice-agent] WARNING: {h.get('detail')}", flush=True)
    threading.Thread(target=_identity_worker, daemon=True).start()
    threading.Thread(target=_message_watcher, daemon=True).start()
    threading.Thread(target=_docs_worker, daemon=True).start()
    threading.Thread(target=_qr_sweeper, daemon=True).start()
    srv.serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="print health and exit")
    ap.add_argument("--identity", action="store_true",
                    help="derive the identity panel now and print it")
    # The safety number, from the machine the agent actually runs on. The app
    # tells people this is the strongest check available to them, and a check
    # that requires decoding base64 and hashing it by hand is a check nobody
    # performs twice (2026-08-19).
    ap.add_argument("--fingerprint", action="store_true",
                    help="print this agent's encryption fingerprint and exit")
    ap.add_argument("--pins", action="store_true",
                    help="list the device keys pinned to this agent")
    ap.add_argument("--unpin", metavar="ACCOUNT",
                    help="forget an account's pinned device key, so a new "
                         "phone can pair (use after a restore that lost the "
                         "old key)")
    a = ap.parse_args()
    if a.check:
        print(json.dumps({**health(), "claude": claude_bin(),
                          "workdir": os.path.expanduser(config()["workdir"]),
                          "branding": branding()}, indent=2))
    elif a.fingerprint:
        try:
            _priv, _pub = agent_keys()
            print(key_fingerprint(_pub))
            print(f"\npublic key : {public_key_b64()}", file=sys.stderr)
            print(f"retired    : {len(retired_keys())} older key(s) kept for "
                  f"reading old messages", file=sys.stderr)
            print("Read the twenty digits aloud and compare them with the app. "
                  "They must match exactly.", file=sys.stderr)
        except Exception as e:
            print(f"no encryption key on this agent: {e}", file=sys.stderr)
            raise SystemExit(1)
    elif a.pins:
        import base64 as _b64
        pins = load(_peers_path(), {})
        if not pins:
            print("no device keys pinned")
        for acct, k in pins.items():
            try:
                fp = key_fingerprint(_b64.b64decode(k))
            except Exception:
                fp = "(unreadable)"
            print(f"{acct}\n  {fp}")
    elif a.unpin:
        if unpin_peer(a.unpin):
            print(f"unpinned {a.unpin} — the next sealed message from that "
                  f"account pins a new device key")
        else:
            print(f"{a.unpin} had no pinned device key")
    elif a.identity:
        print(json.dumps(ensure_identity(force=True), indent=2))
    else:
        serve()
