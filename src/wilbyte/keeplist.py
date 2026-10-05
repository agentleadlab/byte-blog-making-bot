"""Channels no clear-out ever touches - "leave mujeeb out", for good.

By the person, not just the channel: Mujeeb Anwari has two channels, and
keeping one while the other goes would be the same mistake made once. A
channel is kept when the person in its name is on the list, or when the
channel itself is.

Checked three times: when the quiet list is drawn up, at the start of a
clear-out, and at the moment of deleting. `@RYTE quiet keep <name>` adds,
`@RYTE quiet unkeep <name>` takes off, `@RYTE quiet kept` shows the list.
"""

from __future__ import annotations

import json
from pathlib import Path

from .state import _state_dir

KEEP_PATH = _state_dir() / "quiet-keep.json"

#: On the list from the start, before anybody has typed anything.
FROM_THE_START = {"people": ["mujeebanwari"], "channels": []}


def load(path: Path | None = None) -> dict:
    where = path or KEEP_PATH
    try:
        held = json.loads(where.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {key: list(value) for key, value in FROM_THE_START.items()}
    except (OSError, ValueError):
        # A list that can't be read is not an empty list. Keeping the ones
        # from the start is the least it can do; the run itself refuses to
        # go on (see `readable`).
        return {key: list(value) for key, value in FROM_THE_START.items()}
    if not isinstance(held, dict):
        return {key: list(value) for key, value in FROM_THE_START.items()}
    return {"people": [str(one) for one in held.get("people") or []],
            "channels": [str(one) for one in held.get("channels") or []]}


def readable(path: Path | None = None) -> bool:
    """Whether the list could be read - or has never been written."""
    where = path or KEEP_PATH
    if not where.exists():
        return True
    try:
        return isinstance(json.loads(where.read_text(encoding="utf-8")), dict)
    except (OSError, ValueError):
        return False


def save(held: dict, path: Path | None = None) -> None:
    where = path or KEEP_PATH
    where.parent.mkdir(parents=True, exist_ok=True)
    spare = where.with_suffix(".tmp")
    spare.write_text(json.dumps(held, indent=1), encoding="utf-8")
    spare.replace(where)


def kept(channel_id, name: str, held: dict | None = None) -> bool:
    """Whether this channel is on the keep list, by itself or by its person."""
    from .clearout import person_in

    held = load() if held is None else held
    if str(channel_id or "") and str(channel_id) in held.get("channels", []):
        return True
    person = person_in(str(name or ""))
    return bool(person) and person in held.get("people", [])


def keep(people=(), channels=(), path: Path | None = None) -> dict:
    held = load(path)
    held["people"] = sorted({*held["people"], *(str(one) for one in people if one)})
    held["channels"] = sorted({*held["channels"], *(str(one) for one in channels if one)})
    save(held, path)
    return held


def unkeep(people=(), channels=(), path: Path | None = None) -> dict:
    held = load(path)
    gone_people = {str(one) for one in people}
    gone_channels = {str(one) for one in channels}
    held["people"] = [one for one in held["people"] if one not in gone_people]
    held["channels"] = [one for one in held["channels"] if one not in gone_channels]
    save(held, path)
    return held
