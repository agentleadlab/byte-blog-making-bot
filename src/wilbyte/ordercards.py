"""Orders in Discord, made into Trello cards when Franklin drops the gavel.

"instead of ryte looking out for the New Agent Card and Aged Leads card, it
will now create it instead". A payment and its notes are posted in Discord -
the order channel is made fresh every day under the same name, so the old
setups are flushed out. Nothing happens while the team is still adding notes.
When the order is ready, Franklin reacts with :leadlab_hammertime: and RYTE
makes the card, so the board's automations run from there:

- aged leads: made straight away, in Aged Leads Order Done;
- a fresh agent: made straight away too, in the Queue ("it creates the card
  automatically") - FRESH_ASKS_FIRST in the client puts the Create button
  back.

Each card is a copy of an example card - its checklists, labels and members
- with the title and description written in that card's own format from the
order. Notes added in the thread afterwards update the card. RYTE says
nothing in the Growth server for any of this.
"""

from __future__ import annotations

import re

#: The gavel. A custom emoji from Agent Lead Lab Clients - matched by name, so
#: it works from whichever server it's used in.
GAVEL = "leadlab_hammertime"

#: The example cards each kind is copied from, until told otherwise with
#: "@Ryte new agent template <link>" / "@Ryte aged template <link>".
DEFAULT_TEMPLATES = {"fresh": "https://trello.com/c/Hl06MPly", "aged": ""}

#: Where each kind goes, by list name - letters only, so "📥 In Que" and
#: "Queue" are both the Queue.
FRESH_LISTS = ("queue", "inqueue", "inque", "que")
AGED_LISTS = ("agedleadsorderdone",)

KINDS = {"fresh": "New Agent", "aged": "Aged Leads"}

_CARD_LINK = re.compile(r"trello\.com/c/([A-Za-z0-9]{6,})")


def is_gavel(emoji) -> bool:
    name = str(getattr(emoji, "name", None) or emoji or "")
    return name.strip(":").casefold() == GAVEL


def card_id_of(link: str) -> str:
    """"https://trello.com/c/Hl06MPly/123-new-agent" -> "Hl06MPly"."""
    found = _CARD_LINK.search(str(link or ""))
    return found.group(1) if found else ""


def may_gavel(person_id, config) -> bool:
    """Franklin, or whoever ORDER_GAVEL_USER_IDS names. A stray reaction from
    anybody else makes nothing - an aged card is made straight off it."""
    return str(person_id) in gavellers(config)


def gavellers(config) -> list[str]:
    """The Discord ids whose gavel counts: ORDER_GAVEL_USER_IDS, else Franklin
    (DISCORD_NOTIFY_USER_ID) - and whoever he's let gavel with "@Ryte gavel
    allow @Kharyl". Digits only, so "<@123>" in .env is 123."""
    named = re.findall(r"\d+", str(getattr(config.secrets, "order_gavel_user_ids", "") or ""))
    if not named:
        named = re.findall(r"\d+", str(getattr(config.secrets, "discord_notify_user_id", "") or ""))
    for one in _load(_where("order-gavellers.json", GAVELLERS_PATH)).get("allowed") or []:
        if str(one) not in named:
            named.append(str(one))
    return named


GAVELLERS_PATH = None


def allow_gaveller(person_id, *, allowed: bool = True) -> None:
    """Let somebody's gavel count, or stop it counting."""
    where = _where("order-gavellers.json", GAVELLERS_PATH)
    held = _load(where)
    ids = [str(one) for one in held.get("allowed") or [] if str(one) != str(person_id)]
    if allowed:
        ids.append(str(person_id))
    held["allowed"] = ids
    _save(where, held)


def letters(name: str) -> str:
    return re.sub(r"[^a-z]", "", str(name or "").casefold())


def list_for(kind: str, lists: list[dict], *, wanted: str = "") -> dict | None:
    """The board list a kind of card goes in. `wanted` (ORDER_FRESH_LIST /
    ORDER_AGED_LIST) wins when it's set."""
    names = (letters(wanted),) if wanted else (FRESH_LISTS if kind == "fresh" else AGED_LISTS)
    for one in lists:
        if not one.get("closed") and letters(one.get("name", "")) in names:
            return one
    return None


def changed_lines(before: str, after: str) -> list[str]:
    """The description's lines that are new or different - for a comment when
    somebody has edited the card by hand and RYTE mustn't write over them."""
    had = {_line_key(line) for line in str(before or "").splitlines()}
    return [line for line in str(after or "").splitlines()
            if line.strip() and _line_key(line) not in had]


def _line_key(line: str) -> str:
    """A line as compared: "Killian32cooper@gmail.com" is
    "killian32cooper@gmail.com" - case and spacing aren't news."""
    return " ".join(str(line or "").split()).casefold()


_STATUS = re.compile(r"\b(?:done|signed|live|scheduled|sent|ok(?:ay)?|thanks?|thank you|ty|got it|on it|"
                     r"will do|noted|perfect|great|good|yes|yep|confirmed|ready|set|all set)\b", re.IGNORECASE)


def is_chatter(text: str) -> bool:
    """A status line, not an order detail - "setup done", "signed @Nicole",
    "ok thanks". Kept off a card made already: "Monday-Friday 8:00am -
    10:00pm EST" isn't this, a schedule has numbers in it."""
    said = re.sub(r"@\S+|<[@#][!&]?\d+>|\[screenshot \d+\]", " ", str(text or ""))
    said = re.sub(r"[^\w\s']", " ", said)
    words = said.split()
    if "[screenshot" in str(text or "") or re.search(r"\d", said):
        return False
    return len(words) <= 2 or (len(words) <= 6 and bool(_STATUS.search(said)))


def same_text(one: str, two: str) -> bool:
    return " ".join(str(one or "").split()) == " ".join(str(two or "").split())


# ------------------------------------------------------------- remembered

PATH = None
TEMPLATES_PATH = None


def _where(name: str, override):
    from .state import _state_dir

    return override or (_state_dir() / name)


def _load(where) -> dict:
    import json

    try:
        held = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return held if isinstance(held, dict) else {}


def _save(where, held: dict) -> None:
    import json

    where.parent.mkdir(parents=True, exist_ok=True)
    spare = where.with_suffix(".tmp")
    spare.write_text(json.dumps(held, indent=1, default=str), encoding="utf-8")
    spare.replace(where)


def load() -> dict:
    """{gavelled message id: the order and its card}."""
    return _load(_where("order-cards.json", PATH))


def save(held: dict) -> None:
    _save(_where("order-cards.json", PATH), held)


def templates() -> dict:
    """{"fresh": link, "aged": link} - what's been set, over the defaults."""
    held = _load(_where("order-templates.json", TEMPLATES_PATH))
    return {kind: str(held.get(kind) or DEFAULT_TEMPLATES[kind]) for kind in DEFAULT_TEMPLATES}


def set_template(kind: str, link: str) -> None:
    where = _where("order-templates.json", TEMPLATES_PATH)
    held = _load(where)
    held[kind] = link
    _save(where, held)


# ------------------------------------------------------------- not failing here

#: "make sure that this is not failing, we dont want setting up agent with
#: wrong states, wrong lead type, wrong cap, etc." - what Claude read off an
#: order, held up against what the order says, by plain rules.
US_STATES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas", "CA": "california",
    "CO": "colorado", "CT": "connecticut", "DE": "delaware", "DC": "district of columbia",
    "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho", "IL": "illinois",
    "IN": "indiana", "IA": "iowa", "KS": "kansas", "KY": "kentucky", "LA": "louisiana",
    "ME": "maine", "MD": "maryland", "MA": "massachusetts", "MI": "michigan", "MN": "minnesota",
    "MS": "mississippi", "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada",
    "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico", "NY": "new york",
    "NC": "north carolina", "ND": "north dakota", "OH": "ohio", "OK": "oklahoma", "OR": "oregon",
    "PA": "pennsylvania", "RI": "rhode island", "SC": "south carolina", "SD": "south dakota",
    "TN": "tennessee", "TX": "texas", "UT": "utah", "VT": "vermont", "VA": "virginia",
    "WA": "washington", "WV": "west virginia", "WI": "wisconsin", "WY": "wyoming",
}
_ALL_BUT = re.compile(r"\b(?:all|every)\b.*?\b(?:except|besides|but|excluding|other\s+than|minus|not)\b",
                      re.IGNORECASE)
_CODE = re.compile(r"(?<![A-Za-z])([A-Za-z]{2})(?![A-Za-z])")


def _codes(text: str) -> list[str]:
    """The state codes written in `text`, as written - "TX, FL", "tx/fl"."""
    return [one.upper() for one in _CODE.findall(str(text or "")) if one.upper() in US_STATES]


def _apart(one: str, other: str) -> int:
    """How many letters to change to make one the other."""
    before = list(range(len(other) + 1))
    for i, a in enumerate(one, 1):
        now = [i]
        for j, b in enumerate(other, 1):
            now.append(min(before[j] + 1, now[j - 1] + 1, before[j - 1] + (a != b)))
        before = now
    return before[-1]


def _said_state(code: str, source: str) -> bool:
    upper = str(source or "").upper()
    name = US_STATES[code]
    # A whole word: "Kansas" isn't in "Arkansas".
    if (re.search(rf"(?<![A-Z]){code}(?![A-Z])", upper)
            or re.search(rf"(?<![a-z]){name}(?![a-z])", str(source or "").casefold())):
        return True
    # Spelled wrong - Simon Aleman's "Illinoi". A letter or two off, by how
    # long the name is: "Utah" has to be "Utah".
    slack = 0 if len(name) <= 4 else 1 if len(name) <= 7 else 2
    if not slack:
        return False
    words = re.findall(r"[a-z]+", str(source or "").casefold())
    size = len(name.split())
    return any(_apart(" ".join(words[at:at + size]), name) <= slack for at in range(len(words) - size + 1))


def _state_lines(source: str) -> list[str]:
    """The order's lines that list states: three or more codes, or "states"."""
    found = []
    for line in str(source or "").splitlines():
        codes = [one for one in _CODE.findall(line) if one.upper() in US_STATES and one.isupper()]
        if len(codes) >= 3 or (codes and re.search(r"\bstates?\b", line, re.IGNORECASE)):
            found.append(line)
    return found


_STAMP = re.compile(r"^\[\d\d/\d\d \d\d:\d\d[AP]M\]\s*", re.MULTILINE)
_WHEN = re.compile(
    r"\b(?:today|tonight|tomorrow|tmrw?|asap|live|launch\w*|start\w*|"
    r"(?:mon|tues|wednes|thurs|fri|satur|sun)day|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun|"
    r"january|february|march|april|may|june|july|august|september|october|november|december|"
    r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\b\.?|\b\d{1,2}/\d{1,2}\b|\b\d{4}-\d\d-\d\d\b",
    re.IGNORECASE)


#: Words that make a date a launch date. "reach out tomorrow" is a call to
#: make, not a launch - Emilie Cruz's card said Thursday from it.
_LAUNCH_WORD = re.compile(
    r"\b(?:live|launch\w*|start\w*|begin\w*|go[\s-]*live|golive|turn(?:ed|ing)?\s+on|activat\w*|"
    r"deliver\w*|send(?:ing)?\s+leads|leads?\s+(?:on|from|begin|start))\b", re.IGNORECASE)


#: "Same states last time", "same states as before", "states same as last".
SAME_STATES = re.compile(
    r"\bsame\s+states?\b|\bstates?\s+(?:are\s+)?(?:the\s+)?same\b|"
    r"\bstates?\b[^\n]{0,25}\b(?:last\s+time|as\s+before|previous|last\s+order)\b", re.IGNORECASE)


def says_when(source: str) -> bool:
    """Whether the order says when they launch: a launch word and a day or
    date - leaving out the time each message was posted, which RYTE adds as
    it reads them."""
    said = _STAMP.sub("", str(source or ""))
    return bool(_WHEN.search(said)) and bool(_LAUNCH_WORD.search(said))


def doubts(card: dict, *, today) -> tuple[list[str], list[str]]:
    """(what doesn't add up - the card isn't made without a person saying so,
    what's only missing - said when it's made). Every value has to be in the
    order as written: Claude reading it is not the same as it being there."""
    from datetime import date, timedelta

    from . import setupcheck

    fields = card.get("fields") or {}
    source = str(card.get("source") or "")
    kind = card.get("kind") or "fresh"
    wrong, missing = [], []

    # States.
    states = str(fields.get("states") or "").strip()
    if not states:
        missing.append("no states in the order yet")
    elif SAME_STATES.search(states) and SAME_STATES.search(source):
        # "Same states last time" - taken as written.
        pass
    elif _ALL_BUT.search(states):
        left_out = _codes(_ALL_BUT.split(states, maxsplit=1)[-1])
        for code in left_out:
            if not _said_state(code, source):
                wrong.append(f"States: {code} is left out on the card but isn't in the order")
    else:
        on_card = _codes(states)
        if not on_card and not re.search(r"\ball\b", states, re.IGNORECASE):
            wrong.append(f"States: “{states}” isn't a list of states")
        for code in on_card:
            if not _said_state(code, source):
                wrong.append(f"States: {code} is on the card but isn't in the order")
        listed = set()
        for line in _state_lines(source):
            if _ALL_BUT.search(line) or re.search(r"\b(?:except|besides|excluding|not|no)\b", line, re.IGNORECASE):
                continue
            listed.update(code.upper() for code in _CODE.findall(line)
                          if code.upper() in US_STATES and code.isupper())
        left = sorted(listed - set(on_card))
        if left and on_card:
            wrong.append(f"States: the order also says {', '.join(left)} - not on the card")

    # Lead type, against the payment's product line.
    product = re.search(r"PRODUCT:\**\s*([^\n]*)", source, re.IGNORECASE)
    product = " ".join(product.group(1).split()) if product else ""
    package = str(fields.get("package") or "")
    if product and not re.match(r"^[—\-–(]", product):
        aged_paid = bool(re.search(r"\baged\b", product, re.IGNORECASE))
        if aged_paid and kind != "aged":
            wrong.append(f"Lead type: the payment is for “{product}” - aged - but this reads as a new agent's order")
        family, _subtypes = setupcheck.product_of(product)
        if not aged_paid and kind == "aged" and family:
            wrong.append(f"Lead type: the payment is for “{product}” - not aged - but this reads as an aged order")
        if package and setupcheck.same_product(package, product) is False:
            wrong.append(f"Lead type: the payment post says “{product}” but the order"
                         + (" (its Payra invoice)" if "payra invoice note" in source.casefold() else "")
                         + f" says “{package}” — which one did they buy?")
    if not package:
        missing.append("no lead type")

    # How many.
    leads = str(fields.get("leads") or "")
    count = re.search(r"\d+", leads)
    if count and not re.search(rf"(?<![\d$.]){count.group(0)}(?![\d.])", source):
        wrong.append(f"Number of leads: {count.group(0)} isn't in the order")
    if "no cap" in leads.casefold() and not re.search(r"\bphx\b|phoenix|ad\s*spend", source, re.IGNORECASE):
        wrong.append("Number of leads: “No cap (ad spend)” but the order doesn't say Phoenix / ad spend")
    if not leads:
        missing.append("no number of leads")

    # Contact.
    email = str(fields.get("email") or "").strip().casefold()
    # Said, not held for: a phone read off a screenshot of the client's
    # texts (Mohammad Kanan's) isn't in the words, and a card's contact
    # details don't set up anybody's leads.
    if email and email not in source.casefold():
        missing.append(f"check the email ({email}) - not in the order's text")
    phone = re.sub(r"\D", "", str(fields.get("phone") or ""))[-10:]
    if len(phone) == 10 and phone not in re.sub(r"\D", "", source):
        missing.append(f"check the phone ({fields.get('phone')}) - not in the order's text, maybe off a screenshot")

    # When.
    launch = str(card.get("launch_iso") or "")
    if launch:
        try:
            day = date.fromisoformat(launch)
        except ValueError:
            day = None
        if day is not None and not (today - timedelta(days=1) <= day <= today + timedelta(days=45)):
            wrong.append(f"Launch date: {day:%a %m/%d/%Y} is too far from today to be right")
        elif day is not None and kind == "fresh" and not says_when(source):
            # Blaze Lubbers: nothing said when, and the card said "today" -
            # the day the payment was posted.
            wrong.append(f"Launch date: {day:%a %m/%d} is on the card but the order doesn't say when they launch")
    elif kind == "fresh":
        missing.append("no launch date")
    return wrong, missing
