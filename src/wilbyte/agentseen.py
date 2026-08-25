"""Agent cards RYTE has already said it can't read.

A card with no lead type, or no launch date, sits in In Que until somebody
fills it in. The watcher looks every five minutes, so without a memory it
would say the same thing about the same card two hundred times a day, and a
channel that cries wolf that often is one nobody reads.

Only the ones it *couldn't* act on are remembered. A card it filed leaves In
Que, which is memory enough.
"""

from __future__ import annotations

import json
from pathlib import Path

from .state import _state_dir

SEEN_PATH = _state_dir() / "agents-said.json"

# Enough that a card sitting all week is mentioned once, not enough that a
# card fixed and re-broken next month goes unmentioned.
KEEP = 500


def load(path: Path | None = None) -> set[str]:
    where = path or SEEN_PATH
    if not where.exists():
        return set()
    try:
        data = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return set(data) if isinstance(data, list) else set()


def remember(card_ids: list[str], path: Path | None = None) -> None:
    where = path or SEEN_PATH
    kept = list(load(where)) + [card_id for card_id in card_ids if card_id]
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text(json.dumps(kept[-KEEP:], indent=2), encoding="utf-8")


def forget(card_ids: list[str], path: Path | None = None) -> None:
    """Say it again about this card - it changed, or somebody asked."""
    where = path or SEEN_PATH
    kept = [card_id for card_id in load(where) if card_id not in set(card_ids)]
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text(json.dumps(kept, indent=2), encoding="utf-8")
