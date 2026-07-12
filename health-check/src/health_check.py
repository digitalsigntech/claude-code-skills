#!/usr/bin/env python3
"""
Weekly agent health check (runs locally — no LLM token cost).

Keeps a Claude Code agent setup in good health:
  - Memory hygiene: dead MEMORY.md pointers, exact-duplicate memories,
    orphaned memory files, name/filename mismatches, stale path references.
  - Automation health: expected cron jobs present, key processes running,
    log files recently updated.
  - Bloat: oversized logs, total workspace size.

ACTIONS:
  - AUTO-PRUNE (silent fix, recoverable via trash/ + backups):
      * dead pointers in MEMORY.md  -> line removed
      * byte-identical duplicate memory files -> extra copies moved to trash/
  - JUDGMENT CALLS (never auto-changed) -> surfaced to the owner on Telegram so
    they can ask the agent to act: orphan memories, name mismatches, stale file
    refs, dead/stalled crons, processes down, oversized logs.

The notification (Telegram) fires only when there is something to report
(judgment calls or auto-prune actions). A fully clean run is logged to file
only — no ping.

Configuration lives in `config.json` next to this script (see
`config.example.json`). Every field has a sane fallback so the script also runs
with no config file at all; but the workspace/memory paths and notification
target should be set for it to be useful.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))


# ------------------------------------------------------------- config -------
def load_config():
    """Load config.json (if present) and fill in defaults."""
    cfg_path = os.path.join(HERE, "config.json")
    cfg = {}
    if os.path.exists(cfg_path):
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)

    home = os.path.expanduser("~")

    def expand(p):
        return os.path.expanduser(p) if isinstance(p, str) else p

    cfg["workspace_dir"] = expand(cfg.get("workspace_dir", os.path.join(home, "myproject")))
    # Default memory dir mirrors Claude Code's per-project layout; override in config.
    cfg["memory_dir"] = expand(cfg.get("memory_dir", ""))
    cfg["log_bloat_mb"] = cfg.get("log_bloat_mb", 50)
    cfg["cron_stale_days"] = cfg.get("cron_stale_days", 2)
    # [ [pgrep_pattern, label], ... ]
    cfg["expected_procs"] = cfg.get("expected_procs", [])
    # { "crontab_fragment": "human label", ... }
    cfg["expected_crons"] = cfg.get("expected_crons", {})
    # [ [log_dir_relative_to_workspace_or_abs, label], ... ]
    cfg["fresh_log_dirs"] = cfg.get("fresh_log_dirs", [])
    # notification
    tg = cfg.get("telegram", {})
    cfg["telegram"] = {
        "bot_token_file": expand(tg.get("bot_token_file", "")),
        "chat_id": tg.get("chat_id", ""),
    }
    return cfg


CFG = load_config()

WORKSPACE = CFG["workspace_dir"]
MEM_DIR = CFG["memory_dir"]
MEMORY_INDEX = os.path.join(MEM_DIR, "MEMORY.md") if MEM_DIR else ""
HEALTH_DIR = HERE
TRASH = os.path.join(HEALTH_DIR, "trash")
REPORT = os.path.join(HEALTH_DIR, "last_report.md")

BOT_TOKEN_FILE = CFG["telegram"]["bot_token_file"]
OPS_CHAT_ID = CFG["telegram"]["chat_id"]

LOG_BLOAT_MB = CFG["log_bloat_mb"]        # flag individual log files larger than this
CRON_STALE_DAYS = CFG["cron_stale_days"]  # high-frequency logs should be fresher than this

# Key long-running processes that should be up: (pgrep -f pattern, label).
# Example: ["python3 gateway.py", "chat gateway"]
EXPECTED_PROCS = [tuple(p) for p in CFG["expected_procs"]]

os.makedirs(TRASH, exist_ok=True)

now = datetime.now(timezone.utc)
auto_actions = []   # list[str]  things we fixed automatically
judgment = []       # list[str]  things needing the owner's decision
info = []           # list[str]  neutral notes for the log


def stamp():
    return now.strftime("%Y-%m-%d %H:%M UTC")


# ---------------------------------------------------------------- memory ----
def parse_index_pointers(text):
    """Return list of (lineno, raw_line, filename) for pointer lines."""
    out = []
    for i, line in enumerate(text.splitlines()):
        m = re.search(r"\]\(([^)]+\.md)\)", line)
        if m:
            out.append((i, line, m.group(1)))
    return out


def body_hash(path):
    """Hash the body of a memory file (content after frontmatter)."""
    with open(path, encoding="utf-8") as f:
        txt = f.read()
    # strip leading frontmatter block delimited by --- ... ---
    if txt.startswith("---"):
        parts = txt.split("---", 2)
        body = parts[2] if len(parts) == 3 else txt
    else:
        body = txt
    return hashlib.sha256(body.strip().encode("utf-8")).hexdigest(), txt


def front_name(txt):
    m = re.search(r"^name:\s*(.+)$", txt, re.MULTILINE)
    return m.group(1).strip() if m else None


def check_memory():
    if not MEM_DIR:
        info.append("Memory check skipped (no memory_dir configured).")
        return
    if not os.path.isdir(MEM_DIR):
        judgment.append(f"Memory dir missing: {MEM_DIR}")
        return

    files = [f for f in os.listdir(MEM_DIR)
             if f.endswith(".md") and f != "MEMORY.md"]

    with open(MEMORY_INDEX, encoding="utf-8") as f:
        index_text = f.read()
    pointers = parse_index_pointers(index_text)
    referenced = {fn for _, _, fn in pointers}

    # 1) dead pointers -> AUTO-PRUNE
    dead_lines = [(ln, raw, fn) for ln, raw, fn in pointers
                  if not os.path.exists(os.path.join(MEM_DIR, fn))]
    if dead_lines:
        backup = os.path.join(TRASH, f"MEMORY.md.{now:%Y%m%d%H%M%S}.bak")
        shutil.copy2(MEMORY_INDEX, backup)
        drop = {ln for ln, _, _ in dead_lines}
        kept = [l for i, l in enumerate(index_text.splitlines()) if i not in drop]
        with open(MEMORY_INDEX, "w", encoding="utf-8") as f:
            f.write("\n".join(kept) + "\n")
        for _, _, fn in dead_lines:
            auto_actions.append(f"Removed dead MEMORY.md pointer → `{fn}` (no such file)")

    # 2) byte-identical duplicate bodies -> AUTO-PRUNE extras
    by_hash = {}
    meta = {}
    for fn in files:
        p = os.path.join(MEM_DIR, fn)
        try:
            h, txt = body_hash(p)
        except Exception as e:
            judgment.append(f"Could not read memory `{fn}`: {e}")
            continue
        meta[fn] = txt
        by_hash.setdefault(h, []).append(fn)
    for h, group in by_hash.items():
        if len(group) > 1:
            group_sorted = sorted(group)
            keep, extras = group_sorted[0], group_sorted[1:]
            for fn in extras:
                shutil.move(os.path.join(MEM_DIR, fn), os.path.join(TRASH, fn))
                auto_actions.append(
                    f"Pruned duplicate memory `{fn}` (identical body to `{keep}`) → trash/")

    # refresh file list after pruning
    files = [f for f in os.listdir(MEM_DIR)
             if f.endswith(".md") and f != "MEMORY.md"]

    # 3) orphan memories (file present, not in index) -> JUDGMENT
    for fn in sorted(files):
        if fn not in referenced:
            judgment.append(
                f"Orphan memory `{fn}` — file exists but no MEMORY.md pointer. "
                f"Add a pointer or delete it?")

    # 4) name / filename mismatch -> JUDGMENT
    for fn in sorted(files):
        txt = meta.get(fn)
        if txt is None:
            continue
        nm = front_name(txt)
        if nm and nm != fn[:-3]:
            judgment.append(
                f"Memory `{fn}` has frontmatter name `{nm}` ≠ filename slug. Rename which way?")

    # 5) stale path references -> JUDGMENT (conservative)
    home = os.path.expanduser("~")
    path_re = re.compile(
        r"`(" + re.escape(home) + r"/[^`\s]+|[A-Za-z0-9_./-]+\.(?:py|sh|md|json|csv))`")
    for fn in sorted(files):
        txt = meta.get(fn, "")
        seen = set()
        for m in path_re.finditer(txt):
            raw = m.group(1)
            if raw in seen:
                continue
            seen.add(raw)
            # skip placeholder / illustrative paths, not real references:
            # ellipsis ("~/...") or glob wildcards ("token*.json").
            if "..." in raw or "*" in raw:
                continue
            cand = raw if raw.startswith("/") else os.path.join(WORKSPACE, raw)
            # only flag clearly-local, clearly-file paths that are absent
            if raw.startswith(home + "/") and not os.path.exists(cand):
                judgment.append(
                    f"Memory `{fn}` references `{raw}` which no longer exists. Update or remove the note?")

    info.append(f"Memory: {len(files)} files, {len(pointers)} index pointers checked.")


# ------------------------------------------------------------ automation ----
def check_crons():
    if not CFG["expected_crons"] and not CFG["fresh_log_dirs"]:
        return
    try:
        cron = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    except Exception as e:
        judgment.append(f"Could not read crontab: {e}")
        cron = ""

    # Each key is a substring expected to appear in `crontab -l`.
    for frag, label in CFG["expected_crons"].items():
        if frag not in cron:
            judgment.append(f"Cron entry for {label} (`{frag}`) is MISSING from crontab.")

    # log freshness for the high-frequency jobs
    for logdir, label in CFG["fresh_log_dirs"]:
        logdir = logdir if os.path.isabs(logdir) else os.path.join(WORKSPACE, logdir)
        if not os.path.isdir(logdir):
            continue
        newest = 0
        for root, _, fs in os.walk(logdir):
            for fn in fs:
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(root, fn)))
                except OSError:
                    pass
        if newest:
            age_days = (time.time() - newest) / 86400
            if age_days > CRON_STALE_DAYS:
                judgment.append(
                    f"{label} logs haven't updated in {age_days:.1f} days — job may be stalled.")


def check_procs():
    for pat, label in EXPECTED_PROCS:
        r = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True)
        if r.returncode != 0:
            judgment.append(f"Process down: {label} (no match for `{pat}`).")
        else:
            info.append(f"Process up: {label}.")


# ----------------------------------------------------------------- bloat ----
def check_bloat():
    big = []
    for root, _, fs in os.walk(WORKSPACE):
        if "/trash" in root or "/.git" in root:
            continue
        for fn in fs:
            if not (fn.endswith(".log") or "logs" in root):
                continue
            p = os.path.join(root, fn)
            try:
                mb = os.path.getsize(p) / 1e6
            except OSError:
                continue
            if mb > LOG_BLOAT_MB:
                big.append((mb, p))
    for mb, p in sorted(big, reverse=True):
        judgment.append(f"Log file {mb:.0f} MB: `{p}` — rotate/truncate?")
    try:
        sz = subprocess.run(["du", "-sh", WORKSPACE], capture_output=True, text=True).stdout.split()[0]
        info.append(f"Workspace size: {sz}.")
    except Exception:
        pass


# ------------------------------------------------------------- reporting ----
def send_telegram(text):
    if not (BOT_TOKEN_FILE and OPS_CHAT_ID):
        print("[telegram] not configured — skipping ping.", file=sys.stderr)
        return False
    try:
        with open(BOT_TOKEN_FILE) as f:
            token = f.read().strip()
        import urllib.request
        import urllib.parse
        data = urllib.parse.urlencode({
            "chat_id": OPS_CHAT_ID, "text": text, "parse_mode": "Markdown",
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        urllib.request.urlopen(req, timeout=20).read()
        return True
    except Exception as e:
        print(f"[telegram] send failed: {e}", file=sys.stderr)
        return False


def main():
    check_memory()
    check_crons()
    check_procs()
    check_bloat()

    lines = [f"# Agent health check — {stamp()}", ""]
    if auto_actions:
        lines.append("## 🧹 Auto-fixed")
        lines += [f"- {a}" for a in auto_actions]
        lines.append("")
    if judgment:
        lines.append("## ❓ Needs your decision")
        lines += [f"- {j}" for j in judgment]
        lines.append("")
    if not auto_actions and not judgment:
        lines.append("✅ All clean — nothing to fix or decide.")
        lines.append("")
    lines.append("## ℹ️ Notes")
    lines += [f"- {i}" for i in info]
    report = "\n".join(lines) + "\n"

    with open(REPORT, "w", encoding="utf-8") as f:
        f.write(report)
    print(report)

    # ping only when there's something to fix/decide
    if auto_actions or judgment:
        msg = [f"🩺 *Weekly agent health check* — {stamp()}", ""]
        if auto_actions:
            msg.append("🧹 *Auto-fixed:*")
            msg += [f"• {a}" for a in auto_actions]
            msg.append("")
        if judgment:
            msg.append("❓ *Needs your decision:*")
            msg += [f"• {j}" for j in judgment]
            msg.append("")
            msg.append("_Reply here and I'll handle any of these._")
        send_telegram("\n".join(msg))


if __name__ == "__main__":
    main()
