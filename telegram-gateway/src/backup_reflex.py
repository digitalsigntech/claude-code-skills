"""Backup-status reflex — "how are the backups?" answered without an LLM turn.

the owner, 2026-08-07: "when I ask to show the status of our backups the backend should
have a very quick answer — a Python script that reports it as a table instantly
with no LLM round trips."

Same shape as `tasks_reflex.py`, and the same reasoning: the answer is a handful of
file checks, so a model turn adds latency and a chance to be wrong about facts that
are simply on disk.

WHAT IT REPORTS, AND WHAT IT DELIBERATELY DOES NOT. The state column is the point —
behind "show me the backups" is almost always "is anything wrong?", and a column of
timestamps makes the reader do that work. But the honest ceiling here is low:

    ran       the job finished and left its own success marker
    overdue   nothing since well past its schedule
    failed    the job's last line says it failed, or it exited non-zero
    never-run no evidence of it ever having run

`ran` is used where a weaker tool would say `ok`. A backup that RAN and a backup that
was VERIFIED — restored, opened, checked — are different facts, and only the second
one answers the question people think they are asking. Nothing here restores anything,
so nothing here says ok.
"""


import tgconf as C   # identity from config
import os
import re

import subprocess
import time

HOME = os.path.expanduser("~")

# Each job: how to find its last run, and how long is too long between runs.
JOBS = [
    {"name": "Workspace to Google Drive",
     "log": f"{C.WORKSPACE_ROOT}/email/kb/logs/backup.log",
     "done_re": re.compile(r"=== (\S+) done \(exit (\d+)\) ==="),
     "size_re": re.compile(r"uploaded \S+ \((\d+) KB\)"),
     "every_h": 24},
    {"name": "GitHub repositories",
     "marker": f"{HOME}/github_archives/last_run",
     "archive": f"{HOME}/github_archives",
     "every_h": 24},
]


def _ago(ts):
    """Durations in words (#101). Deliberately coarse — 'yesterday' is the
    answer to when, and a count of seconds is not."""
    if not ts:
        return "never"
    s = int(time.time() - ts)
    if s < 3600:
        return f"{max(s // 60, 1)}m ago"
    if s < 172800:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _tail(path, n=4000):
    """The tail of this log AND of the files logrotate moved it into.

    2026-08-11 (the owner: "the last message here shows 2 failures"): logrotate
    truncates these logs at 4:30am. Reading only the live file therefore found
    an EMPTY log a few hours after every rotation and reported two healthy
    backups as failed — the Drive archive was 1986 MB and the voice-VPS mirror
    1.1G at the time. A rotation is not a failure, and a status table that
    invents one is worse than no table: it spends the trust that makes a real
    alert mean something.

    Oldest first, so `findall(...)[-1]` still picks the most recent completion.
    """
    chunks = []
    for cand, opener in ((path + ".3.gz", gzip.open), (path + ".2.gz", gzip.open),
                         (path + ".1", open), (path, open)):
        try:
            with opener(cand, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - n))
                chunks.append(fh.read().decode("utf-8", "replace"))
        except OSError:
            continue
        except Exception:
            # A gz that is still being written reads as corrupt; skip it rather
            # than let one unreadable archive hide a good completion line.
            continue
    return "\n".join(chunks)


def _iso(s):
    try:
        return time.mktime(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return 0


def _human(kb):
    """Sizes in words, like the durations. Precision here is noise — the
    question is 'is it the size it should be', not 'how many bytes'."""
    if not kb:
        return "—"
    if kb >= 1024 * 1024:
        return f"{kb / 1024 / 1024:.1f}G"
    if kb >= 1024:
        return f"{kb / 1024:.0f}M"
    return f"{kb:.0f}K"


def _unit_kb(text):
    """'1.1G' / '900M' / '4096' -> KB."""
    m = re.match(r"([\d.]+)\s*([GMK]?)", str(text).strip(), re.I)
    if not m:
        return 0
    n = float(m.group(1))
    return {"G": n * 1024 * 1024, "M": n * 1024, "K": n}.get(
        m.group(2).upper(), n)


def _sizes(job):
    """(current_kb, previous_kb). Both from the job's OWN record of its runs —
    never from measuring the tree now, which would compare today's disk with
    today's disk and always look healthy."""
    if job.get("size_re"):
        hits = job["size_re"].findall(_tail(job["log"], 20000))
        vals = [_unit_kb(h) for h in hits if _unit_kb(h)]
        if vals:
            return vals[-1], (vals[-2] if len(vals) > 1 else 0)
    if job.get("archive"):
        # The WHOLE archive, not the newest run directory. Measured first, and
        # rejected: those directories are incremental — 3.2M, 243M, 479M, 481M,
        # 1.3G on five consecutive days. As a "size" it means nothing, and as a
        # shrink signal it would have cried wolf four days in five. A column
        # that alarms on normal behaviour gets ignored, and then it is worse
        # than no column.
        try:
            out = subprocess.run(["du", "-sk", job["archive"]],
                                 capture_output=True, text=True, timeout=10)
            return (float(out.stdout.split()[0]) if out.stdout.strip() else 0), 0
        except Exception:
            pass
    return 0, 0


def _one(job):
    last, state, detail = 0, "never-run", ""
    if job.get("marker"):
        try:
            last = os.path.getmtime(job["marker"])
            state = "ran"
        except OSError:
            pass
    else:
        text = _tail(job["log"])
        hits = job["done_re"].findall(text)
        if hits:
            stamp = hits[-1][0]
            last = _iso(stamp) or os.path.getmtime(job["log"])
            tail_bit = hits[-1][1]
            # An exit code that is not zero is a failure even when the line
            # says "done" — the word in the log is not the verdict.
            if tail_bit.isdigit() and int(tail_bit) != 0:
                state, detail = "failed", f"exit {tail_bit}"
            else:
                state = "ran"
                detail = "" if tail_bit.isdigit() else tail_bit
        elif os.path.exists(job["log"]):
            state = "failed"          # a log with no completion line at all
            detail = "no completion line"
            detail = "no completion line"
    # ARTIFACT BEATS LOG. A log says a job ran; the snapshot says the backup
    # exists, and only one of those is the thing being asked about. When the
    # log is silent (rotated away, deleted, never written) but last night's
    # snapshot is on disk, that is a backup, not a failure.
    if state != "ran" and job.get("snapshots"):
        try:
            snaps = sorted(d for d in os.listdir(job["snapshots"])
                           if d.startswith("daily-"))
        except OSError:
            snaps = []
        if snaps:
            newest = os.path.join(job["snapshots"], snaps[-1])
            try:
                mtime = os.path.getmtime(newest)
            except OSError:
                mtime = 0
            if mtime and time.time() - mtime < job["every_h"] * 3600 * 1.5:
                state, last = "ran", mtime
                detail = (detail + " · from snapshot, log silent").strip(" ·")
    if state == "ran" and last:
        # Overdue beats ran: a job that succeeded last week is not a healthy
        # backup, and the last line of its log will happily say it is.
        if time.time() - last > job["every_h"] * 3600 * 1.5:
            state = "overdue"
    if job.get("snapshots") and os.path.isdir(job["snapshots"]):
        n = len([d for d in os.listdir(job["snapshots"])
                 if d.startswith("daily-")])
        detail = (detail + f" · {n} snapshots").strip(" ·")
    cur, prev = _sizes(job)
    # #102 amended: a backup that quietly shrank to a fraction of its last size
    # is the row nobody notices until they need it. A job can exit zero while
    # writing four kilobytes, and that is a failure that looks like a success.
    if cur and prev and cur < prev * 0.5 and state == "ran":
        state = "SHRANK"
        detail = f"was {_human(prev)}"
    return {"name": job["name"], "state": state, "last": last,
            "detail": detail, "size_kb": cur, "prev_kb": prev}


# ------------------------------------------------------------- VPS backups ---
# 2026-09-05 (the owner, on a call: "rerun the Voice VPS backup now" — after this
# table had said "6d ago, overdue" four times). The row it was reporting was the
# box's OWN voice-vps pull-backup, retired 2026-08-30 when every VPS moved to
# the vps-backup app on his work PCs. The job was gone; its last log line was
# not, and this table kept reading it. A status table that reports a retired
# job as failing is worse than no row: he asked to rerun something that no
# longer exists, while the real copy (mars2, that morning, ok) went unmentioned.
#
# The VPS rows now come from the reporting machines' own reports — the same
# files the daily health check reads — one row per VPS, aged against TODAY
# from the dated snapshot name (the `age` column in the file was true when it
# was written and has been ageing with it ever since). Deployment data: the
# report directory is a checkout of the vps-backup repo; any other install
# points VPS_BACKUP_REPORTS at its own.
VPS_REPORTS = os.environ.get(
    "VPS_BACKUP_REPORTS", f"{C.WORKSPACE_ROOT}/operations/vps-backup/app/reports")
VPS_EVERY_H = 24


def _vps_rows():
    rows = []
    try:
        names = sorted(n for n in os.listdir(VPS_REPORTS) if n.endswith(".md"))
    except OSError:
        return rows
    for name in names:
        machine = name[:-3]
        try:
            with open(os.path.join(VPS_REPORTS, name), encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        # When that machine last RAN — the "last run" cell. The snapshot date
        # decides freshness below; this is only when the report was written.
        m = re.search(r"\*\*When:\*\*\s*(\S+)", text)
        ran_at = _iso(m.group(1)) if m else 0
        # The run's mirror sizes, by host ("## This run" table).
        sizes, table = {}, None
        for line in text.splitlines():
            if line.startswith("## "):
                table = ("run" if "this run" in line.lower()
                         else "hosts" if "every host" in line.lower() else None)
                continue
            if not table or not line.startswith("|"):
                continue
            c = [x.strip() for x in line.strip("|").split("|")]
            if len(c) < 5 or c[0] in ("host", "") or set(c[1]) <= set("-: "):
                continue
            if table == "run":
                sizes[c[0]] = _unit_kb(c[2])
                continue
            # "Every host on this machine": host | held | oldest | newest | age
            held = re.match(r"(\d+)\s*/\s*(\d+)", c[1])
            newest = c[3]
            try:
                snap_day = time.mktime(time.strptime(newest, "%Y-%m-%d"))
            except ValueError:
                snap_day = 0
            last = ran_at or snap_day
            state = "ran" if last else "never-run"
            if held and int(held.group(1)) == 0:
                state = "never-run"
            # A daily job is on time while today's or yesterday's dated
            # snapshot exists; the report's own age column is not consulted.
            elif not snap_day or time.time() - snap_day > (
                    VPS_EVERY_H * 3600 * 1.5 + 86400):
                state = "overdue"
            detail = (f"{held.group(1)}/{held.group(2)} snapshots" if held
                      else "")
            rows.append({"name": f"{c[0]} (on {machine})", "state": state,
                         "last": last, "detail": detail,
                         "size_kb": sizes.get(c[0], 0), "prev_kb": 0})
    return rows


def _cell(text, width=None):
    """One table cell: no pipes, no newlines, and NOTHING CUT.

    The pipe rule stays because it is about correctness, not width: a pipe ends
    the column early and shifts every later value under the wrong heading — a
    table that is confidently wrong rather than visibly broken.

    Truncation is gone. It moved through 28, then 48, then 64, and every one of
    those was this side guessing at a width it cannot see. the owner asked for a
    tap to open a cell's full content and the first thing it showed was that
    there was nothing to open — the text had already been cut here, so the
    popup displayed the same ellipsis. The app draws three lines and holds the
    rest. `width` is accepted and ignored so old call sites keep working.
    """
    return " ".join(str(text or "").replace("|", "/").split())


def status():
    return [_one(j) for j in JOBS] + _vps_rows()


def render(rows=None):
    rows = rows or status()
    # Three columns, not four: the transcript scrolls a wide table sideways,
    # so a fourth turns a glance into a drag on the screen where a glance was
    # the whole point. State folds into the last-run cell and nothing is lost.
    # Bold header, same reason as the reminders table (the owner 2026-08-13).
    out = ["| **Backup** | **Last run** | **Size** |", "|---|---|---|"]
    for r in rows:
        when = (f"{_ago(r['last'])}, {r['state']}" if r["last"]
                else r["state"])
        size = _human(r.get("size_kb"))
        if r["state"] == "SHRANK" and r.get("prev_kb"):
            size = f"{size} (was {_human(r['prev_kb'])})"
        out.append(f"| {_cell(r['name'])} | {_cell(when)} | {size} |")
    table = "\n".join(out)
    bad = [r for r in rows
           if r["state"] in ("failed", "overdue", "never-run", "SHRANK")]
    head = (f"*Backups* — all {len(rows)} ran" if not bad
            else f"*Backups* — {len(bad)} need{'s' if len(bad) == 1 else ''} a look")
    # Said every time, because "ran" quietly becoming "fine" in the reader's
    # head is the whole risk of this table.
    note = ("_\"ran\" means the job completed and left its marker. Nothing here "
            "restores a backup, so nothing here can say it is good._")
    return f"{head}\n\n{table}\n\n{note}"


# Fires on "show us the status of our backups", "how are the backups", "did the
# backup run", "when was the last backup". Falls through on questions ABOUT
# backups — how to add one, why one failed last week, how to restore — because
# those want Claude, not a table.
import reflex_guard as guard

NOUN = re.compile(r"\bback[- ]?ups?\b", re.I)
# 2026-08-11 (the owner: "fix the bug that brings up that table upon typing in
# the keyword, without looking at my actual message"). He wrote "You said the
# backups are running well, but the last message here shows 2 failures" — a
# complaint ABOUT the table — and got the table again, because the old STATUS
# pattern accepted "are", "is", "how", "did", "last", "show" and "run". Those
# are English filler: any sentence containing the word backup contains one, so
# the noun alone was effectively the trigger.
#
# Now the sentence has to READ like a request. Real status words only, and they
# have to sit near the noun rather than anywhere in the paragraph.
STATUS = re.compile(r"\b(status|health|healthy|ok|okay|working|fine|"
                    r"up[- ]?to[- ]?date|latest|overdue|stale|fail\w*|"
                    r"succeed\w*|success\w*)\b", re.I)
# Or an actual question/command opening the sentence.
ASK = re.compile(r"^\s*(please\s+)?(show|list|check|display|give|what|which|"
                 r"when|how|did|do|does|are|is|any|has|have)\b", re.I)
ABOUT = re.compile(r"\b(how do i|how to|add|create|set up|setup|configure|"
                   r"restore|restoring|delete|remove|why did|why was|explain|"
                   r"change|schedule a|disable|enable)\b", re.I)
# TALKING ABOUT THE ANSWER, NOT ASKING THE QUESTION. Anaphora — "you said",
# "the last message", "that table", "shows 2 failures" — means the reply
# already happened and is being discussed. Re-printing it answers nobody, and
# it is the specific way this reflex talked over him.
META = re.compile(r"\byou (said|told|reported|claimed|showed)\b|"
                  r"\b(last|previous|above|earlier|this|that) (message|table|"
                  r"reply|answer|report|one)\b|\bshow(s|ed)\b|"
                  r"\bwhat'?s wrong\b|\bwrong\b|\bbut\b", re.I)


def detect(text):
    t = (text or "").strip()
    if not t or len(t) > 120 or "\n" in t or t.startswith("/"):
        return False
    if ABOUT.search(t) or META.search(t) or guard.talking_about_it(t):
        return False
    if not NOUN.search(t):
        return False
    # The status word has to be ABOUT the backups: within a few words of the
    # noun, not merely somewhere in the same sentence.
    near = re.search(r"back[- ]?ups?\W+(\w+\W+){0,3}?(" + STATUS.pattern[2:-2]
                     + r")|(" + STATUS.pattern[2:-2] +
                     r")\W+(\w+\W+){0,3}?back[- ]?ups?", t, re.I)
    return bool(near) or bool(ASK.match(t))


def try_handle(chat_id, text, send):
    """Log summary if handled, None to fall through to Claude."""
    if not detect(text):
        return None
    try:
        rows = status()
    except Exception:
        return None          # invisible on failure; Claude can still answer
    send(chat_id, render(rows))
    bad = [r["name"] for r in rows if r["state"] != "ran"]
    return f"backup reflex: {len(rows)} jobs, {len(bad)} not ran"


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:])
    if q:
        print(f"detect({q!r}) = {detect(q)}")
    t0 = time.time()
    print(render())
    print(f"\n({(time.time() - t0) * 1000:.0f}ms)")
