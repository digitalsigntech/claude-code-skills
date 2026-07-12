#!/usr/bin/env python3
"""idle_watcher — event-driven (push) new-mail watcher for a dedicated Gmail account.

Holds a persistent Gmail IMAP IDLE connection (XOAUTH2, reusing the OAuth token).
The server pushes the instant new mail arrives — no polling. On a new INBOX message
from an allow-listed sender (all other senders ignored), it fetches the body and
drops it as a JSON file into a queue directory. A downstream consumer (e.g. a chat
gateway) drains that queue and processes each email — for example, running it as a
real chat turn so an email from an allow-listed sender is treated like a typed message.

Run via start_idle_watcher.sh (flock single-instance) + @reboot + watchdog cron.

Configuration (env vars, all optional — sane defaults derived from this file's location):
  IDLE_INJECT_DIR   queue directory to drop JSON jobs into   (default ../queue)
  IDLE_BOT_TOKEN_F  file holding a chat-bot API token         (default ../gateway/bot_token)
  IDLE_ALLOWED      comma-separated allow-listed sender addrs  (default owner@example.com,peer@example.com)
  IDLE_DEFAULT_CHAT default chat/route id for notifications    (default 123456789)
"""
import os, sys, ssl, time, json, base64, socket, imaplib, subprocess, urllib.request, urllib.parse

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from auth import get_credentials
from config import ACCOUNTS

ACCOUNT      = ACCOUNTS["agent"]
IMAP_HOST    = "imap.gmail.com"
IMAP_PORT    = 993
STATE_FILE   = os.path.join(BASE, "idle_state.json")
LOG_FILE     = os.path.join(BASE, "logs", "idle_watcher.log")
BOT_TOKEN_F  = os.environ.get("IDLE_BOT_TOKEN_F", os.path.join(BASE, "..", "gateway", "bot_token"))
NOTIFY_CHAT_F= os.path.join(BASE, "idle_notify_chat")   # which chat/route to send to
DEFAULT_CHAT = os.environ.get("IDLE_DEFAULT_CHAT", "123456789")   # fallback chat/route id
INJECT_DIR   = os.environ.get("IDLE_INJECT_DIR", os.path.join(BASE, "..", "queue"))  # queue drained downstream
# This account is an interaction channel for the allow-listed senders only —
# every other sender is ignored.
ALLOWED_SENDERS = tuple(
    s.strip().lower() for s in
    os.environ.get("IDLE_ALLOWED", "owner@example.com,peer@example.com").split(",")
    if s.strip()
)
BODY_MAX     = 12000   # cap email body fed into the downstream turn

IDLE_RENEW   = 300     # re-issue IDLE every 5 min (Gmail drops IDLE ~29 min)
RECONNECT_EVERY = 1500 # full reconnect every 25 min (fresh access token)
SEEN_CAP     = 300


def log(msg):
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


# ---------- state ----------
def load_seen():
    try:
        return json.load(open(STATE_FILE)).get("seen_ids", [])
    except Exception:
        return []


def save_seen(ids):
    tmp = STATE_FILE + ".tmp"
    json.dump({"seen_ids": ids[:SEEN_CAP]}, open(tmp, "w"))
    os.replace(tmp, STATE_FILE)


# ---------- notify (optional chat-bot ping) ----------
def notify(text):
    try:
        token = open(BOT_TOKEN_F).read().strip()
    except Exception as e:
        log(f"notify: no bot token ({e})"); return
    chat = DEFAULT_CHAT
    if os.path.exists(NOTIFY_CHAT_F):
        chat = open(NOTIFY_CHAT_F).read().strip() or DEFAULT_CHAT
    data = urllib.parse.urlencode({
        "chat_id": chat, "text": text,
        "disable_web_page_preview": "true", "parse_mode": "Markdown",
    }).encode()
    # Example: a Telegram Bot API endpoint. Swap for whatever chat backend you use.
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
            r.read()
    except Exception as e:
        log(f"notify failed: {e}")


# ---------- gmail (detect what's new) ----------
def gmail_svc():
    from googleapiclient.discovery import build
    creds = get_credentials("agent", interactive=False)
    if not creds:
        raise RuntimeError("no agent credentials")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _hdr(m, name):
    for h in m.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _body_text(payload):
    """Recursively pull text/plain (fallback: html with tags stripped) from a payload."""
    if payload.get("mimeType", "").startswith("multipart"):
        return "\n".join(x for x in (_body_text(p) for p in payload.get("parts", [])) if x)
    data = payload.get("body", {}).get("data")
    if not data:
        return ""
    text = base64.urlsafe_b64decode(data).decode("utf-8", "replace")
    if payload.get("mimeType") == "text/html":
        import re
        text = re.sub(r"<[^>]+>", "", text)
    return text


def is_auto_error(full, frm, subj, body):
    """Detect automated noise we should NOT treat as a real message: bounces,
    out-of-office / vacation auto-replies, and bot error notices (e.g. a peer agent
    emitting 'the model returned empty content'). Returns a reason string, or None if
    it's a genuine message. Header checks first (RFC 3834), then sender, then text."""
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
    PATTERNS = (
        "returned empty content after retries",
        "the model returned empty content",
        "no reply: the model returned",
        "delivery status notification",
        "mail delivery failed",
        "undeliverable",
        "automatic reply",
        "out of office",
        "autoreply",
    )
    for p in PATTERNS:
        if p in text:
            return f'matched "{p}"'
    return None


def target_chat():
    if os.path.exists(NOTIFY_CHAT_F):
        return open(NOTIFY_CHAT_F).read().strip() or DEFAULT_CHAT
    return DEFAULT_CHAT


def enqueue_injection(rec):
    """Drop an email onto the queue a downstream consumer drains (atomic write)."""
    os.makedirs(INJECT_DIR, exist_ok=True)
    name = f"{int(time.time())}-{rec['id']}.json"
    dest = os.path.join(INJECT_DIR, name)
    tmp = dest + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rec, f)
    os.replace(tmp, dest)


def check_new(first_run=False):
    """List newest INBOX msgs, act on ones we haven't seen. Returns count of new."""
    s = gmail_svc()
    res = s.users().messages().list(userId="me", labelIds=["INBOX"], maxResults=15).execute()
    ids = [m["id"] for m in res.get("messages", [])]          # newest-first
    seen = load_seen()
    seen_set = set(seen)
    new = [i for i in ids if i not in seen_set]               # newest-first
    # always persist the merged seen set so we don't re-notify
    merged, s2 = [], set()
    for i in ids + seen:
        if i not in s2:
            s2.add(i); merged.append(i)
    save_seen(merged)
    if first_run or not new:
        return 0
    acted = 0
    for mid in reversed(new):                                 # oldest first
        m = s.users().messages().get(userId="me", id=mid, format="metadata",
                                     metadataHeaders=["From", "Subject"]).execute()
        frm = _hdr(m, "From"); subj = _hdr(m, "Subject") or "(no subject)"
        if not any(a in frm.lower() for a in ALLOWED_SENDERS):
            log(f"IGNORE {mid} | {frm} | {subj}  (sender not allow-listed)")
            continue
        # Policy: a mail from an allow-listed sender is treated like a chat message —
        # fetch the body and hand it to the downstream consumer to process as a turn.
        full = s.users().messages().get(userId="me", id=mid, format="full").execute()
        body = _body_text(full.get("payload", {})).strip()[:BODY_MAX]
        # Drop automated noise (bounces, OOO auto-replies, bot error notices) so they
        # never trigger a reply/turn — avoids error-driven ping-pong with a peer agent.
        reason = is_auto_error(full, frm, subj, body)
        if reason:
            log(f"SKIP-AUTO {mid} | {frm} | {subj}  ({reason})")
            continue
        log(f"NEW {mid} | {frm} | {subj}  -> queued as chat message")
        enqueue_injection({"id": mid, "from": frm, "subject": subj,
                           "body": body, "chat_id": target_chat(), "ts": int(time.time())})
        acted += 1
    return acted


# ---------- imap idle ----------
def imap_connect():
    creds = get_credentials("agent", interactive=False)
    if not creds:
        raise RuntimeError("no agent credentials")
    auth = f"user={ACCOUNT}\x01auth=Bearer {creds.token}\x01\x01"
    M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    M.authenticate("XOAUTH2", lambda _: auth.encode())
    M.select("INBOX")
    return M


def idle_once(M, timeout):
    """Issue IDLE, block up to `timeout`s for a push. Return True if INBOX changed."""
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
    log("idle_watcher starting")
    # prime seen-set so we don't notify for pre-existing mail on first boot
    try:
        if not os.path.exists(STATE_FILE):
            check_new(first_run=True)
            log("primed seen-set (no notifications for existing inbox)")
    except Exception as e:
        log(f"prime failed: {e}")
    backoff = 5
    while True:
        try:
            M = imap_connect()
            log("IMAP connected; IDLE loop active")
            backoff = 5
            # catch anything that arrived while we were disconnected
            check_new()
            deadline = time.time() + RECONNECT_EVERY
            while time.time() < deadline:
                remaining = max(10, min(IDLE_RENEW, int(deadline - time.time())))
                pushed = idle_once(M, remaining)
                # Check on every wake: instantly on a push (~2s), and as a cheap
                # safety-net poll on each 5-min IDLE renewal (catches dropped pushes).
                n = check_new()
                if n:
                    log(f"{n} new message(s) from allow-listed sender ({'push' if pushed else 'renewal-poll'}); queued")
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
