"""Wrong setups RYTE has already raised.

The check runs every ten minutes and a mismatch stays a mismatch until
somebody fixes it, so without a memory the same agent would be reported a
hundred and forty times a day.

Remembered by what the disagreement *is*, not just by which card it is on: if
the setup gets redone, or the order changes, the pair is different and it is
said again. Fixing it correctly makes it stop; fixing it wrongly does not.
"""

from __future__ import annotations

import json
from pathlib import Path

from .state import _state_dir

SEEN_PATH = _state_dir() / "setups-said.json"

# Enough to cover a busy fortnight, not enough that one raised last quarter
# stays silent when it comes back.
KEEP = 500


def mark(card_id: str, ordered: str, setup: str) -> str:
    """The identity of one disagreement, stable across a restart."""
    return "|".join(
        [card_id or "", " ".join((ordered or "").split()).casefold(),
         " ".join((setup or "").split()).casefold()]
    )


def load(path: Path | None = None) -> set[str]:
    where = path or SEEN_PATH
    if not where.exists():
        return set()
    try:
        data = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return set(data) if isinstance(data, list) else set()


def remember(marks: list[str], path: Path | None = None) -> None:
    where = path or SEEN_PATH
    kept = list(load(where)) + [mark for mark in marks if mark]
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text(json.dumps(kept[-KEEP:], indent=2), encoding="utf-8")


def forget(marks: list[str], path: Path | None = None) -> None:
    """Say it again - somebody asked, or it changed back."""
    where = path or SEEN_PATH
    kept = [held for held in load(where) if held not in set(marks)]
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text(json.dumps(kept, indent=2), encoding="utf-8")
