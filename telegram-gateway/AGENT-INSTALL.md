# Agent install brief

You are an agent installing this Telegram gateway on the machine you are running on.
The operator handed you this file so they would not have to type the instructions.

Read `README.md` in this directory **in full** before you touch anything. It documents
what the gateway does, how sessions map to chats, the optional integrations, and the
manual install steps. This file only adds the judgement calls and the traps — it does
not replace the README.

---

## 1. Decide where it lives — this is the important choice

`tgconf.py` treats its **parent directory** as the Claude working directory. Whatever
folder you put `telegram/` inside becomes the folder every Telegram turn operates in:

    <PROJECT>/telegram/     <- copy src/* here, so turns run inside <PROJECT>

Chatting from a phone to an agent that can already see the real files is the whole
point of installing this. A gateway in an empty home directory technically works and
is worth very little.

**You are expected to work out what `<PROJECT>` is on this machine — nobody can put it
in this file.** Look around before you ask:

```bash
ls -la ~                      # obvious working folders
find ~ -maxdepth 3 -name CLAUDE.md -not -path '*/.*' 2>/dev/null
find ~ -maxdepth 3 -name '.git' -type d 2>/dev/null
```

- A directory containing a `CLAUDE.md` is a strong signal: someone has already set it
  up as a project for an agent to work in. Read it — it usually says what the machine
  is for.
- Otherwise look for a git repo, a knowledge base, a company's document tree, or
  whatever this box clearly exists to do.
- If your own session was started in a project directory, that is a strong default.

Then **state your choice and why, and let the operator confirm or redirect** before you
copy anything. One line is enough: "I found X, which looks like the working project —
installing there unless you say otherwise." Do not silently pick, and do not fall back
to a home directory just because nothing stood out; ask.

Use absolute paths throughout the install, and report the one you settled on.

## 2. Ask the operator for these — do not guess

**The bot token.** They create the bot in Telegram via **@BotFather** → `/newbot` →
follow the prompts. They paste you the token. Store it exactly as the README says:

    echo '<TOKEN>' > <project>/telegram/bot_token && chmod 600 <project>/telegram/bot_token

Never echo the token back into the chat, never commit it, never put it in a systemd
unit file.

**Their Telegram user ID**, for the allowlist. You cannot look this up on their behalf.
Tell them to message the bot once, then run:

    python3 <project>/telegram/tg_whoami.py

That prints the user IDs of recent senders. Write theirs into `allowlist.json`
(copy `allowlist.example.json`). **Anyone not in that file must be refused.** Confirm
the allowlist is in place before you leave the gateway running — an open bot means
strangers get a shell on this box through you.

**Whether they want group chats.** If yes, tell them to disable group privacy:
@BotFather → `/setprivacy` → pick the bot → **Disable**. Otherwise the bot only sees
messages that @-mention it. Skip if they only want direct messages.

## 3. Optional integrations

The README's *Optional integrations* section lists features built for the machine this
came from — document ingest, media search, email injection, chat archive, privacy
routing. They degrade gracefully: if the tools they call are absent, the feature
no-ops and the core gateway runs unaffected.

On a clean box, confirm from the README which ones are inert, and strip anything that
is not. Do not leave a reflex enabled that will throw on every message.

## 4. Run it as a service

Set it up to start on boot and restart on failure. Then **the trap that catches
everyone**:

> The gateway shells out to the `claude` binary. A service manager does not inherit
> the operator's login `PATH`, so if `claude` lives somewhere like `~/.local/bin`, the
> service will start cleanly and then fail on *every message* with a command-not-found
> that is invisible unless you read the logs.

Set an explicit absolute `PATH` in the unit file, or invoke `claude` by absolute path.
Verify with `which claude` as the user the service will run as.

## 5. Verify end to end before reporting success

Do not report the install as done because the process is running. A running process
that cannot reach `claude`, or whose token is wrong, looks identical to a working one.

Required: ask the operator to send a message from Telegram, and confirm a real reply
came back. Then check the logs for exceptions on that turn.

## 6. Report back with

- Where it is installed, and which directory the agent therefore works in
- The service name, and the exact commands to check status, read logs, and restart
- Where the token and allowlist live, and who is currently allowed
- Which optional integrations you kept, stripped, or left inert
- Anything you had to change to make it run on this machine

---

## Quick sanity checklist

- [ ] `telegram/` sits inside the project the operator confirmed, not a default
- [ ] `bot_token` exists, `chmod 600`, never printed or committed
- [ ] `allowlist.json` contains only the intended IDs
- [ ] Group privacy disabled (only if they want group chats)
- [ ] Service starts on boot and restarts on failure
- [ ] Absolute `PATH` set so `claude` resolves under the service manager
- [ ] A real message round-tripped from Telegram
- [ ] Logs clean on that turn

## Last step: prove this install is its own

    python3 install_check.py

It reports absolute paths that do not exist here, mailboxes outside this install's
own, and chat ids that are neither the owner's nor in the allowlist — code and
config only, never the company's content.

Run it even when you installed from this repo, and especially if any file was copied
from a machine that already worked. A copy runs immediately and keeps the original's
home directory, owner and mailboxes; the failure surfaces days later as a database
that is always empty, or a reminder that mails a stranger.
