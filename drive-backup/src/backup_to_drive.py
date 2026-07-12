#!/usr/bin/env python3
"""Scheduled backup of a project folder to Google Drive.

Keeps at most KEEP backups (prunes the oldest). EXCLUDES regenerable bulk (model
files, python venvs, caches) and SECRETS (OAuth tokens/credentials — those are
easily re-authed and you don't want them sitting in Drive). The result stays small.

Reuses the same Google OAuth credentials as the Gmail setup (auth.py + config.py);
the token just needs a Drive scope in addition to Gmail (see README / config.py).

Config via environment (all optional except the folder id; defaults shown):
  MAIL_ACCOUNT       OAuth account name for the Drive client      (default "primary")
  BACKUP_FOLDER_ID   Google Drive folder id to upload into        (REQUIRED)
  BACKUP_SRC         name of the project folder to archive        (default "myproject")
  BACKUP_SRC_PARENT  parent directory that contains BACKUP_SRC    (default "~")
  BACKUP_KEEP        how many newest backups to retain            (default 2)
  BACKUP_PREFIX      archive filename prefix                      (default "backup")
  BACKUP_EXTRA       colon-separated extra files/dirs to include  (optional)
"""
import os, sys, subprocess, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("MAIL_ACCOUNT", os.environ.get("MAIL_ACCOUNT", "primary"))
import gdrive
from googleapiclient.http import MediaFileUpload

FOLDER_ID = os.environ.get("BACKUP_FOLDER_ID", "<YOUR_DRIVE_FOLDER_ID>")
SRC_PARENT = os.path.expanduser(os.environ.get("BACKUP_SRC_PARENT", "~"))
SRC = os.environ.get("BACKUP_SRC", "myproject")
KEEP = int(os.environ.get("BACKUP_KEEP", "2"))          # documented default: keep the 2 newest
PREFIX = os.environ.get("BACKUP_PREFIX", "backup")

# Regenerable bulk + secrets/state we never want in the archive. Paths are relative
# to SRC_PARENT (tar is run with -C SRC_PARENT). Adjust to match your project layout.
EXCLUDES = [
    f"{SRC}/local-ai/models", "*/venv", "__pycache__", "*.gguf",
    # secrets — OAuth client + tokens (re-auth instead of restoring these)
    f"{SRC}/email/credentials.json", f"{SRC}/email/token*.json*", f"{SRC}/email/*.bak",
    "*.pkce_verifier", ".git",
    # rotating state / logs / local mailboxes (regenerable, or private)
    f"{SRC}/email/logs", f"{SRC}/mail", f"{SRC}/mail-secondary",
    f"{SRC}/telegram/bot_token", f"{SRC}/telegram/inbox",
    f"{SRC}/telegram/state", f"{SRC}/telegram/logs",
]


def _stage_external():
    """Copy config/state that lives OUTSIDE the project folder (crontab, plus any
    paths named in BACKUP_EXTRA) so the backup is complete. Returns the staging dir
    to add to the tar, or None if there is nothing external to stage."""
    import shutil
    ext = "/tmp/_backup_external"
    shutil.rmtree(ext, ignore_errors=True)
    os.makedirs(ext, exist_ok=True)
    staged = False
    # crontab (schedule for this and other jobs)
    try:
        cron = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
        if cron:
            open(os.path.join(ext, "crontab.txt"), "w").write(cron)
            staged = True
    except Exception:
        pass
    # any extra files/dirs the user wants captured (e.g. an autostart launcher,
    # an app-memory dir) — colon-separated absolute paths in BACKUP_EXTRA.
    for p in filter(None, os.environ.get("BACKUP_EXTRA", "").split(":")):
        p = os.path.expanduser(p)
        if os.path.isdir(p):
            shutil.copytree(p, os.path.join(ext, os.path.basename(p.rstrip("/"))))
            staged = True
        elif os.path.exists(p):
            shutil.copy2(p, os.path.join(ext, os.path.basename(p)))
            staged = True
    if not staged:
        shutil.rmtree(ext, ignore_errors=True)
        return None
    return ext


def main():
    date = datetime.date.today().isoformat()
    name = f"{PREFIX}_{date}.tar.gz"
    tar = f"/tmp/{name}"
    ext = _stage_external()
    cmd = ["tar", "czf", tar, "-C", SRC_PARENT] + [f"--exclude={e}" for e in EXCLUDES] + [SRC]
    if ext:
        cmd += ["-C", "/tmp", os.path.basename(ext)]
    print(f"{datetime.datetime.now():%F %T} creating {tar} ...")
    subprocess.run(cmd, check=True)
    if ext:
        import shutil as _sh; _sh.rmtree(ext, ignore_errors=True)
    size_kb = os.path.getsize(tar) // 1024

    s = gdrive.svc()
    # remove any same-day backup first (idempotent re-runs)
    for f in s.files().list(q=f"'{FOLDER_ID}' in parents and name='{name}' and trashed=false",
                            fields="files(id)").execute().get("files", []):
        s.files().delete(fileId=f["id"]).execute()
    up = s.files().create(body={"name": name, "parents": [FOLDER_ID]},
                          media_body=MediaFileUpload(tar, resumable=True),
                          fields="id,name").execute()
    print(f"uploaded {name} ({size_kb} KB) id={up['id']}")
    os.remove(tar)

    # prune: keep KEEP newest (names are date-sorted, so a name sort == a time sort)
    backups = s.files().list(
        q=f"'{FOLDER_ID}' in parents and name contains '{PREFIX}_' and trashed=false",
        fields="files(id,name)", orderBy="name desc").execute().get("files", [])
    for old in backups[KEEP:]:
        s.files().delete(fileId=old["id"]).execute()
        print(f"pruned: {old['name']}")
    kept = [b["name"] for b in backups[:KEEP]]
    print(f"{datetime.datetime.now():%F %T} done. retained {len(kept)}: {kept}")


if __name__ == "__main__":
    main()
