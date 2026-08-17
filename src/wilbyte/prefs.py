"""Settings RYTE can change about itself, kept out of the tracked config.

`config/wilbyte.toml` is in git, so editing it on the Mac to move the calendar
would stop the auto-update from fast-forwarding and quietly strand RYTE on old
code - the exact failure this project has already lost a day to. These live in
the state directory instead, where nothing tracks them and nothing conflicts.

Only settings that legitimately change week to week belong here. Anything
structural stays in the config file, where it is reviewable.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import Config
from .state import _state_dir

PREFS_PATH = _state_dir() / "preferences.json"

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


class PrefsError(ValueError):
    """Raised when a setting can't be understood."""


def load(path: Path | None = None) -> dict:
    """Read the preferences. A missing or broken file is simply no preferences."""
    prefs_path = path or PREFS_PATH
    if not prefs_path.exists():
        return {}
    try:
        data = json.loads(prefs_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(values: dict, path: Path | None = None) -> None:
    prefs_path = path or PREFS_PATH
    prefs_path.parent.mkdir(parents=True, exist_ok=True)
    prefs_path.write_text(json.dumps(values, indent=2), encoding="utf-8")


def set_earliest_day(day: date, path: Path | None = None) -> None:
    """Don't schedule anything before this day."""
    values = load(path)
    values["earliest_day"] = day.isoformat()
    save(values, path)


def clear_earliest_day(path: Path | None = None) -> None:
    values = load(path)
    values.pop("earliest_day", None)
    save(values, path)


def apply(config: Config, path: Path | None = None) -> Config:
    """Overlay the saved preferences onto a config, if there are any."""
    earliest = str(load(path).get("earliest_day") or "").strip()
    if not earliest:
        return config
    return replace(config, schedule=replace(config.schedule, earliest_day=earliest))


def parse_day(text: str, *, today: date | None = None) -> date:
    """Read a date the way someone would type it into chat.

    `2026-08-18`, `Aug 18`, `18 Aug`, `8/18`, `today`, `tomorrow`, `monday`.
    A bare month and day means the next such day, not one in the past: typing
    "Aug 18" in December means next August, and typing it in August means now.
    """
    now = today or date.today()
    raw = text.strip().lower().strip(",.")
    if not raw:
        raise PrefsError("Give me a day, like `Aug 18` or `2026-08-18`.")

    if raw in ("today", "now"):
        return now
    if raw == "tomorrow":
        return now + timedelta(days=1)

    weekday = _weekday(raw, now)
    if weekday:
        return weekday

    try:
        return date.fromisoformat(raw)
    except ValueError:
        pass

    found = _month_and_day(raw)
    if not found:
        raise PrefsError(
            f"I couldn't read {text.strip()!r} as a day. Try `Aug 18`, `2026-08-18`, "
            f"`today` or `tomorrow`."
        )
    month, day = found
    for year in (now.year, now.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError as exc:
            raise PrefsError(f"There's no such day as {text.strip()!r}.") from exc
        # Six months' grace, so "Aug 18" typed on Aug 20 still means this year
        # rather than jumping eleven months forward.
        if candidate >= now - timedelta(days=183):
            return candidate
    raise PrefsError(f"I couldn't place {text.strip()!r} on the calendar.")


def _month_and_day(raw: str) -> tuple[int, int] | None:
    numeric = re.fullmatch(r"(\d{1,2})\s*[/-]\s*(\d{1,2})", raw)
    if numeric:
        return int(numeric.group(1)), int(numeric.group(2))

    words = re.findall(r"[a-z]+|\d{1,2}", raw)
    month = next((MONTHS[w[:3]] for w in words if w[:3] in MONTHS and w.isalpha()), None)
    day = next((int(w) for w in words if w.isdigit()), None)
    if month and day:
        return month, day
    return None


def _weekday(raw: str, now: date) -> date | None:
    names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for index, name in enumerate(names):
        if raw in (name, name[:3]):
            ahead = (index - now.weekday()) % 7
            return now + timedelta(days=ahead or 7)
    return None


def describe(config: Config, path: Path | None = None) -> str:
    """What the floor currently is, in words, for a confirmation message."""
    earliest = str(load(path).get("earliest_day") or "").strip()
    if not earliest:
        return "No earliest day set — I'll use the next free weekday."
    try:
        day = datetime.fromisoformat(earliest).date()
    except ValueError:
        return f"Earliest day is set to {earliest}."
    return f"I won't schedule anything before {day:%a %b %d, %Y}."
