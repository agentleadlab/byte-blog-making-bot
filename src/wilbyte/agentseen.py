"""Agent cards RYTE has already said it can't read, and when it said so.

A card with no lead type, or no launch date, sits in In Que until somebody
fills it in. The watcher looks every twenty seconds, so without a memory it
would say the same thing about the same card two hundred times a day, and a
channel that cries wolf that often is one nobody reads.

But saying it exactly once is the other failure: the message lands while
everyone is at lunch, scrolls away, and the card sits there all afternoon. So
what is remembered is the *time* it was said, and a card still waiting three
hours later is worth mentioning again.

How often depends on how soon the agent launches. Today, tomorrow, or no date
at all, and it is raised every few hours until somebody deals with it. Days
out, and it is named once and then left alone - there is nothing to be late
for yet.

Only the ones it couldn't act on are remembered. A card it filed leaves In
Que, which is memory enough.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .state import _state_dir

SEEN_PATH = _state_dir() / "agents-said.json"

# Long enough not to be nagging, short enough that a card left at eleven is
# raised again after lunch.
SAY_AGAIN_AFTER = 3 * 60 * 60

# For a card that isn't urgent - a launch several days out. Naming it once
# puts it on the record; nothing about it needs doing today. It starts being
# raised on the shorter clock by itself once the launch comes within a day,
# because by then it was last said long enough ago to be due again.
ONLY_ONCE = float("inf")

# Enough that a card sitting all week is remembered, not enough to grow
# forever.
KEEP = 500


def load(path: Path | None = None) -> dict[str, float]:
    """Card id -> when it was last mentioned, as epoch seconds."""
    where = path or SEEN_PATH
    if not where.exists():
        return {}
    try:
        data = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}

    if isinstance(data, dict):
        return {str(card): float(when) for card, when in data.items()}
    if isinstance(data, list):
        # The old shape, before the time was kept. Treated as said just now:
        # the alternative is every card ever quietened announcing itself the
        # first time this runs.
        now = time.time()
        return {str(card): now for card in data}
    return {}


def due(
    card_ids: list[str],
    *,
    held: dict[str, float] | None = None,
    every: float = SAY_AGAIN_AFTER,
    now: float | None = None,
    path: Path | None = None,
) -> list[str]:
    """Which of these are worth mentioning: never said, or said a while ago."""
    seen = held if held is not None else load(path)
    at = now if now is not None else time.time()
    return [
        card for card in card_ids
        if card and (card not in seen or at - seen[card] >= every)
    ]


def remember(card_ids: list[str], path: Path | None = None, now: float | None = None) -> None:
    where = path or SEEN_PATH
    seen = load(where)
    at = now if now is not None else time.time()
    for card in card_ids:
        if card:
            seen[card] = at

    # Oldest first, so trimming drops what was said longest ago.
    kept = sorted(seen.items(), key=lambda held: held[1])[-KEEP:]
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text(json.dumps(dict(kept), indent=2), encoding="utf-8")


def forget(card_ids: list[str], path: Path | None = None) -> None:
    """Say it again about this card - it changed, or somebody asked."""
    where = path or SEEN_PATH
    seen = load(where)
    for card in card_ids:
        seen.pop(card, None)
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text(json.dumps(seen, indent=2), encoding="utf-8")
