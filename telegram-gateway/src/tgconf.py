"""Config for the Telegram gateway.

The bot token lives in telegram/bot_token (chmod 600) or env TG_BOT_TOKEN — never
committed/backed up. The allowlist (telegram/allowlist.json) is the set of Telegram
user IDs permitted to talk to the bot; everyone else is ignored. This gateway can
read email, query the KB and run commands on the box, so access MUST stay locked.
"""
import tgconf as C   # identity from config
import os, json

HERE = os.path.dirname(os.path.abspath(__file__))
WORKSPACE_ROOT = os.path.dirname(HERE)
WORKSPACE_ROOT = WORKSPACE_ROOT   # role name; WORKSPACE_ROOT kept for existing callers


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

# Headless Claude (the "brain") — every message runs a real Claude turn with full
# tools, in the workspace, with one persistent session per Telegram chat.
# Resolve to an absolute path: consumers (e.g. the voice server) may run without
# ~/.local/bin on PATH, where a bare "claude" fails with ENOENT.
import shutil as _shutil
CLAUDE_BIN = (os.environ.get("CLAUDE_BIN") or _shutil.which("claude")
              or os.path.expanduser("~/.local/bin/claude"))
CLAUDE_WORKDIR = WORKSPACE_ROOT
# Fable 5.1 since 2026-09-04 (the owner: "Switch to fable 5.1"). It needed the
# Claude Code upgrade to 2.1.260 first — the model id did not resolve on
# 2.1.226. Was Opus 5 from 2026-08-01, which itself replaced Fable 5 when
# that hit its usage limit. Override per-run with TG_TG_MODEL.
CLAUDE_MODEL = os.environ.get("TG_TG_MODEL", "claude-fable-5-1")
CLAUDE_TIMEOUT = int(os.environ.get("TG_TG_TIMEOUT", "900"))

# ---- Identity -------------------------------------------------------------
# Everything the bot says about ITSELF is built from these, never written into a
# string literal in the code. the owner, 2026-08-13: a fresh install of the public
# skill greeted its new owner with "I'm Claude, running on the box (your the workspace
# appliance)" — our branding, hard-coded into the greeting and shipped verbatim.
# The export ships the same code with generic defaults, so the only way a name
# leaks now is by putting it back in a literal. Don't.
# Values come from the agent profile (agent-profile.json at the workspace root),
# which is the ONE place this deployment is described. TG_* env still wins, so an
# operator can override a single field without editing the profile.
import sys as _sys
_sys.path.insert(0, os.path.join(WORKSPACE_ROOT, "lib"))
try:
    import agentprofile as P
except ImportError:                     # profile lib absent = generic defaults
    class P:                            # noqa: N801 - a stand-in, not a class API
        get = staticmethod(lambda k, d=None: d)
        person = staticmethod(lambda r, f=None, d=None: d)
        has = staticmethod(lambda c: False)
        capability = staticmethod(lambda n, f=None, d=None: d)

BOT_NAME = os.environ.get("TG_BOT_NAME") or P.get("agent.name", "Claude")
HOST_LABEL = os.environ.get("TG_HOST_LABEL") or P.get("host.label", "this machine")
WORKSPACE_LABEL = os.environ.get("TG_WORKSPACE_LABEL") or P.get(
    "workspace.label", "workspace")

# Mailbox prefixes the email-injection path routes on (gateway.handle_injected_email
# matches these against the From: address, so they are BRANCHES, not prose — that is
# why they have to be config and not literals for the export to be generatable).
OWNER_MAILBOX = os.environ.get("TG_OWNER_MAILBOX") or P.person(
    "owner", "mailbox", "")
SECOND_OWNER_MAILBOX = os.environ.get("TG_SECOND_OWNER_MAILBOX") or P.person(
    "second_owner", "mailbox", "")
BOT_MAILBOX = os.environ.get("TG_BOT_MAILBOX") or P.get("agent.mailbox", "")
FRIEND_MAILBOX = os.environ.get("TG_FRIEND_MAILBOX") or P.person(
    "friend", "mailbox", "")
# Where the shared secrets env file lives (OPENROUTER_API_KEY etc.). A path under
# our config dir is still OUR path — config, not a literal.
SECRETS_ENV = os.path.expanduser(
    os.environ.get("TG_SECRETS_ENV")
    or P.capability("secrets_env", "path", "~/.config/agent/secrets.env"))
OWNER_NAME = os.environ.get("TG_OWNER_NAME") or P.person("owner", "name", "the owner")
FRIEND_NAME = os.environ.get("TG_FRIEND_NAME") or P.person("friend", "name", "")
OWNER_EMAIL = os.environ.get("TG_OWNER_EMAIL") or P.person("owner", "email", "")
OWNER_PERSONAL_EMAIL = (os.environ.get("TG_OWNER_PERSONAL_EMAIL")
                        or P.person("owner", "personal_email", ""))
OWNER_ID = int(os.environ.get("TG_OWNER_ID") or P.person("owner", "telegram_id", 0))

LONGPOLL = 50          # getUpdates long-poll seconds
TG_MAX = 4000          # message chunk size (Telegram hard limit is 4096)
RICH_MAX = 32768       # Bot API 10.1 sendRichMessage payload cap (rich_message.markdown)
EDIT_INTERVAL = 1.5    # min seconds between live message edits while streaming
STREAMING = False      # False = wait for full reply then send once (the "old way");
                       # True = live-edit a placeholder as Claude generates

# Keep replies snappy: bias Claude toward answering directly instead of reflexively
# exploring the workspace (that exploration is what makes simple messages slow).
def _system_prompt():
    """The per-turn system prompt is deployment CONTENT, not code.

    It says where this company's facts live, what needs whose approval, what is
    private. Max's copy of this file had to be rewritten by hand at install time
    for exactly that reason — so it lives in a file the profile names, and the
    gateway just loads it. Falls back to a generic instruction if there is none.
    """
    path = P.get("agent.system_prompt_file", "agent-system-prompt.md")
    for base in (WORKSPACE_ROOT, HERE):
        try:
            return open(os.path.join(base, path)).read().strip()
        except OSError:
            continue
    return (f"You are {BOT_NAME} replying to {OWNER_NAME} over Telegram. Keep "
            "answers concise and conversational — short paragraphs, minimal "
            "preamble, no status narration. Answer directly; use tools ONLY when "
            "you actually need a fact. Read the single most relevant file rather "
            "than exploring the workspace. Telegram renders only basic markdown "
            "(**bold**, `code`, lists).")


# The reminders queue's own name for the primary owner. It used to be a string
# literal inside a SQL fragment, and the scrub that generalises this deployment
# for publication turned that literal into a config REFERENCE inside the query
# text — where SQLite reads it as a column name. The published copy broke on a
# second install; ours worked, which is exactly why nobody saw it.
#
# Same name here as in the export, so the line is now identical in both and the
# scrub has nothing left to rewrite.
PRIMARY_OWNER_KEY = os.environ.get("TG_PRIMARY_OWNER_KEY", C.PRIMARY_OWNER_KEY)

APPEND_SYSTEM = _system_prompt()
# Photo reflex (2026-07-07): image requests answered deterministically from the warm
# CLIP server + cached Telegram file_ids — sub-second, no LLM. TG_PHOTO_REFLEX=0 off.
PHOTO_REFLEX = (os.environ.get("TG_PHOTO_REFLEX", "1") == "1"
                and P.has("image_search"))   # needs a CLIP server; Max has none
# Doc reflex (2026-07-10): curated documents (doc_registry.json) sent instantly via
# sendDocument + cached file_ids — no LLM. TG_DOC_REFLEX=0 off.
DOC_REFLEX = os.environ.get("TG_DOC_REFLEX", "1") == "1"
# File reflex (2026-07-10, the owner: "show/fetch/get/give me any file — fast, closest
# match"): generic fetch-verb requests resolved deterministically — registry doc,
# KB image set, or (DM chats only) the closest-matching workspace file. Strict
# all-tokens-match; anything ambiguous falls through. TG_FILE_REFLEX=0 off.
FILE_REFLEX = os.environ.get("TG_FILE_REFLEX", "1") == "1"
# Reflex audit (2026-08-16, the owner): after a CLIP/media reflex answers a "show me"
# request, the same prompt goes to the main LLM on a background thread; anything it
# finds that the reflex did not send is posted as a supplement. The fast answer is
# never delayed, and a complete answer says nothing. TG_REFLEX_AUDIT=0 off.
REFLEX_AUDIT = os.environ.get("TG_REFLEX_AUDIT", "1") == "1"
# QR reflex (2026-07-31): the owner asking for an Agent Voice Mode login QR mints
# and sends one deterministically via voice/hosted/make_account_qr.py — no LLM.
# Owner-only + private-chat-only (see qr_reflex.py). TG_QR_REFLEX=0 off.
QR_REFLEX = os.environ.get("TG_QR_REFLEX", "1") == "1"

# Tasks reflex (2026-08-07): "show me the currently running tasks" answered from
# the task registry via the voice server hook, no LLM turn. TG_TASKS_REFLEX=0 off.
TASKS_REFLEX = os.environ.get("TG_TASKS_REFLEX", "1") == "1"
# Backup-status reflex (2026-08-07): file checks, no LLM turn. TG_BACKUP_REFLEX=0 off.
BACKUP_REFLEX = os.environ.get("TG_BACKUP_REFLEX", "1") == "1"
# Reminders reflex (2026-08-07): the pending list as a table, no LLM turn.
REMINDERS_REFLEX = os.environ.get("TG_REMINDERS_REFLEX", "1") == "1"
# KB filing reflex (2026-08-07): file a scan now, index behind the answer.
KB_FILE_REFLEX = os.environ.get("TG_KB_FILE_REFLEX", "1") == "1"
# Personal-note reflex (2026-08-07): save now, label behind the answer.
PERSONAL_NOTE_REFLEX = os.environ.get("TG_PERSONAL_NOTE_REFLEX", "1") == "1"
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
# It cannot be on unless the capability is present. (Max, 2026-08-13.)
PRIVACY_MODE = (os.environ.get("TG_PRIVACY_MODE", "targeted")
                if P.has("private_model") else "off")  # off | targeted | strict
PRIVACY_ROUTER = PRIVACY_MODE != "off"
# Chats where EVERY message runs a full Claude turn — the privacy gate and KB reflex
# are skipped, so Nemotron/local models never handle the message. the owner 2026-07-07:
# "Public" group. the owner 2026-07-08: "Wise" group too — cloud
# LLM only, no masking, private data in cloud replies accepted (emergency-use group).
# the owner 2026-07-13: "Investing with Claude" group — stay on cloud Claude, no Nemotron.
# the owner 2026-07-18: "Space Rigs Website with Claude" group — same treatment.
# the owner 2026-07-30: "Voice Bridge iOS App" group (the app itself is named Agent Voice Mode) — main cloud LLM only, never Nemotron.
# the owner 2026-07-31: "Voice Mode Dashboard" group — cloud LLM only.
# the owner 2026-08-05: "User Feedback" group — cloud LLM only, never Nemotron. It is
# where users' reports land and where replies to them are typed; that traffic is
# product work, and it must be handled by the model that can act on it.
ALWAYS_CLAUDE_CHATS = {C.EXAMPLE_CHAT_ID, C.EXAMPLE_CHAT_ID, C.EXAMPLE_CHAT_ID, C.EXAMPLE_CHAT_ID,
                       C.EXAMPLE_CHAT_ID, C.EXAMPLE_CHAT_ID, C.EXAMPLE_CHAT_ID}
# Chats where EVERY message is answered on-box-path by Nemotron (private_turn: full
# chat history + CRM/KB lookup tools + find_files/send_file so it can deliver private
# documents into the chat, the owner 2026-07-08) — the cloud Claude turn is never used,
# even for casual chat. Fails closed. Explicit /cloud is the only escape hatch.
# the owner 2026-07-07: "Private" group. NOTE: until the DGX Spark lands,
# Nemotron itself runs on OpenRouter (cloud inference) — the owner accepted this.
ALWAYS_NEMOTRON_CHATS = {C.EXAMPLE_CHAT_ID}
# Voice conversation mode (2026-07-13, the owner: "Voice Claude" group): a voice note in
# one of these chats is transcribed on-box (whisper.cpp large-v3-turbo on the iGPU,
# language autodetected), answered with a normal Claude turn, and the reply comes back
# as a Piper-synthesized voice note plus the full text. Other chats keep the existing
# file handling (e.g. a caption-less voice note in the owner's DM stays a personal note).
VOICE_CHATS = {C.EXAMPLE_CHAT_ID}
# Project chats (the owner 2026-07-19, "PHD R&D with Claude" group): a group bound to a
# project directory under workspace/projects/<slug>/ — every post (text/voice/photo/doc)
# is auto-filed there; /wisdom (cloud Claude) vs /privacy (Nemotron) per chat, mode
# shown on the group title. See projects_mode.py. Additional bindings can be added
# at runtime via /project <slug> (persisted in state/projects.json).
PROJECT_CHATS = {C.EXAMPLE_CHAT_ID: "phd-rd"}
WHISPER_BIN = os.path.expanduser("~/whisper.cpp/build-vulkan/bin/whisper-cli")
WHISPER_MODEL = os.path.expanduser("~/whisper.cpp/models/ggml-large-v3-turbo-q5_0.bin")
PIPER = os.path.join(WORKSPACE_ROOT, "voice", "venv", "bin", "piper")
PIPER_VOICES = {
    "en": os.path.join(WORKSPACE_ROOT, "voice", "voices", "en_US-lessac-medium.onnx"),
    "ru": os.path.join(WORKSPACE_ROOT, "voice", "voices", "ru_RU-irina-medium.onnx"),
    "es": os.path.join(WORKSPACE_ROOT, "voice", "voices", "es_ES-davefx-medium.onnx"),
    "de": os.path.join(WORKSPACE_ROOT, "voice", "voices", "de_DE-thorsten-medium.onnx"),
    "fr": os.path.join(WORKSPACE_ROOT, "voice", "voices", "fr_FR-siwis-medium.onnx"),
    # no "ja": Piper has no Japanese voice (checked 2026-07) — ja replies fall
    # back to the en voice per voice_mode.synthesize().
}
# Directories the file reflex never indexes. Deployment-specific: raw mail stores
# are named after their mailbox here, and one of those names is a person.
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
