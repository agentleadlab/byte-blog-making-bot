"""The Agent Lead Lab daily board routine, as rules rather than clicks.

Four dated cards walk In Que -> Today -> Quality Check each day. By half
past eight every
item still unticked has to land on tomorrow's card, on the *same person's*
checklist and the *same card type*. Nicole's unfinished General items go to
Nicole's checklist on tomorrow's General card - not into Ops, not into a
general pile.

Everything here is pure: it takes the card and checklist dicts Trello returns
and works out what should happen. Nothing in this module writes to the board,
so the rules can be tested without an API key and a plan can be shown before
anything moves.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

# The four cards, by the words that identify them. Emoji are not part of the
# match: they get pasted inconsistently and a card is not a different card for
# having lost its gem.
CARD_KINDS = {
    "general": "💎 General",
    "ops": "💻 Ops",
    "ads": "📊 Ads",
    "lead_order": "Lead Order",
}

# The day, as three moves and a carry-over. From how it is actually run:
# "from In Que, by 9am the cards are moved to Today; Today by 6pm the cards are
# moved to Quality Check; by 8:30pm all checklist items that aren't checked are
# moved to next day's cards, which are on In Que."
IN_QUE = "In Que"
TODAY = "Today"
QUALITY_CHECK = "Quality Check"
DONE = "Done"

# The two afternoon looks at Done, named apart because the clock remembers
# what has run by name and each has to happen once.
UNMARKED = ("unmarked_agents_1530", "unmarked_agents_1730")

# The one list the nightly archive is allowed to touch, by name. Nothing else
# on the board is read by it, let alone archived - see `jobs.aged_to_archive`.
AGED_DONE = "Aged Leads Order Done"

# (hour, minute, what happens). Local time on the board's own clock - the
# team's, not the server's.
#
# Six makes the setup card, two days out. Two days, not one: a setup card is
# worked the day before its agents go live, so the one made this morning has
# to be in In Que by this evening for tomorrow to work on it.
#
# Two things happen at half eight, in this order: "after you move those unchecked
# lists to their respective new list you move them to done". The carry has to
# read the cards while they are still the day's, so it goes first.
STEPS = (
    (6, 0, "make_setup"),
    (9, 0, "to_today"),
    # Half eleven, because "the in que cards comes in by 11am" - and the setup
    # card they belong to was made at six. Half an hour of margin, and a late
    # batch is reported rather than silently skipped.
    (11, 30, "link_setup"),
    (15, 30, UNMARKED[0]),
    (17, 30, UNMARKED[1]),
    (18, 0, "to_quality_check"),
    (20, 30, "rollover"),
    (20, 30, "to_done"),
    (22, 0, "archive_aged"),
)

STEP_NAMES = {
    "make_setup": "make the setup card for the day after tomorrow",
    "to_today": f"{IN_QUE} → {TODAY}",
    "link_setup": "put the setup card on tomorrow's Ads and Ops checklists",
    "to_quality_check": f"{TODAY} → {QUALITY_CHECK}",
    "to_done": f"{QUALITY_CHECK} → {DONE}",
    "rollover": "carry the unfinished items to tomorrow",
    UNMARKED[0]: f"the New Agent cards in {DONE} nobody has ticked",
    UNMARKED[1]: f"the New Agent cards in {DONE} nobody has ticked",
    "archive_aged": f"archive the ticked cards in {AGED_DONE}",
}

# Where each move goes. The rollover and the 6am make move no cards, so
# neither is here.
STEP_LISTS = {
    "to_today": (IN_QUE, TODAY),
    "to_quality_check": (TODAY, QUALITY_CHECK),
    "to_done": (QUALITY_CHECK, DONE),
}

# What somebody types to ask for a move, by where they want the cards to end
# up. Named for the destination because that is how anybody says it: "move
# them to Today", not "do the nine o'clock one".
MOVE_WORDS = (
    ("to_today", re.compile(r"\btoday\b", re.IGNORECASE)),
    ("to_quality_check", re.compile(r"\bquality\s*check\b|\bqc\b", re.IGNORECASE)),
    ("to_done", re.compile(r"\bdone\b", re.IGNORECASE)),
)


def time_of(step: str) -> tuple[int, int] | None:
    """When a step runs on its own, or None for one that only happens when asked."""
    return dict((name, (at, past)) for at, past, name in STEPS).get(step)


def clock(hour: int, minute: int = 0) -> str:
    """(15, 30) -> "3:30pm"; (6, 0) -> "6am". The way anybody says it."""
    told = f"{hour % 12 or 12}{'am' if hour < 12 else 'pm'}"
    return told if not minute else f"{hour % 12 or 12}:{minute:02d}{'am' if hour < 12 else 'pm'}"


def said_at(step: str) -> str:
    """The time a step runs, for the line it posts. "" if it has no hour."""
    when = time_of(step)
    return clock(*when) if when else ""


def move_named(text: str) -> str | None:
    """Which move somebody asked for, by the list they named."""
    said = " ".join((text or "").split())
    for step, pattern in MOVE_WORDS:
        if pattern.search(said):
            return step
    return None


def steps_due(now, done_today: set[str]) -> list[str]:
    """Which of the day's steps should have happened by now and haven't.

    Catches up rather than skipping. RYTE is not always awake at nine, and a
    board still sitting in In Que at eleven is worse than one moved late - the
    team's cards are where they should be either way, just later.

    Ordered, because the 6pm move takes cards the 9am move put there.
    """
    return [
        step for hour, minute, step in STEPS
        if (now.hour, now.minute) >= (hour, minute) and step not in done_today
    ]


_KIND_PATTERNS = (
    ("lead_order", re.compile(r"\blead\s*order\b", re.IGNORECASE)),
    ("general", re.compile(r"\bgeneral\b", re.IGNORECASE)),
    ("ops", re.compile(r"\bops\b", re.IGNORECASE)),
    ("ads", re.compile(r"\bads\b", re.IGNORECASE)),
)

# MM/DD/YY. The first date wins: one card is titled "Lead Order 08/14/26 -
# 8/16/26" and the day it belongs to is the one it starts on.
_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b")


def kind_named(text: str) -> str | None:
    """Which of the four somebody meant, or None for all of them.

    "rollover general" is how you try this on one card before trusting it with
    the board. The words are the same ones that identify the cards, so nobody
    has to learn a second vocabulary for it.
    """
    said = " ".join((text or "").split())
    for kind, pattern in _KIND_PATTERNS:
        if pattern.search(said):
            return kind
    return None


# "rollover skip ads" - hold one card back tonight. "unskip" and "skip none"
# take it off again, and both are checked before `skip` so that "unskip" is
# not read as somebody asking to skip.
UNSKIP = re.compile(r"\bunskip\b|\bskip\s+(?:none|nothing)\b|\bcarry\s+all\b", re.IGNORECASE)
SKIP = re.compile(r"\bskip\b|\bexcept\b|\bhold\s+back\b|\bdo\s*n[o']?t\s+carry\b", re.IGNORECASE)


def kinds_named(text: str) -> list[str]:
    """Every one of the four somebody named, in the order the table has them.

    More than one, unlike `kind_named` - "skip ads and ops" is two cards held
    back, and answering with the first would quietly carry the second.
    """
    said = " ".join((text or "").split())
    return [kind for kind, pattern in _KIND_PATTERNS if pattern.search(said)]


def skip_asked(text: str) -> tuple[str, list[str]] | None:
    """("hold"|"release", kinds) when somebody asked about skipping, else None.

    An empty kinds list on a release means all of them; on a hold it means
    somebody said "skip" and named no card, which is a question rather than an
    instruction and is answered as one.
    """
    said = " ".join((text or "").split())
    if UNSKIP.search(said):
        return "release", kinds_named(said)
    if SKIP.search(said):
        return "hold", kinds_named(said)
    return None


def parse_card_title(title: str) -> tuple[str, date] | None:
    """('general', date(2026, 8, 20)) for `💎 General 08/20/26`, else None.

    Returns None for everything that isn't one of the four - `Agent Setup Going
    Live Thursday 08/20` sits in the same lists and must not be picked up.
    """
    text = title or ""
    if re.search(r"agent\s+setup", text, re.IGNORECASE):
        return None

    found = _DATE.search(text)
    if not found:
        return None

    month, day, year = (int(part) for part in found.groups())
    if year < 100:
        year += 2000
    try:
        when = date(year, month, day)
    except ValueError:
        return None

    for kind, pattern in _KIND_PATTERNS:
        if pattern.search(text):
            return kind, when
    return None


def daily_cards(cards: list[dict]) -> dict[tuple[str, date], dict]:
    """Index the four daily cards in a list by (kind, date)."""
    found: dict[tuple[str, date], dict] = {}
    for card in cards:
        key = parse_card_title(str(card.get("name", "")))
        if key:
            found[key] = card
    return found


def cards_for(cards: list[dict], day: date) -> dict[str, dict]:
    """The four cards for one day, keyed by kind. Missing kinds are absent."""
    return {kind: card for (kind, when), card in daily_cards(cards).items() if when == day}


def why_missing(cards: list[dict], kind: str, wanted: date) -> str:
    """What to say about a card that should exist for a day and doesn't.

    Usually because the date on it is not the date it looks like. A card
    titled `General 08/26/25` reads as tomorrow's at a glance and sorts as
    last year's, so "there is no card for tomorrow" is true and useless -
    the card is right there with a typo in it.
    """
    dates = sorted(
        when for (found, when) in daily_cards(cards) if found == kind
    )
    same_look = [
        when for when in dates
        if (when.month, when.day) == (wanted.month, wanted.day) and when != wanted
    ]
    if same_look:
        return f"there's one dated {same_look[0]:%m/%d/%y} — the year on it is wrong"

    ahead = [when for when in dates if when > wanted]
    if ahead:
        return f"the next one I can see is {ahead[0]:%a %b %d}"
    return "nothing dated after today"


def missing_kinds(cards: list[dict], day: date) -> list[str]:
    """Which of the four didn't get generated - Lead Order has gone missing before."""
    present = cards_for(cards, day)
    return [kind for kind in CARD_KINDS if kind not in present]


# ------------------------------------------------------------------ rollover


@dataclass
class Leftover:
    """One unticked item, and everything needed to decide what to do with it."""

    person: str
    name: str
    carries_link: bool = False
    # A linked card whose badge already reads Done while the box stays unticked.
    # The work looks finished and nobody ticked it, so it is not ours to decide.
    looks_done: bool = False
    times_rolled: int = 0

    @property
    def stuck(self) -> bool:
        """Rolled forward enough times that moving it again silently is wrong."""
        return self.times_rolled >= 3


@dataclass
class RolloverPlan:
    kind: str
    from_title: str
    to_title: str
    leftovers: list[Leftover] = field(default_factory=list)
    # Person checklists that tomorrow's card doesn't have yet. The generated
    # cards have arrived with no checklists at all, so this is the normal case
    # rather than an error.
    checklists_to_create: list[str] = field(default_factory=list)

    @property
    def needs_a_look(self) -> list[Leftover]:
        return [item for item in self.leftovers if item.looks_done or item.stuck]

    @property
    def carried(self) -> list[Leftover]:
        return [item for item in self.leftovers if not item.looks_done]


def unchecked(checklists: list[dict]) -> list[tuple[str, dict]]:
    """(person, item) for every item still unticked, in board order.

    Checklist names are people, so the name is the routing information: it
    decides whose list the item lands on tomorrow.
    """
    found = []
    for checklist in checklists or []:
        person = str(checklist.get("name") or "").strip()
        for item in checklist.get("checkItems") or []:
            if str(item.get("state")) != "complete":
                found.append((person, item))
    return found


def plan_rollover(
    kind: str,
    *,
    source_card: dict,
    source_checklists: list[dict],
    target_card: dict,
    target_checklists: list[dict],
    done_lookup=None,
    history: dict[str, int] | None = None,
) -> RolloverPlan:
    """Work out exactly what should move from today's card onto tomorrow's.

    `done_lookup` answers "is this linked card already in Done?" for the
    ambiguous case. `history` counts how many days each item has already been
    carried, so one that keeps reappearing gets raised rather than moved again.
    """
    from . import trello

    history = history or {}
    plan = RolloverPlan(
        kind=kind,
        from_title=str(source_card.get("name", "")),
        to_title=str(target_card.get("name", "")),
    )

    existing = {
        " ".join(str(c.get("name") or "").split()).casefold() for c in target_checklists or []
    }
    wanted: list[str] = []

    for person, item in unchecked(source_checklists):
        name = str(item.get("name") or "")
        carries_link = trello.item_is_link(name)
        looks_done = False
        if carries_link and done_lookup is not None:
            looks_done = bool(done_lookup(trello.linked_card_id(name)))

        plan.leftovers.append(
            Leftover(
                person=person,
                name=name,
                carries_link=carries_link,
                looks_done=looks_done,
                times_rolled=history.get(item_key(kind, person, name), 0),
            )
        )

        key = " ".join(person.split()).casefold()
        if person and key not in existing and person not in wanted:
            wanted.append(person)

    plan.checklists_to_create = wanted
    return plan


def item_key(kind: str, person: str, name: str) -> str:
    """A stable identity for an item across days, for counting rollovers."""
    return f"{kind}|{' '.join(person.split()).casefold()}|{' '.join((name or '').split())}"


def next_day(day: date) -> date:
    """The day a rollover targets. Deliberately the next calendar day.

    The cards are generated daily including weekends, so this does not skip
    them the way the blog scheduler does.
    """
    return day + timedelta(days=1)


def summarise(plans: list[RolloverPlan]) -> str:
    """A short report of a rollover, for reading before anything is written."""
    if not plans:
        return "Nothing to roll over — every item is ticked."

    lines = []
    for plan in plans:
        carried = plan.carried
        label = CARD_KINDS.get(plan.kind, plan.kind)
        lines.append(f"**{label}** — {len(carried)} item(s) → `{plan.to_title}`")
        by_person: dict[str, int] = {}
        for item in carried:
            by_person[item.person] = by_person.get(item.person, 0) + 1
        for person, count in by_person.items():
            new = " (new checklist)" if person in plan.checklists_to_create else ""
            lines.append(f"  • {person}: {count}{new}")
        for item in plan.needs_a_look:
            why = "already Done but unticked" if item.looks_done else (
                f"rolled forward {item.times_rolled} days"
            )
            lines.append(f"  ⚠ {item.person}: {_short(item.name)} — {why}")
    return "\n".join(lines)


def _short(name: str, limit: int = 60) -> str:
    text = " ".join((name or "").split())
    return text if len(text) <= limit else text[:limit] + "…"
