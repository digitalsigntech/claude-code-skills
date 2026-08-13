#!/usr/bin/env python3
"""Check this agent against every message the voice plane can send it.

    python3 conformance.py            # against the running adapter

Why this file exists: the adapter was extracted from a working box by copying its
SHAPE — answer questions, report health — rather than its SURFACE. The box had
grown a dozen more message types over months of use, each one added the day
something in the app looked broken. The extraction kept none of them, and nothing
compared the two, so every gap was rediscovered from the outside, by a person
looking at a phone: a blank identity panel, a crossed-out connect button, an empty
chat. Each was a five-minute fix that cost a day to notice.

A protocol has a list. Checking yourself against it is one request per entry, and
it turns "why is this broken" into a line of output BEFORE anyone installs.

REQUIRED types break something visible if unimplemented. OPTIONAL ones are features
a particular machine may genuinely not have — no chat channel, no media library —
and the plane degrades cleanly when a capability is absent. Silence about an
optional gap is fine; silence about a required one is how today happened.
"""
import argparse, json, pathlib, sys, urllib.error, urllib.request

HERE = pathlib.Path(__file__).resolve().parent

# (type, required, what breaks in the app when it is missing)
PROTOCOL = [
    ("capabilities", True,  "the plane cannot tell what this agent supports"),
    ("health",       True,  "connection tests fall back to a model turn and time out"),
    ("ask",          True,  "spoken questions go nowhere — the whole point"),
    ("progress",     True,  "the connect button is drawn crossed out: agent looks offline"),
    ("history",      True,  "the app opens on a blank chat with nothing to restore"),
    ("branding",     True,  "no name, no company, no logo — the app shows its generic panel"),
    ("file",         False, "the logo cannot be fetched (only matters with a logo set)"),
    ("qr_spent",     False, "a scanned login QR waits for its expiry instead of going now"),
    ("reset",        False, "'clear context' cannot start a fresh thread"),
    ("log",          False, "spoken lines are not mirrored into your chat"),
    ("attachments",  False, "files you send in chat are not offered to the voice session"),
    ("photos",       False, "photos cannot be pushed to the phone"),
    ("media",        False, "no image search over a knowledge base"),
    ("groups",       False, "no chat picker (single-context agents do not need one)"),
    ("group",        False, "cannot switch which chat the agent is working in"),
    ("session_context", False, "the voice model gets identity only, not a fuller briefing"),
]


def probe(port, secret, kind):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/", method="POST",
        data=json.dumps({"v": 1, "type": kind, "account": "conformance",
                         "limit": 1}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {secret}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, OSError) as e:
        return None, str(e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int)
    a = ap.parse_args()
    try:
        cfg = json.loads((HERE / "config.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        raise SystemExit("no config.json beside this script")
    port = a.port or int(cfg.get("port", 8787))
    secret = cfg.get("secret")
    if not secret:
        raise SystemExit("no webhook secret — start voice_agent.py once")

    # `ask` is excluded from the sweep on purpose: probing it would spend a model
    # turn and a minute of wall clock to learn what `capabilities` already says.
    served, missing_required, missing_optional = [], [], []
    for kind, required, breaks in PROTOCOL:
        if kind == "ask":
            code = 200 if "ask" in (probe(port, secret, "capabilities")[1] or {}
                                    ).get("capabilities", []) else 400
        else:
            code, body = probe(port, secret, kind)
            # A handler that answers "no such token" or "nothing to do" HAS the
            # message type — a probe with no real arguments is expected to fail
            # that way. Only "unsupported type" means it is not implemented, so
            # the body decides, not the status code. Otherwise this reports the
            # error handling as the gap and sends someone to fix the wrong thing.
            if code != 200 and isinstance(body, dict) and \
                    "unsupported type" not in str(body.get("error", "")):
                code = 200
        if code is None:
            raise SystemExit(f"nothing answering on 127.0.0.1:{port} — is the adapter running?")
        if code == 200:
            served.append(kind)
        elif required:
            missing_required.append((kind, breaks))
        else:
            missing_optional.append((kind, breaks))

    print(f"serves {len(served)}/{len(PROTOCOL)}: {', '.join(served)}\n")
    for kind, breaks in missing_optional:
        print(f"  optional  {kind:<16} {breaks}")
    if missing_optional:
        print()
    for kind, breaks in missing_required:
        print(f"  MISSING   {kind:<16} {breaks}")
    if missing_required:
        print("\nEach of these is visible to your user and invisible to you.")
        return 1
    print("Every required message type is answered.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
