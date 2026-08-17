#!/usr/bin/env bash
# Double-click this file in Finder to start RYTE.
#
# macOS opens .command files in Terminal and runs them, so this is the
# no-typing way in. Leave the window it opens alone while you work; closing it
# stops RYTE. Posts already scheduled in GoHighLevel publish on their own.

cd "$(dirname "$0")"

# A stale copy still holds the Discord token, and two of them answer every
# message twice - so clear any that are already running before starting.
pkill -f "wilbyte bot" 2>/dev/null && sleep 1

# Pull first. Forgetting this has twice looked like a fix that didn't work,
# because the bot was still answering out of last week's code. Non-fatal by
# design: if the update can't run, RYTE still starts.
bash scripts/update.sh

exec bash scripts/start.sh
