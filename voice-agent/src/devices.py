#!/usr/bin/env python3
"""The agent's own list of devices allowed to read this account's sealed mail.

    ./devices.py list
    ./devices.py approve <device_id|label>
    ./devices.py revoke  <device_id|label>

WHY THIS FILE EXISTS AND NOT A COLUMN ON THE PLANE (#347, veto 1). A device in
the registry is a new pair of eyes on everything the user has ever said. That
makes registration a key-distribution decision, and the relay is the one party
the sealing exists to exclude — so the plane records requests and asks, and
THIS list is what actually gets wrapped for. If the two ever disagree, this one
wins, and the mismatch is worth saying out loud rather than resolving quietly.

Nothing is approved automatically. A registration arrives, the owner is told,
and the device stays pending until a human says otherwise — which is the whole
point: an attacker who can reach the relay still cannot add a reader.
"""
import json
import os
import sys
import time

# e2ee_devices.json, NOT devices.json: that name was already taken by an older
# account-device store with a different schema (created/name/sha256), and my
# first run wrote a row into it. Two systems sharing a file is how one of them
# eventually eats the other's state.
STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "e2ee_devices.json")


def _load():
    try:
        with open(STORE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(d):
    tmp = STORE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=1, sort_keys=True)
    os.replace(tmp, STORE)


def register(device_id, pubkey, label="", kind="", os_name="", location="",
             auto_approve=False):
    """Record a request. `accepted` stays False unless this install says
    otherwise.

    `auto_approve` is the DEMO policy and nothing else (2026-08-30). A shared
    sandbox anyone can enter has no owner to ask, so an approval gate there
    strands every visitor in front of an unreadable screen waiting for a
    message nobody will send. It is a config flag rather than something
    inferred from the account, because "which installs accept strangers" is
    exactly the question that must be answerable by reading one line.

    The vetoes are untouched: the relay still never approves anything. This is
    an AGENT deciding its own policy, which is the shape the contract asks for.
    """
    d = _load()
    row = d.get(device_id) or {}
    row.update({"pubkey": pubkey, "label": label, "type": kind,
                "os": os_name, "location": location,
                "first_seen": row.get("first_seen") or time.time(),
                "last_seen": time.time(),
                "accepted": bool(row.get("accepted")) or bool(auto_approve),
                "revoked": None})
    if auto_approve and not row.get("history_shared"):
        row["history_shared"] = time.time()
        row["approved_at"] = row.get("approved_at") or time.time()
        row["auto"] = True
    d[device_id] = row
    _save(d)
    return row


def approve(key, with_history=True):
    """Approve a device — and, by default, share what came before.

    2026-08-30, the product owner's decision, overriding this contract's
    second veto — and the argument is right. That veto existed so history could not be inherited as a SIDE
    EFFECT of pairing — but pairing is no longer the consent gate. Approval is,
    and approval is a deliberate act on a NAMED device after an announcement
    saying what asked and from where. Two prompts for one intention trains
    people to click through, which costs more security than the second prompt
    buys.

    What survives the change, because it was the part that mattered: the
    announcement says what access was granted, revoking still cuts past and
    future together, and share_history() remains as the repair path for
    devices approved before this.
    """
    d = _load()
    hit = _find(d, key)
    if not hit:
        return None
    d[hit]["accepted"] = True
    d[hit]["revoked"] = None
    d[hit]["approved_at"] = time.time()
    if with_history:
        d[hit]["history_shared"] = d[hit].get("history_shared") or time.time()
    _save(d)
    return hit


def revoke(key):
    d = _load()
    hit = _find(d, key)
    if not hit:
        return None
    d[hit]["accepted"] = False
    d[hit]["revoked"] = time.time()
    _save(d)
    return hit


def _find(d, key):
    """By id, or by label — the owner says "the iPad", not a hex string."""
    if key in d:
        return key
    key = (key or "").strip().lower()
    for dev, row in d.items():
        if key and key in (row.get("label") or "").lower():
            return dev
    return None


def share_history(key):
    """Mark that the owner has agreed this device may read what came before.

    Veto 2 from the contract, made concrete: approving a device lets it read
    what happens NEXT, and this — a separate, announced act — lets it read what
    came before. History is sealed at serve time, so this is a flag rather than
    a rewrite: with it set the agent seals past messages to this device on
    request, without it the device sees the placeholder wall it started with.
    """
    d = _load()
    hit = _find(d, key)
    if not hit:
        return None
    d[hit]["history_shared"] = time.time()
    _save(d)
    return hit


def history_shared(device_id):
    row = _load().get(device_id) or {}
    return bool(row.get("history_shared")) and not row.get("revoked")


def accepted_ids():
    """The only devices anything may be wrapped for."""
    return [k for k, v in _load().items()
            if v.get("accepted") and not v.get("revoked")]


def rows():
    return _load()


def main():
    args = sys.argv[1:]
    if not args or args[0] == "list":
        d = _load()
        if not d:
            print("no devices have asked yet")
            return 0
        for dev, r in sorted(d.items(), key=lambda kv: kv[1].get("first_seen", 0)):
            state = ("revoked" if r.get("revoked") else
                     "LINKED" if r.get("accepted") else "pending")
            print(f"{dev}  {state:<8} {r.get('label') or '?':<22} "
                  f"{r.get('type') or '':<8} {r.get('location') or ''}")
        return 0
    if args[0] in ("approve", "revoke") and len(args) > 1:
        rest = [a for a in args[1:] if a != "--no-history"]
        if args[0] == "approve":
            hit = approve(" ".join(rest),
                          with_history="--no-history" not in args)
            if hit:
                row = _load()[hit]
                print(f"approved {hit} ({row.get('label') or '?'}) — it can "
                      f"read new messages"
                      + (" AND everything from before it was linked"
                         if row.get("history_shared") else
                         " but NOT earlier history"))
        else:
            hit = revoke(" ".join(rest))
            if hit:
                print(f"revoked {hit} — it reads nothing from the next "
                      f"message on, history included")
        if not hit:
            print("no device matched that")
        return 0 if hit else 1
    sys.exit(__doc__.strip())


if __name__ == "__main__":
    sys.exit(main())
