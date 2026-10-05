"""Paid but not set up - money taken with nothing on the board for it.

Every Payra payment should become an agent card within a few hours. One that
hasn't - or whose card still says nothing about when they go live - is an
agent waiting on leads nobody is going to send, and that is how a chargeback
starts.

Read only: RYTE's copy of Payra, and the board. Nothing is filed or moved;
it is said, once per payment, for a person to sort out.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .state import _state_dir

#: How long after paying a card is allowed to take to appear. Before this it
#: is not missing, just not made yet.
GRACE = timedelta(hours=4)

#: How far back payments are looked at - "just those who paid 2-3 days
#: ago". Older ones are either sorted by now or already known about.
LOOK_BACK = timedelta(days=3)

#: A card this much older than the payment is an earlier order's, not this
#: one's - a weekly plan renewing, or a reorder. Neither is "no card".
OWN_CARD_BEFORE = timedelta(days=30)


@dataclass
class Paid:
    """One payment that went through, and who made it."""

    invoice_id: str
    number: str
    name: str
    email: str
    phone: str
    amount: str
    paid_at: datetime
    what: str = ""


@dataclass
class Unset:
    """A payment with nothing - or not enough - on the board."""

    paid: Paid
    #: "no card", or "no launch date".
    why: str
    card_url: str = ""
    card_title: str = ""
    notes: list = field(default_factory=list)


def _when(stamp) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(stamp or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def recent_payments(data: dict, *, now: datetime) -> list[Paid]:
    """Invoices paid between three days and four hours ago, newest payment on
    each - refunds and failed charges left out."""
    from . import payraapi

    found = []
    for invoice in (data.get("invoices") or {}).values():
        if invoice.get("active") is False:
            continue
        went = [one for one in payraapi.payments_on(data, invoice) if payraapi._went_through(one)]
        stamps = [stamp for stamp in (_when(one.get("paid_on")) for one in went) if stamp]
        if not stamps:
            continue
        last = max(stamps)
        if not (now - LOOK_BACK <= last <= now - GRACE):
            continue
        total = sum(payraapi._cents(one.get("amount")) or 0 for one in went)
        # What was bought, short: the line's name as far as the first sentence
        # - Payra's lines carry a paragraph of boilerplate after it.
        lines = [_short_line(payraapi.line_text(one)) for one in invoice.get("lines") or []]
        found.append(Paid(
            invoice_id=str(invoice.get("id") or ""), number=str(invoice.get("number") or ""),
            name=str(invoice.get("name") or "").strip(), email=str(invoice.get("email") or "").casefold(),
            phone=str(invoice.get("phone") or ""), amount=f"${total / 100:,.2f}", paid_at=last,
            what="; ".join(one for one in lines if one) or str(invoice.get("description") or ""),
        ))
    found.sort(key=lambda one: one.paid_at)
    return found


def _short_line(said: str, most: int = 60) -> str:
    said = " ".join(str(said or "").split())
    return said if len(said) <= most else said[:most - 1].rstrip() + "…"


def their_cards(paid: Paid, cards: list[dict]) -> list[dict]:
    """The client cards that are this payer's: by email or phone on the card,
    else by their whole name. Never by part of a name - two Marias are two
    people, and the one with a card would hide the one without."""
    from . import agents as rules
    from . import clearout
    from .contracts import card_email
    from .smsreplies import phones_in

    found = []
    person = clearout.person_in(paid.name)
    for card in cards or []:
        title = str(card.get("name") or "")
        if not rules.is_client_card(title):
            continue
        desc = str(card.get("desc") or "")
        if paid.email and card_email(desc) == paid.email:
            found.append(card)
        elif paid.phone and paid.phone in phones_in(desc):
            found.append(card)
        elif person and clearout.tidy(rules.agent_name(title)) == person:
            found.append(card)
    return found


def own_card(paid: Paid, cards: list[dict]) -> dict | None:
    """The newest of their cards that could be this payment's - made no more
    than a month before it. None when every card is an older order's."""
    from . import agents as rules

    made = [(rules.made_at(str(one.get("id") or "")), one) for one in cards]
    made = [(when, one) for when, one in made if when is not None and when >= paid.paid_at - OWN_CARD_BEFORE]
    return max(made, key=lambda pair: pair[0])[1] if made else None


def describe(one: Unset) -> str:
    paid = one.paid
    who = paid.name or paid.email or paid.phone or "?"
    line = (f"• **{who}** — paid {paid.amount} on {paid.paid_at:%b %-d}"
            + (f" (invoice {paid.number})" if paid.number else "")
            + (f" for {paid.what}" if paid.what else ""))
    if one.why == "no card":
        line += " — **no card on the board**"
    else:
        line += f" — **card has no launch date** · [card](<{one.card_url}>)"
    return line + "".join(f"\n  -# {note}" for note in one.notes)


# ---------------------------------------------------------------- said once

SEEN_PATH = _state_dir() / "paid-not-set-up.json"


def said(path: Path | None = None) -> dict:
    """{invoice id + why: when it was said}."""
    try:
        held = json.loads((path or SEEN_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return held if isinstance(held, dict) else {}


def key(one: Unset) -> str:
    return f"{one.paid.invoice_id}|{one.why}"


def remember(keys, path: Path | None = None) -> None:
    where = path or SEEN_PATH
    held = {**said(where), **{str(one): datetime.now(timezone.utc).isoformat() for one in keys}}
    where.parent.mkdir(parents=True, exist_ok=True)
    spare = where.with_suffix(".tmp")
    spare.write_text(json.dumps(held, indent=1), encoding="utf-8")
    spare.replace(where)
