"""What RYTE has already put in front of somebody, remembered across restarts.

The tag watcher posts a list and waits for the button. Left alone, that list
must not come back: "if you already said it dont add it to the next update i
already said leave it". Holding that in memory was enough until the first
restart, and RYTE is restarted several times on a busy evening - every one of
them re-posted the same twelve lines and the same three warnings.

So it goes on disk, beside the board clock, for the same reason the board
clock is there: "RYTE is restarted often enough that 'it was running at nine'
is not something to rely on".

Kept per day, and a few days of it. A comment on tomorrow's card shown this
afternoon is the same comment tomorrow morning, when that card is today's -
"ryte is sending the same checklist again and again even when leaving it be".
Longer than that is a file nobody ever looks at.
"""

from __future__ import annotations

import json
import threading
from datetime import timedelta
from pathlib import Path

from .state import _state_dir

SAID_PATH = _state_dir() / "already-said.json"

#: Days kept before a day's lines are forgotten. Enough to cover a weekend:
#: a comment on Monday's card shown on Friday afternoon is still the same
#: comment when Monday comes.
KEEP_DAYS = 4

#: How far back a comment counts as already shown. A comment's id belongs to
#: one card, so a line left alone yesterday afternoon on tomorrow's card is the
#: same line this morning, when that card has become today's.
LOOK_BACK_DAYS = 3

#: Two watchers write this file - the tag watcher and the day check - each
#: from its own thread. Without the lock one could read the file, the other
#: write it, and the first then write back a copy without the second's lines.
_LOCK = threading.Lock()


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


def said_lately(day, path: Path | None = None, *, days: int = LOOK_BACK_DAYS) -> set[str]:
    """Everything shown on this day or the `days` before it."""
    book = load(path)
    if not hasattr(day, "isoformat"):
        return said_on(day, path)
    stamps = {_stamp(day - timedelta(days=back)) for back in range(days + 1)}
    return {
        str(one)
        for stamp in stamps
        for one in book.get(stamp) or []
        if isinstance(one, (str, int))
    }


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
    with _LOCK:
        book = load(where)
        book[stamp] = sorted(set(book.get(stamp) or []) | set(lines))
        for old in sorted(book)[:-KEEP_DAYS]:
            book.pop(old, None)
        where.parent.mkdir(parents=True, exist_ok=True)
        # Written beside it and swapped in, so nothing reading at the same
        # moment ever sees half a file. Half a file reads as no file, and no
        # file is every line of the day offered again.
        spare = where.with_suffix(".tmp")
        spare.write_text(json.dumps(book, indent=2, sort_keys=True), encoding="utf-8")
        spare.replace(where)


def forget(path: Path | None = None) -> None:
    """Start again, for when somebody wants the whole list back."""
    where = path or SAID_PATH
    if where.exists():
        where.unlink()
