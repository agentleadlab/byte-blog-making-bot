"""What RYTE noticed while working, kept so it can be said later.

He learns things all day and throws every one of them away. A lead type he
could not place, somebody tagged who keeps no checklist, an item carried for
the fifth night, a setup card whose line disagrees with the agent's own card -
each is handled in the moment and then gone, and the *pattern* in them never
reaches anybody. "PHX STNDRD" stopped him four separate times in a week and
the fix each time was a person noticing a screenshot.

So: a notebook. Every time something is worth noticing he writes a line in it,
which costs nothing and needs no thinking. Later - when asked, or once a day -
the notebook is read back and the repeats in it become suggestions.

He suggests. He does not act. Every entry here is a sentence to Franklin, and
what happens next is a conversation - "dont make him do it just suggest then
we'll brainstorm". Nothing in this module writes to the board, and nothing
that reads it is allowed to either.

An entry is (kind, subject, detail, day). The subject is what makes two
sightings the same sighting: the word he could not place, the person with no
checklist, the item being carried. Counting by subject is the whole point -
one is a Tuesday, five is a thing to fix.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .state import _state_dir

NOTICED_PATH = _state_dir() / "noticed.json"

# Kept for a month. Long enough that a weekly pattern shows up twice and a
# monthly one shows up at all; short enough that a word somebody fixed in
# August is not still being suggested in October.
KEEP_DAYS = 30

# How many sightings before it is worth saying. Once is a typo. Twice is
# somebody's habit, and habits are what a suggestion can actually change.
WORTH_SAYING = 2

# What he notices, and how a suggestion about it opens. The text is the whole
# reason each kind exists - a count with no sentence around it is a number
# nobody acts on.
KINDS = {
    "unplaced": (
        "lead-type wording I couldn't place",
        "Teach me it with `@RYTE words {subject} = ...` and it stops costing "
        "anybody a screenshot.",
    ),
    "no_checklist": (
        "tagged on a card but keeping no checklist",
        "Every one of those is a job that got said and never written down. "
        "A checklist on the card they work from would catch them.",
    ),
    "carried": (
        "carried from one day to the next, over and over",
        "Either it is stuck on somebody, or it is a piece of work too big to "
        "be a checklist line.",
    ),
    "conflict": (
        "set up on leads that don't match what the card says they bought",
        "Worth a look at where the two are being written, because one of "
        "them is wrong every time.",
    ),
    "ambiguous": (
        "a line that could be two different lead types",
        "Naming the tier on the setup card would settle it without anybody "
        "being asked.",
    ),
    "no_card": (
        "work that had nowhere to go, because the card for the day didn't exist",
        "The setup cards are made at six, two days out. One made late is one "
        "that spent its working day off the board.",
    ),
}

# Kinds nobody wants raised again, by (kind, subject). Dismissing is how a
# suggestion stops being a suggestion - without it the same line comes back
# every morning until it is easier to ignore RYTE than to read him.
HUSHED = "hushed"


@dataclass(frozen=True)
class Seen:
    """One thing worth saying, and how often it has happened."""

    kind: str
    subject: str
    times: int
    first: str
    last: str
    detail: str = ""

    def key(self) -> str:
        return f"{self.kind}::{self.subject}"


def load(path: Path | None = None) -> dict:
    """{"kind::subject": {"times": n, "first": ..., "last": ..., "detail": ...}}."""
    where = path or NOTICED_PATH
    if not where.exists():
        return {}
    try:
        data = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(book: dict, path: Path | None = None) -> None:
    where = path or NOTICED_PATH
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text(json.dumps(book, indent=2, sort_keys=True), encoding="utf-8")


def note(kind: str, subject: str, *, detail: str = "", on=None, path=None) -> dict:
    """Write one sighting down. Cheap, and safe to call from anywhere.

    Counted once a day per subject, not once per sighting: the tag watcher
    looks every minute and sees the same person with no checklist each time,
    which is one fact about Tuesday rather than four hundred.
    """
    if kind not in KINDS or not str(subject or "").strip():
        return load(path)

    today = str(on or date.today())
    book = load(path)
    key = f"{kind}::{str(subject).strip()}"
    entry = book.get(key) or {}
    if entry.get("last") != today:
        entry["times"] = int(entry.get("times", 0)) + 1
        entry["first"] = entry.get("first") or today
        entry["last"] = today
    if detail:
        entry["detail"] = detail
    book[key] = entry
    save(_forget_old(book, today), path)
    return book


def notes(kinds=None, *, since=None, path=None) -> list[Seen]:
    """Everything worth saying, most-seen first. Hushed subjects left out."""
    book = load(path)
    hushed = set(book.get(HUSHED) or []) if isinstance(book.get(HUSHED), list) else set()
    found = []
    for key, entry in book.items():
        if key == HUSHED or not isinstance(entry, dict):
            continue
        kind, _, subject = key.partition("::")
        if kind not in KINDS or key in hushed:
            continue
        if kinds and kind not in kinds:
            continue
        if since and str(entry.get("last") or "") < str(since):
            continue
        found.append(Seen(
            kind=kind, subject=subject,
            times=int(entry.get("times", 0)),
            first=str(entry.get("first") or ""),
            last=str(entry.get("last") or ""),
            detail=str(entry.get("detail") or ""),
        ))
    found.sort(key=lambda one: (-one.times, one.kind, one.subject))
    return found


def worth_saying(found: list[Seen], *, times: int = WORTH_SAYING) -> list[Seen]:
    """The ones seen often enough to be a pattern rather than a Tuesday."""
    return [one for one in found if one.times >= times]


def hush(kind: str, subject: str, *, path=None) -> bool:
    """Stop raising this one. Returns whether it was there to hush."""
    book = load(path)
    key = f"{kind}::{str(subject or '').strip()}"
    if key not in book:
        return False
    held = book.get(HUSHED)
    hushed = set(held) if isinstance(held, list) else set()
    hushed.add(key)
    book[HUSHED] = sorted(hushed)
    save(book, path)
    return True


def hush_subject(subject: str, *, path=None) -> list[str]:
    """Hush a subject whatever kind it was noticed under. What got hushed."""
    wanted = str(subject or "").strip().casefold()
    done = []
    for one in notes(path=path):
        if one.subject.casefold() == wanted and hush(one.kind, one.subject, path=path):
            done.append(one.key())
    return done


def clear(kind: str, subject: str, *, path=None) -> bool:
    """Forget one entirely - it was dealt with, so the count should restart."""
    book = load(path)
    key = f"{kind}::{str(subject or '').strip()}"
    if key not in book:
        return False
    book.pop(key)
    save(book, path)
    return True


def _forget_old(book: dict, today: str) -> dict:
    """Drop what has not been seen in a month, hushes included."""
    try:
        cutoff = str(date.fromisoformat(today).toordinal() - KEEP_DAYS)
    except ValueError:
        return book
    kept = {}
    alive = set()
    for key, entry in book.items():
        if key == HUSHED:
            continue
        if not isinstance(entry, dict):
            continue
        last = str(entry.get("last") or "")
        try:
            old = date.fromisoformat(last).toordinal() < int(cutoff)
        except ValueError:
            old = False
        if not old:
            kept[key] = entry
            alive.add(key)
    held = book.get(HUSHED)
    if isinstance(held, list):
        still = sorted(key for key in held if key in alive)
        if still:
            kept[HUSHED] = still
    return kept


def describe(one: Seen) -> str:
    """One line about one thing, for the message."""
    what, _advice = KINDS.get(one.kind, (one.kind, ""))
    when = f" since {one.first}" if one.first and one.first != one.last else ""
    said = f"**{one.subject}** — {what}, {one.times}×{when}"
    return f"{said}\n  {one.detail}" if one.detail else said


def nothing_yet() -> str:
    return (
        "Nothing worth raising yet. I write down what stops me as I go, and "
        "say something once the same thing has happened twice."
    )


def prompt(found: list[Seen]) -> str:
    """What to ask Claude to make of a notebook."""
    lines = []
    for one in found:
        what, advice = KINDS.get(one.kind, (one.kind, ""))
        lines.append(
            f"- [{one.kind}] “{one.subject}” — {what}. Seen {one.times} times, "
            f"{one.first} to {one.last}."
            + (f" Note: {one.detail}" if one.detail else "")
            + (f" Usual advice: {advice}" if advice else "")
        )
    return (
        "You keep the daily board for a lead-generation company. These are "
        "things that stopped you or slowed you down over the last few weeks, "
        "with how often each happened.\n\n"
        + "\n".join(lines)
        + "\n\nWrite at most four suggestions for the manager, best first. "
        "Each one: what you noticed, in one sentence, with the number in it; "
        "then what you would do about it, in one sentence. Group things that "
        "are really the same problem into one suggestion rather than listing "
        "them separately. Suggest only - you are not going to do any of it "
        "without being told to. If something here does not deserve a "
        "suggestion, leave it out rather than padding to four."
    )
