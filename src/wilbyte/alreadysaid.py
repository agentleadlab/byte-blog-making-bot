"""What RYTE has already put in front of somebody, remembered across restarts.

The tag watcher posts a list and waits for the button. Left alone, that list
must not come back: "if you already said it dont add it to the next update i
already said leave it". Holding that in memory was enough until the first
restart, and RYTE is restarted several times on a busy evening - every one of
them re-posted the same twelve lines and the same three warnings.

So it goes on disk, beside the board clock, for the same reason the board
clock is there: "RYTE is restarted often enough that 'it was running at nine'
is not something to rely on".

Kept per day. Yesterday's twelve lines are not going to be offered again
anyway - the comments they came from are on yesterday's cards - and a file
that grows all week is one nobody ever looks at.
"""

from __future__ import annotations

import json
from pathlib import Path

from .state import _state_dir

SAID_PATH = _state_dir() / "already-said.json"

#: Days kept before a day's lines are forgotten. Two, so a restart just after
#: midnight still knows what last night's shift was shown.
KEEP_DAYS = 2


def _stamp(day) -> str:
    return day.isoformat() if hasattr(day, "isoformat") else str(day)


def load(path: Path | None = None) -> dict:
    where = path or SAID_PATH
    if not where.exists():
        return {}
    try:
        data = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def said_on(day, path: Path | None = None) -> set[str]:
    """Everything already shown on this day."""
    found = load(path).get(_stamp(day)) or []
    return {str(one) for one in found if isinstance(one, (str, int))}


def remember(day, lines, path: Path | None = None) -> None:
    """Add these to the day's list, and forget the days before last.

    Written as it is posted rather than after the button, so a tick arriving
    while somebody is still reading does not post the same list underneath.
    """
    lines = [str(one) for one in lines or []]
    if not lines:
        return
    where = path or SAID_PATH
    stamp = _stamp(day)
    book = load(where)
    book[stamp] = sorted(set(book.get(stamp) or []) | set(lines))
    for old in sorted(book)[:-KEEP_DAYS]:
        book.pop(old, None)
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text(json.dumps(book, indent=2, sort_keys=True), encoding="utf-8")


def forget(path: Path | None = None) -> None:
    """Start again, for when somebody wants the whole list back."""
    where = path or SAID_PATH
    if where.exists():
        where.unlink()
