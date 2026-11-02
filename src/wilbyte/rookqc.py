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
from datetime import datetime, timezone

from .ordercards import US_STATES, _codes, same_person
from .state import _state_dir

#: Each payment's QC as last said, by the payment post's id.
PATH = _state_dir() / "rook-qc.json"

#: A labelled line. Never the next line's: Max Wilson's blank "Email:" was
#: read as "Phone: +1561...", his blank "States:" as the line under it.
_FIELD = r"^[ \t>*•-]*{label}[ \t]*:\**[ \t]*(?P<value>[^\n]*)$"
_FAMILIES = (("vet", r"\bvet|veteran"), ("fex", r"\bfex\b|final\s*expense"), ("iul", r"\biul\b"),
             ("mtg", r"\bmtg\b|mortgage"), ("widow", r"widow"), ("trucker", r"truck"),
             ("blue collar", r"blue\s*collar"), ("spanish", r"spanish|espa[nñ]ol"))
_AGED = re.compile(r"\baged\b|\b\d{2,3}\s*(?:\+|[-–]\s*\d{2,3})\s*(?:days?)?\b", re.IGNORECASE)
_HANDED = re.compile(r"^\s*(?:>\s*)?handed\s+to\s+the\s+team\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
#: Rook or Nova saying the setup is theirs no more: "I can't set this up",
#: "unable to set her up", "needs a manual setup".
CANT = re.compile(
    r"\b(?:can(?:'|’)?t|cannot|can\s+not|could(?:n(?:'|’)?t| not)|unable\s+to|won(?:'|’)?t\s+be\s+able\s+to)\s+"
    r"(?:fully\s+)?(?:set\s+(?:this|it|him|her|them|the\s+\w+)?\s*up|do\s+(?:this|the)\s+setup|create\s+(?:this|the)\s+"
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

    def talk_about_setup(self) -> bool:
        """Whether Rook and Nova talked about setting it up at all."""
        return self.picked_up

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
    if _AGED.search(said):
        names.add("aged")
    return frozenset(names)


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
    if lines:
        order.lead_type = " + ".join(" ".join(one[0].split()) for one in lines)
        order.count = sum(int(one[1].replace(",", "")) for one in lines)
        states = [code for one in lines for code in _named(one[2] or "")]
        order.states = list(dict.fromkeys(states))
    else:
        order.lead_type = _field(notes, r"Lead\s*Type(?:\s*Ordered)?", "Package") or _field(text, "PRODUCT")
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
    if not re.search(r"\bsetup\b", said, re.IGNORECASE) or not re.search(r"^\W*name\s*:", said,
                                                                       re.IGNORECASE | re.MULTILINE):
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
    states, left_out = _states_of(_field(said, r"States?", "Estados"))
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
    return len(words) >= 2 and all(re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", said.casefold())
                                   for word in (words[0], words[-1]))


def check(post: str, talk: list[tuple[str, str]], *, hub=None, hub_problem: str = "", sheet_rows: int | None = None,
          sheet_problem: str = "", hub_rows: list | None = None) -> Checked:
    """The payment held up against Rook's setup, Nova's replies, the hub and
    the sheet. `talk`: [(who, what they said)] in #rook-nova, oldest first.
    `hub`: the hub's record for them, or None."""
    paid = payment(post)
    result = Checked(client=paid.name)
    rook = next((found for who, said in talk for found in [rook_setup(said)] if found is not None), None)
    if rook is not None and rook.name:
        # A business paying - "Kunz Marketing, Llc" for Kurtis Kunz.
        result.client = rook.name
    nova = [said for who, said in talk if who.casefold().startswith("nova")]
    result.picked_up = bool(talk)
    fulfilled = _fulfilled(talk)
    if OVERFLOW.search(str(post or "")):
        # "Overflow Trucker - $25/lead": the team delivers it from overflow -
        # not Rook and Nova's, not on the hub. Karli Kelley's was held to her
        # older trucker row, Max Murphy's flagged "Not on the hub".
        result.picked_up = True
        result.notes.append("Overflow order - delivered by the team, not set up on the hub")
        _delivered(result, fulfilled, paid, overflow=True)
        if not fulfilled:
            result.problems.append("Overflow order, and nothing says it's delivered yet (no \"This order has been "
                                   "fulfilled\")")
        return result
    if not talk:
        result.problems.append("Rook and Nova haven't said anything about this payment in #rook-nova")
        return result
    if rook is None:
        result.notes.append("No \"New setup\" message from Rook to check field by field")
    else:
        _compare(result, "Rook's setup", rook, paid)
    for said in nova:
        for line in _HANDED.findall(said):
            result.todo.append(" ".join(line.split()))
    # On the hub and going, or delivered to: set up, whatever was said on
    # the way - Ibrahim Shalash's "I couldn't set up Ibrahim yet", live with 7
    # delivered.
    going = hub is not None and (str(getattr(hub, "status", "") or "").casefold() in
                                 ("live", "active", "done", "fulfilled", "complete", "completed")
                                 or (getattr(hub, "delivered", None) or 0) > 0)
    for who, said in talk:
        found = CANT.search(said)
        if not found:
            continue
        if going or re.match(r"[^.\n]{0,40}\byet\b", said[found.end():], re.IGNORECASE):
            result.notes.append(f"{who} said earlier it couldn't set it up" + (" yet" if not going else "")
                                + (f" - the hub has them {hub.status} now" if going else ""))
            continue
        result.cant = f"{who}: " + " ".join(found.group(0).split())
        result.problems.append(f"{who} said they can't set it up - “{_quote(said, CANT)}”")
    if not nova:
        result.problems.append("Nova hasn't answered Rook yet - nothing says it's set up")
    # What the hub is held to: the payment, and Rook's setup for whatever
    # the payment doesn't say - a Zelle post carries no states or count.
    wanted = _filled(paid, rook)
    if hub_rows and len(hub_rows) > 1:
        # Several rows for them - Jay Rodriguez's Jay_Rodriguez and
        # Jay_Rodriguez_2: the one this payment fits, not the newest.
        hub = pick_row(hub_rows, wanted)
        result.notes.append(f"{len(hub_rows)} hub rows for them - checked {getattr(hub, 'name', '?')}, the one that "
                            "fits this payment best")
    elif hub_rows:
        hub = hub_rows[0]
    if any(getattr(wanted, key) != getattr(paid, key) for key in ("email", "lead_type", "count", "states", "left_out")):
        result.notes.append("The payment doesn't say everything (a Zelle post, or no invoice notes) - the hub is "
                            "checked against Rook's setup where it doesn't")
    if hub is None:
        result.problems.append(f"Not on the hub{f' ({hub_problem})' if hub_problem else ''}")
    else:
        _hub(result, hub, wanted)
    _delivered(result, fulfilled, paid)
    # Rook's setup for more than this payment - Ryan Gsell's 30 paid in two
    # payments of 15, "BOTH payments are in #new-payment".
    said_all = "\n".join(said for _who, said in talk)
    several = len(set(re.findall(r"\bINV-?\d+\b", said_all, re.IGNORECASE))) >= 2 or re.search(
        r"\b(?:both|two|2)\s+payments\b", said_all, re.IGNORECASE)
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
        for one in [one for one in result.problems if one.startswith("Rook's setup has no states")]:
            result.problems.remove(one)
            result.notes.append(one + " - the hub has the right ones")
    # Live or delivering: what was handed over before the setup is done
    # with - Matthew Stewart's "I can't find a PAID payment" under a tick.
    if going and result.todo:
        result.notes += [f"Handed to the team earlier: {one}" for one in dict.fromkeys(result.todo)]
        result.todo = []
    if sheet_problem:
        result.problems.append(f"The sheet: {sheet_problem}")
    elif sheet_rows is not None:
        delivered = getattr(hub, "delivered", None) if hub is not None else None
        if delivered and sheet_rows < delivered:
            # Carson Rentz: 100 delivered on the hub, "0 row(s)" on the sheet.
            result.problems.append(f"The hub says {delivered} delivered, but the sheet has {sheet_rows} lead row(s)")
        else:
            result.ok.append(f"The sheet opens - {sheet_rows} lead row(s) on it")
    return result


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
    found = pattern.search(said)
    start = max(0, found.start() - 60) if found else 0
    return " ".join(said[start:start + 160].split())


def _compare(result: Checked, whose: str, theirs: Order, paid: Order) -> None:
    if paid.name and theirs.name and not same_person(paid.name, theirs.name):
        result.problems.append(f"{whose} has the name {theirs.name}, the payment {paid.name}")
    if paid.email and theirs.email:
        if paid.email.casefold() != theirs.email.casefold():
            result.problems.append(f"{whose} has the email {theirs.email}, the payment {paid.email}")
        else:
            result.ok.append(f"{whose}: email matches")
    elif paid.email and not theirs.email:
        result.problems.append(f"{whose} has no email (the payment's is {paid.email})")
    if paid.phone and theirs.phone and _digits(paid.phone) != _digits(theirs.phone):
        result.problems.append(f"{whose} has the phone {theirs.phone}, the payment {paid.phone}")
    if paid.count is not None and theirs.count is not None:
        if paid.count != theirs.count:
            result.problems.append(f"{whose} has {theirs.count} leads, the payment {paid.count}")
        else:
            result.ok.append(f"{whose}: {theirs.count} leads, as paid for")
    elif paid.count is not None:
        result.problems.append(f"{whose} gives no lead count (paid for {paid.count})")
    paid_kind, their_kind = family(paid.lead_type), family(theirs.lead_type)
    if paid_kind and their_kind and (paid_kind - {"aged"}) != (their_kind - {"aged"}):
        result.problems.append(f"{whose} has the lead type “{theirs.lead_type}”, the payment “{paid.lead_type}”")
    elif paid_kind and their_kind and ("aged" in paid_kind) != ("aged" in their_kind):
        result.problems.append(f"{whose} reads {'aged' if 'aged' in their_kind else 'fresh'}, the payment "
                               f"{'aged' if 'aged' in paid_kind else 'fresh'} (“{paid.lead_type}”)")
    elif paid_kind and their_kind:
        result.ok.append(f"{whose}: lead type matches")
    _compare_states(result, whose, theirs.states, theirs.left_out, paid)


def _compare_states(result: Checked, whose: str, states, left_out, paid: Order) -> None:
    if paid.left_out or left_out:
        if sorted(paid.left_out) != sorted(left_out) or states:
            result.problems.append(f"{whose} has the states "
                                   + (", ".join(states) if states else f"all but {', '.join(left_out) or '?'}")
                                   + f"; the payment all but {', '.join(paid.left_out) or '?'}"
                                   if paid.left_out else
                                   f"{whose} has all but {', '.join(left_out)}; the payment {', '.join(paid.states)}")
        else:
            result.ok.append(f"{whose}: states match")
        return
    if paid.states and states:
        extra = [code for code in states if code not in paid.states]
        missing = [code for code in paid.states if code not in states]
        if extra or missing:
            result.problems.append(f"{whose} states don't match the payment"
                                   + (f" - has {', '.join(extra)} not ordered" if extra else "")
                                   + (f" - missing {', '.join(missing)}" if missing else ""))
        else:
            result.ok.append(f"{whose}: all {len(states)} states match")
    elif paid.states and not states:
        result.problems.append(f"{whose} has no states (the payment: {', '.join(paid.states)})")


def pick_row(rows: list, wanted: Order):
    """The hub row an order fits best: its states, its lead type, its count -
    the newest when they fit alike."""
    from datetime import datetime as _dt, timezone as _tz

    def misfit(row) -> tuple:
        states = set(getattr(row, "states", ()) or ())
        off = len(states ^ set(wanted.states)) if wanted.states and states else 0
        kind = family(getattr(row, "lead_type", ""))
        wrong_kind = 1 if kind and family(wanted.lead_type) and (kind - {"aged"}) != (family(wanted.lead_type) - {"aged"}) else 0
        short = 1 if wanted.count and getattr(row, "ordered", None) is not None and row.ordered < wanted.count else 0
        newest = getattr(row, "updated_at", None) or _dt.min.replace(tzinfo=_tz.utc)
        return (wrong_kind, off, short, -newest.timestamp() if newest.year > 1 else 0)

    return min(rows, key=misfit)


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


def said(result: Checked, *, link: str = "", amount: str = "") -> str:
    """The QC as one message for Ryte The Goat."""
    head = f"🔎 **Setup QC — {result.client or 'a payment'}**" + (f" (${amount})" if amount else "")
    if result.problems:
        lines = [head + " — ⚠ needs a look:", *(f"• {one}" for one in result.problems)]
    else:
        lines = [head + " — ✅ Rook, Nova and the hub match the payment"]
    if result.todo:
        lines.append("**Handed to the team:**")
        lines += [f"• {one}" for one in dict.fromkeys(result.todo)]
    lines += [f"-# ✓ {one}" for one in result.ok]
    lines += [f"-# {one}" for one in result.notes]
    if link:
        lines.append(f"-# <{link}>")
    return "\n".join(lines)


def question(result: Checked, *, link: str = "") -> str:
    """What Ryte asks Rook and Nova when the button's pressed."""
    asks = list(result.problems) + [f"Handed to the team: {one} - who has it?" for one in dict.fromkeys(result.todo)]
    return (f"QC on **{result.client or 'this payment'}**" + (f" ({link})" if link else "") + " - can you check "
            "these and confirm?\n" + "\n".join(f"• {one}" for one in asks))


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
