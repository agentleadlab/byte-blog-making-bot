"""What one teammate asks another to do, on the Ryte Checklist card.

"add this on our card's checklist right and ryte will be on the look out once
nicole added a emoji or say this is done". Faith tags Nicole in the Growth
server - "@Nicole greff", Michael Greff's texts forwarded, 10/day max - and
that is a job for Nicole. RYTE puts it under Concerns Needs to Be Addressed
on the card - agent work only, not the team's own - and ticks it when Nicole
reacts to the request or says it's done. Still open after a while, Franklin
is pinged in Ryte The Goat and reminds them himself.

Only the person asked can close it: an emoji from anybody else is somebody
agreeing, not the job being done. RYTE never posts or reacts in the Growth
server for any of this; it reads there and writes on the card.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

#: The card's checklist these go on - "add them on the ryte checklist on
#: concerns needs to be addressed".
TASKS_LIST = "Concerns Needs to Be Addressed"
#: Open this long, and it's in the 6pm list.
OPEN_TOO_LONG = timedelta(hours=4)
#: Closed this long ago, and it's forgotten - the card keeps the record.
FORGET_AFTER = timedelta(days=30)

_DONE = re.compile(
    r"(?:\b(?:done|did it|completed?|finished|all set|taken care of|fixed|updated|"
    r"set(?:\s+(?:it|that|him|her|them))?(?:\s+up)?|capped|added|sorted|handled)\b|✅|☑️|✔️?)",
    re.IGNORECASE,
)
_NOT_YET = re.compile(r"\b(?:not|n't|will|gonna|going to|about to|later|tomorrow|soon|asap|when)\b", re.IGNORECASE)


def said_done(text: str) -> bool:
    """"done", "all set", "capped him at 10" - and not "will do", "not done
    yet", "I'll set it up later"."""
    said = " ".join(str(text or "").split())
    if not said or not _DONE.search(said):
        return False
    return not _NOT_YET.search(said)


def item_name(task: dict) -> str:
    """"Michael Greff - set a 10/day cap · @Nicole (asked by Faith 10/13)"."""
    who = ", ".join(one.get("name", "") for one in task.get("assignees") or [] if one.get("name"))
    when = ""
    try:
        when = f" {datetime.fromisoformat(task['made']):%m/%d}"
    except (KeyError, ValueError, TypeError):
        pass
    asker = task.get("asker") or "?"
    return f"{task.get('title') or 'Team request'} · {who or 'the team'} (asked by {asker}{when})"


def done_name(task: dict) -> str:
    """The item once it's done: who did it and when, for the record."""
    when = ""
    try:
        when = f" {datetime.fromisoformat(task['done_at']):%m/%d %-I:%M%p}".lower()
    except (KeyError, ValueError, TypeError):
        pass
    return f"{item_name(task)} — done by {task.get('done_by') or '?'}{when}"


#: What a reaction has to be to close a request asked of nobody in
#: particular (@here, a role): a 💙 on it is somebody seeing it, not doing it.
CHECKS = ("✅", "☑️", "✔️", "✔", "white_check_mark", "heavy_check_mark", "ballot_box_with_check")


def reaction_closes(task: dict, person_id, emoji) -> bool:
    """Whether this reaction ticks it off: any emoji from somebody it was
    asked of; a ✅ from anybody but the asker when it was asked of the room."""
    if not closes_it(task, person_id):
        return False
    if task.get("assignees"):
        return True
    name = str(getattr(emoji, "name", None) or emoji or "")
    return name in CHECKS or name.rstrip("\ufe0f") in CHECKS


def closes_it(task: dict, person_id: int | str) -> bool:
    """Whether this person finishing it closes it: somebody it was asked of -
    or, when it named nobody, anybody but the one who asked."""
    ids = {str(one.get("id")) for one in task.get("assignees") or []}
    if not ids:
        return str(person_id) != str(task.get("asker_id"))
    return str(person_id) in ids


def open_ones(held: dict) -> list[dict]:
    return [one for one in held.values() if isinstance(one, dict) and not one.get("done_at")]


def in_place(held: dict, channel_id) -> list[dict]:
    """The open ones asked in this channel or thread."""
    return [one for one in open_ones(held) if str(one.get("channel_id")) == str(channel_id)]


def overdue(held: dict, now: datetime) -> list[dict]:
    late = []
    for one in open_ones(held):
        try:
            made = datetime.fromisoformat(one["made"])
        except (KeyError, ValueError, TypeError):
            continue
        made = made if made.tzinfo else made.replace(tzinfo=timezone.utc)
        if now - made >= OPEN_TOO_LONG:
            late.append(one)
    return late


def tidy(held: dict, now: datetime) -> dict:
    """Done ones from over a month ago dropped."""
    kept = {}
    for key, one in held.items():
        if not isinstance(one, dict):
            kept[key] = one
            continue
        try:
            done = datetime.fromisoformat(one["done_at"]) if one.get("done_at") else None
        except (ValueError, TypeError):
            done = None
        if done is not None:
            done = done if done.tzinfo else done.replace(tzinfo=timezone.utc)
            if now - done > FORGET_AFTER:
                continue
        kept[key] = one
    return kept


# ------------------------------------------------------------- remembered

PATH = None


def _where():
    from .state import _state_dir

    return PATH or (_state_dir() / "team-tasks.json")


def load() -> dict:
    import json

    try:
        held = json.loads(_where().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return held if isinstance(held, dict) else {}


def save(held: dict) -> None:
    import json

    where = _where()
    where.parent.mkdir(parents=True, exist_ok=True)
    spare = where.with_suffix(".tmp")
    spare.write_text(json.dumps(held, indent=1, default=str), encoding="utf-8")
    spare.replace(where)


#: The day the 6pm list last went on the card.
SAID_PATH = None


def _said_where():
    from .state import _state_dir

    return SAID_PATH or (_state_dir() / "team-tasks-evening.txt")


def said_today() -> str:
    try:
        return _said_where().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def said_on(day: str) -> None:
    where = _said_where()
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text(day, encoding="utf-8")
