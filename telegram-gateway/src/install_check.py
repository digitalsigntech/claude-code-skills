#!/usr/bin/env python3
"""Find another deployment's identity inside this one.

    python3 install_check.py [path]        default: the workspace root

Every skill in this repo is written to be installed. Every one of them can also be
COPIED — somebody rsyncs a working machine's directory onto a new box because that
is faster than an install, and it works, right up until the copy still refers to the
original: its home directory, its owner, its mailbox, its chat.

That is not a hypothetical. A second install ran for two days with the first one's
workspace path in its reminders module (silently reading a database that did not
exist), the first one's staff addresses as the answer to "who gets mailed when a
reminder fires", and the first one's voice-app account ids as the push targets. The
published files had none of it. The copy did.

The repository is guarded — a pre-push hook refuses to publish another deployment's
identifiers. Nothing guarded the INSTALL, so this is that hook, pointed the other
way: at a machine rather than at a commit.

What it flags, all from what this install says about itself:

  * absolute paths that do not exist here and look like somebody's workspace
  * email addresses outside this install's configured mailboxes
  * Telegram ids that are neither the owner's nor in the allowlist

Code and configuration only. A company's own mail archive is full of other people's
addresses and that is what an archive IS; reporting them would bury the one line
that matters.

Exit 0 when clean, 1 when something belongs to someone else.
"""
import json, os, pathlib, re, sys

HERE = pathlib.Path(__file__).resolve().parent
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "logs",
             "state", "attic", "backups"}
# CODE AND CONFIG ONLY. The first version read everything and reported a hundred
# customer and vendor addresses out of the company's own correspondence — real
# data, correctly present, and nothing to do with whose machine this is. A check
# that buries one real finding under a hundred false ones has not found anything.
LOOK_AT = (".py", ".sh", ".bash", ".service", ".timer", ".env", ".ini", ".conf",
           ".cfg", ".toml", ".yaml", ".yml", ".json")
TEXT_MAX = 400_000

PATHISH = re.compile(r"[\"'`]((?:/home/[\w.-]+|/root|/srv|/opt)/[\w./-]{2,})[\"'`]")
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
TGID = re.compile(r"(?<![\w.])(-?\d{9,13})(?![\w.])")
# A long number is only a chat id if the line is talking about chats. Without
# this the check reported a phone number out of a website builder three times,
# which is how a tool trains you to skim past its output.
TGID_CONTEXT = re.compile(r"chat|telegram|owner|allow|group|tg_", re.I)
# Documented placeholders are the opposite of a leak — they exist so nobody has
# to paste a real one.
PLACEHOLDER = re.compile(r"^-?100?12345678|^-?1234567890?$|^0+$")


def config():
    sys.path.insert(0, str(HERE))
    import tgconf as C
    return C


def own_mailboxes(C):
    boxes = {str(getattr(C, n, "") or "").lower() for n in
             ("OWNER_MAILBOX", "SECOND_OWNER_MAILBOX", "BOT_MAILBOX",
              "FRIEND_MAILBOX", "OWNER_EMAIL", "OWNER_PERSONAL_EMAIL")}
    return {b for b in boxes if "@" in b}


def own_ids(C, root):
    ids = {int(getattr(C, n, 0) or 0) for n in ("OWNER_ID", "SECOND_OWNER_ID")}
    try:
        allow = json.loads((root / "telegram" / "allowlist.json").read_text())
        ids |= {int(i) for i in allow if isinstance(i, int)}
    except (OSError, ValueError, TypeError):
        pass
    return {i for i in ids if i}


def scan(root, C):
    mailboxes, ids = own_mailboxes(C), own_ids(C, root)
    domains = {m.split("@", 1)[1] for m in mailboxes}
    findings = []
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in SKIP_DIRS and not d.startswith(".")
                 and ".bak" not in d]
        for name in fn:
            p = pathlib.Path(dp) / name
            if p.suffix.lower() not in LOOK_AT or ".bak" in name:
                continue
            try:
                if p.stat().st_size > TEXT_MAX:
                    continue
                text = p.read_text(errors="strict")
            except (OSError, UnicodeDecodeError):
                continue
            rel = p.relative_to(root)
            for n, line in enumerate(text.splitlines(), 1):
                for m in PATHISH.finditer(line):
                    path = m.group(1)
                    # A path that exists here is this machine's business. One that
                    # does not, and looks like a workspace, came from elsewhere.
                    if not os.path.exists(path) and not str(root) in path:
                        findings.append((rel, n, "path", path))
                for addr in EMAIL.findall(line):
                    a = addr.lower()
                    if a in mailboxes or a.split("@", 1)[1] in domains:
                        continue
                    if a.endswith((".example", ".example.com", ".invalid", ".local")):
                        continue
                    findings.append((rel, n, "email", addr))
                if TGID_CONTEXT.search(line):
                    for raw in TGID.findall(line):
                        if (int(raw) not in ids and abs(int(raw)) > 100_000_000
                                and not PLACEHOLDER.match(raw)):
                            findings.append((rel, n, "telegram id", raw))
    return findings


def main():
    C = config()
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                        else getattr(C, "WORKSPACE_ROOT", HERE.parent)).resolve()
    findings = scan(root, C)
    if not findings:
        print(f"clean: nothing in {root} names another deployment")
        return 0
    print(f"{len(findings)} reference(s) to something that is not this install:\n")
    for rel, line, kind, value in findings[:60]:
        print(f"  {rel}:{line}  {kind}: {value}")
    if len(findings) > 60:
        print(f"  … and {len(findings) - 60} more")
    print("\nEach one is either config this install should own, or a file copied "
          "from another machine rather than installed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
