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

# Only used when that tab is empty. Once it has a header, whatever headings
# are on it decide what an appended row carries - see `row_for`.
OTHER_HEADER = ("Type of leads", "Sheet")

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
    # A link straight to the Discord channel. The auto-deploy tab is a list of
    # channels with the wrong sheet attached, and the first thing anybody wants
    # from a row like that is to go and fix it.
    channel: str = ""
    # Made by RYTE on this run, because the lead type had no sheet anywhere.
    created: bool = False
    # What happened to this row on this run, for the flagged tab to say so.
    status: str = ""
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
    r"|copy(?:\s+of)?|auto|deploy|new|20\d\d)\b",
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

# The tab on an auto-deploy sheet where the leads themselves are. Its header is
# the right header for that lead type's masterlist - it is the shape the team's
# own leads already come in, which beats anything RYTE could invent.
LEADS_TAB = re.compile(r"(?:available|master)[\s_-]*leads", re.IGNORECASE)

# The tab every deploy sheet has and no masterlist does. This is the surest
# tell of the three: a deploy sheet kept outside the folder, named nothing in
# particular, still has to configure its agents somewhere.
CONFIG_TAB = re.compile(r"agents?[\s_-]*config", re.IGNORECASE)


def leads_tab_in(tabs: list[str]) -> str:
    """The leads tab of a deploy sheet - Available_Leads, or Master_Leads."""
    for title in tabs or []:
        if LEADS_TAB.search(title):
            return title
    return ""


def config_tab_in(tabs: list[str]) -> str:
    """The Agent_Config tab, when the sheet has one."""
    for title in tabs or []:
        if CONFIG_TAB.search(title):
            return title
    return ""


# What a lead type is qualified by, in the order the team writes them. The
# channel is called "Mortgage Protection" and its deploy sheet is called "Text
# Verified MTG Auto Deploy New Setup" - the qualifier only exists on the deploy
# sheet, and a masterlist called plain "Mortgage Protection" would sit next to
# three others nobody can tell apart.
QUALIFIERS = (
    ("Text-Verified", r"text[\s_-]*verified"),
    ("No OTP", r"\bno[\s_-]*otp\b"),
    ("OTP", r"\botp\b"),
    ("Spanish", r"\bspanish\b"),
    ("Facebook", r"\bfacebook\b|\bfb\b"),
    ("Blue Collar", r"blue[\s_-]*collar"),
    ("Abandoned", r"\babandoned\b"),
    ("Instant", r"\binstant\b"),
    ("Momentum", r"\bmomentum\b"),
    ("Ascend", r"\bascend\b"),
    ("Tax Free", r"tax[\s_-]*free"),
    ("Standard", r"\bstandard\b"),
)


def qualified_name(lead_type: str, deploy_name: str = "") -> str:
    """The lead type, with whatever its deploy sheet says it is.

    One qualifier, not a stack of them: "Text-Verified Facebook OTP Spanish
    IUL" is a file name nobody wants to read. Text-Verified comes first
    wherever it applies, and only what the lead type doesn't already say -
    "OTP Standard IUL" saying Standard twice helps nobody.
    """
    said = " ".join((lead_type or "").split())
    if not deploy_name:
        return said

    found = []
    for word, pattern in QUALIFIERS:
        if not re.search(pattern, deploy_name, re.IGNORECASE):
            continue
        if re.search(pattern, said, re.IGNORECASE):
            continue
        if word == "OTP" and re.search(r"\botp\b", said, re.IGNORECASE):
            continue
        found.append(word)
        break  # The first is the one that counts; QUALIFIERS is the priority.

    return " ".join(found + [said]) if found else said


def new_sheet_title(name: str, *, year: int | None = None) -> str:
    """What to call a masterlist RYTE makes: "Mortgage Protection Masterlist 2026".

    The year is deliberate. There are masterlists in that folder going back
    years and a fair few near-duplicates, so anything RYTE made should be
    obvious at a glance and never mistaken for the one the team keeps.
    """
    from datetime import date

    said = re.sub(r"\s*master\s*list\s*", " ", " ".join((name or "").split()),
                  flags=re.IGNORECASE).strip()
    return f"{said} Masterlist {year or date.today().year}"


_SHEET_ID = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})")


def id_of(url: str) -> str:
    """The spreadsheet id inside a link, or "" if there isn't one."""
    found = _SHEET_ID.search(url or "")
    return found.group(1) if found else ""


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

# The lead types that really do keep a masterlist, and where it is. Nearly
# every channel posts an auto-deploy sheet instead, so these are the exceptions
# somebody had to name by hand. Aliases because the same masterlist is written
# three ways: the channel's name, the file's name, and what people call it.
# `@RYTE masterlist <lead type> <link>` adds to this and beats it.
KNOWN_SHEETS = {
    ("otp trucker iul", "otp iul truckers", "trucker iul", "otp trucker"):
        "https://docs.google.com/spreadsheets/d/1wkwoLkzMfhlmkNSLdSEo5YJKTP0Ax06foBYr8z_8Fqo/edit",
    ("uprise agents lp", "uprise agents", "uprise agent lp", "uprise"):
        "https://docs.google.com/spreadsheets/d/1qANSohg-t-DSZfM_VoXgvpL68lDdbdKLa6YFKkphIR4/edit",
    ("otp vet 2", "otp vet2"):
        "https://docs.google.com/spreadsheets/d/1_14LZh3zTNXBLGgDs3JOwWExpEHWSEigoBUfGOlDRmQ/edit",
    ("vet widows", "vet widows otp", "otp vet widows"):
        "https://docs.google.com/spreadsheets/d/1on4xQOO1PMquE2UlwxHkBmYFqP9i3JLtjMDdkgljpos/edit",
    ("abandoned iul",):
        "https://docs.google.com/spreadsheets/d/1lK4FN2RSUfPAubLSCVOCN2ll2HIWMVXaRpk47__TjXw/edit",
    ("trucker lp", "trucker lp tax free", "trucker lp tax", "trucker lp le"):
        "https://docs.google.com/spreadsheets/d/16D3izKnOFrF7f3igJt7jEuJNpkKvnfLPY2zUFMsxSYU/edit",
}


def known_sheet(name: str) -> str:
    """The masterlist somebody named for this lead type, or ""."""
    from . import leadstate

    wanted = leadstate.key(name)
    for aliases, url in KNOWN_SHEETS.items():
        if wanted in aliases:
            return url
    return ""


def pinned_sheet(name: str, *, said: dict[str, str] | None = None) -> str:
    """The sheet to count for a lead type, whoever said so. "" if nobody did.

    What was typed at RYTE beats the list in the source, which beats whatever
    the channel happens to link - because what the channel links is usually an
    auto-deploy sheet.
    """
    from . import leadstate

    return leadstate.sheet_of(name, held=said) or known_sheet(name)

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


# What every list of leads has and no deploy config does. A masterlist that
# also records which agent got the lead still has these; that is what told
# twenty-two real masterlists apart from the deploy sheets they were mistaken
# for the first time this ran.
LEAD_FIELDS = ("email", "email address", "phone", "phone number", "e-mail")


def deploy_header(header: list[str] | None) -> bool:
    """Whether a first row is an agent deploy config rather than leads.

    A deploy sheet is one that looks like a deploy config *and* holds no
    leads. Agent columns alone are not enough - plenty of masterlists record
    which agent a lead went to - and moving a real masterlist onto the wrong
    tab loses its count off the live total, which is the worse mistake.
    """
    said = {" ".join(str(cell).lower().split()) for cell in header or []}
    if any(field in said for field in LEAD_FIELDS):
        return False
    return sum(1 for field in DEPLOY_FIELDS if field in said) >= 2


def kind_of(
    name: str, header: list[str] | None = None, tabs: list[str] | None = None
) -> str:
    """"leads" or "deploy", from the name, the tabs, or the columns.

    Three tells because one was not enough. Most deploy sheets say so in the
    name; the ones that don't have an Agent_Config tab; and a sheet with
    neither is judged on its columns.
    """
    if AUTO_DEPLOY.search(name or ""):
        return DEPLOY
    if tabs and config_tab_in(tabs):
        return DEPLOY
    return DEPLOY if deploy_header(header) else LEADS


def by_kind(found: list[Masterlist]) -> tuple[list[Masterlist], list[Masterlist]]:
    """(the lead masterlists, the auto-deploy sheets)."""
    return (
        [held for held in found if held.kind != DEPLOY],
        [held for held in found if held.kind == DEPLOY],
    )


DEPLOY_HEADER = ("Lead type", "Channel", "Auto deploy sheet", "Sheet", "Agents", "Status")

# What a row on the flagged tab says once RYTE has built the masterlist for it.
SORTED_OUT = "Masterlist made"

# ...and when somebody named the real masterlist instead of RYTE making one.
PINNED = "Masterlist linked"


def done_rows(rows: list[list]) -> list[int]:
    """Which rows of the flagged tab are sorted out, for highlighting.

    Row 1 is the header, so a row's number here is its position in the sheet.
    """
    return [
        number for number, row in enumerate(rows)
        if number and str(row[-1] or "").startswith((SORTED_OUT, PINNED))
    ]


def deploy_rows(found: list[Masterlist]) -> list[list]:
    """The auto-deploy tab: whose it is, where to fix it, and what it holds.

    This tab is a job list. Each row is a lead type whose channel has a deploy
    sheet attached where a masterlist should be - so the channel link matters
    more than the sheet does, because the fix happens in Discord.
    """
    rows: list[list] = [list(DEPLOY_HEADER)]
    for held in found:
        rows.append([
            held.category or NO_CHANNEL,
            _link(held.channel, "Open channel"),
            held.name,
            _link(held.sheet, "Open sheet"),
            "—" if held.count is None else held.count,
            held.status or "Needs a masterlist",
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
    deployed = {str(file.get("id") or ""): file for file in deploys}
    files = [file for file in files if str(file.get("id") or "") not in deployed]

    # A channel whose posted link *is* a deploy sheet has no masterlist, and
    # saying so is what lets the real one - made or fixed later - take its
    # place. Without this the channel keeps reporting an agent count as leads
    # for as long as the old posts sit there.
    wrongly: dict[str, Masterlist] = {}
    for held in found:
        # Somebody naming the sheet outranks everything: the channel's link is
        # usually the deploy sheet, and the folder holds several files with
        # nearly the same name.
        pinned = pinned_sheet(held.name)
        if pinned:
            if id_of(held.sheet) in deployed:
                wrongly[id_of(held.sheet)] = held
            held.sheet = pinned
            held.status = PINNED
            continue

        linked = id_of(held.sheet)
        if linked and linked in deployed:
            wrongly[linked] = held
            held.sheet = ""

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
    owners = {match_key(held.name): held for held in found}
    filed = []
    for file in deploys:
        # Whose channel actually links it beats whose name it looks like.
        owner = wrongly.get(str(file.get("id") or ""))
        owner = owner or owners.get(match_key(str(file.get("name") or "")))
        filed.append(Masterlist(
            category=owner.name if owner else NO_CHANNEL,
            name=tidy_name(str(file.get("name") or "")),
            sheet=str(file.get("url") or ""),
            channel=owner.channel if owner else "",
            kind=DEPLOY,
            # The channel still links this, but the masterfile no longer does.
            status=f"{PINNED} — {owner.name}" if owner and owner.status == PINNED else "",
        ))
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


def ids_in(rows: list[list[str]]) -> set[str]:
    """Every sheet already linked somewhere in these rows.

    A row links its sheet as a raw URL when somebody pasted it and inside
    =HYPERLINK() when RYTE wrote it. Both carry the id, which is the only
    thing that says two rows are about the same sheet.
    """
    found: set[str] = set()
    for row in rows or []:
        for cell in row or []:
            for link in re.findall(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})", str(cell)):
                found.add(link)
    return found


# What a column means, by what its heading says. The tabs are the team's now -
# somebody has already renamed and reordered these - so a new row is built to
# fit whatever headings are there rather than to a shape RYTE decided.
# Order matters: "Type of leads" is the name column and says "leads" in it,
# so the name pattern has to be asked first.
COLUMN_MEANINGS = (
    ("name", re.compile(r"\btype\b|\bname\b|\bmaster\s*lists?\b", re.IGNORECASE)),
    ("channel", re.compile(r"\bchannel\b", re.IGNORECASE)),
    ("sheet", re.compile(r"\bsheet\b|\blink\b", re.IGNORECASE)),
    ("count", re.compile(r"\btotal\b|\bcount\b|\bhow\s+many\b", re.IGNORECASE)),
)


def column_kind(heading: str) -> str:
    """Which field a column heading is asking for. "" when it asks for none."""
    said = " ".join((heading or "").split())
    for kind, pattern in COLUMN_MEANINGS:
        if pattern.search(said):
            return kind
    return ""


def row_for(header: list[str], held: Masterlist) -> list:
    """One masterlist, laid out to match the headings a tab already has."""
    row: list = []
    for heading in header or []:
        kind = column_kind(heading)
        if kind == "name":
            row.append(held.name)
        elif kind == "sheet":
            row.append(_link(held.sheet, "Open sheet"))
        elif kind == "channel":
            row.append(_link(held.channel, "Open channel"))
        elif kind == "count":
            row.append("—" if held.count is None else held.count)
        else:
            row.append("")
    return row or [held.name, _link(held.sheet, "Open sheet")]


def refill(found: list[Masterlist], files: list[dict]) -> int:
    """Match the folder again, for lead types left without a sheet.

    `combine` runs before a single sheet has been opened, so a channel that
    links an auto-deploy sheet still looks like it has a masterlist and its
    real one in the folder is passed over. The deploy sheet is only recognised
    once it has been read - by which point that match has to happen again, or
    RYTE builds a second masterlist next to the one already there.
    """
    real = [
        file for file in files
        if not AUTO_DEPLOY.search(str(file.get("name") or ""))
    ]
    filled = 0
    for held in found:
        if held.sheet or held.kind == DEPLOY:
            continue
        file = find_sheet(held.name, real)
        if file:
            held.sheet = str(file.get("url") or "")
            held.problem = ""
            filled += 1
    return filled


def missing(found: list[Masterlist]) -> list[Masterlist]:
    """The lead types with no sheet at all - the ones RYTE would have to make."""
    return [held for held in found if not held.sheet]


# No Category column. It repeated what the lead type's own name already says -
# "IUL" beside "OTP Blue Collar IUL" - and the tab it is on already answers
# live or not. Discord's categories still decide which tab a lead type lands
# on; they just aren't worth a column of their own.
HEADER = ("Type of leads", "Channel", "Sheet", "Total leads")


def _link(url: str, said: str) -> str:
    return f'=HYPERLINK("{url}","{said}")' if url else "—"


def summary_rows(found: list[Masterlist]) -> list[list]:
    """The five columns, header included, ready to write.

    A count that couldn't be read is written as a dash and not a nought.
    Nought is a number somebody will act on.
    """
    rows: list[list] = [list(HEADER)]
    for held in found:
        rows.append([
            held.name,
            _link(held.channel, "Open channel"),
            _link(held.sheet, "Open sheet"),
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
