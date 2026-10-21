#!/usr/bin/env bash
# Pack what RYTE remembers on this machine - state/, corpus/ and the saved
# bodies of posts still to publish - into one file
# on the Desktop, for `@RYTE restore state` on the new host (Railway).
#
#   bash scripts/pack-state.sh
#
# The file has client data in it (texts, payments). Attach it in Discord to
# that one message only; RYTE deletes the message once it's restored.

set -euo pipefail
cd "$(dirname "$0")/.."

out="$HOME/Desktop/ryte-state.tar.gz"
folders=()
[ -d state ] && folders+=(state)
[ -d corpus ] && folders+=(corpus)
if [ ${#folders[@]} -eq 0 ]; then
  printf '\n\033[31m✗ No state/ folder here - run this in the RYTE folder on the Mac.\033[0m\n\n' >&2
  exit 1
fi

# The saved bodies of posts still to publish, so they publish from the new
# host too - nothing else from out/.
payloads=()
if [ -d out ]; then
  while IFS= read -r one; do payloads+=("$one"); done < <(find out -mindepth 2 -maxdepth 2 -name ghl-payload.json)
fi

tar -czf "$out" --exclude='*.tmp' --exclude='.DS_Store' --exclude='state/update-blocked' \
  "${folders[@]}" ${payloads[@]+"${payloads[@]}"}
printf '\n\033[32m✓ Packed\033[0m %s (%s)\n\n' "$out" "$(du -h "$out" | cut -f1)"
printf 'Next: in Discord, @Ryte restore state - with that file attached.\n\n'
