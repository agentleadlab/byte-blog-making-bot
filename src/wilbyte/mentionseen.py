"""The @RYTE mentions already answered, and when RYTE was last listening.

Discord does not send a bot what was said while it was away. A mention typed
while RYTE was restarting onto an update - which it does several times a day -
or while the Mac had no wifi was never answered, and looked from the outside
exactly like RYTE ignoring somebody. So every start reads back for them, and
this is what it reads back against: the last minute RYTE is known to have been
listening, and the mentions already taken in hand, so none is answered twice.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

from .state import _state_dir

SEEN_PATH = _state_dir() / "mentions-seen.json"

#: How many answered mentions are remembered. Weeks of them.
KEEP = 1000

_LOCK = threading.Lock()


def load(path: Path | None = None) -> dict:
    try:
        data = json.loads((path or SEEN_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    answered = data.get("answered")
    data["answered"] = [str(one) for one in answered] if isinstance(answered, list) else []
    return data


def _save(data: dict, where: Path) -> None:
    where.parent.mkdir(parents=True, exist_ok=True)
    spare = where.with_suffix(".tmp")
    spare.write_text(json.dumps(data), encoding="utf-8")
    spare.replace(where)


def answered(path: Path | None = None) -> set[str]:
    return set(load(path)["answered"])


def add(message_id, path: Path | None = None) -> None:
    """This mention is taken in hand."""
    where = path or SEEN_PATH
    key = str(message_id)
    with _LOCK:
        data = load(where)
        if key in data["answered"]:
            return
        data["answered"] = (data["answered"] + [key])[-KEEP:]
        _save(data, where)


def listening_at(path: Path | None = None) -> datetime | None:
    """The last time RYTE was known to be listening, or None the first time."""
    said = load(path).get("alive_at")
    try:
        return datetime.fromisoformat(str(said)) if said else None
    except ValueError:
        return None


def listening(now: datetime, path: Path | None = None) -> None:
    where = path or SEEN_PATH
    with _LOCK:
        data = load(where)
        data["alive_at"] = now.isoformat()
        _save(data, where)
