"""Every new payment's setup by Rook and Nova, checked.

"since new agents and aged leads are being automated, ryte's job gotta
change, instead of qc-ing on trello which is now out of equation, i want you
to qc all channel especially Rook's and the sheet, and the actual hub ...
whenever we have a new payment ryte will wait for rook and nova ... and ryte
will qc everything ... for now i want ryte to flag them to me, give me a
button before ryte actually engage with the two of them ... unless Rook/nova
said they cant set it up, thats where ryte will create the card ... I want
Ryte to be detailed oriented, dont miss anything".

The payment post in #new-payment is what was bought. Rook's setup in
#rook-nova is what Rook understood; Nova's replies are what Nova did; the hub
record is what is actually set up; the sheet is where the leads land. Each is
held up against the payment, field by field. Nothing here writes anywhere.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .ordercards import US_STATES, _codes, same_person
from .state import _state_dir

#: Each payment's QC as last said, by the payment post's id.
PATH = _state_dir() / "rook-qc.json"

#: A labelled line. Never the next line's: Max Wilson's blank "Email:" was
#: read as "Phone: +1561...", his blank "States:" as the line under it.
_FIELD = r"^[ \t>*•-]*{label}[ \t]*:\**[ \t]*(?P<value>[^\n]*)$"
_FAMILIES = (("vet", r"\bvet|veteran"), ("fex", r"\bfex\b|final\s*expense"), ("iul", r"\biuls?\b"),
             ("mtg", r"\bmtg\b|mortgage"), ("widow", r"widow"), ("trucker", r"truck"),
             ("blue collar", r"blue\s*collar"), ("spanish", r"spanish|espa[nñ]ol"))
_AGED = re.compile(r"\baged\b|\b\d{2,3}\s*(?:\+|[-–]\s*\d{2,3})\s*(?:days?)?\b", re.IGNORECASE)
_HANDED = re.compile(r"^\s*(?:>\s*)?handed\s+to\s+the\s+team\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
#: Rook or Nova saying the setup is theirs no more: "I can't set this up",
#: "unable to set her up", "needs a manual setup".
CANT = re.compile(
    r"\b(?:can(?:'|’)?t|cannot|can\s+not|could(?:n(?:'|’)?t| not)|unable\s+to|won(?:'|’)?t\s+be\s+able\s+to)\s+"
    r"(?:fully\s+)?(?:set\s+(?:this|it|him|her|them|the\s+\w+|(?-i:[A-Z][\w'’-]+(?:\s+[A-Z][\w'’-]+)?))?\s*up|do\s+(?:this|the)\s+setup|create\s+(?:this|the)\s+"
    r"(?:setup|agent))|\bneeds?\s+(?:a\s+)?(?:manual|trello|human)\s+(?:setup|card)\b|\bset\s*up\s+(?:failed|by\s+hand)\b",
    re.IGNORECASE)
_LIVE_WORDS = re.compile(r"\b(?:paused|on\s+hold|hold|not\s+live|isn'?t\s+live|live)\b", re.IGNORECASE)


@dataclass
class Order:
    """What a payment, a setup message or the hub says about one order."""
    name: str = ""
    email: str = ""
    phone: str = ""
    states: list = field(default_factory=list)
    left_out: list = field(default_factory=list)
    lead_type: str = ""
    count: int | None = None
    launch: str = ""
    amount: str = ""
    #: The aged lines on the same invoice - Nova's pull, not the setup's.
    aged: str = ""


@dataclass
class Checked:
    client: str = ""
    ok: list = field(default_factory=list)
    problems: list = field(default_factory=list)
    #: What Nova handed to the team - work, not a mismatch.
    todo: list = field(default_factory=list)
    #: Said, not a problem: something that couldn't be looked at.
    notes: list = field(default_factory=list)
    #: Rook or Nova said they can't set it up - the card's ours then.
    cant: str = ""
    picked_up: bool = False

    #: Nova pulled it as an aged order in the payment's thread.
    aged_pull: bool = False
    #: An overflow order - the team delivers it; nothing to ask Rook and Nova.
    overflow: bool = False
    #: Somebody on the team said it needs no setup: who, and what they said.
    no_setup: str = ""
    #: Problems flagged by an earlier QC and still there: {problem: when}.
    still: dict = field(default_factory=dict)
    #: Problems an earlier QC flagged that are gone now.
    fixed: list = field(default_factory=list)
    #: Rook and Nova asked about it with the button: (link, when).
    asked: tuple = ()
    #: Rook's setup message the QC was held to, as it was written.
    setup_text: str = ""
    #: Everything said about it, run together - and as (who, what) in order.
    talk_text: str = ""
    talk: list = field(default_factory=list)
    #: Rook or Nova saying they've taken it to Therese / Nicole themselves.
    with_team: str = ""
    #: The other payment post this one looks to be a second of.
    twin: str = ""
    #: Its Trello card, once made.
    trello: str = ""
    #: Rook or Nova's answer to Ryte that it's right as it is.
    rook_ok: str = ""
    #: The team's answer in #rook-qc that it's done: who, and their words.
    team_ok: str = ""
    #: Rook or Nova waiting on the client before it's set up: their words.
    waiting_client: str = ""
    #: The payment post as read, with the team's notes in its thread.
    post_text: str = ""
    #: The hub row this order is, when there's one - and what it was held to.
    hub: object = None
    wanted: object = None
    #: The payment as it was read.
    paid: object = None

    def talk_about_setup(self) -> bool:
        """Whether Rook posted a setup for it - not only an aged pull talked
        about (Kane Curtis'). Not Rook's "New aged setup" either - Christopher
        Gallion's Aged MTG 180+, pulled by Nova, came out as a setup Ryte
        couldn't find on the hub."""
        if not self.setup_text:
            return False
        kind = _field(self.setup_text, r"Lead\s*Type(?:\s*Ordered)?", "Package")
        first = self.setup_text.strip().split("\n", 1)[0]
        return not (re.search(r"\baged\b", kind, re.IGNORECASE)
                    or re.search(r"\baged\s+(?:order|setup|set\s*up|pull)\b", first, re.IGNORECASE))

    def digest(self) -> str:
        return json.dumps([sorted(self.problems), sorted(self.todo), self.cant, self.picked_up])


def _field(text: str, *labels: str) -> str:
    for label in labels:
        found = re.search(_FIELD.format(label=label), str(text or ""), re.IGNORECASE | re.MULTILINE)
        if found and found.group("value").strip(" *—-"):
            return " ".join(found.group("value").split()).strip(" *")
    return ""


def _digits(said: str) -> str:
    return re.sub(r"\D", "", str(said or ""))[-10:]


def _states_of(said: str) -> tuple[list, list]:
    """(states ordered, states left out) - "All states except PR, NY" is the
    second; "AZ, LA" or "Arizona, Louisiana" the first."""
    said = str(said or "")
    if re.search(r"\b(?:all|every|nation\s*-?\s*wide)\b", said, re.IGNORECASE):
        after = re.split(r"\b(?:except|but|excluding|minus|besides|other\s+than|no)\b", said, maxsplit=1,
                         flags=re.IGNORECASE)
        return [], (_named(after[1]) if len(after) > 1 else [])
    return _named(said), []


def _named(said: str) -> list:
    found = list(_codes(said))
    lowered = str(said or "").casefold()
    for code, name in sorted(US_STATES.items(), key=lambda one: -len(one[1])):
        if re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", lowered) and code not in found:
            found.append(code)
    return list(dict.fromkeys(found))


def family(lead_type: str) -> frozenset:
    said = str(lead_type or "").casefold()
    names = {name for name, pattern in _FAMILIES if re.search(pattern, said)}
    # "Trucker Real Times" is Trucker IUL by another name - "yes same
    # product" (Riley Noah's payment against Rook's setup and the hub).
    if "trucker" in names and (re.search(r"\breal[\s-]*times?\b", said) or names == {"trucker"}):
        # And a trucker order naming no product - "OTP Truckers" (Abdullah
        # Bahrani) - is the trucker product, which is IUL.
        names.add("iul")
    if names == {"blue collar"}:
        # The same for blue collar - Jake Shortino's "Text-Verified Blue
        # Collar Leads" against the hub's Blue Collar IUL.
        names.add("iul")
    if "widow" in names:
        # "Fresh Vet Widows" is the widow product (Alexander Salinas').
        names.discard("vet")
    if names & {"vet", "widow"}:
        # "Veteran Final Expense" is the veteran product - final expense for
        # veterans: Peter Kuyper's payment against the hub's Text Verified
        # Veteran Plus was asked about as two lead types, at 12:45 AM.
        names.discard("fex")
    if _AGED.search(said):
        names.add("aged")
    return frozenset(names)


#: First names that are the same person's - "William Stanger" paid, the hub
#: has "Will Stanger".
_NICKNAMES = [set(one.split()) for one in (
    "william will bill billy willy liam", "robert rob bob bobby robby bert", "michael mike mikey mick",
    "james jim jimmy jamie", "thomas tom tommy", "anthony tony", "christopher chris kit", "matthew matt matty",
    "daniel dan danny", "david dave davey", "joseph joe joey", "joshua josh", "nicholas nick nicky",
    "steven stephen steve", "alexander alex al xander", "alexandra alex lexi sandra", "benjamin ben benny",
    "samuel sam sammy", "samantha sam sammy", "katherine catherine kate katie kathy cathy kat",
    "elizabeth liz lizzy beth betsy eliza", "jennifer jen jenny", "edward ed eddie ted", "gregory greg",
    "jeffrey jeff", "jonathan john jon johnny jack", "ronald ron ronnie", "donald don donnie", "richard rick rich ricky dick",
    "patrick pat", "patricia pat patty trish", "timothy tim timmy", "zachary zach zack", "andrew andy drew",
    "charles charlie chuck chas chaz", "francis frank frankie", "franklin frank", "gabriel gabe", "jacob jake",
    "nathan nate", "nathaniel nate nat", "abigail abby", "amanda mandy", "deborah debbie deb",
    "susan sue susie", "margaret maggie meg peggy", "rebecca becky becca", "victoria vicky tori",
    "kenneth ken kenny", "lawrence larry", "leonard leo len lenny", "raymond ray", "jose pepe",
    "isaiah ike", "frederick fred freddie", "vincent vince vinny", "eugene gene", "theodore ted teddy theo",
    "cameron cam", "dominic dom", "jessica jess jessie", "melissa missy mel", "christine chris christy tina",
    "angela angie", "kimberly kim", "cynthia cindy", "douglas doug", "gerald gerry jerry", "harold harry hal",
    "henry hank harry", "jerome jerry", "joanne jo", "jordan jordy", "leslie les", "louis lou", "manuel manny",
    "mitchell mitch", "phillip philip phil", "russell russ", "stanley stan", "terrence terry", "walter walt",
)]


def name_aliases(name: str) -> list:
    """A name and the others it's written with: "JT (James) Wideman" is also
    "JT Wideman" and "James Wideman"; "Cavell (CJ) Stubbs Jr" is also "CJ
    Stubbs"; "Shane Suleiman (also goes by Hussein Suleiman)" is also
    "Hussein Suleiman". Looked for as "JT (James) Wideman", nobody was found."""
    said = " ".join(str(name or "").split())
    out = [said]
    base = " ".join(re.sub(r"\([^)]*\)", " ", said).split())
    if base and base != said:
        out.append(base)
    last = (_name_words(base) or [""])[-1]
    for inside in re.findall(r"\(([^)]*)\)", said):
        other = re.sub(r"^\s*(?:also\s+goes\s+by|aka|a\.k\.a\.?|goes\s+by|or)\s+", "", inside, flags=re.IGNORECASE)
        words = [one for one in re.findall(r"[A-Za-z][A-Za-z'’.-]*", other)]
        if len(words) >= 2:
            out.append(" ".join(words))
        elif len(words) == 1 and last:
            out.append(f"{words[0]} {last.capitalize()}")
    return list(dict.fromkeys(one for one in out if one))


def same_person_strict(one: str, other: str) -> bool:
    """The same first (or its nickname) and last name - not just a shared
    word: Duehat Kanan isn't Mohammad Kanan, Brandon Nguyen isn't Mai. Any
    name either goes by counts - "JT (James) Wideman" is James Wideman."""
    if "(" in str(one or "") or "(" in str(other or ""):
        return any(_same_person_strict(a, b) for a in name_aliases(one) for b in name_aliases(other))
    return _same_person_strict(one, other)


def _same_person_strict(one: str, other: str) -> bool:
    first, second = _name_words(one), _name_words(other)
    if not first or not second:
        return False
    if len(first) == 1 or len(second) == 1:
        return first[0] == second[0] or first[-1] == second[-1]
    return first[-1] == second[-1] and _same_first(first[0], second[0])


def _same_first(one: str, other: str) -> bool:
    if one == other:
        return True
    return any(one in names and other in names for names in _NICKNAMES)


def hub_named(found: list, who: str) -> list:
    """The hub's agents by this name: every word of it, as the hub writes
    it - else the same last name and a first name that's the same person's
    (Will for William)."""
    from .hub import named

    # Both: the name as written and as the same person's - Tom Skinner's
    # ended row was found as "Tom Skinner" and his live one, written
    # "Thomas Skinner", never looked for. And every name they go by.
    aliases = name_aliases(who)
    if len(aliases) > 1:
        out = []
        for alias in aliases:
            if "(" in alias:
                continue
            out += [one for one in hub_named(found, alias) if one not in out]
        return out
    out = list(named(found, who))
    words = _name_words(who)
    if len(words) < 2:
        return out
    first, last = words[0], words[-1]
    for one in found:
        if one in out:
            continue
        theirs = _name_words(getattr(one, "name", ""))
        if len(theirs) >= 2 and theirs[-1] == last and _same_first(theirs[0], first):
            out.append(one)
    return out


def _name_words(name) -> list:
    """A name's words for matching: "D’shane" as "dshane", no middle
    initials, no Jr / Sr / II - Cavell M Stubbs Jr, William J Seitz."""
    said = re.sub(r"['’`]", "", str(name or "").casefold()).replace("_", " ")
    words = [one for one in re.findall(r"[a-z]+", said) if len(one) > 1]
    return [one for one in words if one not in ("jr", "sr", "ii", "iii", "iv")]


def payment(post: str) -> Order:
    """What the payment post in #new-payment says was bought - the invoice's
    notes over the post's own lines, as the client wrote them."""
    text = str(post or "")
    notes = text.split("Notes:", 1)[1] if "Notes:" in text else ""
    order = Order(
        name=_field(notes, "Name", r"Client\s*Name", r"Full\s*Name") or _field(text, "NAME"),
        email=_field(notes, r"E-?mail") or _field(text, "EMAIL"),
        phone=_field(notes, r"Phone(?:\s*Number)?") or _field(text, "PHONE"),
        amount=_field(text, "AMOUNT"),
    )
    lines = re.findall(r"^[\s>*•-]*([A-Za-z][A-Za-z0-9 +&/().'-]{1,60}?)\s*:\s*(\d[\d,]*)\s*(?:x|@)\s*\$?[\d,.]+"
                       r"[^\n—–]*?(?:[—–-]\s*states?\s*:?\s*([^\n]+))?$", text, re.IGNORECASE | re.MULTILINE)
    # Fresh and aged on one invoice - William McClure's 50 Fresh Vets and 100
    # Aged IUL 180+, read as 150 "Fresh Vets + Aged IUL 180+" and held up
    # against his Vets setup: the setup is the fresh lines; the aged ones are
    # Nova's pull, the aged QC's.
    fresh = [one for one in lines if "aged" not in family(one[0])]
    if fresh and len(fresh) < len(lines):
        order.aged = " + ".join(f"{one[1]} {' '.join(one[0].split())}" for one in lines if one not in fresh)
        lines = fresh
    if lines:
        order.lead_type = " + ".join(" ".join(one[0].split()) for one in lines)
        order.count = sum(int(one[1].replace(",", "")) for one in lines)
        states, left_out = [], []
        for one in lines:
            got, out = _states_of(one[2] or "")
            states += got
            left_out += out
        order.states, order.left_out = list(dict.fromkeys(states)), list(dict.fromkeys(left_out))
    else:
        order.lead_type = _field(notes, r"Lead\s*Type(?:\s*Ordered)?", "Package") or _field(text, "PRODUCT")
        # Nova's Zelle post with nothing to go by: "PRODUCT: — (match to
        # their order)" - not a product, and said as one in Ryte's question.
        if re.fullmatch(r"[\W_]*(?:\(?\s*match\s+(?:it\s+)?to\s+(?:their|the|his|her)\s+order\s*\)?)?[\W_]*",
                        order.lead_type or "", re.IGNORECASE):
            order.lead_type = ""
        many = re.search(r"\d[\d,]*", _field(notes, r"Number\s+of\s+(?:Requested\s+)?Leads(?:\s+Requested)?",
                                             r"Leads?", r"Qty", r"Quantity"))
        order.count = int(many.group(0).replace(",", "")) if many else None
        order.states, order.left_out = _states_of(_field(notes, r"States?(?:\s+Leads?\s+In)?", "Estados")
                                                  or _field(text, "STATES?"))
    order.launch = _field(notes, r"Launch(?:\s*Date)?", r"Live(?:\s*Date)?", r"Start(?:\s*Date)?")
    return order


def rook_setup(text: str) -> Order | None:
    """Rook's "New fresh setup:" message, as fields. None when it isn't one."""
    said = str(text or "")
    # "New fresh setup:", and "order details from Rook (the client's texts):"
    # in the invoice form's own words - Kyle Moffitt's 31 FEX in nine states
    # against 28 Vets in HI and AK paid for went unread.
    if not re.search(r"^\W*name\s*:", said, re.IGNORECASE | re.MULTILINE):
        return None
    if not (re.search(r"\bset\s*up\b|\border\s+details\b|\bnew\s+(?:fresh\s+)?order\b", said, re.IGNORECASE)
            or re.search(r"^\W*(?:lead\s*type|states?)\b[^:\n]*:", said, re.IGNORECASE | re.MULTILINE)):
        return None
    many = re.search(r"\d[\d,]*", _field(said, r"Number\s+of\s+(?:Requested\s+)?Leads(?:\s+(?:Ordered|Requested))?",
                                         r"Leads?\s+(?:Ordered|Count|Requested)", r"Lead\s+Count", r"Count", r"Qty",
                                         r"Quantity", r"Leads", r"Order(?:ed)?", r"Amount"))
    if many is None:
        # In the lead type or anywhere as "25 Vets", "25 Text-Verified ...":
        # Timothy Aaron Thomas' read as "no lead count".
        many = re.search(r"(?<![\d$.,/:-])(\d{1,4})\s+(?:aged\s+|fresh\s+|otp\s+|text[\s-]*verified\s+)*"
                         r"(?:leads?|vets?|veterans?|fex|final\s+expense|iuls?|mtg|mortgage|widows?|truckers?|blue)\b",
                         said,
                         re.IGNORECASE)
    states, left_out = _states_of(_field(said, r"States?(?:\s+Leads?\s+In)?", "Estados"))
    return Order(name=_field(said, "Name"), email=_field(said, r"E-?mail"), phone=_field(said, r"Phone(?:\s*Number)?"),
                 states=states, left_out=left_out,
                 lead_type=_field(said, r"Lead\s*Type(?:\s*Ordered)?", "Package"),
                 count=int(many.group(many.lastindex or 0).replace(",", "")) if many else None,
                 launch=_field(said, r"Launch(?:\s*Date)?"))


def about(text: str, order: Order) -> bool:
    """Whether a #rook-nova message is about this order - its email, its
    phone, or the client's whole name."""
    said = str(text or "")
    if order.email and order.email.casefold() in said.casefold():
        return True
    if order.phone and len(_digits(order.phone)) == 10 and _digits(order.phone) in re.sub(r"\D", "", said):
        return True
    words = [word for word in re.findall(r"[a-z]+", order.name.casefold()) if len(word) >= 2]
    if _BUSINESS.search(order.name or ""):
        # "The Eissa Group Llc" - "the" and "llc" are in any message about
        # any company: Nova's on Kunz Marketing, Llc was read as theirs.
        words = [word for word in words if word not in _NOT_NAMES and not _BUSINESS.fullmatch(word)]
        return bool(words) and all(re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", said.casefold())
                                   for word in words)
    return len(words) >= 2 and all(re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", said.casefold())
                                   for word in (words[0], words[-1]))


#: Someone calling a setup off: Rook's "ignore Luna's setup above for
#: Marcus Uhrich ... Do NOT add or renew again".
CALLED_OFF = re.compile(r"\b(?:ignore|disregard|scratch|cancel)\b(?P<what>[^\n]{0,60}?)\bset[\s-]?up\b",
                        re.IGNORECASE)


def the_setup(talk, paid: Order) -> tuple:
    """(the setup this payment's QC is held to, notes on any called off).
    Not one somebody said to ignore - Marcus Uhrich's 50 from Luna, called off
    by Rook for the 55 already renewed - and of several, the one with the
    payment's count, else the newest."""
    setups, notes = [], []
    for who, said in talk:
        found = rook_setup(said)
        if found is not None:
            setups.append([who, found, False, said])
            continue
        off = CALLED_OFF.search(said)
        if off is None or not setups:
            continue
        whose = re.search(r"\b(\w+)['’]s\b", off.group("what"))
        hit = [one for one in setups if not one[2] and (whose is None or one[0].casefold().startswith(
            whose.group(1).casefold()))] or [one for one in setups if not one[2]]
        if hit:
            target = hit[-1] if whose is None else None
            for one in ([target] if target else hit):
                one[2] = True
                notes.append(f"{one[0]}'s setup ({one[1].count or '?'} leads) was called off by {who} - "
                             f"“{_quote(said, CALLED_OFF)}”")
    standing = [one for one in setups if not one[2]]
    # Theirs over another client's that the talk picked up - the $500 Ryan M
    # Thompson sent for Preston Tackett came out as Preston Bridges'.
    # In that order: their name, their email or phone, then a setup that
    # names them as the payer - Colton Ramon's $1,407.60 came out as Keaton
    # Artherton's, whose setup named Colton as well.
    for test in (lambda one: paid.name and same_person_strict(one[1].name, paid.name),
                 lambda one: (paid.email and one[1].email and one[1].email.casefold() == paid.email.casefold())
                 or (paid.phone and one[1].phone and _digits(one[1].phone)[-10:] == _digits(paid.phone)[-10:]),
                 lambda one: _payer_named(one[3], paid.name)):
        theirs = [one for one in standing if test(one)]
        if theirs:
            standing = theirs
            break
    else:
        # None of them theirs: another client's setup isn't held up against
        # this payment - only when it's a business paying, or no name to go by.
        if paid.name and not _BUSINESS.search(paid.name):
            return None, notes
    # And of those, one for what this payment bought - William McClure's old
    # $1,140 Trucker payment was held to his Vets setup.
    kind = family(paid.lead_type) - {"aged"}
    same_kind = [one for one in standing if kind and (family(one[1].lead_type) - {"aged"}) == kind]
    if same_kind:
        standing = same_kind
    # Fresh over aged, for a fresh payment: Rook's aged setup is Nova's pull,
    # the aged QC's - Hussein Suleiman's fresh Veterans were held to one.
    paid_aged = "aged" in family(paid.lead_type)
    same_age = [one for one in standing if ("aged" in family(one[1].lead_type)) == paid_aged]
    if same_age:
        standing = same_age
    if not standing:
        return None, notes
    fits = [one for one in standing if paid.count is not None and one[1].count == paid.count]
    chosen = (fits or standing)[-1]
    return chosen[1], notes, chosen[3]


def check(post: str, talk: list[tuple[str, str]], *, hub=None, hub_problem: str = "", sheet_rows: int | None = None,
          sheet_problem: str = "", hub_rows: list | None = None, paid_at=None) -> Checked:
    """The payment held up against Rook's setup, Nova's replies, the hub and
    the sheet. `talk`: [(who, what they said)] in #rook-nova, oldest first.
    `hub`: the hub's record for them, or None."""
    paid = payment(post)
    result_paid = paid
    result = Checked(client=paid.name)
    result.post_text = str(post or "")
    rook, called_off, *said = the_setup(talk, paid)
    result.notes += called_off
    result.setup_text = said[0] if said else ""
    result.talk_text = "\n".join(said for _who, said in talk)
    result.talk = list(talk)
    result.wanted = _filled(paid, rook)
    result.paid = result_paid
    if paid.aged:
        result.notes.append(f"Also on the invoice: {paid.aged} - Nova's pull, checked by the aged QC")
    if rook is not None and rook.name:
        # A business paying - "Kunz Marketing, Llc" for Kurtis Kunz.
        result.client = rook.name
    nova = [said for who, said in talk if who.casefold().startswith("nova")]
    result.picked_up = bool(talk)
    fulfilled = _fulfilled(talk)
    # The hub row this order is - of its lead type, an open one first.
    hub, hub_note, hub_elsewhere = resolve_hub(hub_rows or ([hub] if hub is not None else []), _filled(paid, rook),
                                               paid_at=paid_at)
    result.hub = hub
    if OVERFLOW.search(str(post or "")):
        # "Overflow Trucker - $25/lead": the team delivers it from overflow -
        # not Rook and Nova's, not on the hub. Karli Kelley's was held to her
        # older trucker row, Max Murphy's flagged "Not on the hub".
        result.picked_up = True
        result.overflow = True
        result.notes.append("Overflow order - delivered by the team, not set up on the hub")
        _delivered(result, fulfilled, paid, overflow=True)
        if not fulfilled:
            # Delivering takes the team days - Riley Noah's and Karli
            # Kelley's pinged Franklin as wrong the day they paid. Only an
            # order that's waited past that is.
            waited = _days_since(paid_at)
            if waited is not None and waited >= OVERFLOW_DAYS:
                result.problems.append(f"Overflow order paid {waited} days ago, and nothing says it's delivered yet "
                                       "(no \"This order has been fulfilled\")")
            else:
                result.todo.append("The team still delivers it from overflow - no \"This order has been fulfilled\" "
                                   "yet")
        return result
    quiet = not talk
    # On the hub for this order: an open row changed since the payment came in.
    touched = (hub is not None and not _over(hub) and getattr(hub, "updated_at", None) is not None
               and paid_at is not None and not _before(hub.updated_at, paid_at))
    if quiet and not touched:
        if _small(paid):
            # Thomas Poulin's $90: nothing said, and too little to be an order
            # of its own - an add-on or a fee. Said, not flagged.
            result.notes.append(f"A small payment (${paid.amount}) nobody's said anything about - an add-on or a "
                                "fee, most likely")
            return result
        result.problems.append("Rook and Nova haven't said anything about this payment in #rook-nova")
        return result
    if quiet:
        # Thomas Poulin's: nobody said a word, but the hub has it for this
        # order - checked on the hub alone.
        result.notes.append("Rook and Nova didn't say anything about it in #rook-nova - checked on the hub alone")
    if rook is None:
        result.notes.append("No \"New setup\" message from Rook to check field by field")
    else:
        _compare(result, "Rook's setup", rook, paid)
    for said in nova:
        for line in _HANDED.findall(said):
            line = " ".join(line.split())
            if _about_someone_else(line, result.client or paid.name):
                # "Ryan Gsell was paused by the team" on David Jacob's card,
                # from a Nova message about the two of them.
                continue
            if _NO_PAYMENT.search(line):
                # The payment's right here - it's what this QC is of.
                result.notes.append("Nova said it couldn't find the payment - it's this one in #new-payment")
                continue
            if _RISK.search(line) and not _about_someone_else(line, result.client or paid.name):
                # Nova warning what would go wrong - Aaron Crane's "the hub
                # would add a second 30 leads ... (he'd be owed 60)".
                result.problems.append(f"Nova warns: {line}")
                continue
            result.todo.append(line)
    # On the hub and going, or delivered to: set up, whatever was said on
    # the way - Ibrahim Shalash's "I couldn't set up Ibrahim yet", live with 7
    # delivered.
    going = hub is not None and (str(getattr(hub, "status", "") or "").casefold() in
                                 ("live", "active", "done", "fulfilled", "complete", "completed")
                                 or (getattr(hub, "delivered", None) or 0) > 0)
    # Set up since - on the hub for this order, put there after it was paid
    # (or not delivered to yet): Antonio Brancato's "couldn't" from before was
    # hammered for a card once he was already there.
    there = hub is not None and not _over(hub) and (
        not _before(getattr(hub, "updated_at", None), paid_at) if getattr(hub, "updated_at", None) and paid_at
        else not (getattr(hub, "delivered", None) or 0))
    for at, (who, said) in enumerate(talk):
        found = CANT.search(said)
        if not found:
            continue
        done_after = next((later for later_who, later in talk[at + 1:] if later_who.casefold().startswith("nova")
                           and _SET_UP.search(later) and not CANT.search(later)), None)
        if going or there or done_after or re.match(r"[^.\n]{0,40}\byet\b", said[found.end():], re.IGNORECASE):
            why = (f" - the hub has them {hub.status} now" if going or there
                   else " - Nova said it's set up after" if done_after else " yet")
            result.notes.append(f"{who} said earlier it couldn't set it up{why}")
            continue
        result.cant = f"{who}: " + " ".join(found.group(0).split())
        result.problems.append(f"{who} said they can't set it up - “{_quote(said, CANT)}”")
        # Said earlier and since - the newest is what stands.
        result.notes = [one for one in result.notes if "said earlier it couldn't set it up" not in one]
    if not nova and not quiet and not touched:
        result.problems.append("Nova hasn't answered Rook yet - nothing says it's set up")
    elif not nova and not quiet:
        result.notes.append("Nova hasn't answered Rook, but the hub has it")
    # Rook taking it to the team itself - "forwarded to Therese + Nicole in
    # Carson Rentz's #rook-qc thread": Ryte doesn't tag them again for it.
    handed = next((said for who, said in talk if who.casefold().startswith(("rook", "nova"))
                   and _TO_TEAM.search(said)), "")
    # Rook's thread in #rook-qc is taking it to the team, whatever it says.
    in_qc = [(who, said) for who, said in talk if who.endswith(IN_ROOK_QC)]
    handed = handed or next((said for who, said in in_qc if who.casefold().startswith(("rook", "nova"))), "")
    if handed:
        result.with_team = " ".join(handed.split())[:200]
        result.notes.append(f"Rook/Nova took it to the team: “{result.with_team}”")
    # Rook or Nova waiting on the client - Beniam's "Rook texted him asking
    # how many + states ... setup waits on his answer": not a miss, nor asked
    # about again.
    if hub is None:
        waiting = next((said for who, said in reversed(talk) if who.casefold().startswith(("rook", "nova"))
                        and _WAITING_CLIENT.search(said)), "")
        if waiting:
            result.waiting_client = " ".join(waiting.split())[:220]
            for one in [one for one in result.problems if one.startswith(_NOT_SET_UP_YET)]:
                result.problems.remove(one)
            result.notes.append(f"Waiting on the client - “{result.waiting_client}”")
    result.team_ok = team_answered(in_qc)
    if result.team_ok:
        result.notes.append(f"✅ The team answered in #rook-qc - {result.team_ok}")
    # Rook or Nova answering Ryte's question that it's right as it is -
    # "Expected — INV-21202 (paid by Chris Amaya) is Alain Ishimwe's order ...
    # No fix needed."
    result.rook_ok = answered_ok(talk)
    asked = open_question(talk)
    if asked:
        # James Wyman's "Can you confirm his exact start time for Wednesday
        # ...? Post it here and I'll set it." - nobody had.
        result.todo.append(f"Nova asked and nobody's answered: “{asked}”")
    # What the hub is held to: the payment, and Rook's setup for whatever
    # the payment doesn't say - a Zelle post carries no states or count.
    wanted = _filled(paid, rook)
    result.wanted = wanted
    if hub_note:
        result.notes.append(hub_note)
    if any(getattr(wanted, key) != getattr(paid, key) for key in ("email", "lead_type", "count", "states", "left_out")):
        result.notes.append("The payment doesn't say everything (a Zelle post, or no invoice notes) - the hub is "
                            "checked against Rook's setup where it doesn't")
    recent = _paid_for_recent(hub_rows or [], wanted, paid_at) if hub is None and hub_elsewhere else None
    # Nova saying it's set up, or pulled, while Ryte finds no row for it: not
    # "not on the hub" - "some of thse are live, and some of these are aged
    # and delivered". Ryte can't find what Nova did, and says so.
    # Only a message about them, by first and last name, that's about nobody
    # else first - Jay Trandahl's card quoted Nova on Jay Rodriguez.
    whom = Order(name=result.client or paid.name or "")
    nova_did = next(((done if isinstance(done, str) else said) for who, said in talk
                     if who.casefold().startswith("nova")
                     for done in [_done_said(said, pulls=not paid.aged)] if done
                     and not CANT.search(said) and (about(said, whom) or _first_named(said, whom.name))
                     and not _names_someone_else_first(said, whom.name)),
                    "") if hub is None and whom.name else ""
    if result.waiting_client and hub is None:
        pass  # waiting on the client - said in the notes, not flagged
    elif nova_did:
        looked = ", ".join(dict.fromkeys(f"{getattr(one, 'name', '?')} ({getattr(one, 'status', '') or '?'})"
                                         for one in hub_rows or [])) or "nobody by this name"
        result.problems.append(f"Nova says it's done (“{' '.join(nova_did.split())[:140]}”), but I can't find it on "
                               f"the hub - I looked for {result.client or paid.name or '?'} and found {looked}. "
                               "What name is it under?")
    elif recent is not None:
        # Brandon Nguyen's $400 Zelle, no product on it, the day after his
        # Vets order started - read as a new order nobody set up, when it
        # paid for that one, delivered. Which it is isn't guessed.
        start = getattr(recent, "start_date", None)
        result.problems.append(
            f"Not sure which order this pays for - the payment doesn't say, and their "
            f"{getattr(recent, 'lead_type', '') or 'last'} order started {start:%b} {start.day} "
            f"({getattr(recent, 'status', '') or '?'}, {getattr(recent, 'delivered', None) or 0} of "
            f"{getattr(recent, 'ordered', '?')} delivered). Payment for that one, or a new order not set up yet?")
    elif hub is None and hub_elsewhere:
        # Preston Bridges' old Mortgage Protection row, ended, held up
        # against his new Vets - nothing for this order was set up yet.
        result.problems.append(f"Nothing on the hub for this order yet - they're only there for {hub_elsewhere}")
    elif hub is None:
        result.problems.append(f"Not on the hub{f' ({hub_problem})' if hub_problem else ''}")
    else:
        _hub(result, hub, wanted)
    _delivered(result, fulfilled, paid)
    if not _set_up_twice(result, hub_rows, wanted, talk):
        _added_twice(result, hub, wanted, talk)
    # Rook's setup for more than this payment - Ryan Gsell's 30 paid in two
    # payments of 15, "BOTH payments are in #new-payment".
    said_all = "\n".join(said for _who, said in talk)
    several = _several_payments(talk)
    parts = _PARTS.search(said_all)
    if parts:
        # Charles Sellers' INV-21037, $2,160 by Zelle "in two parts: $2,000
        # (Nov 2) + $160 (today)" - each part a payment post of its own.
        result.notes.append("Paid in parts - “" + " ".join(_sentence(said_all, parts).split())[:220] + "”")
    if several and rook is not None and rook.count and paid.count and rook.count > paid.count:
        for one in [one for one in result.problems if one.startswith("Rook's setup has") and " leads, the payment " in one]:
            result.problems.remove(one)
            result.notes.append(f"Rook's setup covers more than this payment - {rook.count} leads across their "
                                f"payments, {paid.count} on this one")
    # Rook's message missing the count is nothing when the hub has it right.
    ordered = getattr(hub, "ordered", None) if hub is not None else None
    if paid.count is not None and ordered is not None and ordered >= paid.count:
        for one in [one for one in result.problems if "gives no lead count" in one]:
            result.problems.remove(one)
            result.notes.append(one + f" - the hub has {ordered} ordered")
    # Nor Rook's states left out when the hub's are the payment's: Cheyenne
    # Williams, live on AL and OR as paid for, flagged "Rook's setup has no
    # states".
    if hub is not None and getattr(hub, "states", ()) and not any(one.startswith("The hub states")
                                                                  or one.startswith("The hub shows no states")
                                                                  for one in result.problems):
        # Nor Rook's states being different, once the hub has the payment's
        # - justin techer's IN, added in his payment's thread after Rook's
        # setup: the hub's what the leads go by.
        for one in [one for one in result.problems if one.startswith(("Rook's setup has no states",
                                                                       "Rook's setup states"))]:
            result.problems.remove(one)
            result.notes.append(one + " - the hub has the right ones")
    # Rook's setup for another product, or another count, while the hub's row
    # is what the payment bought: the setup was for another order of theirs -
    # Cameron Bertok's 50 FEX against 100 Mortgage Protection paid for and on
    # the hub, Trey Dougher's Mortgage Protection against his Veterans.
    if hub is not None and not any(one.startswith(("The hub has the lead type", "The hub has ")) and
                                   ("lead type" in one or "ordered" in one) for one in result.problems):
        hub_kind = family(getattr(hub, "lead_type", ""))
        if hub_kind and (hub_kind - {"aged"}) == (family(paid.lead_type) - {"aged"}):
            for one in [one for one in result.problems
                        if one.startswith("Rook's setup has the lead type")
                        or (one.startswith("Rook's setup has ") and " leads, the payment " in one
                            and paid.count is not None and getattr(hub, "ordered", None) == paid.count)
                        or one.startswith("Rook's setup reads ")]:
                result.problems.remove(one)
                result.notes.append(one + " - the hub has what was paid for; Rook's setup is another order's")
    # Live or delivering: what was handed over before the setup is done
    # with - Matthew Stewart's "I can't find a PAID payment" under a tick.
    # Scheduled to start, or going, on a row for this order: what was handed
    # over on the way there is done with - Alyssa Booska's "a person picks
    # which row" and Ian Gibson's contract, both scheduled as paid for, and
    # both taken to Therese and Nicole.
    started = touched and str(getattr(hub, "status", "") or "").casefold() in ("scheduled", "live", "active")
    if going and result.todo:
        result.notes += [f"Handed to the team earlier: {one}" for one in dict.fromkeys(result.todo)]
        result.todo = []
    elif started and result.todo:
        # Nova's own question still open stays - only what was handed over.
        done = [one for one in result.todo if not one.startswith("Nova asked and nobody's answered")]
        result.notes += [f"Handed to the team earlier: {one}" for one in dict.fromkeys(done)]
        result.todo = [one for one in result.todo if one not in done]
    if going and not any("Set up twice?" in one or "added twice" in one for one in result.problems):
        # The hub's going and holds no more than was paid for: what Nova
        # warned of didn't happen.
        for one in [one for one in result.problems if one.startswith("Nova warns: ")]:
            result.problems.remove(one)
            result.notes.append(one.replace("Nova warns: ", "Nova warned earlier: ", 1) + " - the hub doesn't show it")
    if sheet_problem:
        result.problems.append(f"The sheet: {sheet_problem}")
    elif sheet_rows is not None:
        delivered = getattr(hub, "delivered", None) if hub is not None else None
        if delivered and sheet_rows < delivered - 1:
            # Carson Rentz: 100 delivered on the hub, "0 row(s)" on the sheet.
            result.problems.append(f"The hub says {delivered} delivered, but the sheet has {sheet_rows} lead row(s)")
        elif delivered and sheet_rows < delivered:
            # One off - a lead the sheet has once and the hub twice, a row
            # moved: Scout Allen's 24/23, Cameron Bertok's 11/10.
            result.notes.append(f"The hub says {delivered} delivered, the sheet has {sheet_rows} - one off, not flagged")
        else:
            result.ok.append(f"The sheet opens - {sheet_rows} lead row(s) on it")
    return result


#: Days the team has to deliver an overflow order before it's said as late.
OVERFLOW_DAYS = 3


def _days_since(at) -> int | None:
    from datetime import datetime as _dt, timezone as _tz
    if at is None:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=_tz.utc)
    return (_dt.now(_tz.utc) - at).days


#: An order that's overflow - the team's, delivered from overflow.
OVERFLOW = re.compile(r"\bove?r[\s-]*l?f+l*o*w(?:s|ed|ing)?\b", re.IGNORECASE)
_FULFILLED = re.compile(r"\b(?:order\s+has\s+been\s+fulfilled|fulfilled)\b[^\n]{0,40}?\bdelivered\s+(\d[\d,]*)\s+of\s+"
                        r"(\d[\d,]*)", re.IGNORECASE)


def _fulfilled(talk) -> tuple[int, int] | None:
    """(delivered, of) from Luna's "This order has been fulfilled - Delivered
    15 of 15", the newest said."""
    found = None
    for _who, said in talk:
        for one in _FULFILLED.finditer(str(said or "")):
            found = (int(one.group(1).replace(",", "")), int(one.group(2).replace(",", "")))
    return found


def _delivered(result: Checked, fulfilled, paid: Order, *, overflow: bool = False) -> None:
    """Luna's delivered count - held to the payment for an overflow order,
    whose count is its own; a hub row's "of" can be a renewal's whole."""
    if not fulfilled:
        return
    delivered, of = fulfilled
    if overflow and paid.count is not None and of != paid.count:
        result.problems.append(f"Delivered {delivered} of {of} - the payment was for {paid.count}")
    elif delivered < of:
        result.notes.append(f"Delivered {delivered} of {of} so far")
    else:
        result.ok.append(f"Delivered {delivered} of {of}" + (", as paid for" if paid.count == of else ""))


#: A handed-over line that warns of leads owed twice over.
_RISK = re.compile(r"\b(?:second|twice|double[ds]?|duplicat\w*)\b[^.\n]{0,80}\b(?:leads?|order|renewal|owed)\b|"
                   r"\bowed\s+\d+|\boverpa(?:id|y)\b", re.IGNORECASE)
_NO_PAYMENT = re.compile(r"\bcan(?:'|’)?t\s+find\s+(?:a\s+|any\s+|the\s+)?(?:paid\s+)?payment|"
                         r"\bno\s+payment\b[^.]{0,60}\bfound\b|\bwhether\s+(?:he|she|they)(?:'s|\s+has)?\s+paid\b",
                         re.IGNORECASE)


def _filled(paid: Order, rook: Order | None) -> Order:
    """The payment with Rook's setup filling what it leaves out."""
    if rook is None:
        return paid
    gaps = {key: getattr(rook, key) for key in ("email", "phone", "lead_type", "count", "launch")
            if not getattr(paid, key) and getattr(rook, key)}
    if not (paid.states or paid.left_out) and (rook.states or rook.left_out):
        gaps.update(states=list(rook.states), left_out=list(rook.left_out))
    if not gaps:
        return paid
    return Order(**{**paid.__dict__, **gaps})


def _quote(said: str, pattern) -> str:
    """The words around what was found - not the tags in front of them:
    William McClure's came out "60184287276> @🌺 Faith 🌺 @Nicole I couldn't"."""
    said = re.sub(r"<[@#][!&]?\d+>", "", str(said or ""))
    found = pattern.search(said)
    start = max(0, found.start() - 60) if found else 0
    if start:
        # From the start of a sentence or word, not the middle of one.
        cut = max(said.rfind(". ", 0, found.start()) + 2, said.rfind("\n", 0, found.start()) + 1)
        start = cut if found.start() - cut <= 120 and cut > 0 else said.find(" ", start) + 1
    return " ".join(said[start:start + 160].split())


#: A payer that's a business - "The Eissa Group Llc" for Mohammad Kanan.
_BUSINESS = re.compile(r"\b(?:llc|l\.l\.c|inc|corp|corporation|co|company|group|agency|marketing|financial|"
                       r"insurance|holdings|enterprises?|ltd|lp|pllc|partners|solutions|services)\b\.?", re.IGNORECASE)


def _payer_named(text: str, payer: str) -> bool:
    """Rook's setup saying this payer paid for them - "two from Ryan M
    Thompson (his friend sent them for him)", "paid by ...". Not the name
    only: Keaton Artherton's setup named Colton Ramon, who paid for his own
    40, and Colton's payment came out as Keaton's 20."""
    words = [one for one in re.findall(r"[A-Za-z][A-Za-z'’-]+", str(payer or "")) if len(one) > 1]
    if len(words) < 2:
        return False
    said = " ".join(str(text or "").split())
    first, last = re.escape(words[0]), re.escape(words[-1])
    name = rf"{first}(?:\s+\S+){{0,2}}\s+{last}"
    paying = r"(?:paid|pays|paying|sent|sends|zelle[d]?|covered|covers|on\s+behalf|for\s+(?:him|her|them))"
    return bool(re.search(rf"\b(?:paid\s+by|from|sent\s+by|via)\s+{name}\b|\b{name}\b[^.\n]{{0,40}}\b{paying}\b"
                          rf"|\b{paying}\b[^.\n]{{0,25}}\b{name}\b", said, re.IGNORECASE))


#: Words a sentence starts with that aren't somebody's name.
_NOT_NAMES = {"the", "his", "her", "their", "this", "that", "these", "those", "only", "rook", "nova", "luna",
              "payment", "hub", "price", "lead", "leads", "order", "orders", "contract", "setup", "setting",
              "during", "channel", "status", "he", "she", "they", "i", "it", "a", "an", "no", "nothing", "there",
              "more", "one", "both", "team", "start", "discount", "card", "sheet", "zelle", "payra", "after",
              "before", "if", "when", "once", "still", "already", "please", "pending", "veteran", "trucker",
              "fresh", "aged", "text", "everything", "someone", "somebody", "nobody", "lead", "can't", "cannot"}


_DONE_MORE = r"|✅[^.\n]{0,60}\bset\s+up\b|\bpulled\b|\border\s+is\s+(?:done|finished)\b"
#: A sentence that isn't saying it's done: "Scout has never had a Widow
#: order, so there's no last count ... Neither of his two orders is live" -
#: read as Nova saying it was set up.
_NOT_DONE = re.compile(r"\bnever\b|\bnot\b|n['’]t\b|\bno\b|\bneither\b|\bonce\b|\bif\b|\buntil\b|\bbefore\b|"
                       r"\bneeds?\b|\bwaiting\b|\bconfirm\b|\bwill\b|['’]ll\b|\bshould\b|\bwould\b|\?",
                       re.IGNORECASE)


_NOT_SET = re.compile(r"\b(?:didn['’]?t|did\s+not|haven['’]?t|have\s+not|hasn['’]?t|has\s+not|wasn['’]?t|was\s+not|"
                      r"isn['’]?t|is\s+not|can['’]?t|cannot|couldn['’]?t|not)\s+(?:been\s+|yet\s+)?(?:\w+\s+){0,2}"
                      r"set\s*-?\s*up\b|\bisn['’]?t\s+tied\s+to\b", re.IGNORECASE)


def _done_said(said: str, *, pulls: bool = True):
    """Nova saying it's set up or pulled - in a sentence that says so, not
    one that says it isn't, or asks, or waits on something. `pulls`: whether
    "pulled" counts - not when the invoice has aged lines too: William
    McClure's "Pulled 100" was his Aged IUL, not his Veteran setup."""
    text = str(said or "")
    # "I didn't set anything up for Mitchell" - a message saying it isn't
    # set up isn't Nova saying it's done, whatever else it says.
    if _NOT_SET.search(text):
        return ""
    for found in re.finditer(_SET_UP.pattern + _DONE_MORE, text, re.IGNORECASE):
        if not pulls and re.fullmatch(r"pulled", found.group(0), re.IGNORECASE):
            continue
        start = max(text.rfind(". ", 0, found.start()), text.rfind("\n", 0, found.start()),
                    text.rfind("? ", 0, found.start()), text.rfind("! ", 0, found.start())) + 1
        ends = [one for one in (text.find(". ", found.end()), text.find("\n", found.end()),
                                text.find("? ", found.end())) if one >= 0]
        end = min(ends) + 1 if ends else len(text)
        if not _NOT_DONE.search(text[start:end]):
            # The sentence that says so - quoted, not the message's start.
            return " ".join(text[start:end].split()) or True
    return ""


def _first_named(said: str, client: str) -> bool:
    """Their first name in it - Nova writes "I set up Jonathan's order". Not
    a business's first word: "The Eissa Group Llc"'s card quoted Nova on
    Kurtis Kunz, for "the"."""
    words = _name_words(client)
    if _BUSINESS.search(str(client or "")) or (words and (words[0] in _NOT_NAMES or len(words[0]) < 3)):
        return False
    return bool(words) and bool(re.search(rf"\b{re.escape(words[0])}(?:['’]s)?\b", str(said or ""), re.IGNORECASE))


def _names_someone_else_first(said: str, client: str) -> bool:
    """A message whose first name in bold or at its start is somebody else's:
    "**Jay Rodriguez** — what was said above ..." on Jay Trandahl's card."""
    first = re.search(r"\*\*([A-Z][\w'’-]+(?:\s+[A-Z][\w'’-]+){1,2})\*\*", str(said or ""))
    name = first.group(1) if first else ""
    if not name:
        lead = re.match(r"\W*(?:@\S+\s+)*([A-Z][a-z'’-]+\s+[A-Z][a-z'’-]+)\b", str(said or ""))
        name = lead.group(1) if lead else ""
    return bool(name) and not same_person_strict(name, client) and len(_name_words(name)) >= 2 \
        and _name_words(name)[0] not in _NOT_NAMES


def _about_someone_else(line: str, client: str) -> bool:
    """A handed-over line that starts with another person's name - "Ryan's
    pause ...", "Ryan Gsell was paused ..." on David Jacob's card."""
    found = re.match(r"\W*([A-Z][a-z]+)(?:['’]s\b|\s+[A-Z][a-z]+\b)", str(line or ""))
    if not found or found.group(1).casefold() in _NOT_NAMES:
        return False
    return found.group(1).casefold() not in _name_words(client)


def _compare(result: Checked, whose: str, theirs: Order, paid: Order) -> None:
    if paid.name and theirs.name and not same_person(paid.name, theirs.name):
        if _payer_named(result.setup_text + "\n" + result.talk_text, paid.name) or _BUSINESS.search(paid.name):
            # Somebody paying for them, and Rook's setup says so - Preston
            # Tackett's, two parts sent by his friend Ryan M Thompson.
            result.notes.append(f"Paid by {paid.name} for {theirs.name} - Rook's setup says so")
        else:
            result.problems.append(f"{whose} has the name {theirs.name}, the payment {paid.name}")
    if theirs.email and re.search(r"@agentleadlab\.com$", theirs.email.strip(), re.IGNORECASE):
        # Our own address in Rook's setup - a stand-in, not theirs: Perry
        # Sandlin's "support@agentleadlab.com".
        result.notes.append(f"{whose} has our own {theirs.email} as the email - a stand-in, not checked")
    elif paid.email and theirs.email:
        if paid.email.casefold() != theirs.email.casefold():
            result.problems.append(f"{whose} has the email {theirs.email}, the payment {paid.email}")
        else:
            result.ok.append(f"{whose}: email matches")
    elif paid.email and not theirs.email:
        # Left out isn't wrong - Max Wilson's.
        result.notes.append(f"{whose} gives no email (the payment's is {paid.email})")
    if paid.phone and theirs.phone and _digits(paid.phone)[-10:] != _digits(theirs.phone)[-10:]:
        # Not a hub problem - the hub keeps no phone - and not one to take to
        # Therese and Nicole (Corbin Reed's): said, not flagged.
        result.notes.append(f"{whose} has the phone {theirs.phone}, the payment {paid.phone}")
    if paid.count is not None and theirs.count is not None:
        if paid.count != theirs.count:
            result.problems.append(f"{whose} has {theirs.count} leads, the payment {paid.count}")
        else:
            result.ok.append(f"{whose}: {theirs.count} leads, as paid for")
    elif paid.count is not None:
        result.notes.append(f"{whose} gives no lead count (paid for {paid.count})")
    paid_kind, their_kind = family(paid.lead_type), family(theirs.lead_type)
    several = len(re.findall(r"\bx\s*\d+|\d+\s*x\b|×\s*\d+|\band\b|\+|,", str(theirs.lead_type or ""), re.IGNORECASE)) >= 1
    if (paid_kind and their_kind and (paid_kind - {"aged"}) != (their_kind - {"aged"}) and several
            and (paid_kind - {"aged"}) < (their_kind - {"aged"})):
        # Rook's setup for the whole order, the payment's line one product of
        # it - Evan Mori's "Veteran x10, and IUL Plus x10" against "Veteran".
        result.notes.append(f"{whose} is for “{theirs.lead_type}” - more than this payment's “{paid.lead_type}” line")
    elif paid_kind and their_kind and (paid_kind - {"aged"}) != (their_kind - {"aged"}):
        result.problems.append(f"{whose} has the lead type “{theirs.lead_type}”, the payment “{paid.lead_type}”")
    elif paid_kind and their_kind and ("aged" in paid_kind) != ("aged" in their_kind):
        result.problems.append(f"{whose} reads {'aged' if 'aged' in their_kind else 'fresh'}, the payment "
                               f"{'aged' if 'aged' in paid_kind else 'fresh'} (“{paid.lead_type}”)")
    elif paid_kind and their_kind:
        result.ok.append(f"{whose}: lead type matches")
    _compare_states(result, whose, theirs.states, theirs.left_out, paid)


def _state_set(states, left_out) -> set:
    """The states meant, as codes - "all but NY" as every other one."""
    if states:
        return set(states)
    if left_out:
        return set(US_STATES) - set(left_out)
    return set()


def _compare_states(result: Checked, whose: str, states, left_out, paid: Order) -> None:
    # "All but NY" and the 50 others written out are the same states -
    # Duehat Kanan's and Ciana Araya's were flagged as not matching.
    theirs, wanted = _state_set(states, left_out), _state_set(paid.states, paid.left_out)
    if theirs and wanted:
        extra = sorted(theirs - wanted)
        missing = [code for code in (paid.states or sorted(wanted)) if code in wanted - theirs]
        if "DC" in extra + missing:
            # DC, which a list of "all states" has or hasn't: said, not flagged.
            result.notes.append(f"{whose}: DC " + ("not ordered" if "DC" in extra else "left off"))
            extra, missing = [one for one in extra if one != "DC"], [one for one in missing if one != "DC"]
        if extra and whose == "The hub":
            # More states on the hub than ordered: the team widens them to
            # deliver - owen anderson's, Amari Conway's, a dozen a list, all
            # "wrong". Rook's setup with extra states is still said: David
            # Jacob's came off the lead request, not the payment.
            result.notes.append(f"{whose} has {', '.join(extra)} too - more states than ordered, not flagged")
            extra = []
        if extra or missing:
            result.problems.append(f"{whose} states don't match the payment"
                                   + (f" - has {', '.join(extra)} not ordered" if extra else "")
                                   + (f" - missing {', '.join(missing)}" if missing else ""))
        elif not any(one.startswith(f"{whose} has ") and "more states than ordered" in one for one in result.notes):
            result.ok.append(f"{whose}: all {len(theirs)} states match")
    elif wanted and not theirs:
        if whose == "The hub":
            result.problems.append(f"{whose} has no states (the payment: "
                                   + (", ".join(paid.states) or f"all but {', '.join(paid.left_out)}") + ")")
        else:
            # Rook left them out - the hub's held to the payment's all the same.
            result.notes.append(f"{whose} gives no states - the hub's are checked against the payment's")


#: Hub statuses of an order that's over - an older order, not this one.
FINISHED = ("ended", "done", "fulfilled", "complete", "completed", "cancelled", "canceled")


def _wrong_kind(row, wanted: Order) -> bool:
    kind, theirs = family(getattr(row, "lead_type", "")), family(wanted.lead_type)
    return bool(kind and theirs and (kind - {"aged"}) != (theirs - {"aged"}))


def pick_row(rows: list, wanted: Order):
    """The hub row an order fits best: its lead type, an open one over one
    that's over, its states, its count - the newest when they fit alike."""
    from datetime import datetime as _dt, timezone as _tz

    def misfit(row) -> tuple:
        states = set(getattr(row, "states", ()) or ())
        off = len(states ^ set(wanted.states)) if wanted.states and states else 0
        over = 1 if _over(row) else 0
        short = 1 if wanted.count and getattr(row, "ordered", None) is not None and row.ordered < wanted.count else 0
        newest = getattr(row, "updated_at", None) or _dt.min.replace(tzinfo=_tz.utc)
        return (1 if _wrong_kind(row, wanted) else 0, over, off, short,
                -newest.timestamp() if newest.year > 1 else 0)

    return min(rows, key=misfit)


def _before(at, paid_at) -> bool:
    """`at` is before the payment - both known."""
    if at is None or paid_at is None:
        return False
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    if paid_at.tzinfo is None:
        paid_at = paid_at.replace(tzinfo=timezone.utc)
    return at < paid_at


def _paid_for_recent(rows: list, wanted: Order, paid_at):
    """The order a payment that says nothing of what it bought may be paying
    for: their newest row started in the three weeks before it. None when
    the payment (or Rook's setup) says what it's for, or there's none."""
    # "— (match to their order)" says nothing either.
    if wanted.count is not None or family(wanted.lead_type) or paid_at is None:
        return None
    from zoneinfo import ZoneInfo

    paid_on = (paid_at if paid_at.tzinfo else paid_at.replace(tzinfo=timezone.utc)).astimezone(
        ZoneInfo("America/New_York")).date()
    started = [one for one in rows if getattr(one, "start_date", None) is not None
               and timedelta(0) <= paid_on - one.start_date <= timedelta(days=21)]
    return max(started, key=lambda one: one.start_date, default=None)


def _started_on_before(row, paid_at) -> bool:
    """Started on a day before the payment's."""
    start = getattr(row, "start_date", None)
    if start is None or paid_at is None:
        return False
    from zoneinfo import ZoneInfo

    paid_on = (paid_at if paid_at.tzinfo else paid_at.replace(tzinfo=timezone.utc)).astimezone(
        ZoneInfo("America/New_York")).date()
    return start < paid_on


def _started_before(row, paid_at) -> bool:
    """Filled, and started on a day before the payment."""
    start = getattr(row, "start_date", None)
    ordered, delivered = getattr(row, "ordered", None), getattr(row, "delivered", None) or 0
    if start is None or paid_at is None or not ordered or delivered < ordered:
        return False
    from zoneinfo import ZoneInfo

    paid_on = (paid_at if paid_at.tzinfo else paid_at.replace(tzinfo=timezone.utc)).astimezone(
        ZoneInfo("America/New_York")).date()
    return start < paid_on


def _over(row) -> bool:
    """Ended or done - or every lead delivered, whatever the status says:
    Brandon Nguyen's and Benny Biniamin's "paused" at 31 of 30 and 26 of 25
    were held up as their new payments set up."""
    if str(getattr(row, "status", "") or "").casefold() in FINISHED:
        return True
    ordered, delivered = getattr(row, "ordered", None), getattr(row, "delivered", None) or 0
    return bool(ordered) and delivered >= ordered


def _described(row) -> str:
    """"Text Verified Vet (done, 10 of 10 delivered)"."""
    bits = [str(getattr(row, "status", "") or "?")]
    if getattr(row, "ordered", None) is not None:
        bits.append(f"{getattr(row, 'delivered', None) or 0} of {row.ordered} delivered")
    return f"{getattr(row, 'lead_type', '') or '?'} ({', '.join(bits)})"


def _launch_on(launch: str, paid_at) -> str:
    """Rook's launch day as a date, read from the day it was paid - "Monday"
    written on Fri Oct 31 is Mon Nov 2, not the Monday after the card's made
    (William McClure's card said Nov 9). As written when there's no day in it."""
    from .dailyops import day_named

    said = " ".join(str(launch or "").split())
    if not said or paid_at is None:
        return said
    from zoneinfo import ZoneInfo

    paid_on = (paid_at if paid_at.tzinfo else paid_at.replace(tzinfo=timezone.utc)).astimezone(
        ZoneInfo("America/New_York")).date()
    if re.search(r"\d{1,2}/\d{1,2}|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d", said,
                 re.IGNORECASE):
        return said  # a date's written already
    when = paid_on if re.search(r"\btoday\b|\basap\b|\bright\s+away\b", said, re.IGNORECASE) and not re.search(
        r"\btomorrow\b|\b(?:mon|tues|wednes|thurs|fri|satur|sun)day\b", said, re.IGNORECASE) else day_named(
        said, today=paid_on)
    if when is None:
        return said
    return f"{when:%A %b} {when.day} (Rook's setup said “{said}” on {paid_on:%a %b} {paid_on.day}, when they paid)"


def card_notes(result: Checked, *, paid_at=None) -> str:
    """Rook's setup in the invoice form's words, for the card a hammer makes
    - Charles Sellers' $160 Zelle post said nothing, and his card came up
    "Nothing to make a card from" while Rook's setup had it all."""
    text = result.setup_text
    rook = rook_setup(text) if text else None
    if rook is None:
        return ""
    lines = ["Rook's setup in #rook-nova:", f"Name: {rook.name}" if rook.name else ""]
    lines += [f"Email: {rook.email}" if rook.email else "", f"Phone Number: {rook.phone}" if rook.phone else ""]
    lines += [f"Lead Type: {rook.lead_type}" if rook.lead_type else ""]
    states = ", ".join(rook.states) + (f" (all but {', '.join(rook.left_out)})" if rook.left_out else "")
    lines += [f"States Leads In: {states}" if rook.states or rook.left_out else ""]
    lines += [f"Number of Requested Leads: {rook.count}" if rook.count else ""]
    lines += [f"Launch Date: {_launch_on(rook.launch, paid_at)}" if rook.launch else ""]
    extra = _field(text, r"Specific\s+Notes", r"Notes")
    lines += [f"Specific Notes: {extra}" if extra else ""]
    return "\n".join(one for one in lines if one)


#: An answer that what Ryte asked about is meant to be.
_ALL_RIGHT = re.compile(r"\bno\s+fix\s+needed\b|\bnothing\s+(?:to\s+fix|wrong)\b|^\W*expected\b|"
                        r"\bset\s+up\s+(?:right|correctly)\b|\bis\s+(?:right|correct)\s+as\s+is\b|"
                        r"\bas\s+(?:intended|expected)\b|\bthat'?s\s+(?:right|correct|expected|intended)\b|"
                        r"\bare\s+the\s+same\s+(?:lead\s+type|product)\b", re.IGNORECASE | re.MULTILINE)


#: Rook or Nova waiting on the client's answer before setting it up.
_WAITING_CLIENT = re.compile(r"\b(?:setup\s+)?waits?\s+on\s+(?:his|her|their|the\s+client['’]?s?)\s+(?:answer|reply|response)"
                             r"|\bwaiting\s+(?:on|for)\s+(?:his|her|their|the\s+client['’]?s?)\s+(?:answer|reply|response)"
                             r"|\b(?:texted|messaged|asked)\s+(?:him|her|them|the\s+client)\s+(?:asking|to\s+confirm|for|which|"
                             r"what|how\s+many)", re.IGNORECASE)
#: What a payment nobody's set up yet says.
_NOT_SET_UP_YET = ("Not sure which order this pays for", "Not on the hub", "Nothing on the hub for this order",
                   "Nova hasn't answered Rook", "Rook and Nova haven't said")

#: Said after who it is, for what's said in #rook-qc.
IN_ROOK_QC = " (#rook-qc)"

#: The team saying it's handled: "All good. I've shared it with him".
_TEAM_DONE = re.compile(r"\b(?:all\s+good|good\s+to\s+go|done|fixed|sorted|resolved|taken\s+care\s+of|shared|"
                        r"set\s*-?\s*up|added|updated|pulled|voided|refunded|processed|corrected|changed|"
                        r"confirmed|handled|no\s+(?:fix|action)\s+needed)\b|✅", re.IGNORECASE)
_TEAM_NOT = re.compile(r"\?|\b(?:not\s+yet|can['’]?t|cannot|couldn['’]?t|won['’]?t|waiting|pending|need(?:s|ed)?\s+to|"
                       r"still\s+(?:need|waiting|missing|not))\b", re.IGNORECASE)


def team_answered(in_qc) -> str:
    """The team's last word in #rook-qc, when it says it's handled - "Nicole:
    “All good. I've shared it…”". Not when Rook or Nova spoke after it, nor
    a question or a "not yet"."""
    for who, said in reversed(list(in_qc)):
        name = who[:-len(IN_ROOK_QC)] if who.endswith(IN_ROOK_QC) else who
        if name.casefold().startswith(("ryte",)):
            continue
        if name.casefold().startswith(("rook", "nova", "luna")):
            return ""
        if _TEAM_DONE.search(said) and not _TEAM_NOT.search(said):
            return f"{' '.join(name.split())}: “{' '.join(str(said).split())[:160]}”"
        return ""
    return ""


def answered_ok(talk) -> str:
    """Rook or Nova's word, after Ryte last asked, that it's right as it is -
    "" when there's none. Not a "forwarded to Nicole", nor a "can't"."""
    asked = next((at for at in range(len(talk) - 1, -1, -1) if talk[at][0].casefold().startswith("ryte")
                  and "QC on" in talk[at][1]), None)
    if asked is None:
        return ""
    for who, said in talk[asked + 1:]:
        if (who.casefold().startswith(("rook", "nova")) and _ALL_RIGHT.search(said) and not CANT.search(said)
                and not _TO_TEAM.search(said)):
            return " ".join(said.split())[:240]
    return ""


def open_question(talk) -> str:
    """Nova's question in its last word with nothing said after it - "" when
    there's none, or somebody answered."""
    last = next((at for at in range(len(talk) - 1, -1, -1) if talk[at][0].casefold().startswith("nova")), None)
    if last is None or any(not who.casefold().startswith("nova") for who, _said in talk[last + 1:]):
        return ""
    asks = [" ".join(one.split()) for one in re.findall(r"[^.!?\n]*\?", talk[last][1])]
    asks = [one for one in asks if len(one) > 15]
    return " ".join(asks)[:300]


def resolve_hub(rows: list, wanted: Order, *, paid_at=None) -> tuple:
    """(the hub row this order is or None, a note, what they're on the hub
    for instead when none of their rows is this lead type)."""
    rows = [one for one in rows or [] if one is not None]
    if not rows:
        return None, "", ""
    fits = [one for one in rows if not _wrong_kind(one, wanted)]
    # A row that's over and holds fewer than this payment bought is an older
    # order, not this one renewed - Jared Flanigan's done 10 of 10, held up
    # as "the hub has 10 ordered - less than the 40 paid for".
    fits = [one for one in fits if not (_over(one) and wanted.count and getattr(one, "ordered", None) is not None
                                        and one.ordered < wanted.count)]
    # Nor one that was over before this was paid - Jacob Sides' done 8 of 8,
    # read as his new 6 delivered - nor one with every lead delivered that
    # started before it: a renewal adds leads, so it wouldn't be filled.
    # (Not by its start day: a renewal goes onto the same row and keeps its
    # start - Jay Trandahl's 25 on his Oct 29 row, now 57 of 55, and Brandon
    # Nguyen's, were read as older orders. When it last changed tells.)
    fits = [one for one in fits if not (_over(one) and _before(getattr(one, "updated_at", None), paid_at))]
    if not fits and any(not _wrong_kind(one, wanted) for one in rows):
        return None, "", ", ".join(dict.fromkeys(_described(one) for one in rows)) + " - an older order"
    if not fits:
        # An open row of another lead type may be this order set up wrong -
        # held up against it, its lead type is said. Only rows that are over
        # are older orders: nothing for this one is on the hub yet.
        # Not one started before this was paid, though - that's another
        # order: William McClure's live Trucker row, held up against his
        # new Vets while Nova said the Vets weren't on the hub at all.
        fits = [one for one in rows if not _over(one) and not _started_on_before(one, paid_at)]
    if not fits:
        return None, "", ", ".join(dict.fromkeys(_described(one) for one in rows))
    row = pick_row(fits, wanted)
    note = (f"{len(rows)} hub rows for them - checked {getattr(row, 'name', '?')}, the one that fits this payment best"
            if len(rows) > 1 else "")
    return row, note, ""


def _hub(result: Checked, hub, paid: Order) -> None:
    states = list(getattr(hub, "states", ()) or ())
    status = str(getattr(hub, "status", "") or "")
    bits = [f"status {status or '?'}"]
    if getattr(hub, "ordered", None) is not None:
        bits.append(f"{hub.ordered} ordered / {hub.delivered or 0} delivered")
    if getattr(hub, "daily_cap", None):
        bits.append(f"cap {hub.daily_cap}/day")
    if getattr(hub, "schedule", ""):
        bits.append(hub.schedule)
    result.ok.append("On the hub: " + " · ".join(bits))
    if paid.count is not None and getattr(hub, "ordered", None) is not None and hub.ordered < paid.count:
        result.problems.append(f"The hub has {hub.ordered} ordered - less than the {paid.count} paid for")
    hub_kind, paid_kind = family(getattr(hub, "lead_type", "")), family(paid.lead_type)
    if hub_kind and paid_kind and (hub_kind - {"aged"}) != (paid_kind - {"aged"}):
        result.problems.append(f"The hub has the lead type “{hub.lead_type}”, the payment “{paid.lead_type}”")
    if states:
        _compare_states(result, "The hub", states, [], paid)
    elif paid.states or paid.left_out:
        result.problems.append("The hub shows no states for them")
    if not getattr(hub, "sheet_url", ""):
        result.problems.append("The hub has no sheet for them")


#: Rook or Nova taking it to the team: "forwarded to Therese + Nicole".
_TO_TEAM = re.compile(r"\b(?:forward(?:ed|ing)?|passed|sent|flagged|escalated)\b[^.\n]{0,30}\bto\s+"
                      r"@?(?:therese|nicole|faith)\b|\balready\s+with\s+@?(?:therese|nicole|faith|the\s+team)\b",
                      re.IGNORECASE)


#: Nova saying the setup's done: "Done ✅ Jared's start is set for tomorrow".
_SET_UP = re.compile(r"\bdone\b\s*[✅✔]|\b(?:is|are|now|all)\s+set\s+up\b|\bset\s+(?:him|her|them)\s+up\b(?!\s+yet)|"
                     r"\bstart\s+is\s+set\b|\bis\s+set\s+to\s+start\b", re.IGNORECASE)


#: One order paid in pieces: "by Zelle in two parts", "the split payment".
_PARTS = re.compile(r"\bin\s+(?:two|three|2|3|several)\s+(?:parts|payments|installments)\b|\bsplit\s+payments?\b|"
                    r"\btwo\s+separate\s+payments\b", re.IGNORECASE)


def _sentence(text: str, found) -> str:
    start = max(text.rfind(".", 0, found.start()), text.rfind("\n", 0, found.start())) + 1
    ends = [at for at in (text.find("\n", found.end()),) if at != -1]
    return text[start:min(ends) if ends else len(text)]


def _several_payments(talk) -> bool:
    said_all = "\n".join(said for _who, said in talk)
    return bool(len(set(re.findall(r"\bINV-?\d+\b", said_all, re.IGNORECASE))) >= 2 or re.search(
        r"\b(?:both|two|2)\s+payments\b", said_all, re.IGNORECASE) or _PARTS.search(said_all))


def _set_up_twice(result: Checked, rows, paid: Order, talk) -> bool:
    """Two open hub rows of this lead type, neither delivered to, holding
    more than was paid for - Aaron Crane and Aaron_Crane_2, 30 each, both
    unsigned, for one payment of 30: he'd be owed 60. True when said."""
    if not rows or paid.count is None or _several_payments(talk):
        return False
    fresh = [one for one in rows if one is not None and not _wrong_kind(one, paid)
             and str(getattr(one, "status", "") or "").casefold() not in FINISHED
             and getattr(one, "ordered", None) and not (getattr(one, "delivered", None) or 0)]
    if len(fresh) < 2:
        return False
    owed = sum(one.ordered for one in fresh)
    extra = owed - paid.count
    if extra < max(5, paid.count // 2):
        return False
    rows_said = " and ".join(f"{getattr(one, 'name', '?')} ({getattr(one, 'status', '') or '?'}, "
                             f"{one.ordered} to deliver)" for one in fresh)
    result.problems.append(f"{len(fresh)} open hub rows for this lead type, none delivered to yet - {rows_said}: "
                           f"{owed} to deliver for the {paid.count} paid for. Set up twice?")
    return True


#: Under this, a payment nobody's said a word about is an add-on or a fee.
SMALL_PAYMENT = 150.0


def _small(paid: Order) -> bool:
    try:
        return 0 < float(str(paid.amount or "0").replace(",", "").replace("$", "")) < SMALL_PAYMENT
    except ValueError:
        return False


#: A payment for this many leads or fewer onto an order that's running is an
#: add-on to it, not an order of its own.
SMALL_ADD_ON = 5


def _added_twice(result: Checked, hub, paid: Order, talk) -> None:
    """More still to deliver on the hub than this payment bought: a renewal
    added twice - Luna's 50 for Marcus Uhrich on top of the 55 from
    INV-21186 - or leads left over from before."""
    if hub is None or paid.count is None:
        return
    ordered, delivered = getattr(hub, "ordered", None), getattr(hub, "delivered", None) or 0
    if ordered is None or str(getattr(hub, "status", "") or "").casefold() in FINISHED:
        return
    if _several_payments(talk):
        return  # one setup for several payments - Ryan Gsell's
    extra = ordered - delivered - paid.count
    if extra <= 0:
        return
    if paid.count <= SMALL_ADD_ON:
        # A few leads added to an order that's running - Connor Swartz's 4:
        # what's left of the order is the order's, not "added twice".
        result.notes.append(f"{paid.count} lead(s) added to an order still running - {ordered - delivered} to deliver "
                            f"on it")
        return
    line = (f"The hub has {ordered - delivered} still to deliver ({ordered} ordered, {delivered} delivered) - "
            f"{extra} more than the {paid.count} paid for")
    if extra >= max(5, paid.count):
        # As much again as was paid - added twice. Less is what was left of
        # the order before, running on (Victory Aidah's 16 over 30).
        result.problems.append(line + ": added twice, or left over from before?")
    else:
        result.notes.append(line + " - left over from before, most likely")


def said(result: Checked, *, link: str = "", amount: str = "") -> str:
    """The QC as one message for Ryte The Goat."""
    head = f"🔎 **Setup QC — {result.client or 'a payment'}**" + (f" (${amount})" if amount else "")
    if result.problems:
        lines = [head + " — ⚠ needs a look:"]
        for one in result.problems:
            when = result.still.get(one)
            lines.append(f"• {one}" + (f" — **still not fixed** (flagged {_when(when)})" if when else ""))
    else:
        lines = [head + " — ✅ Rook, Nova and the hub match the payment"
                 + (", but there's still something for the team:" if result.todo else "")]
    if result.todo:
        lines.append("**Handed to the team:**")
        lines += [f"• {one}" for one in dict.fromkeys(result.todo)]
    lines += [f"✅ Fixed since it was flagged: {one}" for one in result.fixed]
    if result.asked:
        lines.append(f"-# Asked Rook & Nova {_when(result.asked[1]) if result.asked[1] else ''}: <{result.asked[0]}>")
    lines += [f"-# ✓ {one}" for one in result.ok]
    lines += [f"-# {one}" for one in result.notes]
    if link:
        lines.append(f"-# <{link}>")
    return "\n".join(lines)


def _when(iso: str) -> str:
    """"Nov 2, 4:17 PM" in New York - or as it was kept."""
    try:
        from zoneinfo import ZoneInfo

        at = datetime.fromisoformat(str(iso)).astimezone(ZoneInfo("America/New_York"))
    except (ValueError, TypeError, KeyError):
        return str(iso or "earlier")
    return f"{at:%b} {at.day}, {at:%-I:%M %p}"


def flagged_before(result: Checked, held: dict) -> dict:
    """What an earlier QC flagged, held up against this one: `result.still`
    and `result.fixed` filled in - "did ryte check if this was fixed, cuz we
    flagged this already" (Kyle Moffitt's hub states). The flags to keep."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    before = (dict(held.get("flagged") or {}) if "flagged" in held
              else {one: held.get("at") or now for one in held.get("problems") or []})
    # The same problem with its numbers moved on - "paid 3 days ago" a day
    # later, "41 still to deliver" after a delivery - is the same problem:
    # Karli Kelley's overflow re-tagged Franklin as new every day.
    by_key = {problem_key(one): when for one, when in before.items()}
    now_keys = {problem_key(one) for one in result.problems}
    result.still = {one: by_key[problem_key(one)] for one in result.problems if problem_key(one) in by_key}
    result.fixed = [one for one in before if problem_key(one) not in now_keys]
    if held.get("asked"):
        result.asked = (held["asked"], held.get("asked_at") or "")
    return {one: by_key.get(problem_key(one)) or now for one in result.problems}


def problem_key(problem: str) -> str:
    """A problem as itself, whatever its numbers say today, and whatever order
    the hub listed its states in - Connor Swartz's "has MT, PA, SD, WI, WV"
    came back "has SD, MT, WI, WV, PA" and was asked about again."""
    said = re.sub(r"\d[\d,.:]*", "#", " ".join(str(problem or "").split()))
    return re.sub(r"\b[A-Z]{2}(?:,\s*[A-Z]{2}\b)+", lambda run: ", ".join(sorted(re.findall(r"[A-Z]{2}", run.group(0)))),
                  said)


def among(problem: str, problems) -> bool:
    return problem_key(problem) in {problem_key(one) for one in problems or []}


_STATES_OFF = re.compile(r"^(?P<whose>.*?states don't match the payment)"
                         r"(?: - has (?P<extra>[A-Z]{2}(?:, [A-Z]{2})*) not ordered)?"
                         r"(?: - missing (?P<missing>[A-Z]{2}(?:, [A-Z]{2})*))?$")


def ok_left(problem: str, okd) -> tuple:
    """(what's left of a problem once what was OK'd is taken out, what of it
    was OK'd). Colton Ramon's CT, OK'd by Rook, came back as "has CT not
    ordered - missing GA" - one problem, so the OK didn't count, and CT was
    flagged again. Only the new part is left: "missing GA"."""
    if among(problem, okd):
        return None, problem
    mine = _STATES_OFF.match(" ".join(str(problem or "").split()))
    if not mine or not (mine["extra"] or mine["missing"]):
        return problem, ""
    extra_ok, missing_ok = set(), set()
    for one in okd or []:
        theirs = _STATES_OFF.match(" ".join(str(one or "").split()))
        if theirs and theirs["whose"] == mine["whose"]:
            extra_ok |= set(re.findall(r"[A-Z]{2}", theirs["extra"] or ""))
            missing_ok |= set(re.findall(r"[A-Z]{2}", theirs["missing"] or ""))
    extra = [code for code in re.findall(r"[A-Z]{2}", mine["extra"] or "") if code not in extra_ok]
    missing = [code for code in re.findall(r"[A-Z]{2}", mine["missing"] or "") if code not in missing_ok]
    okd_extra = [code for code in re.findall(r"[A-Z]{2}", mine["extra"] or "") if code in extra_ok]
    okd_missing = [code for code in re.findall(r"[A-Z]{2}", mine["missing"] or "") if code in missing_ok]
    if not (okd_extra or okd_missing):
        return problem, ""
    def put(has, lacks):
        return (mine["whose"] + (f" - has {', '.join(has)} not ordered" if has else "")
                + (f" - missing {', '.join(lacks)}" if lacks else ""))
    return (put(extra, missing) if extra or missing else None), put(okd_extra, okd_missing)


def question(result: Checked, *, link: str = "", invoice: str = "") -> str:
    """What Ryte asks Rook and Nova: the payment as it was paid for, what
    doesn't add up, and what to do - Marco Ayala's was asked as Ryte's own
    QC line, with nothing of what he'd paid for."""
    wanted = getattr(result, "wanted", None)
    paid = []
    if wanted is not None:
        if getattr(wanted, "amount", ""):
            paid.append(str(wanted.amount))
        if invoice:
            paid.append(invoice)
        what = " ".join(str(one) for one in (getattr(wanted, "count", None), getattr(wanted, "lead_type", "")) if one)
        if what:
            paid.append(what)
        if getattr(wanted, "states", None):
            paid.append(", ".join(wanted.states))
    problems = list(result.problems)
    asks = problems + [f"Handed to the team: {one} - who has it?" for one in dict.fromkeys(result.todo)]
    unset = any(one.startswith(("Nothing on the hub for this order", "Not on the hub", "Rook and Nova haven't said",
                                "Not sure which order"))
                for one in problems)
    ask = ("Is this order set up on the hub? If not, please set it up - or say so if you can't." if unset
           else "Can you check these, fix what's wrong on the hub, and confirm?")
    return (f"QC on **{result.client or 'this payment'}**" + (f" - paid {' · '.join(paid)}" if paid else "")
            + (f" ({link})" if link else "") + "\n" + "\n".join(f"• {one}" for one in asks)
            + evidence(result, invoice=invoice) + f"\n{ask}")


def _states_said(states, left_out) -> str:
    if states:
        return f"{', '.join(states)} ({len(states)})"
    if left_out:
        return f"all but {', '.join(left_out)}"
    return "none given"


def evidence(result: Checked, *, invoice: str = "") -> str:
    """What each side says, word for word, under Ryte's question - so a wrong
    read is plain at a glance, and a right one needs no looking up: Justin
    Techer's "has IN not ordered" was right, and nobody could tell."""
    lines = []
    # States only when it's the states that are asked about - Peter Kuyper's
    # lead type question carried all 49 of the hub's, and "states none given".
    about_states = any("state" in one.casefold() for one in [*result.problems, *result.todo])
    paid = getattr(result, "paid", None)
    if paid is not None:
        bits = [one for one in (f"{paid.count} {paid.lead_type}".strip() if paid.count else paid.lead_type,
                                ("states " + _states_said(paid.states, paid.left_out)) if about_states else "")
                if one]
        lines.append(f"-# The payment{f' ({invoice})' if invoice else ''}: " + " · ".join(bits))
    rook = rook_setup(getattr(result, "setup_text", "")) if getattr(result, "setup_text", "") else None
    if rook is not None:
        lines.append(f"-# Rook's setup: {rook.count or '?'} {rook.lead_type or ''}"
                     + (" · states " + _states_said(rook.states, rook.left_out) if about_states else ""))
    hub = getattr(result, "hub", None)
    if hub is not None:
        states = list(getattr(hub, "states", ()) or ())
        lines.append(f"-# The hub: {getattr(hub, 'name', '?')} (row {getattr(hub, 'id', '?')}) · "
                     f"{getattr(hub, 'lead_type', '') or '?'} · {getattr(hub, 'status', '') or '?'} · "
                     f"{getattr(hub, 'ordered', '?')} ordered / {getattr(hub, 'delivered', None) or 0} delivered"
                     + (f" · states {_states_said(states, [])}" if about_states else ""))
    return ("\n" + "\n".join(lines)) if lines else ""


def recalled() -> dict:
    try:
        held = json.loads(PATH.read_text())
    except (OSError, ValueError):
        return {}
    return held if isinstance(held, dict) else {}


def remember(post_id: str, **fields) -> dict:
    held = recalled()
    one = held.setdefault(str(post_id), {})
    one.update(fields, at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(held, indent=1))
    return one


def asked_orders(text: str) -> list:
    """[(name, what they bought)] off a list - "1  Elvis Farina  WEEKEND20
    $1,863.00  50 fresh vets (FL, PA)" - a line each."""
    out = []
    for line in str(text or "").splitlines():
        line = re.sub(r"<@!?\d+>", "", line).strip()
        line = re.sub(r"^\s*\d{1,3}[.)]?\s+", "", line)
        if not line or re.search(r"\bset\s*-?\s*up\b\s*\??\s*$", line, re.IGNORECASE):
            continue
        parts = [one.strip() for one in re.split(r"\t+|\s{2,}", line) if one.strip()]
        if len(parts) < 2:
            # One space between: the name runs to a code, an amount or a count.
            found = re.match(r"(.+?)\s+(?=(?:[A-Z]{3,}\d+\b|\$|\d))", line)
            if not found:
                continue
            parts = [found.group(1), line[found.end():]]
        name = " ".join(re.sub(r"\(.*?\)", "", parts[0]).split())
        if len(name.split()) < 2 or re.search(r"\d|\$", name):
            continue
        rest = " ".join(parts[1:])
        # Promo codes and amounts aren't what was bought: "WEEKEND20 $890.10".
        rest = re.sub(r"\b[A-Z]{3,}\d+\b|\$[\d,]+(?:\.\d{2})?(?:\s*\([^)]*\))?", " ", rest)
        out.append((name, " ".join(rest.split())))
    return out


def setup_answer(name: str, bought: str, rows: list) -> str:
    """One line: whether they're on the hub for what they bought."""
    found = hub_named(rows, name)
    if not found:
        return f"❌ **{name}** — not on the hub" + (f" (bought {bought})" if bought else "")
    many = re.search(r"\b(\d{1,4})\b", bought or "")
    wanted = Order(name=name, lead_type=bought, count=int(many.group(1)) if many else None,
                   states=_states_of(" ".join(re.findall(r"\(([^)]*)\)", bought or "")))[0])
    if "+" in (bought or ""):
        # Several products - Evan Mori's "10 fresh vets + 10 fresh IUL Plus +
        # 10 overflow blue collar": each on a row of its own; overflow isn't
        # on the hub at all.
        wants = [one.strip() for one in bought.split("+") if one.strip()]
        missing = [one for one in wants if "overflow" not in one.casefold() and not any(
            (family(getattr(row, "lead_type", "")) - {"aged"}) == (family(one) - {"aged"}) for row in found)]
        rows_said = "; ".join(f"{getattr(row, 'lead_type', '') or '?'} ({getattr(row, 'status', '') or '?'}, "
                              f"{getattr(row, 'progress', '') or '?'})" for row in found[:4])
        if missing:
            return f"⚠️ **{name}** — on the hub: {rows_said}; not there: {', '.join(missing)}"
        return f"✅ **{name}** — set up: {rows_said}" + (
            " · overflow's the team's" if "overflow" in bought.casefold() else "")
    row = pick_row(found, wanted)
    bits = []
    kind, theirs = family(getattr(row, "lead_type", "")), family(bought)
    if kind and theirs and (kind - {"aged"}) != (theirs - {"aged"}):
        if "overflow" not in (bought or "").casefold() or len(theirs - kind) > 0:
            bits.append(f"lead type {getattr(row, 'lead_type', '?')} (bought {bought})")
    ordered = getattr(row, "ordered", None)
    if wanted.count and ordered is not None and ordered < wanted.count:
        bits.append(f"{ordered} ordered on the hub, bought {wanted.count}")
    missing = [code for code in wanted.states if code not in (getattr(row, "states", ()) or ())]
    if missing and getattr(row, "states", ()):
        bits.append(f"missing {', '.join(missing)}")
    sent = getattr(row, "progress", "") or f"{getattr(row, 'delivered', 0) or 0}/{ordered or '?'}"
    where = (f"{getattr(row, 'lead_type', '') or '?'} · {getattr(row, 'status', '') or '?'} · {sent}"
             + (f" · live {row.start_date:%b %-d}" if getattr(row, "start_date", None) else ""))
    others = len(found) - 1
    tail = f" · {others} other row(s) by that name" if others else ""
    if bits:
        return f"⚠️ **{name}** — on the hub ({where}), but " + "; ".join(bits) + tail
    return f"✅ **{name}** — set up: {where}{tail}"
