import os

BASE = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS = os.path.join(BASE, "credentials.json")   # OAuth client (shared by both accounts)

# Gmail modify scope: read / search / send / draft / labels (everything except
# permanent delete). If you also need permanent-delete or raw IMAP/IDLE push,
# add "https://mail.google.com/" to this list and re-run the login flow.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
]
OAUTH_PORT = 18190

# Connected mailboxes -> address. Each gets its own token file.
# Rename these keys/addresses to your two real mailboxes.
ACCOUNTS = {
    "primary":   "primary@example.com",
    "secondary": "secondary@example.com",
}
DEFAULT_ACCOUNT = "primary"   # bare `mail` / cron use this


def token_path(account=DEFAULT_ACCOUNT):
    # keep the primary account at the plain token.json path; others get a suffix
    return os.path.join(BASE, "token.json" if account == DEFAULT_ACCOUNT else f"token_{account}.json")


# Backward-compat aliases (primary account)
TOKEN = token_path(DEFAULT_ACCOUNT)
ACCOUNT = ACCOUNTS[DEFAULT_ACCOUNT]
