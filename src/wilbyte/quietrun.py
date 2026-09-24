"""Where a run through the quiet channels had got to.

The run lives in the bot's memory, and a restart ends it without a word:
"it stopped and didnt go thru the list" - RYTE had been restarted to pick
up an update, halfway down a hundred and seventy-nine channels. So each step
is written down, and the next start offers to carry on from there.

Nothing is decided from this file. It says which channel was next; the two
questions are still asked on every one, and a channel that is gone by the
time the run carries on is simply not in the list any more.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .state import _state_dir

RUN_PATH = _state_dir() / "quiet-run.json"

#: A run older than this is not offered back. Two days on, the list itself
#: has moved, and "carry on" would be carrying on something nobody remembers.
STALE_AFTER = timedelta(days=2)


def load(path: Path | None = None) -> dict | None:
    where = path or RUN_PATH
    try:
        held = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return held if isinstance(held, dict) and held.get("names") else None


def save(run: dict, path: Path | None = None) -> None:
    where = path or RUN_PATH
    where.parent.mkdir(parents=True, exist_ok=True)
    held = {**run, "saved": datetime.now(timezone.utc).isoformat()}
    spare = where.with_suffix(".tmp")
    spare.write_text(json.dumps(held, indent=1), encoding="utf-8")
    spare.replace(where)


def clear(path: Path | None = None) -> None:
    try:
        (path or RUN_PATH).unlink()
    except OSError:
        pass


def unfinished(run: dict | None, *, now: datetime | None = None) -> bool:
    """Whether a run was cut short recently enough to offer carrying on."""
    if not run or run.get("at", 0) >= len(run.get("names") or []):
        return False
    try:
        saved = datetime.fromisoformat(str(run.get("saved") or ""))
    except ValueError:
        return False
    return (now or datetime.now(timezone.utc)) - saved <= STALE_AFTER


def still_there(names: list, channels: list) -> list:
    """The names left to do whose channel is still in the server.

    One deleted just before the restart - after it went, before the step was
    written down - would otherwise come back as "no channel looks like that",
    and three of those in a row stop the run.
    """
    present = {str(one).casefold() for one in channels}
    return [one for one in names if str(one).casefold() in present]
