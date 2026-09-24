"""How each tagged comment was read, so it is read the same way every time.

Every look at the day's tags sent the whole batch to Claude to be read fresh,
and two looks at the same moment - the watcher's first pass after a restart,
and "@RYTE tags" typed alongside it - read Arnold's "Tony and Overnight
Orders" two different ways seconds apart: "Tony order top priority, others
who paid last night second" in one, "Everyone else who paid last night is
second" in the other. The second took the wrong half, and it is the one that
would have gone on KC's checklist.

So the first reading of a comment is kept, and every later look reuses it.

Keyed on the comment's words as well as its id: an edited comment is a
different comment, and is read again. And on who keeps a checklist that day,
because who a job belongs to is decided against that list - a comment read
when Faith had no checklist is worth reading again once she has one.

Bounded, because a file that only grows is one nobody ever looks at. Nothing
in here is the only copy of anything: forgetting a reading costs one more
call to Claude, not a lost task.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .state import _state_dir

READS_PATH = _state_dir() / "tag-reads.json"

#: How many comments' readings are kept. A busy day is a few dozen comments
#: across the three cards; this is weeks of them.
KEEP = 2000


def people_key(people) -> str:
    """Who keeps a checklist, as one short string."""
    return ",".join(sorted(str(one).casefold() for one in (people or {})))


def key_for(comment_id: str, text: str, who: str) -> str:
    """One reading's key: the comment, its words, and the day's people."""
    said = " ".join(str(text or "").split())
    digest = hashlib.sha1(f"{said}\x00{who}".encode("utf-8")).hexdigest()[:16]
    return f"{comment_id}:{digest}"


def load(path: Path | None = None) -> dict:
    """{key: [line, ...]} - an empty list is a comment read and found to hold
    no job for anybody, which is an answer worth keeping too."""
    where = path or READS_PATH
    if not where.exists():
        return {}
    try:
        data = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A half-written file is the same as no file: everything is read
        # again, once.
        return {}
    return data if isinstance(data, dict) else {}


def save(data: dict, path: Path | None = None) -> None:
    where = path or READS_PATH
    # Oldest out first. Dicts keep the order they were filled in.
    kept = dict(list(data.items())[-KEEP:])
    try:
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(json.dumps(kept), encoding="utf-8")
    except OSError:
        # Worth nothing and worth crashing nothing: the next look reads the
        # comments again rather than the file.
        pass
