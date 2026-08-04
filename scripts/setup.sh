#!/usr/bin/env bash
# One-time setup for running RYTE on your own machine.
#
#   bash scripts/setup.sh
#
# Safe to re-run: it skips whatever is already done.

set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

say()  { printf '\n\033[1m%s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
die()  { printf '\n\033[31m✗ %s\033[0m\n\n' "$1" >&2; exit 1; }

say "RYTE setup"
echo "  Working in $ROOT"

# --- Python -----------------------------------------------------------------

PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    version=$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "0.0")
    major=${version%%.*}; minor=${version##*.}
    if [ "$major" -eq 3 ] && [ "$minor" -ge 10 ]; then
      PYTHON="$candidate"
      break
    fi
  fi
done

if [ -z "$PYTHON" ]; then
  die "Python 3.10 or newer is required but wasn't found.
     On a Mac, the easiest fix is:  brew install python@3.12
     (If you don't have Homebrew: https://brew.sh)"
fi
ok "Python: $PYTHON ($($PYTHON -c 'import sys; print(sys.version.split()[0])'))"

# --- Virtual environment ----------------------------------------------------

if [ ! -d .venv ]; then
  say "Creating a virtual environment"
  "$PYTHON" -m venv .venv
  ok "Created .venv"
else
  ok "Virtual environment already exists"
fi

say "Installing RYTE and its dependencies"
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -e .
ok "Installed"

# --- Chromium ---------------------------------------------------------------
# Renders the cover images. Skipped if a matching build is already present.

say "Installing Chromium for the cover images"
chromium_works() {
  ./.venv/bin/python -c "
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    p.chromium.launch().close()
" >/dev/null 2>&1
}

if chromium_works; then
  ok "Chromium already working"
elif ./.venv/bin/playwright install chromium && chromium_works; then
  ok "Chromium installed"
else
  # Only cover images need this. Everything else - the copy, the scheduling,
  # the GHL upload - works without it, so don't fail the whole setup.
  warn "Chromium didn't install. Everything works except cover images."
  warn "Try again later with:  ./.venv/bin/playwright install chromium"
fi

# --- Configuration ----------------------------------------------------------

if [ ! -f .env ]; then
  cp .env.example .env
  say "Created .env"
  warn "Open it and paste in your keys — the same values as Railway:"
  echo "      DISCORD_BOT_TOKEN   ANTHROPIC_API_KEY"
  echo "      GHL_API_TOKEN       GHL_LOCATION_ID"
  echo ""
  echo "      open -e \"$ROOT/.env\"      # opens it in TextEdit"
else
  ok ".env already exists"
  missing=""
  for key in DISCORD_BOT_TOKEN ANTHROPIC_API_KEY GHL_API_TOKEN GHL_LOCATION_ID; do
    if ! grep -qE "^${key}=.+" .env; then
      missing="$missing $key"
    fi
  done
  if [ -n "$missing" ]; then
    warn "Still needs a value for:$missing"
  else
    ok "All four required keys are filled in"
  fi
fi

say "Done."
echo "  Start RYTE with:  bash scripts/start.sh"
echo ""
