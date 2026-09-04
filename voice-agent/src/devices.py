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


# THE KEY IS THE PAIR, NOT THE DEVICE (2026-09-04, second pass). Scoping the
# READS by account closed the approval leak but left the row shared: this store
# was keyed by device_id alone, so one phone signing into a second account
# MOVED its row and silently unlinked it from the first. Verified rather than
# reasoned — a simulated demo sign-in took his device count from three to two.
#
# Rows are keyed by "account\x00device_id" now, with a migration that stamps
# any legacy flat row with the account it already carries. `rows(account)`
# still answers {device_id: row}, so nothing above this line changed shape.
SEP = "\x00"


def _k(account, device_id):
    return f"{account or ''}{SEP}{device_id}"


def _split(key):
    account, _, dev = key.partition(SEP)
    return (account or None, dev) if SEP in key else (None, key)


def _migrate(d):
    """Flat rows -> pair-keyed, once, keeping every field."""
    if all(SEP in k for k in d):
        return d, False
    out, moved = {}, False
    for k, v in d.items():
        if SEP in k:
            out[k] = v
            continue
        moved = True
        v = dict(v)
        v["device_id"] = k
        out[_k(v.get("account"), k)] = v
    return out, moved


def _load():
    try:
        with open(STORE) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    d, moved = _migrate(d)
    if moved:
        _save(d)
    return d


def _save(d):
    tmp = STORE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=1, sort_keys=True)
    os.replace(tmp, STORE)


def register(device_id, pubkey, label="", kind="", os_name="", location="",
             auto_approve=False, account=None):
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
    key = _k(account, device_id)
    row = d.get(key) or {}
    # AN APPROVAL BELONGS TO ONE ACCOUNT (2026-09-04). This store had no account
    # at all, so an agent serving two accounts treated a device approved for the
    # first as approved for the second — and the inbound gate, which trusts this
    # list, would open its sealed asks on either. On the demo machine that was
    # not hypothetical: signing one phone into the demo account inherited three
    # approvals it had never been given.
    #
    # A row whose account differs is a DIFFERENT device as far as approval goes,
    # so it starts again exactly as a re-key does.

    # A NEW KEY ON A KNOWN ID IS A NEW DEVICE (2026-09-04). Carrying `accepted` forward
    # across a CHANGED pubkey would make re-registration a way to launder a
    # substituted key past the approval gate: present the id of a device the
    # owner once approved, hand over a different key, inherit the tick. The id
    # is a label the client chooses; the key is the thing that reads mail.
    #
    # So the approval belongs to the KEY. A device that re-generates its own
    # key — a restore from a backup without the Keychain, which the E2EE doc
    # names as the case that must be recoverable without support — asks again
    # and is announced again, which costs one tap and closes the hole.
    rekeyed = bool(row.get("pubkey")) and row.get("pubkey") != pubkey
    row.update({"pubkey": pubkey, "label": label, "type": kind,
                **({"account": account} if account else {}),
                "os": os_name, "location": location,
                "first_seen": row.get("first_seen") or time.time(),
                "last_seen": time.time(),
                "accepted": (bool(auto_approve) if rekeyed
                             else bool(row.get("accepted")) or bool(auto_approve)),
                **({"rekeyed_at": time.time()} if rekeyed else {}),
                "revoked": None})
    if rekeyed and not auto_approve:
        # History was shared with the OLD key. The new one starts again.
        row.pop("history_shared", None)
        row.pop("approved_at", None)
    if auto_approve and not row.get("history_shared"):
        row["history_shared"] = time.time()
        row["approved_at"] = row.get("approved_at") or time.time()
        row["auto"] = True
    row["device_id"] = device_id
    d[key] = row
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
    """The STORE key for a device named by id or by label.

    The owner says "the iPad", not a hex string — and since rows are keyed by
    the account/device pair now, a bare device id has to be matched against the
    id INSIDE the row rather than against the key.
    """
    if key in d:
        return key
    raw = (key or "").strip()
    for k, row in d.items():
        if raw and (row.get("device_id") == raw or _split(k)[1] == raw):
            return k
    low = raw.lower()
    for k, row in d.items():
        if low and low in (row.get("label") or "").lower():
            return k
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


def history_shared(device_id, account=None):
    d = _load()
    row = d.get(_find(d, device_id) or "") or {}
    if account and not _mine(row, account):
        return False
    return bool(row.get("history_shared")) and not row.get("revoked")


def _mine(row, account):
    """Does this row belong to the account asking?

    A row with NO account is grandfathered — it predates the field, and refusing
    it would silently unlink devices that work today. New rows always carry one,
    so the ungated set only ever shrinks.
    """
    return (not account) or (not row.get("account")) or row["account"] == account


def accepted_ids(account=None):
    """The only devices anything may be wrapped for, for THIS account."""
    return [v.get("device_id") or _split(k)[1] for k, v in _load().items()
            if v.get("accepted") and not v.get("revoked") and _mine(v, account)]


def rows(account=None):
    """{device_id: row} — the shape every caller above this line expects."""
    return {(v.get("device_id") or _split(k)[1]): v
            for k, v in _load().items() if _mine(v, account)}


def main():
    args = sys.argv[1:]
    if not args or args[0] == "list":
        d = _load()
        if not d:
            print("no devices have asked yet")
            return 0
        for k, r in sorted(d.items(), key=lambda kv: kv[1].get("first_seen", 0)):
            state = ("revoked" if r.get("revoked") else
                     "LINKED" if r.get("accepted") else "pending")
            dev = r.get("device_id") or _split(k)[1]
            print(f"{dev}  {state:<8} {r.get('label') or '?':<22} "
                  f"{r.get('type') or '':<8} {(r.get('account') or '-'):<22}"
                  f"{r.get('location') or ''}")
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
