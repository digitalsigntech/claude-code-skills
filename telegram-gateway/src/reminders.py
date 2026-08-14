#!/usr/bin/env python3
"""Shared reminder/scheduled-job queue for every agent on this machine.

Why: agents used to hand-roll one-shot crontab scripts (or worse, just *say*
"I'll ping you at 5pm" with nothing behind it — Nemotron, 2026-07-23). This is
the one auditable queue both agents write to, drained by a per-minute cron.

Kinds:
  ping — at fire time, send `text` verbatim to `chat_id` via Telegram. With
         --photo, the reminder fires as a photo with `text` as the caption, so
         "remind me to flip this" comes back with the picture you took
         (voice-app camera shots live in voice/realtime/camera/).
  task — at fire time, run `text` as an INSTRUCTION through the on-box private
         agent (privacy_route.py --answer: Nemotron + CRM/email/KB tools) and
         post its answer to `chat_id`. Use for conditional reminders like
         "ping only if we haven't replied to X" — the agent checks, then reports.
  delmsg — at fire time, delete Telegram message `text` (a message_id) from
         `chat_id`. Best-effort: an already-deleted message counts as done.
         Used to expire ephemeral sends (Agent Voice Mode login QRs, 2026-07-29).
         Bot API limit: bots can only delete messages < 48h old.

CLI:
  reminders.py add "YYYY-MM-DD HH:MM" <chat_id> <ping|task> "<text>" [--by NAME]
                          [--photo PATH] [--label SHORT] [--owner <key>|shared]
  reminders.py list [--all] [--owner NAME]
  reminders.py cancel <id|word>
  reminders.py edit <id|word> [--when ...] [--label ...] [--text ...]

OWNERSHIP ("per user"). Every reminder belongs to one
person, or to 'shared'. You see your own rows and the shared ones and nobody
else's — in lists, in the reflex, and when naming a row by number. A fired reminder
goes to the CHAT IT WAS ASKED IN, and emails its owner ('shared' emails both);
ownership governs who may see and edit it, not where it fires. "my reminders"
lists your own, "our reminders" lists the shared ones — never merged. Default owner is the creator, so an
agent queueing on someone's behalf MUST pass --owner. Rows predating this are
the other owner's.
  reminders.py fire          # cron entry point: fire everything due (max 3 attempts)

DELIVERY, as of 2026-08-12 — a fired reminder reaches its owner THREE ways:

  1. The CHAT it was asked in (Telegram), with the photo if it has one.
  2. An EMAIL to the owner; 'shared' mails both. Best-effort: the chat message
     has already gone and IS the reminder, so a mail failure never re-fires it.
  3. A PUSH to the phone, via POST /api/notify on the hosted plane (#161). The
     agent never talks to Apple — it says WHO and WHERE and the VPS, which
     holds the APNs key and the device tokens, decides what a notification may
     say. Suppressed while a voice session is live.

     The notification reads "Reminder notification" on the first line and the
     reminder's LABEL on the second (#169: iOS truncates a title to one line
     and wraps a body, so the words that matter go in the body). Locked, iOS
     shows the first line only — no text, no picture. A photo travels as a
     1024px derived copy, never the original: 21 MB into a notification
     extension fetches nothing at all (#164).

Stdlib only — runs on system python3. DB: reminders.db next to this file.
"""

# ---------------------------------------------------------------- MIRROR GUARD
# #107: a copy of this file lives in the voice-bridge-ios repo for review. One
# of those copies was RUN — make_account_qr.py minted a QR against a host the
# live script had stopped using. The README had asked for read-only for weeks;
# a README cannot stop an interpreter.
#
# This guard lives in the LIVE SOURCE on purpose, so it survives every sync: a
# guard added to the mirror would be overwritten by the next copy and would
# look like protection while being none. It never fires in production, because
# in production this file IS at one of the paths below.
#
# 2026-08-13: same correction as reminders_reflex.py. The guard refused to run
# anywhere except one path on one machine, so a SECOND REAL INSTALL (Max, on
# its own VPS) was told it was a review mirror. The copy that must never run is
# the one in the repo, which exists to be read.
import os as _os, sys as _sys
if "voice-bridge-ios" in _os.path.realpath(__file__).split(_os.sep):
    _sys.exit("This is the REVIEW COPY in the voice-bridge-ios repo, not a "
              "running install.\nEdit the installed copy on the machine that "
              "runs it, then re-sync the mirror.")
# ------------------------------------------------------------ END MIRROR GUARD

import argparse, fcntl, json, os, re, sqlite3, subprocess, sys, time, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
# The workspace this install lives in. Everything else is found relative to it,
# so a copy onto another machine cannot keep reading the first one's disk.
WORKSPACE_ROOT = os.environ.get("TG_WORKSPACE_ROOT") or os.path.dirname(os.path.dirname(HERE))
# Overridable so a test can point at a COPY — see the note in
# telegram/reminders_reflex.py: twice tonight a test cancelled real reminders.
DB = os.environ.get("REMINDERS_DB", os.path.join(HERE, "reminders.db"))
BOT_TOKEN_FILE = os.path.join(WORKSPACE_ROOT, "telegram", "bot_token")
SENDFILE = os.path.join(WORKSPACE_ROOT, "telegram", "sendfile.py")
EMAIL_PY = os.path.join(WORKSPACE_ROOT, "email", "venv", "bin", "python")
PRIVACY_ROUTE = os.path.join(WORKSPACE_ROOT, "email", "kb", "privacy_route.py")
MAX_ATTEMPTS = 3
TIME_FMT = "%Y-%m-%d %H:%M"
# The times in this queue are the OWNER'S local times — "7:40 Monday" is 7:40 in
# the shop, not on the server. A rented box is usually UTC, so an agent that
# correctly resolves the owner's Monday morning hands over "07:40" and mktime,
# reading the process timezone, buries it five hours early; "in 20 minutes" comes
# out in the past and fires the instant it is queued. Pin the conversion instead
# of inheriting it, and every path — add, edit, list, fire — agrees on the hour.
REMINDERS_TZ = os.environ.get("REMINDERS_TZ") or os.environ.get("TG_TZ")
if REMINDERS_TZ:
    os.environ["TZ"] = REMINDERS_TZ
    time.tzset()
# Where a reminder fires when the caller does not name a chat. An agent asked to
# "remind me tomorrow at nine" knows the time and the words; it does not know the
# numeric id of the chat it is being spoken to through, and an install where that
# id has to be guessed is one where the agent reaches for a scheduler it CAN call
# without asking — and the reminder then fires somewhere the owner never looks.
DEFAULT_CHAT_ID = os.environ.get("TG_REMINDER_CHAT_ID") or os.environ.get("TG_OWNER_CHAT_ID")


def _db():
    c = sqlite3.connect(DB, timeout=30)
    c.execute("""CREATE TABLE IF NOT EXISTS reminders(
        id INTEGER PRIMARY KEY,
        created_ts TEXT NOT NULL,
        created_by TEXT NOT NULL,          -- 'claude' | an owner key | ...
        chat_id INTEGER NOT NULL,
        when_local TEXT NOT NULL,          -- 'YYYY-MM-DD HH:MM' local time
        when_epoch REAL NOT NULL,
        kind TEXT NOT NULL,                -- 'ping' | 'task'
        text TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',  -- pending | done | failed | cancelled
        attempts INTEGER NOT NULL DEFAULT 0,
        fired_ts TEXT,
        result TEXT)""")
    # added 2026-08-03: optional image sent with the reminder (camera shot from
    # the voice app, screenshot, whatever the ask was about)
    cols = {r[1] for r in c.execute("PRAGMA table_info(reminders)")}
    if "photo" not in cols:
        c.execute("ALTER TABLE reminders ADD COLUMN photo TEXT")
    if "owner" not in cols:
        # "Per user": a reminder belongs to a PERSON, not
        # to the queue. One owner's rows must not appear in another's list, fire
        # into his chat, or email him — the same line the personal notes draw.
        # 'shared' is the explicit third value for things that are both of theirs.
        c.execute("ALTER TABLE reminders ADD COLUMN owner TEXT")
        # Everything queued before today was his or written for him.
        c.execute("UPDATE reminders SET owner=? WHERE owner IS NULL", (PRIMARY,))
        c.commit()
    if "label" not in cols:
        # On seeing a runbook where a reminder should be:
        # "this is not a reminder, these are instructions for the agent. Here
        # we need only a human readable reminder in a short form." A reminder
        # has TWO audiences — the agent that executes it and the person who
        # asked for it — and `text` had been serving only the first.
        c.execute("ALTER TABLE reminders ADD COLUMN label TEXT")
        c.commit()
    return c


def _tg_send(chat_id, text):
    token = open(BOT_TOKEN_FILE).read().strip()
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text[:4000]}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.loads(r.read())
    if not out.get("ok"):
        raise RuntimeError(f"telegram: {out}")


def _tg_photo(chat_id, path, caption):
    """Send `path` as a photo with `caption`, through sendfile.py (never raw Bot API:
    raw sends skip the attachment spool and the chat.db archive marker)."""
    r = subprocess.run([sys.executable, SENDFILE, str(chat_id), path, caption[:1000]],
                       capture_output=True, text=True, timeout=120)
    if "sent" not in (r.stdout or "").lower():
        raise RuntimeError(f"sendfile: {(r.stderr or r.stdout or '')[:200]}")


def _tg_delete(chat_id, msg_id):
    """deleteMessage, best-effort: 'not found' / 'can't be deleted' = already gone."""
    token = open(BOT_TOKEN_FILE).read().strip()
    data = urllib.parse.urlencode({"chat_id": chat_id, "message_id": int(msg_id)}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/deleteMessage", data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            out = json.loads(r.read())
    except urllib.error.HTTPError as e:
        out = json.loads(e.read() or b"{}")
    if out.get("ok"):
        return "deleted"
    desc = str(out.get("description", out))
    if "not found" in desc or "be deleted" in desc:
        return f"already gone ({desc[:120]})"
    raise RuntimeError(f"telegram: {desc[:200]}")


def summarize(text, limit=90):
    """A human one-liner from an agent runbook. Used when nobody passed a
    label — because the hand-written label only worked for the seven rows
    somebody remembered to write, and the next reminder created in a hurry
    would have put a runbook back in front of the owner.

    Deterministic and dull on purpose: strip the conditional framing an agent
    instruction opens with, take the first sentence, and stop at a word
    boundary. No model, no guessing at intent.
    """
    t = " ".join(str(text or "").split())
    if not t:
        return ""
    # "Check FIRST whether the order for X has already been placed — search…"
    # is procedure; the reminder inside it starts at "remind ... to".
    m = re.search(r"remind\s+\w+\s+to\s+(.+)", t, re.I)
    if m:
        t = m.group(1)
    t = re.sub(r"^(please\s+|kindly\s+)", "", t, flags=re.I)
    # Sentence enders only. Splitting on an em-dash turned "Order more of
    # these — the inline capsule filter" into "Order more of these", which
    # names nothing: the dash usually introduces the very thing being ordered.
    first = re.split(r"(?<=[.!?])\s+|;\s+", t)[0].strip()
    t = first or t
    if len(t) > limit:
        t = t[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"
    return t[:1].upper() + t[1:] if t else t


# Who a reminder can belong to. 'shared' is deliberate and rare: it takes an
# explicit "remind us" / "our", never a guess (the owner).
# Owner keys for this deployment. One person is the common case; "shared" is the
# list both of two owners can see. These are KEYS, not names: they appear in the
# database, so changing one means migrating rows.
OWNERS = tuple(x for x in (os.environ.get("TG_PRIMARY_OWNER_KEY", "owner"),
                           os.environ.get("TG_SECOND_OWNER_KEY", ""),
                           "shared") if x)
PRIMARY = OWNERS[0]


def owner_of(tg_id):
    """Telegram id -> owner key. None for anyone this install does not know —
    a guest cannot own one of these.

    A multi-user install answers from the accounts registry. MOST installs have
    one owner and no registry, and returning None for them is not neutral: every
    query then runs unfiltered, which looks perfect with one person and silently
    shows each of two people the other's rows on the day a second arrives. The
    gateway already knows who the owner is, so ask it."""
    try:
        sys.path.insert(0, os.path.join(WORKSPACE_ROOT, "operations", "accounts"))
        import accounts
        u = accounts.get(tg_id) or {}
        first = (u.get("name") or "").split()[0].lower()
        if first in OWNERS:
            return first
    except Exception:
        pass
    try:
        sys.path.insert(0, os.path.join(WORKSPACE_ROOT, "telegram"))
        import tgconf as C
        if tg_id and int(tg_id) == int(getattr(C, "OWNER_ID", 0) or 0):
            return PRIMARY
        second = getattr(C, "SECOND_OWNER_ID", 0)
        if tg_id and second and int(tg_id) == int(second) and len(OWNERS) > 2:
            return OWNERS[1]
    except Exception:
        pass
    return None


def owner_email(owner):
    # Where a reminder mails, if this install mails at all. Config: shipping one
    # company's staff addresses meant every install wrote to two strangers.
    primary = os.environ.get("TG_OWNER_EMAIL", "")
    second = os.environ.get("TG_SECOND_OWNER_EMAIL", "")
    table = {PRIMARY: [primary] if primary else [], "shared":
             [a for a in (primary, second) if a]}
    if len(OWNERS) > 2 and second:
        table[OWNERS[1]] = [second]
    return table.get(owner or PRIMARY, [primary] if primary else [])


def visible_to(owner):
    """The rows this scope names — ONE owner, never a union. "my reminders" and
    "our reminders" are different lists and are shown separately, which is why
    the table needs no owner column."""
    return list(OWNERS) if not owner else [owner]


def add(when_local, chat_id, kind, text, created_by="claude", photo=None,
        label=None, owner=None):
    """Queue a reminder. Returns (id, when_local). Raises ValueError on bad input.

    photo: optional path to an image sent with the reminder. Resolved and checked
    now, not at fire time — a typo'd path should fail while the user is still here.

    label: the SHORT HUMAN FORM, for lists a person reads ("Order Epson i3200
    driver boards from Meteor"). `text` stays whatever the agent needs at fire
    time, including a full conditional runbook. Always pass one for a `task`;
    without it a list has to show the runbook, which is what went wrong.
    """
    when_local = (when_local or "").strip()
    try:
        when_epoch = time.mktime(time.strptime(when_local, TIME_FMT))
    except ValueError:
        raise ValueError(f"bad time {when_local!r} — use 'YYYY-MM-DD HH:MM' (24h, local)")
    if kind not in ("ping", "task", "delmsg"):
        raise ValueError("kind must be 'ping', 'task' or 'delmsg'")
    if kind == "delmsg" and not str(text).strip().isdigit():
        raise ValueError("delmsg text must be a message_id (integer)")
    if not (text or "").strip():
        raise ValueError("text is empty")
    if photo:
        photo = os.path.abspath(os.path.expanduser(photo))
        if not os.path.isfile(photo):
            raise ValueError(f"photo not found: {photo}")
    if owner and owner not in OWNERS:
        raise ValueError(f"owner must be one of {', '.join(OWNERS)}")
    c = _db()
    cur = c.execute(
        "INSERT INTO reminders(created_ts, created_by, chat_id, when_local, "
        "when_epoch, kind, text, photo, label, owner) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (time.strftime("%Y-%m-%d %H:%M:%S"), created_by, int(chat_id),
         when_local, when_epoch, kind, text.strip(), photo,
         # Derived when absent, so a list can never show a runbook again —
         # the guarantee lives in the data rather than in remembering.
         (label or "").strip() or summarize(text) or None,
         # The creator owns it unless told otherwise. `created_by` is already
         # the asker's name for anything queued from chat.
         owner or (created_by if created_by in OWNERS else PRIMARY)))
    c.commit(); c.close()
    return cur.lastrowid, when_local


def cancel(rid):
    c = _db()
    n = c.execute("UPDATE reminders SET status='cancelled' WHERE id=? AND status='pending'",
                  (int(rid),)).rowcount
    c.commit(); c.close()
    return n == 1


def edit(rid, when_local=None, label=None, text=None):
    """Reschedule or reword a pending reminder. #105: the app can now say
    "move this to Tuesday" or "change it to say X", and those arrive as
    ordinary agent turns — this is what they act on, rather than a delete and
    a re-add, which would lose the photo and the id the app is holding."""
    sets, vals = [], []
    if when_local:
        try:
            epoch = time.mktime(time.strptime(when_local.strip(), TIME_FMT))
        except ValueError:
            raise ValueError(f"bad time {when_local!r} — use 'YYYY-MM-DD HH:MM'")
        sets += ["when_local=?", "when_epoch=?"]
        vals += [when_local.strip(), epoch]
    if label is not None:
        sets.append("label=?")
        vals.append(label.strip() or None)
    if text is not None:
        sets.append("text=?")
        vals.append(text.strip())
    if not sets:
        raise ValueError("nothing to change")
    # #114 (the owner): "I should be able to change anything about a completed
    # reminder." So editing is no longer limited to pending rows — and a row
    # moved into the FUTURE is re-armed, because a reminder with a future time
    # that will never fire is the most useless object in this system.
    rearmed = False
    c = _db()
    row = c.execute("SELECT status FROM reminders WHERE id=?",
                    (int(rid),)).fetchone()
    if not row:
        c.close()
        return False, False
    if when_local and time.mktime(time.strptime(when_local.strip(), TIME_FMT)) \
            > time.time() and row[0] != "pending":
        sets.append("status='pending'")
        sets.append("fired_ts=NULL")
        rearmed = True
    n = c.execute(f"UPDATE reminders SET {', '.join(sets)} WHERE id=?",
                  (*vals, int(rid))).rowcount
    c.commit(); c.close()
    return n == 1, rearmed


def resolve_id(target):
    """An id, or a word that names exactly one reminder.

    #116: `edit 34` means finding 34 first, which is a `list` call and, on a
    voice turn, a second model round trip. `edit latte` / `edit "William"` skips
    it. Pending rows win over finished ones, and anything matching two rows
    raises rather than picking — a wrong id here silently rewrites the wrong
    reminder.
    """
    if str(target).lstrip("#").isdigit():
        return int(str(target).lstrip("#"))
    needle = str(target).strip().lower()
    if len(needle) < 3:
        raise ValueError(f"{target!r} is too short to name a reminder")
    rows = list_rows(all_rows=True)

    def hits(pool):
        return [r for r in pool
                if needle in (r.get("label") or "").lower()
                or needle in (r.get("text") or "").lower()]

    for pool in ([r for r in rows if r["status"] == "pending"], rows):
        found = hits(pool)
        if len(found) == 1:
            return found[0]["id"]
        if len(found) > 1:
            ids = ", ".join(f"#{r['id']}" for r in found)
            raise ValueError(f"{target!r} matches {len(found)} reminders ({ids})"
                             " — name one by number")
    raise ValueError(f"no reminder matching {target!r}")


def owner_keys_in_db():
    """Owner keys present in the data, so a mismatch with this install's config
    cannot hide. Rows written while TG_PRIMARY_OWNER_KEY was unset are keyed to
    the default and then invisible to every per-owner query — they are still in
    the list, under a name nobody is asking for, and nothing says so."""
    try:
        c = _db()
        keys = {r[0] for r in c.execute(
            "SELECT DISTINCT COALESCE(owner,?) FROM reminders", (PRIMARY,))}
        c.close()
    except Exception:
        return set()
    return keys


def warn_on_key_mismatch(out=sys.stderr):
    stray = {k for k in owner_keys_in_db() if k and k not in OWNERS}
    if stray:
        print(f"reminders: rows are keyed to {sorted(stray)}, which this install "
              f"does not use (its keys are {list(OWNERS)}). Those rows will not "
              f"appear in a per-owner list. Re-key them, or set the env to match.",
              file=out)
    return stray


def list_rows(all_rows=False, owner=None):
    """owner: whose list this is. None means unfiltered (cron, CLI); a name
    means their own rows plus the shared ones, and nobody else's."""
    c = _db()
    # when_epoch is included because a caller that wants to keep the row's
    # HOUR while changing its DAY (#114) otherwise has to re-parse when_local.
    # The default owner is a VALUE. Placeholders bind in textual order, so the
    # one in the SELECT list is the first parameter and the WHERE clause's comes
    # after it — get that order wrong and rows come back under the wrong name
    # with no error to show for it.
    q = ("SELECT id, when_local, chat_id, kind, status, created_by, text, "
         "photo, when_epoch, COALESCE(owner,?) FROM reminders")
    where, vals = [], [PRIMARY]
    if not all_rows:
        where.append("status='pending'")
    if owner:
        vis = visible_to(owner)
        where.append("COALESCE(owner,?) IN (%s)" % ",".join("?" * len(vis)))
        vals.append(PRIMARY)
        vals += vis
    if where:
        q += " WHERE " + " AND ".join(where)
    rows = [dict(zip(("id", "when", "chat_id", "kind", "status", "by", "text",
                      "photo", "when_epoch", "owner"), r))
            for r in c.execute(q + " ORDER BY when_epoch", vals)]
    c.close()
    return rows


def _email_copy(row, body, photo=None):
    """Mail the fired reminder to its owner ("when you fire reminders
    here, please also send me an email about it. If a reminder has a photo,
    attach it to the email.").

    Best-effort by design: the Telegram send already happened and IS the
    reminder. A Gmail hiccup must not mark a delivered reminder as failed and
    fire it again next minute — it logs and moves on. Sent from the bot's
    mailbox to the owner's,
    the only direction the send policy allows.
    """
    owner = row.get("owner") or PRIMARY
    label = (row.get("label") or "").strip() or summarize(row["text"])
    md = [f"**{label}**", "", body]
    if photo:
        md += ["", f"_Photo attached: {os.path.basename(photo)}_"]
    md += ["", f"—  \nReminder #{row['id']}, scheduled for {row['when_local']}."]
    cmd = [EMAIL_PY, os.path.join(WORKSPACE_ROOT, "email", "gmailer.py"), "send",
           "--to", ", ".join(owner_email(owner)),
           "--subject", f"⏰ Reminder: {label[:120]}",
           "--body", "\n".join(md), "--md"]
    if photo:
        cmd += ["--attach", photo]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                           cwd=os.path.join(WORKSPACE_ROOT, "email"))
        if "Sent" not in (r.stdout or ""):
            print(f"reminder {row['id']}: email copy failed: "
                  f"{(r.stderr or r.stdout or '')[:200]}", file=sys.stderr)
    except Exception as e:
        print(f"reminder {row['id']}: email copy failed: {e}", file=sys.stderr)


NOTIFY_URL = "https://app.agentvoicemode.ai/api/notify"
NOTIFY_TOKEN_FILE = os.path.join(HERE, ".notify_token")
# owner -> the hosted account whose phone should buzz. 'shared' nudges both.
def _push_accounts():
    """Voice-app accounts to push to when a reminder fires. Empty unless this
    install says otherwise — the previous version hardcoded another deployment's
    account ids, so every install pushed at somebody else's phone."""
    raw = os.environ.get("TG_PUSH_ACCOUNTS", "")
    if raw:
        try:
            return json.loads(raw)
        except ValueError:
            pass
    acct = os.environ.get("TG_OWNER_PUSH_ACCOUNT", "")
    return {PRIMARY: [acct], "shared": [acct]} if acct else {}


PUSH_ACCOUNTS = _push_accounts()


BANNER_MAX_PX = 1024
BANNER_DIR = "/tmp/reminder-banners"


def _banner_copy(path):
    """A small JPEG of `path` for the notification banner, or `path` itself if
    it cannot be made. Never modifies the source."""
    try:
        if os.path.getsize(path) < 400_000:
            return path                       # already small enough to send
        os.makedirs(BANNER_DIR, exist_ok=True)
        out = os.path.join(BANNER_DIR,
                           os.path.splitext(os.path.basename(path))[0] + ".jpg")
        if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(path):
            return out
        r = subprocess.run(
            [EMAIL_PY, "-c",
             "import sys;from PIL import Image;"
             "im=Image.open(sys.argv[1]);im.thumbnail((%d,%d));"
             "im.convert('RGB').save(sys.argv[2],'JPEG',quality=82)"
             % (BANNER_MAX_PX, BANNER_MAX_PX), path, out],
            capture_output=True, text=True, timeout=120)
        if r.returncode == 0 and os.path.exists(out):
            print(f"banner copy: {os.path.getsize(path)//1024}KB -> "
                  f"{os.path.getsize(out)//1024}KB")
            return out
        print(f"banner copy failed: {(r.stderr or '')[:150]}", file=sys.stderr)
    except Exception as e:
        print(f"banner copy failed: {e}", file=sys.stderr)
    return path


def _file_token(path):
    """A token the app can fetch that photo with, minted by the RUNNING box
    adapter — its token map lives there, and deriving a second one here would
    be a second copy of the same secret rule (the same reasoning as
    telegram/reminders_reflex._mint)."""
    if not path or not os.path.isfile(path):
        return None
    # #164: A BANNER IS NOT AN ARCHIVE COPY. The first photo push sent the
    # untouched original — 21 MB — to a Notification Service Extension, which
    # has seconds and a small memory budget, and Apple caps a notification
    # image attachment at 10 MB anyway. So it very probably fetched nothing and
    # showed nothing, which is exactly the "payload is right on both sides and
    # still no picture" case the app's developer warned about.
    #
    # A DERIVED copy, never the original (#119's rule): 1024px on the long
    # edge, quality 82, written beside the camera file and re-used if it is
    # already there. The full-resolution file is still what goes to Telegram,
    # to the email, and to /api/file when the app opens the notification.
    path = _banner_copy(path)
    rt = os.path.join(WORKSPACE_ROOT, "voice", "realtime")
    try:
        secret = open(os.path.join(rt, ".secret")).read().strip()
        bearer = open(os.path.join(rt, ".hook_secret")).read().strip()
        req = urllib.request.Request(
            f"http://127.0.0.1:8478/{secret}/mint-token",
            data=json.dumps({"path": os.path.abspath(path)}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {bearer}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r).get("token")
    except Exception as e:
        print(f"file token failed for {path}: {e}", file=sys.stderr)
        return None


def _push_app(row):
    """Nudge the phone (#161, the owner: "do the push notification on a
    reminder").

    Until now a fired reminder reached Telegram and email and NOTHING reached
    the app — so one asked for in a group the phone has since left could fire
    and be missed entirely. That was written into the user manual as a
    limitation on 2026-08-12; this removes it.

    BEST-EFFORT, exactly like the email copy: the Telegram message has already
    been sent and IS the reminder. A push that fails must never mark a
    delivered reminder as failed and fire it again next minute.

    WE SEND WHO AND WHERE, NEVER WHAT. The alert text lives on the server; this
    passes the chat and the row id so a tap opens the right conversation, and
    those ride outside the alert where a lock screen will not draw them.
    """
    try:
        token = open(NOTIFY_TOKEN_FILE).read().strip()
    except OSError:
        return                      # not configured: silently no push
    accounts = PUSH_ACCOUNTS.get(row.get("owner") or PRIMARY)
    if not accounts:
        return
    # #162 (the owner): the push shows the reminder — its words as the title and
    # its picture on the banner. The words come from the LABEL when there is
    # one, because that is the human form; `text` can be a runbook written to
    # the agent, and a runbook does not belong on a lock screen.
    title = (row.get("label") or "").strip() or summarize(row.get("text") or "")
    photo_token = _file_token(row.get("photo"))
    body = json.dumps({"kind": "reminder", "accounts": accounts,
                       "chat_id": row.get("chat_id"),
                       "reminder_id": row.get("id"),
                       "title": title[:120],
                       **({"photo_token": photo_token} if photo_token else {})
                       }).encode()
    req = urllib.request.Request(NOTIFY_URL, data=body, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            out = json.loads(r.read() or b"{}")
        print(f"reminder {row['id']}: push -> {out.get('pushed')}")
    except Exception as e:
        print(f"reminder {row['id']}: push failed: {e}", file=sys.stderr)


def _fire_one(row):
    rid, chat_id, kind, text = row["id"], row["chat_id"], row["kind"], row["text"]
    # "fire the reminders in the group where a user asks you about it"
    # (the owner): the chat it was ASKED IN is the
    # right place — that is where the context is and where he is looking.
    # Ownership decides who may SEE and EDIT it, and which mailbox gets the
    # email copy; it does not re-route the ping.
    photo = row.get("photo")
    if photo and not os.path.isfile(photo):
        photo = None  # camera shot cleaned up since queueing — still send the words
    if kind == "ping":
        if photo:
            _tg_photo(chat_id, photo, f"⏰ Reminder: {text}")
            _email_copy(row, text, photo)
            _push_app(row)
            return f"ping sent with photo {os.path.basename(photo)}"
        _tg_send(chat_id, f"⏰ Reminder: {text}")
        _email_copy(row, text)
        _push_app(row)
        return "ping sent"
    if kind == "delmsg":
        return "delmsg: " + _tg_delete(chat_id, text)
    # task: run the instruction through the on-box private agent, post its answer
    r = subprocess.run([EMAIL_PY, PRIVACY_ROUTE, text, "--json", "--answer",
                        "--sender", "Scheduler"],
                       capture_output=True, text=True, timeout=280)
    d = json.loads(r.stdout or "{}")
    answer = (d.get("answer") or "").strip()
    if not answer:
        raise RuntimeError(f"agent gave no answer: {(r.stderr or r.stdout)[:200]}")
    if d.get("files"):
        answer += "\n\n(Note: the agent located files; ask in chat to have them sent.)"
    if photo:
        _tg_photo(chat_id, photo, f"⏰ Scheduled task result:\n\n{answer}")
    else:
        _tg_send(chat_id, f"⏰ Scheduled task result:\n\n{answer}")
    _email_copy(row, answer, photo)
    _push_app(row)
    return "task ran: " + answer[:500]


def fire():
    # 2026-08-07: the cron fires every minute, but a `task` reminder can run for
    # minutes (it calls the on-box agent). Without a lock the next minute's run
    # sees the same row still 'pending' and fires it AGAIN — the owner got the ATIS
    # Bill of Lading task three times on 2026-08-04, with three different answers.
    # Whole-run lock: a second runner exits immediately rather than queueing.
    lock = open(os.path.join(HERE, ".fire.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    now = time.time()
    c = _db()
    # #161: owner, label and when_local are SELECTED because the helpers use
    # them — _email_copy reads row["when_local"] and would KeyError without it,
    # and _push_app needs the owner to know whose phone to buzz.
    #
    # This was live and unfired. The email copy was added on 2026-08-10 after
    # the last reminder went out, so the NEXT one would have sent its Telegram
    # message, raised in the email copy, been marked pending, and fired AGAIN
    # the following minute — up to three duplicate pings and then a "failed and
    # was dropped" notice for a reminder that had in fact been delivered three
    # times. That is the ATIS Bill of Lading incident of 2026-08-04 exactly,
    # which is why the whole-run lock above exists.
    cols = ("id", "chat_id", "kind", "text", "attempts", "photo",
            "owner", "label", "when_local")
    due = [dict(zip(cols, r)) for r in c.execute(
        f"SELECT {', '.join(cols)} FROM reminders "
        "WHERE status='pending' AND when_epoch<=? ORDER BY when_epoch", (now,))]
    c.close()
    for row in due:
        c = _db()
        c.execute("UPDATE reminders SET attempts=attempts+1 WHERE id=?", (row["id"],))
        c.commit(); c.close()
        try:
            result = _fire_one(row)
            status = "done"
        except Exception as e:
            result = f"attempt {row['attempts'] + 1} failed: {e}"
            status = "failed" if row["attempts"] + 1 >= MAX_ATTEMPTS else "pending"
            if status == "failed":
                try:
                    _tg_send(row["chat_id"],
                             f"⚠️ Reminder #{row['id']} failed {MAX_ATTEMPTS} times and was dropped: "
                             f"{row['text'][:200]}")
                except Exception:
                    pass
        print(f"{time.strftime('%F %T')} #{row['id']} {status}: {result}", flush=True)
        c = _db()
        c.execute("UPDATE reminders SET status=?, fired_ts=?, result=? WHERE id=?",
                  (status, time.strftime("%Y-%m-%d %H:%M:%S"), result[:1000], row["id"]))
        c.commit(); c.close()


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("when")
    a.add_argument("chat_id", type=int, nargs="?", default=None,
                   help="chat to fire into; omit to use TG_REMINDER_CHAT_ID")
    a.add_argument("kind", choices=("ping", "task", "delmsg")); a.add_argument("text")
    a.add_argument("--by", default="claude")
    a.add_argument("--photo", help="image to send with the reminder (e.g. the "
                                   "voice-app camera shot the ask was about)")
    a.add_argument("--label", help="SHORT human form for lists a person reads; "
                                   "`text` stays what the agent needs at fire "
                                   "time. Always pass one for a task.")
    l = sub.add_parser("list"); l.add_argument("--all", action="store_true")
    l.add_argument("--owner", choices=OWNERS,
                   help="show only this person's rows (theirs + shared)")
    ca = sub.add_parser("cancel")
    ca.add_argument("id", help="reminder id, or a word that names exactly one")
    a.add_argument("--owner", choices=OWNERS,
                   help="whose reminder this is (default: the creator)")
    e = sub.add_parser("edit")
    e.add_argument("id", help="reminder id, or a word that names exactly one "
                              "(e.g. 'latte', 'William')")
    e.add_argument("--when"); e.add_argument("--label"); e.add_argument("--text")
    sub.add_parser("fire")
    ns = ap.parse_args()
    if ns.cmd == "add":
        chat_id = ns.chat_id if ns.chat_id is not None else DEFAULT_CHAT_ID
        if chat_id in (None, ""):
            print("no chat to fire into: pass one, or set TG_REMINDER_CHAT_ID "
                  "for this install. Refusing to queue a reminder that would "
                  "fire nowhere.")
            return
        rid, when = add(ns.when, int(chat_id), ns.kind, ns.text, label=ns.label,
                        created_by=ns.by, owner=ns.owner,
                        photo=ns.photo)
        print(f"queued #{rid} for {when}" + (" (with photo)" if ns.photo else ""))
    elif ns.cmd == "list":
        warn_on_key_mismatch()
        for r in list_rows(ns.all, owner=getattr(ns, "owner", None)):
            print(f"#{r['id']} {r['when']} [{r['status']}] {r['kind']} chat={r['chat_id']} "
                  f"owner={r['owner']}: {r['text'][:100]}"
                  + (f"  📷 {os.path.basename(r['photo'])}" if r.get('photo') else ""))
    elif ns.cmd == "edit":
        try:
            rid = resolve_id(ns.id)
            ok, rearmed = edit(rid, ns.when, ns.label, ns.text)
        except ValueError as e:
            print(e); return
        print(f"reminder #{rid} " + ("updated" + (" and re-armed" if rearmed else "")
                                     if ok else "not found"))
    elif ns.cmd == "cancel":
        try:
            rid = resolve_id(ns.id)
        except ValueError as e:
            print(e); return
        print("cancelled" if cancel(rid) else "not found / not pending")
    elif ns.cmd == "fire":
        fire()


if __name__ == "__main__":
    main()
