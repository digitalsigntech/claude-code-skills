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

`--payload` prints the JSON instead of rendering. `--name "Alice"` sets the
display name (first run only). New accounts start with a small welcome credit;
minutes are billed by the hosted service — that's their product.
"""
import json
import os
import subprocess
import sys
import urllib.request

DIR = os.path.dirname(os.path.abspath(__file__))
CONF_PATH = os.path.join(DIR, "connector.json")
DEFAULT_API = "https://2-24-102-182.sslip.io/api/"


def api_call(base, path, body, bearer=None):
    req = urllib.request.Request(
        base.rstrip("/") + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {bearer}"} if bearer else {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main():
    conf = json.load(open(CONF_PATH))
    base = open(os.path.join(DIR, "url.txt")).read().strip().rstrip("/")
    api = conf.get("api") or DEFAULT_API
    hook_url = f"{base}/{conf['path']}/hook"
    for arg in sys.argv[1:]:
        if arg.startswith("--name="):
            conf["name"] = arg.split("=", 1)[1]
    if "--name" in sys.argv:
        conf["name"] = sys.argv[sys.argv.index("--name") + 1]

    if not conf.get("account_token"):
        # A fresh quick-tunnel hostname can take ~30 s to become resolvable
        # from the service's side — retry rather than fail the first run.
        import time
        r = None
        for attempt in range(6):
            try:
                r = api_call(api, "/signup",
                             {"name": conf.get("name")
                                      or os.environ.get("USER", ""),
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

    payload = json.dumps({"v": 1, "type": "account",
                          "token": conf["account_token"],
                          "name": conf.get("name") or conf.get("account", ""),
                          "api": api}, separators=(",", ":"))
    if "--payload" in sys.argv:
        print(payload)
        return
    png = os.path.join(DIR, "pairing-qr.png")
    subprocess.run(["qrencode", "-o", png, "-s", "8", payload], check=True)
    subprocess.run(["qrencode", "-t", "ANSIUTF8", payload], check=True)
    print(f"\nScan with Agent Voice Mode → Scan QR. One scan signs the phone "
          f"in AND connects this agent.\nPNG copy: {png}\n"
          f"(The QR contains the account credential — share it only with the "
          f"person pairing, delete after scanning.)")


if __name__ == "__main__":
    main()
