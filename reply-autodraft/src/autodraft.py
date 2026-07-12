#!/usr/bin/env python3
"""Reply Auto-Drafting for an owner's Gmail mailbox.

Two phases, run together on each scan (cron):

  PHASE A — DRAFT   For every new unread INBOX message that is a genuine PRODUCT
                    INQUIRY, draft a reply in the OWNER's voice using the product
                    knowledge base, save it to Gmail Drafts (threaded, CC = the
                    original CCs + a fixed teammate address), and ping a chat/
                    notification channel so the owner can review/edit/send.

  PHASE B — LEARN   For each draft we made earlier, look at what happened:
                      * still sitting in Drafts unsent -> do nothing (the owner
                        hasn't replied yet).
                      * draft sent (a sent draft keeps its draft id — the message
                        just gains the SENT label, so detection is by label), or
                        deleted + a reply sent in that thread -> compare what the
                        owner actually sent to our draft, and LEARN from the
                        difference: pull durable KB facts into reply-learnings.md
                        and refine the drafting instructions in SKILL.md.
                      * draft gone + no reply sent -> the owner deleted it; do nothing.

Threads are tracked by Gmail threadId, so a changed subject line never loses the
match. Nothing is ever SENT by this script — the owner always sends themselves.
"""
import os, sys, base64, sqlite3, json, subprocess, time, re
import html as html_lib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from email.utils import parseaddr, getaddresses, parsedate_to_datetime

HERE = os.path.dirname(os.path.abspath(__file__))
# Two dirs up = the project root that holds the knowledge base and (optionally)
# the auth helper / semantic index modules. Override with AUTODRAFT_PROJECT_ROOT.
PROJECT_ROOT = os.environ.get(
    "AUTODRAFT_PROJECT_ROOT", os.path.dirname(os.path.dirname(HERE)))
# Where the Gmail OAuth helper (auth.py, exposing get_credentials) and the
# optional semantic index (kb_index, exposing retrieve) live. Adjust to taste.
AUTH_DIR = os.environ.get("AUTODRAFT_AUTH_DIR", PROJECT_ROOT)
KB_INDEX_DIR = os.environ.get("AUTODRAFT_KB_INDEX_DIR", os.path.join(PROJECT_ROOT, "kb"))
sys.path.insert(0, AUTH_DIR)                      # for auth.py (bring your own)
sys.path.insert(0, KB_INDEX_DIR)                  # for kb_index (semantic KB retrieval)
try:
    import kb_index                                # noqa: E402
except Exception:
    kb_index = None

from googleapiclient.discovery import build       # noqa: E402
from googleapiclient.errors import HttpError       # noqa: E402
from auth import get_credentials                    # noqa: E402  (bring your own — see README)

# --- Identities (override via env) ----------------------------------------
# The mailbox we draft for, the teammate always CC'd, and what counts as
# "internal" mail we never draft replies to.
ACCOUNT = os.environ.get("AUTODRAFT_ACCOUNT", "owner")       # auth profile name
OWNER = os.environ.get("AUTODRAFT_OWNER_EMAIL", "owner@example.com")
CC_TEAMMATE = os.environ.get("AUTODRAFT_CC_EMAIL", "teammate@example.com")
INTERNAL_DOMAIN = os.environ.get("AUTODRAFT_INTERNAL_DOMAIN", "example.com")
INTERNAL = {OWNER, CC_TEAMMATE,
            f"assistant@{INTERNAL_DOMAIN}", f"support@{INTERNAL_DOMAIN}"}

STATE_DB = os.path.join(HERE, "state.db")         # created empty on first run
SKILL_MD = os.path.join(HERE, "SKILL.md")         # holds the learned-instructions block
LEARN_KB = os.path.join(PROJECT_ROOT, "knowledge-base", "from-emails", "reply-learnings.md")
PRODUCTS_DIR = os.path.join(PROJECT_ROOT, "knowledge-base", "products")
# Document attached on the FIRST reply in a thread (e.g. a price list / brochure).
# Set AUTODRAFT_ATTACHMENT to a real file path, or leave unset to attach nothing.
ATTACHMENT = os.environ.get("AUTODRAFT_ATTACHMENT", "")
STYLES_DIR = os.path.join(PROJECT_ROOT, "knowledge-base", "writing-styles")
LOG = os.path.join(HERE, "logs", "autodraft.log")

# Optional chat notification (Telegram Bot API shown; swap for any webhook).
BOT_TOKEN_F = os.environ.get("AUTODRAFT_TG_TOKEN_FILE",
                             os.path.join(PROJECT_ROOT, "telegram", "bot_token"))
NOTIFY_CHAT_F = os.environ.get("AUTODRAFT_TG_CHAT_FILE",
                               os.path.join(PROJECT_ROOT, "telegram", "notify_chat"))
DEFAULT_CHAT = os.environ.get("AUTODRAFT_TG_CHAT", "123456789")

CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or "claude"
CLAUDE_MODEL = os.environ.get("AUTODRAFT_MODEL", "opus")

MAX_DRAFTS_PER_RUN = int(os.environ.get("AUTODRAFT_MAX_DRAFTS", "5"))  # cap so it can't flood Drafts
SCAN_WINDOW = os.environ.get("AUTODRAFT_SCAN_WINDOW", "newer_than:4d")  # only recent unread mail
BODY_CHARS = 6000

# AUTODRAFT_PRIVACY=1 -> mask customer PII (names/emails/phones/addresses/order
# nos.) to [[TYPE_N]] tokens BEFORE any text reaches the cloud LLM, then unmask
# the reply. Unset/0 -> raw text to the cloud LLM. See privacy.py.
PRIVACY = os.environ.get("AUTODRAFT_PRIVACY") == "1"
if PRIVACY:
    from privacy import Masker, leak_check, local_chat, inflection_fix


# --------------------------------------------------------------------------- io
def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def notify(text):
    """Optional chat ping. No-ops quietly if no token/endpoint is configured."""
    import urllib.request, urllib.parse
    try:
        if not os.path.exists(BOT_TOKEN_F):
            return
        token = open(BOT_TOKEN_F).read().strip()
        chat = (open(NOTIFY_CHAT_F).read().strip()
                if os.path.exists(NOTIFY_CHAT_F) else DEFAULT_CHAT)
        data = urllib.parse.urlencode({
            "chat_id": chat, "text": text, "parse_mode": "Markdown",
            "disable_web_page_preview": "true"}).encode()
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data), timeout=20)
    except Exception as e:
        log(f"notify failed: {e}")


# ------------------------------------------------------------------------- llm
def cloud_llm(system, user, max_tokens=1500, timeout=200):
    """Call the main (high-quality) LLM via CLI. Returns text, or '' on failure.

    Drafting/learning is quality-critical and low-volume (only real inquiries),
    so it runs on the interactive LLM subscription — not a metered API. Swap this
    for any provider by editing this one function; see README (backend swappable)."""
    try:
        r = subprocess.run(
            [CLAUDE_BIN, "-p", f"{system}\n\n---\n\n{user}", "--model", CLAUDE_MODEL,
             "--dangerously-skip-permissions", "--output-format", "json"],
            capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            return (json.loads(r.stdout).get("result") or "").strip()
        log(f"llm rc={r.returncode}: {(r.stderr or '')[:200]}")
    except Exception as e:
        log(f"llm error: {e}")
    return ""


def json_obj(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {}
    for cand in (m.group(0), re.sub(r",\s*([}\]])", r"\1", m.group(0))):
        try:
            return json.loads(cand)
        except Exception:
            continue
    return {}


def json_arr(text):
    m = re.search(r"\[.*\]", text or "", re.S)
    if not m:
        return []
    for cand in (m.group(0), re.sub(r",\s*([}\]])", r"\1", m.group(0))):
        try:
            return json.loads(cand)
        except Exception:
            continue
    return []


# ------------------------------------------------------------------- gmail svc
def svc():
    creds = get_credentials(ACCOUNT, interactive=False)
    if not creds:
        sys.exit(f"Not authorized for '{ACCOUNT}'. Run: python auth.py {ACCOUNT}")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def hdr(msg, name):
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def body_text(payload):
    if payload.get("mimeType", "").startswith("multipart"):
        return "\n".join(x for x in (body_text(p) for p in payload.get("parts", [])) if x)
    data = payload.get("body", {}).get("data")
    if not data:
        return ""
    text = base64.urlsafe_b64decode(data).decode("utf-8", "replace")
    if payload.get("mimeType") == "text/html":
        text = re.sub(r"<[^>]+>", "", text)
    return text


def body_html(payload):
    """First text/html part, raw — used to quote the original like Gmail's Reply."""
    if payload.get("mimeType") == "text/html":
        data = payload.get("body", {}).get("data")
        return base64.urlsafe_b64decode(data).decode("utf-8", "replace") if data else ""
    for p in payload.get("parts", []) or []:
        h = body_html(p)
        if h:
            return h
    return ""


# -------------------------------------------------------------------- state db
def db():
    con = sqlite3.connect(STATE_DB)
    con.execute("""CREATE TABLE IF NOT EXISTS drafts (
        inbound_msg_id TEXT PRIMARY KEY,
        thread_id TEXT, sender TEXT, subject TEXT,
        draft_id TEXT, draft_msg_id TEXT, draft_body TEXT, cc TEXT,
        status TEXT,               -- skipped|drafted|learned|abandoned|error
        created_at TEXT, resolved_at TEXT)""")
    return con


def seen(con, mid):
    # 'error' rows (failed draft generation) stay retryable — a transient LLM
    # outage must not permanently swallow an inquiry. The row is kept for audit
    # and overwritten by INSERT OR REPLACE when a later attempt succeeds.
    return con.execute("SELECT 1 FROM drafts WHERE inbound_msg_id=? AND status != 'error'",
                       (mid,)).fetchone() is not None


# ------------------------------------------------------------- KB / style ctx
def read_products_kb():
    parts = []
    if not os.path.isdir(PRODUCTS_DIR):
        return ""
    for fn in sorted(os.listdir(PRODUCTS_DIR)):
        if fn.endswith(".md"):
            try:
                parts.append(f"### {fn}\n" + open(os.path.join(PRODUCTS_DIR, fn)).read())
            except Exception:
                pass
    return "\n\n".join(parts)


def retrieve_kb(query, k=10):
    """Semantic top-k over the WHOLE KB (products, company, faq, technical, Q&A) via a
    local index — scoped to what this email is about instead of dumping every file. Falls
    back to the full product-file dump if the index/embeddings are unavailable."""
    hits = kb_index.retrieve(query, k=k) if kb_index else []
    if not hits:
        return read_products_kb()
    return "\n\n".join(f"### {h['source']}\n{h['text']}" for h in hits)


def read_style():
    out = []
    for fn in ("owner.md", "learned-owner.md"):
        p = os.path.join(STYLES_DIR, fn)
        if os.path.exists(p):
            out.append(open(p).read())
    return "\n\n".join(out)


LEARN_START = "<!-- LEARNED-INSTRUCTIONS:START -->"
LEARN_END = "<!-- LEARNED-INSTRUCTIONS:END -->"


def read_learned_instructions():
    try:
        t = open(SKILL_MD).read()
        i, j = t.find(LEARN_START), t.find(LEARN_END)
        if i != -1 and j != -1:
            return t[i + len(LEARN_START):j].strip()
    except Exception:
        pass
    return ""


def write_learned_instructions(new_block):
    t = open(SKILL_MD).read()
    i, j = t.find(LEARN_START), t.find(LEARN_END)
    if i == -1 or j == -1:
        return
    t = t[:i + len(LEARN_START)] + "\n" + new_block.strip() + "\n" + t[j:]
    open(SKILL_MD, "w").write(t)


def read_reply_learnings():
    try:
        return open(LEARN_KB).read()[-8000:]     # recent tail as context
    except Exception:
        return ""


# =====================================================================  PHASE A
def is_internal(addr):
    a = (addr or "").lower()
    return a in INTERNAL or a.endswith("@" + INTERNAL_DOMAIN)


CLASSIFY_SYS = (
    "You triage inbound email for a company that sells its products and related "
    "supplies. Decide if the email is a genuine PRODUCT INQUIRY from a customer or "
    "prospect that a salesperson would reply to — e.g. asking about products, "
    "compatibility, pricing, a quote, availability, an order, or technical pre-sales "
    "questions. NOT inquiries: newsletters, marketing, invoices/receipts from vendors, "
    "shipping notifications, spam, purely internal chatter, social notifications, "
    "automated messages, POST-SALES technical support from existing customers — someone "
    "who already owns the equipment asking how to use, maintain or troubleshoot it (e.g. "
    "'is this part supposed to look like this', warranty/defect issues) gets a personal "
    "answer from the owner, not a drafted sales reply — or FOLLOW-UPS ON AN ORDER ALREADY "
    "IN PROGRESS: confirmations, corrections ('the order should also include X'), "
    "status/shipping/payment questions about an order that has already been placed, or any "
    "reply in a thread a teammate is actively handling. A NEW purchase request or quote "
    "request still counts as an inquiry. Draft ONLY for clear product "
    "inquiries and NOTHING else: when "
    "in doubt, or if it's ambiguous whether a sales reply is warranted, answer false. "
    "Return ONLY JSON: {\"inquiry\": true|false, \"reason\": \"...\"}.")

DRAFT_SYS = (
    "You are drafting an email reply on behalf of the OWNER of a company. Write the reply "
    "EXACTLY as the owner would: match their writing style, tone, greeting and sign-off "
    "from the STYLE PROFILE below. Answer the customer's questions using ONLY the PRODUCT "
    "KNOWLEDGE provided — never invent specs, prices, compatibility or policies. If "
    "something needed to answer isn't in the knowledge base, don't guess: either omit it "
    "or say you'll follow up with those details. Prices come from the KB as-is.\n\n"
    "Output ONLY the email body — from the greeting through the sign-off — as plain text. "
    "No subject line, no 'Draft:' preamble, no quoted original message, no markdown, no "
    "commentary.")


def build_draft_body(ctx, products_kb, style, learned, reply_kb, gen=cloud_llm):
    user = (
        f"=== PRODUCT KNOWLEDGE (authoritative) ===\n{products_kb}\n\n"
        f"=== FACTS LEARNED FROM THE OWNER'S PAST REPLIES ===\n{reply_kb or '(none yet)'}\n\n"
        f"=== OWNER'S WRITING STYLE ===\n{style}\n\n"
        f"=== LEARNED DRAFTING INSTRUCTIONS (follow these) ===\n{learned or '(none yet)'}\n\n"
        f"=== CUSTOMER EMAIL TO REPLY TO ===\n{ctx}\n\n"
        "Write the owner's reply now (body only):")
    return gen(DRAFT_SYS, user, max_tokens=1800)


def compute_cc(orig_to, orig_cc, sender):
    """Reply-all semantics: CC = everyone on the original To AND Cc + the fixed
    teammate. Drop ALL internal addresses (support@ etc. — the customer sees only
    the humans handling their thread), the customer (they're in To), and de-dupe;
    the fixed teammate is the one internal exception."""
    ccs = [a.lower() for _, a in getaddresses([orig_to or "", orig_cc or ""])
           if a and "@" in a and not is_internal(a)]
    ccs.append(CC_TEAMMATE)
    drop = {sender.lower(), OWNER.lower()}
    seen_, out = set(), []
    for a in ccs:
        if a in drop or a in seen_:
            continue
        seen_.add(a)
        out.append(a)
    return ", ".join(out)


def is_first_reply(s, thread_id):
    """True if nobody internal (owner/teammate/…) has replied in this thread yet."""
    try:
        thr = s.users().threads().get(userId="me", id=thread_id, format="metadata",
                                      metadataHeaders=["From"]).execute()
    except HttpError:
        return True  # can't tell -> treat as first reply (attach)
    for msg in thr.get("messages", []):
        frm = parseaddr(hdr(msg, "From"))[1].lower()
        if frm and is_internal(frm):
            return False
    return True


def make_draft(s, orig_msg, to, cc, orig_subject, body, attach=None):
    """Create the reply draft exactly like clicking Reply in Gmail: HTML body,
    the original message quoted below an 'On <date>, <sender> wrote:' attribution
    line, threaded via In-Reply-To/References. multipart/alternative keeps a
    plaintext fallback for old clients. `orig_msg` is the full inbound message."""
    payload_in = orig_msg.get("payload", {})
    frm = hdr(orig_msg, "From")
    try:
        when = parsedate_to_datetime(hdr(orig_msg, "Date"))
        when_s = when.strftime("%a, %b %-d, %Y at %-I:%M %p")
    except Exception:
        when_s = hdr(orig_msg, "Date")
    attr = f"On {when_s}, {frm} wrote:"

    orig_plain = body_text(payload_in)
    quote_html = (body_html(payload_in) or
                  "<br>\n".join(html_lib.escape(orig_plain).splitlines()))
    reply_html = "<br>\n".join(html_lib.escape(body).splitlines())
    html_full = (
        f'<div dir="ltr">{reply_html}</div><br>'
        f'<div class="gmail_quote"><div dir="ltr" class="gmail_attr">'
        f'{html_lib.escape(attr)}<br></div>'
        f'<blockquote class="gmail_quote" style="margin:0px 0px 0px 0.8ex;'
        f'border-left:1px solid rgb(204,204,204);padding-left:1ex">'
        f'{quote_html}</blockquote></div>')
    plain_full = (body + "\n\n" + attr + "\n" +
                  "\n".join("> " + l for l in orig_plain.splitlines()))

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(plain_full, "plain"))
    alt.attach(MIMEText(html_full, "html"))
    if attach and os.path.exists(attach):
        mime = MIMEMultipart()
        mime.attach(alt)
        with open(attach, "rb") as f:
            part = MIMEApplication(f.read(), _subtype="pdf")
        part.add_header("Content-Disposition", "attachment",
                        filename=os.path.basename(attach))
        mime.attach(part)
    else:
        mime = alt
    mime["To"] = to
    if cc:
        mime["Cc"] = cc
    orig = orig_msg
    msgid = hdr(orig, "Message-ID")
    if msgid:
        mime["In-Reply-To"] = msgid
        mime["References"] = msgid
    subj = orig_subject or ""
    if not subj.lower().startswith("re:"):
        subj = "Re: " + subj
    mime["Subject"] = subj
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
    payload = {"raw": raw, "threadId": orig.get("threadId")}
    res = s.users().drafts().create(userId="me", body={"message": payload}).execute()
    return res["id"], res["message"]["id"]


def phase_a(s, con):
    res = s.users().messages().list(userId="me", labelIds=["INBOX"],
                                    q=f"is:unread {SCAN_WINDOW}", maxResults=40).execute()
    ids = [m["id"] for m in res.get("messages", [])]
    products_kb = style = learned = reply_kb = None
    drafted = 0
    for mid in ids:
        if seen(con, mid):
            continue
        m = s.users().messages().get(userId="me", id=mid, format="full").execute()
        frm = hdr(m, "From"); sender = parseaddr(frm)[1].lower()
        subject = hdr(m, "Subject"); to = hdr(m, "To"); cc = hdr(m, "Cc")
        thread_id = m.get("threadId")
        body = body_text(m.get("payload", {})).strip()[:BODY_CHARS]
        now = datetime.now(timezone.utc).isoformat()

        # skip our own / internal / agent mail outright
        if is_internal(sender) or not sender:
            con.execute("INSERT OR REPLACE INTO drafts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (mid, thread_id, sender, subject, None, None, None, None,
                         "skipped", now, now)); con.commit(); continue

        ctx_raw = (f"From: {frm}\nTo: {to}\nCc: {cc}\nSubject: {subject}\n\n{body}")
        ctx = ctx_raw
        mk = None
        gen = cloud_llm                              # default: masked text -> cloud LLM
        if PRIVACY:
            mk = Masker().seed(body, subject, frm, to, cc)
            if not mk.ner_ok:                        # NER down -> whole thing local
                gen = local_chat
                log(f"privacy: NER unavailable for {sender!r} — routing to local model")
            else:
                ctx = mk.mask(ctx_raw)               # classify + draft see only tokens
                if leak_check(ctx):                  # hard PII survived NER -> local
                    gen = local_chat
                    ctx = ctx_raw
                    log(f"privacy tripwire for {sender!r} — routing to local model")

        cls = json_obj(gen(CLASSIFY_SYS, ctx, max_tokens=300))
        if "inquiry" not in cls:
            # LLM call failed or returned unparseable output — that is NOT a verdict.
            # Leave no state row so the message is retried next run (seen() would
            # otherwise make a transient outage permanently swallow real inquiries).
            log(f"classify failed for {sender!r} — retrying next run")
            continue
        if not cls.get("inquiry"):
            con.execute("INSERT OR REPLACE INTO drafts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (mid, thread_id, sender, subject, None, None, None, None,
                         "skipped", now, now)); con.commit()
            log(f"skip {sender!r} — not an inquiry ({cls.get('reason','')[:60]})")
            continue

        if drafted >= MAX_DRAFTS_PER_RUN:
            log(f"draft cap reached; leaving {sender!r} for next run"); continue
        if style is None:                            # query-independent context: load once/run
            style = read_style()
            learned, reply_kb = read_learned_instructions(), read_reply_learnings()
        products_kb = retrieve_kb(ctx)               # per-email: semantic top-k over the KB

        reply = build_draft_body(ctx, products_kb, style, learned, reply_kb, gen=gen)
        if PRIVACY and mk and gen is cloud_llm:
            had_ph = "[[" in (reply or "")
            reply = mk.unmask(reply)                 # masked path: tokens -> real values
            # Non-English replies: names dropped into placeholder slots can break
            # declension/agreement — repair on the private model (inside the
            # privacy boundary).
            reply = inflection_fix(reply, had_ph)
        if not reply:
            con.execute("INSERT OR REPLACE INTO drafts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (mid, thread_id, sender, subject, None, None, None, None,
                         "error", now, None)); con.commit()
            log(f"draft generation failed for {sender!r}"); continue

        cc_out = compute_cc(to, cc, sender)
        attach = ATTACHMENT if (ATTACHMENT and is_first_reply(s, thread_id)) else None
        did, dmsg = make_draft(s, m, sender, cc_out, subject, reply, attach=attach)
        con.execute("INSERT OR REPLACE INTO drafts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (mid, thread_id, sender, subject, did, dmsg, reply, cc_out,
                     "drafted", now, None)); con.commit()
        drafted += 1
        pl = "  📎 attachment included" if attach else ""
        notify(f"📝 *Draft ready* — reply to {sender}\n*Re:* {subject[:80]}\n"
               f"CC: {cc_out}{pl}\nReview & send from Gmail *Drafts*. I'll learn from your edits.")
        log(f"drafted reply to {sender!r} (draft_id={did})")
    return drafted


# =====================================================================  PHASE B
LEARN_SYS = (
    "You improve an assistant that drafts sales-email replies for the OWNER of a company. "
    "You are given the CUSTOMER email, the assistant's DRAFT reply, and the reply the OWNER "
    "ACTUALLY SENT. Study the difference between the draft and what the owner sent. "
    "Return ONLY JSON with two arrays:\n"
    "  \"kb_facts\": durable, reusable business/product facts revealed by the owner's reply "
    "that the assistant should remember for future replies (specific specs, prices, "
    "compatibility, lead times, policies, discounts, terms). Short factual strings. Omit "
    "anything one-off/customer-specific. Empty array if none.\n"
    "  \"instructions\": concrete drafting-behaviour lessons so next time the draft matches "
    "the owner more closely (structure, tone, what to include or leave out, phrasing, "
    "greeting/sign-off, length). Short imperative strings. Empty array if the draft already "
    "matched well.\n"
    "If the draft and sent reply are essentially identical, return empty arrays.")

MERGE_SYS = (
    "You maintain a CONCISE, de-duplicated list of drafting instructions for an email-reply "
    "assistant. Merge the NEW instructions into the CURRENT list: combine overlapping points, "
    "drop redundancy, keep the most useful and general lessons. Return ONLY the updated list as "
    "markdown bullet points (each line starts with '- '), at most 25 bullets, no preamble.")


def find_sent_reply(s, thread_id, inbound_id):
    """Return the body of a reply the owner sent in this thread (newest), or None."""
    try:
        t = s.users().threads().get(userId="me", id=thread_id, format="full").execute()
    except HttpError:
        return None
    best = None
    for m in t.get("messages", []):
        if m["id"] == inbound_id:
            continue
        labels = m.get("labelIds", [])
        if "SENT" not in labels and "DRAFT" in labels:
            continue
        frm = parseaddr(hdr(m, "From"))[1].lower()
        if frm != OWNER.lower():
            continue
        if "DRAFT" in labels:                       # unsent draft, ignore
            continue
        # newest sent reply from the owner wins
        idate = int(m.get("internalDate", "0"))
        if best is None or idate > best[0]:
            best = (idate, body_text(m.get("payload", {})).strip())
    return best[1] if best else None


def append_kb_facts(facts, sender, subject):
    if not facts:
        return
    stamp = datetime.now().strftime("%Y-%m-%d")
    lines = [f"\n### {stamp} — from reply to {sender} (Re: {subject[:70]})"]
    lines += [f"- {f}" for f in facts if isinstance(f, str) and f.strip()]
    header = ""
    if not os.path.exists(LEARN_KB):
        os.makedirs(os.path.dirname(LEARN_KB), exist_ok=True)
        header = ("# Reply Learnings\n\n_Durable facts extracted by the Reply Auto-Drafting "
                  "skill from replies the owner actually sent. Fed back into future drafts._\n")
    with open(LEARN_KB, "a") as f:
        if header:
            f.write(header)
        f.write("\n".join(lines) + "\n")


def merge_instructions(new_instr):
    if not new_instr:
        return
    current = read_learned_instructions()
    user = (f"CURRENT list:\n{current or '(empty)'}\n\nNEW instructions:\n"
            + "\n".join(f"- {i}" for i in new_instr if isinstance(i, str) and i.strip()))
    merged = cloud_llm(MERGE_SYS, user, max_tokens=900)
    if merged.strip():
        write_learned_instructions(merged)


def phase_b(s, con):
    rows = con.execute("SELECT inbound_msg_id, thread_id, sender, subject, draft_id, "
                       "draft_body FROM drafts WHERE status='drafted'").fetchall()
    for mid, thread_id, sender, subject, draft_id, draft_body in rows:
        # 1) is the draft still sitting in Drafts?
        # NB: a draft sent from the Gmail UI keeps its draft id retrievable —
        # the message just gains the SENT label. Only an *unsent* draft
        # (DRAFT label, no SENT) means the owner hasn't acted yet.
        if draft_id:
            try:
                d = s.users().drafts().get(userId="me", id=draft_id, format="minimal").execute()
                labels = d.get("message", {}).get("labelIds", []) or []
                if "SENT" not in labels:
                    continue                         # still unsent -> owner hasn't acted; do nothing
            except HttpError as e:
                if getattr(e, "resp", None) is None or e.resp.status != 404:
                    log(f"draft.get error for {draft_id}: {e}"); continue
        # 2) draft gone. Did the owner send a reply in this thread?
        sent = find_sent_reply(s, thread_id, mid)
        now = datetime.now(timezone.utc).isoformat()
        if not sent:
            con.execute("UPDATE drafts SET status='abandoned', resolved_at=? WHERE inbound_msg_id=?",
                        (now, mid)); con.commit()
            log(f"draft to {sender!r} deleted without a reply — no action")
            continue
        # 3) learn from the difference
        ctx = (f"CUSTOMER EMAIL:\nFrom: {sender}\nSubject: {subject}\n\n"
               f"ASSISTANT DRAFT:\n{draft_body}\n\nOWNER ACTUALLY SENT:\n{sent}")
        gen = cloud_llm
        masked = False
        if PRIVACY:
            mkb = Masker().seed(subject, sender, draft_body, sent)
            if not mkb.ner_ok:                       # NER down -> learn via local model on raw
                gen = local_chat
            else:
                ctx = mkb.mask(ctx); masked = True
                if leak_check(ctx):                  # hard PII survived -> local
                    gen = local_chat
                    ctx = (f"CUSTOMER EMAIL:\nFrom: {sender}\nSubject: {subject}\n\n"
                           f"ASSISTANT DRAFT:\n{draft_body}\n\nOWNER ACTUALLY SENT:\n{sent}")
                    masked = False
        out = json_obj(gen(LEARN_SYS, ctx, max_tokens=1200))
        facts = out.get("kb_facts") or []
        instr = out.get("instructions") or []
        if PRIVACY and masked:                       # drop anything customer-specific
            facts = [f for f in facts if "[[" not in str(f)]
            instr = [i for i in instr if "[[" not in str(i)]
        append_kb_facts(facts, sender, subject)
        merge_instructions(instr)
        con.execute("UPDATE drafts SET status='learned', resolved_at=? WHERE inbound_msg_id=?",
                    (now, mid)); con.commit()
        log(f"learned from owner's sent reply to {sender!r}: "
            f"{len(facts)} KB fact(s), {len(instr)} instruction(s)")
        if facts or instr:
            notify(f"🧠 Learned from your reply to {sender} — "
                   f"{len(facts)} new KB fact(s), {len(instr)} drafting tweak(s).")


# ============================================================================
def main():
    s = svc()
    con = db()
    try:
        phase_b(s, con)          # learn from anything resolved since last run first
        n = phase_a(s, con)      # then draft for new inquiries
        log(f"run complete — {n} new draft(s)")
    finally:
        con.close()


if __name__ == "__main__":
    main()
