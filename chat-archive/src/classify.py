#!/usr/bin/env python3
"""Assign a project (and multi-labels) to archived chat messages — from the conversation
itself.

A single chat room carries several projects over time, so the room can't be the tag.
This walks the UNCLASSIFIED messages, groups them into conversation bursts (same chat,
small time gaps), and asks a cheap "worker" LLM which project each burst is about.
Bursts are naturally topic-coherent, so one label per burst is accurate. Bursts are
batched into a single call to stay cheap/fast.

The classifier runs on a cheap/metered "worker" model (default: an OpenRouter-hosted
model) to keep this automation off your interactive assistant's budget. The backend is
swappable — see README. If the worker is unavailable/empty it falls back to a local
CLI-driven "chat" LLM and fires a throttled alert.

Cron-friendly: bounded work per run, safe to re-run, only touches project IS NULL rows.

  classify.py                 # classify a batch of unclassified bursts
  classify.py --limit 400     # cap bursts this run (default 200)
  classify.py --batch 20      # bursts per call (default 15)
  classify.py --dry-run       # print decisions, write nothing
"""
import os, sys, json, time, sqlite3, threading, subprocess, argparse
import urllib.request, urllib.error, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import chatdb

# classify keeps its OWN sqlite connection, separate from chatdb's gateway connection.
# When run in-process as the real-time worker, sharing chatdb._conn across threads would
# risk "recursive use of cursor" races with the gateway hot path; a private connection
# sidesteps that entirely (WAL + busy_timeout serialize writes across the two connections).
# In the standalone/cron process this is simply the one connection.
_CONN = None
_CONN_LOCK = threading.Lock()


def _cx():
    global _CONN
    if _CONN is None:
        with _CONN_LOCK:
            if _CONN is None:
                _CONN = sqlite3.connect(chatdb.DB_PATH, check_same_thread=False)
                _CONN.execute("PRAGMA journal_mode=WAL")
                _CONN.execute("PRAGMA busy_timeout=5000")
                # Multi-label support: `project` stays the single primary tag (for the
                # digest / group-by queries); `labels` holds 1-3 comma-separated topic
                # tags, primary first. Defensive migration so fresh DBs get the column.
                cols = [r[1] for r in _CONN.execute("PRAGMA table_info(messages)")]
                if "labels" not in cols:
                    _CONN.execute("ALTER TABLE messages ADD COLUMN labels TEXT")
                    _CONN.commit()
    return _CONN

# --- Worker (primary) LLM backend --------------------------------------------
# Default: an OpenRouter-hosted model, metered and off the interactive assistant's
# budget. SWAP FREELY: point OR_URL/OR_MODEL at any OpenAI-compatible /chat/completions
# endpoint (a local llama.cpp/vLLM server, another provider, etc.). See README.
OR_MODEL = os.environ.get("OR_MODEL", "nvidia/nemotron-3-super-120b-a12b")
OR_URL   = os.environ.get("OR_URL", "https://openrouter.ai/api/v1/chat/completions")


def _load_key():
    """API key for the worker endpoint: env var first, else a secrets.env file."""
    k = os.environ.get("OPENROUTER_API_KEY")
    if k:
        return k
    path = os.environ.get("SECRETS_ENV", os.path.expanduser("~/.config/chat-archive/secrets.env"))
    if os.path.exists(path):
        for l in open(path):
            if l.startswith("OPENROUTER_API_KEY="):
                return l.split("=", 1)[1].strip()
    return None

OR_KEY = _load_key()

# --- Fallback (chat) LLM backend ---------------------------------------------
# Used only when the worker is down/empty. Any CLI that takes a prompt and returns JSON
# on stdout works; the default assumes a `claude`-style CLI. Swap via CHAT_BIN.
CHAT_BIN   = os.environ.get("CHAT_BIN", "claude")
CHAT_MODEL = os.environ.get("FALLBACK_MODEL", "opus")

_ALERT_STATE    = os.path.join(HERE, ".fallback_alert")
_ALERT_THROTTLE = 3600
# Optional: point these at files holding a bot token / chat id to get a throttled alert
# when the worker falls back. Leave unset to disable alerts.
_BOT_TOKEN_F    = os.environ.get("ALERT_BOT_TOKEN_FILE", "")
_NOTIFY_CHAT_F  = os.environ.get("ALERT_CHAT_ID_FILE", "")

PROJECTS_FILE = os.path.join(HERE, "projects.json")
LABELS_FILE   = os.path.join(HERE, "labels.json")
GAP_SECS = 30 * 60          # >30 min idle in a chat starts a new burst
MAX_BURST_MSGS = 16         # cap messages fed per burst
MAX_CHARS = 1600            # cap transcript chars per burst (batched, so keep tight)
LOOKBACK_MSGS = 6           # prior in-chat messages fed as read-only context
LOOKBACK_CHARS = 800        # cap look-back chars per burst


def _is_reset(text):
    """A /clear (or /new) command — an explicit topic boundary. Never cross it when
    building bursts or look-back context: it means the user deliberately dropped the
    prior thread, so nothing before it should influence what comes after."""
    return (str(text).strip().split() or [""])[0] in ("/clear", "/new")


def load_projects():
    return json.load(open(PROJECTS_FILE))["projects"]


def save_new_projects(slugs):
    """Add any new slugs to projects.json (before 'general')."""
    d = json.load(open(PROJECTS_FILE))
    have = {p["slug"] for p in d["projects"]}
    idx = next((i for i, p in enumerate(d["projects"]) if p["slug"] == "general"),
               len(d["projects"]))
    added = False
    for slug in slugs:
        if slug and slug not in have:
            d["projects"].insert(idx, {"slug": slug, "hint": "(auto-added by classifier)"})
            have.add(slug); idx += 1; added = True
    if added:
        tmp = PROJECTS_FILE + ".tmp"
        json.dump(d, open(tmp, "w"), indent=2)
        os.replace(tmp, PROJECTS_FILE)


def load_labels():
    return json.load(open(LABELS_FILE))["labels"]


def save_new_labels(slugs):
    """Append any genuinely-new label slugs to labels.json (before 'general'), so the
    vocabulary the LLM sees grows over time — one source of truth for the label set."""
    d = json.load(open(LABELS_FILE))
    have = {p["slug"] for p in d["labels"]}
    idx = next((i for i, p in enumerate(d["labels"]) if p["slug"] == "general"),
               len(d["labels"]))
    added = False
    for slug in slugs:
        if slug and slug not in have:
            d["labels"].insert(idx, {"slug": slug, "hint": "(auto-added by classifier)"})
            have.add(slug); idx += 1; added = True
    if added:
        tmp = LABELS_FILE + ".tmp"
        json.dump(d, open(tmp, "w"), indent=2)
        os.replace(tmp, LABELS_FILE)


def _alert(text):
    """Throttled (1/hr) alert to an ops chat. Best-effort; disabled unless the alert
    token/chat-id files are configured via env."""
    if not _BOT_TOKEN_F:
        return
    now = time.time()
    try:
        if now - float(open(_ALERT_STATE).read().strip()) < _ALERT_THROTTLE:
            return
    except Exception:
        pass
    try:
        open(_ALERT_STATE, "w").write(str(now))
    except Exception:
        pass
    try:
        token = open(_BOT_TOKEN_F).read().strip()
        chat = (open(_NOTIFY_CHAT_F).read().strip()
                if _NOTIFY_CHAT_F and os.path.exists(_NOTIFY_CHAT_F) else "123456789")
        data = urllib.parse.urlencode({"chat_id": chat, "text": text,
                                       "parse_mode": "Markdown",
                                       "disable_web_page_preview": "true"}).encode()
        urllib.request.urlopen(
            urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                   data=data), timeout=20).read()
    except Exception as e:
        print(f"  [classify] alert failed: {e}", file=sys.stderr)


def _worker(prompt, max_tokens):
    """Primary. Returns (content, reason): content='' on failure/empty; reason describes."""
    payload = json.dumps({
        "model": OR_MODEL, "temperature": 0.0, "max_tokens": max_tokens,
        "reasoning": {"enabled": False},
        "messages": [{"role": "system", "content": "Output ONLY the requested JSON, no prose."},
                     {"role": "user", "content": prompt}]}).encode()
    last = None
    for attempt in range(3):
        req = urllib.request.Request(OR_URL, data=payload,
                                     headers={"Authorization": f"Bearer {OR_KEY}",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=130) as r:
                d = json.load(r)
            c = (d["choices"][0]["message"].get("content") or "").strip()
            return c, (None if c else "returned empty")
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503):
                last = f"HTTP {e.code}"; time.sleep(2 ** attempt); continue
            return "", f"HTTP {e.code}"
        except Exception as e:
            last = type(e).__name__; time.sleep(2 ** attempt); continue
    return "", (last or "failed")


def _chat_fallback(prompt, timeout=240):
    """Fallback: a CLI-driven chat LLM. Returns '' on failure."""
    try:
        r = subprocess.run(
            [CHAT_BIN, "-p", prompt, "--model", CHAT_MODEL,
             "--dangerously-skip-permissions", "--output-format", "json"],
            capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0 and not json.loads(r.stdout).get("is_error"):
            return (json.loads(r.stdout).get("result") or "").strip()
        print(f"  [classify] chat fallback rc={r.returncode}", file=sys.stderr)
    except Exception as e:
        print(f"  [classify] chat fallback error: {e}", file=sys.stderr)
    return ""


def llm(prompt, max_tokens=2000):
    content, reason = _worker(prompt, max_tokens)
    if content:
        return content
    fb = _chat_fallback(prompt)
    if fb:
        _alert(f"⚠️ *Chat classifier: worker {reason}* — fell back to the *chat* LLM. "
               f"Check the worker endpoint.")
        return fb
    raise RuntimeError(f"Worker ({reason}) and chat fallback both failed")


def fetch_bursts(limit):
    c = _cx()
    rows = c.execute(
        "SELECT id, epoch, chat_id, chat_title, sender, direction, text "
        "FROM messages WHERE project IS NULL OR labels IS NULL "
        "ORDER BY chat_id, epoch").fetchall()
    bursts, cur, last_chat, last_ep = [], [], None, None
    for row in rows:
        _id, ep, chat_id, title, sender, direction, text = row
        if cur and (chat_id != last_chat or ep - last_ep > GAP_SECS
                    or _is_reset(text)):
            bursts.append(cur); cur = []
            if len(bursts) >= limit:
                return bursts
        cur.append(row); last_chat, last_ep = chat_id, ep
    if cur and len(bursts) < limit:
        bursts.append(cur)
    return bursts


def transcript(burst):
    lines, total = [], 0
    for _id, ep, chat_id, title, sender, direction, text in burst[:MAX_BURST_MSGS]:
        who = sender or ("Assistant" if direction == "out" else "User")
        one = " ".join(str(text).split())
        line = f"{who}: {one}"
        if total + len(line) > MAX_CHARS:
            lines.append(line[:max(0, MAX_CHARS - total)]); break
        lines.append(line); total += len(line)
    return "\n".join(lines)


def lookback(burst):
    """The messages immediately preceding this burst in the same chat, ANY gap.

    Read-only context so a terse follow-up ('and the price?', 'send it again', 'ok')
    is classified against the running thread instead of in isolation. Each line carries
    its own project tag if already classified. Returns (context_lines, prior_project)
    where prior_project is the tag of the closest preceding classified message — the
    carry-forward default when the burst has no topic of its own.
    """
    c = _cx()
    first_id, first_ep, chat_id = burst[0][0], burst[0][1], burst[0][2]
    rows = c.execute(
        "SELECT sender, direction, text, project FROM messages "
        "WHERE chat_id=? AND (epoch < ? OR (epoch=? AND id < ?)) "
        "ORDER BY epoch DESC, id DESC LIMIT ?",
        (chat_id, first_ep, first_ep, first_id, LOOKBACK_MSGS)).fetchall()
    rows = list(reversed(rows))                       # back to chronological order
    cut = max((j for j, r in enumerate(rows) if _is_reset(r[2])), default=-1)
    rows = rows[cut + 1:]                              # drop everything up to & incl. last /clear
    prior_project = next((p for _s, _d, _t, p in reversed(rows) if p), None)
    lines, total = [], 0
    for sender, direction, text, project in rows:
        who = sender or ("Assistant" if direction == "out" else "User")
        tag = f" [{project}]" if project else ""
        line = f"{who}{tag}: " + " ".join(str(text).split())
        if total + len(line) > LOOKBACK_CHARS:
            lines.append(line[:max(0, LOOKBACK_CHARS - total)]); break
        lines.append(line); total += len(line)
    return lines, prior_project


def classify_batch(bursts, projects, labels_cat):
    catalog = "\n".join(f"- {p['slug']}: {p['hint']}" for p in projects)
    label_catalog = "\n".join(f"- {p['slug']}: {p['hint']}" for p in labels_cat)
    blocks = []
    for i, b in enumerate(bursts):
        ctx_lines, prior = lookback(b)
        parts = [f"### Excerpt {i}"]
        if ctx_lines:
            parts.append("Earlier in this chat (context only — do NOT classify these):\n"
                         + "\n".join(ctx_lines))
        if prior:
            parts.append(f"(Immediately preceding topic: {prior})")
        parts.append("Messages to classify:\n" + transcript(b))
        blocks.append("\n".join(parts))
    excerpts = "\n\n".join(blocks)
    prompt = (
        "You are labeling short business-chat excerpts, each with the ONE project "
        "it is about. Do NOT use any tools — just answer.\n\n"
        "Project catalog (slug: meaning):\n" + catalog + "\n\n"
        "Pick the project by what the HUMAN is actually trying to accomplish in the "
        "excerpt — the subject of the request. Do NOT be swayed by an incidental tool, "
        "command, file, or product name that merely appears while answering (e.g. the "
        "assistant querying a database, or a product mentioned in passing) — those are "
        "the MECHANISM, not the topic. Weight the user's messages over the assistant's.\n"
        "Each excerpt may include a few earlier messages for CONTEXT ONLY, then the "
        "messages to classify. A topic usually continues from what came just before: if "
        "the messages to classify have no clear topic of their own (a short follow-up "
        "like 'and the price?', 'send it again', 'ok'), carry forward the immediately "
        "preceding topic. Only pick a different project when the content clearly shifts.\n"
        "Prefer an existing slug; only if none fit, invent a short lowercase-kebab-case "
        "slug naming the topic.\n\n"
        "Also give `labels`: the topics this excerpt is genuinely ABOUT, chosen from the "
        "label catalog below (the ONE full set of known labels). Put the primary project "
        "slug FIRST, then only the cross-cutting themes the conversation is really "
        "discussing — including the underlying THEME even when it's never named as a tool "
        "(a thread weighing whether data goes to the cloud is about `data-privacy`, even "
        "if the word 'privacy' never appears). Prefer existing labels, but invent a short "
        "lowercase-kebab-case label when none fit.\n"
        "Be selective, NOT exhaustive: aim for 1-3 labels, 4 only if the excerpt truly "
        "spans that many. Do NOT add a label just because a system/tool/file got "
        "name-dropped while answering — label the subject, not the plumbing. One precise "
        "label beats five loose ones; don't pad.\n\n"
        "Label catalog (slug: meaning):\n" + label_catalog + "\n\n"
        + excerpts + "\n\n"
        "Respond with ONLY a JSON array, one object per excerpt, in order:\n"
        '[{"i": 0, "project": "<slug>", "labels": ["<slug>", ...]}, ...]')
    raw = llm(prompt)
    start = raw.index("["); end = raw.rindex("]") + 1
    arr = json.loads(raw[start:end])

    def _norm(s):
        return (s or "").strip().strip('".,').lower().replace(" ", "-").replace("_", "-")

    out = {}
    for item in arr:
        i = item.get("i")
        slug = _norm(item.get("project")) or "general"
        labels = [_norm(x) for x in (item.get("labels") or []) if _norm(x)]
        # Guarantee the primary is present and first; dedupe while preserving order.
        labels = [slug] + [l for l in labels if l != slug]
        seen, ordered = set(), []
        for l in labels:
            if l not in seen:
                seen.add(l); ordered.append(l)
        if isinstance(i, int):
            out[i] = {"project": slug, "labels": ordered}
    return out


def classify_pending(limit=200, batch=15, dry_run=False, log=print):
    """Classify every currently-unclassified message (project IS NULL). Returns the number
    tagged. Shared by the CLI/safety-net cron and the in-process real-time worker.

    Grouping unclassified rows into bursts here means a flurry of messages collapses into
    one batched LLM call even in real-time mode, while a lone message is still tagged on
    its own. Pass log=None to run silently (worker mode)."""
    projects = load_projects()
    valid = {p["slug"] for p in projects}
    labels_cat = load_labels()
    valid_labels = {p["slug"] for p in labels_cat}
    bursts = fetch_bursts(limit)
    if not bursts:
        if log: log("nothing to classify (0 unclassified messages).")
        return 0

    c = _cx()
    tagged = 0
    for b0 in range(0, len(bursts), batch):
        chunk = bursts[b0:b0 + batch]
        try:
            labels = classify_batch(chunk, projects, labels_cat)
        except Exception as e:
            if log: log(f"  batch {b0}: LLM error ({e}) — leaving unclassified")
            continue
        # PRIMARY project grows projects.json; the full label set grows labels.json.
        new_slugs = [d["project"] for d in labels.values() if d["project"] not in valid]
        if new_slugs and not dry_run:
            save_new_projects(new_slugs); valid |= set(new_slugs)
        new_labels = [l for d in labels.values() for l in d["labels"]
                      if l not in valid_labels]
        if new_labels and not dry_run:
            save_new_labels(new_labels); valid_labels |= set(new_labels)
        for i, burst in enumerate(chunk):
            d = labels.get(i) or {"project": "general", "labels": ["general"]}
            slug = d["project"]
            lbls = ",".join(d["labels"])
            ids = [r[0] for r in burst]
            title = burst[0][3] or burst[0][2]
            flag = " *NEW*" if slug not in (valid - set(new_slugs)) else ""
            if log: log(f"  [{lbls}]{flag}  {len(ids)} msg  ({title})")
            if dry_run:
                continue
            c.executemany("UPDATE messages SET project=?, labels=? WHERE id=?",
                          [(slug, lbls, i2) for i2 in ids])
            c.commit()
            tagged += len(ids)
    if log: log(f"done: {len(bursts)} bursts, {tagged} messages classified"
                + (" (dry-run, nothing written)" if dry_run else ""))
    return tagged


# ---- real-time worker -------------------------------------------------------
# Started in-process by the gateway (classify.start_worker()). It turns tagging from a
# 10-min poll into an event: chatdb.record() sets chatdb._new_msg on every insert, we
# wake, and classify whatever is still unclassified. The hourly cron remains only as a
# safety net for rows a transient LLM outage left behind.
_worker_started = False


def worker_loop(debounce=1.5):
    """Wait for new-message signals and tag pending rows.

    We clear the event BEFORE processing, so a message arriving mid-run just re-fires the
    next iteration — at worst one extra empty pass, never a lost message. The short
    debounce lets a rapid burst accumulate so it batches into a single LLM call."""
    def _log(m):
        print(f"[classify-worker] {m}", flush=True)
    _log("started (real-time tagging active)")
    while True:
        chatdb._new_msg.wait()
        chatdb._new_msg.clear()
        if debounce:
            time.sleep(debounce)          # let a burst accumulate before the LLM call
        try:
            n = classify_pending(log=None)
            if n:
                _log(f"tagged {n} message(s)")
        except Exception as e:
            _log(f"error: {e}")
            time.sleep(5)                 # back off; the safety-net cron will retry too


def start_worker(debounce=1.5):
    """Launch the real-time classify worker as a daemon thread. Idempotent."""
    global _worker_started
    if _worker_started:
        return
    _worker_started = True
    threading.Thread(target=worker_loop, args=(debounce,),
                     name="classify-worker", daemon=True).start()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--batch", type=int, default=15)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    classify_pending(limit=a.limit, batch=a.batch, dry_run=a.dry_run)


if __name__ == "__main__":
    main()
