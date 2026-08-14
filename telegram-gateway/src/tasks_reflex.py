"""Tasks reflex — "show me the currently running tasks" answered without an LLM turn.

The owner, 2026-08-07: "when I ask you to show me the currently running tasks, you need
to present me them in the table, and it should take no time without LLM roundtrips.
It should be hardcoded in Python."

He is right that this never needed a model. The answer is a registry lookup: the
voice server already keeps every running task (cron jobs, dev-side scripts, agent
turns) and already renders the three-column table — see `tasks_table()` in
voice/realtime/server.py. This reflex just asks it over the loopback hook and
prints what comes back. One local HTTP call, single-digit milliseconds.

The table is built THERE, not here, on purpose: two copies of the same wording
drift the first time a catalogue entry changes.
"""
import json, os, re, sys, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tgconf as C

REALTIME = os.path.join(C.WORKSPACE_ROOT, "voice", "realtime")
SECRET_FILE = os.path.join(REALTIME, ".secret")
HOOK_SECRET_FILE = os.path.join(REALTIME, ".hook_secret")
PORT = 8478
TIMEOUT = 4

# "what's running", "show me the currently running tasks", "any tasks running?",
# "what are you working on right now", "current jobs". Deliberately requires both
# a task-ish noun and a running-ish word, so "I ran the backup task yesterday"
# does not trigger it.
import reflex_guard as guard

NOUN = re.compile(r"\b(task|tasks|job|jobs|process|processes|agent|agents)\b", re.I)
RUNNING = re.compile(r"\b(running|run|active|in progress|ongoing|going on|"
                     r"current|currently|now|right now|working on|status|busy)\b", re.I)
# Questions about the mechanism itself belong to Claude, not to a table dump.
ABOUT = re.compile(r"\b(how|why|explain|add|remove|change|fix|catalogue|catalog|"
                   r"describe|rename)\b", re.I)


# Phrases that mean the question on their own, with no task-noun in them:
# "what's running", "anything going on right now", "are you busy".
BARE = re.compile(r"^\s*(what('s| is| are you)?\s+(currently\s+)?"
                  r"(running|going on|happening|doing)|"
                  r"anything (running|going on|happening)|"
                  r"are you busy|what are you (running|doing|working on))\b", re.I)


def detect(text):
    t = (text or "").strip()
    if not t or len(t) > 120 or "\n" in t or t.startswith("/"):
        return False
    if ABOUT.search(t):
        return False
    if guard.talking_about_it(t):
        return False
    if BARE.search(t):
        return True
    return bool(NOUN.search(t)) and bool(RUNNING.search(t))


def _hook(payload):
    secret = open(SECRET_FILE).read().strip()
    bearer = open(HOOK_SECRET_FILE).read().strip()
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/{secret}/hook",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {bearer}"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def render(d):
    """Table as-is from the server, plus one honest line about coverage.

    An empty table means nothing is running — say so in words. A bare heading
    with no rows reads like a failure."""
    table = (d.get("table") or "").strip()
    tasks = d.get("tasks") or []
    if not table or not tasks:
        return "Nothing running right now — no scheduled jobs, no agent work."
    head = f"*Running now* ({len(tasks)})"
    note = d.get("coverage_note") or ""
    return f"{head}\n\n{table}" + (f"\n\n_{note}_" if note else "")


def try_handle(chat_id, text, send):
    """Returns a short log summary if handled, None to fall through to Claude."""
    if not detect(text):
        return None
    try:
        d = _hook({"type": "progress"})
    except Exception as e:
        # Fall through rather than reporting a failure: Claude can still shell
        # out and answer. A reflex that cannot reach the server should be
        # invisible, not an error message in the chat.
        return None
    send(chat_id, render(d))
    return f"tasks reflex: {len(d.get('tasks') or [])} running"


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "show me the currently running tasks"
    print(f"detect({q!r}) = {detect(q)}")
    if detect(q):
        print(render(_hook({"type": "progress"})))
