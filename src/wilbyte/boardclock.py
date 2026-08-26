"""Which of the day's board steps have already run.

Five things happen to the daily board on a schedule - the 6am setup card,
the 9am move, the 6pm move, and at 8pm the carry-over and then the move to
Done - and each must happen once. Not twice, because
moving cards that already moved puts them somewhere nobody expects; and not
never, because RYTE is restarted often enough that "it was running at nine" is
not something to rely on.

So each step records the day it ran on. A restart at 9:05 sees the 9am move is
already done and leaves it; a start at 11am sees it isn't and catches up.
"""

from __future__ import annotations

import json
from pathlib import Path

from .state import _state_dir

CLOCK_PATH = _state_dir() / "board-clock.json"


def load(path: Path | None = None) -> dict:
    where = path or CLOCK_PATH
    if not where.exists():
        return {}
    try:
        data = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def done_on(day, path: Path | None = None) -> set[str]:
    """The steps already run on this day."""
    stamp = day.isoformat() if hasattr(day, "isoformat") else str(day)
    return {
        step for step, when in load(path).items() if when == stamp
    }


def mark(step: str, day, path: Path | None = None) -> None:
    """Record that a step ran, so the next tick leaves it alone."""
    where = path or CLOCK_PATH
    stamp = day.isoformat() if hasattr(day, "isoformat") else str(day)
    steps = load(where)
    steps[step] = stamp
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text(json.dumps(steps, indent=2, sort_keys=True), encoding="utf-8")
