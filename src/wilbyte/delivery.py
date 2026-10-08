"""Proof of delivery - "ordered 25, delivered 27 by Oct 2".

For each agent: what the card says they bought, against the rows on the
sheet they were given. Two uses. Somebody short is heard about before they
complain, once their leads should all be in. And when a dispute lands, the
one line that answers it is already written.

Reads only. Nothing here decides anything is wrong for certain: a short
sheet is said as a count, with the card and the sheet beside it, for a
person to look at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

#: Days after going live by which an order should be all in. Before then a
#: short sheet is leads still arriving, not leads missing.
DONE_WITHIN_DAYS = 7

#: How far back the daily look goes. Past this, a short sheet is old news
#: that somebody has already dealt with, or never will through this.
LOOK_BACK_DAYS = 21

# "25 Text Verified Veteran Leads", "30 more OTP vets", "Fresh Veterans: 35".
_COUNT_FIRST = re.compile(r"^\s*(\d{1,4})\b(?!\s*(?:/|%|\$|k\b|states?\b|days?\b|weeks?\b))", re.IGNORECASE)
_COUNT_AFTER = re.compile(r":\s*(\d{1,4})\s*(?:leads?)?\s*$", re.IGNORECASE)


def how_many(phrase: str) -> int | None:
    """How many leads the phrase says were bought, or None if it says none.

    A count at the front ("25 Text Verified Veteran Leads") or after a colon
    ("Fresh Veterans: 35"). Nothing else: "$350/week" is money and "3 states"
    is states, and a guessed number turns into a short sheet that isn't.
    """
    said = " ".join((phrase or "").split())
    found = _COUNT_FIRST.match(said) or _COUNT_AFTER.search(said)
    if not found:
        return None
    many = int(found.group(1))
    return many if 0 < many <= 5000 else None


#: A column that dates each lead.
_DATE_HEADING = re.compile(r"\b(?:date|dated|created|received|delivered|time\s*stamp|timestamp|added)\b", re.IGNORECASE)

_FORMATS = (
    "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %I:%M:%S %p",
    "%b %d, %Y", "%B %d, %Y", "%d %b %Y",
)


def as_day(cell) -> date | None:
    said = " ".join(str(cell or "").split())
    if not said:
        return None
    said = re.sub(r"(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})$", "", said)
    for shape in _FORMATS:
        try:
            return datetime.strptime(said, shape).date()
        except ValueError:
            continue
    return None


def date_column(heads: list, rows: list) -> int | None:
    """Which column dates each lead - headed like a date, and mostly holding
    dates - or None."""
    for at, head in enumerate(heads):
        if not _DATE_HEADING.search(str(head or "")):
            continue
        cells = [row[at] for row in rows if at < len(row) and str(row[at]).strip()]
        if cells and sum(as_day(one) is not None for one in cells) * 2 >= len(cells):
            return at
    return None


@dataclass
class Counted:
    """What one lead sheet holds."""

    rows: int = 0
    tab: str = ""
    #: The rows that carry a date, by the date - empty when the sheet has no
    #: column dating its leads.
    by_day: dict = field(default_factory=dict)

    @property
    def first(self) -> date | None:
        return min(self.by_day) if self.by_day else None

    @property
    def last(self) -> date | None:
        return max(self.by_day) if self.by_day else None

    def since(self, day: date | None) -> int | None:
        """Rows dated on or after `day`, or None when nothing is dated -
        a sheet carried over from an earlier order counts only this one."""
        if day is None or not self.by_day:
            return None
        return sum(many for when, many in self.by_day.items() if when >= day)


#: What a lead sheet's heading row says. Two of these in one row is it.
_LEAD_HEADING = re.compile(r"^\s*(?:(?:first\s+|full\s+|last\s+)?name|e-?mail|phone(?:\s+number)?|state|age|dob)\s*$",
                           re.IGNORECASE)

#: Rows on a sheet that aren't leads: the test rows the sheet is set up with,
#: and the banner telling the agent where to dispo a sale.
_NOT_A_LEAD = re.compile(r"^\s*test(?:\s+lead)?\s*$|^test@|when\s+you\s+make\s+a\s+sale", re.IGNORECASE)


def _heading_row(rows: list) -> int:
    """Which row the lead columns are headed on. Tavin Dougher's sheet has a
    colour key and two banners above it - row 10, not row 1 - and counting
    from row 1 made those nine lines leads."""
    for at, row in enumerate(rows[:40]):
        if sum(bool(_LEAD_HEADING.match(str(cell or ""))) for cell in row) >= 2:
            return at
    return 0


def count_rows(rows: list, *, tab: str = "") -> Counted:
    """The lead rows on a sheet: everything under the heading row that has
    anything in it - not the test leads it was set up with, not a banner."""
    if not rows:
        return Counted(tab=tab)
    top = _heading_row(rows)
    heads = rows[top]
    body = [
        row for row in rows[top + 1:]
        if any(str(cell).strip() for cell in row)
        and not any(_NOT_A_LEAD.search(str(cell or "")) for cell in row[:3])
    ]
    at = date_column(heads, body)
    by_day: dict = {}
    if at is not None:
        for row in body:
            when = as_day(row[at]) if at < len(row) else None
            if when is not None:
                by_day[when] = by_day.get(when, 0) + 1
    return Counted(rows=len(body), tab=tab, by_day=by_day)


@dataclass
class Delivery:
    """One order, against its sheet."""

    agent: str
    card_url: str = ""
    card_id: str = ""
    #: What the card says they bought, as written, and the count in it.
    ordered: str = ""
    many: int | None = None
    launch: date | None = None
    sheet: str = ""
    counted: Counted | None = None
    problems: list = field(default_factory=list)

    @property
    def delivered(self) -> int | None:
        """This order's rows: those dated since it went live, if the sheet
        dates its leads, else every row on it."""
        if self.counted is None:
            return None
        dated = self.counted.since(self.launch)
        return dated if dated is not None else self.counted.rows

    def short_by(self) -> int:
        if self.many is None or self.delivered is None:
            return 0
        return max(0, self.many - self.delivered)

    def due(self, today: date) -> bool:
        """Whether every lead should be in by now."""
        return self.launch is not None and (today - self.launch).days >= DONE_WITHIN_DAYS


def describe(one: Delivery, *, today: date) -> str:
    """One line: "ordered 25, delivered 27 by Oct 2"."""
    bits = []
    if one.many is not None:
        bits.append(f"ordered **{one.many}**" + (f" ({one.ordered})" if one.ordered else ""))
    elif one.ordered:
        bits.append(f"ordered {one.ordered} (no count on the card)")
    else:
        bits.append("nothing on the card says what they ordered")
    got = one.delivered
    if got is None:
        bits.append("sheet not read")
    else:
        counted = one.counted
        last = f" by {counted.last:%b %-d}" if counted and counted.last else ""
        bits.append(f"delivered **{got}**{last}")
        if counted and counted.since(one.launch) is not None and counted.rows != got:
            bits.append(f"{counted.rows} rows on the sheet in all")
    if one.launch:
        days = (today - one.launch).days
        bits.append(f"live {one.launch:%b %-d}" + (f" ({days} days ago)" if days > 0 else ""))
    mark = "⚠" if one.short_by() and one.due(today) else ("⏳" if one.short_by() else "✅")
    if one.many is None or got is None:
        mark = "•"
    line = f"{mark} **{one.agent}** — " + ", ".join(bits)
    if one.short_by():
        line += f" — **{one.short_by()} short**" + ("" if one.due(today) else ", still within the first week")
    links = [f"[card](<{one.card_url}>)" if one.card_url else "", f"[sheet](<{one.sheet}>)" if one.sheet else ""]
    links = [one for one in links if one]
    if links:
        line += " · " + " · ".join(links)
    return line


def for_a_dispute(one: Delivery) -> str:
    """The one line of evidence - "Ordered 25, delivered 27 by 10/02/2026"."""
    got = one.delivered
    if got is None:
        return ""
    last = one.counted.last if one.counted else None
    said = f"Delivered {got} lead{'s' if got != 1 else ''}" + (f" by {last:%m/%d/%Y}" if last else "")
    if one.many is not None:
        said = f"Ordered {one.many}, " + said[0].lower() + said[1:]
        if got > one.many:
            said += f" ({got - one.many} more than ordered)"
    return said + "."


# ---------------------------------------------------------------- said once

import json  # noqa: E402
from pathlib import Path  # noqa: E402

from .state import _state_dir  # noqa: E402

SEEN_PATH = _state_dir() / "delivery-flagged.json"


def flagged(path: Path | None = None) -> dict:
    """{card id: how many short it was said to be} - each short order said once."""
    try:
        held = json.loads((path or SEEN_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return held if isinstance(held, dict) else {}


def remember(card_ids: dict, path: Path | None = None) -> None:
    where = path or SEEN_PATH
    held = {**flagged(where), **{str(key): value for key, value in card_ids.items()}}
    where.parent.mkdir(parents=True, exist_ok=True)
    spare = where.with_suffix(".tmp")
    spare.write_text(json.dumps(held, indent=1), encoding="utf-8")
    spare.replace(where)
