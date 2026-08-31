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
            f'=HYPERLINK("{held.sheet}","Open sheet")' if held.sheet else "—",
            "—" if held.count is None else held.count,
        ])
    return rows


def split_by_state(
    found: list[Masterlist], *, said: dict[str, str] | None = None
) -> tuple[list[Masterlist], list[Masterlist]]:
    """(live, inactive). The Discord category decides unless somebody said.

    "@RYTE move the trucker masterlist to inactive" is an override kept by
    RYTE, because it only has read permission in that server - a bot that can
    restructure somebody else's Discord is a bot that can restructure the
    wrong part of it.
    """
    from . import leadstate

    held_states = said if said is not None else leadstate.load()
    live, idle = [], []
    for held in found:
        state = leadstate.state_of(held.name, held=held_states)
        away = held.inactive if state is None else state == leadstate.INACTIVE
        (idle if away else live).append(held)
    return live, idle


def total(found: list[Masterlist]) -> int:
    """Every lead across every masterlist, ignoring the ones that failed."""
    return sum(held.count for held in found if isinstance(held.count, int))


NO_PROBLEM_GIVEN = "no sheet link in that channel"

# The same refusal, said forty times, is forty times harder to read than the
# forty names under it. One paragraph per cause, one list of who it hit.
MOST_NAMES_LISTED = 12


def trouble(found: list[Masterlist]) -> list[tuple[str, list[str]]]:
    """The masterlists with no count, grouped by what went wrong.

    Twenty sheets that aren't shared are one problem with twenty names on it,
    not twenty problems. Grouped on the refusal itself, so two different
    failures never get merged into one misleading line.
    """
    grouped: dict[str, list[str]] = {}
    for held in found:
        if held.count is not None:
            continue
        grouped.setdefault(held.problem or NO_PROBLEM_GIVEN, []).append(held.name)
    return list(grouped.items())


def _one_line(problem: str) -> str:
    """A refusal short enough to sit above the names it applies to."""
    said = " ".join(problem.split())
    # `explain` appends Google's own words after a small-text marker; the
    # heading above it is the part somebody acts on.
    said = said.split("-# Google said:")[0].strip()
    return said if len(said) <= 240 else said[:237] + "…"


def describe(live: list[Masterlist], idle: list[Masterlist]) -> str:
    """What was written, as something to read before opening the sheet."""
    lines = [
        f"**{len(live)}** live masterlist(s), **{total(live):,}** leads.",
    ]
    if idle:
        lines.append(f"**{len(idle)}** inactive, **{total(idle):,}** leads, on their own tab.")

    for problem, names in trouble(live + idle):
        shown = ", ".join(names[:MOST_NAMES_LISTED])
        if len(names) > MOST_NAMES_LISTED:
            shown += f", +{len(names) - MOST_NAMES_LISTED} more"
        lines.append("")
        lines.append(f"⚠ **{len(names)}** not counted — {_one_line(problem)}")
        lines.append(f"-# {shown}")
    return "\n".join(lines)
