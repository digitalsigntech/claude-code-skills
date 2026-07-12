#!/usr/bin/env python3
"""One-time OAuth login for a Google account (account-aware).

Usage: python auth.py [account]   (account = primary | secondary; default primary)
Prints a Google sign-in URL; open it on this machine's browser, sign in as the
matching address, approve, and a token is saved. Re-running refreshes the token.
"""
import os, sys
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from config import CREDENTIALS, SCOPES, OAUTH_PORT, ACCOUNTS, DEFAULT_ACCOUNT, token_path


def get_credentials(account=DEFAULT_ACCOUNT, interactive=True):
    tok = token_path(account)
    creds = None
    if os.path.exists(tok):
        # load with the token's OWN granted scopes — passing a different scope set
        # than the one originally consented to breaks refresh with invalid_scope.
        creds = Credentials.from_authorized_user_file(tok)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save(creds, tok)
        return creds
    if not interactive:
        return None
    if not os.path.exists(CREDENTIALS):
        sys.exit(f"Missing {CREDENTIALS} — see README (create a Desktop OAuth client).")
    addr = ACCOUNTS.get(account, account)
    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS, SCOPES)
    creds = flow.run_local_server(
        port=OAUTH_PORT, open_browser=False, prompt="consent", access_type="offline",
        login_hint=addr,
        authorization_prompt_message="\n>>> Open this URL on THIS machine's browser, "
        f"sign in as {addr}, and approve:\n\n{{url}}\n",
        success_message="Done — you can close this tab.")
    _save(creds, tok)
    return creds


def _save(creds, tok):
    with open(tok, "w") as f:
        f.write(creds.to_json())
    os.chmod(tok, 0o600)


if __name__ == "__main__":
    acct = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ACCOUNT
    c = get_credentials(acct, interactive=True)
    print(f"\nAuthorized  token saved ({acct})" if c else "Not authorized.")
