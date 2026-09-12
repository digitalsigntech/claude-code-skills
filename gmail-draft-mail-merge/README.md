# Gmail draft mail-merge

One Gmail **draft** per recipient, each a byte-for-byte clone of a draft you composed in
Gmail — links, bold, bullets, signature — with only the greeting name swapped. Nothing is
sent; a person reviews the Drafts folder and sends from there. Built for trade-show
follow-ups, where the wording is fixed and the formatting has to match the sample exactly.

## Why clone a draft instead of rendering a template?

Gmail's HTML for a formatted message is not something a template engine reproduces
reliably. Composing the sample in Gmail and cloning it guarantees the recipient sees
exactly what you saw. The personalisation is a single substitution: the first
`Hi <name>,` / `Hello` / `Dear` near the top. Everything after it is untouched — we verify
this by diffing a generated draft against the sample.

## Inputs

| Input | Where | Provides |
|---|---|---|
| Sample draft | Gmail → Drafts, found by subject | the formatting master |
| Template | text file (or Google Doc): first line `Subject: …`, body with `[Name]` | subject + plain-text part |
| Leads | CSV (or Google Sheet): `firstname`, `recipient` | recipients; blank rows skipped, malformed and duplicate addresses reported |

## Run

```bash
MAIL_ACCOUNT=primary python3 src/draft_merge.py \
  --template followup.txt --leads leads.csv \
  --example you@example.com --dry-run
```

Once the sample draft has been sent it is no longer in Drafts; pass `--sample-message-id <id>`
to use the sent copy (or any message) as the master instead.

Drop `--dry-run` to create the drafts. `--cc addr` copies an address on every draft
(including the example). `--limit N` creates only the first N.
Drive mode (`--doc "<Doc name>" --sheet "<Sheet name>"`) reads the two inputs from
Google Drive instead of local files; it needs a Drive-scoped token and a `gdrive`
module next to `gmailer.py`.

## Depends on

`gmail-multi-mailbox` (this repo) for OAuth and the Gmail service — the script imports
`gmailer.py` from `../gmail-multi-mailbox/src`. Copy it next to that folder or adjust the
import path. If the mailbox is listed in `NO_SEND_ACCOUNTS`, drafts still work.

## Set up for a new campaign

1. Write the email in Gmail as a draft to yourself, formatted as it should go out.
2. Save the same text as `followup.txt` with `Subject: …` first and `[Name]` in the greeting.
3. Export the leads as CSV with `firstname` and `recipient`.
4. Dry-run, then run. Re-running creates the drafts again — delete the earlier batch first.
