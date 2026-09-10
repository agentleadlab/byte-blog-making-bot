"""A chargeback rebuttal, from the dispute facts and what the board remembers.

A customer signs a contract, gets their leads, works them for weeks, and then
disputes the charge. Answering it means assembling the same five things every
time - the signed contract, the paid invoice, the onboarding and delivery,
the lead sheet with the client's own notes on it, and anything they said
afterwards - and most of that is already in systems RYTE reads all day.

So: Franklin pastes the dispute facts and attaches what only he has, and the
rest is gathered. Nothing here invents evidence. Every line in the finished
document traces to a message, a row, a card or a file, and a proof with
nothing behind it is left out rather than written around.

Everything in this module is pure. Reading Discord and the sheet lives in
`bot.jobs`; building the file lives in `bot.rebuttaldoc`. This decides what
the document says and what shape it is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

# The facts off the dispute notice, by what the label sounds like rather than
# by its exact wording: every acquirer names these differently and the block
# gets pasted from whatever portal it came out of.
FIELDS = (
    ("mid", r"\bmid\b|merchant\s*id"),
    ("dba", r"\bdba\b|doing\s+business"),
    ("dispute_date", r"dispute\s*date|chargeback\s*date|case\s*date"),
    ("dispute_type", r"dispute\s*type|reason\s*(?:code)?\b"),
    ("amount", r"dollar\s*amount|dispute\s*amount|\bamount\b"),
    ("arn", r"\barn\b|acquirer.?s?\s*reference"),
    ("card", r"card\s*(?:number|no)|\bcard\b"),
    ("transaction_date", r"transaction\s*date|sale\s*date|purchase\s*date"),
    ("customer_name", r"customer\s*name|cardholder(?:\s*name)?|client\s*name"),
    ("customer_email", r"customer\s*email|cardholder\s*email|\bemail\b"),
)

_MONEY = re.compile(r"\$?\s*([\d,]+(?:\.\d{1,2})?)")
_DATE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b")
_LINE = re.compile(r"^\s*(?P<label>[^:]{2,60}?)\s*:\s*(?P<value>.+?)\s*$")


@dataclass
class Dispute:
    """What the acquirer sent, as fields rather than a block of text."""

    mid: str = ""
    dba: str = "AGENT LEAD LAB"
    dispute_date: str = ""
    dispute_type: str = ""
    amount: str = ""
    arn: str = ""
    card: str = ""
    transaction_date: str = ""
    customer_name: str = ""
    customer_email: str = ""

    def missing(self) -> list[str]:
        """The fields a rebuttal cannot be written without."""
        needed = {
            "customer_name": "Customer Name",
            "amount": "Dispute Dollar Amount",
            "transaction_date": "Transaction Date",
        }
        return [said for name, said in needed.items() if not getattr(self, name)]

    def paid(self) -> date | None:
        return as_date(self.transaction_date)

    def disputed(self) -> date | None:
        return as_date(self.dispute_date)

    def days_waited(self) -> int | None:
        """How long the cardholder waited before disputing.

        The number that makes the argument: somebody who received nothing does
        not wait three months to say so, and does not spend those months
        working the thing they say never arrived.
        """
        paid, disputed = self.paid(), self.disputed()
        if paid is None or disputed is None:
            return None
        return (disputed - paid).days


def as_date(said: str) -> date | None:
    """A date out of "6/15/2026", "06/15/26" or "2026-06-15"."""
    text = str(said or "").strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    found = _DATE.search(text)
    if not found:
        return None
    month, day, year = (int(part) for part in found.groups())
    if year < 100:
        year += 2000
    try:
        return date(year, month, day)
    except ValueError:
        return None


def money(said: str) -> str:
    """"$1,552.50" from whatever was pasted, or "" if there was no number."""
    found = _MONEY.search(str(said or ""))
    if not found:
        return ""
    try:
        return f"${float(found.group(1).replace(',', '')):,.2f}"
    except ValueError:
        return ""


def read_facts(text: str) -> Dispute:
    """The dispute block, parsed. Unknown labels are ignored rather than
    guessed at - a field RYTE invented is worse than one left blank.
    """
    found = Dispute()
    seen = set()
    for line in (text or "").splitlines():
        matched = _LINE.match(line)
        if not matched:
            continue
        label = matched.group("label").strip().casefold()
        value = matched.group("value").strip()
        if not value:
            continue
        for name, pattern in FIELDS:
            if name in seen or not re.search(pattern, label, re.IGNORECASE):
                continue
            setattr(found, name, money(value) if name == "amount" else value)
            seen.add(name)
            break
    return found


def named_in(text: str) -> str:
    """The customer's name off the command line - `rebuttal Jose Zambrano`."""
    said = re.sub(
        r"^\s*(?:trello\s+)?(?:rebuttal|chargeback|dispute)\b", "",
        (text or "").splitlines()[0] if text else "", count=1, flags=re.IGNORECASE,
    )
    said = said.split(":")[0] if ":" not in said[:3] else said
    return " ".join(said.split()[:4]).strip(" -–—,")


# ------------------------------------------------------------------ exhibits

# What a file attached to the command turns out to be. RYTE works it out by
# looking rather than by asking Franklin to label six screenshots, and files
# each under the proof it belongs to.
EXHIBITS = {
    "contract": "the signed agreement, or a page of it",
    "invoice": "an invoice or receipt",
    "texts": "a text-message or SMS conversation",
    "discord": "a Discord conversation or channel",
    "sheet": "a spreadsheet of leads, or a lead record",
    "sale": "a posted sale, commission or win",
    "other": "anything else",
}

# Which proof each kind of exhibit sits under.
UNDER = {
    "contract": 1, "invoice": 2, "discord": 3, "sheet": 4, "sale": 5, "texts": 6,
}


@dataclass
class Exhibit:
    """One file Franklin attached, and what it turned out to be."""

    name: str
    data: bytes = b""
    kind: str = "other"
    caption: str = ""
    text: str = ""

    def is_image(self) -> bool:
        return bool(re.search(r"\.(png|jpe?g|gif|webp)$", self.name, re.IGNORECASE))

    def is_pdf(self) -> bool:
        return self.name.lower().endswith(".pdf")


# -------------------------------------------------------------- the document

PROOFS = (
    (1, "Signed Contract, Fully Executed and Initialed"),
    (2, "Paid Invoice"),
    (3, "Onboarded and Leads Delivered On Schedule, Confirmed in Writing"),
    (4, "Full Lead Sheet Delivered and Personally Worked"),
    (5, "Confirmed Activity From the Delivered Leads"),
    (6, "The Cardholder's Own Words"),
)


@dataclass
class Gathered:
    """What was found, per proof. Empty means the proof is left out."""

    contract: str = ""
    invoice: str = ""
    delivery: str = ""
    sheet: str = ""
    activity: str = ""
    texts: str = ""
    timeline: list = field(default_factory=list)
    holes: list = field(default_factory=list)


def header(one: Dispute) -> list[tuple[str, str]]:
    """The fact table at the top, in the order Skip Scott's rebuttal has it."""
    rows = [
        ("Cardholder", f"{one.customer_name}"
         + (f"  |  {one.customer_email}" if one.customer_email else "")),
        ("Transaction Date", one.transaction_date),
        ("Dispute Date", one.dispute_date),
        ("Dispute Dollar Amount", one.amount),
        ("Dispute Type", one.dispute_type),
        ("Acquirer Reference Number (ARN)", one.arn),
        ("Card Number", one.card),
    ]
    return [(label, value) for label, value in rows if str(value).strip()]


def waited_line(one: Dispute) -> str:
    """The sentence about the gap, or "" when the dates do not support one."""
    days = one.days_waited()
    if days is None or days < 14:
        return ""
    return (
        f"The cardholder waited **{days} days** between the transaction "
        f"({one.transaction_date}) and this dispute ({one.dispute_date}) — a "
        "period during which, as the evidence below shows, they remained in "
        "their dedicated support channel and worked the leads they now say "
        "they did not receive."
    )


def writing_prompt(one: Dispute, found: Gathered, exhibits: list) -> str:
    """What to ask Claude to write, given only what was actually gathered."""
    have = [
        (name, text) for name, text in (
            ("contract", found.contract), ("invoice", found.invoice),
            ("delivery", found.delivery), ("sheet", found.sheet),
            ("activity", found.activity), ("texts", found.texts),
        ) if str(text).strip()
    ]
    seen = "\n\n".join(f"### {name}\n{text}" for name, text in have)
    files = "\n".join(f"- {one.name}: {one.kind}" for one in exhibits) or "- none"
    return (
        "You are writing a chargeback rebuttal for Agent Lead Lab, a "
        "lead-generation company, to be submitted to the card acquirer.\n\n"
        f"THE DISPUTE\nCardholder: {one.customer_name} ({one.customer_email})\n"
        f"Amount: {one.amount}\nTransaction: {one.transaction_date}\n"
        f"Disputed: {one.dispute_date}"
        + (f"\nDays waited: {one.days_waited()}" if one.days_waited() else "")
        + f"\n\nEVIDENCE GATHERED\n{seen}\n\nFILES ATTACHED\n{files}\n\n"
        "Write two things.\n\n"
        "1. SUMMARY — one paragraph, 4-6 sentences, stating what the customer "
        "bought, that it was delivered, what they did with it, and that the "
        "dispute has no factual basis. Concrete and dated. No adjectives you "
        "cannot support.\n\n"
        "2. For each piece of evidence above, a short section of 2-4 "
        "sentences that states what it proves, quoting the actual messages and "
        "notes with their timestamps. Label each one with the evidence name it "
        "came from, exactly, on its own line.\n\n"
        "Rules. Cite only what is in the evidence — never infer a fact, a date "
        "or a sum that is not written there. If a piece of evidence is thin, "
        "say less rather than padding it. Do not write a section for evidence "
        "that was not gathered. Quote the customer's own words verbatim where "
        "they exist, because their own words are the strongest thing here."
    )


def demand(one: Dispute) -> str:
    """The closing paragraph. The amount is the only thing that varies."""
    return (
        "Based on the documentary evidence above, Agent Lead Lab demands the "
        f"immediate reversal of this chargeback and the full restoration of "
        f"{one.amount or 'the disputed amount'} to our merchant account "
        "without further delay."
    )


def what_is_missing(found: Gathered, exhibits: list) -> list[str]:
    """What a person still has to supply, said plainly at the top of the file.

    A gap named is a gap somebody fills. A gap written around is one that
    reaches the acquirer.
    """
    kinds = {one.kind for one in exhibits}
    holes = list(found.holes)
    if "contract" not in kinds and not found.contract:
        holes.append(
            "The signed contract. Download the completed PDF from PandaDoc and "
            "attach it — it carries the signing date, the reference and the "
            "no-chargeback clause."
        )
    if "invoice" not in kinds and not found.invoice:
        holes.append("The paid invoice.")
    if not found.texts:
        holes.append(
            "Any texts or messages from the cardholder after the dispute. "
            "Paste them into the command and they go in the addendum."
        )
    return holes
