import os

BASE = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS = os.path.join(BASE, "credentials.json")   # OAuth client (shared by all accounts)

# Scopes. This tool uploads/prunes files in a Drive folder, so it needs a Drive
# scope. If you already run the Gmail tooling, add the Drive scope to that token
# and re-run the login flow (a token consented to gmail.modify only cannot write
# to Drive). "drive.file" limits the app to files it created — enough for backups.
# Use the broader "drive" scope only if you also want to read/manage other files.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive.file",
]
OAUTH_PORT = 18190

# Connected accounts -> address. Each gets its own token file.
# Rename these keys/addresses to your real account(s).
ACCOUNTS = {
    "primary":   "primary@example.com",
    "secondary": "secondary@example.com",
}
DEFAULT_ACCOUNT = "primary"   # bare tools / cron use this


def token_path(account=DEFAULT_ACCOUNT):
    # keep the primary account at the plain token.json path; others get a suffix
    return os.path.join(BASE, "token.json" if account == DEFAULT_ACCOUNT else f"token_{account}.json")


# Backward-compat aliases (primary account)
TOKEN = token_path(DEFAULT_ACCOUNT)
ACCOUNT = ACCOUNTS[DEFAULT_ACCOUNT]
