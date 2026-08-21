#!/usr/bin/env bash
# Run RYTE on your own machine.
#
#   bash scripts/start.sh
#
# Leave this window open while you work. Ctrl-C stops it.
#
# Posts already scheduled in GoHighLevel publish on their own — GHL does that,
# not RYTE — so it's fine to stop this once your batch is approved.

set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  printf '\n\033[31m✗ Not set up yet. Run this first:\033[0m\n\n    bash scripts/setup.sh\n\n' >&2
  exit 1
fi

if [ ! -f .env ]; then
  printf '\n\033[31m✗ No .env file. Run this first:\033[0m\n\n    bash scripts/setup.sh\n\n' >&2
  exit 1
fi

# RYTE exits with this code when it has spotted an update and wants to come
# back on the new version. Any other exit means it stopped or crashed, and
# looping on a crash would just spin.
RESTART_CODE=42

# Ctrl-C should stop RYTE, not restart it.
trap 'printf "\n\033[1mStopped.\033[0m\n"; exit 0' INT

printf '\n\033[1mStarting RYTE\033[0m — leave this window open. Ctrl-C to stop.\n\n'

while true; do
  set +e
  ./.venv/bin/wilbyte bot
  code=$?
  set -e

  if [ "$code" -ne "$RESTART_CODE" ]; then
    exit "$code"
  fi

  printf '\n\033[1mUpdate found — restarting.\033[0m\n\n'
  bash scripts/update.sh
done
