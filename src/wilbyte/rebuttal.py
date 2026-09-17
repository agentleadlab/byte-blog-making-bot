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
    ("dispute_type", r"dispute\s*type"),
    ("reason", r"reason\b|dispute\s*reason|reason\s*code|^\s*code\b"),
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

# The notification arrives as a Discord embed with its labels in bold, so the
# line is "**ARN:** 2455640616780894270" - the label match stops at the first
# colon and the closing asterisks stay on the front of the value. Every field
# came out as "** Jose Zambrano".
_EMPHASIS = re.compile(r"\*\*|__|^\s*[*_]+|[*_]+\s*$", re.MULTILINE)


def _unbolded(line: str) -> str:
    """One line with its markdown emphasis taken off."""
    return _EMPHASIS.sub("", line or "").strip()


@dataclass
class Dispute:
    """What the acquirer sent, as fields rather than a block of text."""

    mid: str = ""
    dba: str = "AGENT LEAD LAB"
    dispute_date: str = ""
    dispute_type: str = ""
    reason: str = ""
    amount: str = ""
    arn: str = ""
    card: str = ""
    transaction_date: str = ""
    customer_name: str = ""
    customer_email: str = ""
    #: The block as it was pasted. Kept because the reason code does not
    #: reliably arrive on the line labelled "Reason" - Jose Zambrano's notice
    #: said "Reason: Not as Described" and put "code: 13.3" underneath it, and
    #: the first line claimed the field and the second was dropped.
    raw: str = ""

    @property
    def code(self) -> str:
        """The reason code, off whichever line happened to carry it."""
        return code_in(f"{self.reason}\n{self.dispute_type}\n{self.raw}")

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


#: What a reason code actually asserts, and what answers it.
#:
#: The code is the whole shape of the document. Juliana Hernandez's was 37 -
#: the cardholder saying she never authorised the payment at all - and the
#: answer to that is who paid: the AVS match on her own home address, the
#: order she confirmed by text from the number on her invoice, the payment
#: link she clicked herself. Jose Zambrano's was "not as described", where
#: none of that matters and the answer is what was promised and delivered:
#: the tier he chose in writing, the clauses of the agreement he signed, the
#: leads landing in his own CRM, and him working them for months afterwards.
#:
#: Written out here because a rebuttal aimed at the wrong question is a
#: rebuttal that loses while being entirely true.
CODES = {
    "37": (
        "No Cardholder Authorization",
        "that the cardholder never authorised this payment at all - that "
        "somebody else used the card",
        "proof of WHO paid. The gateway's own authorisation record: whether "
        "the payment was customer-initiated, the AVS result on the billing "
        "address, the billing name captured at payment, the issuer's "
        "approval. Then everything tying the payment to the cardholder "
        "personally - an order they confirmed from their own phone number, "
        "an invoice sent to their own email and opened, a payment link they "
        "clicked, delivery to their own address. Quality, description and "
        "usefulness are not the question and arguing them concedes the point",
    ),
    "10.4": (
        "Other Fraud - Card Absent Environment",
        "that the cardholder never authorised this payment at all",
        "the same as code 37: proof of who paid rather than what was sold",
    ),
    "4837": (
        "No Cardholder Authorization",
        "that the cardholder never authorised this payment at all",
        "the same as code 37: proof of who paid rather than what was sold",
    ),
    "13.3": (
        "Not as Described or Defective Merchandise/Services",
        "that what arrived was not what was described, or did not work",
        "proof of WHAT was promised and WHAT arrived. What the cardholder was "
        "shown before buying and what they chose in their own words; the "
        "terms of any signed agreement, quoted by section, especially any "
        "defining what counts as delivered and any saying results are not "
        "guaranteed; what was actually delivered, field by field, against "
        "what was promised; and every sign the cardholder used it afterwards "
        "- a lead worked through a pipeline is not a defective one",
    ),
    "13.1": (
        "Merchandise/Services Not Received",
        "that nothing ever arrived",
        "proof of delivery and of its date: where it was sent, to which "
        "address, when, and anything showing the cardholder received or "
        "opened it. The cardholder's own acknowledgement afterwards is "
        "worth more than any internal record",
    ),
    "13.6": (
        "Credit Not Processed",
        "that a refund was promised or owed and never given",
        "the terms that say what is refundable and what is not, and what was "
        "actually said to the cardholder about a refund",
    ),
    "13.7": (
        "Cancelled Merchandise/Services",
        "that the order was cancelled and charged anyway",
        "when the cancellation was said to have happened, what the terms say "
        "about cancelling, and what had already been delivered by then",
    ),
    "4853": (
        "Cardholder Dispute",
        "that what arrived was not what was described",
        "the same as 13.3: what was promised, what arrived, and what the "
        "cardholder did with it",
    ),
}

# Not part of a longer number. "Dispute Amount: $ 129.37" ends in the digits
# of reason code 37, and reading it as one would aim the entire document at
# the wrong question while looking perfectly correct.
_CODE = re.compile(r"(?<![.\d])(\d{1,2}\.\d{1,2}|\d{2,4})(?![.\d])")

# The line the code is actually on, when there is one.
_CODE_LINE = re.compile(r"^.*\b(?:code|reason)\b.*$", re.IGNORECASE | re.MULTILINE)


def code_in(text: str) -> str:
    """The reason code out of whatever was pasted, or "".

    "Code: 37 - No Cardholder Authorization", "13.3" and a bare "37" all come
    out as the key.

    The line that says "code" or "reason" is read first, because a dispute
    notice is full of numbers that are not reason codes - an ARN, a card's
    last four, an amount, three dates. Only codes this knows about come back:
    guessing one is worse than not having it, since the document is built
    around whichever question it is told to answer.
    """
    said = str(text or "")
    for where in ("\n".join(_CODE_LINE.findall(said)), said):
        for found in _CODE.finditer(where):
            if found.group(1) in CODES:
                return found.group(1)
    return ""


def what_the_code_means(code: str) -> tuple[str, str, str] | None:
    """(its name, what it asserts, what answers it) or None if unknown."""
    return CODES.get(" ".join(str(code or "").split()))


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


# Where the pasted dispute block stops and the payment record starts. The two
# arrive in one message and are read differently: the first is labelled fields,
# the second is whatever the portal happened to lay out.
PAYMENT_MARKER = re.compile(
    r"^[ \t]*(?:payment|gateway|authoriz(?:ation|ed)|authoris(?:ation|ed)|"
    r"transaction\s+details?|invoice\s+log|portal)\b[ \t]*:?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


def split_payment(text: str) -> tuple[str, str]:
    """(the dispute block, the payment record) out of one pasted message.

    Everything after a line that says PAYMENT: - or GATEWAY:, or TRANSACTION
    DETAILS: - is the payment portal's own record and is not parsed as fields.
    It is laid out however the portal laid it out, and trying to read labels
    out of it would turn "AVS Response  Y" into a customer email.

    No marker means the whole thing is the dispute block, which is how every
    rebuttal before this one worked.
    """
    said = str(text or "")
    found = PAYMENT_MARKER.search(said)
    if not found:
        return said, ""
    return said[: found.start()].rstrip(), said[found.end():].strip()


def read_facts(text: str) -> Dispute:
    """The dispute block, parsed. Unknown labels are ignored rather than
    guessed at - a field RYTE invented is worse than one left blank.
    """
    found = Dispute()
    seen = set()
    for line in (text or "").splitlines():
        matched = _LINE.match(_unbolded(line))
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
    found.raw = str(text or "")
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
    "payment": (
        "a payment gateway or invoice portal record - AVS result, "
        "authorisation response, billing name and address, whether the "
        "payment was customer-initiated, or an invoice's event log showing "
        "it sent, opened, clicked and paid"
    ),
    "texts": "a text-message or SMS conversation",
    "discord": "a Discord conversation or channel",
    "sheet": "a spreadsheet of leads, or a lead record",
    "sale": "a posted sale, commission or win",
    "other": "anything else",
}

# Which proof each kind of exhibit sits under.
UNDER = {
    "contract": 1, "invoice": 2, "payment": 3, "discord": 4, "sheet": 5,
    "sale": 6, "texts": 7,
}


@dataclass
class Exhibit:
    """One file Franklin attached, and what it turned out to be."""

    name: str
    data: bytes = b""
    kind: str = "other"
    caption: str = ""
    text: str = ""
    #: "A", "B"... assigned once they are grouped, so the argument can cite
    #: them and the reader can find them at the back.
    letter: str = ""
    #: Which of its kind it is: "screenshot 3 of 6".
    number: int = 0
    of: int = 0
    #: What it actually says, in English. The WhatsApp is in Spanish and the
    #: person reading this at the acquirer will not be.
    transcript: str = ""

    def label(self) -> str:
        if self.of > 1:
            return f"Exhibit {self.letter} — screenshot {self.number} of {self.of}"
        return f"Exhibit {self.letter}"

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
    #: The day the sheet was handed over, "YYYY-MM-DD", off the comment that
    #: carried the link. The timeline's missing middle without it.
    delivered_on: str = ""
    #: The signed contract as PandaDoc emailed it, so it can go in as an
    #: exhibit rather than being described. The clause is in the document.
    contract_pdf: bytes = b""
    contract_name: str = ""
    #: An aged-leads re-order rather than a new agent's setup. "aged leads we
    #: dont have contracts, only fresh/new agents" - so asking for one is
    #: asking for a document that was never signed, and a rebuttal that goes
    #: out with a red line demanding it reads as though something is missing
    #: when nothing is.
    aged: bool = False
    #: The gateway's own record, pasted from the payment portal. AVS, whether
    #: the payment was customer-initiated, the billing name and address it
    #: captured, the invoice's event log. None of it is in the receipt email
    #: and none of it is reachable by any API we have - and against a
    #: no-authorisation code it is the whole argument.
    payment: str = ""
    #: What they bought, for the tracker's own column.
    product: str = ""
    #: What the rebuttal file was called, so the row points at it.
    rebuttal_name: str = ""
    timeline: list = field(default_factory=list)
    holes: list = field(default_factory=list)


def spelled(said: str) -> str:
    """"June 15, 2026" from "6/15/2026". A slashed date in a legal document
    reads as a form somebody filled in; a written one reads as a statement."""
    when = as_date(said)
    return f"{when:%B %-d, %Y}" if when else str(said or "")


def header(one: Dispute) -> list[tuple[str, str]]:
    """The fact table at the top."""
    rows = [
        ("Merchant (DBA)", one.dba),
        ("MID", one.mid),
        ("Acquirer Reference Number", one.arn),
        ("Cardholder", f"{one.customer_name}"
         + (f" ({one.customer_email})" if one.customer_email else "")),
        ("Card", one.card),
        ("Transaction Date", spelled(one.transaction_date)),
        ("Amount", one.amount),
        ("Dispute Date", spelled(one.dispute_date)),
        ("Reason", one.reason or one.dispute_type),
    ]
    return [(label, value) for label, value in rows if str(value).strip()]


#: What a conversation screenshot is of, so a set of them can be put back in
#: the order it happened. WhatsApp hands them over newest first, and a
#: conversation read backwards is one nobody follows.
_WHEN = re.compile(
    r"\b(\d{4})-(\d{2})-(\d{2})\b"
    r"|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b",
    re.IGNORECASE,
)
_MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()


def happened_on(transcript: str) -> tuple:
    """The earliest date a transcript mentions, for sorting. () when none."""
    found = _WHEN.search(transcript or "")
    if not found:
        return ()
    if found.group(1):
        return (int(found.group(1)), int(found.group(2)), int(found.group(3)))
    month = _MONTHS.index(found.group(4)[:3].casefold()) + 1
    return (0, month, int(found.group(5)))


def letter_them(exhibits: list) -> list:
    """Group the attachments by kind and letter each group A, B, C...

    So the argument can cite them and the reader can find them at the back.
    In the order they are argued rather than the order they were attached:
    the messages before the purchase come before the contract, which comes
    before the invoice, which comes before what was delivered.
    """
    order = (
        "texts", "discord", "contract", "invoice", "payment", "sheet", "sale",
        "other",
    )
    lettered = []
    letter = ord("A")
    for kind in order:
        group = [one for one in exhibits if one.kind == kind]
        if not group:
            continue
        # Oldest first. WhatsApp hands its screenshots over newest first, so
        # the conversation arrived backwards: the first exhibit was the last
        # thing said. One with no date it could read keeps its place.
        if kind in ("texts", "discord"):
            dated = [(happened_on(one.transcript), n, one) for n, one in enumerate(group)]
            group = [one for when, _n, one in sorted(dated, key=lambda x: (not x[0], x[0], x[1]))]
        for number, one in enumerate(group, start=1):
            one.letter = chr(letter)
            one.number = number
            one.of = len(group)
            lettered.append(one)
        letter += 1
    return lettered


def exhibit_groups(exhibits: list) -> list[tuple[str, str, list]]:
    """(letter, what the group is, its files), in the order they are lettered."""
    named = {
        "texts": "WhatsApp / Text Conversation with the Cardholder",
        "discord": "Support Channel",
        "contract": "Signed Service Agreement",
        "invoice": "Invoice and Payment",
        "payment": "Payment Authorization Record",
        "sheet": "Lead Delivery Records",
        "sale": "Sale Posted by the Cardholder",
        "other": "Further Supporting Material",
    }
    groups: dict[str, list] = {}
    for one in exhibits:
        if one.letter:
            groups.setdefault(one.letter, []).append(one)
    return [
        (letter, named.get(group[0].kind, "Supporting Material"), group)
        for letter, group in sorted(groups.items())
    ]


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
    """What to ask Claude to write, given only what was actually gathered.

    Numbered arguments rather than a fixed list of proofs. A rebuttal is read
    by somebody deciding whether the service was as described, so it has to
    argue that - "the service was clearly described before purchase" - rather
    than list the documents and leave the reader to join them up.
    """
    have = [
        (name, text) for name, text in (
            ("the signed agreement", found.contract),
            ("the order and invoice", found.invoice),
            ("onboarding and delivery, in writing", found.delivery),
            ("the delivered lead sheet", found.sheet),
            ("what the cardholder did with the leads", found.activity),
            ("the cardholder's own messages", found.texts),
        ) if str(text).strip()
    ]
    seen = "\n\n".join(f"### {name}\n{text}" for name, text in have)

    said = []
    for letter, what, group in exhibit_groups(exhibits):
        lines = [f"EXHIBIT {letter} — {what}"]
        for shown in group:
            if shown.transcript:
                lines.append(f"  {shown.label()}:\n{shown.transcript}")
            elif shown.caption:
                lines.append(f"  {shown.label()}: {shown.caption}")
        said.append("\n".join(lines))
    files = "\n\n".join(said) or "(nothing attached)"

    return (
        "You are writing a chargeback rebuttal for Agent Lead Lab, a "
        "lead-generation company, to be submitted to the card acquirer. It "
        "will be read by somebody deciding whether the service was delivered "
        "as described.\n\n"
        f"THE DISPUTE\nCardholder: {one.customer_name} ({one.customer_email})\n"
        f"Amount: {one.amount}\nTransaction: {spelled(one.transaction_date)}\n"
        f"Disputed: {spelled(one.dispute_date)}\n"
        f"Reason given: {one.reason or one.dispute_type or 'not stated'}"
        + (f"\nDays waited: {one.days_waited()}" if one.days_waited() else "")
        + aimed_at(one)
        + (
            "\n\nTHE GATEWAY'S OWN RECORD OF THE PAYMENT\nThis came out of the "
            "payment portal. Quote its fields exactly as they are written and "
            "say what each one means - an AVS match is a match on the "
            "cardholder's own billing address, and customer-initiated means "
            "the cardholder started the payment rather than the merchant "
            "keying it in.\n" + found.payment
            if found.payment else ""
        )
        + f"\n\nWHAT OUR RECORDS SHOW\n{seen}\n\nEXHIBITS ATTACHED\n{files}\n\n"
        "Write:\n\n"
        "SUMMARY:\nTwo or three paragraphs. What was bought, what was "
        "delivered, what the cardholder did with it, and why the dispute has "
        "no basis. Concrete and dated.\n\n"
        "Then between three and six arguments. Write each one as:\n\n"
        "ARGUMENT: The service was clearly described before purchase "
        "(Exhibit A)\n"
        "Then two to five sentences making it, quoting the messages, the notes "
        "and the terms with their dates.\n\n"
        "The line after ARGUMENT: is a heading - a short phrase naming the "
        "argument, at most twelve words, with the exhibits it rests on in "
        "brackets, and no full stop at the end. Do not number them; they are numbered when the document "
        "is set.\n\n"
        "Then KEY MESSAGES: and, one per line, the handful of messages that "
        "decide this - at most eight - as `when | who | what they said`. Use "
        "'Cardholder' and 'Agent Lead Lab' as the sender. Quote the words, "
        "translated, and keep each under twenty-five words. Leave this out "
        "entirely if there are no messages in the exhibits.\n\n"
        "Then CONCLUSION: and one paragraph.\n\n"
        "Rules. Cite only what is above - never a fact, date or sum that is "
        "not written there. Cite exhibits by letter, and only ones that "
        "exist. Where the money needs explaining, show the arithmetic. Quote "
        "the cardholder's own words wherever they exist, because their own "
        "words are the strongest thing here. Say less rather than padding. "
        "Write it as the merchant: 'we', 'our records'. No markdown - no "
        "asterisks, no hashes. The only labels are SUMMARY:, ARGUMENT: and "
        "CONCLUSION:, each on a line of its own."
    )


def aimed_at(one: Dispute) -> str:
    """What this reason code asserts and what answers it, for the prompt.

    The difference between a rebuttal that wins and one that is merely true.
    Code 37 says the cardholder never authorised the payment; answering it
    with how good the leads were concedes the point without arguing it. "Not
    as described" says the opposite - that they did pay, and got the wrong
    thing - where the authorisation data is beside the point and the terms of
    the agreement are everything.

    Nothing at all when the code is unknown, which is honest: the document
    then argues the whole record rather than being aimed by a guess.
    """
    found = what_the_code_means(one.code)
    if found is None:
        return (
            "\n\nNO REASON CODE WAS GIVEN. Argue the whole record - what was "
            "bought, that the cardholder bought it, that it was delivered as "
            "described, and what they did with it afterwards - rather than "
            "aiming at one question."
        )
    name, asserts, answered = found
    return (
        f"\n\nTHE REASON CODE IS {one.code} - {name}.\n"
        f"This code asserts {asserts}.\n"
        f"What answers it is {answered}.\n"
        "Aim every argument at that question. An argument that answers a "
        "different question, however true, reads to the issuer as not having "
        "answered this one."
    )


#: What each column of the tracker wants, by what its heading sounds like.
#: The sheet is somebody's and its columns are in their order with their
#: wording, so the row is built against the headings that are actually there
#: rather than against a shape assumed here.
TRACKS = (
    ("logged", r"date\s*(?:logged|added|filed|entered)|^date$|log\s*date"),
    ("customer_name", r"customer|client|cardholder|\bname\b|agent"),
    ("customer_email", r"e-?mail"),
    ("amount", r"amount|disputed|\bsum\b|\btotal\b|\$"),
    ("arn", r"\barn\b|acquirer|reference"),
    ("card", r"card\s*(?:number|no|ending)|last\s*4|\bcard\b"),
    ("transaction_date", r"transaction|sale\s*date|charge\s*date|paid"),
    ("dispute_date", r"dispute\s*date|chargeback\s*date|case\s*date|received"),
    ("code", r"\bcode\b|reason"),
    ("product", r"product|package|lead\s*type|what\s*(?:they|was)"),
    ("status", r"status|stage|progress"),
    ("rebuttal", r"rebuttal|response|document|evidence|submitted"),
)


#: What a new dispute's Status says. "new dispute is always pending".
#:
#: The tracker's Status column is a dropdown - Win, Pending, and whatever the
#: third one is - so this is not a label to be invented. A value outside the
#: list fails the sheet's own validation, and a row that reads "Rebuttal
#: drafted" where every other row reads one of three words is a row nobody's
#: filter counts.
NEW_DISPUTE = "Pending"


#: How the tracker's tabs are named - "Aug 2026", "Sept 2026", "Oct 2026".
#: One tab a month, so a chargeback belongs in the month it was logged and
#: writing every one into whichever tab happens to be first would pile the
#: year into August.
_MONTH_TAB = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\b"
    r"[^0-9]{0,4}(\d{2,4})?",
    re.IGNORECASE,
)

_MONTHS_SHORT = (
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sep", "oct", "nov", "dec",
)


def looks_monthly(title: str) -> bool:
    """Whether this tab is named for a month at all.

    The question "is this sheet kept by month" is not the same as "is this
    September", and answering the first with a January date says no to every
    tab in the year.
    """
    found = _MONTH_TAB.search(str(title or ""))
    return bool(found) and found.group(1).casefold()[:3] in _MONTHS_SHORT


def monthly_tab(titles, when: date) -> str:
    """The tab for this month, out of a sheet that has one per month, or "".

    "Sept 2026" and "Sep 2026" and "September 2026" are the same month written
    three ways, so the first three letters decide it and the year has to agree
    when the tab says one.

    "" when nothing matches, which is the honest answer: a chargeback written
    into the wrong month is worse than one nobody wrote down, because the
    second gets noticed.
    """
    wanted = _MONTHS_SHORT[when.month - 1]
    loose = ""
    for title in titles or []:
        found = _MONTH_TAB.search(str(title or ""))
        if not found or found.group(1).casefold()[:3] != wanted:
            continue
        said = found.group(2)
        if not said:
            loose = loose or str(title)
            continue
        year = int(said) + (2000 if len(said) == 2 else 0)
        if year == when.year:
            return str(title)
    return loose


def row_for_tracker(headings, one: Dispute, found: "Gathered", *, when,
                    status: str = NEW_DISPUTE) -> list[str]:
    """One row, laid out to match the tracker's own columns.

    A heading this does not recognise gets an empty cell. Guessing which
    column an unknown heading wants is how an outcome column - the one filled
    in weeks later when the bank decides - gets written over on the day the
    dispute lands.
    """
    code = one.code
    named = what_the_code_means(code)
    have = {
        "logged": f"{when:%Y-%m-%d}",
        "customer_name": one.customer_name,
        "customer_email": one.customer_email,
        "amount": one.amount,
        "arn": one.arn,
        "card": one.card,
        "transaction_date": spelled(one.transaction_date),
        "dispute_date": spelled(one.dispute_date),
        "code": f"{code} — {named[0]}" if named else (code or one.reason),
        "product": found.product,
        "status": status,
        "rebuttal": found.rebuttal_name,
    }
    row = []
    for heading in headings:
        said = " ".join(str(heading or "").split()).casefold()
        row.append(next(
            (have.get(name, "") for name, pattern in TRACKS
             if said and re.search(pattern, said, re.IGNORECASE)),
            "",
        ))
    return row


def describe_row(headings, row) -> str:
    """The row as a person would check it, heading by heading."""
    lines = []
    for heading, cell in zip(headings, row):
        said = " ".join(str(heading or "").split())
        lines.append(f"• **{said or '(unnamed)'}** — {cell or '_(blank)_'}")
    return "\n".join(lines)


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
    if "contract" not in kinds and not found.contract and not found.aged:
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
