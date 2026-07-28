"""On-box tool-calling agent loop for the PRIVATE path (gate #3, mode A).

Nemotron (via OpenRouter today, DGX Spark later) answers private queries agentically:
it can call read-only lookup tools — CRM contacts, the archived-email store, the KB
semantic index — in a loop until it has enough to answer. Every tool executes ON-BOX;
the only thing leaving the machine is the model call itself, same as before.

Design constraints:
  • Tools are READ-ONLY lookups plus file DELIVERY (find_files/send_file). No writes,
    no shell. send_file only queues a path — the gateway does the actual Telegram
    upload after the loop, so this process never touches the network beyond the model.
  • Hard caps: MAX_TURNS loop iterations, WALL_DEADLINE seconds overall — a stuck
    loop degrades to "couldn't finish", never hangs the gateway.
  • Fail closed: any error returns a plain explanation; the caller never falls
    through to the cloud.
"""
import os, sys, json, time, sqlite3, re, subprocess, urllib.request, urllib.error

KB_DIR = os.path.dirname(os.path.abspath(__file__))
DST_ROOT = os.path.dirname(os.path.dirname(KB_DIR))
sys.path.insert(0, KB_DIR)

from kb_index import retrieve
from kb_answer import _load_key, OR_MODEL, OR_URL

MAX_TURNS = 8          # max model calls (i.e. up to 7 rounds of tool use; PDF edits
                       # need read → edit (with a retry or two) → send — 6 was too few)
WALL_DEADLINE = 200    # seconds for the whole loop (gateway kills the subprocess at
                       # 240 — web fetches via Chrome can take 60-75s each, so the old
                       # 120 couldn't fit fetch_page + fetch_image in one turn)
CONTACTS_DB = os.path.join(DST_ROOT, "crm", "contacts.db")

_STOP = {"what", "why", "how", "when", "where", "who", "did", "does", "do", "the",
         "a", "an", "is", "are", "was", "were", "with", "from", "about", "have",
         "has", "had", "want", "wanted", "will", "would", "our", "their", "them",
         "they", "this", "that", "much", "many", "get", "got", "can", "could",
         "please", "tell", "show", "give", "know", "need", "customer", "client"}


def _log(msg):
    print(f"[private-agent] {msg}", file=sys.stderr, flush=True)


# ---- tools (all read-only, all on-box) --------------------------------------
def t_search_contacts(query):
    """LIKE-match contacts by name/company/email; return rolling summaries."""
    out = []
    db = sqlite3.connect(CONTACTS_DB)
    for term in [w for w in re.findall(r"[a-zA-Z][a-zA-Z0-9&'-]{2,}", query.lower())
                 if w not in _STOP][:4] or [query.lower()]:
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
    terms = [w for w in re.findall(r"[a-zA-Z][a-zA-Z0-9&'-]{2,}", query.lower())
             if w not in _STOP][:6]
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
    r = db.execute("SELECT date, from_addr, to_addr, cc, subject, body, has_attachment, "
                   "attachments FROM emails WHERE id=?", (str(email_id),)).fetchone()
    if not r:
        return f"No email with id {email_id}."
    date, frm, to, cc, subj, body, has_att, att = r
    try:
        att_names = [os.path.basename(p) for p in json.loads(att or "[]")]
    except Exception:
        att_names = []
    return {"date": date, "from": frm, "to": to, "cc": cc, "subject": subj,
            "attachments": (att_names or
                            ("yes — call read_attachment to fetch and read them"
                             if has_att else "none")),
            "body": (body or "")[:6000]}


# Attachment content is extracted at INGEST time by attach_text.py (cron) into the
# attachments table + FTS5 index — reads here are instant. ensure_email() only does
# live fetch/extract work for mail the ingest cron hasn't reached yet.
import attach_text


def t_read_attachment(email_id, name=None):
    """Pre-extracted attachment content for one email (extracts on the spot only if
    the ingest cron hasn't processed it yet)."""
    try:
        rows = attach_text.ensure_email(str(email_id))
    except Exception as e:
        return f"Attachment lookup failed: {e}"
    rows = [r for r in rows if not r["path"].startswith(("none:", "gmail:"))]
    if not rows:
        return "That email has no readable attachments."
    if name:
        rows = ([r for r in rows if name.lower() in r["filename"].lower()] or rows)
    return [{"filename": r["filename"], "saved_at": r["path"], "method": r["method"],
             "text": " ".join((r["text"] or "").split())[:6000] or "(no extractable text)"}
            for r in rows[:4]]


def t_search_attachments(query, limit=6):
    """Full-text search over ALL extracted attachment content (POs, invoices, specs)."""
    try:
        return (attach_text.search(query, k=min(int(limit or 6), 10))
                or "No attachment content matched those terms.")
    except Exception as e:
        return f"Attachment search failed: {e}"


def t_kb_search(query):
    """Semantic search over the product/company knowledge base."""
    hits = retrieve(query, k=5)
    return [{"source": h["source"], "score": round(h["score"], 3),
             "text": h["text"][:800]} for h in hits] or "No KB matches."


# ---- file delivery (Vlad 2026-07-08: the private chat must be able to hand over
# actual documents — PDFs, invoices, images — not just talk about them). The chat is
# allowlisted and private; sending confidential DST files there is accepted policy.
# Credentials are the one thing that must never leave the box, so those stay blocked.
SEND_MAX_BYTES = 49 * 1024 * 1024      # Telegram bot upload cap is 50 MB
_DENY_PARTS = ("token", "secret", "credential", "password", "bot_token",
               os.sep + ".git" + os.sep, os.sep + "venv" + os.sep,
               # Vlad's personal notes (2026-07-10) — never surfaced or sent by this
               # agent; delivery only via the gateway's personal_notes gate.
               os.sep + "personal" + os.sep)
# telegram/inbox stays skipped by find_files: KB-ingested uploads are COPIED into
# knowledge-base/uploads/ by the gateway at ingest time (Vlad 2026-07-24 — "copy
# KB files to our KB directory, not rely on telegram inbox"), so the searchable
# home for an upload is the KB tree, and DM/held files stay private.
_SKIP_DIRS = {".git", "venv", "__pycache__", "node_modules", "logs", "state",
              ".claude", "inject", "inbox", "personal"}
_pending_files = []


def _sendable(path):
    """Return (real_path, error). A file is sendable only if it resolves inside the
    DST workspace, exists, fits Telegram's upload cap and isn't credential-like."""
    real = os.path.realpath(path if os.path.isabs(path)
                            else os.path.join(DST_ROOT, path))
    if not real.startswith(DST_ROOT + os.sep):
        return None, f"{path} is outside the DST workspace — not sendable."
    if any(p in real.lower() for p in _DENY_PARTS):
        return None, f"{path} is protected (credentials or personal) — never sendable."
    if not os.path.isfile(real):
        return None, f"{path} does not exist. Use find_files to get the exact path."
    if os.path.getsize(real) > SEND_MAX_BYTES:
        return None, f"{path} exceeds Telegram's 50 MB upload limit."
    return real, ""


def t_find_files(query, limit=8):
    """Filename search under the DST workspace; ranked by terms matched, newest first."""
    terms = [w for w in re.findall(r"[a-zA-Z0-9][a-zA-Z0-9.&'_-]{2,}", query.lower())
             if w not in _STOP][:6]
    if not terms:
        return "Query had no searchable terms."
    hits = []
    for root, dirs, files in os.walk(DST_ROOT):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith((".", "venv"))]
        for f in files:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, DST_ROOT)
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


def t_find_media(query, limit=4):
    """Semantic photo/video search via the local CLIP server (annotation-aware,
    fully on-box). Filename search misses these — 'PHD board' never matches
    'phd-connect-32-v1.5-a.jpeg' (Vlad 2026-07-21, DST Private)."""
    import urllib.request, urllib.parse
    k = min(int(limit or 4), 8)
    url = ("http://127.0.0.1:8477/find?" +
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
    return (f"OK — {os.path.relpath(real, DST_ROOT)} will be attached to your reply. "
            "Do not paste its contents; just tell the user what you are sending.")


# PDF read/edit (Vlad 2026-07-27: "we need to give Nemotron pdf editing capabilities"
# — after it refused to bump a quantity on a proforma invoice in the Private group).
# read_pdf shows the text so the model can quote exact strings + occurrence numbers;
# edit_pdf does redact+reinsert find/replace fully on-box (pdf_edit.py, PyMuPDF) and
# saves an edited COPY into knowledge-base/uploads/ — the original is never touched —
# deliverable via send_file.
def t_read_pdf(path):
    real, err = _sendable(path)
    if not real:
        return err
    if not real.lower().endswith(".pdf"):
        return f"{path} is not a PDF."
    import pdf_edit
    try:
        return pdf_edit.extract_text(real)
    except Exception as e:
        return f"Could not read that PDF: {e}"


def t_edit_pdf(path, edits):
    real, err = _sendable(path)
    if not real:
        return err
    if not real.lower().endswith(".pdf"):
        return f"{path} is not a PDF."
    if isinstance(edits, str):
        try:
            edits = json.loads(edits)
        except Exception:
            return ('edits must be a JSON array like '
                    '[{"find": "1.00", "replace": "2.00", "near": "GEN5"}]')
    if isinstance(edits, dict):
        edits = [edits]
    if not isinstance(edits, list) or not edits:
        return "edits must be a non-empty array of {find, replace, occurrence} objects."
    import pdf_edit
    stem = os.path.splitext(os.path.basename(real))[0]
    stem = re.sub(r"(-edited(-\d+)?)+$", "", stem) or stem
    out = os.path.join(DST_ROOT, "knowledge-base", "uploads", stem + "-edited.pdf")
    if os.path.realpath(out) == real:
        out = os.path.join(os.path.dirname(out), stem + "-edited-2.pdf")
    try:
        rep = pdf_edit.apply_edits(real, edits, out)
    except Exception as e:
        return f"PDF edit failed: {e}"
    rel = os.path.relpath(out, DST_ROOT)
    return {"saved": rel, "edits": rep["edits"],
            "next": ("Edited copy saved (original untouched). Check each "
                     "rows_after_edit line: the changed value must sit under the "
                     "column the user wanted changed. Rows listed under "
                     "same_value_elsewhere_left_unchanged were NOT touched — that "
                     "is usually correct (e.g. a unit price that stays the same); "
                     "edit them ONLY if the user's request requires it. If a row "
                     "changed wrongly, redo from the ORIGINAL file with a corrected "
                     "'column'. When the rows read right, call send_file with path "
                     f"'{rel}' to deliver it.")}


# Web fetch (Vlad 2026-07-27: "give Nemotron a web fetch tool" — after it couldn't
# grab the CPC fitting photos). Fetching/rendering is deterministic headless-Chrome
# code ON-BOX; only the extracted text enters the model context. Outbound HTTP to a
# public URL the owner posted leaks nothing private. Guard: public http(s) only.
WEB_FETCH_DIR = "/tmp/web_fetch"
_CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _public_http_url(url):
    """(ok, err) — allow only http(s) to a public host (no localhost/RFC1918)."""
    import urllib.parse, socket, ipaddress
    try:
        p = urllib.parse.urlparse((url or "").strip())
        if p.scheme not in ("http", "https") or not p.hostname:
            return False, "Only public http(s) URLs can be fetched."
        for info in socket.getaddrinfo(p.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False, f"{p.hostname} resolves to a non-public address — refused."
        return True, ""
    except Exception as e:
        return False, f"Bad URL ({e})."


def t_fetch_page(url):
    """Render a page with headless Chrome (real-browser UA beats TLS/UA WAFs that
    block curl) and return its readable text, capped for the model context."""
    ok, err = _public_http_url(url)
    if not ok:
        return err
    try:
        r = subprocess.run(
            ["google-chrome", "--headless=new", "--no-sandbox", "--disable-gpu",
             "--dump-dom", "--virtual-time-budget=20000",
             f"--user-agent={_CHROME_UA}", url.strip()],
            capture_output=True, text=True, timeout=60)
        html = r.stdout or ""
    except subprocess.TimeoutExpired:
        return "Page fetch timed out after 60s."
    except Exception as e:
        return f"Page fetch failed: {e}"
    if len(html) < 500:
        return "Fetch returned almost nothing — the site likely blocked the request."
    import html as _h
    txt = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html)
    txt = re.sub(r"(?s)<[^>]+>", "\n", txt)
    txt = _h.unescape(txt)
    lines, seen = [], set()
    for ln in (l.strip() for l in txt.splitlines()):
        if len(ln) < 3 or ln in seen:
            continue
        seen.add(ln)
        lines.append(ln)
    body = "\n".join(lines)
    if len(body) > 8000:
        body = body[:8000] + "\n…[truncated]"
    return body or "No readable text extracted."


def t_fetch_image(page_url, match):
    """Capture an image from a web page (WAF/CORS-proof: bytes come off Chrome's own
    network stack) into /tmp/web_fetch/ and queue it for delivery to the chat.
    Returns the saved path — pass it to save_image_to_kb to keep it permanently."""
    ok, err = _public_http_url(page_url)
    if not ok:
        return err
    match = re.sub(r"[^A-Za-z0-9._-]", "", (match or "").strip())
    if len(match) < 3:
        return "Give a distinctive filename fragment of the image URL (e.g. a part number)."
    os.makedirs(WEB_FETCH_DIR, exist_ok=True)
    out = os.path.join(WEB_FETCH_DIR, f"{match}.jpg")
    script = os.path.join(DST_ROOT, "local-ai", "web_image_fetch.py")
    try:
        r = subprocess.run([sys.executable, script, page_url.strip(), match, out],
                           capture_output=True, text=True, timeout=75)
    except subprocess.TimeoutExpired:
        return "Image fetch timed out after 75s."
    if not (r.returncode == 0 and os.path.exists(out)):
        return (f"No image matching '{match}' loaded on that page. Check the fragment "
                f"against the image's URL. [{(r.stdout or r.stderr)[:200].strip()}]")
    _pending_files.append({"path": out, "caption": f"Fetched from {page_url.strip()}"[:900]})
    return (f"OK — image saved to {out} and it will be attached to your reply. "
            "If the owner wants it kept in the KB, also call save_image_to_kb.")


def t_save_image_to_kb(path, annotation, tags=""):
    """Add a fetched image to the product media KB (CLIP-indexed, searchable,
    photo-reflex-served). Only files fetched this session or already in the workspace."""
    path = (path or "").strip()
    real = os.path.realpath(path)
    if not (real.startswith(WEB_FETCH_DIR + os.sep) or real.startswith(DST_ROOT + os.sep)):
        return "Refused — path is outside the fetched-images dir and the workspace."
    if not os.path.isfile(real):
        return f"No such file: {path}"
    if not (annotation or "").strip():
        return "An annotation (what the image shows, per the owner) is required."
    media = os.path.join(DST_ROOT, "local-ai", "media")
    cmd = [media, "add", real, "--annotation", annotation.strip()[:600]]
    if (tags or "").strip():
        cmd += ["--tags", tags.strip()[:200]]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return "media add timed out."
    if r.returncode != 0:
        return f"media add failed: {(r.stderr or r.stdout)[:300]}"
    return ("Saved to the media KB with that annotation — it is now searchable and "
            "can be shown on request. Confirm to the owner what was saved.")


# Diagram rendering (Vlad 2026-07-20: "I need proper image diagrams in the private
# group"). The model writes SVG (text — stays on the private path); headless Chrome
# renders it to PNG on-box; the PNG is queued for delivery like any private file.
# SVG sources are kept so a later turn can revise a diagram instead of redrawing blind.
DIAGRAMS_DIR = os.path.join(DST_ROOT, "diagrams", "private")


def t_render_diagram(source, format="wiring", name="", caption=""):
    """Render a wiring JSON (deterministic layout), Graphviz DOT, or raw SVG to PNG;
    queue the PNG for the chat."""
    source = (source or "").strip()
    if not source:
        return "Not rendered — 'source' was empty."
    fmt = (format or "").lower()
    if source.startswith("<"):
        fmt = "svg"
    elif source.startswith("{"):
        fmt = "wiring"
    elif fmt not in ("wiring", "dot", "svg"):
        fmt = "dot" if re.match(r"^(strict\s+)?(di)?graph\b", source) else "wiring"
    if fmt == "svg" and "<svg" not in source:
        return "Not rendered — format 'svg' needs complete SVG markup (<svg ...>)."
    os.makedirs(DIAGRAMS_DIR, exist_ok=True)
    slug = re.sub(r"[^a-z0-9-]+", "-", (name or "diagram").lower()).strip("-") or "diagram"
    base = f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}"
    ext = {"wiring": "json"}.get(fmt, fmt)
    src_path = os.path.join(DIAGRAMS_DIR, f"{base}.{ext}")
    png_path = os.path.join(DIAGRAMS_DIR, base + ".png")
    with open(src_path, "w") as f:
        f.write(source)
    if fmt == "wiring":
        # The model supplies pure DATA (components + wire list); all geometry is done
        # here deterministically — the LLM never places coordinates.
        import wiring_render
        try:
            svg = wiring_render.render(json.loads(source))
        except ValueError as e:
            return f"Wiring diagram rejected: {e} — fix the JSON and call render_diagram again."
        except json.JSONDecodeError as e:
            return f"Wiring source was not valid JSON ({e}) — fix and call render_diagram again."
        fmt = "svg"
        src_svg = os.path.join(DIAGRAMS_DIR, base + ".render.svg")
        with open(src_svg, "w") as f:
            f.write(svg)
        source, render_src = svg, src_svg
    else:
        render_src = src_path
    try:
        if fmt == "dot":
            r = subprocess.run(["dot", "-Tpng", "-Gdpi=130", src_path, "-o", png_path],
                               capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                return (f"Graphviz rejected the DOT source: {r.stderr.strip()[:400]} "
                        "— fix the syntax and call render_diagram again.")
        else:
            m = re.search(r'<svg[^>]*\bwidth="(\d+)[^"]*"[^>]*\bheight="(\d+)', source)
            w, h = (m.group(1), m.group(2)) if m else ("1200", "900")
            subprocess.run(["google-chrome", "--headless", "--disable-gpu",
                            f"--screenshot={png_path}", f"--window-size={w},{h}",
                            "--default-background-color=FFFFFFFF", f"file://{render_src}"],
                           capture_output=True, timeout=45)
    except Exception as e:
        return f"Render failed: {e}"
    if not os.path.isfile(png_path) or os.path.getsize(png_path) < 2000:
        return "Render failed — no usable PNG produced. Check the diagram source is valid."
    _pending_files.append({"path": png_path, "caption": (caption or "")[:900]})
    return (f"OK — diagram rendered and it will be attached to your reply as an image. "
            f"Source saved as {base}.{ext} — to revise this diagram later, call "
            "read_diagram to get the source, modify it, and render again. In your "
            "reply text just describe the diagram briefly; do not repeat the source.")


def t_read_diagram(filename=""):
    """Return the source (DOT or SVG) of a previously rendered diagram (newest first)."""
    try:
        srcs = sorted((f for f in os.listdir(DIAGRAMS_DIR)
                       if f.endswith((".svg", ".dot", ".json"))
                       and not f.endswith(".render.svg")), reverse=True)
    except OSError:
        srcs = []
    if not srcs:
        return "No diagrams have been rendered yet."
    if filename:
        want = re.sub(r"\.(svg|dot|json|png)$", "", filename.lower())
        srcs = [f for f in srcs if want in f.lower()] or srcs
    chosen = srcs[0]
    with open(os.path.join(DIAGRAMS_DIR, chosen)) as f:
        src = f.read()[:14000]
    others = ", ".join(srcs[1:6]) or "none"
    fmt = {"json": "wiring"}.get(chosen.rsplit(".", 1)[-1], chosen.rsplit(".", 1)[-1])
    return {"filename": chosen, "format": fmt,
            "source": src, "other_recent_diagrams": others}


# Private memory store: facts the owners ask the agent to remember. Lives under
# knowledge-base/private/ — NOT in public_paths (data-classification default =
# private), so everything saved here stays on-box (Vlad, 2026-07-17).
MEMORY_FILE = os.path.join(DST_ROOT, "knowledge-base", "private", "nemotron-memory.md")
_current_sender = "Vladimir"   # set by run(); recorded next to each saved fact
_current_chat = 551954852      # set by run(); reminders fire into this chat (default: Vlad's DM)


def t_save_memory(fact, keywords=""):
    """Append a fact to the private memory file (creates it on first use)."""
    fact = (fact or "").strip()
    if not fact:
        return "Nothing to save — 'fact' was empty."
    os.makedirs(os.path.dirname(MEMORY_FILE), exist_ok=True)
    new = not os.path.exists(MEMORY_FILE)
    with open(MEMORY_FILE, "a") as f:
        if new:
            f.write("# Nemotron private memory\n\n"
                    "Facts the DST owners asked the private assistant to remember.\n"
                    "PRIVATE (Vlad, 2026-07-17): never list this file in public_paths;\n"
                    "contents must only be used on-box / in private chats.\n\n")
        line = f"- {time.strftime('%Y-%m-%d')} (from {_current_sender}): {fact}"
        if (keywords or "").strip():
            line += f"  [keywords: {keywords.strip()}]"
        f.write(line + "\n")
    return (f"Saved to private memory: \"{fact}\". It is stored on-box "
            "(knowledge-base/private/nemotron-memory.md) and treated as private data.")


def t_schedule_reminder(when, text, kind="ping"):
    """Queue a future ping/task in the shared reminder queue (operations/reminders).
    The per-minute cron runner fires it; this call itself is on-box (SQLite insert)."""
    sys.path.insert(0, os.path.join(DST_ROOT, "operations", "reminders"))
    import reminders
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
    rid, when_local = reminders.add(when, _current_chat, kind, text, created_by="nemotron")
    return (f"Scheduled: reminder #{rid} will fire at {when_local} in this chat "
            f"({'the message will be sent verbatim' if kind == 'ping' else 'the instruction will be executed then and the result posted'}). "
            "Confirm this to the user, including the time.")


def t_list_reminders():
    """Pending reminders for the current chat."""
    sys.path.insert(0, os.path.join(DST_ROOT, "operations", "reminders"))
    import reminders
    rows = [r for r in reminders.list_rows() if r["chat_id"] == _current_chat]
    return rows or "No pending reminders for this chat."


def t_cancel_reminder(reminder_id):
    """Cancel a pending reminder (only for the current chat)."""
    sys.path.insert(0, os.path.join(DST_ROOT, "operations", "reminders"))
    import reminders
    rows = [r for r in reminders.list_rows() if r["chat_id"] == _current_chat
            and r["id"] == int(reminder_id)]
    if not rows:
        return f"No pending reminder #{reminder_id} in this chat (use list_reminders)."
    return ("Cancelled." if reminders.cancel(int(reminder_id))
            else "Could not cancel — already fired or cancelled.")


TOOLS = {
    "fetch_page": (t_fetch_page, "Fetch a PUBLIC web page (URL the owner gave or one "
                   "you know) and get its readable text — rendered in a real browser "
                   "on-box, so JS pages and anti-bot walls work. Use when the answer "
                   "needs current content from a specific site (product page, spec, "
                   "price). Extract only what was asked; never paste the whole dump.",
                   {"url": {"type": "string", "description": "full http(s) URL"}}),
    "fetch_image": (t_fetch_image, "Download an image from a public web page and "
                    "attach it to your reply. 'match' is a distinctive fragment of the "
                    "image's URL/filename — usually the part number on a product page "
                    "(e.g. 'PMC100212'). If unsure of the fragment, call fetch_page "
                    "first and look at the image URLs. To keep the image permanently, "
                    "follow up with save_image_to_kb.",
                    {"page_url": {"type": "string", "description": "the page containing the image"},
                     "match": {"type": "string",
                               "description": "filename fragment identifying the image, e.g. a part number"}}),
    "save_image_to_kb": (t_save_image_to_kb, "Save a fetched image into the product "
                         "media KB so it becomes searchable and can be shown later. "
                         "Use after fetch_image when the owner wants the picture kept. "
                         "The annotation is the owner's own description of the item.",
                         {"path": {"type": "string", "description": "path returned by fetch_image"},
                          "annotation": {"type": "string",
                                         "description": "what the image shows — part numbers and usage, verbatim from the owner where possible"},
                          "tags": {"type": "string",
                                   "description": "optional comma-separated keywords (part numbers, product models)"}}),
    "save_memory": (t_save_memory, "REMEMBER a fact permanently. Use when Vladimir or "
                    "Marina EXPLICITLY asks to remember/store/save information ('remember "
                    "that...', 'save to memory...') OR states a fact for the record (a "
                    "declarative message about a part, product, supplier or decision — "
                    "keep part numbers, quantities and URLs verbatim in the saved fact). "
                    "NEVER use it for an instruction to do "
                    "work (make/redo/change/fix/produce something) — carry that out in your "
                    "reply instead. Saves on-box as PRIVATE data. After calling, "
                    "confirm to the user exactly what was saved.",
                    {"fact": {"type": "string",
                              "description": "the fact to remember, as one self-contained sentence"},
                     "keywords": {"type": "string",
                                  "description": "optional comma-separated search keywords"}}),
    "search_contacts": (t_search_contacts, "Search CRM contacts by name/company/email. "
                        "Returns contact info + a rolling summary of all dealings with them.",
                        {"query": {"type": "string", "description": "name, company or email fragment"}}),
    "search_emails": (t_search_emails, "Keyword-search the archived email store (subjects+bodies). "
                      "Returns matching emails with ids and excerpts, best matches first.",
                      {"query": {"type": "string", "description": "keywords, e.g. 'snuggle refund'"},
                       "limit": {"type": "integer", "description": "max results (default 5)"}}),
    "read_email": (t_read_email, "Fetch the full body of one archived email by its id "
                   "(get ids from search_emails).",
                   {"email_id": {"type": "string", "description": "email id"}}),
    "read_attachment": (t_read_attachment, "Download (if needed) and READ the text of an "
                        "email's attachments (PDF/CSV/TXT). Use when the answer is inside "
                        "an attached document — PO, invoice, quote, spec sheet; email "
                        "bodies often just say 'see attached'. Returns extracted text plus "
                        "the saved path (deliverable via send_file).",
                        {"email_id": {"type": "string",
                                      "description": "email id from search_emails"},
                         "name": {"type": "string",
                                  "description": "optional filename filter"}}),
    "search_attachments": (t_search_attachments, "Full-text search INSIDE all email "
                           "attachments (extracted text of every PO, invoice, quote, "
                           "spec, OCR'd scan). Use when the detail you need is in a "
                           "document rather than an email body — e.g. a PO number, an "
                           "item on an invoice, a spec value.",
                           {"query": {"type": "string",
                                      "description": "keywords, e.g. 'PHD17 adapter'"},
                            "limit": {"type": "integer",
                                      "description": "max results (default 6)"}}),
    "kb_search": (t_kb_search, "Semantic search over the product/company knowledge base "
                  "(specs, prices, policies, extracted email knowledge).",
                  {"query": {"type": "string", "description": "natural-language question"}}),
    "find_files": (t_find_files, "Search the DST workspace for files by NAME — invoices, "
                   "PDFs, images, price lists, reports. Returns relative paths with size "
                   "and date, best match first.",
                   {"query": {"type": "string",
                              "description": "filename keywords, e.g. 'proforma flash pdf'"},
                    "limit": {"type": "integer", "description": "max results (default 8)"}}),
    "find_media": (t_find_media, "Find PHOTOS or VIDEOS of products/equipment by what "
                   "they SHOW (semantic content search of the media KB, runs on-box). "
                   "ALWAYS use this — never find_files — when asked for a picture, "
                   "photo, image or video of something ('pic of the PHD board'). "
                   "Returns paths + descriptions; deliver hits with send_file, using "
                   "each annotation as the caption.",
                   {"query": {"type": "string",
                              "description": "what the picture should show, e.g. 'PHD main board PCB'"},
                    "limit": {"type": "integer", "description": "max results (default 4)"}}),
    "read_pdf": (t_read_pdf, "READ the text of a PDF at a known path — an attachment "
                 "the sender just posted (its path is in their message) or a path from "
                 "find_files. ALWAYS call this before edit_pdf so you can quote the "
                 "exact strings to change.",
                 {"path": {"type": "string", "description": "path to the PDF"}}),
    "edit_pdf": (t_edit_pdf, "EDIT text inside a PDF — change quantities, prices, "
                 "dates, names. Each edit is ONE short single-line value (a number, a "
                 "price, a word) copied verbatim from read_pdf output — never a "
                 "multi-line block; use several edits for several values. If the find "
                 "string appears more than once in the document (a price that is both "
                 "unit price and total), you MUST disambiguate EACH edit: set 'near' "
                 "to a unique string on the SAME ROW of the table (the item name for "
                 "a line value, 'TOTAL' for the total) — preferred — or set "
                 "'occurrence' (1-based, top-to-bottom). If the value repeats WITHIN "
                 "that row too (unit price = line amount), also set 'column' to the "
                 "column header above the intended value (e.g. 'AMOUNT'). A repeated "
                 "value with no near/occurrence is REJECTED as ambiguous. Derived "
                 "values do NOT recalculate: if a quantity "
                 "changes, also edit the line amount and the total. The result shows "
                 "each changed row as it now reads (rows_after_edit) — CHECK the "
                 "right column changed before sending. Saves an edited COPY (the "
                 "original is untouched); deliver the returned path with send_file.",
                 {"path": {"type": "string", "description": "path to the source PDF"},
                  "edits": {"type": "array", "description": "the changes to apply",
                            "items": {"type": "object",
                                      "properties": {
                                          "find": {"type": "string"},
                                          "replace": {"type": "string"},
                                          "near": {"type": "string",
                                                   "description": "unique text on the "
                                                   "same row, to pick WHICH occurrence "
                                                   "of find (e.g. 'TOTAL' or the item "
                                                   "name)"},
                                          "column": {"type": "string",
                                                     "description": "column header "
                                                     "above the intended value, when "
                                                     "the row holds the same value "
                                                     "twice (e.g. 'AMOUNT')"},
                                          "occurrence": {
                                              "type": "integer",
                                              "description": "alternative to near: "
                                              "1-based index when the find string "
                                              "appears multiple times; omit both to "
                                              "replace all"}},
                                      "required": ["find", "replace"]}}}),
    "send_file": (t_send_file, "Attach a file to your reply — Vladimir receives it in "
                  "Telegram. Private/confidential DST documents are fine in this chat. "
                  "Use find_files first to get the exact path.",
                  {"path": {"type": "string", "description": "path from find_files"},
                   "caption": {"type": "string", "description": "optional short caption"}}),
    "render_diagram": (t_render_diagram, "Draw a DIAGRAM as an image the user receives "
                       "in the chat. Use whenever asked to make, draw or revise a "
                       "diagram/schematic/chart. For ANY wiring/electrical/connection "
                       "diagram use format 'wiring' (default): you supply ONLY the "
                       "data, all layout is computed locally and is always clean. "
                       "Schema: {\"title\": str, \"components\": [{\"id\": str, "
                       "\"label\": \"line1|line2\", \"terminals\": [str,...], "
                       "\"row\": \"top\"|\"bottom\", \"note\": str}], \"wires\": "
                       "[{\"from\": \"id:terminal\", \"to\": \"id:terminal\", "
                       "\"color\": \"red|black|brown|blue|green|orange\", \"label\": "
                       "str}]}. Put supplies/sources on row top, connectors/loads/"
                       "inputs on row bottom; every wire endpoint must name an "
                       "existing id:terminal exactly. Format 'dot' (Graphviz) is for "
                       "flowcharts/block diagrams; 'svg' only for fully custom art.",
                       {"source": {"type": "string",
                                   "description": "the diagram source: wiring JSON, a "
                                                  "Graphviz DOT document, or complete SVG"},
                        "format": {"type": "string",
                                   "description": "'wiring' (default, for anything with "
                                                  "wires/terminals), 'dot', or 'svg'"},
                        "name": {"type": "string",
                                 "description": "short kebab-case name, e.g. 'pht-m-wiring'"},
                        "caption": {"type": "string",
                                    "description": "optional image caption"}}),
    "read_diagram": (t_read_diagram, "Get the SVG source of a diagram rendered in an "
                     "earlier turn (newest first). ALWAYS use this before revising an "
                     "existing diagram — modify the returned SVG and call render_diagram "
                     "again, instead of redrawing from scratch.",
                     {"filename": {"type": "string",
                                   "description": "optional name filter; empty = newest"}}),
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
                                                   "local time; today's date is in the "
                                                   "system prompt"},
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


def _call_api(messages, key, timeout=45, deadline=None):
    # 4000 tokens: render_diagram takes a whole SVG document as a tool argument —
    # at 900 the JSON got truncated mid-SVG and parsed to {} (2026-07-20).
    payload = {"model": OR_MODEL, "temperature": 0.0, "max_tokens": 4000,
               "reasoning": {"enabled": False},
               "tools": _tool_schemas(),
               # require_parameters: fallback may only pick endpoints that support tool
               # calling (Nebius doesn't — it would silently ignore the tools and the
               # agent loop would answer without lookups). Vlad 2026-07-23: use every
               # capable provider before erroring.
               "provider": {"order": ["DekaLLM", "DigitalOcean", "DeepInfra"],
                            "allow_fallbacks": True, "require_parameters": True},
               "messages": messages}
    data = json.dumps(payload).encode()
    while True:
        req = urllib.request.Request(OR_URL, data=data,
                                     headers={"Authorization": f"Bearer {key}",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = json.loads(r.read())
            if "choices" in body:
                return body["choices"][0]["message"]
            # OpenRouter sometimes returns HTTP 200 with an error object instead
            # (seen 2026-07-27 during a 429 wave: KeyError 'choices' killed the loop).
            err = (body.get("error") or {})
            if err.get("code") in (429, "429") and deadline and time.time() + 30 < deadline - 15:
                _log("200-with-429-body — retrying in 30s")
                time.sleep(30)
                continue
            raise RuntimeError(f"API error: {str(err.get('message') or body)[:200]}")
        except urllib.error.HTTPError as e:
            # 429 = the model is temporarily rate-limited UPSTREAM (all providers at
            # once, 2026-07-08); OpenRouter's body says how long. Wait it out and retry
            # while the wall deadline allows — these waves usually pass within a minute.
            if e.code != 429:
                raise
            try:
                meta = json.loads(e.read())["error"]["metadata"]
                wait = int(meta.get("retry_after_seconds") or 30)
            except Exception:
                wait = 30
            wait = max(5, min(wait, 60))
            if deadline is None or time.time() + wait > deadline - 15:
                raise
            _log(f"429 rate-limited upstream — retrying in {wait}s")
            time.sleep(wait)


SYSTEM = (
    "You are the PRIVATE assistant of Digital Sign Technologies (DST), makers of the "
    "Print Head Doctor and Print Head Tester. You run on-box and are trusted with "
    "confidential company data. You are mid-conversation with DST's co-owners Vladimir "
    "and Marina over Telegram; each message is labeled with its actual sender — address "
    "the person who sent the CURRENT message, never talk about them in third person. "
    "The recent chat history is provided for context.\n"
    "FIRST classify what the CURRENT message asks of you, then act on that alone:\n"
    "• WORK REQUEST — an instruction to make, redo, change, fix, update or produce "
    "something ('redo the diagram', 'change X to Y', 'draft...'). DO THE WORK in this "
    "reply. If it revises something produced earlier in the conversation, output the "
    "ENTIRE revised result with the change applied — not a summary, not a promise, and "
    "NEVER a memory save. DIAGRAMS: when asked to make, draw or revise a diagram or "
    "schematic, call render_diagram — the user receives a rendered image in the chat. "
    "For wiring/electrical diagrams use format 'wiring': you list components+wires as "
    "JSON and the layout is computed locally, so focus entirely on getting the "
    "CONNECTIONS right. To REVISE an earlier diagram, first call read_diagram to get "
    "its source, apply ONLY the requested change to it, then render_diagram — never "
    "redraw from scratch and never answer with ASCII art. When the current message "
    "asks for a NEW diagram (e.g. from a described sketch), build it fresh from the "
    "description in THIS message, not from an old diagram. Alongside the image, state "
    "the key connections as a short numbered wire list in your reply text.\n"
    "• REMEMBER REQUEST — the sender EXPLICITLY says to remember/store/save a piece of "
    "information ('remember that...', 'save to memory...'). Call save_memory "
    "with the fact, then confirm what you saved. An imperative about how something "
    "should be built or done is NOT a remember request — it is a work request. "
    "Previously saved facts appear under 'Private memory' in your context; everything "
    "there is PRIVATE data.\n"
    "• FACT STATEMENT — the sender STATES information without asking anything: a "
    "declarative message describing a part, product, supplier, spec or decision "
    "('these are our fittings for X...', 'a PHD17 uses 8 of them', often with a URL "
    "or part number). An owner telling you facts IS giving you knowledge to keep: "
    "call save_memory with the COMPLETE fact (keep part numbers, quantities, models "
    "and any URL verbatim), then confirm in one line what you saved. Classify by the "
    "CURRENT message alone — a fact statement is NOT a continuation of the previous "
    "topic, even if earlier turns were about photos, documents or anything else. If "
    "it corrects an earlier fact, save the corrected version and say so.\n"
    "• FUTURE ACTION — the sender asks for something to happen LATER ('remind me at "
    "5pm', 'ping me tomorrow', 'check this evening whether...'). You do not run between "
    "messages, so call schedule_reminder — kind 'ping' for a plain reminder message, "
    "kind 'task' for a check to perform at that time (write the instruction "
    "self-contained, with full names and email addresses). NEVER answer 'I will ping "
    "you / check later' without a successful schedule_reminder call in THIS turn — an "
    "unscheduled promise is a lie. Confirm the scheduled time in your reply.\n"
    "• QUESTION — look it up with your tools, then answer.\n"
    "• SOCIAL — greetings, thanks, congratulations, jokes, small talk. Just reply "
    "warmly and briefly like any assistant would ('Thank you, Vladimir! Glad it "
    "helped.'). No tools, no lookups.\n"
    "Whatever the category: NEVER narrate your reasoning or classification ('this is "
    "a statement of praise', 'no tool use is required', 'I will respond "
    "appropriately'). Output ONLY the reply itself, exactly as it should appear in "
    "the chat.\n"
    "You HAVE lookup tools — use them: search_contacts / search_emails / read_email for "
    "customer matters, kb_search for product facts. TOOL ORDER for any question about a "
    "specific customer, company or person: ALWAYS call search_contacts FIRST — the CRM "
    "rolling summaries usually contain the full answer in one fast call (Vladimir's "
    "standing instruction, 2026-07-16). Only if the summary lacks the detail, escalate "
    "to search_emails / read_email. When the answer lives inside an "
    "email's attached document (a PO, invoice, quote or spec — bodies often just say "
    "'see attached'), call read_attachment for that email, or search_attachments to "
    "full-text search inside ALL attachments at once, instead of guessing. When Vladimir asks for an actual "
    "document (a PDF, invoice, price list, report), use find_files to locate it "
    "and send_file to attach it — this chat is private and trusted, so confidential DST "
    "files may be sent here. To READ a PDF (an attachment whose saved path is in the "
    "message, or a find_files hit), call read_pdf. When asked to EDIT or CHANGE a PDF "
    "(a quantity, price, date, name), call read_pdf first, then edit_pdf with exact "
    "find/replace strings from the read_pdf text — when a string appears more than "
    "once, disambiguate each edit with 'near' (unique text on the same table row, "
    "e.g. the item name or 'TOTAL'), and remember to update dependent amounts and "
    "totals too. READ edit_pdf's reply carefully: if it says the job LOOKS UNFINISHED "
    "(an old value still on a TOTAL or other row), you MUST call edit_pdf again on "
    "the saved copy to fix those rows — sending a PDF whose total contradicts its "
    "line items is worse than not sending at all. Only send_file the edited copy "
    "once every value is right. For a PICTURE, PHOTO or VIDEO of something, use find_media "
    "(it searches what images show; find_files only matches filenames) and send_file "
    "each hit with its annotation as the caption. When the owner points you at a WEB "
    "PAGE (a URL in the current message or recent history) for information, call "
    "fetch_page; for a picture ON a web page ('take it from the page I gave you'), "
    "call fetch_image with the page URL and a fragment of the image filename (usually "
    "the part number), then save_image_to_kb if it should be kept. When a FACT "
    "STATEMENT includes a product URL, also fetch_image the product photo and save it "
    "with the stated fact as the annotation — owners expect the KB to get both. "
    "Call tools as needed (several rounds are fine) BEFORE "
    "answering. When you have enough, give the final answer: concise, factual, grounded "
    "in what the tools returned. If the data truly isn't there, say exactly what you "
    "looked for and what's missing. Never invent facts.\n"
    "IMPORTANT: the chat history may contain OLDER replies of yours claiming you cannot "
    "access files, send files, read or edit PDFs, or fetch anything from external URLs "
    "— those came from a "
    "previous version of you that had no tools. They are obsolete: you NOW have "
    "find_files, send_file, read_pdf, edit_pdf, fetch_page and fetch_image. Never "
    "repeat such a claim, and "
    "never say something is unavailable without trying the matching tool in THIS turn."
)


_SOCIAL_RX = re.compile(
    r"^(congrat|thank|thanks|thx|nice|great|awesome|impressive|well done|good job|"
    r"bravo|perfect|love it|hello|hi\b|hey\b|good (morning|afternoon|evening|night)|"
    r"lol|haha|😂|👍|🙏|❤️|🎉)", re.I)


_REQUEST_RX = re.compile(
    r"\b(redo|remake|make|draw|render|send|find|change|update|fix|draft|remember|"
    r"save|invoice|file|photo|picture|diagram|look|check|search|show|list|quote|"
    r"price|email|now)\b", re.I)


def _is_social(text):
    """Short social message (praise/thanks/greeting) — no data intent, no question,
    and no piggy-backed request ('congrats. now redo the diagram')."""
    t = (text or "").strip()
    return (len(t) <= 80 and "?" not in t
            and bool(_SOCIAL_RX.match(t)) and not _REQUEST_RX.search(t))


def run(question, history="", sender="Vladimir", chat_id=None):
    """Tool-calling loop. Returns (answer_text, files_to_send) where files_to_send is
    a list of {path, caption} the gateway should upload to the chat. Raises on hard
    failure."""
    global _current_sender, _current_chat
    _current_sender = (sender or "Vladimir").strip() or "Vladimir"
    if chat_id:
        _current_chat = int(chat_id)
    key = _load_key()
    if not key:
        raise RuntimeError("no OpenRouter API key")
    del _pending_files[:]
    user = f"Current date and time: {time.strftime('%A %Y-%m-%d %H:%M')} (local).\n\n"
    if history.strip():
        user = f"Recent conversation (oldest first):\n{history.strip()}\n\n"
    try:
        with open(MEMORY_FILE) as f:
            mem = f.read().strip()[-4000:]
        if mem:
            user += f"Private memory (facts previously saved via save_memory):\n{mem}\n\n"
    except OSError:
        pass
    user += f"Message from {_current_sender}: {question}"
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": user}]
    deadline = time.time() + WALL_DEADLINE
    used_tools = False
    for turn in range(MAX_TURNS):
        msg = _call_api(messages, key, timeout=min(45, max(5, deadline - time.time())),
                        deadline=deadline)
        calls = msg.get("tool_calls") or []
        if not calls:
            # Anti-parrot guard: an answer with ZERO tool use is almost always the model
            # echoing stale history (e.g. old "I can't access files" replies from before
            # the tools existed — bit us 2026-07-08). Push back once and re-ask.
            # EXCEPT social messages (congrats/thanks/greetings — Vlad 2026-07-21): a
            # no-tool warm reply is exactly right there; nudging produced meta-narration
            # ("this is a statement of praise... I will respond appropriately").
            if _is_social(question):
                return ((msg.get("content") or "").strip() or "(the model returned no text)",
                        list(_pending_files))
            if not used_tools and turn == 0:
                _log("no-tool answer on first turn — nudging to verify with tools")
                messages.append({"role": "assistant",
                                 "content": msg.get("content") or ""})
                messages.append({"role": "user", "content":
                                 "Do not answer from the conversation history alone — it may be "
                                 "outdated. You HAVE working tools in this turn (search_contacts, "
                                 "search_emails, read_email, kb_search, find_files, send_file, "
                                 "read_pdf, edit_pdf, fetch_page, fetch_image, save_image_to_kb, "
                                 "render_diagram, read_diagram, save_memory, schedule_reminder). "
                                 "If they asked for a future ping/check, call schedule_reminder — "
                                 "never just promise. Address ONLY the "
                                 f"current message from {_current_sender}. If it is an instruction "
                                 "to make/redo/change something, do that work in this reply (for a "
                                 "diagram: read_diagram then render_diagram; otherwise output the "
                                 "full revised result) — do NOT save it to memory. If they asked to "
                                 "remember a fact OR simply STATED a fact (declarative message, no "
                                 "question), call save_memory with it. If they asked for a "
                                 "document, call find_files now and send_file to deliver it. "
                                 "Otherwise verify with the relevant lookup tools first, then "
                                 "answer."})
                continue
            return ((msg.get("content") or "").strip() or "(the model returned no text)",
                    list(_pending_files))
        used_tools = True
        messages.append({"role": "assistant", "content": msg.get("content") or "",
                         "tool_calls": calls})
        for tc in calls:
            name = tc["function"]["name"]
            bad_args = False
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except Exception:
                args, bad_args = {}, True
            _log(f"turn {turn + 1}: {name}({json.dumps(args)[:600]})")
            fn = TOOLS.get(name, (None,))[0]
            if bad_args:
                result = ("Tool call FAILED: the arguments were not valid JSON — "
                          "likely truncated. Retry with a SHORTER, simpler argument "
                          "(for render_diagram: a more compact SVG).")
            else:
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
    msg = _call_api(messages, key, timeout=30, deadline=deadline)
    return ((msg.get("content") or "").strip() or "(no answer after tool loop)",
            list(_pending_files))
