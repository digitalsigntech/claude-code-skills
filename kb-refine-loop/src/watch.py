#!/usr/bin/env python3
"""KB self-refinement watcher — cron half of the kb-refine loop.

Scans the local email archive (crm/contacts.db:emails) for NEW sent replies from
the owner to external customers, and for each one launches a headless Claude run of
refine_prompt.md: answer the customer's question from the KB alone, diff against
the owner's actual reply, patch the KB where it was wrong or silent, repeat until the
KB-only answer converges (the owner, 2026-07-12: run this on every reply he sends to a
customer question).

State: state.json {"refined": {msg_id: status}, ...}. First run seeds every
pre-existing outbound id as "seeded" so history is never bulk-processed.

Usage: watch.py [--dry] [--limit N] [--force THREAD_ID:REPLY_ID]
"""
import argparse, json, os, re, sqlite3, subprocess, sys, datetime

DIR = os.path.dirname(os.path.abspath(__file__))
DB = "/home/mercury/DST/crm/contacts.db"
STATE = os.path.join(DIR, "state.json")
PROMPT = os.path.join(DIR, "refine_prompt.md")
CLAUDE = "/home/mercury/.local/bin/claude"
MODEL = "claude-opus-4-8"
TIMEOUT = 1800                      # 30 min per thread, matches other headless jobs
PER_RUN = 2                        # max refine runs per cron tick
MAX_ATTEMPTS = 2                   # give up on a reply after this many failed runs

# Owner identity — set via env: OWNER_EMAIL is the business address whose sent
# replies are learned from; OWNER_DOMAIN is your company domain (internal mail is
# never customer Q&A); OWNER_SKIP_EXTRA adds personal addresses etc. (|-separated).
# KB_WRITERS: comma-separated addr=styleperson pairs — every listed writer's sent
# replies feed the refine loop, each learning into learned-<styleperson>.md.
# Falls back to the single KB_OWNER_EMAIL for backward compatibility.
_writers_env = os.environ.get("KB_WRITERS", "")
OWNER_EMAIL = os.environ.get("KB_OWNER_EMAIL", "owner@example.com")
WRITERS = (dict(p.split("=", 1) for p in _writers_env.split(",") if "=" in p)
           or {OWNER_EMAIL: os.environ.get("KB_OWNER_STYLE", "owner")})
OWNER_DOMAIN = re.escape(os.environ.get("KB_OWNER_DOMAIN", OWNER_EMAIL.split("@")[-1]))
_extra = os.environ.get("KB_OWNER_SKIP_EXTRA", "")
# Replies to these are never customer Q&A (internal, personal, apprentice).
SKIP_TO = re.compile(
    "@" + OWNER_DOMAIN + "|no-?reply|mailer-daemon" + (("|" + _extra) if _extra else ""), re.I)


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def load_state():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {"refined": {}}


def save_state(st):
    tmp = STATE + ".tmp"
    json.dump(st, open(tmp, "w"), indent=1)
    os.replace(tmp, STATE)


def writer_of(row):
    frm = (row["from_addr"] or "").lower()
    return next((p for a, p in WRITERS.items() if a.lower() in frm),
                next(iter(WRITERS.values())))


def outbound_rows(c):
    """All sent writer->external replies (any WRITERS member), oldest first."""
    out = []
    for addr in WRITERS:
        rows = c.execute(
            """SELECT id, thread_id, internal_date, to_addr, subject, from_addr FROM emails
               WHERE from_addr LIKE ? AND labels LIKE '%SENT%'
               ORDER BY internal_date""", (f"%{addr}%",)).fetchall()
        out.extend(r for r in rows if r["to_addr"] and not SKIP_TO.search(r["to_addr"]))
    out.sort(key=lambda r: r["internal_date"])
    return out


def has_inbound_question(c, thread_id, before_ts):
    """True if the thread has an earlier substantive inbound (non-DST) message.

    Deliberately loose: customers often ask without a question mark ("So best
    lowest cost option is ideal" — Ashton Potter 2026-07-13, which the old
    '?'-only gate missed entirely). refine_prompt.md step 2 does the real
    is-there-a-question triage and skips cheaply."""
    rows = c.execute(
        """SELECT from_addr, body_new FROM emails
           WHERE thread_id=? AND internal_date < ?""", (thread_id, before_ts)).fetchall()
    for r in rows:
        frm = (r["from_addr"] or "").lower()
        if re.search("@" + OWNER_DOMAIN, frm):
            continue
        if len((r["body_new"] or "").strip()) >= 20:
            return True
    return False


def refine(thread_id, reply_id, dry=False, writer=None):
    writer = writer or next(iter(WRITERS.values()))
    prompt = (open(PROMPT).read()
              + f"\nTHREAD_ID: {thread_id}\nREPLY_ID: {reply_id}\nWRITER: {writer}\n"
              f"NOTE: the sent reply under test is by WRITER above. Wherever the steps "
              f"say the owner's name, read the writer named on the WRITER line; use "
              f"the style profiles {writer}.md + learned-{writer}.md for style drafting "
              f"and merge style deltas into learned-{writer}.md.\n")
    if dry:
        print(f"{now()} DRY would refine thread={thread_id} reply={reply_id}")
        return True
    print(f"{now()} refine start thread={thread_id} reply={reply_id}")
    r = subprocess.run([CLAUDE, "-p", prompt, "--model", MODEL,
                        "--dangerously-skip-permissions"],
                       capture_output=True, text=True, timeout=TIMEOUT + 60)
    out = (r.stdout or "") + (r.stderr or "")
    m = re.search(r"REFINE-RESULT:.*", out)
    print(f"{now()} refine done rc={r.returncode} | {m.group(0) if m else out[-300:]}")
    return r.returncode == 0 and m is not None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--limit", type=int, default=PER_RUN)
    ap.add_argument("--force", help="THREAD_ID:REPLY_ID — refine this one now")
    a = ap.parse_args()

    if a.force:
        tid, mid = a.force.split(":")
        refine(tid, mid, dry=a.dry)
        return

    c = sqlite3.connect(DB, timeout=60)
    c.row_factory = sqlite3.Row
    st = load_state()
    # Watch-start fence: when a NEW writer's mailbox is added, their whole sent
    # history appears as unseen ids at once — never bulk-refine it. Anything
    # older than the fence is auto-seeded; only replies after it get refine runs.
    ws = st.get("watch_start")
    if ws is None:
        ws = int(datetime.datetime.now().timestamp() * 1000)
        st["watch_start"] = ws
        save_state(st)
    rows = outbound_rows(c)

    if not st["refined"]:                       # first run: seed history, process nothing
        st["refined"] = {r["id"]: "seeded" for r in rows}
        save_state(st)
        print(f"{now()} seeded {len(rows)} pre-existing outbound ids; watching from now on.")
        return

    done = 0
    for r in rows:
        if done >= a.limit:
            break
        prev = st["refined"].get(r["id"])
        if prev and (prev in ("seeded", "ok", "skip") or prev.startswith("gaveup")):
            continue
        if prev is None and r["internal_date"] < ws:
            st["refined"][r["id"]] = "seeded"   # pre-fence history of a newly added writer
            continue
        attempts = int(prev.split(":")[1]) if prev and prev.startswith("fail") else 0
        if not r["thread_id"] or not has_inbound_question(c, r["thread_id"], r["internal_date"]):
            st["refined"][r["id"]] = "skip"     # not a reply to a question
            continue
        try:
            ok = refine(r["thread_id"], r["id"], dry=a.dry, writer=writer_of(r))
        except subprocess.TimeoutExpired:
            ok = False
            print(f"{now()} refine TIMEOUT thread={r['thread_id']}")
        if a.dry:
            done += 1
            continue
        if ok:
            st["refined"][r["id"]] = "ok"
        else:
            attempts += 1
            st["refined"][r["id"]] = (f"fail:{attempts}" if attempts < MAX_ATTEMPTS
                                      else f"gaveup:{attempts}")
        save_state(st)
        done += 1
    if done == 0:
        print(f"{now()} nothing new to refine.")


if __name__ == "__main__":
    main()
