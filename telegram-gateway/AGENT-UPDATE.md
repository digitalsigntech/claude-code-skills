# Agent update brief

You are an agent updating a Telegram gateway that is **already installed and running**
on this machine. If nothing is installed yet, read `AGENT-INSTALL.md` instead — this
file assumes a working install and only covers moving it to a newer version.

The whole update is: refresh the source, keep the local state, restart. The care is
all in knowing which files are ours and which are yours.

---

## 1. What belongs to whom

Two kinds of file live in the installed `telegram/` directory, and the difference is
the only thing that can go wrong here.

**Ours — overwrite freely.** Everything that came from `src/`: `gateway.py`,
`tgconf.py`, `tg_api.py`, `bridge.py` and every `*_reflex.py`. These carry no local
settings. `tgconf.py` in particular looks like config but is not — it reads `TG_*`
environment variables and falls back to files, so replacing it keeps your setup.

**Yours — never touch.** `bot_token`, `allowlist.json`, `doc_registry.json`, the
`state/` directory (the getUpdates cursor and the session map), `inbox/`, `logs/`, and
any `.env` or service file you wrote. An update that clobbers `state/offset` makes the
bot replay or skip messages; one that clobbers `allowlist.json` locks you out.

If you are unsure whether a file is yours, it is yours. Copy from `src/` by name
rather than syncing the whole directory.

---

## 2. Do it

```bash
# 1. Get the new version
cd /path/to/claude-code-skills && git pull

# 2. Stop the running gateway (however it was started — systemd, tmux, nohup)
systemctl --user stop telegram-gateway   # or: pkill -f gateway.py

# 3. Back up what you are about to overwrite, so a bad update is one command to undo
cp -r <PROJECT>/telegram <PROJECT>/telegram.bak-$(date +%Y%m%d)

# 4. Copy ONLY the source files
cp claude-code-skills/telegram-gateway/src/*.py <PROJECT>/telegram/

# 5. Start it again and watch the first minute of log output
systemctl --user start telegram-gateway
```

Then send the bot one message and confirm it answers. A gateway that starts cleanly
but has stopped receiving is the failure mode worth catching immediately, and it does
not show up in the logs as an error.

---

## 3. The traps, in the order they bite

**New files.** `gateway.py` imports its reflexes unconditionally, so a version that
adds one dies on import if you copied only the files you already had. Copy `src/*.py`
as a glob, never a hand-listed subset. `python3 -c "import gateway"` from the install
directory catches this in a second.

**New config knobs.** New features read new `TG_*` variables. They all have defaults,
so the gateway runs without them — the feature is simply inert until you set them.
After an update, diff the top of `tgconf.py` against your environment and set anything
new you actually want. Current knobs worth knowing about: `TG_OWNER_ID`,
`TG_OWNER_NAME`, `TG_OWNER_EMAIL`, `TG_PRIMARY_OWNER_KEY`, `TG_SECOND_OWNER_ID`,
`TG_SECOND_OWNER_KEY`, `TG_FRIEND_EMAIL`.

**Local edits you forgot you made.** If someone patched the installed copy directly,
this overwrites it silently. `diff -r` the install against `src/` BEFORE copying; if
anything differs beyond config, decide deliberately whether to keep it — and then move
it into a proper local module so the next update cannot eat it.

**Python version and deps.** Only `requests` is external. If the update fails to
import something else, that is a bug in the release, not your install.

---

## 4. Rolling back

The backup from step 3 is the rollback: stop the service, move the backup back into
place, start it. State and token come with it, so you land exactly where you were.
