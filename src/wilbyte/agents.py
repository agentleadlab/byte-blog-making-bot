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
    no card for the day yet     -> wait in Franklin's list

Filing an agent never makes a card. The setup cards are made once a day at
six, two days ahead, and a second one made mid-morning because an agent
turned up would split a day's agents across two of them - so an agent whose
card does not exist yet waits for it.

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

# Where an agent card waits, and where it ends up. Today is watched as well as
# In Que: the daily walk leaves agent cards alone, but somebody dragging one
# across by hand shouldn't put it somewhere nothing looks.
IN_QUE = "In Que"
TODAY = "Today"
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
#
# Two forms, because two things write these cards. The \b after "type" keeps
# prose out either way - "several lead types" is somebody talking, not a field.
#
# The order form's own field, colon and value on the one line.
LEAD_TYPE_LINE = re.compile(
    r"^[ \t]*lead[ \t]*type\b[ \t]*:[ \t]*(\S.*?)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)

# Spark writes the label on its own line with the value underneath. There is
# no colon in that shape, so requiring its absence keeps the two apart: a
# colon with nothing after it is an empty field, and the line below it belongs
# to whatever comes next.
LEAD_TYPE_BLOCK = re.compile(
    r"^[ \t]*lead[ \t]*type\b[ \t]*\r?\n[ \t]*(\S.*?)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)

# What kind of leads. The word that matters, whatever it is dressed in.
FAMILIES = (
    ("iul", re.compile(r"\biuls?\b", re.IGNORECASE)),
    ("fex", re.compile(r"\bfex\b|\bfinal\s+expense\b", re.IGNORECASE)),
    ("mtg", re.compile(r"\bmtg\b|\bmortgage\b", re.IGNORECASE)),
    ("vet", re.compile(r"\bvets?\b|\bveterans?\b", re.IGNORECASE)),
    ("widows", re.compile(r"\bwidows?\b", re.IGNORECASE)),
    ("phnx", re.compile(r"\bpho?e?nix\b|\bphnx\b|\bphx\b", re.IGNORECASE)),
    ("ascend", re.compile(r"\bascend\b", re.IGNORECASE)),
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

# The customer level, which is part of what the leads are rather than a
# description of them - "when its uprise/phnx/phoenix always include this".
# Ascend is one of these as well as a family of its own, the same way Phoenix
# is: Ascend Standard and Phoenix Standard are two different things to buy.
# A card can name it away from the lead type line entirely: "Lead Type: vets"
# up top and "uprise- $350/week standard" three lines down is Uprise leads,
# and filing that as plain standard vets loses the half that says which.
LINE = re.compile(
    r"\buprise\b|\bpho?e?nix\b|\bphnx\b|\bphx\b|\bascend\b", re.IGNORECASE
)


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
        return bool((self.lead_type or self.stated) and self.launch)

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
    """The lead type as the form wrote it, or "" if the line isn't there.

    The *last* one when a card carries more than one. A correction gets
    written by pasting a fresh block underneath rather than by editing what is
    already there - Colton Ramon's card says "Text Verified Veteran Plus" up
    top and "Lead type: OTP Widows" further down, and the one further down is
    the one that is current.
    """
    found = sorted(
        list(LEAD_TYPE_LINE.finditer(text or ""))
        + list(LEAD_TYPE_BLOCK.finditer(text or "")),
        key=lambda at: at.start(),
    )
    return " ".join(found[-1].group(1).split()) if found else ""


def family_of(text: str) -> str | None:
    for name, pattern in FAMILIES:
        if pattern.search(text or ""):
            return name
    return None


def families_in(text: str) -> set[str]:
    """Every kind of leads a phrase names, not just the first one found.

    "OTP WIDOW VET" names two. Which of them it means - widows, veterans, or
    widows of veterans as one product - is not something to work out from the
    words, and `family_of` picking whichever comes first in the table is a
    coin toss dressed as an answer.
    """
    return {
        name for name, pattern in FAMILIES if pattern.search(text or "")
    }


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


# A lead type is a phrase, not a sentence. Gustin Elrod's card says "Gustin
# Elrod paid for OTP IUL leads and with Don A setup for immediate launch" -
# true, and not the name of anything you can buy.
MOST_WORDS = 8


def named_lead_types(text: str) -> list[str]:
    """Every phrase on the card that names a kind of leads, in the order they
    are written.

    In order, because that is what decides which one is current: a correction
    gets pasted below rather than typed over the top. Aliana Arevalo's card
    says "Lead Type: Text Verified Spanish IUL" and then, four lines down,
    "14 Spanish OTP IUL" - and the one further down is the one that counts.

    A line naming the customer level counts even when it names no family.
    Benji Missey's card says "Lead Type: vets" and then "uprise- $350/week
    standard", and that second line says which leads they are in the words the
    order was written in.

    The Lead Type field is here on its own terms - it is one of these lines -
    and only falls back to being read as a field when no line qualifies.
    """
    said = []
    for line in (text or "").splitlines():
        phrase = tidy_lead_type(" ".join(line.split()))
        if not phrase or len(phrase.split()) > MOST_WORDS:
            continue
        # A level on its own is a modifier, not a lead type. Sebastian
        # Espinoza's card has "Uprise" sitting alone on a line above "Phoenix
        # Standard" - it says something about the leads without saying what
        # they are, and `with_line` is what grafts it back on.
        if family_of(phrase) or (LINE.search(phrase) and tier_of(phrase)):
            said.append(phrase)
    if said:
        return said
    field = find_lead_type(text)
    return [field] if field else []


def tier_word(text: str) -> str:
    """The word the card used for the tier, not the tier it means.

    "otp" and "text verified" and "plus" are one tier and three words, and the
    card's own word is the one to repeat back - inventing a different one
    reads as RYTE having decided something.
    """
    # The lines nobody labelled first. "30 otp vtes" is somebody writing down
    # the order; "Package Selected: Text Verified" is the form's own field, and
    # it says the same tier in the words the form uses rather than theirs.
    lines = (text or "").splitlines()
    loose = "\n".join(line for line in lines if not _LABEL_PREFIX.match(line.strip()))
    for where in (loose, text or ""):
        found = STANDARD.search(where) or PLUS.search(where)
        if found:
            return found.group(0)
    return ""


def stated_lead_type(text: str) -> str:
    """The most specific thing the card says the leads are.

    No board to check against - this is for the lines that go on a setup card,
    which has a checklist per person rather than per lead type, and for saying
    what an agent is before anything has been matched. A phrase naming its
    tier beats a phrase that leaves it out.

    And where the best phrase names no tier but the card does somewhere else,
    the card's word goes in front of it. "Lead Type: vets" with "30 otp vtes"
    below is OTP vets, and filing that as plain vets throws away the half that
    says which vets. The same goes for the customer level - see `with_line`.
    """
    said = named_lead_types(text)
    if not said:
        return with_line(find_lead_type(text), text)

    # The last one, not the first. A card that says it twice has been
    # corrected, and the correction is written underneath.
    tiered = [phrase for phrase in said if tier_of(phrase)]
    if tiered:
        return with_line(tiered[-1], text)

    word = tier_word(text)
    return with_line(f"{word} {said[-1]}".strip() if word else said[-1], text)


def line_word(text: str) -> str:
    """Uprise or Phoenix as the card wrote it, or "" if it names neither."""
    found = LINE.search(text or "")
    return found.group(0) if found else ""


def with_line(phrase: str, text: str) -> str:
    """The phrase with the card's Uprise/Phoenix word in front, if it needs one.

    Only when the phrase names no level of its own. "Phoenix Standard" on a
    card that also says "Uprise" is already saying which, and a card naming
    both is naming one thing twice rather than two things.
    """
    if not phrase or LINE.search(phrase):
        return phrase
    word = line_word(text)
    return f"{word} {phrase}" if word else phrase


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



def best_lead_type(text: str, existing: list[str]) -> tuple[str, str | None, list[str]]:
    """(the phrase to use, the checklist it lands on, what else could fit).

    A card naming its tier outright beats one that leaves it to be worked out:
    "Lead Type: VETS" says which leads and "UPRISE PHX PLUS" says which leads
    *and* which tier, so the second one is the one to act on - as long as the
    board actually has that checklist.

    Two phrases naming different checklists, both with a tier, is the card
    disagreeing with itself - unless one of them is the card's own Lead Type
    line, which is the current word on it: a correction gets pasted below and
    `find_lead_type` reads the lowest one. Otherwise both come back and a
    person is asked.
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
        # Colton Ramon's card says "Text Verified Veteran Plus" up top and
        # "Lead type: OTP Widows" below. That is not a card to ask about -
        # somebody corrected it, and the labelled line is where they said so.
        field = find_lead_type(text)
        for _tiered, phrase, landed in picked:
            if field and phrase == field:
                return phrase, landed, []
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


# ----------------------------------------- what was actually set up, and for whom

# The confirmation Therese and Kathleen leave when a setup is finished. Three
# real ones, all with the lead type sitting immediately before the words:
#
#   "OTP SPANISH FEX ON DISTRO HUB setup is complete for ALIANA AREVALO"
#   "OTP VET ON DISTRO HUB setup is complete for MILLS FINANCIAL LLC"
#   "Updated previous setup of BRODY SULLIVAN for OTP VET ON DISTRO HUB"
#
# Four words back, no more. Further and a surname starts being read as a lead
# type, and this comparison exists to catch a wrong one - it must not invent
# them.
DISTRO_HUB = re.compile(
    r"((?:[\w/&+-]+[ \t]+){0,3}[\w/&+-]+)[ \t]+on[ \t]+distro[ \t]+hub",
    re.IGNORECASE,
)


# The words a lead type is made of. Anything in front of the first of them is
# somebody's name or a connecting word - "Updated previous setup of BRODY
# SULLIVAN for OTP VET" is four words back, and two of them are Brody.
_LEAD_WORDS = tuple(pattern for _name, pattern in FAMILIES + QUALIFIERS) + (
    STANDARD, PLUS, LINE,
)


def _from_the_leads(phrase: str) -> str:
    """The phrase from its first lead-type word onwards."""
    words = (phrase or "").split()
    for at, word in enumerate(words):
        if any(pattern.search(word) for pattern in _LEAD_WORDS):
            return " ".join(words[at:])
    return ""


def setup_said(comments) -> str:
    """The lead type a Distro Hub confirmation says was set up, or "".

    The last confirmation on the card, because a setup gets redone: Brody
    Sullivan's comment begins "Updated previous setup of".
    """
    found = [
        _from_the_leads(match.group(1))
        for said in comments or []
        for match in DISTRO_HUB.finditer(said or "")
    ]
    found = [phrase for phrase in found if phrase]
    return found[-1] if found else ""


def wrong_setup(ordered: str, comments) -> tuple[str, str] | None:
    """(what was ordered, what was set up) when they are not the same leads.

    Compared by shape rather than by wording, so "OTP Vets - 30 more OTP vets"
    against "OTP VET" is a match and nobody is bothered about it. What is not
    a match is Vets against Final Expense, and that is somebody's money going
    to the wrong campaign with the card in Done and nothing looking wrong.

    None when either side says nothing, or says too much. A card with no
    confirmation yet is not a card set up wrongly; neither is one whose
    confirmation nobody wrote a lead type into; and neither is one where a
    phrase names two kinds of leads at once. "OTP WIDOW VET" is one campaign
    or it is two, and a check that cannot tell has nothing to report - eight
    correct setups raised as wrong is a check nobody will read the ninth time.
    """
    said = setup_said(comments)
    if not ordered or not said:
        return None
    if len(families_in(ordered)) != 1 or len(families_in(said)) != 1:
        return None
    return None if shape_of(ordered) == shape_of(said) else (ordered, said)


# ------------------------------------------------------ when they go live

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
# "live fri, aug 28" is how it is actually written about half the time.
_SHORT_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# An existing agent buying more of what they already have. "Add to his active
# order - Wednesday, August 26" is not a launch, so none of the words below
# appear on the card - and it is the usual same-day job, onto that day's Lead
# Order, Ads and Ops cards.
ADD_TO_ORDER = re.compile(
    r"\badd(?:ed|ing)?\s+(?:it\s+|them\s+|these\s+)?to\b[^.\n]*\border\b",
    re.IGNORECASE,
)

# The ways people say when somebody goes live, strongest first. "Launch date"
# is unmistakable; a bare "live" is not - "Live transfer leads" is a product -
# so it is looked at last and only counts if a date is sitting next to it.
_WHEN_SAID = (
    re.compile(r"[^.\n]*\blaunch\s*date\b[^.\n]*", re.IGNORECASE),
    re.compile(r"[^.\n]*\b(?:launch(?:ing|es|ed)?|go(?:es|ing)?\s+live)\b[^.\n]*", re.IGNORECASE),
    re.compile(
        r"[^.\n]*\badd(?:ed|ing)?\s+(?:it\s+|them\s+|these\s+)?to\b[^.\n]*\border\b[^.\n]*",
        re.IGNORECASE,
    ),
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


def _weekday_named(sentence: str) -> int | None:
    """Which day of the week a sentence names, if it names one."""
    said = (sentence or "").lower()
    for index, name in enumerate(_WEEKDAYS):
        if re.search(rf"\b{name}\b|\b{_SHORT_DAYS[index]}\b", said):
            return index
    return None


def launch_conflict(text: str, *, today: date) -> str:
    """When the card names a weekday and a date that are not the same day.

    "live fri, aug 27" - August 27 is a Thursday. One of the two is a typo,
    and which one is not RYTE's to decide: taking the date puts the agent live
    a day early if the writer meant the Friday, taking the weekday puts them
    live a day late if the writer meant the date, and neither mistake looks
    like anything on the board afterwards.

    Only the sentence the launch date was read out of, so a "see you Friday"
    somewhere else on the card is not an argument with anything.
    """
    for pattern in _WHEN_SAID:
        for sentence in pattern.findall(text or ""):
            found = _date_in(sentence, today=today)
            if found is None:
                continue
            named = _weekday_named(sentence)
            if named is None or named == found.weekday():
                return ""
            return (
                f"The card says {_WEEKDAYS[named].title()} and "
                f"{found:%B} {found.day}, which is a {found:%A}. "
                "Which day do they go live?"
            )
    return ""


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
    # "Add to his active order" with no date on it. Adding to an order that
    # already exists is the same-day job, so today is what it means rather
    # than a guess about what it might mean.
    if ADD_TO_ORDER.search(said):
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


def read_agent(
    card: dict, *, text: str, today: date, comments: tuple[str, ...] = ()
) -> Agent | None:
    """One agent card read into what RYTE needs, or None if it isn't one.

    `text` is the description. The comments are read for the launch date,
    because it turns up in either and which one it is in is nobody's decision
    to make - but never for what the leads are.

    That split is the whole point. These cards get copied from one agent to
    the next with the comments attached, and a week-old "OTP SPANISH FEX on
    Distro Hub setup is complete" is a note about somebody else's order. Read
    as a lead type it beat a description that plainly said Spanish IUL.

    A description that names no lead type comes back with none, and the card
    is held for a person. Guessing one out of the comments is how a stale note
    ends up on somebody's order with nothing on the board looking wrong.
    """
    title = str(card.get("name", ""))
    if not is_agent_card(title):
        return None

    said = text or ""
    everything = "\n".join([said, *comments])
    agent = Agent(
        name=agent_name(title),
        card_id=str(card.get("id") or ""),
        url=str(card.get("shortUrl") or card.get("url") or ""),
        lead_type=find_lead_type(said),
        launch=find_launch(everything, today=today),
        # What gets matched against the board's checklists, so it carries the
        # same restriction.
        said=said,
        stated=stated_lead_type(said),
        note=launch_conflict(everything, today=today),
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
    # What the card is read as saying, not only the field it wrote it in. A
    # card can name the leads three times over and still not have the form's
    # own line on it, and refusing that one is refusing a card that says
    # exactly what it is.
    if needs_lead_type and not (agent.lead_type or agent.stated):
        missing.append("a lead type")
    return f"I can't find {' or '.join(missing)} on this card." if missing else ""


# ------------------------------------------- the card tomorrow's ones go on

SETUP_TITLE = "Agent Setup Going Live"
_SETUP_CARD = re.compile(r"agent\s+setup", re.IGNORECASE)
# No word boundary in front: the real ones are typed "Wednesday08/26" as often
# as "Wednesday 08/26", and a boundary needs a space that isn't there.
_SETUP_DATE = re.compile(r"(\d{1,2})/(\d{1,2})(?!\d)")


# Fridays make one card for the whole weekend - "Agent Setup Going Live
# Saturday-Monday 08/22-08/24" - so an agent going live on the Sunday is set
# up on the Friday along with the Saturday's.
SATURDAY = 5


def weekend_span(day: date) -> tuple[date, date] | None:
    """(Saturday, Monday) when `day` is a Saturday, else None."""
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


def is_setup_card(title: str) -> bool:
    """Whether a card is one of the "Agent Setup Going Live" ones."""
    return bool(_SETUP_CARD.search(title or ""))


def _near(month: int, dom: int, near: date) -> date | None:
    """A bare MM/DD from a card title as a real date, in the nearest year.

    The titles carry no year, so December's card read in January has to come
    out as next month rather than eleven months ago.
    """
    for year in (near.year, near.year + 1, near.year - 1):
        try:
            when = date(year, month, dom)
        except ValueError:  # 02/29 in a year that hasn't got one
            continue
        if abs((when - near).days) <= 182:
            return when
    return None


def setup_starts(title: str, near: date) -> date | None:
    """The first day a setup card's agents go live, as a real date.

    The first date, not the last: the Friday card runs Saturday to Monday, and
    the whole weekend's worth of setting up is done before the Saturday.
    """
    found = _SETUP_DATE.findall(title or "")
    if not found:
        return None
    return _near(int(found[0][0]), int(found[0][1]), near)


def setup_worked_on(title: str, near: date) -> date | None:
    """The day the card is worked, which is the day before its agents go live.

    Everything about where these cards sit follows from this. A card headed
    Thursday is Wednesday's work, so it has to be in In Que on the Tuesday
    evening for nine on Wednesday morning to put it in Today - and by six on
    Wednesday its work is finished and it walks on like anything else.
    """
    starts = setup_starts(title, near)
    return starts - timedelta(days=1) if starts is not None else None


def find_setup_card(cards: list[dict], day: date) -> dict | None:
    """The setup card covering a day, wherever on the board it has got to.

    Found by the dates in its title rather than by list: they are made in
    Automation Department and do not always stay there, and a second one made
    because the first was moved is worse than looking everywhere.
    """
    for card in cards or []:
        title = str(card.get("name", ""))
        if is_setup_card(title) and setup_covers(title, day):
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
