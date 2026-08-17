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

printf '\033[1mChecking for updates\033[0m (%s)... ' "$branch"

if ! git fetch --quiet origin "$branch" 2>/dev/null; then
  printf '\033[33mcan'\''t reach GitHub. Starting the copy you already have.\033[0m\n\n'
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

# A fast-forward is the only safe automatic update. Anything else means local
# edits or a diverged history, and guessing at a merge here would be worse than
# saying so.
printf '\033[33mcouldn'\''t update automatically.\033[0m\n'
printf '    Something local is in the way. Run these two lines, then start again:\n\n'
printf '        cd "%s"\n' "$(pwd)"
printf '        git stash && git pull\n\n'
printf '    Starting the copy you already have for now.\n\n'
exit 0
