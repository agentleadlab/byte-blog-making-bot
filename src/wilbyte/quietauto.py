"""What a quiet run left for a person, so the next one doesn't start on it.

`@RYTE quiet` goes down the list with RYTE pressing the buttons. A channel
that needed a person's eye - a note, a problem, a client still ordering -
is left as it is and listed. Remembered here for a week, or every run would
begin on the same ones and never get further down the list.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .state import _state_dir

AUTO_PATH = _state_dir() / "quiet-auto.json"

#: A channel left for a person isn't taken again for this long. Then it comes
#: back once more - somebody may have fixed what was wrong, and if not, it is
#: a reminder that it is still there.
LEAVE_FOR = timedelta(days=7)


def load(path: Path | None = None) -> dict:
    try:
        held = json.loads((path or AUTO_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return held if isinstance(held, dict) else {}


def save(held: dict, path: Path | None = None) -> None:
    where = path or AUTO_PATH
    where.parent.mkdir(parents=True, exist_ok=True)
    spare = where.with_suffix(".tmp")
    spare.write_text(json.dumps(held, indent=1), encoding="utf-8")
    spare.replace(where)


def leave_alone(now: datetime, held: dict | None = None) -> set[str]:
    """The channel ids a run doesn't take, because one was left for a person
    within the last week."""
    held = load() if held is None else held
    out = set()
    for channel_id, until in (held.get("leave") or {}).items():
        try:
            when = datetime.fromisoformat(str(until))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when > now:
            out.add(str(channel_id))
    return out


def left_for_a_person(channel_ids, now: datetime, path: Path | None = None) -> None:
    """Remember the channels this run left, and forget the ones long past."""
    held = load(path)
    keep = {key: value for key, value in (held.get("leave") or {}).items()
            if key in leave_alone(now, held)}
    until = (now + LEAVE_FOR).isoformat()
    keep.update({str(one): until for one in channel_ids})
    held["leave"] = keep
    save(held, path)
