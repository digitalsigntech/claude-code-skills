#!/usr/bin/env python3
"""Fire due reminders: into the Telegram chat, and onto the phone.

2026-08-13. An owner, watching one arrive: *"This reminder came into the Claude
app. Instead, it should have come to my Telegram DM chat with the agent, and to
the iOS device."*

He was looking at the consequence of a real gap. The agent could CREATE reminders and
ANSWER about them — the table, the amend, the list all work — but it had no
firing path at all. What fired was a Claude cloud routine, because that is what
the agent reached for when it had nothing of its own, and a cloud routine
delivers into the Claude app. Reminders were being made and listed correctly
and going off somewhere he was not.

TWO DESTINATIONS, both of which now exist on this machine:

    Telegram  — the DM with this agent's bot, photo included. This IS the
                reminder; it is what he reads.
    APNs      — a nudge so the phone buzzes when the chat is not open, through
                the hosted plane's /api/notify.

THE PUSH IS BEST-EFFORT AND THE TELEGRAM SEND IS NOT. A push that fails must
never mark a delivered reminder as failed and fire it again a minute later —
the same rule the box's daemon follows, for the same reason.

WE SEND WHO AND WHERE, NEVER WHAT... except the title, which the plane puts in
the BODY of the alert (its own #169). The plane owns the wording; this passes
the reminder's words and its picture and nothing else.

Run every minute from cron. Overlap is impossible: an flock is held for the
whole pass, and a second copy exits rather than waiting.
"""
import fcntl
import json
import os
import subprocess
import sqlite3
import sys
import time
import urllib.request

# Everything is derived from the adapter's own config, so an install that
# already works needs no second set of paths: workdir comes from config.json,
# the store sits under it by convention, and the account is the one this
# machine is paired to. Any of them can still be overridden by env.
AGENT_DIR = os.environ.get("VOICE_AGENT_DIR", "/opt/voice-agent")
sys.path.insert(0, AGENT_DIR)
import voice_agent as _va                      # noqa: E402  (path first)

WORKDIR = os.path.expanduser(_va.config()["workdir"])
DB = os.environ.get("REMINDERS_DB",
                    f"{WORKDIR}/operations/reminders/reminders.db")
LOCK = "/tmp/fire-reminders.lock"
LOG = f"{WORKDIR}/operations/reminders/fired.log"

PLANE = os.environ.get("VOICE_PLANE",
                       "https://app.agentvoicemode.ai") + "/api/notify"
def _account():
    """Which account to nudge. Set VOICE_ACCOUNT in the cron line at install
    time; failing that, the paired account is the one with a session on this
    machine. Test accounts sort out because a real pairing is the only one that
    ever gets a session — but naming it explicitly is one line and removes the
    guess entirely."""
    env = os.environ.get("VOICE_ACCOUNT")
    if env:
        return env
    try:
        keys = [k for k in _va.load(_va.STATE, {}).get("sessions", {})
                if str(k).startswith("acct-")]
        return keys[-1] if len(keys) == 1 else (keys[-1] if keys else "")
    except Exception:
        return ""


ACCOUNT = _account()

# A reminder is late, not cancelled: something that was due while the machine
# was rebooting still deserves to arrive. Older than this and it is history,
# and firing it would be noise rather than a reminder.
GRACE_S = 6 * 3600
BANNER_PX = 1024                  # a lock-screen banner, not an archive copy


def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


def agent():
    """The adapter module — its Telegram helpers and its token minter, rather
    than a second copy of either."""
    return _va


def banner(path):
    """A DERIVED copy for the notification, never the original.

    The box learned this the expensive way: it sent a 21 MB original to a
    Notification Service Extension, which has seconds and a small memory budget
    while Apple caps the attachment at 10 MB. Nothing was drawn and both sides
    looked correct. Full resolution still goes to Telegram.
    """
    if not path or not os.path.isfile(path):
        return None
    out = os.path.join("/tmp/reminder-banners",
                       os.path.basename(path).rsplit(".", 1)[0] + ".jpg")
    if os.path.exists(out):
        return out
    os.makedirs("/tmp/reminder-banners", exist_ok=True)
    # PIL first: it is a library call, so it cannot be missing quietly the way
    # an external binary can. 2026-08-13: this machine had NEITHER `convert`
    # nor `ffmpeg`, every banner silently came back None, and the pushes went
    # out photoless while both sides looked correct — the app developer
    # asked why the banners had lost their pictures, and the answer was
    # two absent packages.
    try:
        from PIL import Image
        im = Image.open(path)
        im = im.convert("RGB")
        im.thumbnail((BANNER_PX, BANNER_PX))
        im.save(out, "JPEG", quality=82)
        if os.path.exists(out):
            return out
    except Exception as e:
        log(f"banner: PIL failed on {os.path.basename(path)}: {e}")
    for cmd in (["convert", path, "-auto-orient", "-resize",
                 f"{BANNER_PX}x{BANNER_PX}>", "-quality", "82", out],
                ["ffmpeg", "-y", "-loglevel", "error", "-i", path, "-vf",
                 f"scale='min({BANNER_PX},iw)':-2", "-q:v", "5", out]):
        try:
            if subprocess.run(cmd, timeout=60).returncode == 0 and \
                    os.path.exists(out):
                return out
        except Exception:
            continue
    # No downsizer at all: no banner, and certainly no 21 MB original. SAY SO —
    # the first version returned None here in silence, which is how a photoless
    # push became somebody else's mystery.
    log(f"banner: NO DOWNSIZER on this machine — {os.path.basename(path)} "
        f"pushed without a picture (install python3-pil or imagemagick)")
    return None


def push(va, row, title):
    """Nudge the phone. Authenticated with this agent's OWN plane secret, which
    the plane scopes to this account alone — the master notify token stays on
    the box that owns it."""
    try:
        secret = json.load(open(f"{AGENT_DIR}/config.json"))["secret"]
    except Exception:
        return "no secret"
    b = banner(row["photo"]) if row.get("photo") else None
    body = {"kind": "reminder", "account": ACCOUNT,
            "chat_id": row.get("chat_id"), "reminder_id": row["id"],
            "title": title[:120]}
    if b:
        body["photo_token"] = va.media_token(b)
    req = urllib.request.Request(
        PLANE, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {secret}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read() or b"{}")
    except Exception as e:
        return f"push failed: {e}"


def fire_one(va, row):
    """Telegram first, because that is the reminder; the push is a nudge."""
    title = (row.get("label") or "").strip() or row["text"]
    text = f"⏰ {title}"
    ok = va.tg_text(text)
    if row.get("photo") and os.path.isfile(row["photo"]):
        va.tg_file(row["photo"], title[:1000])
    p = push(va, row, title)
    log(f"#{row['id']} {row['when_local']} {title[:60]!r} "
        f"telegram={'ok' if ok else 'FAILED'} push={str(p)[:120]}")
    return ok


def main():
    if not os.path.exists(DB):
        return 0
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0                  # a pass is already running; never queue up
    now = time.time()
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT * FROM reminders WHERE status='pending' AND when_epoch <= ? "
        "AND when_epoch > ? ORDER BY when_epoch", (now, now - GRACE_S)
    ).fetchall()
    # Rows created as cloud routines are fired by the cloud. Firing them here
    # too would buzz him twice for one reminder, which is worse than either
    # channel alone.
    rows = [dict(r) for r in rows if (r["created_by"] or "") != "cloud-routine"]
    if not rows:
        c.close()
        return 0
    va = agent()
    for row in rows:
        try:
            ok = fire_one(va, row)
        except Exception as e:
            log(f"#{row['id']} EXCEPTION {e}")
            ok = False
        if ok:
            c.execute("UPDATE reminders SET status='done', fired_ts=?, "
                      "result='delivered' WHERE id=?",
                      (time.strftime('%Y-%m-%d %H:%M'), row["id"]))
        else:
            # Left pending on purpose: the next pass retries it. A reminder
            # nobody received is not a reminder that happened.
            c.execute("UPDATE reminders SET attempts=attempts+1 WHERE id=?",
                      (row["id"],))
        c.commit()
    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
