"""A go-live day changed after the agent was filed.

"people changes their live date sometimes even they got move to done ... tre
said he is to go live same day, it was change when it was move to done so
ryte dont have the ability to catch it". Filing reads the card once, puts the
agent on a setup card for that day and moves the card to Done - and nothing
looked at it again.

So the setup cards still to come are read back against the agents on them:
the newest comment on the agent's own card that says when they go live, and
the description when no comment does. An agent whose day no longer matches
the setup card they're on is said once, with who changed it and what they
wrote. Reads only - which card they belong on is a person's call.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo


@dataclass
class Moved:
    agent: str
    card_url: str
    setup_card: str
    checklist: str
    live: date
    #: Who said so and what they wrote - "" when it's the description.
    who: str = ""
    when: str = ""
    said: str = ""

    def key(self) -> str:
        return f"livemoved|{self.card_url}|{self.live.isoformat()}"


def _local(stamp: str, zone) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(stamp or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.astimezone(zone)


def current_launch(notes: list[dict], desc: str, *, made: date, timezone: str):
    """(the day they go live now, the comment that says so or None).

    The newest comment that names a day wins - it is the latest thing anybody
    said - read against the day it was written, so "same day" and "tomorrow"
    mean what they meant then. With no such comment, the description.
    """
    from . import agents as rules

    zone = ZoneInfo(timezone)
    for note in notes or []:  # newest first, as Trello gives them
        when = _local(note.get("when"), zone)
        if when is None:
            continue
        found = rules.find_launch(str(note.get("text") or ""), today=when.date())
        if found is not None:
            return found, note
    return rules.find_launch(desc or "", today=made), None


def describe(found: list[Moved]) -> str:
    lines = []
    for one in found:
        line = (f"• **[{one.agent}](<{one.card_url}>)** — on **{one.setup_card}**"
                + (f" ({one.checklist}'s list)" if one.checklist else "")
                + f", but now goes live **{one.live:%a %b %d}**")
        if one.who or one.said:
            quote = " ".join(one.said.split())
            quote = quote if len(quote) <= 120 else quote[:119] + "…"
            line += f" — {one.who or 'a comment'}{', ' + one.when if one.when else ''}: “{quote}”"
        else:
            line += " — per their card's description"
        lines.append(line)
    return ("📅 **Live date changed after filing:**\n" + "\n".join(lines)
            + "\n-# Move their line to the right setup card - and Lead Order, if it's been spread.")
