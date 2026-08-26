"""Cards told to stay put tonight.

"I don't want RYTE to move that checklist to tomorrow's card." The rollover
finds the day's cards by the date in the title, anywhere on the board, so
dragging one into another list does not stop it - the only way to say no is
to say no.

Kept per day and read by the evening run, so it is a standing instruction
rather than a one-off: saying it at four in the afternoon still holds at half
past eight.
Yesterday's entries are dropped on the next write, which makes tomorrow start
clean without anybody having to remember to undo it.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from .state import _state_dir

SKIP_PATH = _state_dir() / "rollover-skip.json"

# Enough that a note left on Friday survives the weekend, short enough that a
# forgotten one cannot quietly hold a card back next month.
KEEP_DAYS = 7


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def for_day(day: date, path: Path | None = None) -> tuple[str, ...]:
    """Which card kinds were told to stay put on that day."""
    held = _read(path or SKIP_PATH).get(day.isoformat()) or []
    return tuple(kind for kind in held if isinstance(kind, str))


def hold(day: date, kinds, path: Path | None = None) -> tuple[str, ...]:
    """Add kinds to the day's list. Returns everything held for that day."""
    where = path or SKIP_PATH
    data = _forget_old(_read(where), day)
    said = list(data.get(day.isoformat()) or [])
    for kind in kinds:
        if kind and kind not in said:
            said.append(kind)
    data[day.isoformat()] = said
    _write(where, data)
    return tuple(said)


def release(day: date, kinds=None, path: Path | None = None) -> tuple[str, ...]:
    """Take kinds off the day's list, or all of them when none are named."""
    where = path or SKIP_PATH
    data = _forget_old(_read(where), day)
    if kinds is None:
        data.pop(day.isoformat(), None)
        _write(where, data)
        return ()
    said = [k for k in (data.get(day.isoformat()) or []) if k not in set(kinds)]
    data[day.isoformat()] = said
    _write(where, data)
    return tuple(said)


def _forget_old(data: dict, day: date) -> dict:
    floor = (day - timedelta(days=KEEP_DAYS)).isoformat()
    return {when: kinds for when, kinds in data.items() if when >= floor}


def _write(where: Path, data: dict) -> None:
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
