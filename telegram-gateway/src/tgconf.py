"""Config for the DST Telegram gateway.

The bot token lives in telegram/bot_token (chmod 600) or env TG_BOT_TOKEN — never
committed/backed up. The allowlist (telegram/allowlist.json) is the set of Telegram
user IDs permitted to talk to the bot; everyone else is ignored. This gateway can
read email, query the KB and run commands on Mercury, so access MUST stay locked.
"""
import os, json

HERE = os.path.dirname(os.path.abspath(__file__))
DST_ROOT = os.path.dirname(HERE)


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
OWNER_ID = int(os.environ.get("TG_OWNER_ID", "0"))
OWNER_NAME = os.environ.get("TG_OWNER_NAME", "Owner")
OWNER_EMAIL = os.environ.get("TG_OWNER_EMAIL", "")            # business mailbox
OWNER_PERSONAL_EMAIL = os.environ.get("TG_OWNER_PERSONAL_EMAIL", "")

# Headless Claude (the "brain") — every message runs a real Claude turn with full
# tools, in the DST workspace, with one persistent session per Telegram chat.
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CLAUDE_WORKDIR = DST_ROOT
CLAUDE_MODEL = os.environ.get("DST_TG_MODEL", "claude-fable-5")
CLAUDE_TIMEOUT = int(os.environ.get("DST_TG_TIMEOUT", "900"))

LONGPOLL = 50          # getUpdates long-poll seconds
TG_MAX = 4000          # message chunk size (Telegram hard limit is 4096)
RICH_MAX = 32768       # Bot API 10.1 sendRichMessage payload cap (rich_message.markdown)
EDIT_INTERVAL = 1.5    # min seconds between live message edits while streaming
STREAMING = False      # False = wait for full reply then send once (the "old way");
                       # True = live-edit a placeholder as Claude generates

# Keep replies snappy: bias Claude toward answering directly instead of reflexively
# exploring the workspace (that exploration is what makes simple messages slow).
APPEND_SYSTEM = (
    "You are Claude replying to the owner of DST over Telegram. Keep answers "
    "concise and conversational — short paragraphs, minimal preamble, no status narration. "
    "Answer directly; use tools ONLY when you actually need a fact, and aim to finish in "
    "1-2 tool calls. When you need a fact, READ the single most relevant file directly — "
    "DST product/pricing facts live in knowledge-base/products/ (e.g. price-list.md, "
    "print-head-doctor.md). Do NOT explore the workspace broadly, and do NOT use the local "
    "docpipe / RAG / media CLIs for quick lookups: they run a slow on-box model and are much "
    "slower than reading the file yourself. Telegram renders only basic markdown "
    "(**bold**, `code`, lists). EXCEPTION — requests for KB photos/images: query the "
    "warm CLIP server (curl 'http://127.0.0.1:8477/find?q=<query>&k=4', ~10ms) and "
    "send hits with local-ai/show_media.py '<query>' --chat <chat_id>; NEVER explore "
    "folders looking for images. "
    "PRIVACY — personal notes: everything under DST/personal/ (files + notes.db) is "
    "the owner's PRIVATE note store. Never quote, summarize, list or send anything from "
    "it except in the owner's own DM (their user-id chat) or a group verified to contain "
    "only him and the bot (telegram/personal_notes.py allowed_chat). In every other "
    "chat — including all DST groups and other users' DMs — behave as if personal/ "
    "does not exist. To send a note use personal_notes.send(chat_id, path), which "
    "enforces the gate itself."
)
# Photo reflex (2026-07-07): image requests answered deterministically from the warm
# CLIP server + cached Telegram file_ids — sub-second, no LLM. DST_PHOTO_REFLEX=0 off.
PHOTO_REFLEX = os.environ.get("DST_PHOTO_REFLEX", "1") == "1"
# Doc reflex (2026-07-10): curated documents (doc_registry.json) sent instantly via
# sendDocument + cached file_ids — no LLM. DST_DOC_REFLEX=0 off.
DOC_REFLEX = os.environ.get("DST_DOC_REFLEX", "1") == "1"
# File reflex (2026-07-10, the owner: "show/fetch/get/give me any file — fast, closest
# match"): generic fetch-verb requests resolved deterministically — registry doc,
# KB image set, or (DM chats only) the closest-matching DST workspace file. Strict
# all-tokens-match; anything ambiguous falls through. DST_FILE_REFLEX=0 off.
FILE_REFLEX = os.environ.get("DST_FILE_REFLEX", "1") == "1"
# Tier-1 reflex: answer product Q&A instantly from the local KB semantic index
# (no LLM round trip), then verify with the full model in the background. See gateway.
KB = os.path.join(DST_ROOT, "email", "kb", "kb")   # `kb ask "<question>" --json`
# OFF by default (the owner, 2026-07-07): Telegram chat is Always Claude again — no Nemotron
# quick answers. Set DST_KB_REFLEX=1 to re-enable.
KB_REFLEX = os.environ.get("DST_KB_REFLEX", "0") == "1"
# Tier-1 quick answer: retrieve a few KB chunks, let a FAST grounded LLM (Nemotron via
# OpenRouter) answer from JUST those snippets or say ESCALATE. Replaces the old score-band
# reflex — cosine score is a good retrieval signal but a bad correctness arbiter (a wrong
# entity-mismatch can out-score a right answer). Small context, ~1-3s, metered off-sub.
KB_PY = os.path.join(DST_ROOT, "email", "venv", "bin", "python")
KB_ANSWER = os.path.join(DST_ROOT, "email", "kb", "kb_answer.py")  # `kb_answer.py "<q>" --json`
PRIVACY_ROUTE = os.path.join(DST_ROOT, "email", "kb", "privacy_route.py")  # strict public/private router
DOCPIPE = os.path.join(DST_ROOT, "local-ai", "docpipe")
MEDIA = os.path.join(DST_ROOT, "local-ai", "media")
# Gate #3: route queries touching PRIVATE info (customer balances, invoices, PII) to the
# on-box model only — never to the cloud Claude turn. Classified locally; fails closed.
# OFF by default (the owner, 2026-07-07): revert Telegram chat to Claude for every message.
# Privacy routing mode (the owner 2026-07-07): "targeted" (mode A) = only queries whose
# INTENT touches private data (balances, invoices+party, PII) route to Nemotron —
# WITH full chat history so it isn't context-blind; everything else goes to Claude
# as normal. "strict" (mode B, shelved — broke chat 2026-07-06) = every message is
# label-routed. "off" = no privacy gate.
PRIVACY_MODE = os.environ.get("DST_PRIVACY_MODE", "targeted")  # off | targeted | strict
PRIVACY_ROUTER = PRIVACY_MODE != "off"
# Chats where EVERY message runs a full Claude turn — the privacy gate and KB reflex
# are skipped, so Nemotron/local models never handle the message. the owner 2026-07-07:
# "Claude DST Public" group. the owner 2026-07-08: "Claude DST Wise" group too — cloud
# LLM only, no masking, private data in cloud replies accepted (emergency-use group).
ALWAYS_CLAUDE_CHATS = set()   # add your group chat ids, e.g. {-100123456789}
# Chats where EVERY message is answered on-box-path by Nemotron (private_turn: full
# chat history + CRM/KB lookup tools + find_files/send_file so it can deliver private
# documents into the chat, the owner 2026-07-08) — the cloud Claude turn is never used,
# even for casual chat. Fails closed. Explicit /cloud is the only escape hatch.
# the owner 2026-07-07: "Claude DST Private" group. NOTE: until the DGX Spark lands,
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
# DST_ROOT/projects/<slug>/ — every post (text/voice/photo/doc) is auto-filed there;
# /wisdom (cloud Claude) vs /privacy (local-policy Nemotron) per chat, mode shown on
# the group title. Module: projects_mode.py (canonical copy in the ../projects skill).
# Additional bindings can be added at runtime via /project <slug> (persisted in
# state/projects.json).
PROJECT_CHATS = {}             # e.g. {-100123456789: "my-project"}
WHISPER_BIN = os.path.expanduser("~/whisper.cpp/build-vulkan/bin/whisper-cli")
WHISPER_MODEL = os.path.expanduser("~/whisper.cpp/models/ggml-large-v3-turbo-q5_0.bin")
PIPER = os.path.join(DST_ROOT, "voice", "venv", "bin", "piper")
PIPER_VOICES = {   # one .onnx per language, keyed by ISO-639-1 (whisper's detection)
    "en": os.path.join(DST_ROOT, "voice", "voices", "en_US-lessac-medium.onnx"),
    "ru": os.path.join(DST_ROOT, "voice", "voices", "ru_RU-irina-medium.onnx"),
    "es": os.path.join(DST_ROOT, "voice", "voices", "es_ES-davefx-medium.onnx"),
    "de": os.path.join(DST_ROOT, "voice", "voices", "de_DE-thorsten-medium.onnx"),
    "fr": os.path.join(DST_ROOT, "voice", "voices", "fr_FR-siwis-medium.onnx"),
    # languages without an installed voice fall back to "en" in voice_mode.synthesize()
}
DOC_EXTS = (".pdf", ".csv", ".tsv", ".txt", ".md")
IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".bmp")
VID_EXTS = (".mp4", ".mov", ".webm", ".m4v")


def allowlist():
    """Reloaded on every check so adding an ID takes effect without a restart."""
    try:
        return set(int(x) for x in json.load(open(ALLOWLIST_FILE)))
    except Exception:
        return set()
