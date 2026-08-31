"""The fresh-lead masterlists, as a summary of what is in each one.

The aged leads have a reference file - one row per masterfile, how many leads
are in it, a link to it. The fresh leads have no such thing, because they do
not live in one place: each lead type is a Discord channel, every lead lands
there as a message, and every message carries a link to that type's sheet.

So the summary is a walk: read the sheet link out of each channel, count the
rows in that sheet, write the row. Nothing here reads an individual lead - the
sheet already has them, and the question is only how many.

Discord's own categories are the grouping, because they are the grouping the
team already maintains: MTG Masterlist, IUL Masterlist, VET Masterlist, FEX
Masterlist. One of them is INACTIVE, and those go on a tab of their own so the
live list stays a list of what is live.

Pure. The Discord walk and the Sheets calls happen elsewhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# The link at the bottom of every lead post: "Access Sheet Here".
SHEET_LINK = re.compile(
    r"https://docs\.google\.com/spreadsheets/d/[A-Za-z0-9_-]{20,}[^\s>)\]]*"
)

# Words that are part of a channel's name and not part of a lead type's.
_NOT_A_TYPE = re.compile(r"\bmaster\s*lists?\b|\bmasterfiles?\b|\bleads?\b", re.IGNORECASE)

# Spelled the way the team spells them, not title-cased into "Otp Iul".
ACRONYMS = {
    "otp": "OTP", "iul": "IUL", "iuls": "IULs", "fex": "FEX", "mtg": "MTG",
    "vet": "VET", "vets": "VETS", "lp": "LP", "ai": "AI", "bc": "BC",
    "siul": "SIUL", "fb": "FB", "phnx": "PHNX", "phx": "PHX", "tfr": "TFR",
    "no": "No",
}

# The category whose channels are not live any more.
INACTIVE = re.compile(r"\binactive\b", re.IGNORECASE)

ACTIVE_TAB = "Active"
INACTIVE_TAB = "Inactive"


@dataclass
class Masterlist:
    """One lead type: where it sits, what it is called, and how many it holds."""

    category: str
    name: str
    sheet: str = ""
    count: int | None = None
    # Why there is no count, when there isn't one. Silence reads as nought.
    problem: str = ""

    @property
    def inactive(self) -> bool:
        return bool(INACTIVE.search(self.category))


def sheet_in(text: str) -> str:
    """The first Google Sheet link in a message, or "" when there is none."""
    found = SHEET_LINK.search(text or "")
    return found.group(0).rstrip(".,;)>") if found else ""


def sheet_in_message(content: str, embeds: list[dict] | None = None) -> str:
    """The sheet link in a message, wherever the posting app put it.

    LeadLab posts each lead as an embed, so the link is in the embed's
    description or one of its fields rather than in the message text. Both are
    looked at, because another app's format is not a promise.
    """
    found = sheet_in(content or "")
    if found:
        return found

    for embed in embeds or []:
        parts = [
            str(embed.get("description") or ""),
            str(embed.get("title") or ""),
            str(embed.get("url") or ""),
            str((embed.get("footer") or {}).get("text") or ""),
        ]
        parts += [
            f"{field.get('name', '')} {field.get('value', '')}"
            for field in embed.get("fields") or []
        ]
        found = sheet_in("\n".join(parts))
        if found:
            return found
    return ""


def tidy_name(raw: str) -> str:
    """"🚚 otp-trucker-iul-masterlist" -> "OTP Trucker IUL".

    The emoji and the dashes are Discord's; the acronyms are the team's, and
    title-casing them into "Otp Iul" would make the summary read as though a
    machine wrote it about a product it had never heard of.
    """
    text = re.sub(r"[^A-Za-z0-9]+", " ", raw or "")
    text = _NOT_A_TYPE.sub(" ", text)
    words = [word for word in text.split() if word]
    if not words:
        return " ".join((raw or "").split()) or "(unnamed)"
    return " ".join(ACRONYMS.get(word.lower(), word.capitalize()) for word in words)


HEADER = ("Category", "Type of leads", "Sheet", "Total leads")


def summary_rows(found: list[Masterlist]) -> list[list]:
    """The four columns, header included, ready to write.

    A count that couldn't be read is written as a dash and not a nought.
    Nought is a number somebody will act on.
    """
    rows: list[list] = [list(HEADER)]
    for held in found:
        rows.append([
            tidy_name(held.category),
            held.name,
            held.sheet,
            "—" if held.count is None else held.count,
        ])
    return rows


def split_by_state(found: list[Masterlist]) -> tuple[list[Masterlist], list[Masterlist]]:
    """(live, inactive). Inactive goes on its own tab, by its category."""
    return (
        [held for held in found if not held.inactive],
        [held for held in found if held.inactive],
    )


def total(found: list[Masterlist]) -> int:
    """Every lead across every masterlist, ignoring the ones that failed."""
    return sum(held.count for held in found if isinstance(held.count, int))


def describe(live: list[Masterlist], idle: list[Masterlist]) -> str:
    """What was written, as something to read before opening the sheet."""
    lines = [
        f"**{len(live)}** live masterlist(s), **{total(live):,}** leads.",
    ]
    if idle:
        lines.append(f"**{len(idle)}** inactive, **{total(idle):,}** leads, on their own tab.")

    stuck = [held for held in (live + idle) if held.count is None]
    if stuck:
        lines.append("")
        lines += [f"⚠ {held.name} — {held.problem or 'no sheet link in that channel'}" for held in stuck]
    return "\n".join(lines)
