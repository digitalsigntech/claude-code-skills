import os

BASE = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS = os.path.join(BASE, "credentials.json")   # OAuth client — see credentials.example.json

# Full mailbox scope is required for IMAP IDLE push; gmail.modify covers API send.
SCOPES = [
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/gmail.modify",
]
OAUTH_PORT = 18191

# The mailbox the agent lives in (the bot's own address).
ACCOUNTS = {
    "agent": os.environ.get("GCA_AGENT_EMAIL", "agent@example.com"),
}
DEFAULT_ACCOUNT = "agent"

# ---- security: who is allowed to command the agent -------------------------
# ONLY mail whose From address is in this list AND passes Gmail's own
# DKIM/SPF authentication is treated as a chat turn and replied to.
# Everything else is ignored (never replied to) and reported to Telegram.
OWNER_EMAILS = [a.strip().lower() for a in os.environ.get(
    "GCA_OWNERS", "owner@example.com").split(",") if a.strip()]

# If True, an owner mail with no aligned DKIM pass is still accepted when SPF
# passes for the owner's domain. Keep False unless the owner's provider is
# known not to sign with DKIM (rare — Gmail/Workspace always sign).
ALLOW_SPF_ONLY = os.environ.get("GCA_ALLOW_SPF_ONLY", "0") == "1"

# ---- the agent turn ---------------------------------------------------------
# Command that turns an email into a reply: prompt on stdin, reply on stdout.
# Default is Claude Code headless mode. Swap for any agent CLI.
AGENT_CMD = os.environ.get("GCA_AGENT_CMD", "claude -p")
AGENT_CWD = os.environ.get("GCA_AGENT_CWD", "")        # working dir for the agent ("" = here)
AGENT_TIMEOUT = int(os.environ.get("GCA_AGENT_TIMEOUT", "600"))   # seconds

BODY_MAX = int(os.environ.get("GCA_BODY_MAX", "12000"))           # cap body fed to the agent
MAX_TURNS_PER_HOUR = int(os.environ.get("GCA_MAX_TURNS_PER_HOUR", "20"))  # runaway-loop brake

# ---- telegram reporting (optional) ------------------------------------------
# Put the bot token in src/bot_token and the chat id in src/report_chat
# (or set the env vars). If neither exists, events are only logged.
BOT_TOKEN_FILE = os.path.join(BASE, "bot_token")
REPORT_CHAT_FILE = os.path.join(BASE, "report_chat")
BOT_TOKEN_ENV = "GCA_BOT_TOKEN"
REPORT_CHAT_ENV = "GCA_REPORT_CHAT"


def token_path(account=DEFAULT_ACCOUNT):
    return os.path.join(BASE, "token.json" if account == DEFAULT_ACCOUNT else f"token_{account}.json")


# Backward-compat aliases used by auth.py
TOKEN = token_path("agent")
ACCOUNT = ACCOUNTS["agent"]
