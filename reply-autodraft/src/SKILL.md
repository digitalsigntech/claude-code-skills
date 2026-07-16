# Skill: Reply Auto-Drafting

**Goal:** eventually draft replies to product inquiries *exactly* as the owner would,
so they only have to click Send. Learn from every reply the owner actually sends until
the gap closes.

> This file is both documentation **and** runtime state: `autodraft.py` reads and
> rewrites the `LEARNED-INSTRUCTIONS` block below, so keep the two HTML comment
> markers intact. It ships with an empty block; the skill fills it in as it learns.

## What it does
1. On each scan of the owner's inbox, find new **unread** messages that are genuine
   **product inquiries** (classified by the LLM). Internal/vendor/newsletter/automated
   mail is skipped.
2. Draft a reply in the **owner's voice** using the product KB
   (`knowledge-base/products/`), facts learned from past replies
   (`from-emails/reply-learnings.md`), the owner's writing-style profile, and the
   **learned drafting instructions** below.
3. Save it to **Gmail Drafts**, threaded to the original. **To** = the customer.
   **CC** = everyone CC'd on the original **+ a fixed teammate** (owner and the
   customer removed). Subject = `Re: <original>`.
4. Ping a chat channel so the owner can review, edit, and **send it themselves**
   (this skill never sends).
5. On a later scan, reconcile each draft by **threadId** (survives a changed subject):
   - Still in Drafts, unsent → **do nothing** (they haven't replied yet).
   - Draft was sent — possibly edited first (a draft sent from the Gmail UI keeps its
     draft id; the message just gains the `SENT` label) — **or** deleted and replaced
     by the owner's own reply in the thread → **learn** from the difference
     (extract KB facts + refine the instructions below). A sent message counts
     as a reply ONLY if the customer (or their corporate domain) is in To/Cc —
     a forward to a colleague is never learned from.
   - Draft gone **and** no reply sent → they deleted it → **do nothing**.

## Run
`autodraft.py` runs both phases (learn, then draft) each invocation. Scheduled via
`run.sh` on cron. State in `state.db` (created empty on first run). Never sends
email; only creates drafts.

## Learned drafting instructions
_This section is rewritten automatically each time the owner's sent reply differs
from the draft. Keep it concise — it is fed verbatim into every new draft._

<!-- LEARNED-INSTRUCTIONS:START -->
(none yet — this list fills in automatically as the skill learns from sent replies)
<!-- LEARNED-INSTRUCTIONS:END -->
