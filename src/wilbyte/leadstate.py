"""Masterlists told to sit on the other tab.

Which tab a masterlist belongs on comes from its Discord category, and that is
the right default: the team already maintains those categories and moving a
channel into INACTIVE is a decision somebody made deliberately.

But "@RYTE move the trucker masterlist to inactive" has to work without RYTE
restructuring somebody else's Discord server. It is in that server with
View Channels and Read Message History and nothing else, on purpose - a bot
that can move channels around is a bot that can move the wrong one.

So this is an override, kept here: a name said out loud beats the category it
sits in, until somebody says the opposite. No expiry, unlike the rollover
skips - "that lead type is finished" is a standing fact, not tonight's
instruction.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .state import _state_dir

STATE_PATH = _state_dir() / "masterlist-state.json"

INACTIVE = "inactive"
ACTIVE = "active"


def key(name: str) -> str:
    """A masterlist name reduced to what makes it that masterlist.

    Said out loud it is "the otp trucker iul masterlist"; on Discord it is
    "🚚 otp-trucker-iul-masterlist"; in the sheet it is "OTP Trucker IUL". All
    three have to land on the same entry.
    """
    text = re.sub(r"[^a-z0-9]+", " ", (name or "").lower())
    text = re.sub(r"\bmaster\s*lists?\b|\bmasterfiles?\b|\bleads?\b", " ", text)
    return " ".join(text.split()).removeprefix("the ").strip()


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load(path: Path | None = None) -> dict[str, str]:
    """Every override, by name key -> "active" or "inactive"."""
    held = _read(path or STATE_PATH)
    return {
        str(name): str(state)
        for name, state in held.items()
        if str(state) in (ACTIVE, INACTIVE)
    }


def set_state(name: str, state: str, path: Path | None = None) -> str:
    """Remember that a masterlist is live, or isn't. Returns the key set."""
    if state not in (ACTIVE, INACTIVE):
        raise ValueError(f"Not a state a masterlist can be in: {state!r}")

    where = path or STATE_PATH
    held = load(where)
    wanted = key(name)
    held[wanted] = state
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text(json.dumps(held, indent=2, sort_keys=True), encoding="utf-8")
    return wanted


def clear(name: str, path: Path | None = None) -> None:
    """Forget an override, so the Discord category decides again."""
    where = path or STATE_PATH
    held = load(where)
    if held.pop(key(name), None) is None:
        return
    where.write_text(json.dumps(held, indent=2, sort_keys=True), encoding="utf-8")


def state_of(name: str, *, held: dict[str, str] | None = None) -> str | None:
    """"active", "inactive", or None when nobody has said."""
    return (held if held is not None else load()).get(key(name))


# --------------------------------------------------- the sheet to count
#
# Nearly every masterlist channel posts an auto-deploy sheet rather than its
# masterlist, so the link in the channel is usually the wrong one. Where
# somebody has said which sheet a lead type really keeps its leads in, that is
# kept here and it beats anything found in Discord or in the folder.

SHEETS_PATH = _state_dir() / "masterlist-sheets.json"


def sheets(path: Path | None = None) -> dict[str, str]:
    """Every pinned sheet, by name key -> its URL."""
    held = _read(path or SHEETS_PATH)
    return {str(name): str(url) for name, url in held.items() if str(url).strip()}


def set_sheet(name: str, url: str, path: Path | None = None) -> str:
    """Remember which sheet a lead type's leads are actually in."""
    where = path or SHEETS_PATH
    held = sheets(where)
    wanted = key(name)
    held[wanted] = url.strip()
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text(json.dumps(held, indent=2, sort_keys=True), encoding="utf-8")
    return wanted


def sheet_of(name: str, *, held: dict[str, str] | None = None) -> str:
    """The pinned sheet for a lead type, or "" when nobody has said."""
    return (held if held is not None else sheets()).get(key(name), "")


# "OTP Trucker IUL https://docs.google.com/spreadsheets/d/…" - a name and a
# link, in either order, is somebody telling RYTE where the leads really are.
_A_SHEET = re.compile(r"https://docs\.google\.com/spreadsheets/d/[A-Za-z0-9_-]{20,}\S*")
_FILLER = re.compile(
    r"^\s*(?:masterlists?|sheet|for|is|use|set|link|the|to|:|-|–|—)\b\W*", re.IGNORECASE
)
# The same words turn up after the name as easily as before it: "masterlist
# OTP FEX <link>" and "OTP VET 2 sheet is <link>" are the same instruction.
_TRAILING = re.compile(
    r"\W*\b(?:masterlists?|sheet|for|is|are|use|set|link|the|to)\s*$", re.IGNORECASE
)


def sheet_asked(text: str) -> tuple[str, str] | None:
    """(the lead type named, the sheet URL), or None when it isn't that.

    Both halves are required. A link with no name says nothing about which
    lead type it belongs to, and a name with no link is not this command.
    """
    said = " ".join((text or "").split())
    found = _A_SHEET.search(said)
    if not found:
        return None

    named = (said[: found.start()] + " " + said[found.end():]).strip()
    while True:
        shorter = _TRAILING.sub("", _FILLER.sub("", named)).strip(" -–—:,")
        if shorter == named:
            break
        named = shorter
    named = key(named)
    return (named, found.group(0)) if named else None


# "move the otp trucker masterlist to inactive", "move otp trucker to active".
_MOVE = re.compile(
    r"\b(?:to|as|is)\s+(?P<state>in\s*active|active)\b",
    re.IGNORECASE,
)
_LEADING = re.compile(r"^\s*(?:move|mark|set|put)\s+(?:the\s+)?", re.IGNORECASE)


def move_asked(text: str) -> tuple[str, str] | None:
    """(the masterlist named, the state wanted), or None when it isn't that.

    The state has to be said - "move the trucker masterlist" on its own is not
    an instruction anybody could carry out, and guessing which way somebody
    meant is how a live lead type disappears off the list.
    """
    said = " ".join((text or "").split())
    found = _MOVE.search(said)
    if not found:
        return None

    state = INACTIVE if "in" in found.group("state").lower().replace(" ", "")[:2] else ACTIVE
    named = _LEADING.sub("", said[: found.start()]).strip(" -–—:,")
    named = key(named)
    return (named, state) if named else None
