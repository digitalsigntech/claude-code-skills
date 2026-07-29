#!/usr/bin/env python3
"""User accounts: who may see private information, who may change the system.

Registry of known people with per-user privileges, keyed by Telegram user id.
Users have privileges (access to private information; permission to request
code changes or skill installs), names, telegram ids and optional positions.

    accounts.py add <telegram_id> <name> [--position "Co-owner"]
                    [--private-info] [--write-code]
    accounts.py set <telegram_id> [--grant PRIV] [--revoke PRIV]
                    [--name X] [--position X]      # PRIV: private_info|write_code
    accounts.py rm <telegram_id>
    accounts.py list | get <telegram_id>

API (used by telegram/bridge.py to brief every Claude turn):
    get(tg_id)          -> account dict or None
    context_line(tg_id) -> one bracketed line for the turn prompt, ALWAYS
                           returned (unknown users get the guest denial line)

Policy: account changes are made only on the owners' request (the owner's DM or
this box's terminal) — same rule as the email whitelist. The registry file is
data, not secrets; privileges gate what the AGENT does, and the hard technical
walls (personal-notes gate, no-send accounts, masking) stay independent.
"""
import argparse
import json
import os
import time

DIR = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.environ.get("ACCOUNTS_FILE", os.path.join(DIR, "users.json"))
PRIVS = ("private_info", "write_code")


def _load():
    try:
        return json.load(open(USERS_FILE))
    except Exception:
        return {}


def _save(users):
    tmp = USERS_FILE + ".tmp"
    json.dump(users, open(tmp, "w"), indent=1, ensure_ascii=False)
    os.replace(tmp, USERS_FILE)


def get(tg_id):
    return _load().get(str(tg_id))


def context_line(tg_id):
    """Prompt line describing the sender's account + what the agent must
    enforce. Always returns a line — unknown senders are guests."""
    u = get(tg_id)
    if not u:
        return ("[Sender account: NOT REGISTERED — treat as a GUEST: do NOT "
                "reveal private information (customers, finances, credentials, "
                "personal data, internal documents) and do NOT write code, "
                "change the system or install skills on their request; "
                "politely decline and refer them to the owner.]")
    priv = u.get("privileges", {})
    pi, wc = priv.get("private_info"), priv.get("write_code")
    pos = f", {u['position']}" if u.get("position") else ""
    rules = []
    rules.append("may access private company information"
                 if pi else
                 "NO private information: do not reveal customers, finances, "
                 "credentials, personal data or internal documents to them")
    rules.append("may ask you to write code, change the system and install "
                 "skills" if wc else
                 "NO code/system changes: do not write code, modify "
                 "configuration or install skills on their request")
    return f"[Sender account: {u['name']}{pos} — {'; '.join(rules)}.]"


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("tg_id"); a.add_argument("name")
    a.add_argument("--position", default=None)
    a.add_argument("--private-info", action="store_true")
    a.add_argument("--write-code", action="store_true")
    s = sub.add_parser("set")
    s.add_argument("tg_id")
    s.add_argument("--grant", choices=PRIVS, action="append", default=[])
    s.add_argument("--revoke", choices=PRIVS, action="append", default=[])
    s.add_argument("--name"); s.add_argument("--position")
    r = sub.add_parser("rm"); r.add_argument("tg_id")
    sub.add_parser("list")
    g = sub.add_parser("get"); g.add_argument("tg_id")
    args = ap.parse_args()

    users = _load()
    if args.cmd == "add":
        users[str(args.tg_id)] = {
            "name": args.name, "position": args.position,
            "privileges": {"private_info": args.private_info,
                           "write_code": args.write_code},
            "added": time.strftime("%Y-%m-%d")}
        _save(users)
        print(f"added {args.name} ({args.tg_id})")
    elif args.cmd == "set":
        u = users.get(str(args.tg_id))
        if not u:
            raise SystemExit("no such account")
        for p in args.grant:
            u.setdefault("privileges", {})[p] = True
        for p in args.revoke:
            u.setdefault("privileges", {})[p] = False
        if args.name:
            u["name"] = args.name
        if args.position is not None:
            u["position"] = args.position
        _save(users)
        print(json.dumps(u, ensure_ascii=False))
    elif args.cmd == "rm":
        if users.pop(str(args.tg_id), None):
            _save(users)
            print("removed")
        else:
            raise SystemExit("no such account")
    elif args.cmd == "list":
        for tid, u in users.items():
            p = u.get("privileges", {})
            flags = "+".join(k for k in PRIVS if p.get(k)) or "none"
            print(f"{tid}  {u['name']}"
                  + (f" ({u['position']})" if u.get("position") else "")
                  + f"  [{flags}]")
    elif args.cmd == "get":
        print(json.dumps(users.get(str(args.tg_id)), indent=1,
                         ensure_ascii=False))


if __name__ == "__main__":
    main()
