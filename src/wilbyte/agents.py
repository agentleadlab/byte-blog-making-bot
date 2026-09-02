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

# What actually sits on the own-setup checklist: Basic, Instant and FB, in
# every combination. Instant and FB name no tier of their own and belong here
# anyway.
OWN_SETUP_WORDS = re.compile(r"\binstant\b|\bfb\b|\bfacebook\b|\bbasics?\b", re.IGNORECASE)


def is_own_setup(lead_type: str) -> bool:
    """Whether these leads belong on the own-setup checklist.

    Basic, Instant and FB set themselves up. A line product does not: Uprise,
    Phoenix and Ascend are ordered, and Standard is a tier of them rather than
    a synonym for self-setup, so "UPRISE STANDARDS- $350/WEEK" gets the same
    checklist of its own that PHNX Plus does.

    This used to send every Standard here, on the reading that Standard *was*
    the self-setup half. It isn't: Carsen Anderson landed on own setup beside
    six Basic Spanish IUL lines, and he had bought Uprise. Basic and Standard
    reduce to one tier for comparing lead types, which is right for that job
    and is what made the two look alike here.
    """
    said = lead_type or ""
    if OWN_SETUP_WORDS.search(said):
        return True
    if LINE.search(said):
        return False
    return tier_of(said) == "standard"

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
    # SIUL is Spanish IUL, BC is Blue Collar IUL, and a trucker is an IUL
    # too - all three are IUL products on this board, and the checklists say
    # so outright: "OTP Blue Collar IUL", "OTP IUL TRUCKER". So a card saying
    # only "OTP Truckers" still names the family it belongs to.
    ("iul", re.compile(r"\biuls?\b|\bsiul\b|\bbc\b|\btruckers?\b", re.IGNORECASE)),
    ("fex", re.compile(r"\bfex\b|\bfinal\s+expense\b", re.IGNORECASE)),
    ("mtg", re.compile(r"\bmtg\b|\bmortgage\b", re.IGNORECASE)),
    ("vet", re.compile(r"\bvets?\b|\bveterans?\b", re.IGNORECASE)),
    ("widows", re.compile(r"\bwidows?\b", re.IGNORECASE)),
    # Uprise and Phoenix are one product under two names - "PHNX Standard" and
    # "UPRISE STANDARDS" are the same leads - so they reduce to one family and
    # a card written either way lands on the same checklist.
    ("phnx", re.compile(r"\bpho?e?nix\b|\bphnx\b|\bphx\b|\buprise\b", re.IGNORECASE)),
    ("ascend", re.compile(r"\bascend\b", re.IGNORECASE)),
)

# Variants that sit alongside the plain one and are not it. "OTP Spanish IUL"
# is a different checklist from "OTP IUL Plus" and filing into the wrong one
# sends somebody's leads to the wrong place.
QUALIFIERS = (
    # "22 OTP BC leads" and "25 OTP SIUL" are how these get written when
    # somebody is typing fast. Both went onto the board as checklists of their
    # own because neither abbreviation was known.
    ("blue collar", re.compile(r"\bblue\s*collar\b|\bbc\b", re.IGNORECASE)),
    ("spanish", re.compile(r"\bspanish\b|\bsiul\b", re.IGNORECASE)),
    # Truckers are their own product, not IUL Plus with a note on it: Marcel
    # Trifan's "TEXT VERIFIED TRUCKER IUL" read as either and had to be asked
    # about, and the answer was always the trucker checklist.
    ("trucker", re.compile(r"\btruckers?\b", re.IGNORECASE)),
)

# Franklin's rule: standard or basic means Standard; text-verified or OTP
# means Plus. OTP and text-verified are the same thing said two ways.
STANDARD = re.compile(r"\bstandards?\b|\bbasics?\b", re.IGNORECASE)
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


# Every word the reading turns on. A card is typed in a hurry by somebody with
# forty of them to get through, and "OTP TRCUKER IUL" is the same order as
# "OTP Trucker IUL" - read as a different one, it goes on the board as an
# agent set up on leads he never bought.
_VOCABULARY = (
    "trucker", "truckers", "spanish", "collar", "blue", "veteran", "veterans",
    "widow", "widows", "mortgage", "expense", "final", "standard", "standards",
    "basic", "basics", "plus", "instant", "facebook", "uprise", "phoenix",
    "ascend", "verified", "text",
)

# Close enough to be the same word, far enough not to catch a different one.
# "vet" and "fex" are three letters and one apart from plenty; short words are
# left alone entirely for that reason.
_CLOSE_ENOUGH = 0.82
_SHORTEST_TO_FIX = 5


def despell(text: str) -> str:
    """The same words, with the obvious typos put right.

    Only words nobody meant: a word already in the vocabulary is left alone,
    and so is anything short, where one letter's difference is usually another
    word rather than a slip.
    """
    from difflib import get_close_matches

    known = set(_VOCABULARY)

    def fixed(word: str) -> str:
        low = word.lower()
        if len(low) < _SHORTEST_TO_FIX or low in known:
            return word
        near = get_close_matches(low, _VOCABULARY, n=1, cutoff=_CLOSE_ENOUGH)
        if not near or sorted(near[0]) != sorted(low):
            # Same letters in a different order is a typo. Different letters
            # is a different word, and correcting those invents orders.
            return word
        return near[0].upper() if word.isupper() else near[0]

    return re.sub(r"[A-Za-z]+", lambda found: fixed(found.group(0)), text or "")


def qualifiers_of(text: str) -> frozenset[str]:
    return frozenset(
        name for name, pattern in QUALIFIERS if pattern.search(text or "")
    )


def shape_of(text: str) -> tuple:
    """What a lead type reduces to, for comparing two ways of writing it.

    "Text Verified IUL Plus" and "OTP IUL Plus" are the same leads: OTP and
    text-verified are one thing said two ways, so both reduce to (iul, plus).
    """
    said = despell(text)
    return (family_of(said), tier_of(said), qualifiers_of(said))


# "$1050/WEEK- UPRISE PHX PLUS" is a price and then a lead type. The price is
# not part of what the leads are called.
_PRICE_PREFIX = re.compile(r"^\$\s*[\d,.]+\s*(?:/\s*\w+)?\s*[-–—:]*\s*")

# "Package Selected: Basic Spanish IUL" is a label and then a lead type. The
# label is the form's word for the field, not anything about the leads.
_LABEL_PREFIX = re.compile(r"^[A-Za-z][A-Za-z ]{0,30}:\s*")


# How somebody types an order into a card: "let's do 40 Text-Verified Veteran
# Leads". The sentence is not the lead type, and it goes onto three people's
# checklists exactly as written unless it comes off here.
# "Tommy Vereau here paid for OTP vets" - whoever typed it introduced
# themselves first. Everything up to the buying word is who, not what.
_WHO_BOUGHT = re.compile(
    r"^.*?\b(?:paid\s+for|paying\s+for|ordered|order(?:ing)?|bought|buying|"
    r"purchased|wants?|needs?|signed\s+up\s+for|is\s+(?:getting|taking)|"
    r"would\s+like)\s+(?:the\s+|some\s+|a\s+)?",
    re.IGNORECASE,
)

_LEAD_IN = re.compile(
    r"^(?:(?:let'?s|lets|let\s+us|we(?:'ll|\s+will)?|i'?ll|please|can\s+we)\s+)?"
    r"(?:do|get|give|book|send|start|add|run)\s+(?:me\s+|us\s+|them\s+|him\s+|her\s+)?",
    re.IGNORECASE,
)

# A note about the sale rather than about the leads. Dropped, because the line
# is read by whoever loads the leads and none of this changes what they load.
# "Uprise" is the exception - it says which product the order is.
_SALES_NOTE = re.compile(
    r"\s*\b(?:record\s+)?(?:unsigned|signed|discount|discounted|record)\b\s*$",
    re.IGNORECASE,
)

# "40 OTP Vets - RECORD discount". Everything after the dash is a note unless
# it names leads of its own.
_TRAILING = re.compile(r"^(?P<head>.+?)\s*[-–—,]\s*(?P<tail>[^-–—,]+)$")

KEEP_ANYWAY = re.compile(r"\buprise\b", re.IGNORECASE)


def _has_count(said: str) -> bool:
    return bool(re.search(r"\d", said or ""))


def _drop_notes(said: str) -> str:
    """Take the sales talk off a line without taking the order with it."""
    while True:
        found = _TRAILING.match(said)
        if not found:
            break
        head, tail = found.group("head").strip(), found.group("tail").strip()
        if KEEP_ANYWAY.search(tail):
            break
        if families_in(tail):
            # Both halves name leads. "OTP Vets - 25 OTP Vets" is one order
            # written twice, and the half with the count is the useful one.
            if families_in(head) and not _has_count(head) and _has_count(tail):
                said = tail
                continue
            break
        said = head

    while True:
        shorter = _SALES_NOTE.sub("", said).strip(" -–—:,")
        if shorter == said or not shorter:
            break
        said = shorter
    return said


def tidy_lead_type(phrase: str) -> str:
    """A lead type with the label, the money, the sales talk and the
    punctuation taken off.

    What is left is what the person loading the leads needs: how many, and of
    what. The count stays; "RECORD discount" and "unsigned" do not, because
    neither changes what gets loaded. Anything saying Uprise stays, because
    that does.
    """
    said = " ".join((phrase or "").split())
    said = _LABEL_PREFIX.sub("", said)
    said = _PRICE_PREFIX.sub("", said).strip(" -–—:")
    said = _WHO_BOUGHT.sub("", said, count=1)
    said = _LEAD_IN.sub("", said, count=1)
    if not KEEP_ANYWAY.search(said):
        said = _drop_notes(said)
    return said.strip(" -–—:,")


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
    said = _phrases_in(text)
    if said:
        return said
    field = find_lead_type(text)
    return [field] if field else []


def _phrases_in(text: str) -> list[str]:
    """The lines of `text` that name a kind of leads, in order."""
    said = []
    for line in (text or "").splitlines():
        phrase = tidy_lead_type(" ".join(line.split()))
        if not phrase or len(phrase.split()) > MOST_WORDS:
            continue
        # A level on its own is a modifier, not a lead type. Sebastian
        # Espinoza's card has "Uprise" sitting alone on a line above "Phoenix
        # Standard" - it says something about the leads without saying what
        # they are, and `with_line` is what grafts it back on.
        if _bare_line(phrase):
            continue
        if family_of(phrase) or (LINE.search(phrase) and tier_of(phrase)):
            said.append(phrase)
    return said


def ordered_lead_types(text: str) -> list[str]:
    """Every *separate* order on the card, or [] when there is only one.

    Catherine Y Barney's card ends "15 OTP VETS" then "15 OTP FEX". She paid
    for both, and filing her under the second alone lost half the order.

    Only the unlabelled lines stack. A "Lead Type:" field is the form's own,
    and a second one is a correction rather than a second purchase - Colton
    Ramon's card carries two of them naming different families and he ordered
    once. Somebody typing lines at the bottom of a card is doing something
    else, and each of those lines brings its own count.

    The same family twice is still one order, corrected: Aliana Arevalo's card
    says Spanish IUL twice, and the lower one is the current wording.

    Two phrases belong to one order when the families they name *overlap* at
    all, not when a single family picked off each of them happens to differ.
    Tyler Menge's card says "veteran-widows" where Spark wrote it and "OTP
    Widows" where the team did, and that is one purchase of ten leads written
    twice - it was read as two because "veteran-widows" names vet and widows
    both, and picking one of the two made it disagree with the other line.
    """
    loose = "\n".join(
        line for line in (text or "").splitlines()
        if not _LABEL_PREFIX.match(line.strip())
    )
    said = _phrases_in(loose)
    if len(said) < 2:
        return []

    # (the families this order names, the latest phrase for it). Merging on
    # overlap, so a phrase naming two families joins both rather than starting
    # a third order of its own.
    orders: list[tuple[set[str], str]] = []
    for phrase in said:
        named = set(families_in(phrase)) or {phrase.casefold()}
        joined = [held for held in orders if held[0] & named]
        for held in joined:
            named |= held[0]
            orders.remove(held)
        orders.append((named, phrase))

    if len(orders) < 2:
        return []

    kept = {phrase for _families, phrase in orders}
    return [with_line(phrase, text) for phrase in said if phrase in kept]


# How two orders are written on one line. The setup card has a checklist per
# person, so an agent gets one line there however much they bought.
ORDER_JOIN = " + "


def stated_orders(text: str) -> str:
    """What was ordered, both halves when there were two of them."""
    several = ordered_lead_types(text)
    if len(several) > 1:
        return ORDER_JOIN.join(several)
    return stated_lead_type(text)


# A note about who the agent is, not about what they bought. "Uprise Agent"
# sits on Jack Duval's card beside "5 OTP VETS" and names the agency he is
# under; read as an order it becomes a line nobody can file, and filing it
# somewhere would put five VET leads under Phoenix Plus.
_WHO_NOT_WHAT = re.compile(
    r"^\s*[\w'&.-]*\s*(?:agents?|agency|team|group|partners?|financial)\s*$",
    re.IGNORECASE,
)


def an_order(part: str) -> bool:
    """Whether a line names something bought, rather than who bought it.

    A phrase naming no kind of leads and ending in "agent" is somebody's
    agency. A phrase naming leads is an order however it is written.
    """
    said = " ".join((part or "").split())
    if not said:
        return False
    # The note is checked first on purpose. "Uprise" is one of the words that
    # names a lead family, so "Uprise Agent" reads as an order until you look
    # at the shape of it: one word and then "Agent", nothing bought.
    return not _WHO_NOT_WHAT.match(said)


def order_parts(label: str) -> list[str]:
    """A checklist line back into the orders it names.

    The setup card gives an agent one line however much they bought, so two
    orders are written on it as "15 OTP VETS + 15 OTP FEX". The Lead Order card
    is filed by lead type, so there the same agent needs a line under each.

    Only the joiner RYTE writes is split on. "Text-Verified Final Expense and
    Vets" is one lead type with an "and" in the middle of it, and a line typed
    by hand is left exactly as it was typed.
    """
    parts = [part.strip() for part in (label or "").split(ORDER_JOIN) if part.strip()]
    # An agency note alongside a real order is dropped; on its own it is left
    # alone, so a line RYTE can't read still gets reported rather than vanish.
    ordered = [part for part in parts if an_order(part)]
    parts = ordered or parts
    return parts or ([label.strip()] if (label or "").strip() else [])


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


# A line word and nothing else. Phoenix is a family as well as a line, so
# "Phoenix Campaign" names leads and a bare "Uprise" does not - the difference
# is whether anything else is on the line with it.
_JUST_A_LINE = re.compile(r"^(?:uprise|pho?e?nix|phnx|phx|ascend)$", re.IGNORECASE)


def _bare_line(phrase: str) -> bool:
    return bool(_JUST_A_LINE.match(" ".join((phrase or "").split())))


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

    Two checklists that reduce to the *same* leads are not two options, they
    are one product written twice: Marcel Trifan's card offered "OTP IUL
    TRUCKER" or "OTP TRUCKERS", which are the same thing, and asking which
    only asked somebody to pick a spelling. Those get the one whose name says
    more of what was ordered.
    """
    found = candidates(lead_type, existing, tier=tier)
    if len(found) == 1:
        return found[0]
    if len(found) > 1 and len({shape_of(name) for name in found}) == 1:
        return _closest(lead_type, found)
    return None


def _words_of(text: str) -> set[str]:
    return {word for word in re.split(r"[^A-Za-z0-9]+", (text or "").lower()) if word}


def _closest(lead_type: str, names: list[str]) -> str:
    """The checklist whose name says most of what was ordered.

    Only ever asked among checklists that mean the same leads, so this decides
    a spelling and never a product. Ties keep the board's own order, which is
    the one somebody looking at the card would land on first.
    """
    asked = _words_of(despell(lead_type))
    return max(names, key=lambda name: len(_words_of(despell(name)) & asked))



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
        # Compared tidied: the labelled line reads "OTP Widows - 50 OTP
        # Widows" on the card and "50 OTP Widows" once the sales talk is off,
        # and those have to be recognised as the same line.
        field = tidy_lead_type(find_lead_type(text))
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

    None whenever the two cannot honestly be compared, which is often:

    - No confirmation yet, or one nobody wrote a lead type into.
    - A phrase naming two kinds of leads at once. "OTP WIDOW VET" is one
      campaign or it is two, and nothing here can tell. This also covers an
      agent who genuinely ordered two things: setting Catherine Y Barney up on
      vets is right, and so is setting her up on FEX, so there is nothing to
      compare a single confirmation against.
    - Either side naming a customer level. The Distro Hub is organised by
      level rather than by lead type, so an Uprise order is confirmed as "PHX
      STANDARD" and the two words have nothing to do with each other. Landon
      Brown ordered Uprise vets, was set up correctly, and was raised as
      wrong.

    Eleven correct setups raised as wrong and none caught is a check nobody
    reads the twelfth time, so where it cannot tell it says nothing.
    """
    said = setup_said(comments)
    if not ordered or not said:
        return None
    if line_word(ordered) or line_word(said):
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

# "Launch Date: As soon as is possible for the team." A date somebody declined
# to pick, which means the first day anybody can do it - today. Reading it as
# no date at all parks the agent in Franklin's list waiting for a day that is
# never going to be written.
ASAP = re.compile(
    r"\basap\b"
    r"|\bas\s+soon\s+as\s+(?:is\s+)?possib\w*"
    r"|\bas\s+soon\s+as\s+(?:you|we|they|the\s+team)\s+can\b"
    r"|\bright\s+away\b|\bat\s+once\b",
    re.IGNORECASE,
)

# An existing agent buying more of what they already have. "Add to his active
# order - Wednesday, August 26" is not a launch, so none of the launch words
# appear on the card - and it is the usual same-day job, onto that day's Lead
# Order, Ads and Ops cards.
#
# "order" is not always the word: Sebastian Salas's card says "add it to his
# current once fulfilled", and reading that as a card somebody forgot to date
# left fifty veteran leads waiting for an answer.
ADD_TO_ORDER = re.compile(
    r"\badd(?:ed|ing)?\s+(?:it\s+|them\s+|these\s+|him\s+|her\s+)?"
    r"to\b[^.\n]*\b(?:order|current|existing|active|batch|list)\b"
    r"|\bonce\s+(?:it\s+is\s+|its\s+|it's\s+)?fulfill?ed\b"
    r"|\bwhen\s+(?:it\s+is\s+|its\s+|it's\s+)?fulfill?ed\b"
    r"|\bafter\s+(?:his|her|their)\s+current\b",
    re.IGNORECASE,
)

# The ways people say when somebody goes live, strongest first. "Launch date"
# is unmistakable; a bare "live" is not - "Live transfer leads" is a product -
# so it is looked at last and only counts if a date is sitting next to it.
_WHEN_SAID = (
    re.compile(r"[^.\n]*\blaunch\s*date\b[^.\n]*", re.IGNORECASE),
    # "Going live" is one way of saying it. "Leads going by tomorrow" and
    # "leads go out Friday" are two more, and neither contains the word live.
    re.compile(
        r"[^.\n]*\b(?:launch(?:ing|es|ed)?"
        r"|go(?:es|ing)?\s+(?:live|out|by)"
        r"|leads?\s+(?:are\s+)?go(?:es|ing)?)\b[^.\n]*",
        re.IGNORECASE,
    ),
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


# Short forms that cost a card its date. "LIVE TOM. SEPT 3" is read as the
# sentence "LIVE TOM" - the full stop after the abbreviation ends it, and the
# date sitting right there never gets looked at.
_SHORTHAND = (
    (re.compile(r"\btom\.(?=\s|$)|\btomm?\.?(?=\s|$)|\btmrw?\b|\btmr\b", re.IGNORECASE),
     "tomorrow"),
    (re.compile(r"\btod\.(?=\s|$)|\btdy\b", re.IGNORECASE), "today"),
    # "Sept. 3" and "Aug. 28" - the stop belongs to the abbreviation, not to
    # the end of what somebody was saying.
    (re.compile(r"\b(jan|feb|mar|apr|jun|jul|aug|sept?|oct|nov|dec)\.", re.IGNORECASE),
     r"\1"),
)


def spelled_out(text: str) -> str:
    """The same card with the shorthand written out.

    Only forms that mean a day. Everything else is left exactly as typed - a
    card is somebody's words and rewriting it further would be guessing at
    them.
    """
    said = text or ""
    for pattern, instead in _SHORTHAND:
        said = pattern.sub(instead, said)
    return said


def find_launch(text: str, *, today: date) -> date | None:
    """When the agent goes live, read out of the sentence that says so.

    A written-out date wins over "today", over "ASAP" and over a weekday. They
    agree when the card is read the day it was written and disagree when it
    isn't, and the calendar date is the one that stays true - so every sentence
    is looked at for a real date before any of them is read for a relative one.
    """
    text = spelled_out(text)
    said = [
        sentence
        for pattern in _WHEN_SAID
        for sentence in pattern.findall(text or "")
    ]
    for sentence in said:
        found = _written_date_in(sentence, today=today)
        if found:
            return found
    for sentence in said:
        found = _date_in(sentence, today=today)
        if found:
            return found

    # A top-up has no launch date because it isn't a launch - it goes on as
    # soon as it is set up. Looked for across the whole card rather than in a
    # launch sentence, because a card like this has no launch sentence at all.
    if ADD_TO_ORDER.search(text or ""):
        return today
    return None


def _written_date_in(sentence: str, *, today: date) -> date | None:
    """The calendar date in a sentence, ignoring "today" and the weekdays."""
    numeric = _NUMERIC.search(sentence)
    if numeric:
        month, day, year = numeric.groups()
        return _made(int(month), int(day), year, today=today)

    written = _MONTH_DAY.search(sentence)
    if written:
        month = _MONTHS[written.group(1)[:3].lower()]
        return _made(month, int(written.group(2)), None, today=today)
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
    if re.search(r"\btoday\b|\bimmediate(?:ly)?\b", said) or ASAP.search(said):
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
        # Both halves when two things were ordered - this is what the line on
        # every checklist says, and half an order is worse than none.
        stated=stated_orders(said),
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


# A checklist line RYTE wrote: the agent's card, then what the leads are.
_LINKED_ITEM = re.compile(r"(https?://(?:www\.)?trello\.com/c/\S+)\s*(.*)", re.IGNORECASE | re.DOTALL)


# The bit of a Trello card link that identifies the card. Trello writes the
# same card two ways - "trello.com/c/tIA7tp2B" and
# "trello.com/c/tIA7tp2B/431-new-agent-jorge-flores" - so comparing whole
# links reads one agent as two and files them twice.
_CARD_TOKEN = re.compile(r"/c/([A-Za-z0-9]+)", re.IGNORECASE)


def card_key(url: str) -> str:
    """One agent card, however its link happens to be written."""
    found = _CARD_TOKEN.search(url or "")
    return found.group(1).casefold() if found else " ".join((url or "").split()).casefold()


def split_item(name: str) -> tuple[str, str]:
    """(the agent card the line links to, what the rest of the line says)."""
    found = _LINKED_ITEM.search(name or "")
    if not found:
        return "", " ".join((name or "").split())
    return found.group(1), " ".join(found.group(2).split())


@dataclass
class Spread:
    """One agent's line moving from the setup card onto the Lead Order card."""

    url: str
    label: str
    checklist: str
    make_checklist: bool = False


def setup_agents(checklists: list[dict]) -> list[tuple[str, str]]:
    """(card url, lead type) for every agent on a setup card, once each.

    The person checklists on a setup card are copies of one another - Therese,
    Kathleen and Nicole each get the same list, because each of them does the
    same setting up. So this reads all of them rather than trusting any one
    person's copy to be complete.

    An agent listed twice under different leads keeps both, joined the way one
    line carrying two orders is written. Somebody who bought two things gets
    two lines on the setup card as often as one, and taking the first sighting
    of them threw the other order away.
    """
    order: list[str] = []
    links: dict[str, str] = {}
    labels: dict[str, list[str]] = {}
    for checklist in checklists or []:
        for item in checklist.get("checkItems") or []:
            url, label = split_item(str(item.get("name") or ""))
            if not url:
                continue
            # By card, not by link text. The same agent is written both
            # "trello.com/c/tIA7tp2B" and with the slug on the end.
            key = card_key(url)
            if key not in labels:
                labels[key] = []
                links[key] = url
                order.append(key)
            for part in order_parts(label):
                if part not in labels[key]:
                    labels[key].append(part)
    return [(links[key], ORDER_JOIN.join(labels[key])) for key in order]


def plan_spread(
    setup_held: list[dict], order_held: list[dict]
) -> tuple[list[Spread], list[str]]:
    """Which setup-card agents belong on the Lead Order card, and where.

    The setup card is filed by who does the work; the Lead Order card is filed
    by what was bought. An agent set up days in advance only ever reaches the
    first, because their New Agent card went to Done the day it was read - so
    on the day they go live nothing puts them on the second.

    Returns the lines to add and the agents that couldn't be placed. An agent
    already on the Lead Order card is skipped rather than doubled: the same-day
    path may have put them there hours earlier.
    """
    anywhere: dict[str, int] = {}
    on_checklist: set[tuple[str, str]] = set()
    for checklist in order_held or []:
        where = " ".join(str(checklist.get("name") or "").split()).casefold()
        for item in checklist.get("checkItems") or []:
            url = split_item(str(item.get("name") or ""))[0]
            if url:
                key = card_key(url)
                anywhere[key] = anywhere.get(key, 0) + 1
                on_checklist.add((key, where))

    names = {
        " ".join(str(c.get("name") or "").split()).casefold()
        for c in order_held or []
    }
    have = [str(c.get("name") or "") for c in order_held or []]

    spreads: list[Spread] = []
    problems: list[str] = []
    made: set[str] = set()
    for url, label in setup_agents(setup_held):
        if not label:
            problems.append(f"{url} — the setup card's line doesn't say what the leads are")
            continue

        # Two orders are two lines, so an agent already filed under one of them
        # still needs the other. Otherwise, being on the card as many times as
        # they have orders is enough - somebody may have filed them by hand
        # under a name that matches nothing here, and a second line is worse
        # than none.
        parts = order_parts(label)
        key = card_key(url)
        if anywhere.get(key, 0) >= len(parts):
            continue

        for part in parts:
            landed = OWN_SETUP if is_own_setup(part) else match_checklist(
                part, have, tier=tier_of(part)
            )
            checklist = landed or part
            where = " ".join(checklist.split()).casefold()
            if (key, where) in on_checklist:
                continue
            on_checklist.add((key, where))
            spreads.append(Spread(
                url=url,
                label=part,
                checklist=checklist,
                make_checklist=where not in names and where not in made,
            ))
            made.add(where)
    return spreads, problems


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
