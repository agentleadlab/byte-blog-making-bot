#!/usr/bin/env bash
# Pull the latest RYTE. Runs automatically from "Start RYTE.command".
#
# Deliberately non-fatal: a failed update should leave you running the copy you
# already have, not stop RYTE from starting. The one thing it must never do is
# fail silently - a bot quietly running last week's code looks identical to a
# fix that didn't work, and that mistake has cost a day already.

cd "$(dirname "$0")/.."

if [ ! -d .git ]; then
  exit 0  # not a checkout (a zip download, say) - nothing to update from
fi

branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '')"
if [ -z "$branch" ] || [ "$branch" = "HEAD" ]; then
  exit 0
fi

# RYTE reads this on start and says so in Discord. A warning that only exists
# in a terminal window is a warning nobody sees.
BLOCKED_FILE="${WILBYTE_STATE_DIR:-state}/update-blocked"
mkdir -p "$(dirname "$BLOCKED_FILE")" 2>/dev/null
rm -f "$BLOCKED_FILE"

printf '\033[1mChecking for updates\033[0m (%s)... ' "$branch"

if ! git fetch --quiet origin "$branch" 2>/dev/null; then
  printf '\033[33mcan'\''t reach GitHub. Starting the copy you already have.\033[0m\n\n'
  echo "Couldn't reach GitHub to check for updates." > "$BLOCKED_FILE"
  exit 0
fi

before="$(git rev-parse HEAD)"

if git merge --ff-only -q "origin/$branch" 2>/dev/null; then
  after="$(git rev-parse HEAD)"
  if [ "$before" = "$after" ]; then
    printf 'already current.\n\n'
  else
    printf '\033[32mupdated.\033[0m\n'
    git --no-pager log --oneline "$before..$after" | sed 's/^/    /'
    printf '\n'
  fi
  exit 0
fi

# The fast-forward failed, which in practice means a local edit to a tracked
# file - a config line changed by hand months ago. Printing instructions was
# not enough: this window scrolls past in a second, and RYTE ran five commits
# behind for a day while every push looked like it had landed.
#
# So set the edits aside and try once more. `git stash` keeps them, and the
# recovery is one command that this prints. Silently running old code is the
# worse outcome by a wide margin.
printf '\033[33mblocked by local edits.\033[0m\n'

if git stash push --quiet -m "wilbyte auto-update $(git rev-parse --short HEAD)" 2>/dev/null; then
  if git merge --ff-only -q "origin/$branch" 2>/dev/null; then
    printf '    Set your local edits aside and updated. To get them back:\n'
    printf '        cd "%s" && git stash pop\n\n' "$(pwd)"
    exit 0
  fi
  # The edits were not what stood in the way, so put them straight back.
  git stash pop --quiet 2>/dev/null
fi

echo "Update blocked — I am running $(git rev-parse --short HEAD), not the latest." > "$BLOCKED_FILE"
printf '    Couldn'\''t update automatically, and it isn'\''t just local edits.\n'
printf '    Run this and send me what it says:\n\n'
printf '        cd "%s" && git status && git pull\n\n' "$(pwd)"
printf '    \033[33mStarting the copy you already have — it is NOT the latest.\033[0m\n\n'
exit 0
