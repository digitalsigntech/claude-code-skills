# Gmail multi-mailbox access

Programmatic read / search / send / draft access to **any number of** Gmail
mailboxes from one small CLI, using the Gmail API over OAuth. Each mailbox has
its own saved token; you pick which one a command targets with an environment
variable. (Born as a two-mailbox tool; the account map takes as many entries as
you need — we run it with three.) By
policy the tool **saves drafts by default** and only sends when explicitly told.

## What it does

- Read, search (full Gmail query syntax), and print single messages or whole threads.
- Compose new mail or threaded replies, either as a **draft** (default, safe) or a **send**.
- Works against any number of independent mailboxes — every key in the
  `ACCOUNTS` map gets its own OAuth token file. No passwords are stored; access is revocable from the
  Google account/admin console at any time.
- **Draft-only accounts**: add an account key to `NO_SEND_ACCOUNTS` in
  `src/config.py` and the CLI will refuse `send` for it at the tool level
  (only `draft` works). Use this when a mailbox owner grants access on the
  condition that nothing is ever sent on their behalf — the promise is then
  enforced in code, not just by convention.

## How it works

| File | Role |
|------|------|
| `src/gmailer.py` | The CLI: `profile`, `list`, `read`, `thread`, `send`, `draft`. |
| `src/auth.py` | One-time OAuth login per account; loads/refreshes the saved token. |
| `src/config.py` | Scopes, OAuth port, the `ACCOUNTS` map, and token file paths. |
| `src/mail` | Bash wrapper that runs the CLI against the **primary** mailbox. |
| `src/mail-secondary` | Same wrapper for the `secondary` account — copy one per extra mailbox (`MAIL_ACCOUNT=<key>` inside). |
| `credentials.example.json` | Template for the Google OAuth Desktop client JSON. |

The CLI selects a mailbox from the `MAIL_ACCOUNT` env var (default `primary`).
`auth.py` maps each account name to an email address (`ACCOUNTS` in `config.py`)
and saves its credentials to a per-account token file (`token.json` for the
primary, `token_<account>.json` for the others).

## Prerequisites

- Python 3.8+.
- Python deps:
  ```bash
  pip install google-api-python-client google-auth google-auth-oauthlib
  ```
  (The wrappers assume a virtualenv at `src/venv`; create one there or edit the
  wrapper to point at your interpreter.)
- A Google Cloud project with the **Gmail API** enabled and a **Desktop** OAuth client.

## Install / setup

### 1. Create the OAuth client (one time, in Google Cloud Console)

1. Go to <https://console.cloud.google.com/> and **create a project**.
2. **Enable the Gmail API:** APIs & Services -> Library -> search "Gmail API" -> **Enable**.
3. **OAuth consent screen:** APIs & Services -> OAuth consent screen.
   - User type: **Internal** if both mailboxes live in your own Google Workspace
     domain (no Google verification needed); otherwise **External** and add each
     mailbox address as a test user.
   - Fill in an app name and support email. Save.
4. **Create credentials:** APIs & Services -> Credentials -> **Create Credentials**
   -> **OAuth client ID** -> Application type: **Desktop app** -> **Create**.
5. **Download the JSON** and save it next to the code as `src/credentials.json`.
   Use `credentials.example.json` as a shape reference — the real file must have
   your actual `client_id` / `client_secret`. The same client is shared by both
   mailboxes.

### 2. Point it at your two mailboxes

Edit `src/config.py` -> `ACCOUNTS` and replace the placeholder addresses:

```python
ACCOUNTS = {
    "primary":   "you@example.com",
    "secondary": "other@example.com",
}
```

### 3. Log in each mailbox once

```bash
cd src
python auth.py primary      # prints a URL; open on THIS machine, sign in as primary, approve
python auth.py secondary    # repeat, signing in as the secondary address
```

Tokens are written to `token.json` / `token_secondary.json` (chmod 600) and are
auto-refreshed on later runs.

## Usage

```bash
# via the wrappers (primary vs secondary mailbox)
./src/mail profile
./src/mail list -q "is:unread" -n 20
./src/mail read <message_id>
./src/mail thread <thread_id>
./src/mail draft --to a@b.com --subject "Hi" --body "..."          # save a draft (default-safe)
./src/mail send  --to a@b.com --subject "Hi" --body "..."          # actually send
./src/mail send  --to a@b.com --reply-to <msg_id> --body "..."     # threaded reply
./src/mail-secondary list -q "newer_than:2d"                       # the other mailbox

# or call the CLI directly and choose the mailbox by env var
MAIL_ACCOUNT=secondary python src/gmailer.py profile
```

Long bodies: use `--body-file /path/to/text.txt` instead of `--body`.
Gmail query examples for `-q`: `is:unread`, `from:bob newer_than:7d`, `subject:invoice`.

## Config

| Where | Knob | Meaning |
|-------|------|---------|
| env | `MAIL_ACCOUNT` | Which mailbox key to use (default `primary`). |
| `config.py` | `ACCOUNTS` | Map of account key -> email address. |
| `config.py` | `SCOPES` | OAuth scopes. Default `gmail.modify` (read/search/send/draft/labels, no permanent delete). Add `https://mail.google.com/` for permanent-delete / raw IMAP. |
| `config.py` | `OAUTH_PORT` | Local port for the login callback. |
| `config.py` | `DEFAULT_ACCOUNT` | Account used when `MAIL_ACCOUNT` is unset. |

Changing scopes requires re-running `auth.py` for each account, because a token
refreshes only with the exact scopes it was originally granted.

## Caveats

- **Bring your own credentials.** No `credentials.json` or token files are shipped
  — only `credentials.example.json` as a shape reference. Create your own OAuth
  client and log in as described above. Keep `credentials.json` and `token*.json`
  private (chmod 600); they are gitignored.
- **Drafts by default.** The `draft` command is the safe default path; `send`
  transmits immediately. Wire your own workflow so review-then-send is the norm.
- **Two mailboxes, two consents.** Each account is a separate OAuth login and a
  separate token file; there is no shared session.
- **Scope trade-off.** `gmail.modify` cannot permanently delete. Switch to the
  full `https://mail.google.com/` scope only if you need that (or raw IMAP/IDLE).
- **Attachment sending is not part of this CLI.** `send`/`draft` handle plain-text
  bodies only. A one-off attachment helper existed in the source project but was
  omitted here as private scratch code.

## HTML email (`--md`)

`send`/`draft` accept `--md`: the body is treated as markdown and sent as
multipart/alternative — an HTML part (rendered via the `markdown` package,
`pip install markdown`; graceful plain fallback if absent) plus a plaintext
part with the markdown markers stripped. Use it for any formatted prose so
recipients never see literal `**` markers.
