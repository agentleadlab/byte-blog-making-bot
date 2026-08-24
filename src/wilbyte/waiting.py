"""Videos announced before YouTube had finished captioning them.

The announcement bot posts the moment a video goes up. YouTube's automatic
captions are not ready then - they take anywhere from a few minutes to an hour
on a ten-minute upload - so the run fails, and the only thing that gets the
post written is somebody noticing the red box and pasting the link back in
later. Which means it does not get written.

Nothing is wrong in that case, though, and treating it as a failure is what
makes it somebody's job. The video is simply early. So it goes on a list and
is tried again until the captions turn up or the wait runs out.

Kept in the state directory next to the ledger, so a restart doesn't lose the
queue - RYTE gets restarted more often than YouTube captions a video.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .state import _state_dir

WAITING_PATH = _state_dir() / "waiting-for-captions.json"

# How long to keep trying, and how often. YouTube usually has captions within
# the hour; past six the answer is that this video is not going to get them
# automatically, and saying so is more use than trying for another day.
RETRY_EVERY = timedelta(minutes=15)
GIVE_UP_AFTER = timedelta(hours=6)


@dataclass
class Waiting:
    """One video, and how long it has been early for."""

    url: str
    title: str = ""
    channel_id: int | None = None
    first_seen: str = ""
    last_tried: str = ""
    tries: int = 0

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "title": self.title,
            "channel_id": self.channel_id,
            "first_seen": self.first_seen,
            "last_tried": self.last_tried,
            "tries": self.tries,
        }


@dataclass
class Queue:
    path: Path = WAITING_PATH
    items: dict[str, Waiting] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> "Queue":
        where = path or WAITING_PATH
        if not where.exists():
            return cls(path=where)
        try:
            raw = json.loads(where.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls(path=where)
        items = {}
        for entry in raw if isinstance(raw, list) else []:
            if isinstance(entry, dict) and entry.get("url"):
                items[entry["url"]] = Waiting(
                    url=entry["url"],
                    title=entry.get("title", ""),
                    channel_id=entry.get("channel_id"),
                    first_seen=entry.get("first_seen", ""),
                    last_tried=entry.get("last_tried", ""),
                    tries=int(entry.get("tries", 0)),
                )
        return cls(path=where, items=items)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps([item.to_dict() for item in self.items.values()], indent=2),
            encoding="utf-8",
        )

    def add(self, url: str, *, title: str = "", channel_id: int | None = None, now=None) -> Waiting:
        """Start waiting on a video, or note another try on one already waiting."""
        moment = (now or datetime.now(timezone.utc)).isoformat()
        item = self.items.get(url)
        if item is None:
            item = Waiting(url=url, title=title, channel_id=channel_id, first_seen=moment)
            self.items[url] = item
        if title and not item.title:
            item.title = title
        item.last_tried = moment
        item.tries += 1
        self.save()
        return item

    def drop(self, url: str) -> None:
        if self.items.pop(url, None) is not None:
            self.save()

    def due(self, *, now=None) -> list[Waiting]:
        """The ones worth trying again, oldest first."""
        moment = now or datetime.now(timezone.utc)
        ready = [
            item for item in self.items.values()
            if not _too_soon(item, moment) and not _too_late(item, moment)
        ]
        return sorted(ready, key=lambda item: item.first_seen)

    def expired(self, *, now=None) -> list[Waiting]:
        """The ones that waited long enough. Reported once, then dropped."""
        moment = now or datetime.now(timezone.utc)
        return [item for item in self.items.values() if _too_late(item, moment)]


def _when(stamp: str):
    try:
        return datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None


def _too_soon(item: Waiting, now: datetime) -> bool:
    last = _when(item.last_tried)
    return bool(last and now - last < RETRY_EVERY)


def _too_late(item: Waiting, now: datetime) -> bool:
    first = _when(item.first_seen)
    return bool(first and now - first >= GIVE_UP_AFTER)


# A transcript that isn't there yet reads differently from one that can't be
# had at all. "No caption track published" is YouTube saying it hasn't got
# round to it; a 403 is it saying no.
NOT_YET = ("no caption track", "could not retrieve a transcript", "transcriptsdisabled")
REFUSALS = ("403", "permission", "private", "members-only", "age-restricted")


def not_ready_yet(problem: str) -> bool:
    """Whether this looks like captions that haven't appeared yet.

    Only worth waiting on when nothing in the message says the door is shut:
    a video whose captions are disabled outright will still have none in six
    hours, and retrying it twenty-four times is just noise.
    """
    said = (problem or "").casefold()
    if any(word in said for word in REFUSALS):
        return False
    return any(word in said for word in NOT_YET)
