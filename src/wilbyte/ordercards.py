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


#: A link as Trello writes one once it's had the card - "[a@b.com](mailto:a@b.com)".
_LINKED = re.compile(r"\[([^\]\n]*)\]\((?:mailto:|tel:|https?://)[^)\s]*\)")


def _unlinked(text: str) -> str:
    """The text with Trello's links back to what was written - Cole
    Petersen's card was commented "New in Discord: Email: ..." with the same
    address it already had, linked."""
    return _LINKED.sub(r"\1", str(text or ""))


def _line_key(line: str) -> str:
    """A line as compared: "Killian32cooper@gmail.com" is
    "killian32cooper@gmail.com" - case, spacing and Trello's links aren't news."""
    return " ".join(_unlinked(line).split()).casefold()


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
    # Four words at most, and never one that says how or where - "i sent
    # this one via sms" (Lisasi Dieumerci) is how her leads went, not chatter.
    if _HOW.search(said):
        return False
    return len(words) <= 2 or (len(words) <= 4 and bool(_STATUS.search(said)))


#: Words that make a short line say something: how leads went, or where.
_HOW = re.compile(r"\b(?:via|by|through|sms|text(?:ed)?|e-?mail(?:ed)?|sheet|discord|zelle|ringy|crm|csv|"
                  r"drive|link|google)\b", re.IGNORECASE)


def same_text(one: str, two: str) -> bool:
    return " ".join(_unlinked(one).split()) == " ".join(_unlinked(two).split())


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


_LEAVE_OUT_WORD = re.compile(r"(?i)^(?:no|except|excluding|excl|without|minus|besides)$")
_LEAVE_OUT_JOIN = re.compile(r"(?i)^(?:and|or|&|/|,|nor)$")
#: What a client writes for a state that isn't its code: Mohammad Kanan's
#: "no ny ak no ha" - Hawaii.
_LEAVE_OUT_ALIAS = {"HA": "HI"}


def left_out_groups(text: str) -> list[tuple[list[str], str]]:
    """The states an order leaves out, a group per place it says so, with
    the words as written: "100 Aged Vets, 30-90 no ny ak no ha" ->
    [(["NY", "AK", "HI"], "no ny ak no ha")]. Shahan Uddin's card went out
    with "States: NY, AK" - the two states he was NOT to get."""
    groups = []
    for line in str(text or "").splitlines():
        tokens = re.findall(r"[A-Za-z]+|[,&/]", line)
        at = 0
        while at < len(tokens):
            if not _LEAVE_OUT_WORD.match(tokens[at]):
                at += 1
                continue
            start, codes, last = at, [], at
            at += 1
            while at < len(tokens):
                word = tokens[at]
                code = _LEAVE_OUT_ALIAS.get(word.upper(), word.upper())
                if len(word) == 2 and code in US_STATES and word.casefold() not in ("in", "or", "me", "hi", "oh"):
                    codes.append(code)
                    last = at
                elif len(word) == 2 and word.isupper() and code in US_STATES:
                    codes.append(code)
                    last = at
                elif _LEAVE_OUT_WORD.match(word) or _LEAVE_OUT_JOIN.match(word):
                    pass
                else:
                    break
                at += 1
            if codes:
                groups.append((list(dict.fromkeys(codes)), " ".join(tokens[start:last + 1]).replace(" ,", ",")))
    return groups


def _ordered_but_left_out(on_card: list[str], source: str) -> list[str]:
    """States on a card that the order only ever says to leave out: Shahan
    Uddin's "no ny ak no ha" went out as "States: NY, AK"."""
    groups = left_out_groups(source)
    if not groups:
        return []
    outside = source
    for _codes_out, said_out in groups:
        outside = re.sub(r"\s+".join(map(re.escape, said_out.replace(",", " , ").split())), " ",
                         outside, flags=re.IGNORECASE)
    wrong = []
    for code in on_card:
        where = next((said_out for codes_out, said_out in groups if code in codes_out), "")
        if where and not _said_state(code, outside):
            wrong.append(f"States: {code} is on the card, but the order says to leave it out (“{where}”)")
    return wrong


#: An order in packets or batches: "6 batch of orders", "2 packets of this",
#: "one packet".
PACKETS = re.compile(r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten|another|other)?\s*"
                     r"(?:other\s+|more\s+|separate\s+)?(?:packets?|batch(?:es)?)\b", re.IGNORECASE)
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                 "nine": 9, "ten": 10}


def _count_word(said: str) -> int:
    said = str(said or "").casefold()
    return int(said) if said.isdigit() else _NUMBER_WORDS.get(said, 0)


def left_out_states(text: str) -> list[str]:
    """Every state the order says to leave out - see `left_out_groups`."""
    return list(dict.fromkeys(code for codes, _said in left_out_groups(text) for code in codes))


def states_left_out_fixed(states: str, source: str) -> str:
    """States read as the very ones the order leaves out, written as what
    they are: "NY, AK" off "no ny ak no ha" -> "All states except NY, AK,
    HI". Anything else as it was."""
    states = str(states or "")
    if not states.strip() or _ALL_BUT.search(states):
        return states
    on_card = _codes(states)
    if not on_card or re.search(r"\d", states):
        return states
    groups = [codes for codes, _said in left_out_groups(source) if set(codes) & set(on_card)]
    left_out = list(dict.fromkeys(code for codes in groups for code in codes))
    if not left_out or not set(on_card) <= set(left_out):
        return states
    return "All states except " + ", ".join(left_out)


def _apart(one: str, other: str) -> int:
    """How many letters to change to make one the other."""
    before = list(range(len(other) + 1))
    for i, a in enumerate(one, 1):
        now = [i]
        for j, b in enumerate(other, 1):
            now.append(min(before[j] + 1, now[j - 1] + 1, before[j - 1] + (a != b)))
        before = now
    return before[-1]


#: The short ways a state gets written. Drucilla Bell's "md,dc,va,sc,mo,tx,
#: ok,or,west va,nc,tn" was held for WV.
STATE_SHORT = {
    "WV": ("west va", "w va", "wva", "w virginia", "west virg"),
    "NC": ("n carolina", "no carolina"), "SC": ("s carolina", "so carolina"),
    "ND": ("n dakota", "no dakota"), "SD": ("s dakota", "so dakota"),
    "NH": ("n hampshire",), "NJ": ("n jersey",), "NM": ("n mexico",), "NY": ("n york",),
    "RI": ("rhode isl",), "DC": ("washington dc", "district of columbia"),
    "CA": ("calif", "cali"), "PA": ("penn", "penna"), "MA": ("mass",), "TN": ("tenn",),
    "WI": ("wisc",), "MN": ("minn",), "MI": ("mich",), "OK": ("okla",), "AZ": ("ariz",),
    "CO": ("colo",), "CT": ("conn",), "FL": ("fla",), "NE": ("neb", "nebr"), "NV": ("nev",),
    "OR": ("ore",), "TX": ("tex",), "WY": ("wyo",), "AL": ("ala",), "AR": ("ark",),
    "KS": ("kan", "kans"), "DE": ("del",), "MT": ("mont",),
}


def _said_state(code: str, source: str) -> bool:
    upper = str(source or "").upper()
    name = US_STATES[code]
    said = str(source or "").casefold()
    # Without the dots: "N.C.", "W. Va.".
    plain = re.sub(r"\b([a-z])\.\s?(?=[a-z]\b)", r"\1", said).replace(".", " ")
    # A whole word: "Kansas" isn't in "Arkansas".
    if (re.search(rf"(?<![A-Z]){code}(?![A-Z])", upper)
            or re.search(rf"(?<![A-Z]){code}(?![A-Z])", plain.upper())
            or re.search(rf"(?<![a-z]){name}(?![a-z])", said)):
        return True
    words = " ".join(re.findall(r"[a-z]+", plain))
    if any(re.search(rf"(?<![a-z]){re.escape(short)}(?![a-z])", words) for short in STATE_SHORT.get(code, ())):
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


def _invoice_split(source: str) -> tuple[str, str]:
    """(the order as posted and said in the thread, the Payra invoice's note
    without its Description and Lines)."""
    said = str(source or "")
    found = re.search(r"^Payra invoice note:", said, re.MULTILINE)
    if not found:
        return said, ""
    rest = said[found.start():]
    ends = re.search(r"^\[\d\d/\d\d \d\d:\d\d[AP]M\]", rest, re.MULTILINE)
    block, after = (rest[:ends.start()], rest[ends.start():]) if ends else (rest, "")
    kept = "\n".join(line for line in block.splitlines()
                     if not re.match(r"\s*(?:Description|Lines)\s*:", line, re.IGNORECASE))
    return said[:found.start()] + after, kept


def _states_listed(source: str) -> tuple[list[str], dict]:
    """The state codes the order's state lines list, and the line each is on."""
    listed, where = [], {}
    for line in _state_lines(source):
        if _ALL_BUT.search(line) or re.search(r"\b(?:except|besides|excluding|not|no)\b", line, re.IGNORECASE):
            continue
        # States in brackets are said about, not ordered: Marcus Uhrich's
        # "FL, IA, ... TX (GA & NJ ...)" was held for GA and NJ, which the
        # card was right to leave off.
        listed_part = re.sub(r"\([^)]*(?:\)|$)", " ", line)
        for code in _CODE.findall(listed_part):
            if code.upper() in US_STATES and code.isupper() and code.upper() not in where:
                listed.append(code.upper())
                where[code.upper()] = " ".join(line.split())
    return listed, where


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
    r"deliver\w*|send(?:ing)?\s+leads|leads?\s+(?:on|from|begin|start)|set[\s-]?up\s+(?:for|on))\b", re.IGNORECASE)


#: A time of day beside a day said relatively - "2:30 PM EST today" (Hutchinson
#: Yang) - is when they start, with no "live" or "start" in it. Not a date
#: and a time: every payment post has "10/24/2026 9:12 AM".
_CLOCK = r"\b\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)(?:\s*(?:[ecmp][sd]?t|eastern|central|pacific))?"
_DAY_WORD = r"\b(?:today|tonight|tomorrow|tmrw|(?:mon|tues?|wed(?:nes)?|thu(?:rs)?|fri|sat(?:ur)?|sun)(?:day)?)\b"
CLOCK_DAY = re.compile(rf"^(?!.*\d{{1,2}}/\d{{1,2}}).*(?:{_CLOCK}.*{_DAY_WORD}|{_DAY_WORD}.*{_CLOCK}).*$",
                       re.IGNORECASE | re.MULTILINE)


def clock_day_as_start(text: str) -> str:
    """Each line that's a time and a day - "2:30 PM EST today" - read as
    "start 2:30 PM EST today"."""
    def start(found):
        line = found.group(0)
        # A schedule isn't a start: "Mon-Fri 9am-5pm", "8 am to 8 pm EST".
        if len(re.findall(_CLOCK, line, re.IGNORECASE)) != 1 or re.search(
                rf"{_DAY_WORD}\s*(?:-|–|to|thru|through)\s*{_DAY_WORD}|\b(?:schedule|hours|between|every|"
                rf"weekdays?|weekends?|no\s+\w+days?)\b", line, re.IGNORECASE):
            return line
        return "start " + line.strip()

    return CLOCK_DAY.sub(start, str(text or ""))


#: The order changed to another product after it was paid.
SWITCHED = re.compile(r"\b(?:switch|chang|swap|convert|mov)\w*\s+(?:the\s+|his\s+|her\s+|their\s+)?"
                      r"(?:order|leads?|it)\s+(?:to|into|over\s+to)\b|\binstead\s+of\b", re.IGNORECASE)


#: "Same states last time", "same states as before", "states same as last".
SAME_STATES = re.compile(
    r"\bsame\s+states?\b|\bstates?\s*:?\s*(?:are\s+)?(?:the\s+)?same\b|"
    r"\bstates?\b[^\n]{0,25}\b(?:last\s+time|as\s+before|previous|last\s+order)\b", re.IGNORECASE)


#: The States field saying only "Same as before" / "Same as Fresh" / "same
#: as last time" - Shane Suleiman's was held as "isn't a list of states".
_SAME_ANSWER = re.compile(r"^\s*(?:the\s+)?same(?:\s+(?:as|like)\b[^\n]{0,30})?\s*[.!]*\s*$", re.IGNORECASE)


#: "50 Aged VET 180+", "25 LA" - a count before what it's of. Not "$1,000",
#: "180+", "30-90", a phone number, or "3 available" from a list of what's
#: on hand.
_COUNT_OF = re.compile(r"(?<![\d$.,\-/:])(\d{1,4})(?![\d.,+\-/%])\s+(?:x\s+)?(?!avail|left\b|remaining\b|in\s+stock)(?=[A-Za-z])",
                       re.IGNORECASE)


def _adds_up_to(total: int, source: str) -> bool:
    """Whether the order's counts add up to `total`: two or more on one line
    - Kurtis Kunz's "25 LA and 25 PA" is 50 - or one at the start of each of
    two or more lines - Gabriela Samaan's 50 VET and 50 Mortgage Protection
    are 100."""
    said = re.sub(r"^\[\d\d/\d\d \d\d:\d\d[AP]M\][^:]*:\s*", "", str(source or ""), flags=re.MULTILINE)
    starts = []
    for line in said.splitlines():
        line = line.strip()
        counts = [int(one.group(1)) for one in _COUNT_OF.finditer(line)]
        if len(counts) >= 2 and sum(counts) == total:
            return True
        first = _COUNT_OF.match(line)
        if first:
            starts.append(int(first.group(1)))
    if len(starts) >= 2 and sum(starts) == total:
        return True
    return total in _all_of_it(said)


def _all_of_it(said: str) -> tuple[int, ...]:
    """"180+ days ALL" over a list of what's on hand - "Texas · AEP agency
    VET 180+: 34 available" and the rest - is all of them (Alexis Lesme), and
    with any other count said ("90-180 days-- 37 leads") added on. (all of
    them with the others, all of them alone), or () when it doesn't say."""
    if not re.search(r"\ball\b", said, re.IGNORECASE):
        return ()
    on_hand = [int(one) for one in re.findall(r"(?<![\d.$])(\d{1,4})\s+available\b", said, re.IGNORECASE)]
    if not on_hand:
        return ()
    others = [int(one.group(1)) for line in said.splitlines() for one in _COUNT_OF.finditer(line.strip())]
    return (sum(on_hand) + sum(others), sum(on_hand))


def all_counted(source: str) -> int | None:
    """How many leads an order asking for "ALL" of a stock list comes to,
    with any other count in it added on - Alexis Lesme's 1 + 34 + 4 + 130
    of the 180+ and 37 of the 90-180 are 206. None when it doesn't ask that
    way."""
    said = re.sub(r"^\[\d\d/\d\d \d\d:\d\d[AP]M\][^:]*:\s*", "", str(source or ""), flags=re.MULTILINE)
    found = _all_of_it(said)
    return found[0] if found else None


_PAYER = re.compile(r"NAME:\**[ \t]*([^\n*]+)")

#: The payment post as it is from Oct 19: "INVOICE: INV-20162 · card ••••
#: 7872 · ...", then the invoice's own "Notes:" and "Internal Notes:", quoted.
_POST_INVOICE = re.compile(r"INVOICE:\**\s*(INV-?\d+|#?\d{3,})", re.IGNORECASE)
_NOTES_HEAD = re.compile(r"^\s*\**\s*(internal\s+)?notes\s*:\s*\**\s*(.*)$", re.IGNORECASE)
_NOTHING = re.compile(r"^[\s*_(\[]*(?:none|n/?a|-+|—)?[\s*_)\]]*$", re.IGNORECASE)


def post_invoice(said: str) -> tuple[str, str]:
    """(the invoice number a payment post names, the invoice's notes it
    carries - both its Notes and its Internal Notes), "" for each it doesn't."""
    said = str(said or "")
    number = _POST_INVOICE.search(said)
    blocks, current = {}, None
    for line in said.splitlines():
        head = _NOTES_HEAD.match(line)
        # The post's own heading - bold, or the first one - not a "Notes: Only
        # Motion" line inside the notes: Hunter Kahle's lost the rest of his
        # notes to it, and his card was made out to "Only Motion".
        if head and not line.lstrip().startswith(">") and (current is None or head.group(1) or "**" in line):
            current = "internal" if head.group(1) else "notes"
            blocks[current] = [head.group(2)] if head.group(2).strip() else []
            continue
        if current is None:
            continue
        # The next message: the post ends where somebody else speaks.
        # Bailey Lawrence's Internal Notes "(none)" ran on into the thread -
        # Tre's "live tomorrow" and Faith's tags - and onto the card.
        if re.match(r"\s*\[\d\d/\d\d \d\d:\d\d[AP]M\]", line):
            current = None
            continue
        bare = re.sub(r"^\s*>+\s?", "", line)
        if not line.lstrip().startswith(">") and re.match(r"\s*\*\*[^*\n]{1,40}:", line):
            # The next labelled part of the post, not the notes any more.
            current = None
            continue
        blocks[current].append(bare)
    kept = []
    for kind in ("notes", "internal"):
        text = "\n".join(blocks.get(kind) or []).strip()
        if text and not _NOTHING.match(text):
            kept.append(text if kind == "notes" else f"Internal notes: {text}")
    return (number.group(1).upper().lstrip("#") if number else ""), "\n".join(kept)


_POST_AMOUNT = re.compile(r"^\W*AMOUNT:\**\s*\$?\s*([\d,]+(?:\.\d+)?)", re.IGNORECASE | re.MULTILINE)
_POST_TOTAL = re.compile(r"^\W*Total:\**\s*\$?\s*([\d,]+(?:\.\d+)?)", re.IGNORECASE | re.MULTILINE)


def post_charge(said: str) -> tuple[int | None, int | None]:
    """(what the card was charged, what the post's own invoice comes to), in
    cents - None for one the post doesn't give. More charged than the
    invoice is a charge that paid other invoices too: Dawson Clevenger's
    $279.45 paid INV-20313 ($134.55), INV-20314 and INV-20315, Nova posted
    the first, and the other two lines were never made."""
    def cents(found):
        try:
            return round(float(found.group(1).replace(",", "")) * 100) if found else None
        except ValueError:
            return None

    said = str(said or "")
    return cents(_POST_AMOUNT.search(said)), cents(_POST_TOTAL.search(said))


#: "for his agent Matthew Stewart", "on behalf of", "leads ... go to Matthew".
_PAYING_FOR = re.compile(
    r"\bfor\s+(?:his|her|their|my|our)\s+(?:agent|recruit|downline|team\s*mate|teammate|producer)s?\b"
    r"|\bon\s+behalf\s+of\b|\bpaying\s+for\b|\bpaid\s+for\s+by\b|\bleads?\b[^\n.;]{0,40}\bgo(?:es)?\s+to\b",
    re.IGNORECASE)


def paid_for_by(source: str, named: str) -> bool:
    """Whether the order itself says somebody is paying for the person the
    card is for - Hayden Keltner's "invoice 2 of 2, for his agent Matthew
    Stewart ... leads go to Matthew directly". Their name next to it, on the
    same line."""
    words = [word for word in re.findall(r"[a-z]+", str(named or "").casefold()) if len(word) >= 3]
    if not words:
        return False
    for line in str(source or "").splitlines():
        if _PAYING_FOR.search(line) and any(re.search(rf"\b{re.escape(word)}\b", line, re.IGNORECASE) for word in words):
            return True
    return False


_LINE_KIND = re.compile(r"^\W*(?:payra invoice note:\s*)?lead\s*type\W*:\s*(.+)$", re.IGNORECASE)
_LINE_STATES = re.compile(r"^\W*states?(?:\s+leads?\s+in)?\W*:\s*(.+)$", re.IGNORECASE)


def _lines_with_states(source: str) -> list[tuple[str, list[str]]]:
    """[(the line, its states)] for an order written a line at a time -
    "Lead Type: Aged Vets 30-90 / States: AL CA KS ..." then the next."""
    found, kind = [], ""
    for line in str(source or "").splitlines():
        line = re.sub(r"^\[\d\d/\d\d \d\d:\d\d[AP]M\][^:]*:\s*", "", line).strip(" >*")
        said = _LINE_KIND.match(line)
        if said:
            kind = " ".join(said.group(1).split())
            continue
        places = _LINE_STATES.match(line)
        if places and kind:
            codes = [one.upper() for one in _CODE.findall(places.group(1)) if one.upper() in US_STATES]
            if codes:
                found.append((kind, codes))
            kind = ""
    return found


def paid_by(source: str) -> str:
    """The name on the payment post - "NAME: Tony Moderno", as Payra and
    Zelle posts write it, in capitals - or ""."""
    found = _PAYER.search(str(source or ""))
    return " ".join(found.group(1).split()) if found else ""


#: A payer that's a company, not a person.
_BUSINESS = re.compile(
    r"\b(?:llc|l\.l\.c|inc|incorporated|corp|corporation|co|company|ltd|llp|pllc|pc|group|agency|agencies|"
    r"financial|insurance|enterprises?|holdings?|solutions|services|partners|associates|brokerage|"
    r"consulting|ventures|capital|trust)\b\.?", re.IGNORECASE)


#: "if it says overflow, take it to aged leads ... it'll be treated as new
#: agent but on aged leads" - Christa Arriaga's card went to In Que.
#: Typed in a hurry too: Karli Kelley's "OVERLFLOW BC" went to the Queue.
OVERFLOW = re.compile(r"\bove?r[\s-]*l?f+l*o*w(?:s|ed|ing)?\b", re.IGNORECASE)


def same_person(one: str, other: str) -> bool:
    """Whether two names share a name - "Mike Smith" and "Michael Smith" do,
    "Tony Moderno" and "Preston Bridges" don't."""
    words = lambda said: {word for word in re.findall(r"[a-z]+", str(said or "").casefold()) if len(word) >= 3}
    first, second = words(one), words(other)
    return not first or not second or bool(first & second)


#: Payra's invoice header, on every invoice: "IF YOU USE CC/DEBIT WE CAN
#: START YOUR ORDER TODAY" - not a launch date.
INVOICE_BOILERPLATE = re.compile(r"(?:if\s+you\s+use\s+cc\s*/?\s*debit\s*,?\s*)?we\s+can\s+start\s+your\s+order\s+today",
                                 re.IGNORECASE)


def says_when(source: str) -> bool:
    """Whether the order says when they launch: a launch word and a day or
    date - leaving out the time each message was posted, which RYTE adds as
    it reads them."""
    said = clock_day_as_start(_STAMP.sub("", INVOICE_BOILERPLATE.sub(" ", str(source or ""))))
    # Not the payment post's "INVOICE: INV-20162 · card •••• 7872 ·
    # 10/19/2026 8:50 AM" - a date on every post since Oct 19.
    said = "\n".join(line for line in said.splitlines()
                     if not re.match(r"^\W*(?:invoice|date|due|paid\s+on)\W*:", line, re.IGNORECASE))
    return bool(_WHEN.search(said)) and bool(_LAUNCH_WORD.search(said))


def payer_doubts(source: str, named: str) -> tuple[list[str], list[str]]:
    """Who paid, against who the card is for - (held, said). Tony Moderno's
    payment went on a card for Preston Bridges - the thread's notes were
    about him - and was made without a word: somebody paying for them, or
    the wrong notes."""
    payer = paid_by(source)
    if not (payer and named) or same_person(payer, named):
        return [], []
    if paid_for_by(source, named):
        return [], [f"paid by {payer} for {named}, as the order says"]
    if _BUSINESS.search(payer):
        # Montaser Aidah paid through "Victory Freedom Llc" - his own company,
        # its email the one on his card. Said, not held.
        return [], [f"paid by {payer} - their business? - for {named}"]
    return [f"Name: paid by {payer}, but the order is for {named} - somebody paying for them, "
            f"or notes from another order?"], []


def count_said(count, source: str, *, total: bool = False) -> bool:
    """Whether a lead count is in the order's words - not in the times RYTE
    stamps on them ("[10/15 01:38PM]" had 10 in the order whatever it said),
    not inside an amount, a date or a phone. "1,000" and "1000" are the same.
    `total`: or the order's counts add up to it."""
    digits = str(count).replace(",", "").strip()
    if not digits.isdigit():
        return False
    words = re.sub(r"^\[\d\d/\d\d \d\d:\d\d[AP]M\][^:]*:", "", str(source or ""), flags=re.MULTILINE)
    shown = {digits, f"{int(digits):,}"}
    for one in shown:
        said = re.escape(one)
        if re.search(rf"(?<![\d$./:]){said}(?![\d./:])", words) \
                or re.search(rf"[A-Za-z)]\s*:\s*{said}(?![\d./:])", words):
            return True
    return total and _adds_up_to(int(digits), source)


def _cap_of(number: str) -> re.Pattern:
    return re.compile(
        rf"\bcap(?:ped)?\s*(?:of|at|to|is|=|:|-)?\s*{number}(?![\d.,])(?:\s*(?:leads?\s*)?(?:per|a|/)\s*day\b)?"
        rf"|(?<![\d.,$]){number}\s*(?:leads?\s*)?(?:per|a|/|each)\s*day\b"
        rf"|(?<![\d.,$]){number}\s*(?:leads?\s*)?daily\b|\bdaily\s*(?:cap|limit|max)\s*(?:of|:)?\s*{number}(?![\d.,])",
        re.IGNORECASE)


def only_a_cap(count, source: str) -> str:
    """The words, when the only place a count is in the order is as a daily
    cap - Paul Miller's "CAP 5" and "a cap of 5 leads per day", made into
    "Number of Leads Requested: 5" for the 24 he bought. "" otherwise."""
    digits = str(count).replace(",", "").strip()
    if not digits.isdigit():
        return ""
    pattern = _cap_of(re.escape(digits))
    said = pattern.search(str(source or ""))
    if not said:
        return ""
    rest = pattern.sub(" ", str(source or ""))
    return "" if count_said(digits, rest) else " ".join(said.group(0).split())


def agency_doubts(package: str, leads: str, source: str) -> list[str]:
    """An agency on the card the order doesn't name - Matthew Odierno's said
    Phoenix; his order said Liftoff Ascend - and "No cap" without one."""
    wrong = []
    for agency, said in (("PHNX", r"\bphx\b|\bphnx\b|phoenix"), ("Ascend", r"ascend|lift\s*off"),
                         ("Spark", r"\bspark\b")):
        if re.search(said, str(package or ""), re.IGNORECASE) and not re.search(said, source, re.IGNORECASE):
            wrong.append(f"Lead type: the card says {agency} but the order doesn't name {agency}")
    if "no cap" in str(leads or "").casefold() and not re.search(r"\bphx\b|\bphnx\b|phoenix|ad\s*spend", source,
                                                                 re.IGNORECASE):
        wrong.append("Number of leads: “No cap (ad spend)” but the order doesn't say Phoenix / ad spend")
    return wrong


def launch_doubts(launch_iso: str, source: str, *, kind: str, today) -> list[str]:
    """A launch date too far from today to be right, or on a new agent's card
    when the order never says when they launch."""
    from datetime import date, timedelta

    if not launch_iso:
        return []
    try:
        day = date.fromisoformat(str(launch_iso))
    except ValueError:
        return [f"Launch date: “{launch_iso}” isn't a date"]
    if not (today - timedelta(days=1) <= day <= today + timedelta(days=45)):
        return [f"Launch date: {day:%a %m/%d/%Y} is too far from today to be right"]
    if kind == "fresh" and not says_when(source):
        # Blaze Lubbers: nothing said when, and the card said "today" - the
        # day the payment was posted.
        return [f"Launch date: {day:%a %m/%d} is on the card but the order doesn't say when they launch"]
    return []


def doubts(card: dict, *, today) -> tuple[list[str], list[str]]:
    """(what doesn't add up - the card isn't made without a person saying so,
    what's only missing - said when it's made). Every value has to be in the
    order as written: Claude reading it is not the same as it being there."""
    from . import setupcheck

    fields = card.get("fields") or {}
    source = str(card.get("source") or "")
    kind = card.get("kind") or "fresh"
    wrong, missing = [], []

    held, said = payer_doubts(source, " ".join(str(fields.get("name") or "").split()))
    wrong += held
    missing += said

    # States.
    states = str(fields.get("states") or "").strip()
    if not states:
        missing.append("no states in the order yet")
    elif (SAME_STATES.search(states) or _SAME_ANSWER.match(states)) and SAME_STATES.search(source):
        # "Same states last time" - taken as written.
        pass
    elif _ALL_BUT.search(states):
        left_out = _codes(_ALL_BUT.split(states, maxsplit=1)[-1])
        # "ha" for Hawaii is said, as Hawaii.
        said_out = set(left_out_states(source))
        for code in left_out:
            if code not in said_out and not _said_state(code, source):
                wrong.append(f"States: {code} is left out on the card but isn't in the order")
    else:
        on_card = _codes(states)
        if not on_card and not re.search(r"\ball\b", states, re.IGNORECASE):
            wrong.append(f"States: “{states}” isn't a list of states")
        for code in on_card:
            if not _said_state(code, source):
                wrong.append(f"States: {code} is on the card but isn't in the order")
        wrong += _ordered_but_left_out(on_card, source)
        # The team's own lines first. The Payra invoice's note only when the
        # thread lists none, and never its Description or Lines - the
        # product's own wording: Collin Williams' card was held for "IN, MD,
        # NC" his thread never said.
        team, invoice = _invoice_split(source)
        listed, where = _states_listed(team)
        if not listed:
            listed, where = _states_listed(invoice)
        left = sorted(set(listed) - set(on_card))
        if left and on_card:
            shown = next((line for code in left for line in [where.get(code, "")] if line), "")
            wrong.append(f"States: the order also says {', '.join(left)} - not on the card"
                         + (f" (“{shown[:160]}”)" if shown else ""))

    # Each line's own states the same way: the order's lines go on cards of
    # their own.
    for part in card.get("parts") or []:
        said = str((part or {}).get("states") or "")
        if said and not _ALL_BUT.search(said):
            wrong += [f"{part.get('package') or 'A line'}: {one}"
                      for one in _ordered_but_left_out(_codes(said), source)]
    # Packets with states of their own, read as one list: Mohammad Kanan's
    # 600 Aged Vets in six packets - one of fifteen states, two of six, two
    # of "all states except ny ak ha" - went out as one States list of every
    # state named, the "all except" packets lost.
    packets = PACKETS.findall(source)
    if kind == "aged" and len(card.get("parts") or []) < 2 and (
            len(packets) >= 2 or any(_count_word(one) >= 2 for one in packets)):
        wrong.append("States: the order is in packets or batches, each with its own states - one States list "
                     "sends every packet the same ones. Each packet needs its own line and count")

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
        # A split order - "50 Aged VET 180+ + 50 Aged Mortgage Protection" -
        # is right when one of its parts is what the payment line names.
        parts = [one for one in re.split(r"\s+\+\s+|\s*&\s*|\s+and\s+|,\s*(?=\d)", package) if one.strip()]
        # Against what the payment says, not the other way: "Aged Leads
        # (IUL)" paid and "50 Aged IUL Spanish (30-90)" written is the order
        # being more exact, not another product (Leonardo Lopez).
        # Unless the order says it was switched - Ben Jacob paid for Trucker
        # IUL and Faith wrote "Instead of Trucker IUL's / Switch order to 45
        # Text Verified Veteran leads".
        if package and all(setupcheck.same_product(product, one) is False for one in parts or [package]) \
                and not SWITCHED.search(source):
            wrong.append(f"Lead type: the payment post says “{product}” but the order"
                         + (" (its Payra invoice)" if "payra invoice note" in source.casefold() else "")
                         + f" says “{package}” — which one did they buy?")
    if not package:
        missing.append("no lead type")

    # How many.
    leads = str(fields.get("leads") or "")
    count = re.search(r"\d+", leads)
    # The payment's own count, against the card's: Jordan Holmes's Zelle post
    # said LEADS: 50 and the card 21.
    posted = re.search(r"^\W*LEADS\W*:\**\s*(\d[\d,]*)\b", source, re.MULTILINE)
    if count and posted and int(posted.group(1).replace(",", "")) != int(count.group(0)) \
            and not _adds_up_to(int(count.group(0)), source):
        wrong.append(f"Number of leads: the payment says {posted.group(1)}, the card {count.group(0)} — which is it?")
    if count and not count_said(count.group(0), source, total=True):
        wrong.append(f"Number of leads: {count.group(0)} isn't in the order")
    elif count and only_a_cap(count.group(0), source):
        wrong.append(f"Number of leads: {count.group(0)} is the daily cap the order gives (“{only_a_cap(count.group(0), source)}”)"
                     f", not how many they bought")
    # Every count in the lead type, too: Mateo Lyon's card said "50 Aged Vets
    # 30-90" for the 163 he bought, and only the total was checked.
    for each in _COUNT_OF.finditer(package):
        said = each.group(1)
        if not count_said(said, source):
            wrong.append(f"Lead type: {said} (in “{package}”) isn't in the order")
    # Lines with their own states, on the card as one list: Mateo Lyon's Vets
    # 90-180 were Texas only, and went out with every state of the other two.
    lines = _lines_with_states(source)
    if len(lines) >= 2 and len({frozenset(codes) for _kind, codes in lines}) >= 2 \
            and ";" not in str(fields.get("states") or ""):
        wrong.append("States: each line of the order has its own states, and the card has them as one list - "
                     + "; ".join(f"{kind}: {', '.join(codes)}" for kind, codes in lines))
    wrong += agency_doubts(package, leads, source)
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

    # Nobody's details at all: what was gavelled wasn't the payment - a
    # note saying "21 Trucker IUL leads / Add to current order" made a card
    # with no email, phone or states.
    if kind == "fresh" and source.strip() and not str(fields.get("email") or "").strip() \
            and not re.sub(r"\D", "", str(fields.get("phone") or "")) and not states \
            and not re.search(r"new\s+payment|^\W*(?:name|amount)\W*:", source, re.IGNORECASE | re.MULTILINE):
        wrong.append("No email, phone or states — was the payment gavelled, or only a note about it?")

    # When - and when the notes give more than one day, which.
    said_days = [str(one) for one in card.get("launch_conflict") or []]
    if len(said_days) > 1 and kind == "fresh":
        from datetime import date as _day

        shown = []
        for one in said_days:
            try:
                shown.append(f"{_day.fromisoformat(one):%a %m/%d}")
            except ValueError:
                shown.append(one)
        wrong.append(f"Launch date: the notes say {' and then '.join(shown)} - I took the latest, "
                     f"{shown[-1]}. Which is it?")
    launch = str(card.get("launch_iso") or "")
    if launch:
        wrong += [one for one in launch_doubts(launch, source, kind=kind, today=today) if "isn't a date" not in one]
    elif kind == "fresh":
        missing.append("no launch date")
    return wrong, missing
