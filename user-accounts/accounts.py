#!/usr/bin/env python3
"""User accounts: who may see private information, who may change the system.

Registry of known people with per-user privileges, keyed by Telegram user id.
Users have privileges (private information access; code/skill-change
requests; account administration), names, telegram ids, emails, positions
and a preferred language.

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

Policy: account changes on request of the owners (the owner's side) or
of a registered ADMIN (admin privilege; verified by sender id). Only owners
grant/revoke admin itself. The registry file is
data, not secrets; privileges gate what the AGENT does, and the hard technical
walls (personal-notes gate, no-send accounts, masking) stay independent.
"""
import argparse
import json
import os
import time

DIR = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.environ.get("ACCOUNTS_FILE", os.path.join(DIR, "users.json"))
PRIVS = ("private_info", "write_code", "admin")


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


def set_language(tg_id, lang):
    """Persist the user's preferred language (ISO 639-1). Used by the voice
    backend: spoken 'switch to Russian' commands and auto-detection."""
    users = _load()
    u = users.get(str(tg_id))
    if not u:
        return False
    u["language"] = lang
    _save(users)
    return True


def get_by_email(email):
    """Resolve an account by any of its email addresses (case-insensitive) —
    lets the email side (IDLE watcher etc.) apply the same privileges."""
    email = (email or "").strip().lower()
    for tid, u in _load().items():
        if email in [e.lower() for e in u.get("emails", [])]:
            return {**u, "telegram_id": tid}
    return None


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
    pi, wc, adm = (priv.get("private_info"), priv.get("write_code"),
                   priv.get("admin"))
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
    if adm:
        rules.append("ADMIN: may manage user accounts (add/remove users, "
                     "grant/revoke private_info and write_code) — but only "
                     "the owners may grant or revoke admin itself")
    else:
        rules.append("may NOT manage user accounts")
    return f"[Sender account: {u['name']}{pos} — {'; '.join(rules)}.]"


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("tg_id"); a.add_argument("name")
    a.add_argument("--position", default=None)
    a.add_argument("--email", action="append", default=[],
                   help="repeatable — a user may have several")
    a.add_argument("--private-info", action="store_true")
    a.add_argument("--write-code", action="store_true")
    a.add_argument("--admin", action="store_true")
    s = sub.add_parser("set")
    s.add_argument("tg_id")
    s.add_argument("--grant", choices=PRIVS, action="append", default=[])
    s.add_argument("--revoke", choices=PRIVS, action="append", default=[])
    s.add_argument("--name"); s.add_argument("--position")
    s.add_argument("--email", action="append", default=[],
                   help="add an email address")
    s.add_argument("--rm-email", action="append", default=[])
    s.add_argument("--language", help="preferred language, ISO 639-1")
    r = sub.add_parser("rm"); r.add_argument("tg_id")
    sub.add_parser("list")
    g = sub.add_parser("get"); g.add_argument("tg_id")
    args = ap.parse_args()

    users = _load()
    if args.cmd == "add":
        users[str(args.tg_id)] = {
            "name": args.name, "position": args.position,
            "emails": args.email,
            "privileges": {"private_info": args.private_info,
                           "write_code": args.write_code,
                           "admin": args.admin},
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
        if args.language:
            u["language"] = args.language
        if args.email or args.rm_email:
            ems = [e.lower() for e in u.get("emails", [])]
            ems += [e.lower() for e in args.email if e.lower() not in ems]
            u["emails"] = [e for e in ems
                           if e not in {x.lower() for x in args.rm_email}]
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
            em = ", ".join(u.get("emails", []))
            print(f"{tid}  {u['name']}"
                  + (f" ({u['position']})" if u.get("position") else "")
                  + f"  [{flags}]" + (f"  {em}" if em else ""))
    elif args.cmd == "get":
        print(json.dumps(users.get(str(args.tg_id)), indent=1,
                         ensure_ascii=False))


if __name__ == "__main__":
    main()
