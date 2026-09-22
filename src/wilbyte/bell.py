"""Who rang the bell, and what they said, remembered across restarts.

A client's own channel is the lead feed. What is worth keeping about them is
in ring-da-bell - "$1548 ethos aged 6/7", "$1440 trans aged lead", "$1960
fresh vet lead" - which is the client saying the leads worked, in their own
words, with the team's reactions on it. "WE want this type of screenshots".

Reading that channel per clear-out does not work. A thousand messages of it
is thirteen days, and a client who stopped buying in May is four months and
ten thousand messages down; a hundred and eighty clear-outs would each pay
that again. So it is read once, deeply, and what every person said is kept
here by their Discord id. After that a clear-out is a dictionary lookup, and
keeping up means reading only what has been said since.

Bounded on purpose. The last `KEEP_EACH` messages per person, and nothing
from a bot: this is a record of who sold what, not a copy of the channel.
"""

from __future__ import annotations

import json
from pathlib import Path

from .state import _state_dir

BELL_PATH = _state_dir() / "ring-da-bell.json"

#: How many of one person's messages to keep. Enough to show a client was
#: selling and roughly when they stopped, not enough to become an archive.
KEEP_EACH = 20


def load(path: Path | None = None) -> dict:
    """What is remembered: {"channels": {id: newest seen}, "said": {...}}."""
    where = path or BELL_PATH
    if not where.exists():
        return {"channels": {}, "said": {}}
    try:
        data = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A half-written file is the same as no file. Nothing here is the
        # only copy of anything - the channel is still there to read again.
        return {"channels": {}, "said": {}}
    if not isinstance(data, dict):
        return {"channels": {}, "said": {}}
    data.setdefault("channels", {})
    data.setdefault("said", {})
    return data


def save(data: dict, path: Path | None = None) -> None:
    where = path or BELL_PATH
    try:
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(json.dumps(data, indent=1), encoding="utf-8")
    except OSError:
        # Worth nothing and worth crashing nothing: the next run reads the
        # channel again rather than the file.
        pass


def since(data: dict, channel_id) -> str:
    """The newest message already read in this channel, or ""."""
    return str((data.get("channels") or {}).get(str(channel_id), "") or "")


def keep(data: dict, *, author_id, message_id, who, when, text, where, at) -> None:
    """Remember one thing somebody said, newest last, oldest dropped."""
    said = data.setdefault("said", {})
    mine = said.setdefault(str(author_id), [])
    if any(one.get("id") == str(message_id) for one in mine):
        return
    mine.append({
        "id": str(message_id), "who": who, "when": when,
        "text": text, "where": where, "at": at,
    })
    # By when it was said rather than by when it was read: the first deep
    # read walks backwards and every catch-up walks forwards, so appending
    # alone would leave one person's list in two directions at once.
    mine.sort(key=lambda one: str(one.get("at") or ""))
    said[str(author_id)] = mine[-KEEP_EACH:]


def read_to(data: dict, channel_id, message_id) -> None:
    """Remember how far this channel has been read."""
    if message_id:
        data.setdefault("channels", {})[str(channel_id)] = str(message_id)


def theirs(data: dict, author_id) -> list:
    """Everything remembered of one person's, oldest first."""
    return list((data.get("said") or {}).get(str(author_id), []))


def forget(data: dict, author_id) -> None:
    """Drop somebody once their channel has gone.

    A client who has been cleared out is not coming back, and a file that
    only ever grows is one nobody ever looks at.
    """
    (data.get("said") or {}).pop(str(author_id), None)
