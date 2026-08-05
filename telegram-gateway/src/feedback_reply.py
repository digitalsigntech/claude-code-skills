"""Dev replies in a feedback group -> back to the user who reported (opt-in).

If your app POSTs user feedback into a Telegram group, this closes the loop:
a reply typed in that group is delivered back to the reporter inside the app,
instead of sitting in a chat they cannot see.

Two address forms, both resolved before any reflex or model turn:

  swipe-reply  reply to the bot's feedback post; the message_id is looked up
               in a map your poster writes when it posts a report
  @acct-id     explicit — needed once a report has aged out of the map, or to
               open a conversation with a user who never reported anything

The text is POSTed to your backend, which queues it for that account; the app
collects it on its next poll.

Config (all env, feature is OFF unless the first two are set):
  TG_FEEDBACK_CHAT       chat id of the feedback group
  TG_DEVREPLY_URL        backend ingest endpoint (POST {account,text,author})
  TG_DEVREPLY_SECRET     bearer for it (or TG_DEVREPLY_SECRET_FILE)
  TG_DEVREPLY_MAP        JSON file: {"<message_id>": {"account": "..."}, ...}
  TG_DEVREPLY_PREFIX     id prefix to match, default "acct-"

Owner-gated: this writes to a user's phone, so only OWNER_ID may send. Wire
the map file from whatever posts the reports — without it only @id works.
"""
import json, os, re
import urllib.error, urllib.request

import tgconf as C
import tg_api as TG


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


FEEDBACK_CHAT = _int(os.environ.get("TG_FEEDBACK_CHAT", "0"))
INGEST_URL = os.environ.get("TG_DEVREPLY_URL", "")
MAP_FILE = os.environ.get("TG_DEVREPLY_MAP", "")
PREFIX = os.environ.get("TG_DEVREPLY_PREFIX", "acct-")
ENABLED = bool(FEEDBACK_CHAT and INGEST_URL)

ADDR_RE = re.compile(r"^\s*@(" + re.escape(PREFIX) + r"[A-Za-z0-9_-]+)\s+(.+)",
                     re.S | re.I)


def _secret():
    s = os.environ.get("TG_DEVREPLY_SECRET", "")
    if s:
        return s
    path = os.environ.get("TG_DEVREPLY_SECRET_FILE", "")
    try:
        return open(path).read().strip() if path else ""
    except OSError:
        return ""


def _lookup(mid):
    if not MAP_FILE:
        return None
    try:
        return json.load(open(MAP_FILE)).get(str(mid))
    except Exception:
        return None


def _push(account, text, author):
    """POST to the backend. Returns (ok, note) — note is shown in the group."""
    secret = _secret()
    body = json.dumps({"account": account, "text": text,
                       "author": author}).encode()
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    try:
        with urllib.request.urlopen(
                urllib.request.Request(INGEST_URL, data=body,
                                       headers=headers), timeout=20) as rp:
            return bool(json.load(rp).get("delivered")), ""
    except urllib.error.HTTPError as e:
        # A typo must fail LOUDLY, in the room where it was typed: silence
        # here is indistinguishable from a delivered message.
        return False, (f"no account `{account}`" if e.code == 404
                       else f"HTTP {e.code}")
    except Exception as e:
        return False, str(e)[:120]


def resolve(msg, text):
    """(account, message_text, how) or (None, None, None).

    An explicit id wins over the reply target: someone who types an id while
    replying means the id.
    """
    m = ADDR_RE.match(text or "")
    if m:
        return m.group(1), m.group(2).strip(), "@id"
    mid = (msg.get("reply_to_message") or {}).get("message_id")
    if mid:
        hit = _lookup(mid)
        if hit and hit.get("account"):
            return hit["account"], (text or "").strip(), "reply"
    return None, None, None


def try_handle(msg, chat_id, text):
    """Route a dev reply. Returns a log summary if handled, else None."""
    if not ENABLED or chat_id != FEEDBACK_CHAT or not (text or "").strip():
        return None
    account, body, how = resolve(msg, text)
    if not account or not body:
        return None
    uid = (msg.get("from") or {}).get("id")
    if C.OWNER_ID and uid != C.OWNER_ID:
        TG.send_message(chat_id, "⚠️ Only the owner can send a reply to a "
                                 "user's app — this stayed in the group.",
                        reply_to=msg["message_id"])
        return f"dev-reply BLOCKED (not owner: {uid}) -> {account}"
    author = ((msg.get("from") or {}).get("first_name") or "Support").strip()
    ok, note = _push(account, body[:2000], author)
    TG.send_message(
        chat_id,
        f"✅ Sent to `{account}` — it lands in their app." if ok else
        f"⚠️ NOT delivered to `{account}`{' — ' + note if note else ''}",
        reply_to=msg["message_id"])
    return f"dev-reply via {how} -> {account} ok={ok}{' ' + note if note else ''}"
