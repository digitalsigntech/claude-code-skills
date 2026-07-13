# Agent health checks

Two scheduled self-maintenance routines for a Claude Code agent setup, sharing
one config and one Telegram alert target:

- **Weekly health check** (`src/health_check.py`) — runs locally (no LLM
  tokens), grooms the agent's persistent memory, audits the supporting
  automation (cron jobs, long-running processes, log freshness), watches for
  log-file bloat, and pings the owner **only** when there is a judgment call
  or an auto-fix worth reporting. A fully clean run is logged to a file and
  stays silent.
- **Auth watchdog** (`src/auth_check.py`, every ~10 min) — detects when the
  Claude Code OAuth session dies and pings the owner to re-login, since a dead
  login silently takes down every headless agent on the box.

## What it does

| Area | Check | Action |
|------|-------|--------|
| Memory | Dead `MEMORY.md` pointers (index links a file that's gone) | **Auto-prune** the line (backs up the index to `trash/` first) |
| Memory | Byte-identical duplicate memory files | **Auto-prune** the extra copy to `trash/` |
| Memory | Orphan file (present but not linked in the index) | **Judgment** — pinged, never touched |
| Memory | `name:` frontmatter ≠ filename slug | **Judgment** |
| Memory | Note references a local file path that no longer exists | **Judgment** |
| Automation | Expected cron entries missing from `crontab -l` | **Judgment** |
| Automation | High-frequency job's log dir hasn't updated recently | **Judgment** |
| Automation | Expected long-running process not found (`pgrep -f`) | **Judgment** |
| Bloat | Individual log file over a size threshold | **Judgment** |
| Bloat | Total workspace size | Informational note |

"Auto-prune" actions are recoverable: nothing is deleted, only moved to the
skill's `trash/` folder (and the memory index is backed up before edits).

## Auth watchdog

Why it exists: a Claude Code subscription login can die silently. OAuth refresh
tokens are single-use, and concurrent `claude` processes sharing
`~/.claude/.credentials.json` (a chat gateway, cron jobs, an interactive
session) can race the token refresh — the loser's refresh is rejected and the
whole session is invalidated (known upstream bug cluster: anthropics/claude-code
issues [#56339](https://github.com/anthropics/claude-code/issues/56339),
[#24317](https://github.com/anthropics/claude-code/issues/24317),
[#43392](https://github.com/anthropics/claude-code/issues/43392)). Every
headless agent on the box is then down until someone runs `/login`, and nothing
says so.

`src/auth_check.py` checks cheapest-first and only spends tokens when
suspicious:

1. Credentials file missing/unparseable → **dead**, alert.
2. Access token not yet expired → OK (quiet — no log line, no ping).
3. Expired less than `grace_min` → OK; the next real agent turn will refresh it.
4. Expired longer than `grace_min` → live probe: one minimal headless
   `claude -p` turn (a successful turn rewrites the token, so a stale-but-valid
   session self-heals here). An auth-shaped probe error → **dead**, alert;
   network-type failures are logged and the prior state is kept.

Alerts go out over the plain Telegram Bot API, which works precisely when
Claude itself can't: one 🔴 ping per outage, a re-ping every `realert_hours`
while it stays broken, and one 🟢 recovery message. State lives in
`src/state/auth_check.json`; the log only grows on state changes and probes.

Schedule it every 10 minutes:

```cron
*/10 * * * *  /usr/bin/python3 /full/path/to/health-check/src/auth_check.py >> /full/path/to/health-check/src/logs/auth_check.log 2>&1
```

Tip: add `"src/auth_check.py": "auth watchdog"` to `expected_crons` so the
weekly check flags the watchdog itself going missing.

## How it works

- **`src/health_check.py`** — the whole routine. It reads `src/config.json`
  (see `config.example.json`), runs the checks, writes a Markdown report to
  `src/last_report.md`, prints it, and — only if there are auto-fixes or
  judgment calls — sends a Telegram message to the configured chat.
- **`src/run.sh`** — thin wrapper for cron: takes a `flock` so only one
  instance runs, and appends output to `src/logs/health.log`.
- **`src/auth_check.py`** — the auth watchdog (see its section below). Reads
  the same `src/config.json`; runs standalone on its own, faster cron cadence.

The memory model assumes the Claude Code convention where a `MEMORY.md` index
links out to individual `<slug>.md` memory files with GFM links
(`[Title](slug.md)`), and each memory file may carry YAML frontmatter with a
`name:` field.

## Prerequisites

- Python 3 (standard library only — no pip installs).
- Unix tools: `crontab`, `pgrep`, `du`, `flock` (present on typical Linux).
- Optional: a Telegram bot (token + chat ID) if you want the ping. Without it,
  the script still runs and writes the report file; it just skips the ping.

## Install / setup

1. Copy this folder somewhere stable (e.g. into your skills directory).
2. Create the config from the template:
   ```bash
   cp config.example.json src/config.json
   ```
   Edit `src/config.json` (see **Config** below). At minimum set
   `workspace_dir` and `memory_dir`; add your `expected_procs` /
   `expected_crons` and Telegram target as needed.
3. (Optional, for the ping) create a Telegram bot via `@BotFather`, put its
   token in the file named by `telegram.bot_token_file`, and set
   `telegram.chat_id` to your chat/group ID. **Never commit the token file.**
4. Test it:
   ```bash
   python3 src/health_check.py
   ```
5. Schedule it weekly with cron, e.g. Sundays at 15:00 UTC:
   ```cron
   0 15 * * 0  /full/path/to/health-check/src/run.sh
   ```

## Config

All fields live in `src/config.json`. Paths may use `~`.

| Field | Meaning | Default |
|-------|---------|---------|
| `workspace_dir` | Root of the project the agent works in; scanned for log bloat and total size | `~/myproject` |
| `memory_dir` | Directory holding `MEMORY.md` + memory files; empty string skips memory checks | *(empty)* |
| `log_bloat_mb` | Flag individual log files larger than this (MB) | `50` |
| `cron_stale_days` | Flag a watched log dir whose newest file is older than this (days) | `2` |
| `expected_procs` | `[[pgrep_pattern, label], ...]` — processes that should be up | `[]` |
| `expected_crons` | `{ "substring_in_crontab": "label", ... }` — entries that should exist | `{}` |
| `fresh_log_dirs` | `[[log_dir, label], ...]` — dirs whose freshness is checked against `cron_stale_days`; relative paths resolve under `workspace_dir` | `[]` |
| `telegram.bot_token_file` | File containing the bot token (shared by both scripts) | *(empty → no ping)* |
| `telegram.chat_id` | Chat/group ID to notify (shared by both scripts) | *(empty → no ping)* |
| `auth_watchdog.credentials_file` | Claude Code OAuth credentials file to watch | `~/.claude/.credentials.json` |
| `auth_watchdog.claude_bin` | Path to the `claude` binary — cron's PATH usually lacks `~/.local/bin`, so use an absolute path | `claude` |
| `auth_watchdog.probe_model` | Model for the probe turn (keep it cheap) | `haiku` |
| `auth_watchdog.probe_timeout_sec` | Probe turn timeout | `120` |
| `auth_watchdog.grace_min` | Expired less than this (minutes) → assume normal use will refresh; no probe | `60` |
| `auth_watchdog.realert_hours` | Re-ping this often while auth stays dead | `6` |
| `auth_watchdog.host_label` | Machine name used in alert text | hostname |

Every field has a fallback, so the script runs even with no config file — but
without `memory_dir`, `expected_procs`, `expected_crons`, and the Telegram
target it won't do much beyond the bloat scan.

## Caveats

- **Bring your own credentials.** No token, chat ID, or populated database ships
  with this skill. The Telegram token must be supplied by you and kept out of
  version control.
- **Notification backend is swappable.** The ping uses the Telegram Bot API via
  `send_telegram()`. Replace that one function to route alerts to Slack, email,
  a webhook, etc. — the rest of the routine is transport-agnostic.
- **Memory layout is opinionated.** The memory checks assume the Claude Code
  `MEMORY.md` index + `<slug>.md` convention with optional `name:` frontmatter.
  Adapt `parse_index_pointers()` / `front_name()` if your layout differs.
- **Auto-fixes are conservative and recoverable** — items move to `trash/`,
  and the memory index is backed up before edits; nothing is hard-deleted.
- **What's stripped from the original:** all company/owner identifiers, real
  paths, the real bot token and chat ID, the concrete process/cron names, and
  the runtime `logs/`, `trash/`, `.lock`, and `last_report.md` artifacts. The
  hardcoded process/cron/log lists became configurable examples in
  `config.example.json`.
