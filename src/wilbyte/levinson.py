"""The monthly report for the Levinson agency: who they sent us, and who paid.

Levinson market to their agents on a landing page we run. Every month they ask
which of those agents actually bought leads, and until now the answer was
somebody reading two spreadsheets against each other.

Two questions, and keeping them apart is the whole design:

*Who is a Levinson agent* is a membership question. GoHighLevel tags a contact
`levison leads` the moment they come through that page, and the tag never
expires. The opt-in sheet is the same list kept by hand.

*Who paid, and when* is an event question, and the only honest source for it is
a payment. It is tempting to read the `onboarded` tag instead - it is right
there, and it means somebody became a customer. But a tag is a state: it says
this happened once, never how many times or in which month. An agent who
bought in May and buys again in September carries the same tag on both days,
and a report built on it counts the first order and silently loses every one
after - which for an agency partner is precisely the money they are owed for.
"An agent who purchase no matter how many times or how long ago they opted in,
as long as they purchase, they get track."

So membership comes from the tag, and the money comes from Payra's payment
notifications, one message per payment, with a date on it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

# What a Payra notification calls itself. Every payment lands in the same
# channel whoever it came from - "all payments are notified the same thing so
# there's no identifying if its levinson agent unless you know their contact
# information" - so this only says "this is a payment", and who it belongs to
# is settled afterwards against the member list.
PAYMENT_TITLE = re.compile(r"\bnew\s+payment\b", re.IGNORECASE)

# NAME: Tony Moderno / EMAIL: tony.moderno@tryeverlife.com / AMOUNT: $1863
# Written as one block with bold labels, so each field runs to the next label
# or the end rather than to the end of the line - a product name that wrapped
# would otherwise arrive cut in half.
_LABELS = ("name", "email", "phone", "amount", "product")
_FIELD = re.compile(
    r"\*{0,2}(" + "|".join(_LABELS) + r")\*{0,2}\s*:\s*(.*?)"
    r"(?=\*{0,2}(?:" + "|".join(_LABELS) + r")\*{0,2}\s*:|\Z)",
    re.IGNORECASE | re.DOTALL,
)

_MONEY = re.compile(r"-?\$?\s*([\d,]+(?:\.\d{1,2})?)")


@dataclass(frozen=True)
class Payment:
    """One payment, as Payra announced it."""

    name: str
    email: str
    phone: str
    cents: int
    product: str
    paid_at: datetime

    @property
    def day(self) -> date:
        return self.paid_at.date()

    @property
    def dollars(self) -> str:
        return f"${self.cents / 100:,.2f}"


def is_payment(text: str) -> bool:
    return bool(PAYMENT_TITLE.search(text or ""))


def fields_in(text: str) -> dict[str, str]:
    """The labelled values out of a payment notification."""
    found = {}
    for label, value in _FIELD.findall(text or ""):
        # Trailing markdown and the blank line before the next label.
        found[label.lower()] = value.strip().strip("*").strip()
    return found


def as_cents(amount: str) -> int | None:
    """"$1,086.75" -> 108675. None when there is no number in it at all.

    Cents rather than a float because these get added up and sent to a partner
    as what they are owed, and 0.1 + 0.2 is not 0.3.
    """
    found = _MONEY.search(amount or "")
    if not found:
        return None
    whole, _, part = found.group(1).replace(",", "").partition(".")
    return int(whole) * 100 + int((part + "00")[:2])


def read_payment(text: str, *, paid_at: datetime) -> Payment | None:
    """One notification, read. None when it isn't one, or has no money in it.

    The date is the message's own timestamp rather than anything in the text:
    Payra doesn't put one in, and the message arrived when the payment did.
    """
    if not is_payment(text):
        return None
    said = fields_in(text)
    cents = as_cents(said.get("amount", ""))
    if cents is None:
        return None
    return Payment(
        name=said.get("name", ""),
        email=said.get("email", "").lower(),
        phone=digits(said.get("phone", "")),
        cents=cents,
        # Ryder Hamlin's came through with PRODUCT blank. That is a payment
        # with a gap in it, not a payment that didn't happen.
        product=said.get("product", ""),
        paid_at=paid_at,
    )


def digits(phone: str) -> str:
    """Just the numbers, so +1 (801) 637-1314 and 18016371314 are one phone."""
    kept = re.sub(r"\D", "", phone or "")
    # US numbers arrive both ways. Ten digits is the part that identifies
    # somebody; the leading 1 is a habit.
    return kept[1:] if len(kept) == 11 and kept.startswith("1") else kept


@dataclass(frozen=True)
class Member:
    """One agent Levinson sent us, however we came to know about them."""

    name: str
    email: str
    phone: str = ""
    source: str = ""


def member_key(email: str) -> str:
    return (email or "").strip().lower()


def roll_up(members: list[Member]) -> tuple[dict[str, Member], dict[str, Member]]:
    """(by email, by phone). Two ways in, because Payra and GHL disagree.

    Somebody who signed up with a personal address and paid on the company
    card is the same agent, and the phone is what says so.
    """
    by_email, by_phone = {}, {}
    for one in members:
        key = member_key(one.email)
        if key:
            by_email.setdefault(key, one)
        if one.phone:
            by_phone.setdefault(digits(one.phone), one)
    return by_email, by_phone


def whose(payment: Payment, by_email: dict, by_phone: dict) -> Member | None:
    """The member who made this payment, or None if it wasn't one of theirs.

    Email first, phone second, and nothing else. Matching on names would find
    a second Antonio Norton and hand an agency money for somebody they never
    sent us, and a report to a partner is the wrong place to be approximately
    right.
    """
    found = by_email.get(member_key(payment.email))
    if found is not None:
        return found
    return by_phone.get(payment.phone) if payment.phone else None


def in_month(payments: list[Payment], year: int, month: int) -> list[Payment]:
    return [
        one for one in payments
        if one.paid_at.year == year and one.paid_at.month == month
    ]


@dataclass
class Line:
    """One payment by one Levinson agent, ready to go on the sheet."""

    paid_on: date
    name: str
    email: str
    phone: str
    amount: str
    product: str
    matched_by: str

    def as_row(self) -> list[str]:
        return [
            f"{self.paid_on:%m/%d/%Y}", self.name, self.email, self.phone,
            self.amount, self.product, self.matched_by,
        ]


HEADERS = ["Date", "Name", "Email", "Phone", "Amount", "Product", "Matched by"]


def lines_for(payments: list[Payment], members: list[Member]) -> list[Line]:
    """Every payment that belongs to a Levinson agent, oldest first.

    One line per payment, not per agent: three orders in a month is three
    lines, because that is three times they spent money.
    """
    by_email, by_phone = roll_up(members)
    found = []
    for one in sorted(payments, key=lambda p: p.paid_at):
        member = whose(one, by_email, by_phone)
        if member is None:
            continue
        matched = "email" if by_email.get(member_key(one.email)) else "phone"
        found.append(
            Line(
                paid_on=one.day,
                # The name Payra has is the name on the card; the member list
                # has the name they signed up with. Levinson know the second
                # one, so that is the one on the report.
                name=member.name or one.name,
                email=one.email or member.email,
                phone=one.phone or digits(member.phone),
                amount=one.dollars,
                product=one.product,
                matched_by=matched,
            )
        )
    return found


def total(lines: list[Line]) -> str:
    cents = sum(as_cents(line.amount) or 0 for line in lines)
    return f"${cents / 100:,.2f}"


MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)


def month_named(said: str, *, today: date) -> tuple[int, int] | None:
    """(year, month) out of "levinson august", "levinson 08/2026", "levinson".

    A month named without a year means the most recent one that has already
    happened: asked in January for December, the answer is last year's.
    """
    text = " ".join((said or "").split()).lower()
    if not text or "this month" in text:
        return today.year, today.month
    if "last month" in text:
        return (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)

    numbered = re.search(r"\b(\d{1,2})[/-](\d{4})\b", text)
    if numbered and 1 <= int(numbered.group(1)) <= 12:
        return int(numbered.group(2)), int(numbered.group(1))

    for number, name in enumerate(MONTHS, start=1):
        # "aug" and "august" both land; "augment" doesn't.
        typed = re.search(rf"\b({name[:3]}\w*)\b", text)
        if not typed or not name.startswith(typed.group(1)):
            continue
        year = re.search(r"\b(20\d{2})\b", text)
        if year:
            return int(year.group(1)), number
        return (today.year if number <= today.month else today.year - 1), number
    return None


def tab_for(year: int, month: int) -> str:
    """"September 2026" - one tab per month, the way it gets sent on."""
    return f"{MONTHS[month - 1].title()} {year}"
