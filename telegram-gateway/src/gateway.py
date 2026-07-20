"""DST Telegram gateway — chat with Claude over Telegram, send files, use groups.

Long-polls Telegram; every text message runs a real Claude turn (full tools, DST
workspace) with one persistent session per chat. Files are saved to inbox/ and the
bot offers: Ingest to KB / Analyze / Hold for this chat. Locked to an allowlist of
Telegram user IDs. Run via start_telegram.sh (single-instance, @reboot).
"""
import os, sys, time, json, re, uuid, datetime, threading, subprocess, traceback, tempfile
from concurrent.futures import ThreadPoolExecutor
from email.utils import parseaddr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tgconf as C
import tg_api as TG
import bridge
import privacy_router
import photo_reflex
import doc_reflex
import file_reflex
import personal_notes
import voice_mode
import qa_cache
import projects_mode   # R&D project chats — see ../../projects/ skill

# Searchable chat archive (every message + reply -> SQLite/FTS5). Best-effort: if the
# module can't load, archiving silently no-ops and the gateway runs unaffected.
sys.path.insert(0, os.path.join(C.DST_ROOT, "chatlog"))
try:
    import chatdb
except Exception:
    chatdb = None


def _who(msg):
    f = msg.get("from", {}) or {}
    return f.get("username") or f.get("first_name") or str(f.get("id") or "user")


def _sender_first(msg):
    """Sender's first name for the on-box private agent."""
    f = msg.get("from", {}) or {}
    return f.get("first_name") or f.get("username") or "the owner"


def _sender_full(msg):
    """Full sender identity for Claude's context line: 'First Last (@username, id N)'."""
    f = msg.get("from", {}) or {}
    name = " ".join(x for x in (f.get("first_name"), f.get("last_name")) if x)
    extras = ", ".join(x for x in (f"@{f['username']}" if f.get("username") else None,
                                   f"id {f['id']}" if f.get("id") else None) if x)
    if name and extras:
        return f"{name} ({extras})"
    return name or extras or "unknown"


def _room(msg, chat_id):
    ch = msg.get("chat", {}) or {}
    return (ch.get("title") or ch.get("first_name")
            or ("private" if ch.get("type") == "private" else str(chat_id)))


def _arc_in(msg, chat_id, text, kind="text"):
    if not chatdb:
        return
    chatdb.record(text, "in", sender=_who(msg), chat_id=chat_id,
                  chat_title=_room(msg, chat_id), kind=kind,
                  session_id=bridge.sid_for(chat_id))


def _current_topic(chat_id):
    """The project this chat is currently about: the most recent classified message in
    this chat since its last /clear. Returns a slug, or None if nothing is tagged yet
    (a fresh chat, or the last message or two are still within the ~1-2s tagging lag)."""
    if not chatdb:
        return None
    try:
        with chatdb._lock:
            c = chatdb._get()
            reset_id = c.execute(
                "SELECT max(id) FROM messages WHERE chat_id=? AND kind='command' AND "
                "(text LIKE '/clear%' OR text LIKE '/new%' OR text LIKE '/reset%')",
                (chat_id,)).fetchone()[0] or 0
            row = c.execute(
                "SELECT project FROM messages WHERE chat_id=? AND id>? "
                "AND project IS NOT NULL ORDER BY id DESC LIMIT 1",
                (chat_id, reset_id)).fetchone()
            return row[0] if row else None
    except Exception:
        return None


def _arc_out(chat_id, text, kind="text", sender="claude", title=None):
    if not chatdb:
        return
    chatdb.record(text, "out", sender=sender, chat_id=chat_id,
                  chat_title=title or bridge.title_for(chat_id), kind=kind,
                  session_id=bridge.sid_for(chat_id))

POOL = ThreadPoolExecutor(max_workers=4)
PENDING = {}            # file_key -> {path, caption, chat_id}
PENDING_GUARD = threading.Lock()
INJECT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inject")  # email→chat queue
EMAIL_DIR = os.path.join(C.DST_ROOT, "email")          # for emailing replies back (claude@)
EMAIL_PY = os.path.join(EMAIL_DIR, "venv", "bin", "python")
GMAILER = os.path.join(EMAIL_DIR, "gmailer.py")

# Loop guard removed permanently (the owner, 2026-06-26): the claude@ ↔ neo@ thread is
# important real work and must continue — never throttle bot-to-bot auto-replies.

HELP = (
    "👋 I'm Claude, running on Mercury (your DST appliance).\n\n"
    "• Just message me — I reply with full access to the DST workspace, email and KB.\n"
    "• Each group is its own separate conversation/topic.\n"
    "• Send a file and I'll offer to ingest it into the KB, analyze it, or hold it for this chat.\n\n"
    "🔒 Privacy (mode A): questions touching private data (customer balances, invoices, "
    "PII) are answered ON-BOX by Nemotron with full chat context; everything else is a "
    "real Claude turn.\n\n"
    "Commands:\n"
    "/cloud <text> — force a cloud (Claude) turn, bypassing the privacy gate (public info only)\n"
    "/topic — show what project this conversation is currently about\n"
    "/privacy | /wisdom — in a project chat: switch answers between the local-policy "
    "model and cloud Claude (mode shows on the group title)\n"
    "/project [slug] — show/set which project this chat files into\n"
    "/clear (or /new) — clear this chat's memory and start fresh\n"
    "/whoami — show this chat's IDs\n"
    "/help — this message")


def log(msg):
    line = f"{datetime.datetime.now():%F %T} {msg}"
    print(line, flush=True)


# ---- typing indicator -------------------------------------------------------
class Typing:
    def __init__(self, chat_id):
        self.chat_id, self._stop = chat_id, threading.Event()

    def __enter__(self):
        threading.Thread(target=self._loop, daemon=True).start(); return self

    def _loop(self):
        while not self._stop.is_set():
            TG.send_chat_action(self.chat_id, "typing")
            self._stop.wait(4.5)

    def __exit__(self, *a):
        self._stop.set()


# ---- file handling ----------------------------------------------------------
def _save_incoming(msg, chat_id):
    """Pull a document/photo/audio/voice/video out of a message and download it.
    Returns (path, caption) or (None, None)."""
    caption = msg.get("caption", "") or ""
    name, file_id = None, None
    if "document" in msg:
        name = msg["document"].get("file_name") or f"doc_{msg['document']['file_id'][:8]}"
        file_id = msg["document"]["file_id"]
    elif "photo" in msg:
        ph = msg["photo"][-1]            # largest rendition
        # file_id[:8] is a constant base64 header ("AgACAgEA") shared by every photo,
        # so two photos saved in the same second collide and overwrite each other
        # (lost 2 of 3 album photos on 2026-07-07). file_unique_id is actually unique.
        uniq = ph.get("file_unique_id") or ph["file_id"][-8:]
        name = f"photo_{uniq}.jpg"; file_id = ph["file_id"]
    elif "voice" in msg:
        name = f"voice_{msg['voice']['file_id'][:8]}.ogg"; file_id = msg["voice"]["file_id"]
    elif "audio" in msg:
        a = msg["audio"]; name = a.get("file_name") or f"audio_{a['file_id'][:8]}.mp3"; file_id = a["file_id"]
    elif "video" in msg:
        name = f"video_{msg['video']['file_id'][:8]}.mp4"; file_id = msg["video"]["file_id"]
    if not file_id:
        return None, None
    d = os.path.join(C.INBOX_DIR, str(chat_id))
    os.makedirs(d, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "._- " else "_" for ch in name).strip()
    dest = os.path.join(d, f"{datetime.datetime.now():%Y%m%d-%H%M%S}_{safe}")
    got = TG.download(file_id, dest)
    if got:
        # An inbound photo's file_id is reusable for SENDING — cache it now so the
        # photo reflex can re-send this image sub-second, forever, with no upload.
        photo_reflex.remember(got, file_id)
    return (got, caption)


def _ingest(path, caption):
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in C.DOC_EXTS:
            r = subprocess.run([C.DOCPIPE, "ingest", path], capture_output=True, text=True, timeout=900)
            return f"📚 Ingested into the KB (docpipe).\n{(r.stdout or r.stderr).strip()[:500]}"
        if ext in C.IMG_EXTS:
            cmd = [C.MEDIA, "add", path]
            if caption:
                cmd += ["--annotation", caption]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            note = f" with annotation: “{caption}”" if caption else " (no caption — reply with one to make it searchable)"
            return f"🖼️ Indexed into CLIP media search{note}.\n{(r.stdout or r.stderr).strip()[:400]}"
        return f"🤷 I don't have an automatic KB importer for {ext or 'this type'}. Use *Hold* and ask me about it."
    except Exception as e:
        return f"⚠️ Ingest failed: {e}"


def _file_keyboard(key):
    return {"inline_keyboard": [[
        {"text": "📚 Ingest to KB", "callback_data": f"ing:{key}"},
        {"text": "🔍 Analyze", "callback_data": f"anl:{key}"},
        {"text": "📎 Hold for chat", "callback_data": f"hld:{key}"},
    ]]}


# Telegram albums (multiple photos/files sent at once) arrive as SEPARATE messages
# sharing a media_group_id, and only ONE of them carries the caption. Buffer them
# until the album settles, then process the whole group with the shared caption.
ALBUMS = {}             # media_group_id -> {"msgs": [...], "chat_id": int, "timer": Timer}
ALBUM_GUARD = threading.Lock()
ALBUM_SETTLE_SECS = 2.5


def _queue_album(mgid, msg, chat_id):
    with ALBUM_GUARD:
        e = ALBUMS.setdefault(mgid, {"msgs": [], "chat_id": chat_id, "timer": None})
        e["msgs"].append(msg)
        if e["timer"]:
            e["timer"].cancel()
        t = threading.Timer(ALBUM_SETTLE_SECS, _flush_album, args=(mgid,))
        t.daemon = True
        t.start()
        e["timer"] = t


def _flush_album(mgid):
    with ALBUM_GUARD:
        e = ALBUMS.pop(mgid, None)
    if not e:
        return
    try:
        handle_album(e["msgs"], e["chat_id"])
    except Exception:
        log("album handler error:\n" + traceback.format_exc())


def handle_album(msgs, chat_id):
    msgs.sort(key=lambda m: m["message_id"])
    caption = next((m.get("caption") or "" for m in msgs if m.get("caption")), "")
    paths = []
    with Typing(chat_id):
        for m in msgs:
            p, _ = _save_incoming(m, chat_id)
            if p:
                paths.append(p)
    if not paths:
        TG.send_message(chat_id, "⚠️ I couldn't download that album from Telegram.")
        return
    _arc_in(msgs[0], chat_id,
            f"[album: {len(paths)} file(s)] {caption}".strip(), kind="file")
    if len(paths) < len(msgs):
        TG.send_message(chat_id, f"⚠️ Only {len(paths)} of {len(msgs)} album files downloaded.")
    # Project chats: file the whole album into the project.
    if projects_mode.is_project_chat(chat_id):
        handle_project_album(msgs, chat_id, paths, caption)
        return
    # Personal notes (the owner 2026-07-10): a caption-less album in his DM = one note per file.
    if not caption.strip() and chat_id == personal_notes.OWNER:
        ids = []
        for p in paths:
            try:
                nid, _ = personal_notes.add(p, orig_name=os.path.basename(p))
                ids.append(nid)
            except Exception as e:
                log(f"personal note save FAILED for {p}: {e}")
        log(f"personal notes saved from album: {ids}")
        TG.send_message(chat_id,
                        f"📝 Saved {len(ids)} personal notes (#{ids[0]}–#{ids[-1]})." if ids
                        else "⚠️ Couldn't save that album as personal notes.",
                        reply_to=msgs[0]["message_id"])
        return
    if caption.strip():
        for p in paths:
            bridge.hold_file(chat_id, p)
        listing = "\n".join(f"  - {p}" for p in paths)
        prompt = (f"{len(paths)} files were sent together as one Telegram album and saved at:\n"
                  f"{listing}\n"
                  f"The user's caption on the album (it applies to ALL of the files): "
                  f"\"{caption.strip()}\"\n"
                  f"Open/read the files and respond to their message.")
        with Typing(chat_id):
            TG.send_message(chat_id, bridge.ask(chat_id, prompt, sender=_sender_full(msgs[0])), reply_to=msgs[0]["message_id"])
        return
    key = uuid.uuid4().hex[:10]
    with PENDING_GUARD:
        PENDING[key] = {"path": paths, "caption": caption, "chat_id": chat_id}
    TG.send_message(chat_id,
                    f"📥 Saved {len(paths)} files from your album.\nWhat should I do with them?",
                    reply_to=msgs[0]["message_id"], reply_markup=_file_keyboard(key))


def _image_data_url(path):
    import base64
    b64 = base64.b64encode(open(path, "rb").read()).decode()
    ext = os.path.splitext(path)[1].lstrip(".").lower() or "jpeg"
    ext = {"jpg": "jpeg"}.get(ext, ext)
    return f"data:image/{ext};base64,{b64}"


def handle_file(msg, chat_id):
    with Typing(chat_id):
        path, caption = _save_incoming(msg, chat_id)
    if not path:
        TG.send_message(chat_id, "⚠️ I couldn't download that file from Telegram.")
        return
    _arc_in(msg, chat_id, f"[file: {os.path.basename(path)}] {caption}".strip(), kind="file")
    # Project chats: every posted file is filed into the project — no action keyboard.
    if projects_mode.is_project_chat(chat_id):
        handle_project_file(msg, chat_id, path, caption)
        return
    # Personal notes (the owner 2026-07-10): a file in HIS DM with no caption is a personal
    # note — stored in the private personal/ db, never offered the KB-ingest keyboard.
    if not caption.strip() and chat_id == personal_notes.OWNER:
        try:
            nid, dest = personal_notes.add(path, orig_name=os.path.basename(path))
            log(f"personal note #{nid} saved: {os.path.basename(dest)}")
            TG.send_message(chat_id, f"📝 Saved as personal note #{nid} (private — "
                            f"only ever shared back to you).", reply_to=msg["message_id"])
        except Exception as e:
            log(f"personal note save FAILED: {e}")
            TG.send_message(chat_id, f"⚠️ Couldn't save that as a personal note: {e}")
        return
    # A caption is the user's actual question/instruction about the file — answer it
    # directly instead of swallowing it behind the action buttons. Hold the file too
    # so follow-up messages in this chat can keep referring to it.
    if caption.strip():
        # Always-Nemotron chats stay on-box even for file captions (a captioned photo
        # in a private group must never escape to the cloud turn). The local model
        # can't view the image, but it gets the caption + file location and handles
        # the instruction like any other private turn.
        if chat_id in C.ALWAYS_NEMOTRON_CHATS:
            note = f"[The sender attached a file, saved at {path}"
            # Images: describe on the local-policy vision model (same as project chats)
            # so the text agent can work from the picture — e.g. redraw a hand sketch
            # as a proper diagram. Best-effort; falls back to a "can't view it" note.
            if path.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                try:
                    desc = projects_mode._or_chat([
                        {"role": "system", "content":
                         "Describe this image in exhaustive detail so a text-only model "
                         "can work from it: every component, label, terminal, value and "
                         "connection you can read, spatial layout, and any handwriting. "
                         "Factual only; no speculation."},
                        {"role": "user", "content": [
                            {"type": "text", "text": "Describe the image."},
                            {"type": "image_url", "image_url": {"url":
                                _image_data_url(path)}}]}],
                        projects_mode.OR_VISION_MODEL, max_tokens=600)
                    note += f". A vision model describes it as: {desc}"
                except Exception as e:
                    log(f"private image describe failed: {e}")
                    note += " — you cannot view it, but their message refers to it"
            else:
                note += " — you cannot view it, but their message refers to it"
            turn_text = f"{note}] {caption.strip()}"
            with Typing(chat_id):
                answer, files = private_turn(turn_text, chat_id, sender=_sender_first(msg))
                reply = "🔒 Nemotron (Always-Nemotron chat):\n\n" + answer
            TG.send_message(chat_id, reply, reply_to=msg["message_id"])
            _arc_out(chat_id, reply)
            _send_private_files(chat_id, files)
            log(f"always-nemotron file-caption chat={chat_id} files={len(files)}")
            return
        bridge.hold_file(chat_id, path)
        prompt = (f"A file was sent to you via Telegram and saved at: {path}\n"
                  f"The user's message accompanying it: \"{caption.strip()}\"\n"
                  f"Open/read the file and respond to their message.")
        with Typing(chat_id):
            reply = bridge.ask(chat_id, prompt, sender=_sender_full(msg))
            TG.send_message(chat_id, reply, reply_to=msg["message_id"])
        _arc_out(chat_id, reply)
        return
    key = uuid.uuid4().hex[:10]
    with PENDING_GUARD:
        PENDING[key] = {"path": path, "caption": caption, "chat_id": chat_id}
    TG.send_message(chat_id,
                    f"📥 Saved `{os.path.basename(path)}`.\nWhat should I do with it?",
                    reply_to=msg["message_id"], reply_markup=_file_keyboard(key))


def handle_voice(msg, chat_id):
    """Voice conversation (2026-07-13): a voice note in a VOICE_CHATS chat becomes a
    spoken turn — on-box whisper transcription (language autodetected), the normal
    Claude turn, then a Piper voice note back plus the full reply text. Any failure
    on the audio side degrades to a plain text reply, never a lost turn."""
    with Typing(chat_id):
        path, _ = _save_incoming(msg, chat_id)
    if not path:
        TG.send_message(chat_id, "⚠️ I couldn't download that voice note.")
        return
    try:
        with Typing(chat_id):
            text, lang = voice_mode.transcribe(path)
    except Exception as e:
        log(f"voice transcribe FAILED chat={chat_id}: {e}")
        TG.send_message(chat_id, f"⚠️ Transcription failed: {str(e)[:200]}")
        return
    if not text:
        TG.send_message(chat_id, "⚠️ I couldn't make out any speech in that voice note.")
        return
    _arc_in(msg, chat_id, text, kind="voice")
    log(f"voice in chat={chat_id} lang={lang} {text[:80]!r}")
    # Echo what was heard so a bad transcription is immediately visible.
    TG.send_message(chat_id, f"🎙️ _{text}_", reply_to=msg["message_id"])
    # Q&A cache: a repeat spoken question is answered instantly (still as a voice note).
    reply = qa_cache.lookup(text) if qa_cache.cacheable(text) else None
    if reply:
        log(f"qa-cache hit (voice) chat={chat_id} {text[:60]!r}")
    else:
        prompt = (f"[Voice conversation: the user SPOKE this as a Telegram voice note and your "
                  f"reply will be read aloud by TTS. Keep it short and conversational — plain "
                  f"prose only: no markdown, no lists, no tables, no code. Reply in the language "
                  f"they spoke (detected: {lang}).]\n\n{text}")
        with Typing(chat_id):
            reply = bridge.ask(chat_id, prompt, sender=_sender_full(msg))
        if qa_cache.cacheable(text):
            qa_cache.store(text, reply, source="voice-note")
    _arc_out(chat_id, reply, kind="voice")
    ogg = None
    try:
        TG.send_chat_action(chat_id, "record_voice")
        ogg = voice_mode.synthesize(reply, lang)
    except Exception as e:
        log(f"voice synth FAILED chat={chat_id}: {e}")
    if ogg and voice_mode.send_voice(chat_id, ogg, reply_to=msg["message_id"]):
        # Full text follows the audio: authoritative record, and the spoken version
        # may be truncated for length.
        TG.send_message(chat_id, reply)
    else:
        TG.send_message(chat_id, reply, reply_to=msg["message_id"])


def handle_callback(cb):
    data = cb.get("data", "")
    msg = cb.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    action, _, key = data.partition(":")
    with PENDING_GUARD:
        info = PENDING.pop(key, None)
    TG.answer_callback(cb["id"])
    if not info:
        TG.send_message(chat_id, "That file action expired. Re-send the file.")
        return
    TG.clear_markup(chat_id, msg["message_id"])
    path, caption = info["path"], info["caption"]
    paths = path if isinstance(path, list) else [path]   # albums hold a list of paths
    if action == "ing":
        with Typing(chat_id):
            TG.send_message(chat_id, "\n".join(_ingest(p, caption) for p in paths))
    elif action == "anl":
        with Typing(chat_id):
            listing = "\n".join(f"  - {p}" for p in paths)
            prompt = (f"File(s) sent to you via Telegram and saved at:\n{listing}\n"
                      f"Caption from the user: \"{caption or '(none)'}\"\n"
                      f"Open/read them and tell me what they are and anything important about them.")
            TG.send_message(chat_id, bridge.ask(chat_id, prompt, sender=_sender_full(cb)))
    elif action == "hld":
        for p in paths:
            bridge.hold_file(chat_id, p)
        TG.send_message(chat_id, "📎 Got it — I'll keep that file in mind for this chat. "
                                 "Ask me anything about it.")


def _chat_history(chat_id, limit=20, max_chars=6000):
    """Recent transcript of this chat (oldest first) from the archive, so the private
    model gets the same conversational context Claude has. Best-effort: '' if no db."""
    if not chatdb:
        return ""
    try:
        with chatdb._lock:
            c = chatdb._get()
            rows = c.execute(
                "SELECT sender, direction, text FROM messages WHERE chat_id=? "
                "AND kind IN ('text','email','file') ORDER BY id DESC LIMIT ?",
                (chat_id, limit)).fetchall()
        lines = []
        for sender, direction, txt in reversed(rows):
            who = "Assistant" if direction == "out" else (sender or "User")
            lines.append(f"{who}: {(txt or '').strip()}")
        return "\n".join(lines)[-max_chars:]
    except Exception as e:
        log(f"history build failed: {e}")
        return ""


def private_turn(text, chat_id, sender="the owner"):
    """Mode A private turn: answer on-box with Nemotron, giving it FULL context (recent
    chat history + KB retrieval) so it can follow the conversation like Claude would.
    Returns (answer, files) — files are {path, caption} dicts the agent queued for
    delivery to this chat (the owner 2026-07-08: private documents may be sent here).
    FAILS CLOSED: on any error we apologize locally — never fall through to the cloud."""
    try:
        r = subprocess.run([C.KB_PY, C.PRIVACY_ROUTE, text, "--json", "--answer",
                            "--history-stdin", "--sender", sender],
                           input=_chat_history(chat_id),
                           capture_output=True, text=True, timeout=240)
        d = json.loads(r.stdout or "{}")
        if d.get("answer"):
            return d["answer"], d.get("files") or []
        raise ValueError(f"empty answer: {(r.stderr or r.stdout)[:200]}")
    except Exception as e:
        log(f"private turn failed (staying on-box, NOT escalating): {e}")
        return ("This involves private data, so I kept it on-box and did NOT send it to the "
                f"cloud — but the on-box answerer failed ({str(e)[:160]}). Try again, or use "
                "/cloud if you're sure it's public."), []


def _send_private_files(chat_id, files):
    """Upload files the private agent queued (sendPhoto for images, sendDocument for
    the rest; a failed photo send retries as a document). Best-effort per file."""
    for f in files or []:
        path, caption = f.get("path") or "", (f.get("caption") or "").strip()
        name = os.path.basename(path)
        # Personal notes leave the workspace only toward the owner himself — last-line gate
        # even if an upstream agent queued one (the owner 2026-07-10).
        if personal_notes.is_personal_path(path) and not personal_notes.allowed_chat(chat_id):
            log(f"BLOCKED personal-note delivery to chat={chat_id}: {name}")
            continue
        as_photo = path.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))
        attempts = ([("sendPhoto", "photo"), ("sendDocument", "document")] if as_photo
                    else [("sendDocument", "document")])
        for method, field in attempts:
            try:
                params = {"chat_id": chat_id}
                if caption:
                    params["caption"] = caption[:1000]
                with open(path, "rb") as fh:
                    r = TG._call(method, _files={field: fh}, _timeout=120, **params)
                if r.get("ok"):
                    _arc_out(chat_id, f"[sent file: {name}]" + (f" {caption}" if caption else ""))
                    log(f"private file sent chat={chat_id} {name} via {method}")
                    break
                log(f"private file send FAILED chat={chat_id} {name} {method}: {r.get('error')}")
            except Exception as e:
                log(f"private file send error chat={chat_id} {name} {method}: {e}")
        else:
            TG.send_message(chat_id, f"⚠️ Couldn't attach {name} — see gateway log.")


# ---- tier-1 grounded quick answer -------------------------------------------
# Retrieve a few KB chunks and let a FAST LLM (Nemotron) answer from JUST those snippets,
# or say ESCALATE. Replaces the old score-threshold reflex: cosine score picks WHAT to read
# but the LLM decides whether it actually answers THIS question (right model? complete?).
# Answered -> send it; escalate/failure -> fall through to the full LLM below.
def kb_reflex(text):
    """Grounded quick answer: retrieve KB chunks + let a fast LLM answer from JUST those, or
    escalate. Returns {answer, score, sources} when it answered, else None -> full LLM.
    ~1-3s (one metered Nemotron call). Escalate/failure both fall through to None."""
    try:
        r = subprocess.run([C.KB_PY, C.KB_ANSWER, text, "--json"],
                           capture_output=True, text=True, timeout=50)
        d = json.loads(r.stdout or "{}")
        # Return the dict either way: answered -> send it; escalate -> hand the already-
        # retrieved snippets to Claude so it answers from them instead of re-searching.
        if d.get("answer") or d.get("snippets"):
            return d
    except Exception as e:
        log(f"kb reflex error: {e}")
    return None


# ---- project chats ----------------------------------------------------------
# (the owner, 2026-07-19): every post in a bound group is filed into its project
# under DST_ROOT/projects/<slug>/ BEFORE the conversational turn; the turn's
# first source of context is the project directory, not the KB. Privacy toggle:
# /wisdom = cloud Claude, /privacy = local-policy Nemotron (fails closed).
def handle_project_text(msg, chat_id, text, kind="note", already_filed=None):
    """File the message into the project notebook, then answer per privacy mode."""
    pm = projects_mode.get(chat_id)
    note_path = projects_mode.add_note(pm["project"], text, _sender_first(msg), kind=kind)
    filed = already_filed or f" The user's message was ALREADY auto-filed to {note_path} — do not file it again."
    if pm.get("privacy") == "privacy":
        ctx = (f"[Project '{pm['project']}' chat. Project files live under "
               f"{projects_mode.root(pm['project'])} (PROJECT.md, REGISTRY.md, notes/, "
               f"files/) — consult them FIRST, before the KB.] ")
        with Typing(chat_id):
            answer, files = private_turn(ctx + text, chat_id, sender=_sender_first(msg))
        reply = "🔒 Nemotron (Privacy mode):\n\n" + answer
        TG.send_message(chat_id, reply, reply_to=msg["message_id"])
        _arc_out(chat_id, reply)
        _send_private_files(chat_id, files)
        log(f"project turn (privacy) chat={chat_id}")
        return
    prompt = projects_mode.turn_context(chat_id, filed) + "\n\n" + text
    with Typing(chat_id):
        reply = bridge.ask(chat_id, prompt, sender=_sender_full(msg))
    TG.send_message(chat_id, reply, reply_to=msg["message_id"])
    _arc_out(chat_id, reply)
    log(f"project turn (wisdom) chat={chat_id}")


def handle_project_file(msg, chat_id, path, caption):
    """File one downloaded item into the project. Voice notes: transcribe on-box
    (whisper.cpp), file both the audio and the transcript, then treat the transcript
    as a message. Photos/docs: annotate/summarize via the local-policy model."""
    sender = _sender_first(msg)
    pm = projects_mode.get(chat_id)
    if "voice" in msg or "audio" in msg:
        text = ""
        try:
            with Typing(chat_id):
                text, _lang = voice_mode.transcribe(path)
        except Exception as e:
            log(f"project voice transcribe FAILED chat={chat_id}: {e}")
        ann = caption or (f"voice note; transcript: {text[:300]}" if text else "(transcription failed)")
        dest, kind, _a, _auto = projects_mode.ingest_file(chat_id, path, ann, sender)
        if not text:
            TG.send_message(chat_id, f"📁 Filed audio to `{os.path.relpath(dest, projects_mode.PROJECTS_DIR)}` "
                                     "— ⚠️ transcription failed.", reply_to=msg["message_id"])
            return
        TG.send_message(chat_id, f"🎙️ _{text}_", reply_to=msg["message_id"])
        handle_project_text(msg, chat_id, text, kind="voice",
                            already_filed=f" (Voice note: audio filed at {dest}; transcript auto-filed to notes/.)")
        return
    with Typing(chat_id):
        dest, kind, annotation, auto = projects_mode.ingest_file(chat_id, path, caption, sender)
    rel = os.path.relpath(dest, projects_mode.PROJECTS_DIR)
    tag = "auto-annotation" if auto else ("annotation" if caption else "note")
    preview = (annotation or "")[:350]
    TG.send_message(chat_id, f"📁 Filed {kind} → `{rel}`\n_{tag}:_ {preview}",
                    reply_to=msg["message_id"])
    # A caption may also be a question/instruction about the file — give the
    # conversational turn a chance to act on it (it's told to stay quiet-short
    # if the caption was purely descriptive).
    if caption.strip():
        prompt = (f"The user attached a {kind} (filed at {dest}, registry updated) with "
                  f"this caption: \"{caption.strip()}\". If the caption contains a question "
                  f"or instruction, respond/act on it (project files are the first source "
                  f"of context). If it was just a description/annotation, reply with one "
                  f"very short acknowledgement.")
        handle_project_text(msg, chat_id, prompt, kind="caption",
                            already_filed=" (File + caption already filed — do not re-file.)")


def handle_project_album(msgs, chat_id, paths, caption):
    sender = _sender_first(msgs[0])
    lines, dests = [], []
    with Typing(chat_id):
        for p in paths:
            dest, kind, annotation, auto = projects_mode.ingest_file(chat_id, p, caption, sender)
            dests.append(dest)
            lines.append(f"• {kind} `{os.path.relpath(dest, projects_mode.PROJECTS_DIR)}` — "
                         f"{(annotation or '')[:150]}")
    TG.send_message(chat_id, "📁 Filed album:\n" + "\n".join(lines),
                    reply_to=msgs[0]["message_id"])
    if caption.strip():
        listing = "\n".join(dests)
        prompt = (f"The user posted an album of {len(dests)} files (already filed):\n{listing}\n"
                  f"Album caption: \"{caption.strip()}\". If it contains a question or "
                  f"instruction, respond/act on it; if purely descriptive, reply with one "
                  f"very short acknowledgement.")
        handle_project_text(msgs[0], chat_id, prompt, kind="caption",
                            already_filed=" (Album + caption already filed — do not re-file.)")


# ---- text -------------------------------------------------------------------
def handle_text(msg, chat_id, text):
    # Match just the first token, lowercased, with the @botname suffix (added in
    # groups, e.g. "/clear@Claude_DST_bot") stripped — so commands work everywhere.
    word = text.strip().split(maxsplit=1)[0].lower() if text.strip() else ""
    command = word.split("@")[0]
    _arc_in(msg, chat_id, text, kind="command" if command.startswith("/") else "text")
    if command in ("/start", "/help"):
        TG.send_message(chat_id, HELP); return
    if command == "/whoami":
        TG.send_message(chat_id, f"chat_id: `{chat_id}`\nyour user_id: `{msg['from']['id']}`\n"
                                 f"chat type: {msg['chat'].get('type')}"); return
    if command in ("/cloud", "/c"):
        # Escape hatch for STRICT privacy mode: force a real cloud (Claude) turn,
        # bypassing the on-box router. Use only when you know the query is public.
        parts = text.strip().split(maxsplit=1)
        rest = parts[1].strip() if len(parts) > 1 else ""
        if not rest:
            r2 = msg.get("reply_to_message", {})
            rest = (r2.get("text") or r2.get("caption") or "").strip()
        if not rest:
            TG.send_message(chat_id, "Usage: /cloud <message> — force a cloud (Claude) turn, "
                                     "bypassing the on-box privacy router. ⚠️ Whatever you ask "
                                     "may be processed on the AI cloud, so keep it to public info.")
            return
        with Typing(chat_id):
            reply = bridge.ask(chat_id, rest, sender=_sender_full(msg))
        TG.send_message(chat_id, reply, reply_to=msg["message_id"])
        _arc_out(chat_id, reply)
        log(f"/cloud forced Claude turn chat={chat_id}")
        return
    if command == "/topic":
        topic = _current_topic(chat_id)
        if topic:
            TG.send_message(chat_id, f"🏷️ Current topic: *{topic}*")
        else:
            TG.send_message(chat_id, "🏷️ No topic classified yet for this chat — tagging "
                                     "lands a second or two after each message. Try again in a moment.")
        return
    if command in ("/clear", "/new", "/reset"):
        bridge.reset(chat_id)
        TG.send_message(chat_id, "🧹 Cleared. This chat starts fresh — I won't remember earlier "
                                 "messages (or held files) here."); return
    # Project-chat controls: privacy toggle (shown on the group title) + rebinding.
    if command in ("/privacy", "/wisdom", "/wise"):
        if not projects_mode.is_project_chat(chat_id):
            TG.send_message(chat_id, "This chat isn't bound to a project. Use "
                                     "`/project <slug>` first."); return
        mode = "privacy" if command == "/privacy" else "wisdom"
        projects_mode.set_privacy(chat_id, mode)
        err = projects_mode.apply_title(chat_id, msg["chat"].get("title"))
        who = ("🔒 *Privacy* — answers run on the local-policy model (Nemotron); "
               "nothing goes to the cloud Claude turn." if mode == "privacy" else
               "💡 *Wisdom* — answers run on cloud Claude (filing/analysis of files "
               "stays on the local-policy model).")
        note = (f"\n⚠️ Couldn't update the group title ({err}) — make the bot a group "
                f"admin with *Change group info* so the mode shows up top." if err else "")
        TG.send_message(chat_id, f"{who}{note}")
        log(f"project chat={chat_id} privacy -> {mode} (title err: {err})")
        return
    if command == "/project":
        parts = text.strip().split(maxsplit=1)
        if len(parts) < 2:
            pm = projects_mode.get(chat_id)
            TG.send_message(chat_id,
                            f"📌 Project: *{pm['project']}* · mode: {pm.get('privacy','wisdom')}\n"
                            f"Dir: `{projects_mode.root(pm['project'])}`" if pm else
                            "Usage: `/project <slug>` — bind this chat to DST/projects/<slug>/")
            return
        slug = projects_mode.set_project(chat_id, parts[1])
        TG.send_message(chat_id, f"📌 This chat now files into `projects/{slug}/`."
                        if slug else "⚠️ Bad project name — use letters/digits/dashes.")
        return
    # Project chats: file-then-answer, project files first. Handled BEFORE the KB
    # reflexes/caches on purpose — the KB is the wrong first source here.
    if projects_mode.is_project_chat(chat_id) and not command.startswith("/"):
        handle_project_text(msg, chat_id, text)
        return

    # Doc reflex (2026-07-10, the owner: "make the labelexpo pass instant"): requests for a
    # CURATED registered document ("fetch my labelexpo pass") are answered by a direct
    # sendDocument with a cached file_id — no LLM turn. Registry: doc_registry.json,
    # re-read every message so new docs need no restart. Anything unmatched falls through.
    if C.DOC_REFLEX:
        try:
            summary = doc_reflex.try_handle(chat_id, text)
        except Exception as e:
            summary = None
            log(f"doc reflex error: {e}")
        if summary:
            _arc_out(chat_id, summary)
            log(f"doc reflex chat={chat_id} {summary}")
            return

    # Photo reflex (2026-07-07, the owner: "sub-second image retrieval"): an image request
    # ("show me the qs256 heads", "/pic voxeljet") is answered deterministically from
    # the warm CLIP index + cached Telegram file_ids — no LLM turn at all. Fires only
    # on an exact keyword match against curated tags/annotations; anything fuzzy costs
    # ~10ms and falls through to the normal paths below.
    if C.PHOTO_REFLEX:
        try:
            summary = photo_reflex.try_handle(chat_id, text)
        except Exception as e:
            summary = None
            log(f"photo reflex error: {e}")
        if summary:
            _arc_out(chat_id, summary)
            log(f"photo reflex chat={chat_id} {summary}")
            return

    # File reflex (2026-07-10, the owner: "show me / fetch / get / give me ... PDF, images,
    # docs and other files — fast, closely matching"): generic fetch-verb requests are
    # resolved deterministically against the KB image index and (DM chats only) a
    # cached walk of the DST workspace. Only a candidate covering EVERY distinctive
    # query token is sent; ambiguous/partial/question-shaped asks fall through.
    if C.FILE_REFLEX:
        try:
            summary = file_reflex.try_handle(chat_id, text)
        except Exception as e:
            summary = None
            log(f"file reflex error: {e}")
        if summary:
            _arc_out(chat_id, summary)
            log(f"file reflex chat={chat_id} {summary}")
            return

    # Always-Claude chats: no local-model interception of any kind — every message
    # falls through to the full Claude turn (the owner: "Claude DST Public" group).
    always_claude = chat_id in C.ALWAYS_CLAUDE_CHATS

    # Always-Nemotron chats: EVERY non-command message runs the on-box private turn
    # (Nemotron with full chat history + CRM/KB lookup tools + file delivery) — never
    # the cloud Claude turn. Fails closed; /cloud is the explicit escape hatch.
    if chat_id in C.ALWAYS_NEMOTRON_CHATS and not command.startswith("/"):
        with Typing(chat_id):
            answer, files = private_turn(text, chat_id, sender=_sender_first(msg))
            reply = "🔒 Nemotron (Always-Nemotron chat):\n\n" + answer
        TG.send_message(chat_id, reply, reply_to=msg["message_id"])
        _arc_out(chat_id, reply)
        _send_private_files(chat_id, files)
        log(f"always-nemotron chat={chat_id} files={len(files)}")
        return

    # Grammar routing removed entirely: the NL auto-trigger on 2026-07-07 and the
    # /grammar command on 2026-07-08 (the owner: "we don't need it"). The on-box grammar
    # CLI still exists outside the gateway.

    # Gate #3 (mode A, targeted): the privacy LABEL of the data a query needs decides
    # the LLM. Only queries whose intent touches PRIVATE data (customer balances,
    # invoices tied to a party, PII) route to Nemotron — WITH this chat's recent
    # history, so it has full context like Claude would. Everything else falls through
    # to the normal Claude turn. Fails closed on the private path (never escalates).
    if C.PRIVACY_MODE == "targeted" and not command.startswith("/") and not always_claude:
        priv, why = privacy_router.is_private(text)
        if priv:
            with Typing(chat_id):
                answer, files = private_turn(text, chat_id, sender=_sender_first(msg))
                reply = ("🔒 Private — answered on-box by Nemotron (not sent to the cloud):\n\n"
                         + answer)
            TG.send_message(chat_id, reply, reply_to=msg["message_id"])
            _arc_out(chat_id, reply)
            _send_private_files(chat_id, files)
            log(f"privacy-route PRIVATE [{why}] -> Nemotron(full-context) chat={chat_id} "
                f"files={len(files)}")
            return

    # Q&A cache (2026-07-13): a question semantically equal to one already answered by
    # a Claude turn is served from the local cache in ~0.1s — no LLM at all. Placed
    # AFTER the privacy gate so private-intent queries never reach it. Only standalone
    # question-shaped messages qualify (see qa_cache.cacheable); everything else falls
    # through untouched.
    user_q = text          # pristine question for the cache (reflexes may augment text)
    if not command.startswith("/") and qa_cache.cacheable(text):
        cached = qa_cache.lookup(text)
        if cached:
            TG.send_message(chat_id, "⚡ " + cached, reply_to=msg["message_id"])
            _arc_out(chat_id, cached)
            log(f"qa-cache hit chat={chat_id} {text[:60]!r}")
            return

    # Tier-1 reflex: if the KB has a confident Q&A answer, send it INSTANTLY (~0.15s,
    # no LLM), then verify in the background and only follow up if it was wrong. Low
    # confidence -> None -> straight to the full LLM path below (unchanged).
    if C.KB_REFLEX and not command.startswith("/") and not always_claude:
        with Typing(chat_id):                         # grounded lookup; show "typing…"
            hit = kb_reflex(text)
        if hit and hit.get("answer"):                 # grounded answer -> send instantly
            TG.send_message(chat_id, hit["answer"], reply_to=msg["message_id"])
            _arc_out(chat_id, hit["answer"])
            log(f"reflex answer {hit['score']:.3f} {','.join(hit.get('sources') or [])} chat={chat_id}")
            return
        if hit and hit.get("snippets"):               # escalate -> feed snippets to full LLM
            text = (f"{text}\n\n[KB snippets already retrieved for this question — answer from "
                    f"these if they cover it; only search further if they don't:\n{hit['snippets']}]")
            log(f"reflex escalate {hit.get('score', 0):.3f} -> Claude with snippets chat={chat_id}")

    # Non-streaming ("old way"): show a typing indicator, wait for the full reply,
    # send it once. Toggle C.STREAMING to re-enable live editing.
    if not C.STREAMING:
        with Typing(chat_id):
            reply = bridge.ask(chat_id, text, sender=_sender_full(msg))
        TG.send_message(chat_id, reply, reply_to=msg["message_id"])
        _arc_out(chat_id, reply)
        if qa_cache.cacheable(user_q):
            qa_cache.store(user_q, reply, source="text")
        return

    # Stream the reply: send a placeholder immediately, then edit it live as Claude's
    # text arrives / tools run — so you see progress instead of 30-40s of silence.
    placeholder = TG.send_message(chat_id, "💭 …", reply_to=msg["message_id"])
    mid = TG.message_id(placeholder)
    st = {"t": 0.0, "shown": None}

    def on_event(kind, data):
        now = time.time()
        if kind == "tool" and mid and now - st["t"] >= 1.0:
            TG.edit_text(chat_id, mid, f"🔧 {data}…")
            st["t"] = now
        elif kind == "text" and mid and data != st["shown"] and now - st["t"] >= C.EDIT_INTERVAL:
            TG.edit_text(chat_id, mid, data[-3500:])   # live tail (plain, fast)
            st["shown"] = data; st["t"] = now

    reply = bridge.ask_stream(chat_id, text, on_event, sender=_sender_full(msg))
    _arc_out(chat_id, reply)
    if mid:
        TG.deliver_final(chat_id, mid, reply)
    else:
        TG.send_message(chat_id, reply, reply_to=msg["message_id"])
    if qa_cache.cacheable(user_q):
        qa_cache.store(user_q, reply, source="text")


# ---- injected emails (from the claude@ IDLE watcher) ------------------------
# A mail from the owner/neo@ is treated like a message typed in this chat: the watcher
# drops it as a JSON file in inject/, we run it as a real Claude turn in the same
# per-chat session (so it shares history + serializes via bridge's per-chat lock).
def _email_reply(to_addr, reply_to_id, text):
    """Send the reply back to the original sender, in-thread, from claude@.
    Returns None on success, else an error string."""
    if not to_addr or not reply_to_id:
        return "missing sender/message-id"
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
            tf.write(text); tmp = tf.name
        cmd = [EMAIL_PY, GMAILER, "send", "--to", to_addr,
               "--reply-to", reply_to_id, "--body-file", tmp, "--md"]
        # Replies to neo@ always CC the owner so they stay in the loop (2026-06-26).
        if "neo@" in to_addr.lower() and C.OWNER_EMAIL and C.OWNER_EMAIL not in to_addr.lower():
            cmd += ["--cc", C.OWNER_EMAIL]
        r = subprocess.run(cmd, cwd=EMAIL_DIR, capture_output=True, text=True, timeout=120)
        os.unlink(tmp)
        return None if r.returncode == 0 else (r.stderr or r.stdout or "send failed").strip()[:300]
    except Exception as e:
        return str(e)


def _is_throttle_bounce(body):
    """True if an inbound email is an auto-generated throttle/usage-limit bounce
    (Neo's agent loop out of quota), not real content — e.g.
    "API call failed after 3 retries: HTTP 429: The usage limit has been reached"."""
    b = (body or "").lower()
    return ("usage limit has been reached" in b
            or ("429" in b and "usage limit" in b)
            or ("http 429" in b and "api call failed" in b))


def handle_injected_email(rec):
    chat_id = int(rec["chat_id"])
    frm = rec.get("from", ""); subj = rec.get("subject", "(no subject)")
    body = (rec.get("body", "") or "").strip()
    addr = frm.lower()
    # Neo throttled: his replies become 429/usage-limit bounces. Do NOT run a turn or
    # auto-reply — that just lands in his inbox, re-triggers his loop, and spams this
    # chat. Drop it silently; resume when he sends real content. (the owner, 2026-07-03)
    if _is_throttle_bounce(body):
        log(f"dropping throttle-bounce from {frm}: {body[:80]!r}")
        return
    if "neo@" in addr:
        who, first = "Neo (neo@)", "Neo"
    elif C.OWNER_EMAIL and C.OWNER_EMAIL in addr:
        who, first = f"{C.OWNER_NAME} (business)", C.OWNER_NAME
    elif C.OWNER_PERSONAL_EMAIL and C.OWNER_PERSONAL_EMAIL in addr:
        who, first = f"{C.OWNER_NAME} (personal gmail)", C.OWNER_NAME
    else:
        who, first = frm, frm
    bridge.set_chat_meta(chat_id, None, "private")
    if chatdb:
        chatdb.record(f"Subject: {subj}\n\n{body}", "in", sender=first, chat_id=chat_id,
                      kind="email", session_id=bridge.sid_for(chat_id))
    preview = body if len(body) <= 3000 else body[:3000] + " …(truncated)"
    atts = rec.get("attachments") or []
    att_line = f"\n📎 {len(atts)} attachment(s)" if atts else ""
    TG.send_message(chat_id, f"📩 *Email from {who}*\n*Subject:* {subj}{att_line}\n\n{preview}")
    att_note = ("\n\nAttachments (already downloaded to disk):\n"
                + "\n".join(f"- {p}" for p in atts)) if atts else ""
    # Standing routing policy for instruction-less mail from the owner (the owner, 2026-07-11).
    routing = ""
    if C.OWNER_PERSONAL_EMAIL and C.OWNER_PERSONAL_EMAIL in addr:
        routing = (
            " STANDING POLICY for this sender (the owner's PERSONAL gmail): if the subject/body "
            "contain instructions, follow them. If NOT (the mail is just content/files, empty "
            "or trivial text), file it as a PERSONAL NOTE: save the email body (if non-trivial) "
            "as a .md file, then move it and EVERY attachment into the personal store. For each "
            "attachment FIRST open/read it (PDF, image, doc) and extract 5-15 content keywords; "
            "a descriptive subject line serves as the file's label. Then file each with "
            "`cd /home/mercury/DST/telegram && python3 -c \"import personal_notes; "
            "print(personal_notes.add('<path>', orig_name='<name>', label='<subject>', "
            "keywords=['kw1','kw2',...]))\"` (one call per file) — label and keywords are "
            "searchable later. Personal notes are private — never mention their content outside "
            "the owner's DM. Then reply with a short confirmation of what was filed."
        )
    elif C.OWNER_EMAIL and C.OWNER_EMAIL in addr:
        routing = (
            " STANDING POLICY for this sender: if the subject/body contain instructions, follow "
            "them. If NOT (the mail is just content/files, empty or trivial text), add it to the "
            "PRIVATE KB: save the email contents as a .md file and move every attachment under "
            "/home/mercury/DST/knowledge-base/from-emails/ (KB default is private — do NOT add "
            "anything to public_paths in data-classification.json). For each attachment FIRST "
            "open/read it and extract 5-15 content keywords, then write a sidecar `<file>.meta.md` "
            "next to it containing: the description (a descriptive subject line serves as this), "
            "the attachment filename, and the keywords — that sidecar is what makes the file "
            "findable via ./kb search and ug. Then reply with a short confirmation of what was filed."
        )
    prompt = (
        f"[This just arrived as an email to claude@ from {who} <{frm}>. Treat it EXACTLY "
        f"as if {first} sent it to you as a message in THIS Telegram chat: read it and "
        f"respond/act on it.{routing} Write ONE reply: it is delivered to them BOTH here in this "
        f"chat AND automatically emailed back to {first} in-thread — so write it as a "
        f"self-contained reply that reads well as an email too. Do NOT call the email "
        f"tools yourself (the reply is sent for you), and never send mail as "
        f"any human.]\n\nSubject: {subj}\n\n{body}{att_note}")
    with Typing(chat_id):
        reply = bridge.ask(chat_id, prompt)
    TG.send_message(chat_id, reply)
    _arc_out(chat_id, reply, kind="email")
    # Also email the same reply back to the sender, in-thread (the owner's request 2026-06-26).
    to_addr = parseaddr(frm)[1]
    err = _email_reply(to_addr, rec.get("id"), reply)
    if err:
        TG.send_message(chat_id, f"⚠️ (Replied in chat, but the email reply to {to_addr} failed: {err})")
        log(f"email reply FAILED to {to_addr}: {err}")
    else:
        TG.send_message(chat_id, f"✉️ (Also emailed this reply to {to_addr}.)")
        log(f"email reply sent to {to_addr}")


def _safe_injected(rec):
    try:
        handle_injected_email(rec)
    except Exception:
        log("injected-email error:\n" + traceback.format_exc())


def drain_injections():
    """Poll inject/ and run any queued email as a chat turn. Daemon thread."""
    os.makedirs(INJECT_DIR, exist_ok=True)
    while True:
        try:
            for fn in sorted(f for f in os.listdir(INJECT_DIR) if f.endswith(".json")):
                path = os.path.join(INJECT_DIR, fn)
                try:
                    rec = json.load(open(path))
                except Exception:
                    try: os.remove(path)
                    except OSError: pass
                    continue
                # claim the file (remove) before running so it can't be processed twice
                try:
                    os.remove(path)
                except FileNotFoundError:
                    continue
                POOL.submit(_safe_injected, rec)
        except Exception:
            log("inject drain error:\n" + traceback.format_exc())
        time.sleep(2)


# ---- dispatch ---------------------------------------------------------------
def handle_update(upd):
    try:
        if "callback_query" in upd:
            cb = upd["callback_query"]
            if cb.get("from", {}).get("id") in C.allowlist():
                handle_callback(cb)
            else:
                TG.answer_callback(cb["id"], "Not authorized.")
            return
        msg = upd.get("message")
        if not msg or msg.get("from", {}).get("is_bot"):
            return
        chat_id = msg["chat"]["id"]
        uid = msg.get("from", {}).get("id")
        bridge.set_chat_meta(chat_id, msg["chat"].get("title"), msg["chat"].get("type"))
        if uid not in C.allowlist():
            log(f"DENY uid={uid} ({msg.get('from',{}).get('username')}) chat={chat_id} type={msg['chat'].get('type')}")
            if msg["chat"].get("type") == "private":
                TG.send_message(chat_id, f"⛔ Not authorized. Your user ID is {uid}.")
            return
        if "voice" in msg and chat_id in C.VOICE_CHATS and not msg.get("media_group_id"):
            handle_voice(msg, chat_id)
        elif any(k in msg for k in ("document", "photo", "voice", "audio", "video")):
            mgid = msg.get("media_group_id")
            if mgid:
                _queue_album(mgid, msg, chat_id)
            else:
                handle_file(msg, chat_id)
        elif "text" in msg:
            handle_text(msg, chat_id, msg["text"])
    except Exception:
        log("handler error:\n" + traceback.format_exc())


def _load_offset():
    try:
        return int(open(C.OFFSET_FILE).read().strip())
    except Exception:
        return 0


def _save_offset(o):
    open(C.OFFSET_FILE, "w").write(str(o))


def main():
    if not C.TOKEN:
        sys.exit("No bot token. Put it in telegram/bot_token or set TG_BOT_TOKEN.")
    me = TG.get_me()
    if not me.get("ok"):
        sys.exit(f"getMe failed — bad token? {me.get('error')}")
    who = me["result"]
    TG.set_commands([
        {"command": "cloud", "description": "Force a cloud (Claude) turn — public info only"},
        {"command": "privacy", "description": "Project chat: answers on the local-policy model"},
        {"command": "wisdom", "description": "Project chat: answers on cloud Claude"},
        {"command": "project", "description": "Show or set this chat's project binding"},
        {"command": "topic", "description": "Show this conversation's current project topic"},
        {"command": "clear", "description": "Clear this chat's memory, start fresh"},
        {"command": "whoami", "description": "Show chat & user IDs"},
        {"command": "help", "description": "What I can do"},
    ])
    log(f"gateway up as @{who.get('username')} ({who.get('id')}); allowlist={sorted(C.allowlist())}")
    if not C.allowlist():
        log("WARNING: allowlist is EMPTY — every message will be denied until you add a user ID "
            "to telegram/allowlist.json")
    threading.Thread(target=drain_injections, daemon=True).start()
    log(f"email→chat injection queue active: {INJECT_DIR}")

    # Project chats: assert the privacy-mode suffix on each bound group's title once
    # per boot (first boot fetches the base title via getChat). Best-effort — a bot
    # without "change info" admin rights just logs the refusal.
    def _assert_project_titles():
        for cid in set(C.PROJECT_CHATS) | {int(k) for k in projects_mode._load()}:
            try:
                st = projects_mode.get(cid) or {}
                base = st.get("base_title")
                if not base:
                    r = TG._call("getChat", chat_id=cid)
                    base = (r.get("result") or {}).get("title") if r.get("ok") else None
                err = projects_mode.apply_title(cid, base)
                if err and "not modified" not in err.lower():
                    log(f"project title assert chat={cid}: {err}")
            except Exception as e:
                log(f"project title assert failed chat={cid}: {e}")
    threading.Thread(target=_assert_project_titles, daemon=True).start()
    if chatdb:
        try:
            import classify
            classify.start_worker()
            log("real-time chat classifier active (tags each message ~1-2s after save)")
        except Exception as e:
            log(f"could not start real-time classifier ({e}) — hourly cron still covers it")
    offset = _load_offset()
    while True:
        res = TG.get_updates(offset)
        if not res.get("ok"):
            log(f"getUpdates error: {res.get('error')}"); time.sleep(5); continue
        for upd in res["result"]:
            offset = upd["update_id"] + 1
            POOL.submit(handle_update, upd)
        _save_offset(offset)


if __name__ == "__main__":
    main()
