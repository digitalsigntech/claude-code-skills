#!/usr/bin/env python3
"""Create/refresh this connector's hosted voice account and print the login QR.

The QR IS the onboarding (one scan, that's it): on first run this signs the
user up with the hosted voice service — presenting this connector's webhook,
which the service probes live before creating anything — and receives the
account bearer. The QR payload the iOS app scans:

    {"v":1, "type":"account", "token":"<bearer>", "name":"…", "api":"https://…/api/"}

Scanning signs the phone in AND the agent is already connected, because the
webhook was registered at signup. On later runs (the quick tunnel's URL
changes per restart) the stored account is kept and only the webhook URL is
re-synced — re-scan only if the user got signed out.

The QR carries a SHORT-LIVED scan-token (minted per run via POST /token/mint,
~15 min): unscanned it simply dies; once scanned the phone stays signed in.
The permanent account bearer stays in connector.json and never appears in a
QR. If you post the QR into a chat, schedule that chat message's deletion at
the printed expiry time (older hosted services without /token/mint fall back
to the permanent bearer — then deleting the message after scanning is a MUST).

`--payload` prints the JSON instead of rendering. `--name "Alice"` sets the
display name (first run only). New accounts start with a small welcome credit;
minutes are billed by the hosted service — that's their product.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

DIR = os.path.dirname(os.path.abspath(__file__))
CONF_PATH = os.path.join(DIR, "connector.json")
DEFAULT_API = "https://app.agentvoicemode.ai/api/"


def api_call(base, path, body, bearer=None):
    req = urllib.request.Request(
        base.rstrip("/") + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {bearer}"} if bearer else {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def detect_country():
    """Best-effort country of THIS machine (≈ the user), for the service's
    admin dashboard. Public-IP geolocation first, locale as fallback."""
    for url in ("https://ipapi.co/country_name/",
                "http://ip-api.com/line/?fields=country"):
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                c = r.read().decode().strip()
                if c and len(c) <= 56 and "error" not in c.lower():
                    return c
        except Exception:
            pass
    try:                                   # e.g. LANG=en_US.UTF-8 -> "US"
        loc = (os.environ.get("LC_ALL") or os.environ.get("LANG") or "")
        terr = loc.split(".")[0].split("_")
        return terr[1] if len(terr) > 1 and terr[1].isalpha() else None
    except Exception:
        return None


def detect_agent_type(conf):
    """Readable label for the agent behind this connector, from agent_cmd."""
    cmd = (conf.get("agent_cmd") or ["claude"])
    exe = os.path.basename(str(cmd[0] if isinstance(cmd, list) else cmd)
                           .split()[0])
    return {"claude": "Claude Code"}.get(exe, exe.capitalize() or None)


def push_profile(api, conf):
    """Tell the service what we know about our user — country, agent type.
    Fill-if-empty on the server; never overwrites the operator's edits.
    Older hosted services answer 404 — fine, skip silently."""
    meta = {"country": detect_country(),
            "agent_type": detect_agent_type(conf),
            "agent_name": conf.get("name") or None}
    meta = {k: v for k, v in meta.items() if v}
    if not meta:
        return
    try:
        api_call(api, "/profile", meta, bearer=conf["account_token"])
        print(f"[pair] profile pushed ({', '.join(sorted(meta))})")
    except Exception:
        pass


def main():
    conf = json.load(open(CONF_PATH))
    base = open(os.path.join(DIR, "url.txt")).read().strip().rstrip("/")
    api = conf.get("api") or DEFAULT_API
    hook_url = f"{base}/{conf['path']}/hook"
    for flag in ("name", "language"):
        for arg in sys.argv[1:]:
            if arg.startswith(f"--{flag}="):
                conf[flag] = arg.split("=", 1)[1]
        if f"--{flag}" in sys.argv:
            conf[flag] = sys.argv[sys.argv.index(f"--{flag}") + 1]
    if not conf.get("language"):
        # OS-locale fallback (en_US.UTF-8 -> en); the agent passing
        # --language from its own conversations beats this every time.
        loc = os.environ.get("LANG", "")[:2]
        conf["language"] = loc if loc.isalpha() else ""

    if not conf.get("account_token"):
        # A fresh quick-tunnel hostname can take ~30 s to become resolvable
        # from the service's side — retry rather than fail the first run.
        r = None
        for attempt in range(6):
            try:
                r = api_call(api, "/signup",
                             {"name": conf.get("name")
                                      or os.environ.get("USER", ""),
                              **({"language": conf["language"]}
                                 if conf.get("language") else {}),
                              "webhook_url": hook_url,
                              "webhook_secret": conf["secret"]})
                break
            except urllib.error.HTTPError as e:
                msg = e.read().decode()[:200]
                if e.code == 429 or attempt == 5:
                    sys.exit(f"signup failed (HTTP {e.code}): {msg}\n"
                             "Is the tunnel up (start.sh) and reachable "
                             "from outside?")
                print(f"[pair] not reachable yet ({msg.strip()[:60]}…) — "
                      f"retrying in 10 s", flush=True)
                time.sleep(10)
        conf.update(account_token=r["token"], account=r["account"], api=api)
        json.dump(conf, open(CONF_PATH, "w"), indent=1)
        os.chmod(CONF_PATH, 0o600)
        print(f"[pair] hosted account created: {r['account']} "
              f"(balance ${r['balance_cents'] / 100:.2f})")
    else:
        # tunnel URL may have changed since last run — keep the webhook fresh
        api_call(api, "/agent", {"url": hook_url, "secret": conf["secret"]},
                 bearer=conf["account_token"])
        print(f"[pair] webhook re-synced for {conf.get('account', '?')}")
    push_profile(api, conf)

    # The QR gets a fresh SHORT-LIVED scan-token (never the stored bearer):
    # unscanned it expires server-side in ~15 min; the first scan redeems it
    # into a normal permanent sign-in. Older hosted services without
    # POST /token/mint answer 404 — fall back to the stored bearer as before.
    qr_token, note = conf["account_token"], ""
    try:
        t = api_call(api, "/token/mint", {"ttl": 900},
                     bearer=conf["account_token"])
        qr_token = t["token"]
        exp = t.get("expires")
        note = (f"This QR EXPIRES in ~{max(1, int(t.get('ttl', 900)) // 60)} "
                "min"
                + (time.strftime(" (at %H:%M)", time.localtime(exp))
                   if exp else "")
                + " if not scanned — re-run qr.py for a fresh one. If you "
                  "send it into a chat, schedule that message's deletion at "
                  "expiry.")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        note = ("This hosted service predates expiring scan-tokens: the QR "
                "carries the permanent account credential — share it only "
                "with the person pairing and DELETE it after scanning (if "
                "sent into a chat, delete that chat message).")

    payload = json.dumps({"v": 1, "type": "account",
                          "token": qr_token,
                          "name": conf.get("name") or conf.get("account", ""),
                          "api": api}, separators=(",", ":"))
    if "--payload" in sys.argv:
        print(payload)
        print(f"[pair] {note}", file=sys.stderr)
        return
    png = os.path.join(DIR, "pairing-qr.png")
    subprocess.run(["qrencode", "-o", png, "-s", "8", payload], check=True)
    subprocess.run(["qrencode", "-t", "ANSIUTF8", payload], check=True)
    print(f"\nScan with Agent Voice Mode → Scan QR. One scan signs the phone "
          f"in AND connects this agent.\nPNG copy: {png}\n{note}")


if __name__ == "__main__":
    main()
