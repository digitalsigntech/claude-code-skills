# KB self-refinement loop (one customer thread)

You are running headless on Mercury. Work SILENTLY (no emails, no drafts, no files
outside `~/DST/knowledge-base/`). Your job: test whether the DST knowledge base could
have answered a customer's questions as well as the owner's actual reply did — in BOTH
facts and writing style — and if not, fix the KB / style profile until it can. This
is a loop the owner approved; style capture added at their request.

Inputs (appended below): THREAD_ID and REPLY_ID — a Gmail thread in the local email
archive and the id of the owner's sent reply to test against.

## Procedure

1. **Load the thread** (oldest first):
   `sqlite3 ~/DST/crm/contacts.db "SELECT id, date, from_addr, subject, body_new FROM emails WHERE thread_id='<THREAD_ID>' ORDER BY internal_date;"`
   Read everything EXCEPT the body of REPLY_ID. Do NOT read REPLY_ID's body yet —
   that is the answer key. (Earlier replies from the owner in the thread are fine to read.)

2. **Extract the customer's question(s)** that REPLY_ID is answering (the latest
   inbound message before it). If there is no factual/technical/product question —
   pure scheduling, thanks, shipping notice, price-list request, etc. — print
   `REFINE-RESULT: skip (no factual question)` and stop. Also check REPLY_ID's
   to/cc (metadata only, not the body): if it is NOT addressed to the customer
   who asked — e.g. a forward to a colleague or third party — it is not a reply;
   print `REFINE-RESULT: skip (not addressed to the customer)` and stop.

3. **Attempt N (start N=1): draft the full reply from the KB only, as the owner.**
   Facts: use `~/DST/knowledge-base/` (product-qa.md, manuals,
   phd-recovery-fluids.md, price-list.md, from-emails/, `ug -in` searches).
   Style: write it as an email the owner would actually send, following
   `knowledge-base/writing-styles/<owner>.md` + `learned-<owner>.md`
   (greeting, sentence length, structure, sign-off — the works). Do not guess
   facts: if the KB doesn't cover something, say so in the draft.

4. **Now read REPLY_ID's body** (the same sqlite query, `WHERE id='<REPLY_ID>'`).
   Compare your attempt to the owner's reply on two axes:
   - **Facts:** classify each of the owner's facts: MATCH / MISSING from KB / CONFLICT
     (KB says otherwise or holds contradictory entries) / EXTRA (the owner omitted it;
     not an error).
   - **Style:** diff greeting, sign-off, length, sentence structure, tone,
     formatting, and how he frames the answer (e.g. leads with the fix, adds a
     caveat, upsells, sets a boundary). Note each way your draft would read as
     "not the owner" to someone who knows his emails.

5. **Patch the KB** for every MISSING and CONFLICT:
   - New facts → append Q&A pairs to `knowledge-base/products/product-qa.md`
     (product) or `knowledge-base/company/company-qa.md` (company), Q&A format.
   - Conflicts → CONSOLIDATE into one canonical entry (the owner's sent reply wins;
     note superseded values with date + source, e.g. "per the owner's reply to X,
     YYYY-MM-DD"). Never leave two QA pairs answering the same question differently.
   - Keep edits surgical; don't rewrite unrelated entries.

   **Patch the style profile** for real style deltas: merge them into
   `knowledge-base/writing-styles/learned-<owner>.md` (autodraft reads this file
   too, so it improves both systems). Rules:
   - MERGE, don't append — update the existing bullet under the right heading
     (Voice & tone / Greetings / Sign-offs / Sentence & structure / Vocabulary /
     Formatting) or add one concise bullet; keep the file short.
   - One email is a data point, not a rule. Only write a delta that is clearly
     systematic (contradicts nothing and fits his known voice) or that CONTRADICTS
     an existing profile line (then fix that line). Phrase one-off observations as
     tendencies ("often", "in technical-support replies tends to..."), and prefer
     context-scoped notes — style in technical advice may differ from sales
     replies.
   - Never copy customer names/PII into the profile; describe the pattern, not
     the email.

6. **Re-run the attempt (N+1) from the patched KB + style profile.** Converged =
   every fact matches AND the draft reads like the owner wrote it (no style delta a
   reader would flag). Max 3 attempts; if still diverging, record what couldn't
   be reconciled.

7. **Report.** Print exactly one line starting with `REFINE-RESULT:` —
   `skip (...)` | `converged attempt 1 (KB already sufficient)` |
   `converged attempt N; fixed: <short list, mark style items "style:">` |
   `NOT converged: <why>`.
   Then, ONLY IF you changed KB files, send the owner a short Telegram note (2-4
   sentences: which customer/thread, what the KB got wrong or lacked, what you
   fixed):
   ```
   python3 -c "import sys; sys.path.insert(0,'/home/mercury/DST/telegram'); import tg_api; tg_api.send_message(<OWNER_USER_ID>, '<message>')"
   ```
   If nothing was changed (skip or converged on attempt 1), stay silent — the log
   is enough.

## Guard rails
- Read-only everywhere except `~/DST/knowledge-base/**`.
- Never send email; never create drafts; never touch `~/DST/personal/`.
- the owner's sent reply is ground truth for DST facts. If a reply looks like a typo or
  contradicts the machine manual, do NOT silently overwrite the manual — record the
  conflict in the QA entry and flag it in the Telegram note.
- The semantic index refreshes on the next 15-min cron; no need to rebuild it.

---
