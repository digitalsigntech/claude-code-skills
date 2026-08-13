#!/usr/bin/env python3
"""Pair this machine's agent with the voice plane.

    python3 pair.py --signup --url https://…/
                                    # no account yet: create one from here
    python3 pair.py --token <account token> --url https://…/
                                    # account already exists (token from the app)
    python3 pair.py --qr            # login QR for the phone (one scan = signed in)
    python3 pair.py --test          # ask the plane to test the connection
    python3 pair.py --status        # what the plane currently has registered

`--api` defaults to the plane behind the Agent Voice Mode app; pass it only to point
at a different deployment.

`--url` is the address the PLANE will call. It must be reachable from the public
internet and serve HTTPS. If this machine has no public address, run `tunnel.py`
instead — it creates the URL, signs up or re-registers with it, and keeps it fresh.

Two ways to get an account, and they are not equivalent:

  --signup   this agent creates the account, then `--qr` signs the phone in. The
             user needs nothing beforehand — no app account, no token to copy.
             The plane PROBES the webhook before creating anything, so the agent
             must already be serving and publicly reachable at `--url`.
  --token    the user already signed up in the app and read the token out of
             Settings. Use this when the account exists.
"""
import argparse, json, os, pathlib, subprocess, sys, time, urllib.error, urllib.request

HERE = pathlib.Path(__file__).resolve().parent
CONFIG = HERE / "config.json"

# The plane this adapter talks to by default — the service behind the Agent Voice
# Mode app. An installer cannot derive this and has nowhere to look it up, so a
# placeholder here is a blocker, not a configuration choice. Override with --api
# (or "api" in config.json) to point at a different deployment.
#
# The named host, never the IP-literal one it also answers to: an address a person
# is asked to trust with a credential has to LOOK like the product it belongs to.
# Existing installs keep whatever they stored, and the old host stays served.
DEFAULT_API = "https://app.agentvoicemode.ai/api/"


def cfg():
    try:
        return json.loads(CONFIG.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def identity(force=False):
    """Who this machine's agent is, derived by the adapter from its own project.

    Pairing is the moment it matters: the plane probes capabilities the instant we
    register, and an identity that arrives a minute later means the first QR scan
    shows a blank panel. So this blocks here rather than leaving it to the
    background refresh — once, at install, for the run that has nobody watching."""
    sys.path.insert(0, str(HERE))
    try:
        import voice_agent
    except ImportError:
        return {}
    if force or not voice_agent.cached_identity():
        print("[pair] working out who this agent is (one turn, cached)…", flush=True)
    return voice_agent.ensure_identity(force=force)


def save(d):
    CONFIG.write_text(json.dumps(d, indent=2) + "\n")
    try:
        CONFIG.chmod(0o600)
    except OSError:
        pass


def call(api, path, token, payload=None, method=None, timeout=60):
    url = api.rstrip("/") + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        raise SystemExit(f"plane returned HTTP {e.code}: {body}")
    except urllib.error.URLError as e:
        raise SystemExit(f"could not reach the plane at {url}: {e.reason}")


def _with_propagation_retry(attempt, label, tries=6, wait=10):
    """Run a plane call that PROBES a just-created public hostname.

    A brand-new quick-tunnel address is not resolvable from the plane's side for
    the first ~10-30 s, so the first attempt legitimately fails with "does not
    resolve". Treating that as final is how an agent ends up unreachable after
    every tunnel restart — the URL is fine, we just asked too early."""
    last = ""
    for i in range(tries):
        try:
            return attempt()
        except SystemExit as e:
            last = str(e)
            if "HTTP 429" in last or i == tries - 1:
                break
            print(f"[pair] {label}: not reachable from the plane yet — retrying in {wait}s "
                  f"({last[:90]})", flush=True)
            time.sleep(wait)
    raise SystemExit(last)


def register(api, token, url, secret):
    """Tell the plane where to find this agent. The plane probes the URL now,
    so a failure here means it genuinely could not reach you — not a stored
    setting that silently never worked."""
    r = _with_propagation_retry(
        lambda: call(api, "/agent", token, {"url": url, "secret": secret}), "register")
    print(f"registered: {url}")
    return r


def signup(api, url, secret, name=None, language=None):
    """Create the account from this machine, with this agent already attached.

    The plane probes the webhook before it creates anything, so this doubles as
    the only reachability test that matters. A freshly minted quick-tunnel
    hostname can take ~30 s to resolve from the plane's side, so a rejection on
    the first attempt means "not yet", not "never" — retry rather than fail the
    install on DNS propagation."""
    body = {"webhook_url": url, "webhook_secret": secret,
            # The account is named after the PERSON, not the unix user that
            # happened to run the install — "root" is the name they would
            # otherwise see on their own account.
            "name": name or identity().get("user_name")
                    or os.environ.get("USER", "") or "agent"}
    if language:
        body["language"] = language
    try:
        r = _with_propagation_retry(lambda: call(api, "/signup", None, body), "signup")
    except SystemExit as e:
        if "HTTP 429" in str(e):
            raise SystemExit(
                "the plane's daily signup cap is reached — try again tomorrow, "
                "or sign up in the app and pair with --token instead.")
        raise SystemExit(f"signup failed: {e}\n"
                         f"The plane must be able to reach {url} from the public internet. "
                         f"Check the agent is running and the tunnel or proxy is up.")
    print(f"account created: {r['account']} "
          f"(balance ${r.get('balance_cents', 0) / 100:.2f})")
    return r


def show_qr(api, token, name=None, payload_only=False):
    """Print the login QR for the phone: one scan signs the app in AND the agent
    is already attached, because the webhook was registered at signup.

    The QR carries a SHORT-LIVED scan-token minted per run, never the stored
    account bearer: unscanned it dies server-side in ~15 minutes, and the first
    scan redeems it into a normal permanent sign-in. A plane too old to mint one
    falls back to the permanent bearer — then the code must be deleted the moment
    it is scanned, and never left sitting in a chat."""
    qr_token, note = token, ""
    try:
        t = call(api, "/token/mint", token, {"ttl": 900})
        qr_token = t["token"]
        exp = t.get("expires")
        mins = max(1, int(t.get("ttl", 900)) // 60)
        note = (f"Expires in ~{mins} min"
                + (time.strftime(" (at %H:%M)", time.localtime(exp)) if exp else "")
                + " if not scanned — re-run `pair.py --qr` for a fresh one. If you send "
                  "it into a chat, schedule that message's deletion at expiry.")
    except SystemExit as e:
        if "HTTP 404" not in str(e):
            raise
        note = ("This plane predates expiring scan-tokens: the code carries the "
                "PERMANENT account credential. Show it only to the person pairing and "
                "delete it immediately after scanning.")

    blob = json.dumps({"v": 1, "type": "account", "token": qr_token,
                       "name": name or "", "api": api}, separators=(",", ":"))
    if payload_only:
        print(blob)
        print(f"[pair] {note}", file=sys.stderr)
        return
    png = HERE / "pairing-qr.png"
    try:
        subprocess.run(["qrencode", "-o", str(png), "-s", "8", blob], check=True)
        subprocess.run(["qrencode", "-t", "ANSIUTF8", blob], check=True)
        png_line = f"\nPNG copy: {png}"
    except (FileNotFoundError, subprocess.CalledProcessError):
        # No qrencode: the payload is still the whole credential, so the install
        # is not blocked on a rendering tool. Render it anywhere, or install
        # qrencode (`apt install qrencode` / `brew install qrencode`).
        print(blob)
        png_line = ("\nNo `qrencode` on this machine, so the payload above is printed "
                    "raw — install qrencode for an actual QR, or encode it yourself.")
    print(f"\nApp → Scan QR. One scan signs the phone in AND connects this agent."
          f"{png_line}\n{note}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", help=f"plane API base (default {DEFAULT_API})")
    ap.add_argument("--token", help="account token from the app")
    ap.add_argument("--url", help="public HTTPS URL of this agent's webhook")
    ap.add_argument("--signup", action="store_true",
                    help="create the account from here (no app token needed)")
    ap.add_argument("--name", help="display name for the account (signup only)")
    ap.add_argument("--identity", action="store_true",
                    help="re-derive the identity panel now (names or logo changed)")
    ap.add_argument("--language", help="ISO 639-1 code you and your user converse in")
    ap.add_argument("--qr", action="store_true", help="print the phone login QR")
    ap.add_argument("--payload", action="store_true", help="with --qr: print JSON, do not render")
    ap.add_argument("--test", action="store_true", help="plane-side connection test")
    ap.add_argument("--status", action="store_true", help="what the plane has registered")
    a = ap.parse_args()

    if a.identity:
        print(json.dumps(identity(force=True), indent=2))
        return

    c = cfg()
    api = a.api or c.get("api") or DEFAULT_API
    token = a.token or c.get("token")

    secret = c.get("secret")
    if not secret:
        raise SystemExit("no webhook secret yet — start voice_agent.py once to generate one")

    url = a.url or c.get("public_url")
    c["api"] = api
    if a.url:
        c["public_url"] = a.url
    if a.name:
        c["name"] = a.name
    if a.language:
        c["language"] = a.language

    if a.signup and not token:
        if not url:
            raise SystemExit("--signup needs --url (or run tunnel.py, which supplies one)")
        identity()
        r = signup(api, url, secret, c.get("name"), c.get("language"))
        token = r["token"]
        c.update({"token": token, "account": r["account"]})
        save(c)
        print("now run:  python3 pair.py --qr")
        return
    save(c)

    if not token:
        raise SystemExit("no account token: pass --token (from the app) or use --signup")

    if a.status:
        print(json.dumps(call(api, "/agent", token), indent=2))
        return
    if a.qr:
        show_qr(api, token,
                c.get("name") or identity().get("user_name") or c.get("account"),
                a.payload)
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

    if not url:
        raise SystemExit("need --url (or run tunnel.py if this machine has no public address)")
    # Before registering, not after: registering is what makes the plane ask what
    # this agent can do, and the answer includes whether it has an identity panel.
    identity()
    register(api, token, url, secret)
    print("now run:  python3 pair.py --test")


if __name__ == "__main__":
    main()
