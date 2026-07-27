#!/usr/bin/env python3
"""gmail-chat-agent — realtime email-as-chat agent with owner verification.

Holds a persistent Gmail IMAP IDLE connection (XOAUTH2) on the bot's mailbox, so
new mail is seen within seconds — no polling. Each new INBOX message goes
through a security gate:

  1. The From address must EXACTLY match one of OWNER_EMAILS (parsed with
     email.utils.parseaddr — display-name tricks like
     "owner@real.com <attacker@evil.com>" don't work).
  2. The message must pass Gmail's own authentication: the mx.google.com
     Authentication-Results header must show dkim=pass with header.d aligned to
     the owner's domain (optionally spf=pass fallback via ALLOW_SPF_ONLY).
     A mail *claiming* the owner's address but failing this is flagged to
     Telegram as a possible impersonation attempt — and never replied to.
  3. Automated noise (bounces, out-of-office, RFC 3834 auto-submitted mail, the
     bot's own address) is dropped so the agent can never enter a reply loop.
     Outgoing replies carry `Auto-Submitted: auto-replied` for the same reason,
     and a per-hour turn cap acts as a runaway brake.

A verified owner mail is treated exactly like a chat message: body (+ saved
attachment paths) is piped into AGENT_CMD (default `claude -p`), and whatever
the agent prints is emailed back as a threaded reply. All other senders are
ignored — no reply, no agent turn — but reported to a Telegram chat.

Run via start_watcher.sh (flock single-instance) + @reboot + watchdog cron.
"""
import os, re, sys, ssl, time, json, base64, socket, imaplib, subprocess
import urllib.request, urllib.parse
from email.utils import parseaddr
from email.message import EmailMessage
from html import escape as html_escape

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from auth import get_credentials
import config as C

IMAP_HOST, IMAP_PORT = "imap.gmail.com", 993
STATE_FILE = os.path.join(BASE, "state.json")
LOG_FILE   = os.path.join(BASE, "logs", "agent_watcher.log")
ATTACH_DIR = os.path.join(BASE, "attachments")

IDLE_RENEW = 300       # re-issue IDLE every 5 min (Gmail drops IDLE ~29 min)
RECONNECT_EVERY = 1500 # full reconnect every 25 min (fresh access token)
SEEN_CAP = 300


def log(msg):
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}\n")


# ---------- state ----------
def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {}


def save_state(st):
    st["seen_ids"] = st.get("seen_ids", [])[:SEEN_CAP]
    tmp = STATE_FILE + ".tmp"
    json.dump(st, open(tmp, "w"))
    os.replace(tmp, STATE_FILE)


# ---------- telegram reporting ----------
def _report_conf():
    token = os.environ.get(C.BOT_TOKEN_ENV, "")
    chat = os.environ.get(C.REPORT_CHAT_ENV, "")
    if not token and os.path.exists(C.BOT_TOKEN_FILE):
        token = open(C.BOT_TOKEN_FILE).read().strip()
    if not chat and os.path.exists(C.REPORT_CHAT_FILE):
        chat = open(C.REPORT_CHAT_FILE).read().strip()
    return token, chat


def report(text):
    """Ping the Telegram report chat; falls back to log-only if not configured."""
    token, chat = _report_conf()
    if not token or not chat:
        log(f"report (no telegram configured): {text}")
        return
    data = urllib.parse.urlencode({
        "chat_id": chat, "text": text, "disable_web_page_preview": "true",
    }).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
            r.read()
    except Exception as e:
        log(f"report failed: {e} | {text}")


# ---------- gmail helpers ----------
def gmail_svc():
    from googleapiclient.discovery import build
    creds = get_credentials("agent", interactive=False)
    if not creds:
        raise RuntimeError("no agent credentials — run: python auth.py agent")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _hdr(m, name):
    for h in m.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _body_text(payload):
    """Recursively pull text/plain (fallback: html with tags stripped)."""
    if payload.get("mimeType", "").startswith("multipart"):
        return "\n".join(x for x in (_body_text(p) for p in payload.get("parts", [])) if x)
    data = payload.get("body", {}).get("data")
    if not data:
        return ""
    text = base64.urlsafe_b64decode(data).decode("utf-8", "replace")
    if payload.get("mimeType") == "text/html":
        text = re.sub(r"<[^>]+>", "", text)
    return text


# ---------- security gate ----------
def sender_address(frm):
    """The actual RFC 5322 address, lowercased — immune to display-name spoofing."""
    return parseaddr(frm)[1].lower()


def gmail_auth_results(full):
    """The Authentication-Results header STAMPED BY GMAIL (authserv-id
    mx.google.com), topmost occurrence. Gmail strips inbound headers claiming
    its own authserv-id (RFC 8601 §5), so this one is trustworthy — never use
    an AR header with any other authserv-id, an attacker can attach those."""
    for h in full.get("payload", {}).get("headers", []):
        if h["name"].lower() == "authentication-results" and \
           h["value"].strip().lower().startswith("mx.google.com"):
            return h["value"].lower()
    return ""


def _domains_align(d, domain):
    return d == domain or d.endswith("." + domain) or domain.endswith("." + d)


def sender_authenticated(full, addr):
    """True only if Gmail itself verified the mail really came from addr's
    domain: dkim=pass with header.d aligned to the domain (or, if explicitly
    enabled, spf=pass aligned). This is what stops From-header impersonation."""
    ar = gmail_auth_results(full)
    if not ar:
        return False
    domain = addr.rsplit("@", 1)[-1]
    for m in re.finditer(r"dkim=pass[^;]*?header\.(?:d|i)=@?([\w.\-]+)", ar):
        if _domains_align(m.group(1), domain):
            return True
    if C.ALLOW_SPF_ONLY:
        m = re.search(r"spf=pass[^;]*?smtp\.mailfrom=(?:[^\s;]*@)?([\w.\-]+)", ar)
        if m and _domains_align(m.group(1), domain):
            return True
    return False


def is_auto_mail(full, frm, subj, body):
    """Detect mail that must never trigger a turn or reply: bounces, vacation
    auto-replies, our own outgoing mail looping back. Returns reason or None."""
    if sender_address(frm) == C.ACCOUNT.lower():
        return "own address"
    auto = _hdr(full, "Auto-Submitted").lower()
    if auto and auto != "no":
        return f"Auto-Submitted: {auto}"
    prec = _hdr(full, "Precedence").lower()
    if prec in ("auto_reply", "bulk", "junk", "list"):
        return f"Precedence: {prec}"
    fl = frm.lower()
    if "mailer-daemon" in fl or "postmaster" in fl:
        return "mailer-daemon/postmaster"
    text = f"{subj}\n{body}".lower()
    for p in ("delivery status notification", "mail delivery failed",
              "undeliverable", "automatic reply", "out of office", "autoreply"):
        if p in text:
            return f'matched "{p}"'
    return None


def rate_limited(st):
    """Runaway brake: cap agent turns per rolling hour."""
    now = time.time()
    turns = [t for t in st.get("turn_ts", []) if now - t < 3600]
    st["turn_ts"] = turns
    return len(turns) >= C.MAX_TURNS_PER_HOUR


# ---------- attachments ----------
def _walk_attachment_parts(payload, out):
    for p in payload.get("parts", []) or []:
        if p.get("filename"):
            out.append(p)
        _walk_attachment_parts(p, out)


def save_attachments(svc, mid, payload):
    parts = []
    _walk_attachment_parts(payload, parts)
    paths = []
    for p in parts:
        body = p.get("body", {})
        data = body.get("data")
        if not data and body.get("attachmentId"):
            try:
                att = svc.users().messages().attachments().get(
                    userId="me", messageId=mid, id=body["attachmentId"]).execute()
                data = att.get("data")
            except Exception as e:
                log(f"attachment fetch failed {mid}/{p['filename']}: {e}")
                continue
        if not data:
            continue
        safe = re.sub(r"[^\w.\-]+", "_", p["filename"])[:120] or "attachment"
        d = os.path.join(ATTACH_DIR, mid)
        os.makedirs(d, exist_ok=True)
        path, n = os.path.join(d, safe), 1
        while os.path.exists(path):
            path = os.path.join(d, f"{n}_{safe}")
            n += 1
        with open(path, "wb") as f:
            f.write(base64.urlsafe_b64decode(data))
        paths.append(path)
    return paths


# ---------- the agent turn ----------
def run_agent(frm, subj, body, attachments):
    prompt = (
        f"You received an email from your owner ({frm}). Treat the body below as a "
        f"chat message from them and act on it. Your stdout will be emailed back "
        f"verbatim as the reply body, so output ONLY the reply text — no subject "
        f"line, no markdown fences around the whole message, no meta commentary.\n\n"
        f"Subject: {subj}\n"
        + (f"Attachments saved locally: {', '.join(attachments)}\n" if attachments else "")
        + f"\n{body}\n")
    p = subprocess.run(
        C.AGENT_CMD, shell=True, input=prompt.encode(),
        capture_output=True, timeout=C.AGENT_TIMEOUT,
        cwd=C.AGENT_CWD or None)
    out = p.stdout.decode("utf-8", "replace").strip()
    if not out:
        err = p.stderr.decode("utf-8", "replace").strip()[:400]
        raise RuntimeError(f"agent produced no output (rc={p.returncode}) {err}")
    return out


def send_reply(svc, full, frm, subj, reply_text):
    msg = EmailMessage()
    msg["To"] = frm
    msg["From"] = C.ACCOUNT
    msg["Subject"] = subj if subj.lower().startswith("re:") else f"Re: {subj}"
    orig_id = _hdr(full, "Message-ID")
    if orig_id:
        msg["In-Reply-To"] = orig_id
        refs = _hdr(full, "References")
        msg["References"] = f"{refs} {orig_id}".strip()
    msg["Auto-Submitted"] = "auto-replied"   # RFC 3834: lets other bots ignore us
    msg.set_content(reply_text)
    msg.add_alternative(
        '<div style="white-space:pre-wrap;font-family:system-ui,sans-serif;'
        f'font-size:14px">{html_escape(reply_text)}</div>', subtype="html")
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    svc.users().messages().send(
        userId="me", body={"raw": raw, "threadId": full.get("threadId")}).execute()


# ---------- main pipeline ----------
def handle_message(svc, st, mid):
    meta = svc.users().messages().get(
        userId="me", id=mid, format="metadata",
        metadataHeaders=["From", "Subject"]).execute()
    frm = _hdr(meta, "From")
    subj = _hdr(meta, "Subject") or "(no subject)"
    addr = sender_address(frm)

    if addr not in C.OWNER_EMAILS:
        log(f"IGNORE {mid} | {frm} | {subj} (not owner)")
        report(f"📧 {C.ACCOUNT}: ignored email from {frm} — “{subj}” "
               f"(not the owner; no reply sent)")
        return

    full = svc.users().messages().get(userId="me", id=mid, format="full").execute()
    body = _body_text(full.get("payload", {})).strip()[:C.BODY_MAX]

    reason = is_auto_mail(full, frm, subj, body)
    if reason:
        log(f"SKIP-AUTO {mid} | {frm} | {subj} ({reason})")
        return

    if not sender_authenticated(full, addr):
        log(f"SKIP-SPOOF {mid} | {frm} | {subj} (auth failed)")
        report(f"⚠️ {C.ACCOUNT}: POSSIBLE IMPERSONATION — email claims to be from "
               f"{addr} but failed DKIM/SPF verification. Subject: “{subj}”. "
               f"Ignored, no reply sent.")
        return

    if rate_limited(st):
        log(f"RATE-LIMIT {mid} | {frm} | {subj}")
        report(f"⚠️ {C.ACCOUNT}: turn rate limit reached "
               f"({C.MAX_TURNS_PER_HOUR}/h) — owner email “{subj}” NOT processed.")
        return

    attachments = save_attachments(svc, mid, full.get("payload", {}))
    log(f"TURN {mid} | {frm} | {subj}"
        + (f" ({len(attachments)} attachment(s))" if attachments else ""))
    st.setdefault("turn_ts", []).append(time.time())
    save_state(st)
    try:
        reply = run_agent(frm, subj, body, attachments)
        send_reply(svc, full, frm, subj, reply)
        log(f"REPLIED {mid} ({len(reply)} chars)")
    except Exception as e:
        log(f"TURN-FAIL {mid}: {type(e).__name__}: {e}")
        report(f"⚠️ {C.ACCOUNT}: agent turn failed on owner email “{subj}”: {e}")


def check_new(first_run=False):
    svc = gmail_svc()
    res = svc.users().messages().list(userId="me", labelIds=["INBOX"], maxResults=15).execute()
    ids = [m["id"] for m in res.get("messages", [])]          # newest-first
    st = load_state()
    seen = st.get("seen_ids", [])
    new = [i for i in ids if i not in set(seen)]
    merged, s2 = [], set()
    for i in ids + seen:
        if i not in s2:
            s2.add(i); merged.append(i)
    st["seen_ids"] = merged
    save_state(st)
    if first_run or not new:
        return 0
    for mid in reversed(new):                                 # oldest first
        try:
            handle_message(svc, st, mid)
        except Exception as e:
            log(f"handle failed {mid}: {type(e).__name__}: {e}")
    return len(new)


# ---------- imap idle ----------
def imap_connect():
    creds = get_credentials("agent", interactive=False)
    if not creds:
        raise RuntimeError("no agent credentials — run: python auth.py agent")
    auth = f"user={C.ACCOUNT}\x01auth=Bearer {creds.token}\x01\x01"
    M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    M.authenticate("XOAUTH2", lambda _: auth.encode())
    M.select("INBOX")
    return M


def idle_once(M, timeout):
    tag = M._new_tag()
    M.send(tag + b" IDLE\r\n")
    resp = M.readline()
    if not resp.startswith(b"+"):
        raise imaplib.IMAP4.error(f"IDLE not accepted: {resp!r}")
    changed = False
    M.sock.settimeout(timeout)
    try:
        while True:
            line = M.readline()
            if not line:
                raise imaplib.IMAP4.abort("connection closed during IDLE")
            if b"EXISTS" in line or b"RECENT" in line:
                changed = True
                break
    except (socket.timeout, ssl.SSLError):
        pass
    finally:
        try:
            M.sock.settimeout(30)
            M.send(b"DONE\r\n")
            while True:
                l = M.readline()
                if l.startswith(tag) or not l:
                    break
        except Exception:
            raise imaplib.IMAP4.abort("failed to end IDLE")
        finally:
            M.sock.settimeout(None)
    return changed


def main():
    log(f"agent_watcher starting (account={C.ACCOUNT}, owners={C.OWNER_EMAILS})")
    try:
        if not os.path.exists(STATE_FILE):
            check_new(first_run=True)
            log("primed seen-set (pre-existing inbox not processed)")
    except Exception as e:
        log(f"prime failed: {e}")
    backoff = 5
    while True:
        try:
            M = imap_connect()
            log("IMAP connected; IDLE loop active")
            backoff = 5
            check_new()   # catch anything that arrived while disconnected
            deadline = time.time() + RECONNECT_EVERY
            while time.time() < deadline:
                remaining = max(10, min(IDLE_RENEW, int(deadline - time.time())))
                idle_once(M, remaining)
                check_new()
            try:
                M.logout()
            except Exception:
                pass
        except Exception as e:
            log(f"loop error: {type(e).__name__}: {e}; reconnecting in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)


if __name__ == "__main__":
    main()
