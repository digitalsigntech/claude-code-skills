#!/bin/bash
# PII scrub gate (2026-07-14, after the kb-refine cron agent pushed unscrubbed
# live files): block ANY push whose tree contains owner identifiers. See
# DST/telegram/skill-export-scrub.md for the conventions.
cd "$(git rev-parse --show-toplevel)" || exit 1
# 2026-08-13: branding added. A fresh install greeted its owner as "I'm Claude,
# running on Mercury (your DST appliance)" — company and host names are identifiers
# too. -I skips binaries outright (a stale .pyc used to slip through as noise).
# 2026-08-14, widened on the owner's instruction: "Nothing in our skills can have
# any of our private information: our names, company names, our localhost names,
# our telegram ids and so on." The list below is therefore not only the operator's
# own identifiers but every OTHER machine and persona in this estate — a second
# install's hostname or a demo company's name is somebody's private information
# too, and naming one in a comment is how it ends up in a stranger's deployment.
#
# Three classes, each learned from something that actually shipped:
#   people/company  — the greeting that introduced a stranger's bot as us
#   machines/hosts  — a copied file that read another box's paths for two days
#   ids/addresses   — chat ids and mailboxes that route to us from someone else's
IDENT='551954852|[Vv]ladimir|\bVlad\b|[Gg]alentovsky|vgalentovsky|vladimir_dst|digitalsigntech|marina@|vlad@|[Mm]arina|neo@|\bNeo\b'
HOSTS='/home/mercury|[Mm]ercury|\bDST\b|DST_|_DST|summitlabel|[Ss]ummit Label|Maclaude|demo-vps|voice-vps|trading-vps|\bsrv1[0-9]{6}\b'
NUMS='-55[0-9]{8}|\b2\.24\.102\.182\b|2-24-102-182'
HITS=$(grep -rIn -E "$IDENT|$HOSTS|$NUMS" --exclude-dir=.git .)
if [ -n "$HITS" ]; then
  echo "PUSH BLOCKED — owner PII found in working tree:" >&2
  echo "$HITS" | head -20 >&2
  echo "Scrub per DST/telegram/skill-export-scrub.md, commit, then push again." >&2
  exit 1
fi
exit 0
