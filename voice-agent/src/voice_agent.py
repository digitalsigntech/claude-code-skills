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


def progress():
    """What this agent is working on, answered without a model.

    The app probes this to decide whether an agent is there — a 400 here is read
    as unreachable and the connect button is drawn crossed out, on a machine that
    is answering questions perfectly well. So an adapter that cannot report work
    must still report NO work rather than refuse the question.

    `covers_all_origins` is false and stays false: this sees turns that arrived
    through the app, and nothing about a cron job or a terminal session on the
    same machine. Claiming otherwise would have the voice say "nothing is
    running" while something is."""
    with _inflight_lock:
        tasks = [{"id": tid, "question": t["question"][:120], "state": "running",
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
        try:
            db.record(text[:4000], direction,
                      sender=sender, chat_id=chat or 0,
                      chat_title=_chat_title(), kind="text")
        except Exception:
            pass
    if mirror and text:
        tg_text(text, who=(sender if direction == "in" else None))


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
    if telegram_chat():
        for i, p in enumerate(paths):
            sent = tg_file(p, caption if i == 0 else None) and sent
    try:
        db.record(text[:4000] + ("" if sent else " [NOT delivered to the chat]"),
                  "in", sender=sender or "you", chat_id=telegram_chat() or 0,
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


def _already_archived(text, within=180):
    """Has this line just been written? Used only to stop a double entry.

    Compared on the first 200 characters: the voice model's `log` call and the
    answer `ask` archived are the same sentence, but one of them can arrive
    truncated or with trailing punctuation from the speech pipeline."""
    d = archive_dir()
    if not d or not text:
        return False
    try:
        import sqlite3
        cx = sqlite3.connect(f"file:{d / 'chat.db'}?mode=ro", uri=True, timeout=3)
        rows = cx.execute("SELECT text FROM messages WHERE epoch > ? "
                          "ORDER BY epoch DESC LIMIT 4",
                          (time.time() - within,)).fetchall()
        cx.close()
    except Exception:
        return False
    head = " ".join(text.split())[:200]
    return any(" ".join(str(r[0] or "").split())[:200] == head for r in rows)


def _archive_history(limit, since):
    """The tail of the archive, newest-last, across every chat it holds."""
    db = _chatdb()
    if not db:
        return None
    d = archive_dir()
    try:
        import sqlite3
        cx = sqlite3.connect(f"file:{d / 'chat.db'}?mode=ro", uri=True, timeout=3)
        rows = cx.execute(
            "SELECT epoch, sender, text, direction FROM messages "
            "WHERE epoch > ? ORDER BY epoch DESC LIMIT ?",
            (since, limit)).fetchall()
        cx.close()
    except Exception:
        return None
    name = branding().get("bot_name") or "agent"
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
             "text": _strip_injected_prefix(str(text))[:2000],
             "ts": float(ep)}
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
                         "text": text[:2000], "ts": ts})
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


def ask(account, question, account_name="", archive_question=True):
    """`archive_question=False` for a turn whose words are already recorded.

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
    prompt = question
    if account_name:
        prompt = f"[Voice turn from {account_name}]\n\n{question}"

    cmd = [exe, "-p", prompt, "--dangerously-skip-permissions"]
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
        archive(question, "in", sender=account_name or "you")

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
        return ask(account, question, account_name)

    remember_session(account, sid)
    _finish_turn(turn_id)
    archive(out, "out", sender=branding().get("bot_name") or "agent")
    return {"answer": out[:8000]}


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
    established by being used. "You are Max" appears in no file on Max's machine,
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
    for day in sorted(os.listdir(root), reverse=True) if os.path.isdir(root) else []:
        dd = os.path.join(root, day)
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


def capabilities():
    """Declared from what this install can actually answer. `branding` is claimed
    whenever a panel exists — including one derived a moment ago, which is why the
    derivation runs before pair.py registers rather than after."""
    # `progress` is unconditional: it costs nothing, and the app treats its absence
    # as an agent that is not there.
    caps = ["ask", "health", "progress", "history"]
    b = branding()
    if b:
        caps.append("branding")
    # `file` used to be claimed only when a logo existed, which was true when
    # the only servable file WAS the logo. Uploads are servable by the same
    # route, so it is now unconditional — and the plane checks this list before
    # relaying, so an unclaimed capability is a feature that silently is not
    # there rather than one that fails loudly.
    caps.append("file")
    # Claimed only if there is somewhere to record an upload. Without an
    # archive a photo could be stored but never appear in the conversation,
    # and the honest answer to "can you take a photo" is then no.
    if _chatdb():
        caps += ["photo", "photos", "attachments", "log", "reset"]
    return caps


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
        name = str(d.get("account_name") or "")

        if kind == "capabilities":
            return self._send(200, {"capabilities": capabilities()})
        if kind == "progress":
            return self._send(200, progress())
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
            return self._send(200, {"messages": history(limit, since)})
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
            if not text:
                return self._send(200, {"ok": True, "mirrored": False})
            if _already_archived(text):
                return self._send(200, {"ok": True, "mirrored": False,
                                        "suppressed": "already_archived"})
            nm = branding().get("bot_name") or "agent"
            archive(text, "out" if who != "you" else "in",
                    sender=nm if who != "you" else (name or "you"))
            return self._send(200, {"ok": True, "mirrored": True})
        if kind == "reset":
            # A new conversation: the next turn opens a fresh session instead
            # of resuming. The transcript stays — this ends a thread, it does
            # not erase one.
            with _lock:
                st = load(STATE, {})
                st.setdefault("sessions", {}).pop(account, None)
                save(STATE, st)
            archive("[cleared context — new conversation]", "in",
                    sender=name or "you")
            self.log_message("reset: session cleared for %s", account)
            return self._send(200, {"ok": True})
        if kind == "attachments":
            try:
                since = float(d.get("since") or 0)
            except (TypeError, ValueError):
                since = 0.0
            items = list_attachments(since)
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
            posted = archive_file(paths, cap, name or "you")
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
                    answer = ask(account, f"{cap}\n\n[The user just sent "
                                          f"{len(paths)} file(s), saved at: "
                                          f"{where}]", name,
                                 archive_question=False).get("answer")
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
            if not q:
                return self._send(400, {"error": "no question"})
            # 2026-08-13: "what reminders do I have" came back as a bulleted
            # list here and as a grid on the box, because the box answers it
            # WITHOUT a model — a deterministic reflex emits the markdown table
            # the app turns into a tappable grid. A model asked to produce a
            # table produces one shaped however it feels that turn, and the row
            # ids that make a row openable cannot survive that. So the same
            # reflex answers here, from this machine's own reminder store.
            quick = reflex_answer(q)
            if quick:
                self.log_message("reflex answered: %.40s", q)
                archive(q, "in", sender=name or "you")
                archive(quick, "out",
                        sender=branding().get("bot_name") or "agent",
                        mirror=False)     # the table is for the app's grid
                return self._send(200, {"answer": quick})
            self.log_message("ask from %s: %.60s", name or account, q)
            t0 = time.time()
            res = ask(account, q, name)
            self.log_message("answered in %.1fs (%s)", time.time() - t0,
                             res.get("agent_error") or "ok")
            return self._send(200, res)

        # Everything else: the plane treats HTTP 400 as "ask-only agent".
        self._send(400, {"error": f"unsupported type: {kind}"})


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
    threading.Thread(target=_qr_sweeper, daemon=True).start()
    srv.serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="print health and exit")
    ap.add_argument("--identity", action="store_true",
                    help="derive the identity panel now and print it")
    a = ap.parse_args()
    if a.check:
        print(json.dumps({**health(), "claude": claude_bin(),
                          "workdir": os.path.expanduser(config()["workdir"]),
                          "branding": branding()}, indent=2))
    elif a.identity:
        print(json.dumps(ensure_identity(force=True), indent=2))
    else:
        serve()
