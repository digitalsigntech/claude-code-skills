#!/bin/bash
# Block any push whose tree names a person, a machine or an account.
#
# Install:  cp tools/pre-push-hook.sh .git/hooks/pre-push && chmod +x .git/hooks/pre-push
#
# The patterns are NOT in this file. They are the very strings that must never be
# published, and an earlier version listed them here in plain text — a scrub gate
# that was itself the largest single disclosure in the repository, and which
# eventually blocked its own push by matching itself.
#
# So the list lives outside the tree, one extended-regex pattern per line:
#
#   $IDENTITY_GUARD_FILE            if set
#   ../.identity-guard              beside the checkout (the usual place)
#   ~/.config/skills/identity-guard
#
# See identity-guard.example for the shape and for what belongs in it. With no
# list the hook says so and lets the push through: a guard that silently passes
# when unconfigured is worse than none, and one that blocks every push on a fresh
# clone gets deleted within the day.
cd "$(git rev-parse --show-toplevel)" || exit 1

for candidate in "$IDENTITY_GUARD_FILE" "../.identity-guard" \
                 "$HOME/.config/skills/identity-guard"; do
  [ -n "$candidate" ] && [ -f "$candidate" ] && GUARD="$candidate" && break
done

if [ -z "$GUARD" ]; then
  echo "note: no identity-guard list found — pushing unchecked." >&2
  echo "      see tools/identity-guard.example to set one up." >&2
  exit 0
fi

# Comments and blank lines out; everything else is a pattern.
PATTERNS=$(grep -vE '^\s*(#|$)' "$GUARD" | paste -sd'|' -)
[ -z "$PATTERNS" ] && exit 0

HITS=$(grep -rIn -E "$PATTERNS" --exclude-dir=.git .)
if [ -n "$HITS" ]; then
  echo "PUSH BLOCKED — this tree names something that must not be published:" >&2
  echo "$HITS" | head -20 >&2
  echo >&2
  echo "Every one of these is an identifier: a person, a company, a host, a chat," >&2
  echo "a mailbox. Replace it with a variable read from config at install time," >&2
  echo "commit, and push again. Never --no-verify." >&2
  exit 1
fi
exit 0
