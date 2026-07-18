"""Config for the email knowledge-base / contacts extraction pipeline.

Everything company-specific is an env var or a value you edit here. Nothing in this
file is secret — the LLM API key is loaded separately (see extract.py / README).
"""
import os, re

KB_DIR   = os.path.dirname(os.path.abspath(__file__))            # src/
# Where all outputs live. Override with EKB_DATA_DIR; defaults to ../data next to src/.
DATA_DIR = os.path.abspath(os.environ.get(
    "EKB_DATA_DIR", os.path.join(KB_DIR, os.pardir, "data")))

# LLM chat endpoint hint. Any OpenAI-compatible /chat/completions endpoint works
# (a hosted router, or a local llama.cpp / vLLM / Ollama server). Swappable — see
# extract.py for how the call is made. Kept here only for reference/other tools.
CHAT_URL = os.environ.get("EKB_CHAT_URL", "http://127.0.0.1:8080/v1")

# Outputs
CONTACTS_DB    = os.path.join(DATA_DIR, "contacts.db")
CONTACTS_CSV   = os.path.join(DATA_DIR, "contacts.csv")
PRODUCT_KB     = os.path.join(DATA_DIR, "knowledge-base", "product-knowledge.md")
OPERATIONAL_KB = os.path.join(DATA_DIR, "knowledge-base", "operational-knowledge.md")
LOG_DIR        = os.path.join(KB_DIR, "logs")

# Your own domain/addresses. Product knowledge is harvested from YOUR outbound mail,
# so the pipeline must know which addresses are "us". Edit these to match your org.
OWN_DOMAIN = os.environ.get("EKB_OWN_DOMAIN", "example.com")
INTERNAL_ADDRESSES = {   # never stored as external contacts
    "owner@example.com", "user2@example.com",
    "agent@example.com", "support@example.com",
}

ACCOUNTS_TO_PROCESS = ["owner"]   # which archived mailbox(es) get KB-processed — add as many accounts as you like; cross-mailbox duplicates are dropped by RFC Message-ID (see db.upsert_email)
LIST_SCAN   = 60          # how many recent messages to scan per mailbox per run
BODY_CHARS  = 6000        # truncate email body fed to the model

# Day vs night processing. The pipeline is meant to run on a short cron interval;
# run.sh sets EKB_MODE by hour.
# DAY   = gentle: only RECENT mail, small cap, short time budget.
# NIGHT = catch-up: the whole unprocessed backlog, oldest-first, large cap, long budget.
DAY_MAX, NIGHT_MAX       = 5, 500          # max emails per run
DAY_BUDGET, NIGHT_BUDGET = 240, 780        # seconds per run (each < the cron slot)
RECENT_DAYS = 3                            # "recent" window for daytime processing
MAX_EXTRACT_RETRIES = 3                    # defer a timed-out email this many runs, then give up
MAX_PER_RUN = DAY_MAX                      # fallback when EKB_MODE unset
DOWNLOAD_N  = 0                            # 0 = UNLIMITED archive (never prune)

# Attachments: optional external doc->markdown/index tool. Empty = disabled.
ATTACH_DIR  = os.path.join(DATA_DIR, "attachments")
DOCPIPE     = os.environ.get("EKB_DOCPIPE", "")   # path to an "ingest <file>" CLI, or ""
ATTACH_EXTS = (".pdf", ".csv", ".tsv", ".txt", ".md")

# Local email archive: emails are downloaded HERE first (by a separate downloader,
# not shipped with this skill), then processed from the DB.
CORPUS = os.path.join(DATA_DIR, "email_corpus.jsonl")

# Writing-style learning: for emails SENT BY these people, an extra pass extracts
# their style and refreshes a learned profile. Map address -> short profile name.
WRITERS = {"owner@example.com": "owner", "user2@example.com": "user2"}
WRITING_STYLES_DIR = os.path.join(DATA_DIR, "writing-styles")

# Noise senders: kept in the archive, but NOT extracted into the CRM/knowledge base
# (marketing, social, automated alerts/tracking). Be conservative — do NOT list broad
# domains (amazon.com, google.com) that could be a real customer. Edit to taste.
NOISE_SENDER_DOMAINS = (
    "linkedin.com", "facebookmail.com", "substack.com", "mailchimp.com",
    "reply.github.com", "fedex.com", "ups.com", "accounts.google.com",
)


def is_noise_sender(from_addr):
    dom = from_addr.split("@")[-1].lower().strip(">").strip()
    return any(dom == d or dom.endswith("." + d) or dom.endswith(d) for d in NOISE_SENDER_DOMAINS)


# Internal agent accounts. Any email with one of these as a participant is treated as
# an INTERNAL agent/assistant conversation (not business mail) — archived but NOT
# extracted into the CRM/KB. A real customer email never involves these addresses.
AGENT_ADDRESSES = {"agent@" + OWN_DOMAIN, "peer-agent@" + OWN_DOMAIN}


def is_internal_chat(frm, to, cc):
    """True if any participant is one of the internal agent accounts -> an internal
    assistant conversation, not business mail. Such mail is archived but NOT extracted."""
    from email.utils import getaddresses
    parts = [a.lower() for _, a in getaddresses([frm or "", to or "", cc or ""]) if a and "@" in a]
    return any(a in AGENT_ADDRESSES for a in parts)


# --- Forwarded-email parsing -------------------------------------------------
# When someone on your team forwards a customer's email internally, the message's own
# From: is one of US (looks outbound), but the real value is the ORIGINAL sender + their
# inquiry, sitting as a quoted header block in the body. parse_forwarded() recovers that
# original message so it gets parsed in its own right.
_FWD_SUBJECT = re.compile(r"^\s*(fwd?|fw)\s*:", re.I)
# Markers that introduce a forwarded/original block (Gmail, Apple Mail, Outlook).
_FWD_MARKER = re.compile(
    r"(-{2,}\s*Forwarded message\s*-{2,})|(Begin forwarded message:)|"
    r"(-{2,}\s*Original Message\s*-{2,})", re.I)
_HDR_LINE = re.compile(r"^\s*(From|To|Cc|Subject|Date|Sent)\s*:\s*(.*)$", re.I)


def is_forward(subject, body):
    return bool(_FWD_SUBJECT.match(subject or "")) or bool(_FWD_MARKER.search(body or ""))


# --- Quoted-reply stripping --------------------------------------------------
# Replies top-post new text above the quoted history. We keep only the new text
# (the `body_new` column) for CRM/knowledge extraction — the quoted history is
# redundant across the thread and just makes the model re-extract the same facts.
# The FULL body is still stored (and used for forwarded-original parsing).
_QUOTE_BOUNDARY = re.compile(
    r"^\s*("
    r"On\s.+?wrote:\s*$"                              # Gmail/Apple "On <date>, <x> wrote:"
    r"|-{2,}\s*Original Message\s*-{2,}"              # Outlook
    r"|-{2,}\s*Forwarded message\s*-{2,}"             # Gmail forward
    r"|Begin forwarded message:"                       # Apple forward
    r"|_{10,}"                                          # Outlook underscore divider
    r"|From:\s.+@.+"                                    # Outlook inline reply header
    r")", re.I)
# "On <date>, <name> ... wrote:" folded across up to 3 lines (email addr in between).
# Lead line must look date-like (has a digit) to avoid cutting a real sentence "On Monday...".
_ON_LEAD = re.compile(r"^\s*On\s.*\d.*$", re.I)
_WROTE_TAIL = re.compile(r".*\bwrote:\s*$", re.I)
_FOLD_LOOKAHEAD = 3


def strip_quoted(body):
    """Return only the new (top-posted) text, cutting at the first quote boundary.

    Conservative: if stripping would leave nothing (e.g. a bottom-posted reply or
    a pure quote), returns the original body unchanged rather than lose content.
    """
    body = body or ""
    lines = body.splitlines()
    cut = None
    for i, ln in enumerate(lines):
        if _QUOTE_BOUNDARY.match(ln):
            cut = i; break
        if ln.lstrip().startswith(">"):              # first quoted block
            cut = i; break
        if _ON_LEAD.match(ln) and any(_WROTE_TAIL.match(lines[j])    # folded "On ...\n...\n wrote:"
                                      for j in range(i + 1, min(i + _FOLD_LOOKAHEAD + 1, len(lines)))):
            cut = i; break
    if cut is None:
        return body
    new = "\n".join(lines[:cut]).strip()
    return new if new else body


def parse_forwarded(subject, body):
    """Recover the first forwarded/original message embedded in `body`.

    Returns {orig_from, orig_to, orig_cc, orig_subject, orig_date, orig_body} when a
    forwarded header block is found, else None. Conservative: needs at least an
    original From: with an email address to be worth a second extraction pass.
    """
    body = body or ""
    m = _FWD_MARKER.search(body)
    region = body[m.end():] if m else (body if _FWD_SUBJECT.match(subject or "") else "")
    if not region.strip():
        return None
    lines = region.splitlines()
    fields, body_start, seen, last_key = {}, len(lines), False, None
    for i, ln in enumerate(lines):
        hm = _HDR_LINE.match(ln)
        if hm:
            key = hm.group(1).lower()
            key = "date" if key == "sent" else key
            if key not in fields:                 # keep the FIRST occurrence
                fields[key] = hm.group(2).strip()
            seen, body_start, last_key = True, i + 1, key
        elif seen and ln.strip() == "":
            body_start = i + 1                     # blank line ends the header block
            break
        elif seen and last_key in ("to", "cc", "from") and ("@" in ln or ln.strip().endswith((">", ","))):
            fields[last_key] = (fields.get(last_key, "") + " " + ln.strip()).strip()  # folded header
            body_start = i + 1
        elif seen:
            break                                  # first non-header line after headers
    if "from" not in fields or "@" not in fields.get("from", ""):
        return None
    return {
        "orig_from": fields.get("from", ""), "orig_to": fields.get("to", ""),
        "orig_cc": fields.get("cc", ""), "orig_subject": fields.get("subject", ""),
        "orig_date": fields.get("date", ""),
        "orig_body": "\n".join(lines[body_start:]).strip(),
    }
