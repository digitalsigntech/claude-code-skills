#!/usr/bin/env python3
"""Backfill the chat archive from existing chat-LLM session transcripts.

If your chat gateway runs one headless assistant session per chat, every turn is stored
as JSONL under the assistant's projects dir. This walks those transcripts and inserts
the real conversation turns — human messages and the assistant's text replies — into
chat.db, skipping tool calls, tool results, thinking blocks and subagent sidechains.

The transcript format assumed here matches a Claude Code / claude-style JSONL session
(one JSON object per line, with `type`, `message.content`, `timestamp`, `sessionId`,
`isSidechain`). Adapt the regexes/paths below to your own gateway if it differs.

Idempotent: a turn already in the DB (same session + direction + text) is skipped, so
re-running never duplicates. Project tags are left NULL for classify.py to fill in.

  backfill.py --dry-run     # counts only, writes nothing
  backfill.py               # insert
"""
import os, sys, json, re, glob, datetime, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import chatdb

# Where the gateway's per-chat JSONL session transcripts live. Override via env.
TRANSCRIPT_DIR = os.path.expanduser(
    os.environ.get("TRANSCRIPT_DIR", "~/.claude/projects/my-project"))

# The gateway prepends a small context header to each human turn; these regexes strip it
# back out. Tune them to whatever your gateway injects (or leave — they simply no-op if
# they never match).
CTX_RE = re.compile(r'\[You are in the chat "(?P<title>.*?)" \(chat_id (?P<cid>-?\d+)\)\.?\]')
FILES_RE = re.compile(r'\[Files the user sent in this chat.*?below\.\]\s*', re.S)
EMAIL_RE = re.compile(r'\[This just arrived as an email from (?P<who>[^<\]]+?)[\s<].*?\]\s*', re.S)
SUBJECT_RE = re.compile(r'Subject:\s*(.*)', re.S)


def to_epoch(ts):
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def clean_human(text):
    """Strip the gateway's prepended context, and unwrap relayed emails.
    Returns (clean_text, sender_override, kind)."""
    sender, kind = None, "text"
    m = EMAIL_RE.search(text)
    if m:
        who = m.group("who").strip().lower()
        sender = who.split()[0] if who else None    # first token of the sender name
        kind = "email"
        text = EMAIL_RE.sub("", text, count=1)
    text = CTX_RE.sub("", text)          # drop the injected "[You are in the chat ...]"
    text = FILES_RE.sub("", text)        # drop the held-files block
    text = text.strip()
    if kind != "email" and text.startswith("/"):
        kind = "command"
    return text, sender, kind


def extract(path):
    """Yield (epoch, sender, direction, kind, session_id, title, cid, text) for one transcript."""
    room = {"title": None, "cid": None}
    for line in open(path, errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("isSidechain"):
            continue
        t = d.get("type")
        if t not in ("user", "assistant"):
            continue
        msg = d.get("message", {}) or {}
        content = msg.get("content")
        ep = to_epoch(d.get("timestamp"))
        sid = d.get("sessionId") or os.path.splitext(os.path.basename(path))[0]

        if t == "user":
            # content is usually a string (the prompt); a list means tool_result -> skip
            if isinstance(content, list):
                if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                    continue
                content = " ".join(b.get("text", "") for b in content
                                   if isinstance(b, dict) and b.get("type") == "text")
            if not isinstance(content, str):
                continue
            cm = CTX_RE.search(content)      # learn the room from the first augmented turn
            if cm:
                room["title"] = room["title"] or cm.group("title")
                room["cid"] = room["cid"] or int(cm.group("cid"))
            text, sender, kind = clean_human(content)
            # skip local slash-command help/noise and empties
            if not text or text in ("/help", "/start", "/whoami"):
                continue
            yield (ep, sender or "user", "in", kind, sid, room["title"], room["cid"], text)

        else:  # assistant: keep only the visible text blocks
            if not isinstance(content, list):
                continue
            text = "\n\n".join(b.get("text", "") for b in content
                               if isinstance(b, dict) and b.get("type") == "text").strip()
            if not text:
                continue                     # pure tool-use / thinking turn
            yield (ep, "assistant", "out", "text", sid, room["title"], room["cid"], text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    c = chatdb._get()
    # preload existing (session, direction, text) signatures for idempotent re-runs
    seen = set(c.execute("SELECT session_id, direction, text FROM messages").fetchall())
    files = sorted(glob.glob(os.path.join(TRANSCRIPT_DIR, "*.jsonl")))
    ins = dup = 0
    per_dir = {}
    for path in files:
        n = 0
        for ep, sender, direction, kind, sid, title, cid, text in extract(path):
            key = (sid, direction, text)
            if key in seen:
                dup += 1; continue
            seen.add(key)
            if not a.dry_run:
                chatdb.record(text, direction, sender=sender, chat_id=cid,
                              chat_title=title, kind=kind, session_id=sid, epoch=ep)
            ins += 1; n += 1
        if n:
            per_dir[os.path.basename(path)[:8]] = n
    print(f"transcripts: {len(files)}  |  new turns: {ins}  |  already-present: {dup}"
          + ("  (dry-run — nothing written)" if a.dry_run else ""))
    top = sorted(per_dir.items(), key=lambda kv: -kv[1])[:10]
    for name, n in top:
        print(f"  {n:>5}  {name}…")


if __name__ == "__main__":
    main()
