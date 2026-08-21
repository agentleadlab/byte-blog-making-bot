"""Turn a posted sales-call recording into a Notion gallery entry.

Someone drops a link in Discord with the passcode on the line underneath:

    https://us06web.zoom.us/rec/share/qVQ0NLoGrnVQQZbM-EnD-1J-hOD6vtm...
    Passcode: U^M^s7Bw

RYTE gets tagged, and the call lands in Notion as `Sales Recording 12` with the
link, the passcode, and a summary of what was said where one can be reached.

Nothing here talks to a network: parsing and numbering are decided from the
text and the rows that already exist, so the rules can be tested without a
Notion token and the entry can be shown before it is written.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

# Where a recording can come from. Ordered: the first match names the platform,
# and a Zoom link inside a YouTube description shouldn't outrank the video.
PLATFORMS = (
    ("Zoom", re.compile(r"https?://[\w.-]*zoom\.us/\S+", re.IGNORECASE)),
    ("Fathom", re.compile(r"https?://(?:www\.)?fathom\.video/\S+", re.IGNORECASE)),
    (
        "YouTube",
        re.compile(r"https?://(?:www\.|m\.)?(?:youtube\.com|youtu\.be)/\S+", re.IGNORECASE),
    ),
)

# "Passcode: U^M^s7Bw", "passcode U^M^s7Bw", "Password - abc123".
# Everything after the label is taken verbatim to the end of the line: these
# are full of ^, $ and & and a tidy-up would silently break the passcode.
_PASSCODE = re.compile(
    r"^\s*(?:pass\s*code|pass\s*word|pwd|code)\s*[:\-–]?\s*(?P<value>\S.*?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

TITLE_PREFIX = "Sales Recording"
_NUMBERED = re.compile(rf"^\s*{TITLE_PREFIX}\s*\(?\s*(\d+)\s*\)?\s*$", re.IGNORECASE)


@dataclass
class Recording:
    """One posted recording, ready to file."""

    url: str
    platform: str
    passcode: str = ""
    posted_by: str = ""
    posted_on: date | None = None

    # Filled in once the platform has been asked for the call, so the summary
    # step and the title can both use it without fetching twice.
    topic: str = ""
    host_email: str = ""
    participants: tuple[str, ...] = ()
    # Who recorded it, and who from outside was on it. These name the card.
    closer: str = ""
    guests: tuple[str, ...] = ()
    # Fathom writes a summary of its own; used in preference to paying for one.
    fathom_summary: str = ""

    def transcribable(self, config=None) -> bool:
        """Whether the transcript can be reached at all.

        YouTube always. Zoom only with a Server-to-Server OAuth app configured -
        the share link itself is a browser door with a passcode on it, and the
        API is the way round that. Fathom needs its own key and isn't wired up
        yet, so it is filed with the link and nothing invented about the call.
        """
        if self.platform == "YouTube":
            return True
        if config is None:
            return False
        if self.platform == "Zoom":
            return bool(
                config.secrets.zoom_account_id
                and config.secrets.zoom_client_id
                and config.secrets.zoom_client_secret
            )
        if self.platform == "Fathom":
            return bool(config.secrets.fathom_api_key)
        return False


def find_recording(text: str) -> Recording | None:
    """The first recording link in a message, with the passcode that follows it."""
    for platform, pattern in PLATFORMS:
        match = pattern.search(text or "")
        if match:
            url = match.group(0).rstrip(".,;)>")
            return Recording(url=url, platform=platform, passcode=find_passcode(text))
    return None


def find_passcode(text: str) -> str:
    """The passcode, exactly as typed.

    Verbatim to the end of the line on purpose. `U^M^s7Bw` survives; stripping
    punctuation or splitting on a space would hand over a passcode that doesn't
    work, which is worse than none at all because it looks right.
    """
    match = _PASSCODE.search(text or "")
    return match.group("value").strip() if match else ""


def call_title(rec: "Recording", fallback: str) -> str:
    """"Santiago and Derrick Robison" - closer first, then the agent.

    Matching how these were being named by hand, so the gallery reads the same
    before and after RYTE took the job over. Falls back through the meeting
    title to a numbered name, because a card called "Sales Recording 12" is
    findable and one called "" is not.
    """
    closer = (rec.closer or "").strip()
    agent = next((name.strip() for name in rec.guests if name and name.strip()), "")

    if closer and agent:
        return f"{closer} and {agent}"
    if closer:
        return closer
    if agent:
        return agent
    return (rec.topic or "").strip() or fallback


def next_number(existing_titles, *, prefix: str = TITLE_PREFIX) -> int:
    """One past the highest number already used.

    Highest rather than a count: a deleted row must not make the next recording
    reuse a number that is already written down somewhere else.
    """
    highest = 0
    for title in existing_titles or []:
        match = _NUMBERED.match(str(title or ""))
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def title_for(number: int, *, prefix: str = TITLE_PREFIX) -> str:
    return f"{prefix} {number}"


def row_titles(rows: list[dict]) -> list[str]:
    """Pull the title text out of Notion database rows."""
    titles = []
    for row in rows or []:
        for value in (row.get("properties") or {}).values():
            if value.get("type") != "title":
                continue
            titles.append("".join(part.get("plain_text", "") for part in value.get("title") or []))
    return titles


# ----------------------------------------------------------- the database


def database_schema(name_property: str = "Name") -> dict:
    """The gallery's columns: what was asked for, plus the day it happened.

    Deliberately small. Link and passcode are what a recording *is*; the date
    is what makes a growing gallery findable six months from now.
    """
    return {
        name_property: {"title": {}},
        "Link": {"url": {}},
        "Passcode": {"rich_text": {}},
        "Date": {"date": {}},
    }


# What a column might be called, by what it holds. The gallery was built by
# hand before RYTE saw it, so its column names are whatever made sense to the
# person who made it - matching several spellings beats renaming their board.
COLUMN_ALIASES = {
    "link": ("link", "url", "video", "recording", "call"),
    "passcode": ("passcode", "password", "code", "pwd"),
    "description": ("description", "notes", "details", "summary"),
}


# Added to the gallery when it doesn't have them. Additive and reversible;
# dropping the two things a recording *is* would not be.
EXTRA_COLUMNS = {
    "Link": {"url": {}},
    "Passcode": {"rich_text": {}},
    "Description": {"rich_text": {}},
}


def map_properties(
    schema: dict, recording: "Recording", title: str, *, description: str = ""
) -> dict:
    """Fill the columns this database actually has, and skip the ones it doesn't.

    Writing a property Notion doesn't know about fails the whole create, so the
    database's own schema decides what gets sent rather than a shape assumed
    here.
    """
    properties: dict = {}
    used: set[str] = set()

    for column, spec in (schema or {}).items():
        kind = spec.get("type")
        key = " ".join(column.split()).casefold()

        if kind == "title":
            properties[column] = {"title": [{"type": "text", "text": {"content": title}}]}
        elif kind == "url" and _matches(key, "link") and "link" not in used:
            properties[column] = {"url": recording.url}
            used.add("link")
        elif kind == "date":
            if recording.posted_on:
                properties[column] = {"date": {"start": recording.posted_on.isoformat()}}
        elif kind == "rich_text":
            if _matches(key, "passcode") and recording.passcode:
                properties[column] = _rich(recording.passcode)
            elif _matches(key, "description") and description:
                properties[column] = _rich(description)
    return properties


def _matches(column_key: str, role: str) -> bool:
    return any(alias in column_key for alias in COLUMN_ALIASES[role])


def _rich(text: str) -> dict:
    return {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]}


def description_for(recording: "Recording") -> str:
    """The link, and the passcode when there is one - what the spec asked for."""
    if recording.passcode:
        return f"{recording.url}\nPasscode: {recording.passcode}"
    return recording.url


def page_properties(recording: Recording, title: str, *, name_property: str = "Name") -> dict:
    """The row itself. A blank passcode is left blank rather than filled with '-'."""
    properties: dict = {
        name_property: {"title": [{"type": "text", "text": {"content": title}}]},
        "Link": {"url": recording.url},
    }
    if recording.passcode:
        properties["Passcode"] = {
            "rich_text": [{"type": "text", "text": {"content": recording.passcode}}]
        }
    if recording.posted_on:
        properties["Date"] = {"date": {"start": recording.posted_on.isoformat()}}
    return properties


def page_blocks(recording: Recording, summary: str = "") -> list[dict]:
    """What's inside the card when you open it."""
    from . import notion

    blocks = [notion.bookmark(recording.url)]

    details = [f"Platform: {recording.platform}"]
    if recording.passcode:
        # In the body as well as the property: this is the line someone copies
        # while they have the recording open in the next tab.
        details.append(f"Passcode: {recording.passcode}")
    if recording.posted_by:
        details.append(f"Posted by: {recording.posted_by}")
    blocks.append(notion.paragraph(" · ".join(details)))

    if summary.strip():
        blocks.append(notion.heading("Summary"))
        for line in summary.strip().splitlines():
            text = line.strip()
            if not text:
                continue
            if text.startswith(("- ", "• ", "* ")):
                blocks.append(notion.bullet(text[2:].strip()))
            else:
                blocks.append(notion.paragraph(text))
    return blocks
