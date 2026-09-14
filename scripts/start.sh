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

# Pull before starting, not only after a restart-exit. Ctrl-C and re-run is how
# a restart actually happens in a terminal, and without this that route came
# back up on the same old code - which looks exactly like a fix that didn't
# work. Non-fatal by design: a failed update still starts the copy you have.
bash scripts/update.sh

# The package itself, not just the folder. A venv can lose it - macOS moves
# Python out from under it on an update, and the editable link points at
# nothing - and the only sign is "ModuleNotFoundError: No module named
# 'wilbyte'" at the moment somebody is trying to start work. Putting it back
# is one command, so do it rather than print it.
if ! ./.venv/bin/python -c "import wilbyte" >/dev/null 2>&1; then
  printf '\n\033[1mPutting RYTE back into the environment\033[0m — one moment.\n'
  if ! ./.venv/bin/pip install --quiet -e . >/dev/null 2>&1 \
     || ! ./.venv/bin/python -c "import wilbyte" >/dev/null 2>&1; then
    printf '\n\033[31m✗ The environment is broken. This rebuilds it:\033[0m\n\n' >&2
    printf '    rm -rf .venv && bash scripts/setup.sh\n\n' >&2
    printf 'Your .env and its keys are left alone.\n\n' >&2
    exit 1
  fi
  printf '\033[32m✓ Back.\033[0m\n'
fi

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
