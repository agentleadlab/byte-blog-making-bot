"""The down alarm - "right now, if Ryte goes down, nobody finds out".

When the Mac sleeps or the wifi drops, RYTE can't say so: it is the thing
that is down. Two halves, then.

- While it's up, a check-in every five minutes to an outside service
  (healthchecks.io, free) at HEALTHCHECK_URL. When the check-ins stop, that
  service is the one that texts or emails Franklin.
- When it comes back, RYTE says how long it was gone, so whatever should
  have happened in between gets a look.

The check-in is a GET with nothing in it - no names, no data, just "alive".
"""

from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .state import _state_dir

log = logging.getLogger("wilbyte.bot")

ALIVE_PATH = _state_dir() / "alive.json"

#: How often RYTE checks in, and writes down that it is up.
BEAT_SECONDS = 300

#: Gone longer than this is worth saying when it comes back. A restart onto
#: an update is seconds; this is the Mac asleep, or the wifi gone.
DOWN_AFTER = timedelta(minutes=15)


def last_seen(path: Path | None = None) -> datetime | None:
    try:
        held = json.loads((path or ALIVE_PATH).read_text(encoding="utf-8"))
        moment = datetime.fromisoformat(str(held.get("last") or ""))
    except (OSError, ValueError, AttributeError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def beat(now: datetime, path: Path | None = None) -> None:
    where = path or ALIVE_PATH
    where.parent.mkdir(parents=True, exist_ok=True)
    spare = where.with_suffix(".tmp")
    spare.write_text(json.dumps({"last": now.isoformat()}), encoding="utf-8")
    spare.replace(where)


def was_down(now: datetime, last: datetime | None) -> timedelta | None:
    """How long RYTE was gone, if long enough to say - else None."""
    if last is None:
        return None
    gone = now - last
    return gone if gone > DOWN_AFTER else None


def how_long(gone: timedelta) -> str:
    minutes = int(gone.total_seconds() // 60)
    days, minutes = divmod(minutes, 60 * 24)
    hours, minutes = divmod(minutes, 60)
    bits = [f"{days} day{'s' if days != 1 else ''}" if days else "",
            f"{hours} h" if hours else "", f"{minutes} min" if minutes or not (days or hours) else ""]
    return " ".join(bit for bit in bits if bit)


def back_up(last: datetime, now: datetime, zone) -> str:
    """"I was down from 2:10 PM to 4:45 PM (2 h 35 min)"."""
    since, until = last.astimezone(zone), now.astimezone(zone)
    day = "" if since.date() == until.date() else f"{since:%a %b %-d} "
    return (f"⚠ **I was down** from {day}{since:%-I:%M %p} to {until:%-I:%M %p} "
            f"({how_long(now - last)}). Anything that should have happened in between "
            "may need a look — I've caught up on @mentions, and the checks run again now.")


def check_in(url: str, *, timeout: float = 10) -> bool:
    """Tell the outside service RYTE is alive. Never raises."""
    url = (url or "").strip()
    if not url.startswith("https://"):
        return False
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=timeout) as answer:
            return 200 <= getattr(answer, "status", 200) < 300
    except Exception as exc:
        log.warning("Couldn't check in at the down alarm: %s", exc)
        return False
