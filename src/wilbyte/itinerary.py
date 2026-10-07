"""A trip itinerary sheet as a calendar file - "this schedule is Philippine
Time, i want ryte to add these to Tre's calendar but tre's calendar is on est".

Every row with a time becomes an event, stamped Asia/Manila. Whoever imports
the file sees it in their own time - Google Calendar converts it - so nothing
is converted here, and nothing can be converted wrong.

The sheet is laid out for people: a date on the first row of each day and
blank under it, day headings between the days, times written every way
("1:30 AM - 2:00 AM", "4:00:00 PM - 6:00 PM", "9:00PM +", "23:30"). Rows that
can't be placed on a day and a time are listed, never guessed at.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

ZONE = "Asia/Manila"

#: How long an event with no end time lasts - "9:00PM +".
OPEN_ENDED = timedelta(hours=1)

#: Categories that are not something to put in a calendar.
SKIPPED_CATEGORIES = ("sleeping time",)

_MONTHS = {name[:3]: n for n, name in enumerate(
    "january february march april may june july august september october november december".split(), start=1)}

_A_DATE = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s*(\d{4}))?")
_NUMERIC_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
_A_TIME = re.compile(r"(\d{1,2})(?::(\d{2}))?(?::\d{2})?\s*([AaPp]\.?[Mm]\.?)?")


@dataclass
class Event:
    day: date
    start: time
    end: time | None
    title: str
    where: str = ""
    category: str = ""
    links: str = ""
    row: int = 0

    def starts(self) -> datetime:
        return datetime.combine(self.day, self.start)

    def ends(self) -> datetime:
        if self.end is None:
            return self.starts() + OPEN_ENDED
        finish = datetime.combine(self.day, self.end)
        # "10:00 PM - 1:00 AM" runs past midnight.
        return finish if finish > self.starts() else finish + timedelta(days=1)


@dataclass
class Read:
    events: list = field(default_factory=list)
    #: (row number, what it said, why it was left out)
    skipped: list = field(default_factory=list)


def the_year(rows: list, *, today: date) -> int:
    """The year the sheet names anywhere - "Travel Dates: Oct 9 - Oct 21,2026"
    - else this year."""
    for row in rows[:6]:
        found = re.search(r"\b(20\d{2})\b", " ".join(row))
        if found:
            return int(found.group(1))
    return today.year


def as_date(said: str, *, year: int) -> date | None:
    said = " ".join(str(said or "").split())
    found = _A_DATE.search(said)
    if found and found.group(1)[:3].casefold() in _MONTHS:
        try:
            return date(int(found.group(3) or year), _MONTHS[found.group(1)[:3].casefold()], int(found.group(2)))
        except ValueError:
            return None
    found = _NUMERIC_DATE.search(said)
    if found:
        given = found.group(3)
        full = int(given) + (2000 if given and len(given) == 2 else 0) if given else year
        try:
            return date(full, int(found.group(1)), int(found.group(2)))
        except ValueError:
            return None
    return None


def _clock(said: str, *, meridiem: str = "") -> time | None:
    found = _A_TIME.fullmatch(said.strip())
    if not found:
        return None
    hour, minute = int(found.group(1)), int(found.group(2) or 0)
    half = (found.group(3) or meridiem or "").replace(".", "").casefold()
    if half.startswith("p") and hour < 12:
        hour += 12
    elif half.startswith("a") and hour == 12:
        hour = 0
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    return time(hour, minute)


def times(said: str) -> tuple[time | None, time | None]:
    """(start, end) out of "1:30 AM - 2:00 AM", "6:00PM - 9:00PM", "9:00PM +",
    "23:30". A start written without AM/PM takes the end's."""
    said = " ".join(str(said or "").replace("–", "-").replace("—", "-").split())
    # "9:00PM +", "6:00 PM onwards", "7:30 PM until late" - a start, no end.
    said = re.sub(r"\s*(?:\+|onwards?|and\s+(?:on|after)|until\s+late|till\s+late|-\s*late|late)\s*$",
                  "", said, flags=re.IGNORECASE).strip()
    if not said:
        return None, None
    parts = [one.strip() for one in re.split(r"\s*-\s*|\s+to\s+", said, maxsplit=1)]
    if len(parts) == 1:
        return _clock(parts[0]), None
    end = _clock(parts[1])
    meridiem = (re.search(r"[AaPp]\.?[Mm]", parts[1]) or [""])[0] if end else ""
    return _clock(parts[0], meridiem=meridiem if not re.search(r"[AaPp]\.?[Mm]", parts[0]) else ""), end


def _column(heads: list, *names) -> int | None:
    for at, head in enumerate(heads):
        if any(name in str(head).casefold() for name in names):
            return at
    return None


def read(rows: list, *, today: date) -> Read:
    """Every row with a day and a time, as an event; the rest, said."""
    found = Read()
    year = the_year(rows, today=today)
    header = next((at for at, row in enumerate(rows)
                   if _column(row, "time") is not None and _column(row, "activity") is not None), None)
    if header is None:
        found.skipped.append((0, "", "no heading row with Time and Activity in it"))
        return found
    heads = rows[header]
    at_date, at_time = _column(heads, "date"), _column(heads, "time")
    at_category, at_what = _column(heads, "category"), _column(heads, "activity")
    at_where, at_links = _column(heads, "location", "notes"), _column(heads, "link")

    def cell(row, at):
        return " ".join(str(row[at]).split()) if at is not None and at < len(row) else ""

    day = None
    for number, row in enumerate(rows[header + 1:], start=header + 2):
        if not any(str(one).strip() for one in row):
            continue
        written = as_date(cell(row, at_date), year=year)
        if written:
            day = written
        when, what = cell(row, at_time), cell(row, at_what)
        category = cell(row, at_category)
        if not when and not what and not category:
            continue  # a day's heading - "Day 2 (Friday) - PH Team Meetup"
        said = what or category or "?"
        if category.casefold() in SKIPPED_CATEGORIES:
            found.skipped.append((number, said, category))
            continue
        start, end = times(when)
        if day is None:
            found.skipped.append((number, said, "no date above it"))
            continue
        if start is None:
            found.skipped.append((number, said, "no time" if not when else f"couldn't read the time “{when}”"))
            continue
        found.events.append(Event(
            day=day, start=start, end=end, title=said, where=cell(row, at_where),
            category=category, links=cell(row, at_links), row=number,
        ))
    return found


def _escaped(text: str) -> str:
    return (str(text or "").replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n"))


def _folded(line: str) -> str:
    """Lines over 75 octets folded, as the calendar format asks."""
    out, chunk = [], ""
    for ch in line:
        if len((chunk + ch).encode("utf-8")) > 74:
            out.append(chunk)
            chunk = " " + ch
        else:
            chunk += ch
    out.append(chunk)
    return "\r\n".join(out)


def ics(events: list, *, name: str, now: datetime | None = None) -> str:
    """The calendar file. Stamped Asia/Manila; each event keeps the same UID
    every time it's made, so importing it again doesn't double it up."""
    now = now or datetime.utcnow()
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Agent Lead Lab//RYTE//EN",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH", f"X-WR-CALNAME:{_escaped(name)}",
        f"X-WR-TIMEZONE:{ZONE}",
        # Manila has no daylight saving: one standard rule covers every date.
        "BEGIN:VTIMEZONE", f"TZID:{ZONE}", "BEGIN:STANDARD", "DTSTART:19700101T000000",
        "TZOFFSETFROM:+0800", "TZOFFSETTO:+0800", "TZNAME:PHT", "END:STANDARD", "END:VTIMEZONE",
    ]
    for one in events:
        uid = hashlib.sha1(f"{name}|{one.starts():%Y%m%dT%H%M}|{one.title}".encode()).hexdigest()[:20]
        about = "\n".join(bit for bit in (one.category, one.links) if bit)
        lines += [
            "BEGIN:VEVENT", f"UID:{uid}@agentleadlab", f"DTSTAMP:{stamp}",
            f"DTSTART;TZID={ZONE}:{one.starts():%Y%m%dT%H%M%S}",
            f"DTEND;TZID={ZONE}:{one.ends():%Y%m%dT%H%M%S}",
            f"SUMMARY:{_escaped(one.title)}",
        ]
        if one.where:
            lines.append(f"LOCATION:{_escaped(one.where)}")
        if about:
            lines.append(f"DESCRIPTION:{_escaped(about)}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_folded(line) for line in lines) + "\r\n"
