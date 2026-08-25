"""New agents, from the form that made the card to the day they go live.

A landing-page form drops a card into Tre. Faith and Casey fill in the
details, decide the launch date, and move it to In Que. From there it is
mechanical, and it is the mechanical part that gets done wrong at 5pm on a
Friday: copy the lead type, copy the card link, paste both onto three
different cards on the right date, on the right checklists.

What happens depends only on when the agent goes live:

    today      -> onto that day's Lead Order, Ads and Ops cards, then Done
    tomorrow   -> onto tomorrow's "Agent Setup Going Live" card, then Done
    later      -> park it in Franklin's list until it becomes tomorrow

Everything here is pure. It reads the card's description and comments and
works out what should happen; nothing in this module writes to the board, so
the reading can be tested against real descriptions without an API key.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

# Where an agent card waits, and where it ends up.
IN_QUE = "In Que"
PARKED = "Franklin (Admin)"
AUTOMATION = "AUTOMATION DEPARTMENT"
DONE = "Done"

# Who gets told, per card. Kath and Kathleen are one person; the checklists
# are named differently on different cards and both mean her.
ADS_PEOPLE = ("Jenn", "Kath", "Nicole")
OPS_PEOPLE = ("Therese",)
SETUP_PEOPLE = ("Therese", "Kathleen", "Nicole")

# "New Agent - Gustin Elrod", "NEW AGENT- Jeffrey Boyd", "New Agent - Everlife".
# The dash is required: "New Agent Onboarding SOP" is a procedure, not a
# person, and it would sit in the same list looking near enough the same.
AGENT_CARD = re.compile(r"^\s*new\s+agent\s*[-–—:]+\s*", re.IGNORECASE)

# The line the form writes. Authoritative: the body text mentions lead types
# in passing ("paid for OTP IUL leads") and that is not the field.
LEAD_TYPE_LINE = re.compile(r"^\s*lead\s*type\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)

# What kind of leads. The word that matters, whatever it is dressed in.
FAMILIES = (
    ("iul", re.compile(r"\biul\b", re.IGNORECASE)),
    ("fex", re.compile(r"\bfex\b|\bfinal\s+expense\b", re.IGNORECASE)),
    ("mtg", re.compile(r"\bmtg\b|\bmortgage\b", re.IGNORECASE)),
    ("vet", re.compile(r"\bvets?\b|\bveterans?\b", re.IGNORECASE)),
    ("widows", re.compile(r"\bwidows?\b", re.IGNORECASE)),
)

# Variants that sit alongside the plain one and are not it. "OTP Spanish IUL"
# is a different checklist from "OTP IUL Plus" and filing into the wrong one
# sends somebody's leads to the wrong place.
QUALIFIERS = (
    ("blue collar", re.compile(r"\bblue\s*collar\b", re.IGNORECASE)),
    ("spanish", re.compile(r"\bspanish\b", re.IGNORECASE)),
)

# Franklin's rule: standard or basic means Standard; text-verified or OTP
# means Plus. OTP and text-verified are the same thing said two ways.
STANDARD = re.compile(r"\bstandard\b|\bbasic\b", re.IGNORECASE)
PLUS = re.compile(r"\bplus\b|\btext[\s-]*verified\b|\botp\b", re.IGNORECASE)


@dataclass
class Agent:
    """One new agent, and everything needed to file them."""

    name: str
    card_id: str
    url: str
    lead_type: str = ""
    launch: date | None = None
    note: str = ""

    @property
    def ready(self) -> bool:
        """Whether there is enough here to act on without guessing."""
        return bool(self.lead_type and self.launch)

    def when(self, today: date) -> str:
        """today / tomorrow / later / past, by the launch date."""
        if self.launch is None:
            return "unknown"
        if self.launch <= today:
            return "today"
        if self.launch == today + timedelta(days=1):
            return "tomorrow"
        return "later"


def is_agent_card(title: str) -> bool:
    """Whether a card in In Que is a new agent rather than a daily card."""
    return bool(AGENT_CARD.match(title or ""))


def agent_name(title: str) -> str:
    """"New Agent - Gustin Elrod" -> "Gustin Elrod"."""
    said = " ".join((title or "").split())
    return AGENT_CARD.sub("", said).lstrip(" -–—:").strip() or said


def find_lead_type(text: str) -> str:
    """The lead type as the form wrote it, or "" if the line isn't there."""
    found = LEAD_TYPE_LINE.search(text or "")
    return " ".join(found.group(1).split()) if found else ""


def family_of(text: str) -> str | None:
    for name, pattern in FAMILIES:
        if pattern.search(text or ""):
            return name
    return None


def tier_of(text: str) -> str | None:
    """Standard beats Plus when both are said - "basic" is the stronger word."""
    if STANDARD.search(text or ""):
        return "standard"
    if PLUS.search(text or ""):
        return "plus"
    return None


def qualifiers_of(text: str) -> frozenset[str]:
    return frozenset(
        name for name, pattern in QUALIFIERS if pattern.search(text or "")
    )


def shape_of(text: str) -> tuple:
    """What a lead type reduces to, for comparing two ways of writing it.

    "Text Verified IUL Plus" and "OTP IUL Plus" are the same leads: OTP and
    text-verified are one thing said two ways, so both reduce to (iul, plus).
    """
    return (family_of(text), tier_of(text), qualifiers_of(text))


def match_checklist(lead_type: str, existing: list[str]) -> str | None:
    """The checklist this lead type belongs on, out of the ones that exist.

    Matched by what the leads *are* rather than by the words used, so the card
    can say "Text Verified IUL Plus" and land on "OTP IUL Plus". None when
    nothing on the card is those leads - the caller makes one.

    A tier only decides it when both sides name one. "Spanish IUL" says
    nothing about Standard or Plus, and refusing to match it to "OTP Spanish
    IUL" over a word neither of them disputes would make a second checklist
    for the same leads.
    """
    family, tier, marks = shape_of(lead_type)
    if family is None:
        return None
    for name in existing or []:
        theirs = shape_of(name)
        if theirs[0] != family or theirs[2] != marks:
            continue
        if tier and theirs[1] and tier != theirs[1]:
            continue
        return name
    return None


def checklist_item(url: str, lead_type: str) -> str:
    """The link, then the lead type. What goes onto every checklist."""
    return f"{url} {lead_type}".strip()


# ------------------------------------------------------ when they go live

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

# The sentence that says when. Cut at the sentence end so a second date later
# in the description is not read as the launch.
_LAUNCH_SENTENCE = re.compile(r"[^.\n]*\blaunch(?:ing|es)?\b[^.\n]*", re.IGNORECASE)

_MONTH_DAY = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")[a-z]*\.?\s+(\d{1,2})\b", re.IGNORECASE
)
_NUMERIC = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")


def find_launch(text: str, *, today: date) -> date | None:
    """When the agent goes live, read out of the sentence that says so.

    A written-out date wins over "today". They agree when the card is read the
    day it was written and disagree when it isn't, and the calendar date is
    the one that stays true.
    """
    for sentence in _LAUNCH_SENTENCE.findall(text or ""):
        found = _date_in(sentence, today=today)
        if found:
            return found
    return None


def _date_in(sentence: str, *, today: date) -> date | None:
    numeric = _NUMERIC.search(sentence)
    if numeric:
        month, day, year = numeric.groups()
        return _made(int(month), int(day), year, today=today)

    written = _MONTH_DAY.search(sentence)
    if written:
        month = _MONTHS[written.group(1)[:3].lower()]
        return _made(month, int(written.group(2)), None, today=today)

    said = sentence.lower()
    if re.search(r"\btoday\b|\bimmediate(?:ly)?\b", said):
        return today
    if "tomorrow" in said:
        return today + timedelta(days=1)

    for index, name in enumerate(_WEEKDAYS):
        if re.search(rf"\b{name}\b", said):
            ahead = (index - today.weekday()) % 7
            return today + timedelta(days=ahead)
    return None


def _made(month: int, day: int, year: str | None, *, today: date) -> date | None:
    """A date from its parts, with the year worked out when it wasn't given."""
    if year:
        number = int(year)
        number += 2000 if number < 100 else 0
        try:
            return date(number, month, day)
        except ValueError:
            return None
    # No year: this one, unless that is well past, in which case next.
    for candidate in (today.year, today.year + 1):
        try:
            made = date(candidate, month, day)
        except ValueError:
            return None
        if made >= today - timedelta(days=60):
            return made
    return None


def read_agent(card: dict, *, text: str, today: date) -> Agent | None:
    """One agent card read into what RYTE needs, or None if it isn't one.

    `text` is the description and every comment run together: the launch date
    turns up in either, and which one it is in is nobody's decision to make.
    """
    title = str(card.get("name", ""))
    if not is_agent_card(title):
        return None

    agent = Agent(
        name=agent_name(title),
        card_id=str(card.get("id") or ""),
        url=str(card.get("shortUrl") or card.get("url") or ""),
        lead_type=find_lead_type(text),
        launch=find_launch(text, today=today),
    )
    missing = [
        what for what, got in (("a lead type", agent.lead_type), ("a launch date", agent.launch))
        if not got
    ]
    if missing:
        agent.note = f"I can't find {' or '.join(missing)} on this card."
    return agent


# ------------------------------------------- the card tomorrow's ones go on

SETUP_TITLE = "Agent Setup Going Live"
_SETUP_CARD = re.compile(r"agent\s+setup", re.IGNORECASE)
# No word boundary in front: the real ones are typed "Wednesday08/26" as often
# as "Wednesday 08/26", and a boundary needs a space that isn't there.
_SETUP_DATE = re.compile(r"(\d{1,2})/(\d{1,2})(?!\d)")


def setup_title(day: date) -> str:
    """"Agent Setup Going Live Wednesday 08/26"."""
    return f"{SETUP_TITLE} {day:%A} {day:%m/%d}"


def find_setup_card(cards: list[dict], day: date) -> dict | None:
    """Tomorrow's setup card, wherever on the board it has got to.

    Found by month and day rather than by list: they are made in Automation
    Department and do not always stay there, and a second one made because
    the first was moved is worse than looking everywhere.
    """
    for card in cards or []:
        title = str(card.get("name", ""))
        if not _SETUP_CARD.search(title):
            continue
        found = _SETUP_DATE.search(title)
        if found and (int(found.group(1)), int(found.group(2))) == (day.month, day.day):
            return card
    return None


# ---------------------------------------------------------------- the plan


@dataclass
class Step:
    """One checklist an agent's line is going onto."""

    card_title: str
    card_id: str
    checklist: str
    item: str
    make_checklist: bool = False


@dataclass
class AgentPlan:
    """What happens to one agent card, ready to be read before it happens."""

    agent: Agent
    when: str
    steps: list[Step] = field(default_factory=list)
    move_to: str = ""
    make_card: str = ""
    problems: list[str] = field(default_factory=list)

    @property
    def doable(self) -> bool:
        return not self.problems and (bool(self.steps) or bool(self.move_to))


def describe(plans: list[AgentPlan]) -> str:
    """The whole pass, as something to read before pressing anything."""
    if not plans:
        return "No new agents waiting in In Que."

    lines = []
    for plan in plans:
        agent = plan.agent
        when = {
            "today": "launching today",
            "tomorrow": "launching tomorrow",
            "later": f"launching {agent.launch:%a %b %d}" if agent.launch else "later",
            "unknown": "no launch date",
        }[plan.when]
        head = f"**{agent.name}**"
        if agent.lead_type:
            head += f" — {agent.lead_type}"
        lines.append(f"{head} — {when}")

        if plan.problems:
            lines.extend(f"  ⚠ {problem}" for problem in plan.problems)
            continue
        if plan.make_card:
            lines.append(f"  + make `{plan.make_card}`")
        for step in plan.steps:
            made = " (new checklist)" if step.make_checklist else ""
            lines.append(f"  → {step.card_title} · {step.checklist}{made}")
        if plan.move_to:
            lines.append(f"  → move to {plan.move_to}")
    return "\n".join(lines)
