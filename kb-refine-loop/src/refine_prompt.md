# KB self-refinement loop (one customer thread)

You are running headless on the box. Work SILENTLY (no emails, no drafts, no files
outside `~/the company/knowledge-base/`). Your job: test whether the the company knowledge base could
have answered a customer's questions as well as the owner's actual reply did — and if not,
fix the KB until it can. This is the same loop the owner approved on 2026-07-12
(Color Lab / soak-cycle incident).

Inputs (appended below): THREAD_ID and REPLY_ID — a Gmail thread in the local email
archive and the id of the owner's sent reply to test against.

## Procedure

1. **Load the thread** (oldest first):
   `sqlite3 ~/the company/crm/contacts.db "SELECT id, date, from_addr, subject, body_new FROM emails WHERE thread_id='<THREAD_ID>' ORDER BY internal_date;"`
   Read everything EXCEPT the body of REPLY_ID. Do NOT read REPLY_ID's body yet —
   that is the answer key. (Earlier replies from the owner in the thread are fine to read.)

2. **Extract the customer's question(s)** that REPLY_ID is answering (the latest
   inbound message before it). If there is no factual/technical/product question —
   pure scheduling, thanks, shipping notice, price-list request, etc. — print
   `REFINE-RESULT: skip (no factual question)` and stop.

3. **Attempt N (start N=1): answer from the KB only.** Use `~/the company/knowledge-base/`
   (product-qa.md, manuals, phd-recovery-fluids.md, price-list.md, from-emails/,
   `ug -in` searches). Write a complete draft answer, fact by fact. Do not guess:
   if the KB doesn't cover something, say so in the draft.

4. **Now read REPLY_ID's body** (the same sqlite query, `WHERE id='<REPLY_ID>'`).
   Compare your attempt to the owner's reply fact-by-fact. Classify each of the owner's facts:
   MATCH / MISSING from KB / CONFLICT (KB says otherwise or holds contradictory
   entries) / EXTRA (the owner omitted it; not an error).

5. **Patch the KB** for every MISSING and CONFLICT:
   - New facts → append Q&A pairs to `knowledge-base/products/product-qa.md`
     (product) or `knowledge-base/company/company-qa.md` (company), Q&A format.
   - Conflicts → CONSOLIDATE into one canonical entry (the owner's sent reply wins;
     note superseded values with date + source, e.g. "per the owner's reply to X,
     YYYY-MM-DD"). Never leave two QA pairs answering the same question differently.
   - Keep edits surgical; don't rewrite unrelated entries.

6. **Re-run the attempt (N+1) from the patched KB.** If every fact now matches,
   you've converged. Max 3 attempts; if still diverging, record what couldn't be
   reconciled.

7. **Report.** Print exactly one line starting with `REFINE-RESULT:` —
   `skip (...)` | `converged attempt 1 (KB already sufficient)` |
   `converged attempt N; fixed: <short list>` | `NOT converged: <why>`.
   Then, ONLY IF you changed KB files, send the owner a short Telegram note (2-4
   sentences: which customer/thread, what the KB got wrong or lacked, what you
   fixed):
   ```
   python3 -c "import sys; sys.path.insert(0,'$HOME/the company/telegram'); import tg_api; tg_api.send_message(OWNER_CHAT_ID, '<message>')"
   ```
   If nothing was changed (skip or converged on attempt 1), stay silent — the log
   is enough.

## Guard rails
- Read-only everywhere except `~/the company/knowledge-base/**`.
- Never send email; never create drafts; never touch `~/the company/personal/`.
- the owner's sent reply is ground truth for the company facts. If a reply looks like a typo or
  contradicts the machine manual, do NOT silently overwrite the manual — record the
  conflict in the QA entry and flag it in the Telegram note.
- The semantic index refreshes on the next 15-min cron; no need to rebuild it.

---
