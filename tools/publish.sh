#!/bin/bash
# Publish the skills to BOTH homes: the working repo and the branded mirror
# customers are sent to.
#
# WHY A SCRIPT AND NOT A SECOND PUSH URL ON `origin`. That is the one-liner
# everybody reaches for, and it hung here: with two push URLs the credential
# helper is invoked twice, and the second invocation waits on a terminal that a
# cron job does not have. A push that HANGS is worse than one that fails —
# nothing is published and nothing says so. Two explicit pushes, prompting
# disabled, each with its own timeout and its own verdict.
#
# And it ASKS EACH REMOTE WHAT IT HOLDS afterwards. "git push said ok" is the
# same class of evidence as "the log line said announced": true about the call,
# silent about the outcome. The exit status is non-zero unless both repositories
# actually contain this commit.
set -u
cd "$(git rev-parse --show-toplevel)" || exit 1

BRANCH=$(git rev-parse --abbrev-ref HEAD)
LOCAL=$(git rev-parse HEAD)
rc=0

for remote in origin org; do
  url=$(git remote get-url --push "$remote" 2>/dev/null) || {
    echo "[$remote] NO SUCH REMOTE — nothing published there" >&2
    rc=1
    continue
  }
  GIT_TERMINAL_PROMPT=0 timeout 90 git push "$remote" "HEAD:$BRANCH" 2>&1 \
    | sed "s|^|[$remote] |"
  got=$(GIT_TERMINAL_PROMPT=0 timeout 45 git ls-remote "$url" \
        -h "refs/heads/$BRANCH" 2>/dev/null | cut -f1)
  if [ "$got" = "$LOCAL" ]; then
    echo "[$remote] holds $(git rev-parse --short HEAD) — in sync"
  else
    echo "[$remote] STALE: holds ${got:-unreachable}, expected $LOCAL" >&2
    rc=1
  fi
done

exit $rc
