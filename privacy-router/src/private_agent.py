"""Tool-calling agent loop for the PRIVATE path.

The private model (any OpenAI-compatible endpoint that supports tool calling — e.g.
Nemotron via OpenRouter, or a local llama.cpp/vLLM server) answers private queries
agentically: it calls read-only lookup tools — CRM contacts, an archived-email store,
an optional semantic KB index — in a loop until it can answer. Every tool executes
ON-BOX; only the model inference itself goes to the endpoint (fully private once the
model is local).

Why a loop: a single-shot call with pre-retrieved context can't follow up. Worse, a
capable model will *pretend* it can ("let me check the database…") and stop — a
hallucinated capability. Giving it real tools fixes both.

Design constraints:
  • Tools are READ-ONLY lookups plus file DELIVERY (find_files/send_file). No writes,
    no shell. send_file only queues a path — the gateway does the actual chat upload
    after the loop, so this process never touches the network beyond the model.
  • Hard caps: MAX_TURNS model calls, WALL_DEADLINE seconds overall — a stuck loop
    degrades to "couldn't finish", never hangs the caller.
  • Fail closed: any error surfaces as a plain explanation; the caller must never
    fall through to the cloud LLM.

Config (env):
  PRIVATE_LLM_URL     chat/completions endpoint (default: OpenRouter)
  PRIVATE_LLM_MODEL   model id
  PRIVATE_LLM_KEY     api key, or PRIVATE_LLM_KEY_FILE=path (KEY=value lines)
  CONTACTS_DB         sqlite CRM db (tables: contacts, emails — see crm-contacts skill)
  WORKSPACE_ROOT      dir find_files/send_file are confined to (default: cwd)
"""
import os, sys, json, time, sqlite3, re, urllib.request

OR_URL = os.environ.get("PRIVATE_LLM_URL", "https://openrouter.ai/api/v1/chat/completions")
OR_MODEL = os.environ.get("PRIVATE_LLM_MODEL", "nvidia/nemotron-3-super-120b-a12b")
CONTACTS_DB = os.environ.get("CONTACTS_DB", "")
WORKSPACE_ROOT = os.path.realpath(os.environ.get("WORKSPACE_ROOT", os.getcwd()))

MAX_TURNS = 6          # max model calls (i.e. up to 5 rounds of tool use)
WALL_DEADLINE = 120    # seconds for the whole loop

_STOP = {"what", "why", "how", "when", "where", "who", "did", "does", "do", "the",
         "a", "an", "is", "are", "was", "were", "with", "from", "about", "have",
         "has", "had", "want", "wanted", "will", "would", "our", "their", "them",
         "they", "this", "that", "much", "many", "get", "got", "can", "could",
         "please", "tell", "show", "give", "know", "need", "customer", "client"}


def _log(msg):
    print(f"[private-agent] {msg}", file=sys.stderr, flush=True)


def _load_key():
    k = os.environ.get("PRIVATE_LLM_KEY")
    if k:
        return k
    path = os.environ.get("PRIVATE_LLM_KEY_FILE", "")
    try:
        return next((l.split("=", 1)[1].strip() for l in open(path) if "=" in l), None)
    except Exception:
        return None


def _terms(text, n=6):
    return [w for w in re.findall(r"[a-zA-Z][a-zA-Z0-9&'-]{2,}", text.lower())
            if w not in _STOP][:n]


# ---- tools (all read-only, all on-box) --------------------------------------
def t_search_contacts(query):
    """LIKE-match contacts by name/company/email; return rolling summaries."""
    out = []
    db = sqlite3.connect(CONTACTS_DB)
    for term in _terms(query, 4) or [query.lower()]:
        for email, name, company, base, act in db.execute(
                "SELECT email, name, company, base_summary, activity_summary FROM contacts "
                "WHERE lower(coalesce(name,'')||' '||coalesce(company,'')||' '||email) "
                "LIKE ? LIMIT 4", (f"%{term}%",)):
            summ = " ".join(s for s in (base, act) if s).strip()
            out.append({"email": email, "name": name, "company": company,
                        "summary": summ[:1500] or "(no summary)"})
    dedup = list({c["email"]: c for c in out}.values())[:6]
    return dedup or "No matching contacts."


def t_search_emails(query, limit=5):
    """Keyword-rank archived emails; return id/date/from/subject + short excerpt."""
    terms = _terms(query)
    if not terms:
        return "Query had no searchable terms."
    db = sqlite3.connect(CONTACTS_DB)
    score = "+".join("(CASE WHEN instr(lower(coalesce(subject,'')||' '||coalesce(body,'')), ?) "
                     ">0 THEN 1 ELSE 0 END)" for _ in terms)
    rows = db.execute(
        f"SELECT id, date, from_addr, to_addr, subject, body, ({score}) AS m FROM emails "
        f"WHERE m>0 ORDER BY m DESC, internal_date DESC LIMIT ?",
        terms + [min(int(limit or 5), 8)]).fetchall()
    out = []
    for eid, date, frm, to, subj, body, m in rows:
        body = body or ""
        pos = min((p for p in (body.lower().find(t) for t in terms) if p >= 0), default=0)
        out.append({"id": eid, "date": date, "from": frm, "to": to, "subject": subj,
                    "excerpt": body[max(0, pos - 150):pos + 450].strip()})
    return out or "No matching emails."


def t_read_email(email_id):
    """Full body of one archived email by id (from search_emails results)."""
    db = sqlite3.connect(CONTACTS_DB)
    r = db.execute("SELECT date, from_addr, to_addr, cc, subject, body FROM emails "
                   "WHERE id=?", (str(email_id),)).fetchone()
    if not r:
        return f"No email with id {email_id}."
    date, frm, to, cc, subj, body = r
    return {"date": date, "from": frm, "to": to, "cc": cc, "subject": subj,
            "body": (body or "")[:6000]}


def t_kb_search(query):
    """Semantic search over the knowledge base (needs the kb-semantic-index skill's
    kb_index.py importable; tool is skipped gracefully if it isn't)."""
    from kb_index import retrieve
    hits = retrieve(query, k=5)
    return [{"source": h["source"], "score": round(h["score"], 3),
             "text": h["text"][:800]} for h in hits] or "No KB matches."


# ---- file delivery (the private chat must be able to hand over actual documents —
# PDFs, invoices, images — not just talk about them). The chat is allowlisted and
# private; sending confidential company files there is accepted policy. Credentials
# are the one thing that must never leave the box, so those stay blocked.
SEND_MAX_BYTES = 49 * 1024 * 1024      # Telegram bot upload cap is 50 MB
_DENY_PARTS = ("token", "secret", "credential", "password", "bot_token",
               os.sep + ".git" + os.sep, os.sep + "venv" + os.sep)
_SKIP_DIRS = {".git", "venv", "__pycache__", "node_modules", "logs", "state",
              ".claude", "inject", "inbox"}
_pending_files = []


def _sendable(path):
    """Return (real_path, error). A file is sendable only if it resolves inside the
    workspace, exists, fits the chat upload cap and isn't credential-like."""
    real = os.path.realpath(path if os.path.isabs(path)
                            else os.path.join(WORKSPACE_ROOT, path))
    if not real.startswith(WORKSPACE_ROOT + os.sep):
        return None, f"{path} is outside the workspace — not sendable."
    if any(p in real.lower() for p in _DENY_PARTS):
        return None, f"{path} looks like credentials — never sendable."
    if not os.path.isfile(real):
        return None, f"{path} does not exist. Use find_files to get the exact path."
    if os.path.getsize(real) > SEND_MAX_BYTES:
        return None, f"{path} exceeds the 50 MB upload limit."
    return real, ""


def t_find_files(query, limit=8):
    """Filename search under the workspace; ranked by terms matched, newest first."""
    terms = [w for w in re.findall(r"[a-zA-Z0-9][a-zA-Z0-9.&'_-]{2,}", query.lower())
             if w not in _STOP][:6]
    if not terms:
        return "Query had no searchable terms."
    hits = []
    for root, dirs, files in os.walk(WORKSPACE_ROOT):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for f in files:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, WORKSPACE_ROOT)
            m = sum(1 for t in terms if t in rel.lower())
            if m:
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                hits.append((m, st.st_mtime, rel, st.st_size))
    hits.sort(key=lambda h: (-h[0], -h[1]))
    return [{"path": rel, "size_kb": round(size / 1024, 1),
             "modified": time.strftime("%Y-%m-%d", time.localtime(mt))}
            for m, mt, rel, size in hits[:min(int(limit or 8), 15)]
            ] or "No files matched those terms."


MEDIA_SEARCH_URL = os.environ.get("MEDIA_SEARCH_URL", "http://127.0.0.1:8477")


def t_find_media(query, limit=4):
    """Semantic photo/video search via a local CLIP server (see the
    clip-media-search skill). Filename search misses these — 'PHD board' never
    matches 'phd-connect-32-v1.5-a.jpeg'."""
    import urllib.request, urllib.parse
    k = min(int(limit or 4), 8)
    url = (MEDIA_SEARCH_URL + "/find?" +
           urllib.parse.urlencode({"q": query, "k": k}))
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        return f"Media search unavailable ({e}) — fall back to find_files."
    out = [{"path": os.path.relpath(h["path"], DST_ROOT),
            "annotation": (h.get("annotation") or "")[:300],
            "score": round(h.get("score", 0), 2)}
           for h in data.get("results", []) if h.get("score", 0) >= 0.75]
    return out or "No matching photos/videos in the media KB."


def t_send_file(path, caption=""):
    """Queue a file; the gateway uploads it to the chat after the loop finishes."""
    real, err = _sendable(path)
    if not real:
        return err
    _pending_files.append({"path": real, "caption": (caption or "")[:900]})
    return (f"OK — {os.path.relpath(real, WORKSPACE_ROOT)} will be attached to your reply. "
            "Do not paste its contents; just tell the owner what you are sending.")


# ---- scheduling (reminders.py in this dir) ----------------------------------
# Without this, the model *pretends* to schedule ("I'll ping you at 5pm") and
# nothing happens — the same hallucinated-capability failure the tool loop fixes
# for lookups. Reminders fire into the chat of the current turn; run() sets this.
_current_chat = int(os.environ.get("TG_OWNER_ID", "0"))   # 0 = unknown


def t_schedule_reminder(when, text, kind="ping"):
    """Queue a future ping/task in the shared reminder queue (a local SQLite insert;
    the per-minute cron runner fires it)."""
    import reminders
    if not _current_chat:
        return "FAILED: no target chat known (set TG_OWNER_ID or pass chat_id to run())."
    now = time.time()
    try:
        when_epoch = time.mktime(time.strptime((when or "").strip(), "%Y-%m-%d %H:%M"))
    except ValueError:
        return (f"FAILED: bad time {when!r}. Use 'YYYY-MM-DD HH:MM' (24-hour, local time). "
                f"Right now it is {time.strftime('%Y-%m-%d %H:%M')}.")
    if when_epoch < now + 60:
        return (f"FAILED: {when} is in the past (now: {time.strftime('%Y-%m-%d %H:%M')}). "
                "Ask the sender for the intended time if unsure.")
    if kind not in ("ping", "task"):
        return "FAILED: kind must be 'ping' or 'task'."
    rid, when_local = reminders.add(when, _current_chat, kind, text,
                                    created_by="private-agent")
    return (f"Scheduled: reminder #{rid} will fire at {when_local} in this chat "
            f"({'the message will be sent verbatim' if kind == 'ping' else 'the instruction will be executed then and the result posted'}). "
            "Confirm this to the user, including the time.")


def t_list_reminders():
    """Pending reminders for the current chat."""
    import reminders
    rows = [r for r in reminders.list_rows() if r["chat_id"] == _current_chat]
    return rows or "No pending reminders for this chat."


def t_cancel_reminder(reminder_id):
    """Cancel a pending reminder (only for the current chat)."""
    import reminders
    rows = [r for r in reminders.list_rows() if r["chat_id"] == _current_chat
            and r["id"] == int(reminder_id)]
    if not rows:
        return f"No pending reminder #{reminder_id} in this chat (use list_reminders)."
    return ("Cancelled." if reminders.cancel(int(reminder_id))
            else "Could not cancel — already fired or cancelled.")


TOOLS = {
    "search_contacts": (t_search_contacts, "Search CRM contacts by name/company/email. "
                        "Returns contact info + a rolling summary of all dealings with them.",
                        {"query": {"type": "string", "description": "name, company or email fragment"}}),
    "search_emails": (t_search_emails, "Keyword-search the archived email store (subjects+bodies). "
                      "Returns matching emails with ids and excerpts, best matches first.",
                      {"query": {"type": "string", "description": "keywords, e.g. 'acme refund'"},
                       "limit": {"type": "integer", "description": "max results (default 5)"}}),
    "read_email": (t_read_email, "Fetch the full body of one archived email by its id "
                   "(get ids from search_emails).",
                   {"email_id": {"type": "string", "description": "email id"}}),
    "kb_search": (t_kb_search, "Semantic search over the company knowledge base "
                  "(specs, prices, policies, extracted email knowledge).",
                  {"query": {"type": "string", "description": "natural-language question"}}),
    "find_files": (t_find_files, "Search the workspace for files by NAME — invoices, "
                   "PDFs, images, price lists, reports. Returns relative paths with size "
                   "and date, best match first.",
                   {"query": {"type": "string",
                              "description": "filename keywords, e.g. 'invoice acme pdf'"},
                    "limit": {"type": "integer", "description": "max results (default 8)"}}),
    "find_media": (t_find_media, "Find PHOTOS or VIDEOS of products/equipment by what "
                   "they SHOW (semantic content search of the media KB, runs on-box). "
                   "ALWAYS use this — never find_files — when asked for a picture, "
                   "photo, image or video of something. Returns paths + descriptions; "
                   "deliver hits with send_file, using each annotation as the caption.",
                   {"query": {"type": "string",
                              "description": "what the picture should show"},
                    "limit": {"type": "integer", "description": "max results (default 4)"}}),
    "send_file": (t_send_file, "Attach a file to your reply — the owner receives it in "
                  "the chat. Private/confidential company documents are fine in this "
                  "chat. Use find_files first to get the exact path.",
                  {"path": {"type": "string", "description": "path from find_files"},
                   "caption": {"type": "string", "description": "optional short caption"}}),
    "schedule_reminder": (t_schedule_reminder, "Schedule a FUTURE action ('remind me at "
                          "5pm', 'ping me tomorrow at 9', 'check later whether...'). You "
                          "do NOT run between messages — a promise to ping/check later is "
                          "a lie unless you call this tool. kind 'ping': text is sent to "
                          "this chat verbatim at that time. kind 'task': text is an "
                          "INSTRUCTION executed at that time by an agent with the same "
                          "email/CRM/KB lookup tools, which posts its findings — use for "
                          "conditional reminders ('at 17:00 check whether we replied to "
                          "X; report the status'); phrase the instruction self-contained "
                          "with full names/emails, since the executor has no chat "
                          "history. After calling, confirm the reminder id and exact "
                          "time to the user.",
                          {"when": {"type": "string",
                                    "description": "fire time, 'YYYY-MM-DD HH:MM' 24-hour "
                                                   "local time; the current date/time is "
                                                   "in your context"},
                           "text": {"type": "string",
                                    "description": "ping message, or self-contained task "
                                                   "instruction"},
                           "kind": {"type": "string",
                                    "description": "'ping' (default) or 'task'"}}),
    "list_reminders": (t_list_reminders, "List pending scheduled reminders for this chat "
                       "(id, time, kind, text). Use before cancelling, or when asked "
                       "what is scheduled.", {}),
    "cancel_reminder": (t_cancel_reminder, "Cancel a pending reminder by id (see "
                        "list_reminders).",
                        {"reminder_id": {"type": "integer",
                                         "description": "id from list_reminders"}}),
}


def _tool_schemas():
    return [{"type": "function",
             "function": {"name": name, "description": desc,
                          "parameters": {"type": "object",
                                         "properties": props,
                                         "required": [next(iter(props))] if props else []}}}
            for name, (fn, desc, props) in TOOLS.items()]


def _call_api(messages, key, timeout=45):
    payload = {"model": OR_MODEL, "temperature": 0.0, "max_tokens": 900,
               "reasoning": {"enabled": False},
               "tools": _tool_schemas(),
               "messages": messages}
    req = urllib.request.Request(OR_URL, data=json.dumps(payload).encode(),
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())["choices"][0]["message"]


SYSTEM = (
    "You are the PRIVATE assistant of the company. You run on-box and are trusted with "
    "confidential company data. You are mid-conversation with the owner; the recent "
    "chat history is provided for context.\n"
    "You HAVE lookup tools — use them: search_contacts / search_emails / read_email for "
    "customer matters, kb_search for product facts. When the owner asks for an actual "
    "document (a PDF, invoice, image, price list, report), use find_files to locate it "
    "and send_file to attach it; for a PICTURE/PHOTO/VIDEO of something use find_media "
    "(searches what images show; find_files only matches filenames) and send_file each "
    "hit with its annotation as the caption — this chat is private and trusted, so confidential "
    "company files may be sent here. Call tools as needed (several rounds are fine) "
    "BEFORE answering. When you have enough, give the final answer: concise, factual, "
    "grounded in what the tools returned. If the data truly isn't there, say exactly "
    "what you looked for and what's missing. Never invent facts.\n"
    "FUTURE actions ('remind me at 5pm', 'ping me tomorrow', 'check this evening "
    "whether...'): you do not run between messages, so call schedule_reminder — kind "
    "'ping' for a plain reminder message, kind 'task' for a check to perform at that "
    "time (write the instruction self-contained, with full names and addresses). NEVER "
    "answer 'I will ping you / check later' without a successful schedule_reminder "
    "call in THIS turn — an unscheduled promise is a lie. Confirm the scheduled time "
    "in your reply.\n"
    "SOCIAL messages (greetings, thanks, congratulations, small talk) need no tools — "
    "just reply warmly and briefly like any assistant would. Whatever the message: "
    "NEVER narrate your reasoning or classification ('this is praise, no tool use is "
    "required, I will respond appropriately') — output ONLY the reply itself, exactly "
    "as it should appear in the chat."
)


def run(question, history="", chat_id=None):
    """Tool-calling loop. Returns (answer_text, files_to_send) where files_to_send is
    a list of {path, caption} the gateway should upload to the chat. Raises on hard
    failure."""
    global _current_chat
    if chat_id:
        _current_chat = int(chat_id)
    key = _load_key()
    if not key:
        raise RuntimeError("no API key (set PRIVATE_LLM_KEY or PRIVATE_LLM_KEY_FILE)")
    del _pending_files[:]
    # The model needs today's date to resolve "tomorrow at 9" into an absolute time.
    user = f"Current date and time: {time.strftime('%A %Y-%m-%d %H:%M')} (local).\n\n"
    if history.strip():
        user += f"Recent conversation (oldest first):\n{history.strip()}\n\n"
    user += f"Owner's message: {question}"
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": user}]
    deadline = time.time() + WALL_DEADLINE
    for turn in range(MAX_TURNS):
        msg = _call_api(messages, key, timeout=min(45, max(5, deadline - time.time())))
        calls = msg.get("tool_calls") or []
        if not calls:
            return ((msg.get("content") or "").strip() or "(the model returned no text)",
                    list(_pending_files))
        messages.append({"role": "assistant", "content": msg.get("content") or "",
                         "tool_calls": calls})
        for tc in calls:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except Exception:
                args = {}
            _log(f"turn {turn + 1}: {name}({json.dumps(args)[:120]})")
            fn = TOOLS.get(name, (None,))[0]
            try:
                result = fn(**args) if fn else f"Unknown tool {name}"
            except Exception as e:
                result = f"Tool error: {e}"
            messages.append({"role": "tool", "tool_call_id": tc.get("id", name),
                             "content": json.dumps(result, default=str)[:8000]})
        if time.time() > deadline - 10:
            messages.append({"role": "user", "content":
                             "Time is up — answer NOW from what you already gathered."})
    # Loop exhausted: force a final answer from gathered context.
    messages.append({"role": "user", "content":
                     "Stop using tools. Give your best final answer from what you gathered."})
    msg = _call_api(messages, key, timeout=30)
    return ((msg.get("content") or "").strip() or "(no answer after tool loop)",
            list(_pending_files))
