"""Configuration + shared helpers for the CRM contact archive.

Paths are derived from this file's location so the tool is portable: by default
the SQLite DB and CSV export live in a sibling `crm/` folder next to the project
root. Point PROJECT_ROOT wherever you like (env var overrides the default).
"""
import os, re

# Project root: env override, else two levels up from this file (…/src/config.py -> project root).
PROJECT_ROOT = os.environ.get(
    "CRM_PROJECT_ROOT",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)

CONTACTS_DB  = os.environ.get("CRM_DB",  os.path.join(PROJECT_ROOT, "crm", "contacts.db"))
CONTACTS_CSV = os.environ.get("CRM_CSV", os.path.join(PROJECT_ROOT, "crm", "contacts.csv"))


# --- Quoted-reply stripping -------------------------------------------------
# Used to store only the sender's NEW text (body_new) so downstream consumers
# don't re-read the whole quoted thread on every reply.

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
