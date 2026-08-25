"""New agents, from the form that made the card to the day they go live.

A landing-page form drops a card into Tre. Faith and Casey fill in the
details, decide the launch date, and move it to In Que. From there it is
mechanical, and it is the mechanical part that gets done wrong at 5pm on a
Friday: copy the lead type, copy the card link, paste both onto three
different cards on the right date, on the right checklists.

What happens turns on when the agent goes live, and on whether the card they
go on exists yet:

    today                       -> that day's Lead Order, Ads and Ops cards
    a setup card covers the day -> that card
    tomorrow, and none exists   -> make one, then that card
    anything else               -> wait in Franklin's list

Everything filed ends up in Done, at the top of it. Franklin's list means
waiting, and it is read every time In Que is - an agent put there on Monday
because nothing existed for Thursday goes on the Thursday card the moment
somebody makes it. A waiting room nobody goes back to is where things get
lost.

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

# Where the self-setup agents go on the Lead Order card. Only these lead types
# land there - every item on the real checklist is one of them: "40 Basic FB
# Spanish IUL", "30 Spanish Instant/Basic IUL Leads", "FB Index Universal
# Life". Anything else goes on the checklist for its own lead type.
OWN_SETUP = "own setup"

# Instant and FB name no tier of their own and belong here anyway.
OWN_SETUP_WORDS = re.compile(r"\binstant\b|\bfb\b|\bfacebook\b", re.IGNORECASE)


def is_own_setup(lead_type: str) -> bool:
    """Whether these leads belong on the own-setup checklist.

    The Standard tier is the self-setup half - basic, standard, instant, FB -
    as against the Plus half, which is ordered and has a checklist each. So
    Phoenix Standard goes here and PHX Plus does not, the same rule telling
    them apart rather than Phoenix being an exception to it.
    """
    said = lead_type or ""
    return tier_of(said) == "standard" or bool(OWN_SETUP_WORDS.search(said))

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
    ("phnx", re.compile(r"\bpho?e?nix\b|\bphnx\b|\bphx\b", re.IGNORECASE)),
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
    # Everything written on the card, kept so the lead type can be worked out
    # against the checklists that actually exist rather than in the abstract.
    said: str = ""
    # The most specific thing the card calls the leads, for the lines that go
    # on a setup card and for saying what an agent is before matching.
    stated: str = ""
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


# "$1050/WEEK- UPRISE PHX PLUS" is a price and then a lead type. The price is
# not part of what the leads are called.
_PRICE_PREFIX = re.compile(r"^\$\s*[\d,.]+\s*(?:/\s*\w+)?\s*[-–—:]*\s*")

# "Package Selected: Basic Spanish IUL" is a label and then a lead type. The
# label is the form's word for the field, not anything about the leads.
_LABEL_PREFIX = re.compile(r"^[A-Za-z][A-Za-z ]{0,30}:\s*")


def tidy_lead_type(phrase: str) -> str:
    """A lead type with the label, the money and the punctuation taken off."""
    said = " ".join((phrase or "").split())
    said = _LABEL_PREFIX.sub("", said)
    return _PRICE_PREFIX.sub("", said).strip(" -–—:")


def named_lead_types(text: str) -> list[str]:
    """Every phrase on the card that names a kind of leads.

    The Lead Type line first, then each line of the rest. A card says it more
    than once and not always in the same words: "Lead Type: VETS" up top and
    "$1050/WEEK- UPRISE PHX PLUS" further down are both somebody saying what
    was bought.
    """
    said = []
    field = find_lead_type(text)
    if field:
        said.append(field)
    for line in (text or "").splitlines():
        line = " ".join(line.split())
        if line and line != field and family_of(line):
            said.append(tidy_lead_type(line))
    return said


def stated_lead_type(text: str) -> str:
    """The most specific thing the card says the leads are.

    No board to check against - this is for the lines that go on a setup card,
    which has a checklist per person rather than per lead type, and for saying
    what an agent is before anything has been matched. The rule is the same
    one: a phrase naming its tier beats a phrase that leaves it out.
    """
    said = named_lead_types(text)
    if not said:
        return find_lead_type(text)
    tiered = [phrase for phrase in said if tier_of(phrase)]
    return (tiered or said)[0]


def candidates(
    lead_type: str, existing: list[str], *, tier: str | None = None, strict: bool = True
) -> list[str]:
    """Every checklist these leads could belong on.

    More than one means the lead type didn't say enough. "Phoenix Campaign"
    is Phoenix leads and says nothing about Standard or Plus, and a board with
    both is a coin toss over somebody's money.
    """
    family, said, marks = shape_of(lead_type)
    tier = said or tier
    if family is None:
        return []
    found = []
    for name in existing or []:
        theirs = shape_of(name)
        if theirs[0] != family or theirs[2] != marks:
            continue
        if strict and tier and theirs[1] and tier != theirs[1]:
            continue
        found.append(name)
    return found


def match_checklist(
    lead_type: str, existing: list[str], *, tier: str | None = None
) -> str | None:
    """The checklist this lead type belongs on, out of the ones that exist.

    Matched by what the leads *are* rather than by the words used, so the card
    can say "Text Verified IUL Plus" and land on "OTP IUL Plus". None when
    nothing on the card is those leads - the caller makes one.

    A tier only decides it when both sides name one. "Spanish IUL" says
    nothing about Standard or Plus, and refusing to match it to "OTP Spanish
    IUL" over a word neither of them disputes would make a second checklist
    for the same leads.

    None when two could fit, as well as when none does. Picking one of two is
    guessing, and this is the guess that puts somebody's leads on the wrong
    order.
    """
    found = candidates(lead_type, existing, tier=tier)
    return found[0] if len(found) == 1 else None


# "$1050/WEEK- UPRISE PHX PLUS" is a price and then a lead type. The price is
# not part of what the leads are called.
_PRICE_PREFIX = re.compile(r"^\$\s*[\d,.]+\s*(?:/\s*\w+)?\s*[-–—:]*\s*")

# "Package Selected: Basic Spanish IUL" is a label and then a lead type. The
# label is the form's word for the field, not anything about the leads.
_LABEL_PREFIX = re.compile(r"^[A-Za-z][A-Za-z ]{0,30}:\s*")


def tidy_lead_type(phrase: str) -> str:
    """A lead type with the label, the money and the punctuation taken off."""
    said = " ".join((phrase or "").split())
    said = _LABEL_PREFIX.sub("", said)
    return _PRICE_PREFIX.sub("", said).strip(" -–—:")


def named_lead_types(text: str) -> list[str]:
    """Every phrase on the card that names a kind of leads.

    The Lead Type line first, then each line of the rest. A card says it more
    than once and not always in the same words: "Lead Type: VETS" up top and
    "$1050/WEEK- UPRISE PHX PLUS" further down are both somebody saying what
    was bought.
    """
    said = []
    field = find_lead_type(text)
    if field:
        said.append(field)
    for line in (text or "").splitlines():
        line = " ".join(line.split())
        if line and line != field and family_of(line):
            said.append(tidy_lead_type(line))
    return said


def stated_lead_type(text: str) -> str:
    """The most specific thing the card says the leads are.

    No board to check against - this is for the lines that go on a setup card,
    which has a checklist per person rather than per lead type, and for saying
    what an agent is before anything has been matched. The rule is the same
    one: a phrase naming its tier beats a phrase that leaves it out.
    """
    said = named_lead_types(text)
    if not said:
        return find_lead_type(text)
    tiered = [phrase for phrase in said if tier_of(phrase)]
    return (tiered or said)[0]


def best_lead_type(text: str, existing: list[str]) -> tuple[str, str | None, list[str]]:
    """(the phrase to use, the checklist it lands on, what else could fit).

    A card naming its tier outright beats one that leaves it to be worked out:
    "Lead Type: VETS" says which leads and "UPRISE PHX PLUS" says which leads
    *and* which tier, so the second one is the one to act on - as long as the
    board actually has that checklist.

    Two phrases naming different checklists, both with a tier, is the card
    disagreeing with itself. Both come back and a person is asked.
    """
    hint = tier_of(text)
    found = []
    for phrase in named_lead_types(text):
        landed = match_checklist(phrase, existing, tier=hint)
        if landed:
            found.append((bool(tier_of(phrase)), phrase, landed))

    if not found:
        # Nothing matched. A different tier is a different product - Basic
        # Spanish IUL and OTP Spanish IUL are two things you can buy - so it
        # is not a near miss and the checklist gets made. What does need
        # asking is a card that named its leads and not its tier, where two
        # on the board would both fit.
        said = stated_lead_type(text)
        return said, None, candidates(said, existing, tier=hint)

    tiered = [item for item in found if item[0]]
    picked = tiered or found
    landings = {item[2] for item in picked}
    if len(landings) > 1:
        return picked[0][1], None, sorted(landings)
    return picked[0][1], picked[0][2], []


def tier_hint(text: str) -> str | None:
    """The tier said anywhere on the card, for when the Lead Type line doesn't.

    "Lead Type: Phoenix Campaign" with "Phoenix Standard / $350" three lines
    below it is one card saying one thing twice, and the second half is the
    half with the answer in it.
    """
    return tier_of(text)


def checklist_item(url: str, lead_type: str) -> str:
    """The link, then the lead type. What goes onto every checklist."""
    return f"{url} {lead_type}".strip()


# ------------------------------------------------------ when they go live

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
# "live fri, aug 28" is how it is actually written about half the time.
_SHORT_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# The ways people say when somebody goes live, strongest first. "Launch date"
# is unmistakable; a bare "live" is not - "Live transfer leads" is a product -
# so it is looked at last and only counts if a date is sitting next to it.
_WHEN_SAID = (
    re.compile(r"[^.\n]*\blaunch\s*date\b[^.\n]*", re.IGNORECASE),
    re.compile(r"[^.\n]*\b(?:launch(?:ing|es|ed)?|go(?:es|ing)?\s+live)\b[^.\n]*", re.IGNORECASE),
    re.compile(r"[^.\n]*\blive\b[^.\n]*", re.IGNORECASE),
)

# "August 27th" and "Sept 3rd" are how dates get typed by people rather than
# by forms. The suffix runs straight into the number, so a word boundary after
# it never matches and the whole date was missed.
_MONTH_DAY = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b",
    re.IGNORECASE,
)
_NUMERIC = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")


def find_launch(text: str, *, today: date) -> date | None:
    """When the agent goes live, read out of the sentence that says so.

    A written-out date wins over "today". They agree when the card is read the
    day it was written and disagree when it isn't, and the calendar date is
    the one that stays true.
    """
    for pattern in _WHEN_SAID:
        for sentence in pattern.findall(text or ""):
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
        if re.search(rf"\b{name}\b|\b{_SHORT_DAYS[index]}\b", said):
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
        said=text or "",
        stated=stated_lead_type(text),
    )
    return agent


def cannot_read(agent: "Agent", *, needs_lead_type: bool) -> str:
    """What is missing, and only what is missing *yet*.

    A card parked for next Friday needs a launch date and nothing else - the
    lead type is often filled in later, and saying it is missing days before
    anybody needs it is how a warning stops being read.
    """
    missing = []
    if agent.launch is None:
        missing.append("a launch date")
    if needs_lead_type and not agent.lead_type:
        missing.append("a lead type")
    return f"I can't find {' or '.join(missing)} on this card." if missing else ""


# ------------------------------------------- the card tomorrow's ones go on

SETUP_TITLE = "Agent Setup Going Live"
_SETUP_CARD = re.compile(r"agent\s+setup", re.IGNORECASE)
# No word boundary in front: the real ones are typed "Wednesday08/26" as often
# as "Wednesday 08/26", and a boundary needs a space that isn't there.
_SETUP_DATE = re.compile(r"(\d{1,2})/(\d{1,2})(?!\d)")

# Fridays make one card for the whole weekend - "Agent Setup Going Live
# Saturday-Monday 08/22-08/25" - because nobody is making a card on Saturday.
FRIDAY = 4
SATURDAY = 5


def weekend_span(day: date) -> tuple[date, date] | None:
    """(Saturday, Monday) when `day` is a Saturday, else None.

    The Friday card covers until the next working day, so an agent going live
    on the Sunday belongs on it just as much as one going live on the Saturday.
    """
    if day.weekday() != SATURDAY:
        return None
    return day, day + timedelta(days=2)


def setup_title(day: date, through: date | None = None) -> str:
    """"Agent Setup Going Live Wednesday 08/26", or a weekend's worth of one."""
    if through is None or through == day:
        return f"{SETUP_TITLE} {day:%A} {day:%m/%d}"
    return f"{SETUP_TITLE} {day:%A}-{through:%A} {day:%m/%d}-{through:%m/%d}"


def setup_covers(title: str, day: date) -> bool:
    """Whether a setup card's title covers a given day.

    One date means one day. Two mean a span, and everything between them is
    on it - which is what the Friday card is for.
    """
    found = _SETUP_DATE.findall(title or "")
    if not found:
        return False

    wanted = (day.month, day.day)
    if len(found) == 1:
        return (int(found[0][0]), int(found[0][1])) == wanted

    start = (int(found[0][0]), int(found[0][1]))
    end = (int(found[-1][0]), int(found[-1][1]))
    if start <= end:
        return start <= wanted <= end
    # A span across the turn of the year: 12/31-01/02 is three days, not none.
    return wanted >= start or wanted <= end


def find_setup_card(cards: list[dict], day: date) -> dict | None:
    """The setup card covering a day, wherever on the board it has got to.

    Found by the dates in its title rather than by list: they are made in
    Automation Department and do not always stay there, and a second one made
    because the first was moved is worse than looking everywhere.
    """
    for card in cards or []:
        title = str(card.get("name", ""))
        if _SETUP_CARD.search(title) and setup_covers(title, day):
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
        leads = agent.stated or agent.lead_type
        if leads:
            head += f" — {leads}"
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
        elif not plan.steps:
            # Parked and staying parked. Saying nothing under a name reads as
            # "nothing will happen to this", which is true and unhelpful.
            lines.append(f"  · waiting in {PARKED} — no card for that day yet")
    return "\n".join(lines)
