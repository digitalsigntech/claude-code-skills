#!/usr/bin/env python3
"""
Claude Code auth watchdog (cron, every ~10 min).

A Claude Code subscription login can die silently: OAuth refresh tokens are
single-use, and concurrent `claude` processes sharing ~/.claude/.credentials.json
(a chat gateway, cron jobs, an interactive session) can race the token refresh —
the loser gets `invalid_grant` and the whole session is invalidated (see
anthropics/claude-code issues #56339, #24317, #43392). When that happens every
headless agent on the box is down until someone runs /login, and nothing says so.
This watchdog does.

How it decides (cheap check first, live probe only when suspicious):
  1. credentials file missing/unparseable        -> DEAD, alert
  2. accessToken not yet expired                 -> OK
  3. expired < grace_min                         -> OK (next real turn refreshes it)
  4. expired >= grace_min -> live probe: one minimal `claude -p` turn (a
     successful turn rewrites the token). Probe auth error -> DEAD, alert;
     other probe failure (network, timeout) -> logged only, prior state kept.

Alerts go out over the plain Telegram Bot API, so they work precisely when
Claude itself can't. One alert per outage, a re-ping every `realert_hours`
while broken, one recovery message when auth comes back. Quiet OK runs write
nothing, so the log only grows on state changes and probes.

Config: reads the same src/config.json as health_check.py — the `telegram`
section for the ping target plus an optional `auth_watchdog` section (see
config.example.json). Every field has a fallback.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))


def load_config():
    try:
        with open(os.path.join(HERE, "config.json")) as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}

    def expand(p):
        return os.path.expanduser(p) if isinstance(p, str) else p

    tg = cfg.get("telegram", {})
    aw = cfg.get("auth_watchdog", {})
    return {
        "bot_token_file": expand(tg.get("bot_token_file", "")),
        "chat_id": tg.get("chat_id", ""),
        "credentials_file": expand(aw.get("credentials_file",
                                          "~/.claude/.credentials.json")),
        "claude_bin": expand(aw.get("claude_bin", "claude")),
        "probe_model": aw.get("probe_model", "haiku"),
        "probe_timeout_sec": aw.get("probe_timeout_sec", 120),
        "grace_min": aw.get("grace_min", 60),
        "realert_hours": aw.get("realert_hours", 6),
        "host_label": aw.get("host_label", os.uname().nodename),
    }


CFG = load_config()

CREDS = CFG["credentials_file"]
STATE = os.path.join(HERE, "state/auth_check.json")
LOG = os.path.join(HERE, "logs/auth_check.log")

AUTH_ERR_MARKERS = ("oauth", "401", "unauthorized", "authentication", "invalid_grant",
                    "log in", "login", "invalid api key", "revoked")


def now():
    return time.time()


def stamp():
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")


def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{stamp()} {msg}\n")


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {"status": "ok", "alerted_at": 0}


def save_state(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(st, f)


def send_telegram(text):
    if not (CFG["bot_token_file"] and CFG["chat_id"]):
        log("telegram not configured; alert not sent")
        return False
    try:
        with open(CFG["bot_token_file"]) as f:
            token = f.read().strip()
        import urllib.request
        import urllib.parse
        data = urllib.parse.urlencode({
            "chat_id": CFG["chat_id"], "text": text, "parse_mode": "Markdown",
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        urllib.request.urlopen(req, timeout=20).read()
        return True
    except Exception as e:
        log(f"telegram send failed: {e}")
        return False


def token_expires_at():
    """Return expiresAt in epoch seconds, or None if the file is missing/broken."""
    try:
        with open(CREDS) as f:
            return json.load(f)["claudeAiOauth"]["expiresAt"] / 1000.0
    except Exception:
        return None


def probe():
    """Run a minimal headless Claude turn. Returns 'ok', 'auth', or 'other'."""
    try:
        r = subprocess.run(
            [CFG["claude_bin"], "-p", "Reply with exactly: ok",
             "--model", CFG["probe_model"]],
            capture_output=True, text=True, timeout=CFG["probe_timeout_sec"],
        )
    except Exception as e:
        log(f"probe error: {e}")
        return "other"
    if r.returncode == 0:
        return "ok"
    err = (r.stdout + r.stderr).lower()
    if any(m in err for m in AUTH_ERR_MARKERS):
        log(f"probe auth failure: {(r.stdout + r.stderr).strip()[:300]}")
        return "auth"
    log(f"probe non-auth failure (rc {r.returncode}): {(r.stdout + r.stderr).strip()[:300]}")
    return "other"


def main():
    st = load_state()
    exp = token_expires_at()
    host = CFG["host_label"]

    if exp is None:
        status = "dead"
        why = "credentials file missing or unreadable"
    elif exp > now():
        status = "ok"
        why = ""
    elif now() - exp < CFG["grace_min"] * 60:
        status = "ok"  # freshly expired; a normal turn will refresh it
        why = ""
    else:
        p = probe()
        if p == "ok":
            status = "ok"
            log(f"token was {int((now() - exp) / 60)} min past expiry; probe refreshed it")
            why = ""
        elif p == "auth":
            status = "dead"
            why = f"token expired {int((now() - exp) / 60)} min ago and probe got an auth error"
        else:
            status = st["status"]  # inconclusive (network etc.) — keep prior state
            why = ""

    if status == "dead":
        if st["status"] != "dead" or now() - st["alerted_at"] > CFG["realert_hours"] * 3600:
            sent = send_telegram(
                f"🔴 *Claude auth is DEAD on {host}* — " + why + ".\n"
                "Headless Claude agents on this box are down until someone runs "
                "`/login` in a Claude Code session there.\n"
                "_Likely the known OAuth refresh race between concurrent claude "
                "processes — not an account problem._")
            log(f"DEAD ({why}); alert sent={sent}")
            st = {"status": "dead", "alerted_at": now() if sent else st["alerted_at"]}
    else:
        if st["status"] == "dead":
            send_telegram(f"🟢 Claude auth on {host} is back — automation resumed.")
            log("recovered; recovery ping sent")
        st = {"status": "ok", "alerted_at": 0}

    save_state(st)


if __name__ == "__main__":
    sys.exit(main())
