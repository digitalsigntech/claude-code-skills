#!/bin/bash
# Refuse to publish anything that names a person, a machine or an account.
#
# Install as this repo's pre-push hook:
#   ln -sf ../../tools/scrub-check.sh .git/hooks/pre-push
#
# The patterns live OUTSIDE the repository, in $SKILLS_SCRUB_PATTERNS or
# ~/.config/skills-scrub/patterns.txt — one extended regex per line, # for
# comments. That is not tidiness: the first version of this hook carried its
# patterns inline, and publishing it would have published a tidy list of every
# name, host and id it exists to keep out. A denylist of your own identifiers is
# itself a list of your own identifiers.
#
# See tools/patterns.example.txt for the shape.
set -u
cd "$(git rev-parse --show-toplevel)" || exit 1
PATTERNS="${SKILLS_SCRUB_PATTERNS:-$HOME/.config/skills-scrub/patterns.txt}"

if [ ! -r "$PATTERNS" ]; then
  echo "scrub-check: no pattern file at $PATTERNS — nothing is being checked." >&2
  echo "Copy tools/patterns.example.txt there and list what must never ship." >&2
  exit 0            # a fresh clone with nothing to hide is not an error
fi

RE=$(grep -vE '^\s*(#|$)' "$PATTERNS" | paste -sd'|' -)
[ -z "$RE" ] && exit 0

# -I skips binaries: a stale .pyc used to match as noise and hide the real hit.
HITS=$(grep -rIn -E "$RE" --exclude-dir=.git .)
if [ -n "$HITS" ]; then
  echo "PUSH BLOCKED — this tree names something that must not be published:" >&2
  echo "$HITS" | head -20 >&2
  echo >&2
  echo "Replace each with a variable read from config at install time, commit," >&2
  echo "then push again. Never --no-verify." >&2
  exit 1
fi
exit 0
