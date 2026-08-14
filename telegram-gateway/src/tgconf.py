"""Config for the Telegram gateway.

EDIT THIS FILE (or set the TG_* env vars) before you start the bot — everything
the gateway says about itself, and every path it reads, comes from here.

The bot token lives in telegram/bot_token (chmod 600) or env TG_BOT_TOKEN — never
committed/backed up. The allowlist (telegram/allowlist.json) is the set of Telegram
user IDs permitted to talk to the bot; everyone else is ignored. This gateway can
read email, query the KB and run commands on the machine it runs on, so access
MUST stay locked.
"""
import os, json, socket

HERE = os.path.dirname(os.path.abspath(__file__))
# Root of the workspace the agent works in — the tree it reads files from and runs
# in. Defaults to the parent of this skill; point TG_WORKSPACE_ROOT at your own.
WORKSPACE_ROOT = os.path.expanduser(
    os.environ.get("TG_WORKSPACE_ROOT") or os.path.dirname(HERE))

# ---- Identity -------------------------------------------------------------
# What the bot calls itself in /help and in anything else it says about itself.
# Nothing here is hard-coded anywhere else in the code: change these and the bot
# introduces itself as YOURS. HOST_LABEL defaults to this machine's hostname.
# Read from the agent profile — agent-profile.json at your workspace root, or
# $AGENT_PROFILE. See agent-profile.example.json. Env still wins over the profile,
# and a missing profile just means generic defaults, never a crash.
import sys as _sys
_sys.path.insert(0, HERE)
try:
    import agentprofile as P
except ImportError:
    class P:                            # noqa: N801 - stand-in, not a class API
        get = staticmethod(lambda k, d=None: d)
        person = staticmethod(lambda r, f=None, d=None: d)
        has = staticmethod(lambda c: False)
        capability = staticmethod(lambda n, f=None, d=None: d)

BOT_NAME = os.environ.get("TG_BOT_NAME") or P.get("agent.name", "Claude")
HOST_LABEL = (os.environ.get("TG_HOST_LABEL") or P.get("host.label")
              or socket.gethostname())
WORKSPACE_LABEL = os.environ.get("TG_WORKSPACE_LABEL") or P.get(
    "workspace.label", "workspace")


def _read(path):
    try:
        return open(path).read().strip()
    except Exception:
        return ""


TOKEN = os.environ.get("TG_BOT_TOKEN") or _read(os.path.join(HERE, "bot_token"))
API = f"https://api.telegram.org/bot{TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{TOKEN}"

STATE_DIR = os.path.join(HERE, "state")
INBOX_DIR = os.path.join(HERE, "inbox")
LOG_DIR = os.path.join(HERE, "logs")
SESSIONS_FILE = os.path.join(STATE_DIR, "sessions.json")
OFFSET_FILE = os.path.join(STATE_DIR, "offset")
ALLOWLIST_FILE = os.path.join(HERE, "allowlist.json")
for _d in (STATE_DIR, INBOX_DIR, LOG_DIR):
    os.makedirs(_d, exist_ok=True)

# Owner identity — set via env (or edit here). OWNER_ID is your numeric Telegram
# user id (== your DM chat id); telegram/tg_whoami.py prints it.
OWNER_ID = int(os.environ.get("TG_OWNER_ID") or P.person("owner", "telegram_id", 0))
OWNER_NAME = os.environ.get("TG_OWNER_NAME") or P.person("owner", "name", "Owner")
OWNER_EMAIL = os.environ.get("TG_OWNER_EMAIL") or P.person("owner", "email", "")
OWNER_PERSONAL_EMAIL = (os.environ.get("TG_OWNER_PERSONAL_EMAIL")
                        or P.person("owner", "personal_email", ""))
# Optional trusted outside collaborator ("friend" tier): their emails run agent
# turns like the owner's, but replies always CC the owner. Empty = feature off.
FRIEND_EMAIL = (os.environ.get("TG_FRIEND_EMAIL")
                or P.person("friend", "email", "")).lower()
FRIEND_NAME = os.environ.get("TG_FRIEND_NAME") or P.person("friend", "name", "Friend")

# Mailbox prefixes the email-injection path routes on: an inbound mail is matched
# against these to decide whose policy applies (see gateway.handle_injected_email).
# Use the local part plus "@" — e.g. "jane@" matches jane@ on any domain you own.
OWNER_MAILBOX = (os.environ.get("TG_OWNER_MAILBOX")
                 or P.person("owner", "mailbox", "") or OWNER_EMAIL)
SECOND_OWNER_MAILBOX = (os.environ.get("TG_SECOND_OWNER_MAILBOX")
                        or P.person("second_owner", "mailbox", ""))
BOT_MAILBOX = os.environ.get("TG_BOT_MAILBOX") or P.get("agent.mailbox", "")
FRIEND_MAILBOX = (os.environ.get("TG_FRIEND_MAILBOX")
                  or P.person("friend", "mailbox", "") or FRIEND_EMAIL)

# Headless Claude (the "brain") — every message runs a real Claude turn with full
# tools, in the workspace, with one persistent session per Telegram chat.
# Resolve to an absolute path: consumers (e.g. a voice-server bridge) may run
# without ~/.local/bin on PATH, where a bare "claude" fails with ENOENT.
import shutil as _shutil
CLAUDE_BIN = (os.environ.get("CLAUDE_BIN") or _shutil.which("claude")
              or os.path.expanduser("~/.local/bin/claude"))
CLAUDE_WORKDIR = WORKSPACE_ROOT
CLAUDE_MODEL = os.environ.get("TG_MODEL", "claude-fable-5")
CLAUDE_TIMEOUT = int(os.environ.get("TG_TIMEOUT", "900"))

LONGPOLL = 50          # getUpdates long-poll seconds
TG_MAX = 4000          # message chunk size (Telegram hard limit is 4096)
RICH_MAX = 32768       # Bot API 10.1 sendRichMessage payload cap (rich_message.markdown)
EDIT_INTERVAL = 1.5    # min seconds between live message edits while streaming
STREAMING = False      # False = wait for full reply then send once (the "old way");
                       # True = live-edit a placeholder as Claude generates

# Keep replies snappy: bias Claude toward answering directly instead of reflexively
# exploring the workspace (that exploration is what makes simple messages slow).
# CUSTOMISE THIS for your own workspace. Everything after the first two sentences
# is an example of the KIND of thing worth putting here: where your facts live, what
# is private, which local tools to prefer. The generic part — be brief, answer
# directly, don't go exploring — is what actually keeps replies fast.
APPEND_SYSTEM = (
    f"You are {BOT_NAME} replying to {OWNER_NAME} over Telegram. Keep answers "
    "concise and conversational — short paragraphs, minimal preamble, no status narration. "
    "Answer directly; use tools ONLY when you actually need a fact, and aim to finish in "
    "1-2 tool calls. When you need a fact, READ the single most relevant file directly "
    "rather than exploring the workspace broadly — that exploration is what makes simple "
    "messages slow. Telegram renders only basic markdown (**bold**, `code`, lists). "
    "PRIVACY — personal notes: everything under personal/ (files + notes.db) is a "
    "PRIVATE note store. Never quote, summarize, list or send anything from it except "
    "in the owner's own DM, or a group verified to contain only them and the bot "
    "(personal_notes.py allowed_chat). In every other chat — including group chats and "
    "other users' DMs — behave as if personal/ does not exist. To send a note use "
    "personal_notes.send(chat_id, path), which enforces the gate itself."
)
# Photo reflex (2026-07-07): image requests answered deterministically from the warm
# CLIP server + cached Telegram file_ids — sub-second, no LLM. TG_PHOTO_REFLEX=0 off.
PHOTO_REFLEX = (os.environ.get("TG_PHOTO_REFLEX", "1") == "1"
                and P.has("image_search"))   # needs a CLIP image server
# Doc reflex (2026-07-10): curated documents (doc_registry.json) sent instantly via
# sendDocument + cached file_ids — no LLM. TG_DOC_REFLEX=0 off.
DOC_REFLEX = os.environ.get("TG_DOC_REFLEX", "1") == "1"
# File reflex (2026-07-10, the owner: "show/fetch/get/give me any file — fast, closest
# match"): generic fetch-verb requests resolved deterministically — registry doc,
# KB image set, or (DM chats only) the closest-matching workspace file. Strict
# all-tokens-match; anything ambiguous falls through. TG_FILE_REFLEX=0 off.
FILE_REFLEX = os.environ.get("TG_FILE_REFLEX", "1") == "1"
# QR reflex: the owner asking for a login QR ("make me a qr for the app") runs
# TG_QR_SCRIPT directly (called with `--chat <chat_id>`) — no LLM. Off unless
# TG_QR_SCRIPT is set. TG_QR_CHATS = comma-separated chat ids where it may fire
# (keep to owner-only chats — the QR usually carries a live credential).
QR_SCRIPT = os.environ.get("TG_QR_SCRIPT", "")
QR_CHATS = {int(x) for x in os.environ.get("TG_QR_CHATS", "").split(",") if x.strip()}
QR_REFLEX = bool(QR_SCRIPT) and os.environ.get("TG_QR_REFLEX", "1") == "1"
# Tier-1 reflex: answer product Q&A instantly from the local KB semantic index
# (no LLM round trip), then verify with the full model in the background. See gateway.
KB = os.path.join(WORKSPACE_ROOT, "email", "kb", "kb")   # `kb ask "<question>" --json`
# OFF by default (the owner, 2026-07-07): Telegram chat is Always Claude again — no Nemotron
# quick answers. Set TG_KB_REFLEX=1 to re-enable.
KB_REFLEX = (os.environ.get("TG_KB_REFLEX", "0") == "1"
             and P.has("kb_index"))
# Tier-1 quick answer: retrieve a few KB chunks, let a FAST grounded LLM (Nemotron via
# OpenRouter) answer from JUST those snippets or say ESCALATE. Replaces the old score-band
# reflex — cosine score is a good retrieval signal but a bad correctness arbiter (a wrong
# entity-mismatch can out-score a right answer). Small context, ~1-3s, metered off-sub.
KB_PY = os.path.join(WORKSPACE_ROOT, "email", "venv", "bin", "python")
KB_ANSWER = os.path.join(WORKSPACE_ROOT, "email", "kb", "kb_answer.py")  # `kb_answer.py "<q>" --json`
PRIVACY_ROUTE = os.path.join(WORKSPACE_ROOT, "email", "kb", "privacy_route.py")  # strict public/private router
DOCPIPE = os.path.join(WORKSPACE_ROOT, "local-ai", "docpipe")
MEDIA = os.path.join(WORKSPACE_ROOT, "local-ai", "media")
# Gate #3: route queries touching PRIVATE info (customer balances, invoices, PII) to the
# on-box model only — never to the cloud Claude turn. Classified locally; fails closed.
# OFF by default (the owner, 2026-07-07): revert Telegram chat to Claude for every message.
# Privacy routing mode (the owner 2026-07-07): "targeted" (mode A) = only queries whose
# INTENT touches private data (balances, invoices+party, PII) route to Nemotron —
# WITH full chat history so it isn't context-blind; everything else goes to Claude
# as normal. "strict" (mode B, shelved — broke chat 2026-07-06) = every message is
# label-routed. "off" = no privacy gate.
# A router that sends private questions to a local model FAILS CLOSED, so on a
# machine without that model it breaks exactly the questions asked most often.
PRIVACY_MODE = (os.environ.get("TG_PRIVACY_MODE", "targeted")
                if P.has("private_model") else "off")  # off | targeted | strict
PRIVACY_ROUTER = PRIVACY_MODE != "off"
# Chats where EVERY message runs a full Claude turn — the privacy gate and KB reflex
# are skipped, so Nemotron/local models never handle the message. the owner 2026-07-07:
# e.g. a public group. Another use: a "wise" group — cloud
# LLM only, no masking, private data in cloud replies accepted (emergency-use group).
ALWAYS_CLAUDE_CHATS = set()   # add your group chat ids, e.g. {-100123456789}
# Chats where EVERY message is answered on-box-path by Nemotron (private_turn: full
# chat history + CRM/KB lookup tools + find_files/send_file so it can deliver private
# documents into the chat, the owner 2026-07-08) — the cloud Claude turn is never used,
# even for casual chat. Fails closed. Explicit /cloud is the only escape hatch.
# e.g. a private group. NOTE: on the original deployment
# Nemotron itself runs on OpenRouter (cloud inference) — the owner accepted this.
ALWAYS_NEMOTRON_CHATS = set()  # add your group chat ids
# Voice conversation mode (2026-07-13): a voice note in one of these chats is
# transcribed on-box (whisper.cpp, language autodetected), answered with a normal
# Claude turn, and the reply comes back as a Piper-synthesized voice note plus the
# full text. Other chats keep the existing file handling (e.g. a caption-less voice
# note in the owner's DM stays a personal note). Requires whisper.cpp built locally
# (a Vulkan build uses the iGPU; a plain CPU build works too) and Piper TTS in a
# venv with one .onnx voice per language.
VOICE_CHATS = set()            # add your voice-conversation group chat ids
# Project chats (the owner, 2026-07-19): a group bound to a project directory under
# WORKSPACE_ROOT/projects/<slug>/ — every post (text/voice/photo/doc) is auto-filed there;
# /wisdom (cloud Claude) vs /privacy (local-policy Nemotron) per chat, mode shown on
# the group title. Module: projects_mode.py (canonical copy in the ../projects skill).
# Additional bindings can be added at runtime via /project <slug> (persisted in
# state/projects.json).
PROJECT_CHATS = {}             # e.g. {-100123456789: "my-project"}
WHISPER_BIN = os.path.expanduser("~/whisper.cpp/build-vulkan/bin/whisper-cli")
WHISPER_MODEL = os.path.expanduser("~/whisper.cpp/models/ggml-large-v3-turbo-q5_0.bin")
PIPER = os.path.join(WORKSPACE_ROOT, "voice", "venv", "bin", "piper")
PIPER_VOICES = {   # one .onnx per language, keyed by ISO-639-1 (whisper's detection)
    "en": os.path.join(WORKSPACE_ROOT, "voice", "voices", "en_US-lessac-medium.onnx"),
    "ru": os.path.join(WORKSPACE_ROOT, "voice", "voices", "ru_RU-irina-medium.onnx"),
    "es": os.path.join(WORKSPACE_ROOT, "voice", "voices", "es_ES-davefx-medium.onnx"),
    "de": os.path.join(WORKSPACE_ROOT, "voice", "voices", "de_DE-thorsten-medium.onnx"),
    "fr": os.path.join(WORKSPACE_ROOT, "voice", "voices", "fr_FR-siwis-medium.onnx"),
    # languages without an installed voice fall back to "en" in voice_mode.synthesize()
}
# Directories the file reflex never indexes, on top of its own built-in list.
# Deployment-specific: raw mail stores are often named after their mailbox.
FILE_REFLEX_EXCLUDE_DIRS = set(
    P.get("workspace.exclude_dirs")
    or ["mail", "telegram", "chatlog", "personal"])

DOC_EXTS = (".pdf", ".csv", ".tsv", ".txt", ".md")
IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".bmp")
VID_EXTS = (".mp4", ".mov", ".webm", ".m4v")


def allowlist():
    """Reloaded on every check so adding an ID takes effect without a restart."""
    try:
        return set(int(x) for x in json.load(open(ALLOWLIST_FILE)))
    except Exception:
        return set()

# Reminder ownership (per-user reminders). Each reminder belongs to one of
# these keys; "my reminders" lists the asker's own and "our reminders" the
# shared ones, never merged. Rename them for your own install.
PRIMARY_OWNER_KEY = os.environ.get("TG_PRIMARY_OWNER_KEY", "owner")
SECOND_OWNER_KEY = os.environ.get("TG_SECOND_OWNER_KEY", "second")
REMINDER_OWNERS = (PRIMARY_OWNER_KEY, SECOND_OWNER_KEY, "shared")
SECOND_OWNER_ID = int(os.environ.get("TG_SECOND_OWNER_ID", "0"))
SECOND_OWNER_EMAIL = os.environ.get("TG_SECOND_OWNER_EMAIL", "")
