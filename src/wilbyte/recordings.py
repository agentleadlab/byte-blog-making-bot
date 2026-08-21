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
    # Why there is no summary, when there isn't one. Silence here reads as
    # "nothing to say about the call" rather than "I never found it".
    note: str = ""
    # How the call was identified. Zoom's API can't resolve a share link, so
    # sometimes this is a judgement rather than a match - and a judgement gets
    # said out loud where somebody can correct it.
    matched_by: str = ""
    # The client's name where it was typed into the message. Names the card on
    # its own, so a call nobody can read still files under the right heading.
    client_hint: str = ""
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
            return Recording(
                url=url,
                platform=platform,
                passcode=find_passcode(text),
                client_hint=find_client(text),
            )
    return None


# "Sales: Derrick Robison <link>". The label is what separates a client's name
# from any other words in the message, and it is how these get posted anyway.
_CLIENT_LINE = re.compile(
    r"(?:^|\s)(?:sales?|call|client|with|for)\s*[:\-–]\s*(?P<name>[^\n]+)",
    re.IGNORECASE,
)


def find_client(text: str) -> str:
    """The client's name where somebody typed it, or "".

    Worth having twice over: it names the card without anything being read,
    and - because Zoom titles a recording after whoever was on it - it is also
    the search that finds the call, which is what makes the common case need no
    dropdown at all.
    """
    from .bot.mentions import ANY_URL_RE, MENTION_RE, ROLE_MENTION_RE

    cleaned = ANY_URL_RE.sub(" ", text or "")
    cleaned = ROLE_MENTION_RE.sub(" ", MENTION_RE.sub(" ", cleaned))
    match = _CLIENT_LINE.search(cleaned)
    if not match:
        return ""
    # A passcode line opens with its own label, so it can't land here - but a
    # name still has to read like one rather than like half a sentence.
    return _as_a_name(match.group("name"), words=6)


def find_passcode(text: str) -> str:
    """The passcode, exactly as typed.

    Verbatim to the end of the line on purpose. `U^M^s7Bw` survives; stripping
    punctuation or splitting on a space would hand over a passcode that doesn't
    work, which is worse than none at all because it looks right.
    """
    match = _PASSCODE.search(text or "")
    return match.group("value").strip() if match else ""


def call_title(rec: "Recording", *, prefix: str = TITLE_PREFIX) -> str:
    """"Sales Recording: Santiago Villegas + Derrick Robison".

    The `+` is part of the format, not decoration: two full names run together
    read as one long name, and the gallery is scanned rather than read.

    The people are the title. A running number was tried first and was worse in
    both directions: it read the highest number already used out of the existing
    titles, which stopped working the moment names were appended to them, and a
    number tells you nothing about a call you are trying to find. Who was on it
    does.

    Either name may be missing - Zoom gives a meeting topic rather than a
    labelled guest list - so the title degrades a piece at a time rather than
    trailing off after the colon.
    """
    # Read from the call where it could be read; otherwise from what the person
    # posting it already said. Whoever posts a recording is nearly always the
    # closer who was on it, and the client's name is the one thing they type.
    closer = _as_a_name(rec.closer) or _as_a_name(person_name(rec.posted_by))
    client = (
        next((found for found in (_as_a_name(g) for g in rec.guests) if found), "")
        or _as_a_name(rec.client_hint)
    )

    who = " + ".join(part for part in (closer, client) if part)
    if not who:
        who = _as_a_name(rec.topic, words=8) or (rec.topic or "").strip()[:60]
    return f"{prefix}: {who}" if who else prefix


# Where a Discord display name stops being a name. People decorate them with
# a role, a team, an emoji - "Franklin 🐻| General Manager" - and all of it
# ended up on a card title.
# A dash only separates when it stands alone: "Mary-Jane O'Brien" is one name,
# "Tre - Closer" is a name with a job title after it.
_DECORATION = re.compile(r"\s*(?:[|/\u00b7\u2022]|\s[-\u2013\u2014]\s).*$")


def person_name(display: str) -> str:
    """`Franklin 🐻| General Manager` -> `Franklin`.

    Whoever posts a recording stands in for the closer on the card, so their
    Discord name is read as a person's name - and Discord names carry job
    titles, pronouns and emoji that a card title has no use for.
    """
    found = _DECORATION.sub("", " ".join((display or "").split()))
    # Emoji and symbols, but not letters, digits, spaces or ordinary name
    # punctuation - an apostrophe or a hyphen inside a name has to survive.
    kept = "".join(
        ch for ch in found
        if ch.isalnum() or ch.isspace() or ch in "'-."
    )
    return " ".join(kept.split()).strip(" .-")


# A guard, not a parser. Names arrive from a transcript's speaker labels, and
# when that reading goes wrong it goes wrong by swallowing a sentence - which
# is how a card ended up titled "... How are you? Good morning. Derrick
# Robison". The upstream fix is the real one; this is what stops the next
# variant of it reaching a title.
def _as_a_name(text: str | None, *, words: int = 5, chars: int = 48) -> str:
    found = " ".join((text or "").split())
    if not found or len(found) > chars or len(found.split()) > words:
        return ""
    return "" if any(mark in found for mark in ".?!") else found


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
    "description": ("description", "notes", "details"),
    "summary": ("summary", "notes", "description", "what it covers"),
    "kind": ("kind", "type", "format", "source"),
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
    # Verbatim: a passcode can contain asterisks, and reading those as
    # emphasis would drop them from the line people copy.
    blocks.append(notion.paragraph(" · ".join(details), markdown=False))

    if summary.strip():
        blocks.append(notion.heading("Summary"))
        blocks.extend(summary_blocks(summary))
    return blocks


# "1. " or "2) " opening a line.
_NUMBERED_LINE = re.compile(r"^\d{1,2}[.)]\s+")
# A whole line wrapped in asterisks - a section title, not a bold sentence.
_ALL_BOLD = re.compile(r"^\*\*(?P<text>[^*]+?)\*\*:?$")


def summary_blocks(summary: str) -> list[dict]:
    """A written summary turned into Notion blocks that read like a document.

    The model writes markdown, and Notion has never heard of it - so `**What
    the prospect wanted**` arrived on the card as literal asterisks. Emphasis
    inside a line becomes a real mark; a line that is nothing but a bold phrase
    becomes a heading, because that is what it was being used as.
    """
    from . import notion

    blocks: list[dict] = []
    for line in (summary or "").strip().splitlines():
        text = line.strip()
        if not text:
            continue

        if text.startswith("### "):
            blocks.append(notion.subheading(text[4:].strip()))
        elif text.startswith(("## ", "# ")):
            blocks.append(notion.heading(text.lstrip("#").strip()))
        elif _ALL_BOLD.match(text):
            # The colon usually sits inside the asterisks - "**Objections
            # raised:**" - so it has to come off after unwrapping, not before.
            blocks.append(
                notion.subheading(_ALL_BOLD.match(text).group("text").strip().rstrip(":").strip())
            )
        elif text.startswith(("- ", "• ", "* ")):
            blocks.append(notion.bullet(text[2:].strip()))
        elif _NUMBERED_LINE.match(text):
            blocks.append(notion.numbered(_NUMBERED_LINE.sub("", text)))
        else:
            blocks.append(notion.paragraph(text))
    return blocks


# ------------------------------------------------ which calls are already in

# Zoom's API cannot resolve a share link to a recording, so when neither the
# link nor a passcode identifies one, RYTE falls back to "the most recent call
# not filed yet". That answer is only right if it knows what it has filed.

FILED_PATH_NAME = "filed-recordings.json"


def _filed_path(path=None):
    from pathlib import Path

    from .state import _state_dir

    return Path(path) if path else _state_dir() / FILED_PATH_NAME


def filed_ids(path=None) -> set[str]:
    """Zoom meeting ids already written to the gallery."""
    import json

    found = _filed_path(path)
    if not found.exists():
        return set()
    try:
        data = json.loads(found.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {str(item) for item in data} if isinstance(data, list) else set()


def remember_filed(meeting_id: str, path=None) -> None:
    """Note that a call is in the gallery, so it isn't offered again."""
    import json

    if not meeting_id:
        return
    found = _filed_path(path)
    known = filed_ids(path)
    known.add(str(meeting_id))
    found.parent.mkdir(parents=True, exist_ok=True)
    found.write_text(json.dumps(sorted(known), indent=2), encoding="utf-8")


# ------------------------------------------- finding a card again later

# "@RYTE need the sales recording for Derrick Robison". The gallery is the
# point of filing these, and a link somebody has to go and dig out by hand is
# most of the way back to not having filed it.

# Words that carry no information about *which* recording is wanted.
_ASKING_WORDS = {
    "a", "again", "and", "back", "call", "calls", "can", "could", "do", "find",
    "for", "get", "give", "got", "have", "hey", "i", "is", "it", "link", "links",
    "me", "my", "need", "notion", "of", "on", "one", "please", "pull", "recording",
    "recordings", "ryte", "sales", "send", "share", "show", "that", "the", "to", "up",
    "want", "was", "what", "where", "which", "with", "you", "your",
}


def wanted_name(text: str) -> str:
    """What was asked for, once the asking is taken out.

    "need the sales recording for Derrick Robison call" leaves "Derrick
    Robison". Everything else is scaffolding around the one part that says
    which recording.
    """
    from .bot.mentions import ANY_URL_RE, MENTION_RE, ROLE_MENTION_RE

    cleaned = ANY_URL_RE.sub(" ", text or "")
    cleaned = ROLE_MENTION_RE.sub(" ", MENTION_RE.sub(" ", cleaned))
    kept = [
        word for word in re.split(r"[^\w'-]+", cleaned)
        if word and word.casefold() not in _ASKING_WORDS
    ]
    return " ".join(kept).strip()


def row_title(row: dict) -> str:
    """The title text of one Notion row."""
    for value in (row.get("properties") or {}).values():
        if value.get("type") == "title":
            return "".join(part.get("plain_text", "") for part in value.get("title") or [])
    return ""


def row_link(row: dict) -> str:
    """The recording's own URL off a card, where the gallery has one.

    "Need the video of Derrick Robison" wants the video. Handing back only the
    Notion page makes somebody open it and click again.
    """
    for name, value in (row.get("properties") or {}).items():
        if value.get("type") == "url" and name.strip().casefold() in COLUMN_ALIASES["link"]:
            return str(value.get("url") or "")
    return ""


def matching_rows(rows: list[dict], name: str) -> list[tuple[str, str, str]]:
    """(title, card, recording) for cards whose title carries every word asked for.

    Every word rather than any: "derrick robison" should not also return every
    other Derrick. With nothing asked for, the newest handful - which is the
    sensible reading of "send me the sales recordings".
    """
    found = [
        (row_title(row), str(row.get("url") or ""), row_link(row))
        for row in rows or []
    ]
    found = [entry for entry in found if entry[0]]
    words = [word.casefold() for word in (name or "").split()]
    if not words:
        return found
    return [
        entry for entry in found
        if all(word in entry[0].casefold() for word in words)
    ]
