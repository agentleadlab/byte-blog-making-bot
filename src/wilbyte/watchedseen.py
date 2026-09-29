"""The video announcements RYTE has already taken in hand.

A video announced while RYTE was offline - no wifi, the Mac asleep - was a
message nobody replays: Discord does not send a bot what it missed, and the
blog post for that video was simply never written. So the announcement
channel is read back on every start and every quarter of an hour, and these
are the announcements already done, so nothing is written twice.
"""

from __future__ import annotations

import json
from pathlib import Path

from .state import _state_dir

SEEN_PATH = _state_dir() / "watched-seen.json"

#: How many are remembered. Months of announcements.
KEEP = 2000


def exists(path: Path | None = None) -> bool:
    return (path or SEEN_PATH).exists()


def load(path: Path | None = None) -> list[str]:
    try:
        data = json.loads((path or SEEN_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [str(one) for one in data] if isinstance(data, list) else []


def add(message_id, path: Path | None = None) -> None:
    where = path or SEEN_PATH
    held = load(where)
    key = str(message_id)
    if key in held:
        return
    held = (held + [key])[-KEEP:]
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text(json.dumps(held), encoding="utf-8")
