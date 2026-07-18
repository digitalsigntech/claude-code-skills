# kb-refine-loop — self-improving knowledge base from your own sent replies

Every reply you send to a customer question is a free training example: it is the
ground-truth answer to a real question. This skill closes the loop — it tests
whether your knowledge base ALONE could have produced your answer, in both facts
and writing style, and where it couldn't, it fixes the KB / style profile. Over
time the KB converges toward answering the way you actually answer.

## How it works

A cron watcher (`src/watch.py`) scans a local SQLite email archive for new SENT
replies from the owner to external addresses, in threads where the customer asked
a question. For each one it launches a headless Claude session with
`src/refine_prompt.md`, which runs this loop:

1. **Blind attempt** — load the thread but hold back the target reply's body (it's
   the answer key); draft the full reply as the owner would send it — facts from
   the KB only, voice from the owner's writing-style profile.
2. **Diff** — read the actual reply; classify every fact as MATCH / MISSING /
   CONFLICT / EXTRA, and note style deltas (greeting, sign-off, length,
   structure, tone, framing).
3. **Patch** — append missing facts as Q&A pairs; consolidate conflicting KB
   entries into one canonical answer (the sent reply wins; superseded values are
   kept as dated notes, never silently erased). Merge systematic style deltas
   into the learned style profile (one email is a data point, not a rule).
4. **Re-test** — re-draft from the patched KB + profile; repeat until converged
   (max 3 attempts).
5. **Report** — one `REFINE-RESULT:` line in the log; a short Telegram note to the
   owner ONLY when KB files were actually changed.

In the field this converged in 2 iterations on the first real thread, catching a
genuine conflict (two contradictory pressure specs) the plain fact-extraction
pipeline had let through.

## Why not just extract facts from sent mail?

Extraction (see `email-knowledge-extract`) captures facts but never checks that
the KB can REPRODUCE the answer, and it happily accumulates contradictory entries.
The refine loop is generative testing: it finds silent gaps ("KB never says which
pressure to use") and conflicts ("two QA pairs disagree") that extraction can't.

## Install

1. Adapt paths at the top of `src/watch.py` (archive DB, prompt path, claude
   binary, model). The archive is any SQLite table of emails with
   `id, thread_id, internal_date, from_addr, to_addr, labels, body_new` —
   see `email-knowledge-extract` for the downloader that builds it.
2. Adapt `src/refine_prompt.md`: your KB layout, your notification channel
   (replace `OWNER_CHAT_ID`; or swap the Telegram call for email/Slack).
3. First run seeds state with ALL existing sent mail (nothing is bulk-processed):
   `python3 watch.py`. Then install the cron:
   `*/30 7-22 * * * .../run.sh`
4. Manual run on one thread: `python3 watch.py --force THREAD_ID:REPLY_ID`.

## Guard rails worth keeping

- Headless runs may write only inside the knowledge-base directory; never send
  email; never touch private folders.
- Exclude DRAFT-labeled messages from your archive at ingest — Gmail auto-saves
  drafts while composing, and a stale draft is a poisoned answer key (we learned
  this the hard way: a draft said 3 psi, the sent reply said 5).
- Seed state before enabling the cron, cap runs per tick, and gate on "the
  inbound message actually contains a question" to control LLM spend.

## Multiple writers

Set `KB_WRITERS="alice@co.com=alice,bob@co.com=bob"` to watch every listed
person's sent replies; each learns into their own `learned-<person>.md` style
file while feeding one shared KB. A watch-start fence auto-seeds a newly added
writer's pre-existing history so it is never bulk-refined. Without `KB_WRITERS`
the loop falls back to the single `KB_OWNER_EMAIL`.
