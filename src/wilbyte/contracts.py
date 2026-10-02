"""Which order each signed contract belongs to.

"every order" - a contract is signed for each lead order, not once per
client, so a reorder needs its own and the one from last month does not
count. Every order is paired with a contract of its own: signed no earlier
than CONTRACT_BEFORE_DAYS before the order's card was made, and claimed by
one order only.

Matched strictly, because a contract put against the wrong agent hides the
one who really has none: by the email on the card first, which is exact; by
the name in the contract's title only when no email matches - and that is
reported as needing a look, not as found.

Nothing here reads or writes anything. It is handed what was read.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

#: How long before an order's card was made its contract can have been signed.
CONTRACT_BEFORE_DAYS = 14


@dataclass
class Contract:
    doc_id: str
    title: str
    signed: datetime
    emails: tuple = ()

    @property
    def person(self) -> str:
        """"Basic Contract (1x lead order) x Oliver Lisowski" -> "Oliver Lisowski"."""
        parts = re.split(r"\s+x\s+", self.title or "")
        return parts[-1].strip() if len(parts) > 1 else ""


@dataclass
class Order:
    card_id: str
    name: str
    email: str
    made: datetime
    url: str = ""
    launch: date | None = None
    closed: bool = False


@dataclass
class Paired:
    order: Order
    contract: Contract | None = None
    #: "email", "name" (a name only - needs a look), or "" for none.
    how: str = ""
    notes: list = field(default_factory=list)


def same_name(one: str, other: str) -> bool:
    """Whole names, accents and spacing aside - "Mario Guajardo López" is
    "mario guajardo lopez"."""
    def bare(said: str) -> str:
        flat = unicodedata.normalize("NFKD", said or "")
        flat = "".join(ch for ch in flat if not unicodedata.combining(ch))
        return " ".join(re.sub(r"[^a-z ]+", " ", flat.casefold()).split())

    return bool(bare(one)) and bare(one) == bare(other)


_EMAIL = re.compile(r"\bemail\s*:\s*([\w.+-]+@[\w-]+(?:\.[\w-]+)+)", re.IGNORECASE)


def card_email(desc: str) -> str:
    found = _EMAIL.search(desc or "")
    return found.group(1).casefold() if found else ""


def pair(orders: list[Order], contracts: list[Contract]) -> dict[str, Paired]:
    """Every order with its own contract, or none. {card id: Paired}.

    Oldest order first, each taking the earliest contract still unclaimed in
    its window - so a client's first order keeps the first contract and their
    reorder needs a second one.
    """
    claimed: set[str] = set()
    found: dict[str, Paired] = {}
    for order in sorted(orders, key=lambda one: one.made):
        earliest = order.made - timedelta(days=CONTRACT_BEFORE_DAYS)
        open_ = sorted(
            (one for one in contracts if one.doc_id not in claimed and one.signed >= earliest),
            key=lambda one: one.signed,
        )
        by_email = [
            one for one in open_
            if order.email and order.email in {e.casefold() for e in one.emails}
        ]
        by_name = [one for one in open_ if same_name(one.person, order.name)]
        if by_email:
            claimed.add(by_email[0].doc_id)
            found[order.card_id] = Paired(order, by_email[0], "email")
        elif by_name:
            claimed.add(by_name[0].doc_id)
            seen = ", ".join(by_name[0].emails) or "no email"
            found[order.card_id] = Paired(order, by_name[0], "name", [
                f"a contract for {by_name[0].person} was sent to {seen}"
                + (f", not {order.email}" if order.email else "")
            ])
        else:
            found[order.card_id] = Paired(order)
    return found
