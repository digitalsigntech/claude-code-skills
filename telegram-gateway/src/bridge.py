"""Bridge to headless Claude — one persistent session per Telegram chat.

Each Telegram chat (a DM or a dedicated group) maps to its own Claude session UUID,
so separate groups keep separate, rolling conversations. First message in a chat
creates the session (--session-id); subsequent messages resume it (--resume).
Calls within one chat are serialized; different chats run concurrently.

ask_stream() runs Claude with --output-format stream-json and surfaces incremental
text + tool activity via a callback, so Telegram can show the reply building live
instead of going silent for the whole (often 30-40s) agentic turn.
"""
import os, json, uuid, subprocess, threading
import tgconf as C

_locks_guard = threading.Lock()
_locks = {}
_sess_guard = threading.Lock()
_sessions = None


def _load():
    global _sessions
    if _sessions is None:
        try:
            _sessions = json.load(open(C.SESSIONS_FILE))
        except Exception:
            _sessions = {}
    return _sessions


def _save():
    tmp = C.SESSIONS_FILE + ".tmp"
    json.dump(_sessions, open(tmp, "w"))
    os.replace(tmp, C.SESSIONS_FILE)


def _chat_lock(chat_id):
    with _locks_guard:
        return _locks.setdefault(str(chat_id), threading.Lock())


def reset(chat_id):
    """Forget this chat's session so the next message starts a fresh Claude conversation."""
    with _sess_guard:
        _load().pop(str(chat_id), None)
        _save()


def hold_file(chat_id, path):
    with _sess_guard:
        s = _load()
        ent = s.setdefault(str(chat_id), {"sid": str(uuid.uuid4()), "init": False, "held": []})
        ent.setdefault("held", []).append(path)
        _save()


def _take_held(chat_id):
    with _sess_guard:
        ent = _load().get(str(chat_id))
        if not ent or not ent.get("held"):
            return []
        held = ent["held"]; ent["held"] = []; _save()
        return held


def sid_for(chat_id):
    """Current session UUID for a chat, or None — read-only (never creates one).
    Used by the chat archive to tag each message with its Claude session."""
    ent = _load().get(str(chat_id))
    return ent.get("sid") if ent else None


def title_for(chat_id):
    ent = _load().get(str(chat_id)) or {}
    return ent.get("title")


def _resolve(chat_id):
    with _sess_guard:
        s = _load()
        ent = s.get(str(chat_id))
        if not ent:
            ent = {"sid": str(uuid.uuid4()), "init": False, "held": []}
            s[str(chat_id)] = ent; _save()
        return ent["sid"], ent.get("init", False)


def _mark_inited(chat_id, sid):
    with _sess_guard:
        ent = _load().setdefault(str(chat_id), {"sid": sid, "init": False, "held": []})
        ent["sid"] = sid; ent["init"] = True
        _save()


def set_chat_meta(chat_id, title, ctype):
    """Record the Telegram chat's display title + type so Claude knows which room
    it's in. Called on every inbound message (titles can change)."""
    if not title and ctype == "private":
        return
    with _sess_guard:
        ent = _load().setdefault(str(chat_id), {"sid": str(uuid.uuid4()), "init": False, "held": []})
        if ent.get("title") == title and ent.get("ctype") == ctype:
            return
        ent["title"] = title; ent["ctype"] = ctype
        _save()


def _chat_context(chat_id, sender=None):
    ent = _load().get(str(chat_id)) or {}
    title, ctype = ent.get("title"), ent.get("ctype")
    frm = f" This message is from: {sender}." if sender else ""
    if title:
        return f'[You are in the Telegram {ctype or "group"} "{title}" (chat_id {chat_id}).{frm}]'
    if sender:
        return f'[Telegram chat {chat_id}.{frm}]'
    return None


def _augment(chat_id, prompt, sender=None):
    parts = []
    ctx = _chat_context(chat_id, sender)
    if ctx:
        parts.append(ctx)
    held = _take_held(chat_id)
    if held:
        listed = "\n".join(f"  - {p}" for p in held)
        parts.append(f"[Files the user sent in this chat are saved on disk:\n{listed}\n"
                     f"Read them if relevant to the message below.]")
    if parts:
        return "\n".join(parts) + f"\n\n{prompt}"
    return prompt


def _base_cmd(prompt):
    cmd = [C.CLAUDE_BIN, "-p", prompt, "--model", C.CLAUDE_MODEL, "--dangerously-skip-permissions"]
    if C.APPEND_SYSTEM:
        cmd += ["--append-system-prompt", C.APPEND_SYSTEM]
    return cmd


def _sess_args(sid, inited):
    return ["--resume", sid] if inited else ["--session-id", sid]


# ---- non-streaming (used for file analysis) ---------------------------------
def _run(cmd):
    try:
        r = subprocess.run(cmd, cwd=C.CLAUDE_WORKDIR, capture_output=True,
                           text=True, timeout=C.CLAUDE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, "⏳ That took too long and timed out. Try again or narrow it down."
    if r.returncode != 0:
        return None, (r.stderr or r.stdout or "").strip()[:500]
    try:
        d = json.loads(r.stdout)
    except Exception:
        return None, (r.stdout or "").strip()[:500] or "Could not parse Claude output."
    if d.get("is_error"):
        return None, str(d.get("result") or d.get("error") or "Claude reported an error.")[:1000]
    return (d.get("result") or "").strip(), None


def ask(chat_id, prompt, sender=None):
    prompt = _augment(chat_id, prompt, sender)
    with _chat_lock(chat_id):
        sid, inited = _resolve(chat_id)
        text, err = _run(_base_cmd(prompt) + ["--output-format", "json"] + _sess_args(sid, inited))
        if err is not None and not inited and "already in use" in err:
            # Session exists on disk but init was never recorded (first turn died after
            # the CLI created it). Resume it instead of re-creating.
            text, err = _run(_base_cmd(prompt) + ["--output-format", "json", "--resume", sid])
        if err is not None:
            sid = str(uuid.uuid4())
            text, err = _run(_base_cmd(prompt) + ["--output-format", "json", "--session-id", sid])
        if err is not None:
            return f"⚠️ Claude error: {err}"
        _mark_inited(chat_id, sid)
        return text or "(Claude returned an empty reply.)"


# ---- streaming (used for chat) ----------------------------------------------
def _stream_run(cmd, on_event):
    """Run a stream-json Claude turn. on_event('text', full_text_so_far) on each text
    delta; on_event('tool', tool_name) when a tool starts. Returns (final_text, err)."""
    try:
        proc = subprocess.Popen(cmd, cwd=C.CLAUDE_WORKDIR, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1)
    except Exception as e:
        return None, str(e)
    flag = {"timeout": False}

    def _kill():
        flag["timeout"] = True
        proc.kill()

    killer = threading.Timer(C.CLAUDE_TIMEOUT, _kill)
    killer.start()
    buf, final, err = [], None, None
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            t = e.get("type")
            if t == "stream_event":
                ev = e.get("event", {})
                et = ev.get("type")
                if et == "content_block_delta":
                    d = ev.get("delta", {})
                    if d.get("type") == "text_delta":
                        buf.append(d.get("text", "")); on_event("text", "".join(buf))
                elif et == "content_block_start":
                    cb = ev.get("content_block", {})
                    if cb.get("type") == "tool_use":
                        on_event("tool", cb.get("name", "tool"))
                    elif cb.get("type") == "text" and "".join(buf).strip():
                        # separate a new text block from earlier text (e.g. text
                        # before a tool call + text after it) so they don't mash.
                        buf.append("\n\n")
            elif t == "result":
                if e.get("is_error"):
                    err = str(e.get("result") or e.get("error") or "error")[:1000]
                else:
                    final = (e.get("result") or "").strip()
    finally:
        killer.cancel()
        proc.wait()
    if flag["timeout"]:
        return None, "⏳ timed out"
    if final is None and err is None:
        err = (proc.stderr.read() or "").strip()[:500] or f"exit {proc.returncode}"
    # Deliver the full streamed transcript (all text blocks), not just the result
    # field — which is only the LAST block, so earlier narration would vanish from
    # the bubble when it's replaced at the end.
    full = "".join(buf).strip()
    return (full or final), err


def ask_stream(chat_id, prompt, on_event, sender=None):
    prompt = _augment(chat_id, prompt, sender)
    flags = ["--output-format", "stream-json", "--include-partial-messages", "--verbose"]
    with _chat_lock(chat_id):
        sid, inited = _resolve(chat_id)
        text, err = _stream_run(_base_cmd(prompt) + flags + _sess_args(sid, inited), on_event)
        if err is not None and not inited and "already in use" in err:
            # Session exists on disk but init was never recorded (first turn died after
            # the CLI created it). Resume it instead of re-creating.
            text, err = _stream_run(_base_cmd(prompt) + flags + ["--resume", sid], on_event)
        if err is not None and "timed out" not in err:
            sid = str(uuid.uuid4())
            text, err = _stream_run(_base_cmd(prompt) + flags + ["--session-id", sid], on_event)
        if err is not None:
            return f"⚠️ Claude error: {err}"
        _mark_inited(chat_id, sid)
        return text or "(Claude returned an empty reply.)"
