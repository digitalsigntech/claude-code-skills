import os

BASE = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS = os.path.join(BASE, "credentials.json")   # OAuth client (shared) — see credentials.example.json

# Gmail full mailbox scope (https://mail.google.com/) is required for IMAP/IDLE push;
# it is a superset of gmail.modify. Add/remove scopes to match your needs.
SCOPES = [
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/gmail.modify",
]
OAUTH_PORT = 18190

# Connected mailboxes -> address. Each gets its own token file.
# "agent" is the mailbox the watcher listens on; add more accounts as needed.
ACCOUNTS = {
    "agent": "agent@example.com",
    "owner": "owner@example.com",
}
DEFAULT_ACCOUNT = "agent"   # cron + bare invocations use this


def token_path(account=DEFAULT_ACCOUNT):
    # keep the default account at the canonical token.json path
    return os.path.join(BASE, "token.json" if account == DEFAULT_ACCOUNT else f"token_{account}.json")


# Backward-compat aliases (default account)
TOKEN = token_path("agent")
ACCOUNT = ACCOUNTS["agent"]
