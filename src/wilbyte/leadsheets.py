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
_NOT_A_TYPE = re.compile(
    r"\bmaster\s*lists?\b|\bmaster\s*files?\b|\bmaterlists?\b|\bleads?\b", re.IGNORECASE
)

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
OTHER_TAB = "Other sheets"

# Discord's categories are the list of what counts. A channel filed under no
# category, or left in the default "Text Channels", is not a lead type - it is
# a channel somebody made. The team keeps the categories tidy; that is the
# whole reason to read them rather than guess from names.
_NOT_A_CATEGORY = re.compile(
    r"^\s*(?:text\s+channels?|voice\s+channels?|\(no\s+category\)|general)\s*$",
    re.IGNORECASE,
)


def worth_listing(category: str) -> bool:
    """Whether a Discord category is one the masterlists are filed under."""
    return not _NOT_A_CATEGORY.match(category or "") and bool((category or "").strip())


@dataclass
class Masterlist:
    """One lead type: where it sits, what it is called, and how many it holds."""

    category: str
    name: str
    sheet: str = ""
    count: int | None = None
    # Why there is no count, when there isn't one. Silence reads as nought.
    problem: str = ""
    # Made by RYTE on this run, because the lead type had no sheet anywhere.
    created: bool = False
    # What the sheet actually is: a list of leads, or an agent deploy config.
    kind: str = "leads"

    @property
    def inactive(self) -> bool:
        return bool(INACTIVE.search(self.category))


# The category a Drive file gets when no Discord channel matches it. It is
# still a masterlist and it still belongs on the masterfile.
DRIVE_ONLY = "Not in Discord"

# Words that belong to the filing and not to the lead type. "Materlist" is in
# there because it is on the team's own reference file, spelled that way.
_FILING = re.compile(
    r"\b(?:master\s*lists?|master\s*files?|materlists?|leads?|sheets?|the|v\d+"
    r"|copy(?:\s+of)?|auto|deploy|new)\b",
    re.IGNORECASE,
)


def match_key(raw: str) -> tuple[str, ...]:
    """The words of a name that say which lead type it is, sorted.

    "🚚 otp-trucker-iul-masterlist", "OTP Trucker IUL" and "TRUCKER IUL Leads
    Masterlist" are the same masterlist written three ways, and the summary is
    only right if all three land on one row.
    """
    text = re.sub(r"[^A-Za-z0-9]+", " ", raw or "")
    text = _FILING.sub(" ", text)
    return tuple(sorted({word.lower() for word in text.split() if word}))


def find_sheet(name: str, files: list[dict]) -> dict | None:
    """The Drive file that is this lead type's masterlist, if one is.

    Exact first. A near match only counts when it is the *only* near match and
    has two words to go on: "Vet Leads Masterlist" is inside "OTP VET 2" as a
    word, and pairing those two would put one masterlist's count on another
    masterlist's row.
    """
    wanted = match_key(name)
    if not wanted:
        return None

    keyed = [(match_key(str(file.get("name") or "")), file) for file in files]
    for key, file in keyed:
        if key == wanted:
            return file

    near = [
        file for key, file in keyed
        if key and (set(key) <= set(wanted) or set(wanted) <= set(key))
        and min(len(key), len(wanted)) >= 2
    ]
    return near[0] if len(near) == 1 else None


# What a brand new masterlist gets as its first row. Nobody's leads are in it
# yet, so the header is only there so the first person to open it knows the
# shape. Change the columns in the sheet and RYTE will not put them back.
NEW_SHEET_HEADER = ["Name", "Email", "Phone", "State", "Date Added"]


def new_sheet_title(name: str) -> str:
    """What to call a masterlist RYTE has to make: "OTP Trucker IUL Masterlist"."""
    said = " ".join((name or "").split())
    if re.search(r"master\s*list", said, re.IGNORECASE):
        return said
    return f"{said} Masterlist"


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


NO_SHEET_ANYWHERE = "no sheet in the folder and no link in the channel"

# --------------------------------------------------- auto-deploy sheets
#
# Not every file in the folder is a list of leads. Some are the agent deploy
# configs - Agent_Name, Agent_Spend, Lead Cap, Launch_Date, one row per agent -
# and counting their rows gives a number of agents wearing a lead's clothes.
# They belong to a lead type all the same, so they are kept and labelled
# rather than thrown away.

LEADS = "leads"
DEPLOY = "deploy"
DEPLOY_TAB = "Auto Deploy"

AUTO_DEPLOY = re.compile(r"auto[\s_-]*deploy", re.IGNORECASE)

# Column names no masterlist has and every deploy sheet does.
DEPLOY_FIELDS = (
    "agent_name", "agent name", "agent_spend", "lead cap", "daily cap",
    "launch_date", "lead_received", "last_reset_time", "leads today",
)

# Which lead type an auto-deploy sheet has no channel for.
NO_CHANNEL = "—"


def deploy_header(header: list[str] | None) -> bool:
    """Whether a first row is an agent deploy config rather than leads.

    Two matches, not one: "Daily Cap" alone could plausibly turn up on
    somebody's lead sheet, and moving a real masterlist onto the wrong tab is
    worse than leaving a deploy sheet among them.
    """
    said = {" ".join(str(cell).lower().split()) for cell in header or []}
    return sum(1 for field in DEPLOY_FIELDS if field in said) >= 2


def kind_of(name: str, header: list[str] | None = None) -> str:
    """"leads" or "deploy", from what the file is called or what is in it."""
    if AUTO_DEPLOY.search(name or ""):
        return DEPLOY
    return DEPLOY if deploy_header(header) else LEADS


def by_kind(found: list[Masterlist]) -> tuple[list[Masterlist], list[Masterlist]]:
    """(the lead masterlists, the auto-deploy sheets)."""
    return (
        [held for held in found if held.kind != DEPLOY],
        [held for held in found if held.kind == DEPLOY],
    )


DEPLOY_HEADER = ("Lead type", "Auto deploy sheet", "Sheet", "Agents")


def deploy_rows(found: list[Masterlist]) -> list[list]:
    """The auto-deploy tab: which lead type, which sheet, how many agents."""
    rows: list[list] = [list(DEPLOY_HEADER)]
    for held in found:
        rows.append([
            held.category or NO_CHANNEL,
            held.name,
            f'=HYPERLINK("{held.sheet}","Open sheet")' if held.sheet else "—",
            "—" if held.count is None else held.count,
        ])
    return rows


def combine(found: list[Masterlist], files: list[dict]) -> list[Masterlist]:
    """The Discord lead types and the Drive folder, as one list.

    A lead type whose channel posts a sheet link keeps that link - it is the
    one the lead system writes into, and a folder file with a similar name is
    not a promise that they are the same sheet. The folder fills in the ones
    with no link, and whatever the channels never claimed goes on the end,
    because the folder is the masterlist of masterlists.
    """
    # Pulled out before any matching happens. "Mortgage Protection [New] -
    # Auto Deploy" reads as the Mortgage Protection channel's masterlist on
    # name alone, and adopting it would put an agent count in a lead column.
    deploys = [file for file in files if AUTO_DEPLOY.search(str(file.get("name") or ""))]
    deployed = {str(file.get("id") or "") for file in deploys}
    files = [file for file in files if str(file.get("id") or "") not in deployed]

    claimed: set[str] = set()
    for held in found:
        file = find_sheet(held.name, files)
        if file:
            claimed.add(str(file.get("id") or ""))
            if not held.sheet:
                held.sheet = str(file.get("url") or "")
                held.problem = ""
        if not held.sheet and not held.problem:
            held.problem = NO_SHEET_ANYWHERE

    extra = [
        Masterlist(
            category=DRIVE_ONLY,
            name=tidy_name(str(file.get("name") or "")),
            sheet=str(file.get("url") or ""),
        )
        for file in files
        if str(file.get("id") or "") not in claimed
    ]

    # Which lead type each deploy sheet belongs to, so the tab answers "whose
    # is this" rather than listing forty files called Auto Deploy.
    owners = {match_key(held.name): held.name for held in found}
    filed = [
        Masterlist(
            category=owners.get(match_key(str(file.get("name") or "")), NO_CHANNEL),
            name=tidy_name(str(file.get("name") or "")),
            sheet=str(file.get("url") or ""),
            kind=DEPLOY,
        )
        for file in deploys
    ]
    return list(found) + extra + filed


def apart(found: list[Masterlist]) -> tuple[list[Masterlist], list[Masterlist]]:
    """(the lead types, the Drive files nobody claimed).

    The second lot are real sheets and worth keeping sight of, but they are not
    the team's lead types and they do not belong on the tab somebody opens to
    ask what is live. They get a tab of their own.
    """
    theirs = [held for held in found if held.category != DRIVE_ONLY]
    rest = [held for held in found if held.category == DRIVE_ONLY]
    return theirs, rest


def missing(found: list[Masterlist]) -> list[Masterlist]:
    """The lead types with no sheet at all - the ones RYTE would have to make."""
    return [held for held in found if not held.sheet]


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

    made = [held.name for held in (live + idle) if held.created]
    if made:
        lines.append(f"🆕 Made in the folder: {', '.join(made)}")

    # Told apart from the counting failures below: nothing went wrong here,
    # there is simply no sheet yet, and the answer is to make one.
    sheetless = [
        held for held in (live + idle)
        if not held.sheet and held.problem in ("", NO_SHEET_ANYWHERE)
    ]
    blank = [held.name for held in sheetless]
    if blank:
        lines.append("")
        lines.append(
            f"**{len(blank)}** lead type(s) have no sheet anywhere: "
            f"{', '.join(blank[:MOST_NAMES_LISTED])}"
            + (f", +{len(blank) - MOST_NAMES_LISTED} more" if len(blank) > MOST_NAMES_LISTED else "")
        )
        lines.append("-# `@RYTE masterlists create` makes one for each, in the folder.")

    already = {id(held) for held in sheetless}
    stuck = [held for held in (live + idle) if id(held) not in already]
    for problem, names in trouble(stuck):
        shown = ", ".join(names[:MOST_NAMES_LISTED])
        if len(names) > MOST_NAMES_LISTED:
            shown += f", +{len(names) - MOST_NAMES_LISTED} more"
        lines.append("")
        lines.append(f"⚠ **{len(names)}** not counted — {_one_line(problem)}")
        lines.append(f"-# {shown}")
    return "\n".join(lines)
