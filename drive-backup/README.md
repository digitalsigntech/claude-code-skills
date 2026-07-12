# drive-backup

Scheduled backup of a project folder to Google Drive. Runs on a cron/systemd timer,
uploads a compressed archive of your project into a chosen Drive folder, and keeps
only the newest N copies (default 2). Regenerable bulk (venvs, model files, caches)
and secrets (OAuth tokens/credentials) are excluded so the archive stays small and
carries no keys.

## What it does

Each run tars the project folder (minus the excludes), optionally appends a bit of
external config (your crontab plus anything listed in `BACKUP_EXTRA`), uploads it to
Google Drive as `<prefix>_<YYYY-MM-DD>.tar.gz`, then prunes older archives so at most
`BACKUP_KEEP` remain. Re-running on the same day replaces that day's archive
(idempotent).

## How it works

| File | Role |
|------|------|
| `src/backup_to_drive.py` | Builds the archive, uploads it, prunes old copies. All knobs are env vars. |
| `src/backup_run.sh` | Cron entrypoint: activates the venv, runs the job, logs output. |
| `src/gdrive.py` | Minimal Google Drive client (list/upload/prune/etc.). Backup uses `svc()`. |
| `src/auth.py`, `src/config.py` | OAuth login + scopes/accounts config (shared with the Gmail tooling). |
| `credentials.example.json` | Template for the OAuth client file — fill in and rename to `credentials.json`. |

It reuses **the same Google OAuth credentials as the Gmail setup**. If you already run
that tooling, point this at the same `credentials.json`/token — you only need to add a
Drive scope (see below) and re-run the login once.

## Prerequisites

- Python 3.9+
- `pip install google-api-python-client google-auth google-auth-oauthlib`
- A Google Cloud project with the **Drive API** enabled and a **Desktop** OAuth client.
- A destination folder in Google Drive (grab its folder id from the URL).

## Install / setup

1. Copy `credentials.example.json` to `src/credentials.json` and fill in your OAuth
   client id/secret (Google Cloud Console → APIs & Services → Credentials → Desktop app).
   *Never commit the real `credentials.json` or any `token*.json`.*
2. Make sure `config.py` `SCOPES` includes a Drive scope (it ships with
   `drive.file`). If you are reusing an existing Gmail token, add the Drive scope
   there too — a token consented to Gmail only cannot write to Drive.
3. Authorize: `cd src && python auth.py primary` — open the printed URL on this
   machine, sign in, approve. A `token.json` is written (mode 600).
4. Create/choose a Drive folder and note its id.
5. Set up a venv the run script can find (default `src/venv/`), or point
   `BACKUP_VENV_PY` at your interpreter.
6. Schedule it, e.g. daily at 03:00 via crontab:
   ```cron
   0 3 * * *  BACKUP_FOLDER_ID=<YOUR_DRIVE_FOLDER_ID> BACKUP_SRC=myproject \
              BACKUP_SRC_PARENT=$HOME /path/to/drive-backup/src/backup_run.sh
   ```

## Config

All via environment variables:

| Var | Default | Meaning |
|-----|---------|---------|
| `BACKUP_FOLDER_ID` | *(required)* | Google Drive folder id to upload into. |
| `BACKUP_SRC` | `myproject` | Name of the project folder to archive. |
| `BACKUP_SRC_PARENT` | `~` | Parent directory that contains `BACKUP_SRC`. |
| `BACKUP_KEEP` | `2` | How many newest archives to retain (older are pruned). |
| `BACKUP_PREFIX` | `backup` | Archive filename prefix. |
| `BACKUP_EXTRA` | *(none)* | Colon-separated extra files/dirs to fold into the archive (e.g. an autostart launcher, an app-state dir). |
| `MAIL_ACCOUNT` | `primary` | Which OAuth account/token to use (see `config.py`). |
| `BACKUP_VENV_PY` | `src/venv/bin/python` | Interpreter the run script uses. |
| `BACKUP_LOG` | `src/backup.log` | Log file path. |

**Exclusion list** (edit `EXCLUDES` in `backup_to_drive.py` to match your layout).
Three kinds of things are deliberately left out:

- **Secrets** — `credentials.json`, `token*.json*`, `*.pkce_verifier`, `*.bak`.
  These are trivial to re-auth; keeping them out of Drive avoids leaking keys.
- **State / logs** — log dirs, telegram `bot_token`/`inbox`/`state`, `.git`.
- **Mailboxes & regenerable bulk** — local `mail/` folders, model files (`*.gguf`),
  python `venv`s, `__pycache__`. Big and rebuildable, so not worth backing up.

## Caveats

- **Bring your own Google credentials.** No `credentials.json` or token ships here —
  only `credentials.example.json`. Authorize on your own Google account.
- **Drive scope required.** The token must carry a Drive scope. `drive.file` (app's
  own files) is enough for backups; use the broader `drive` scope only if you also
  want the `gdrive.py` read/list commands to see other files.
- **Retention is by filename date.** Archives sort by name, which equals time order
  because names embed the ISO date. If you change `BACKUP_PREFIX` mid-stream, old
  archives under the previous prefix won't be pruned.
- **Excludes are project-specific.** The shipped `EXCLUDES` reflect a Gmail/Telegram/
  local-AI style layout; review and trim them for your own project before relying on it.
- The archive is written to `/tmp` during upload and removed afterward.
