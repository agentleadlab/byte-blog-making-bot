"""The quiet run on its own clock - "i have to not click the button no more".

Every so often RYTE takes the next ten quiet channels by itself: keeps the
sheet and the screenshots, checks its own work, and deletes only what passes
every check. Nobody presses anything. What it remembers between runs is here:

- whether it is switched on (`@RYTE quiet auto off` / `on`),
- when it last ran, so a restart to pick up an update isn't a run of its own,
- which channels it left for a person, so the next run doesn't take the same
  ones again and again and never get further down the list.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .state import _state_dir

AUTO_PATH = _state_dir() / "quiet-auto.json"

#: How long between runs - ten channels each.
EVERY = timedelta(hours=1)

#: The hours it runs in, on the board's clock: when somebody is about to read
#: what it did, and to see it straight away if something it needs is down.
FROM_HOUR, UNTIL_HOUR = 8, 20

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


def is_on(held: dict | None = None) -> bool:
    """On unless somebody switched it off."""
    held = load() if held is None else held
    return held.get("on", True) is not False


def switch(on: bool, path: Path | None = None) -> None:
    held = load(path)
    held["on"] = bool(on)
    save(held, path)


def due(now: datetime, held: dict | None = None) -> bool:
    """Whether a run is due: on, inside the hours, and an hour since the last."""
    held = load() if held is None else held
    if not is_on(held) or not (FROM_HOUR <= now.hour < UNTIL_HOUR):
        return False
    try:
        last = datetime.fromisoformat(str(held.get("last") or ""))
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return now - last >= EVERY


def ran(now: datetime, path: Path | None = None) -> None:
    held = load(path)
    held["last"] = now.isoformat()
    save(held, path)


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
