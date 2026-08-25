"""Dev replies in the "User Feedback" group -> the reporter's app (#88).

the owner, 2026-08-05: "if I long-press on the message and select reply, can
this reply go directly to the user without me doing the @ mention?"

Two ways to address a user from the group, both handled here:

  swipe-reply  reply to the bot's feedback post; the message_id is looked up
               in the map voice/realtime/server.py writes when it posts a
               report (.feedback_msgmap.json)
  @acct-id     explicit, still works — needed to write to someone whose
               report has aged out of the map, or to start a conversation

Either way the text is POSTed to the VPS (/api/dev-messages/ingest), which
queues it for that account; the app collects it from GET /dev-messages.

Runs in the gateway rather than as a chat.db poller (the pre-#88 design) for
two reasons: chat.db stores no message ids, so swipe-replies are invisible
there — and a message addressed to a USER must not also become a Claude turn.
the owner, same day: "This message was addressed to a user, not to you."

Owner-gated on purpose: this pushes text out to a paying customer's phone,
which is not something a guest in the group gets to do.
"""
import tgconf as C   # identity from config
import json, os, re, time
import urllib.error, urllib.request

import tg_api as TG

FEEDBACK_CHAT = C.EXAMPLE_CHAT_ID
REALTIME_DIR = "<workspace>/voice/realtime"
MAP_FILE = os.path.join(REALTIME_DIR, ".feedback_msgmap.json")
SECRET_FILE = os.path.join(REALTIME_DIR, ".hook_secret")
INGEST_URL = os.environ.get(
    "HOSTED_INGEST_URL",
    "https://app.agentvoicemode.ai/api/dev-messages/ingest")

# Same shape the VPS resolves case-insensitively (resolve_account).
ADDR_RE = re.compile(r"^\s*@(acct-[A-Za-z0-9_-]+)\s+(.+)", re.S | re.I)

try:
    import sys
    sys.path.insert(0, "<workspace>/operations/accounts")
    import accounts as _accounts
except Exception:                                    # pragma: no cover
    _accounts = None


def _secret():
    try:
        return open(SECRET_FILE).read().strip()
    except OSError:
        return None


def _is_owner(uid):
    if not _accounts:
        return False
    u = _accounts.get(uid) or {}
    return bool((u.get("privileges") or {}).get("admin"))


def _lookup(mid):
    """account for a replied-to feedback message, or None."""
    try:
        return json.load(open(MAP_FILE)).get(str(mid))
    except Exception:
        return None


def _map_add(msg_ids, account):
    """Extend the map so a THREAD keeps routing (2026-08-05).

    The report post is only the first message of a conversation: after a reply
    goes out, the natural next move is to reply again — to one's own message or
    to the confirmation. Mapping those too means the second message reaches the
    same user instead of silently becoming a Claude turn.
    """
    ids = [i for i in msg_ids if i]
    if not ids or not account:
        return
    try:
        try:
            m = json.load(open(MAP_FILE))
        except Exception:
            m = {}
        for mid in ids:
            m[str(mid)] = {"account": account, "ts": time.time()}
        if len(m) > 500:
            m = dict(sorted(m.items(), key=lambda kv: kv[1].get("ts", 0),
                            reverse=True)[:500])
        tmp = MAP_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(m, fh)
        os.replace(tmp, MAP_FILE)
    except Exception:
        pass          # routing already happened; a lost thread link is minor


MINT_URL = os.environ.get(
    "DEVREPLY_MINT_URL",
    "http://127.0.0.1:8478/{secret}/mint-token")


def _mint(path):
    """Token for a picture attached to a reply (#91), or (None, None).

    The bytes never leave the box: the app fetches them through the VPS's
    GET /file/<token>, which relays {"type":"file"} back here. Minting has to
    happen inside the running server — its token map is loaded at startup.
    """
    secret = _secret()
    try:
        url_secret = open(os.path.join(REALTIME_DIR, ".secret")).read().strip()
    except OSError:
        return None, None
    try:
        rq = urllib.request.Request(
            MINT_URL.format(secret=url_secret),
            data=json.dumps({"path": os.path.abspath(path)}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {secret}"})
        with urllib.request.urlopen(rq, timeout=20) as rp:
            d = json.load(rp)
        return d.get("token"), d.get("filename")
    except Exception as e:
        print(f"[feedback_reply] mint failed: {e}")
        return None, None



def _display_name(account, report_id=None):
    """WHO the reply reached: the person, then the company.

    the owner, 2026-08-17: "We need to replace the demo thing with THE USER NAME
    AND COMPANY NAME." Both, and in that order — he is answering a specific
    human and this line is his receipt that it reached them. Company alone
    cannot even tell two demo accounts apart: every one of them is Summit.

    The person comes from the REPORT (the app sends `user` in its context); the
    company is the account name. Falls back to the account id only for a real
    account with nothing else, and never for a guest — that id is the one
    string a customer must not read over his shoulder.
    """
    acct = str(account or "")
    person = ""
    if report_id:
        try:
            sys.path.insert(0, REALTIME_DIR)
            import feedback_log
            for r in feedback_log._rows():
                if str(r.get("id")) == str(report_id):
                    person = str((r.get("context") or {}).get("user") or "")
                    break
        except Exception:
            person = ""
    company = ""
    try:
        import urllib.request as _u2
        rq2 = _u2.Request(INGEST_URL.replace("/dev-messages/ingest", "/reporters"),
                          headers={"Authorization": f"Bearer {_secret()}"})
        with _u2.urlopen(rq2, timeout=6) as rp2:
            for r in json.load(rp2).get("reporters", []):
                if r.get("id") == acct and r.get("name"):
                    company = str(r["name"])
                    break
    except Exception:
        company = ""
    both = " \u00b7 ".join(x for x in (person, company) if x)
    if both:
        return both
    return "the demo account" if acct.startswith("demo-") else f"`{acct}`"


def _push(account, text, author, report_id=None, image_path=None):
    """POST to the VPS. Returns (ok, note) — note is shown in the group."""
    secret = _secret()
    if not secret:
        return False, "no shared secret on this box"
    img_tok = img_name = None
    if image_path:
        img_tok, img_name = _mint(image_path)
    body = json.dumps({"account": account, "text": text,
                       "author": author,
                       **({"report_id": report_id} if report_id else {}),
                       **({"image_token": img_tok, "image_name": img_name}
                          if img_tok else {})}).encode()
    rq = urllib.request.Request(
        INGEST_URL, data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {secret}"})
    try:
        with urllib.request.urlopen(rq, timeout=20) as rp:
            return bool(json.load(rp).get("delivered")), ""
    except urllib.error.HTTPError as e:
        # 404 = unknown/retired id. Loud, in the room where it was typed:
        # a typo that vanishes silently reads exactly like a delivery.
        return False, (f"no account `{account}`" if e.code == 404
                       else f"HTTP {e.code}")
    except Exception as e:
        return False, str(e)[:120]


def resolve(msg, text):
    """(account, message_text, how, report_id) for a group message.

    The explicit @acct- form wins over the reply target: someone who types an
    id while replying means the id. #92: a swipe-reply also carries WHICH
    report it answers; a bare @acct- mention has no report to point at, and
    None is the honest answer rather than the nearest guess.
    """
    m = ADDR_RE.match(text or "")
    if m:
        return m.group(1), m.group(2).strip(), "@id", None
    r2 = msg.get("reply_to_message") or {}
    mid = r2.get("message_id")
    if mid:
        hit = _lookup(mid)
        if hit and hit.get("account"):
            return (hit["account"], (text or "").strip(), "reply",
                    hit.get("report_id"))
    return None, None, None, None


def try_handle(msg, chat_id, text, image_path=None):
    """Route a dev reply. Returns a log summary if handled, else None.

    `image_path` is set when the message was a PHOTO with a caption (#91,
    the owner: "I am testing image sending"). A picture with a caption is one
    message to the user, not a caption that routes and a picture that stays
    behind.
    """
    if chat_id != FEEDBACK_CHAT or not (text or "").strip():
        return None
    account, body, how, report_id = resolve(msg, text)
    if not account:
        return None
    uid = (msg.get("from") or {}).get("id")
    if not _is_owner(uid):
        # Not silent: the person typed to a customer and must know it didn't go.
        TG.send_message(chat_id, "⚠️ Only the workspace owners can send a reply to a "
                                 "user's app — this stayed in the group.",
                        reply_to=msg["message_id"])
        return f"dev-reply BLOCKED (non-owner {uid}) -> {account}"
    if not body:
        return None
    author = ((msg.get("from") or {}).get("first_name") or "the workspace").strip()
    ok, note = _push(account, body[:2000], author, report_id, image_path)
    if ok and report_id:
        # #99: "is anything outstanding" must separate a report nobody has
        # answered from one that is answered and still open.
        try:
            sys.path.insert(0, REALTIME_DIR)
            import feedback_log
            feedback_log.mark_replied(int(report_id), author)
        except Exception as e:
            print(f"[feedback_reply] mark_replied failed: {e}", flush=True)
    pic = " (with the picture)" if (ok and image_path) else ""
    # The confirmation names WHO, not the key. 2026-08-17: this group is read
    # over his shoulder while he demonstrates the app, and `demo-4e84ad8e0220`
    # is the one word a customer must never see. The reply is routed by the
    # message-id map either way, so the id here was only ever for the reader.
    who = _display_name(account, report_id)
    conf = TG.send_message(
        chat_id,
        f"✅ Sent to {who}{pic} — it lands in their app." if ok else
        f"⚠️ NOT delivered to {who}{' — ' + note if note else ''}",
        reply_to=msg["message_id"])
    if ok:
        # Keep the thread routable: this message and the confirmation both
        # now resolve to the same account.
        cid = ((conf or {}).get("result") or {}).get("message_id") \
            if isinstance(conf, dict) else None
        _map_add([msg["message_id"], cid], account)
    return f"dev-reply via {how} -> {account} ok={ok}{' ' + note if note else ''}"


if __name__ == "__main__":                       # manual check
    import sys
    acct = sys.argv[1] if len(sys.argv) > 1 else "acct-owner"
    txt = sys.argv[2] if len(sys.argv) > 2 else "test from feedback_reply.py"
    print(_push(acct, txt, "the owner"), time.strftime("%H:%M:%S"))
