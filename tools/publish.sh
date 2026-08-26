#!/bin/bash
# Publish the skills to BOTH repositories: the working remote and the branded
# mirror customers are pointed at.
#
# WHY NOT A SECOND PUSH URL ON `origin`. That is the one-liner, and it hangs in
# this environment: git runs the credential helper twice inside one process and
# the second call never returns, so `git push origin HEAD` sits forever while
# each URL pushed on its own completes instantly. A publish that hangs is worse
# than one that takes two commands — it gets killed halfway, and then exactly
# one of the two repositories has the change, which is the stale mirror this
# was meant to prevent.
#
# Sequential, timeout-bounded, and it reports what each side ended up holding
# rather than assuming the push meant delivery.
set -u
cd "$(git rev-parse --show-toplevel)" || exit 1
BRANCH=$(git rev-parse --abbrev-ref HEAD)
LOCAL=$(git rev-parse --short HEAD)
rc=0

for remote in origin org; do
  url=$(git remote get-url --push "$remote" 2>/dev/null) || continue
  if GIT_TERMINAL_PROMPT=0 timeout 90 git push "$remote" "HEAD:$BRANCH" 2>&1 | sed "s/^/[$remote] /"; then :; else
    echo "[$remote] PUSH FAILED ($url)" >&2
    rc=1
  fi
done

echo
for remote in origin org; do
  url=$(git remote get-url --push "$remote" 2>/dev/null) || continue
  head=$(GIT_TERMINAL_PROMPT=0 timeout 45 git ls-remote "$url" -h "refs/heads/$BRANCH" | cut -c1-7)
  status="stale"; [ "$head" = "$LOCAL" ] && status="in sync"
  printf '%-8s %s  %s  %s\n' "$remote" "${head:-unreachable}" "$status" "$url"
done
exit $rc
