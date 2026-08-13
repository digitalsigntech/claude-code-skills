#!/usr/bin/env python3
"""Pair this machine's agent with the voice plane.

    python3 pair.py --api https://…/api/ --token <account token> --url https://…/
    python3 pair.py --test          # ask the plane to test the connection
    python3 pair.py --status        # what the plane currently has registered

`--url` is the address the PLANE will call. It must be reachable from the public
internet and serve HTTPS. If this machine has no public address, run `tunnel.py`
instead — it creates the URL and pairs with it for you.

The account token comes from the app (Settings, or the pairing QR). It is written
to config.json and reused for `--test` and `--status`.
"""
import argparse, json, pathlib, sys, urllib.error, urllib.request

HERE = pathlib.Path(__file__).resolve().parent
CONFIG = HERE / "config.json"


def cfg():
    try:
        return json.loads(CONFIG.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save(d):
    CONFIG.write_text(json.dumps(d, indent=2) + "\n")
    try:
        CONFIG.chmod(0o600)
    except OSError:
        pass


def call(api, path, token, payload=None, method=None):
    url = api.rstrip("/") + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        raise SystemExit(f"plane returned HTTP {e.code}: {body}")
    except urllib.error.URLError as e:
        raise SystemExit(f"could not reach the plane at {url}: {e.reason}")


def register(api, token, url, secret):
    """Tell the plane where to find this agent. The plane probes the URL now,
    so a failure here means it genuinely could not reach you — not a stored
    setting that silently never worked."""
    r = call(api, "/agent", token, {"url": url, "secret": secret})
    print(f"registered: {url}")
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", help="plane API base, e.g. https://host/api/")
    ap.add_argument("--token", help="account token from the app")
    ap.add_argument("--url", help="public HTTPS URL of this agent's webhook")
    ap.add_argument("--test", action="store_true", help="plane-side connection test")
    ap.add_argument("--status", action="store_true", help="what the plane has registered")
    a = ap.parse_args()

    c = cfg()
    api = a.api or c.get("api")
    token = a.token or c.get("token")
    if not api or not token:
        raise SystemExit("need --api and --token at least once (they are then saved)")

    secret = c.get("secret")
    if not secret:
        raise SystemExit("no webhook secret yet — start voice_agent.py once to generate one")

    c.update({"api": api, "token": token})
    if a.url:
        c["public_url"] = a.url
    save(c)

    if a.status:
        print(json.dumps(call(api, "/agent", token), indent=2))
        return
    if a.test:
        r = call(api, "/agent/test", token, {})
        print(json.dumps(r, indent=2))
        # The plane distinguishes unreachable from signed_out because the two
        # have opposite remedies. Pass that distinction through, do not flatten it.
        if not r.get("ok"):
            reason = r.get("reason")
            if reason == "signed_out":
                print("\nThe plane reached this machine, but Claude here is signed out.\n"
                      "Fix it HERE — run `claude` in a terminal on this machine and log in.\n"
                      "Re-pairing will not help.", file=sys.stderr)
            else:
                print("\nThe plane could not reach this machine.\n"
                      "Check the agent is running, the URL is correct and public, "
                      "and HTTPS works from outside.", file=sys.stderr)
            raise SystemExit(1)
        print("\nConnected.")
        return

    url = a.url or c.get("public_url")
    if not url:
        raise SystemExit("need --url (or run tunnel.py if this machine has no public address)")
    register(api, token, url, secret)
    print("now run:  python3 pair.py --test")


if __name__ == "__main__":
    main()
