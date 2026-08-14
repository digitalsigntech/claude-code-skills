# Reminders: the block the agent has to be able to read

Installing `reminders.py` and its firing cron gives a deployment a reminder queue.
It does not give the **agent** one. Nothing in a queue announces itself: an agent
asked "remind me tomorrow at 9:15 to call the paper mill" will answer with whatever
scheduling tool it can see, and every model has cloud schedulers in its tool list.

That failure is quiet and total. The agent says *"done — you'll be reminded
tomorrow at 9:15"*, and means it. The reminder is real, somewhere. It fires into a
cloud runner nobody is looking at, it is absent from the list the owner asks for,
and the next turn — "move that to eleven" — amends a row that does not exist. The
queue is empty the whole time, the firing cron has nothing to fire, and every
component reports itself healthy. The only detector is an owner who eventually
notices they were never reminded.

So the queue ships with the words that make the agent use it. **Paste the block
below into the file the agent reads on every turn** — the project `CLAUDE.md`, or
`~/.claude/CLAUDE.md` for a whole machine. Not into a system-prompt file unless you
have checked that this deployment's adapter actually injects it: one voice adapter
kept a carefully written `agent-system-prompt.md` next to the workspace and never
passed `--append-system-prompt`, so the reminder instructions had been sitting one
directory away from the agent for a day.

Fill in the two placeholders (`<workspace>`, `<owner-key>`) and delete the rest of
this file's commentary — the block is what the agent reads.

---

## Reminders

Reminders live in ONE place on this machine: the queue at
`<workspace>/operations/reminders/reminders.py`. Creating, listing, amending and
cancelling a reminder all mean running that script. It is a queue a per-minute cron
fires from, so a row in it is the only reminder that will actually reach the owner.

**Never** schedule a reminder with anything else — not a cloud scheduler or routine
tool, not a `/schedule`-style skill, not a hand-written `crontab` line, not a
background sleep. Those fire somewhere the owner is not looking, cannot be listed
back, and cannot be amended. If the queue is broken, say so; do not substitute
another mechanism silently.

Times are absolute times in `YYYY-MM-DD HH:MM`, **in the owner's timezone**, so
resolve "tomorrow", "Monday", "in two hours" yourself and hand over the hour the
owner would say. Run `date` first and check what timezone the box is in before
doing that arithmetic: a rented server is usually UTC while the owner is not.

    cd <workspace>

    # create — the id it prints is what an amendment needs
    python3 operations/reminders/reminders.py add "2026-08-15 09:15" ping \
        "Call the paper mill about the roll stock quote" --owner <owner-key>

    # recall — the owner's own rows, pending first
    python3 operations/reminders/reminders.py list --owner <owner-key>

    # amend — id, or a word that names exactly one row
    python3 operations/reminders/reminders.py edit 12 --when "2026-08-15 11:30"
    python3 operations/reminders/reminders.py edit "paper mill" --text "Call the mill about BOTH quotes"

    # cancel
    python3 operations/reminders/reminders.py cancel 12

`ping` sends the text verbatim at fire time — that is the normal kind, and the text
is what the owner will read, so write it as a message to them rather than as a note
to yourself. `task` runs the text as an instruction at fire time and posts the
result; give a `task` a short `--label` for lists a person reads. `--photo PATH`
fires the reminder as a picture with the text as its caption.

Confirm back what was queued, in the owner's words and their timezone, with the
date beside the relative word: "queued — tomorrow (Sat Aug 15) at 9:15 AM". A
confirmation that only says "done" is exactly what a reminder that went nowhere
also says.

When the owner asks what they have coming up, in any language or phrasing ("what
have I got on", "show me my reminders", "покажи напоминания") — that is this queue,
read with `list --owner <owner-key>`. A message that merely MENTIONS reminders
while asking something else is not a request for the list.

---

## Wiring, once per install

- `TG_REMINDER_CHAT_ID` — where a reminder fires when the caller does not name a
  chat. Set it, and the block above works verbatim; leave it unset and the agent
  has to know a numeric chat id it has no way to learn, which is the surest way to
  send it back to a scheduler it can call without asking. `add` refuses rather than
  queues a reminder that would fire nowhere.
- `TG_PRIMARY_OWNER_KEY` — the `<owner-key>` in the block. One per person on the
  install; owner keys live in the database, so pick them before rows exist.
- `REMINDERS_TZ` — the OWNER's timezone (e.g. `America/Chicago`), not the box's.
  Every conversion between the written time and the fired time goes through it, so
  set it on the install and not just on the cron line; a box left on UTC otherwise
  stores the owner's Monday morning five hours early and fires "in 20 minutes"
  immediately. Set it anywhere the queue runs from — the service unit, the cron,
  and the agent's own environment.
- The firing cron, carrying the same timezone:

      REMINDERS_TZ=<owner's timezone>
      * * * * * cd <workspace> && REMINDERS_TZ=<owner's timezone> python3 operations/reminders/reminders.py fire >> operations/reminders/fire.log 2>&1

Verify the install the way the failure happens, not the way the code reads: from a
fresh session, **ask the agent in words** for a reminder a few minutes out, then
check that a row appeared (`list --owner <owner-key>`) and that it fired. An agent
that answers "done" proves nothing — the whole failure is an agent that means it.
