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

# 4. Copy ONLY the source files, plus the headless wrapper (see "Staying signed in")
cp claude-code-skills/telegram-gateway/src/*.py <PROJECT>/telegram/
mkdir -p <PROJECT>/lib && install -m 755 claude-code-skills/telegram-gateway/src/claude-headless <PROJECT>/lib/

# 5. Start it again and watch the first minute of log output
systemctl --user start telegram-gateway
```

On the first start after this version the log says `identity rendered into
<workspace>/CLAUDE.md, ~/.claude/CLAUDE.md` — the profile's identity block, added to
those files (2026-09-08). Nothing outside the block markers is touched. If the
persona file says "over Telegram", take that phrase out: the gateway now adds it.
The pointer in `~/.claude/CLAUDE.md` also imports the workspace's memory index
(`@~/.claude/projects/<path-slug>/memory/MEMORY.md`), so after this update run
`cd /tmp && claude -p "What is your name, and what does your memory index list?"`
— the answer must match one asked inside the workspace, including the detail
from a memory file (the render also adds the allow rules that let a session outside
the workspace read them; the log names `~/.claude/settings.json` once when it does). Then
`cd /tmp && claude -p "Remember: <fact>. Tell me the path you wrote."` — the file must
appear in the workspace's memory directory, written via `agentprofile.py remember`. Before, a session started
outside the workspace knew the name but had an empty memory.

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

## Staying signed in

Symptom: every so often Claude is logged out ("OAuth session expired", "please run
/login") and someone has to sign in again. Cause: OAuth refresh tokens are single-use
and every `claude` process shares `~/.claude/.credentials.json`; when the gateway, the
voice adapter, a cron and a terminal session refresh at the same moment, the loser gets
`invalid_grant` and the whole login dies.

Fix, two layers, both shipped in `src/`:
- `claude_token.py` — imported by `tgconf`, puts `CLAUDE_CODE_OAUTH_TOKEN` in the
  gateway's environment, so every turn it spawns inherits it.
- `claude-headless` — installed to `<PROJECT>/lib/`, picked up by tgconf as the binary
  when executable; re-reads the token on every turn (the file wins), so renewal needs
  no restart. Point the voice adapter at it with `"claude_bin"` in its config.json.

Then, once per Claude account:

```bash
claude setup-token        # browser sign-in; prints a token valid for one year
echo 'CLAUDE_CODE_OAUTH_TOKEN=<token>' >> ~/.config/<name>/secrets.env   # chmod 600
python3 <PROJECT>/telegram/claude_token.py    # "background token: configured"
```

That token never refreshes, so it cannot race; only an interactive terminal session
still refreshes the shared login. One token serves every machine on the account. With
no token the gateway works but stays exposed. Renew a year later the same way.
