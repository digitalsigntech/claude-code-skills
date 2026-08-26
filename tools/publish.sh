#!/bin/bash
# Publish the skills to BOTH homes: the working repo and the branded mirror
# customers are sent to.
#
# WHY A SCRIPT AND NOT A SECOND PUSH URL ON `origin`. That is the one-liner
# everybody reaches for, and it hung here: with two push URLs the credential
# helper is invoked twice and the second invocation waits on a terminal that a
# cron job does not have. A push that hangs is worse than one that fails —
# nothing is published and nothing says so. Two explicit pushes, each with
# prompting disabled, each reporting its own result.
set -u
cd "$(git rev-parse --show-toplevel)" || exit 1
BRANCH=$(git rev-parse --abbrev-ref HEAD)
HEAD_SHA=$(git rev-parse HEAD)
rc=0

for remote in origin org; do
  url=$(git remote get-url --push "$remote" 2>/dev/null) || { echo "no remote $remote" >&2; rc=1; continue; }
  if GIT_TERMINAL_PROMPT=0 timeout 90 git push "$remote" "HEAD:$BRANCH" 2>&1 | sed "s/^/[$remote] /"; then :; else
    echo "[$remote] PUSH FAILED" >&2; rc=1
  fi
  # Verified, not assumed: ask the remote what it now holds.
  got=$(GIT_TERMINAL_PROMPT=0 timeout 45 git ls-remote "$url" -h "refs/heads/$BRANCH" | cut -f1)
  if [ "$got" = "$HEAD_SHA" ]; then
    echo "[$remote] at $(git rev-parse --short HEAD) ✓"
  else
    echo "[$remote] STALE — holds ${got:0:12}, expected $(git rev-parse --short HEAD)" >&2
    rc=1
  fi
done
exit $rc
