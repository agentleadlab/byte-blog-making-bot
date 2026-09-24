"""Faith's texts, remembered, and which of them Franklin has been pinged about.

Read once deeply and then only what is new: a minute's watch should cost one
request for the last minute, not one for four months of texts. And which
agent texts have already had a suggestion posted, so a restart does not ping
Franklin about the same message twice.

Bounded both ways. Nothing in here is the only copy of anything - the texts
are still on RingCentral - so forgetting the oldest costs a re-read at most.
"""

from __future__ import annotations

import json
from pathlib import Path

from .state import _state_dir

TEXTS_PATH = _state_dir() / "ring-texts.json"

#: How many texts are kept. A few months of one person's messages with every
#: agent, which is plenty to learn how she writes.
KEEP_TEXTS = 6000

#: How many pinged-about message ids are kept. Days of them.
KEEP_PINGED = 2000


def load(path: Path | None = None) -> dict:
    """{"texts": [...], "pinged": [...]}."""
    where = path or TEXTS_PATH
    empty = {"texts": [], "pinged": []}
    if not where.exists():
        return empty
    try:
        data = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    data.setdefault("texts", [])
    data.setdefault("pinged", [])
    return data


def save(data: dict, path: Path | None = None) -> None:
    where = path or TEXTS_PATH
    try:
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def keep(data: dict, texts: list) -> int:
    """Add these, by id, oldest first, oldest dropped. How many were new."""
    held = {str(one.get("id")): one for one in data.get("texts") or []}
    fresh = 0
    for one in texts:
        as_dict = one.as_dict() if hasattr(one, "as_dict") else dict(one)
        key = str(as_dict.get("id") or "")
        if not key:
            continue
        if key not in held:
            fresh += 1
        held[key] = as_dict
    ordered = sorted(held.values(), key=lambda one: str(one.get("at") or ""))
    data["texts"] = ordered[-KEEP_TEXTS:]
    return fresh


def newest(data: dict) -> str:
    """When the newest text remembered was sent, or ""."""
    texts = data.get("texts") or []
    return str(texts[-1].get("at") or "") if texts else ""


def was_pinged(data: dict, message_id: str) -> bool:
    return str(message_id) in set(data.get("pinged") or [])


def pinged(data: dict, message_id: str) -> None:
    held = [one for one in data.get("pinged") or [] if one != str(message_id)]
    held.append(str(message_id))
    data["pinged"] = held[-KEEP_PINGED:]
