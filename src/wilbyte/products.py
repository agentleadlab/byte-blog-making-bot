"""What Agent Lead Lab sells, as the words a client sees on a payment page.

The descriptions are the team's own, written once and reused: a client who
gets two links a month should see the same sentence about the same package
both times. They live here rather than being typed each time, because typed
each time is how "Text Verified" and "Text-Verified" and "TV" end up on three
invoices for one product.

Pure. Making the price and the link happens in `stripe.py`; this only decides
which package was asked for and what it is called.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Spelled the way the team spells them. Title-casing the lot would give
# "Otp Iul Leads", which reads as though nobody checked.
ACRONYMS = {
    "otp": "OTP", "iul": "IUL", "iuls": "IULs", "fex": "FEX", "mtg": "MTG",
    "vet": "VET", "vets": "VETS", "lp": "LP", "tfr": "TFR", "phnx": "PHNX",
    "phx": "PHX", "siul": "SIUL", "fb": "FB", "ai": "AI",
}

# Words that are never capitalised mid-phrase.
_SMALL = {"a", "an", "and", "for", "of", "the", "to", "with"}


def titled(text: str) -> str:
    """"40 basic spanish leads" -> "40 Basic Spanish Leads".

    Whatever case it was typed in. The client reads this, so it should look
    written rather than transcribed - and the acronyms keep theirs.
    """
    words = " ".join((text or "").split()).split(" ")
    said: list[str] = []
    for place, word in enumerate(words):
        # A word can carry punctuation - "text-verified" is two to capitalise.
        pieces = re.split(r"([-/])", word)
        fixed = []
        for piece in pieces:
            low = piece.lower()
            if piece in "-/":
                fixed.append(piece)
            elif low in ACRONYMS:
                fixed.append(ACRONYMS[low])
            elif low in _SMALL and place and not fixed:
                fixed.append(low)
            elif piece.isdigit() or not piece:
                fixed.append(piece)
            else:
                fixed.append(piece[:1].upper() + piece[1:].lower())
        said.append("".join(fixed))
    return " ".join(said)


@dataclass(frozen=True)
class Product:
    """One package: what it is called, what it says, and what people call it."""

    name: str
    description: str
    # The words somebody might type instead of the name. Matching is on words,
    # so these are phrases rather than spellings - "basic" and "instant" are
    # the same package said two ways.
    aliases: tuple[str, ...] = ()


# The catalogue, as Franklin wrote it. The description is what appears on the
# payment page, so it is kept word for word - including the ones that read a
# little long, because that is the team's wording and not RYTE's to tidy.
CATALOGUE = (
    Product(
        "Spanish Text-Verified IUL Leads",
        "This package focuses on generating high-intent life insurance leads. "
        "These are Text-Verified Spanish Indexed Universal Life leads. These are "
        "Middle-class Americans (20-80) exploring tax-free retirement options "
        "leveraging life insurance (Whole Life + IUL).",
        ("spanish text verified iul", "text verified spanish iul", "spanish tv iul"),
    ),
    Product(
        "Facebook IUL Leads",
        "This package focuses on generating high-intent life insurance leads. "
        "These are instant IUL leads that focus on Middle-class Americans "
        "exploring tax-free retirement options leveraging life insurance "
        "(Whole Life + IUL).",
        ("fb iul", "facebook iul", "instant iul", "basic iul"),
    ),
    Product(
        "Text-Verified Trucker IUL Leads",
        "This package focuses on generating high-intent trucker IUL insurance "
        "leads. These are Premium truckers looking for tax free retirement options",
        ("trucker iul", "trucker", "otp trucker iul"),
    ),
    Product(
        "Text-Verified Mortgage Protection Leads",
        "This package focuses on generating high-intent life insurance leads. "
        "These are Premium Text Verified Mortgage protection leads Homeowners "
        "seeking Mortgage Protection coverage.",
        ("mortgage protection", "mtg", "mortage protection"),
    ),
    Product(
        "Text-Verified Blue Collar Leads",
        "This package focuses on generating high-intent life insurance leads. "
        "These are blue-collar tradespeople looking for Coverage for themselves.",
        ("blue collar", "blue collar iul", "bc"),
    ),
    Product(
        "Text-Verified IUL Leads",
        "This package focuses on generating high-intent life insurance leads. "
        "These are Text-Verified Indexed Universal Life leads. These are "
        "Middle-class Americans exploring tax-free retirement options leveraging "
        "life insurance (Whole Life + IUL).",
        ("text verified iul", "tv iul", "otp iul"),
    ),
    Product(
        "Text-Verified Veteran Leads",
        "This package focuses on generating high-intent life insurance leads. "
        "These are Premium Veteran leads. Former & active veterans (20–85) "
        "looking for better rates than VA coverage.",
        ("veteran", "vet", "vets", "text verified vet", "otp vet"),
    ),
    Product(
        "Text-Verified Final Expense Leads",
        "This package focuses on generating high-intent life insurance leads. "
        "These are Premium Final Expense leads. Seniors 40–85 shopping for Final "
        "Expense Life Insurance (non-state regulated).",
        ("final expense", "fex", "otp fex"),
    ),
    Product(
        "Text-Verified Widow Leads",
        "This package focuses on generating high-intent life insurance leads. "
        "These are Premium Veteran Widow leads. Former & active veterans "
        "spouses/widows (20–85) looking for better rates than VA coverage.",
        ("widow", "widows", "vet widow", "veteran widow", "otp widow"),
    ),
    Product(
        "Spanish Instant IUL Leads",
        "Leads looking for Tax - Free Wealth strategies using IUL. These leads "
        "will be 40-60 middle class americans that speak spanish.",
        ("spanish instant iul", "basic spanish", "basic spanish iul",
         "instant spanish iul", "spanish basic"),
    ),
    Product(
        "Text-Verified Aged IUL Leads",
        "This payment is for a list of aged leads provided by Agent Lead Lab. "
        "The leads may include people interested in Indexed Universal Life (IUL). "
        "These leads have shown past interest and are included as part of a lead "
        "package.",
        ("aged iul", "aged iul leads"),
    ),
    Product(
        "Text-Verified Aged Veteran Leads",
        "This payment is for a list of aged leads provided by Agent Lead Lab. "
        "The leads may include people interested in Veteran life insurance "
        "Protection. These leads have shown past interest and are included as "
        "part of a lead package.",
        ("aged veteran", "aged vet"),
    ),
    Product(
        "Text-Verified Aged Final Expense Leads",
        "This payment is for a list of aged leads provided by Agent Lead Lab. "
        "The leads may include people interested in final expense life insurance "
        "Protection. These leads have shown past interest and are included as "
        "part of a lead package.",
        ("aged final expense", "aged fex"),
    ),
    Product(
        "Text-Verified Aged Mortgage Protection Leads",
        "This payment is for a list of aged leads provided by Agent Lead Lab. "
        "The leads may include people interested in Mortgage Protection. These "
        "leads have shown past interest and are included as part of a lead "
        "package.",
        ("aged mortgage protection", "aged mtg"),
    ),
    Product(
        "OTP Vet Standard",
        "This package focuses on generating high-intent life insurance leads. "
        "These are Premium Veteran leads. Former & active veterans (20–85) "
        "looking for better rates than VA coverage.",
        ("otp vet standard", "vet standard", "standard vet"),
    ),
)

# Words that say nothing about which package it is.
_NOISE = re.compile(
    r"\b(?:leads?|lead|package|list|of|for|please|the|a|an|new|premium)\b",
    re.IGNORECASE,
)

# "$621 for 40 basic Spanish Leads" - the money and the quantity, in either
# order and with or without the dollar sign.
_MONEY = re.compile(r"\$\s*([\d,]+(?:\.\d{1,2})?)")
_HOW_MANY = re.compile(r"\b(\d{1,5})\s*(?=[a-z])", re.IGNORECASE)


def amount_asked(said: str) -> float | None:
    """The dollar amount, in dollars. None when nobody said one."""
    found = _MONEY.search(said or "")
    if not found:
        return None
    try:
        return float(found.group(1).replace(",", ""))
    except ValueError:
        return None


def how_many(said: str) -> int | None:
    """How many leads. The number that isn't the money."""
    text = _MONEY.sub(" ", said or "")
    found = _HOW_MANY.search(text)
    return int(found.group(1)) if found else None


def _words(said: str) -> set[str]:
    text = re.sub(r"[^A-Za-z0-9]+", " ", said or "")
    text = _NOISE.sub(" ", text)
    return {word.lower() for word in text.split() if word and not word.isdigit()}


def matches(said: str) -> list[Product]:
    """Every package the words could mean, best first.

    Scored on how much of a package's name or alias the message actually says.
    An exact alias wins outright; otherwise the most words shared, and a tie is
    left as a tie for somebody to settle.
    """
    asked = _words(_MONEY.sub(" ", said or ""))
    if not asked:
        return []

    scored: list[tuple[int, int, Product]] = []
    for product in CATALOGUE:
        best = 0
        for phrase in (product.name, *product.aliases):
            wanted = _words(phrase)
            if not wanted:
                continue
            shared = len(wanted & asked)
            if shared != len(wanted):
                # Every word of the alias has to be there. "spanish iul" is not
                # "iul", and filing one as the other is a wrong invoice.
                shared = 0 if len(wanted) > 1 else shared
            if shared > best:
                best = shared
        if best:
            scored.append((best, -len(_words(product.name)), product))

    scored.sort(key=lambda held: (-held[0], held[1]))
    return [product for _score, _size, product in scored]


def find(said: str) -> Product | None:
    """The one package meant, or None when it is nought or more than one."""
    found = matches(said)
    if not found:
        return None
    if len(found) > 1:
        first = _score_of(found[0], said)
        if first == _score_of(found[1], said):
            return None
    return found[0]


def _score_of(product: Product, said: str) -> int:
    asked = _words(_MONEY.sub(" ", said or ""))
    return max(
        (len(_words(phrase) & asked) for phrase in (product.name, *product.aliases)),
        default=0,
    )


def line_for(said: str, product: Product) -> str:
    """The line a client sees: "40 Spanish Instant IUL Leads"."""
    count = how_many(said)
    return f"{count} {product.name}" if count else product.name
