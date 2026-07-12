#!/usr/bin/env python3
"""Autodraft liveness watchdog.

Born from a real incident: the autodraft cron entry was accidentally removed
during unrelated maintenance and no drafts were produced for 3 days — nothing
noticed until a customer inquiry was found sitting unanswered. Checks two things:
  1. the crontab still contains the autodraft run.sh line
  2. the log shows a "run complete" within the last STALE_MIN minutes
On failure, pings the same Telegram chat autodraft uses for draft notifications.
Alerts are throttled to one per condition per SNOOZE_H hours so a real outage
doesn't spam the chat every watchdog tick.

Cron it alongside the drafter, e.g.:
  */30 * * * * /usr/bin/python3 /path/to/autodraft/watchdog.py >> /path/to/autodraft/logs/watchdog.log 2>&1
"""
import json, os, re, subprocess, time, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "logs", "autodraft.log")
STATE = os.path.join(HERE, ".watchdog_state.json")
BOT_TOKEN_F = os.environ.get("AUTODRAFT_TG_TOKEN_FILE",
                             os.path.join(HERE, "tg_bot_token"))
NOTIFY_CHAT_F = os.environ.get("AUTODRAFT_TG_CHAT_FILE",
                               os.path.join(HERE, "tg_notify_chat"))
DEFAULT_CHAT = os.environ.get("AUTODRAFT_TG_CHAT_ID", "")

STALE_MIN = 35          # cron is */10 — 3 missed ticks = something is wrong
SNOOZE_H = 6


def notify(text):
    if not os.path.exists(BOT_TOKEN_F):
        return
    token = open(BOT_TOKEN_F).read().strip()
    chat = (open(NOTIFY_CHAT_F).read().strip()
            if os.path.exists(NOTIFY_CHAT_F) else DEFAULT_CHAT)
    if not chat:
        return
    data = urllib.parse.urlencode({
        "chat_id": chat, "text": text, "parse_mode": "Markdown",
        "disable_web_page_preview": "true"}).encode()
    urllib.request.urlopen(urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data), timeout=20)


def throttled(key):
    st = {}
    if os.path.exists(STATE):
        try:
            st = json.load(open(STATE))
        except Exception:
            st = {}
    if time.time() - st.get(key, 0) < SNOOZE_H * 3600:
        return True
    st[key] = time.time()
    json.dump(st, open(STATE, "w"))
    return False


def main():
    problems = []

    cron = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    if "autodraft/run.sh" not in cron:
        if not throttled("cron_missing"):
            problems.append("the `autodraft/run.sh` line is MISSING from the crontab")

    last = None
    try:
        with open(LOG, "rb") as f:
            f.seek(max(0, os.path.getsize(LOG) - 65536))
            for line in f.read().decode(errors="replace").splitlines():
                m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) run complete", line)
                if m:
                    last = m.group(1)
    except FileNotFoundError:
        pass
    stale = (last is None or
             time.time() - time.mktime(time.strptime(last, "%Y-%m-%d %H:%M:%S"))
             > STALE_MIN * 60)
    if stale and not throttled("log_stale"):
        problems.append(f"no completed run since {last or 'ever'} "
                        f"(threshold {STALE_MIN} min)")

    if problems:
        notify("⚠️ *Reply auto-drafting watchdog*\n" +
               "\n".join(f"• {p}" for p in problems) +
               "\nNew product inquiries are NOT being drafted.")


if __name__ == "__main__":
    main()
