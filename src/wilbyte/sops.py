"""Turn what gets posted in #sop into a Notion library RYTE can be asked about.

Somebody drops a Loom link with a title above it, or a YouTube walkthrough, or
a screenshot of a settings page, or just types out how a thing is done. Each of
those is a standard operating procedure, and each ends up as a card:

    How to Create Internal Ads LeadForm
    https://www.loom.com/share/56abe3196b4f482caa68363e00355377

The point is not the filing. It is that three weeks later somebody can ask
"@RYTE do we have an SOP for lead forms?" and get the answer, instead of
scrolling a channel. So the summary matters more here than it does for a sales
call - it is what the question is matched against.

Nothing in this module talks to a network: it takes the message text and the
attachments Discord already handed over and works out what the card should say,
so the rules can be tested without a Notion token.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

TITLE_PREFIX = "SOP"

# What a posted SOP can be. Ordered: the first match names the kind, so a Loom
# link inside a longer message outranks the fact that there is also text.
SOURCES = (
    ("Loom", re.compile(r"https?://(?:www\.)?loom\.com/\S+", re.IGNORECASE)),
    (
        "YouTube",
        re.compile(r"https?://(?:www\.|m\.)?(?:youtube\.com|youtu\.be)/\S+", re.IGNORECASE),
    ),
    ("Drive", re.compile(r"https?://(?:docs|drive)\.google\.com/\S+", re.IGNORECASE)),
    ("Notion", re.compile(r"https?://(?:www\.)?notion\.so/\S+", re.IGNORECASE)),
)

ANY_LINK = re.compile(r"https?://\S+", re.IGNORECASE)

# Discord decorates a title with markdown far more often than not - people bold
# or italicise the heading above a link. It is a heading either way.
_MARKDOWN = re.compile(r"(\*\*|\*|__|_|`|~~)")

# `<@123>` is a person; `@here` and `@everyone` are just as much a mention and
# just as little a heading. One became a card called "SOP: @here here's how to
# connect luna to your calendar".
_MENTION = re.compile(r"<@[!&]?\d+>|@here\b|@everyone\b", re.IGNORECASE)


@dataclass
class Sop:
    """One posted procedure, ready to file."""

    title: str
    kind: str
    url: str = ""
    body: str = ""
    posted_by: str = ""
    posted_on: date | None = None
    # Files that came with the message: a screenshot of the settings screen, a
    # voice note walking through it.
    images: tuple[str, ...] = ()
    audio: tuple[str, ...] = ()
    # Why a card has no real summary, when it doesn't. Said out loud rather
    # than left as an empty section nobody can explain.
    note: str = ""
    # Whether somebody typed a heading. When they didn't, the page's own title
    # is a far better name than "Drive SOP".
    named_by_hand: bool = True
    links: tuple[str, ...] = field(default_factory=tuple)

    @property
    def readable(self) -> bool:
        """Whether there is anything RYTE can actually turn into a summary."""
        return bool(self.kind == "YouTube" or self.body.strip() or self.images or self.url)


def strip_markdown(text: str) -> str:
    return _MARKDOWN.sub("", text or "").strip()


def find_title(text: str) -> str:
    """The heading somebody typed above the link.

    Almost always the first line that isn't the link itself - "How to Create
    Internal Ads LeadForm" over a Loom URL. Falls back to the first sentence of
    the body, because an SOP with no name is one nobody will find again.
    """
    for line in (text or "").splitlines():
        without_links = ANY_LINK.sub(" ", line)
        found = strip_markdown(" ".join(without_links.split()))
        # Drop a line that was only ever a link, and one that is only a mention.
        found = _MENTION.sub(" ", found).strip(" :-–—")
        if len(found) >= 3:
            return found[:120]
    return ""


def find_kind(text: str, *, images=(), audio=()) -> str:
    """What sort of SOP this is, by what came with it."""
    for name, pattern in SOURCES:
        if pattern.search(text or ""):
            return name
    if ANY_LINK.search(text or ""):
        return "Link"
    if audio:
        return "Voice note"
    if images:
        return "Screenshot"
    return "Written"


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_SECRET_WORDS = ("password", "passcode", "login", "credential", "log in", "pwd", "api key")


def looks_like_credentials(text: str) -> bool:
    """Whether a message is somebody sharing a login.

    The backfill filed one: an email, a password on the line under it, and
    "NEW LOOM LOGIN" beneath that. It is not a procedure, and a Notion card is
    a place a password outlives the conversation it was meant to die in.
    """
    lowered = (text or "").casefold()
    if not any(word in lowered for word in _SECRET_WORDS):
        return False
    return bool(_EMAIL.search(text or ""))


def find_sop(text: str, *, images=(), audio=()) -> Sop | None:
    """Read a posted message into an SOP, or None if there is nothing in it.

    A message with no link, no file and nothing but "nice one" is chatter, and
    filing chatter is how a library stops being worth searching.
    """
    body = strip_markdown(ANY_LINK.sub(" ", _MENTION.sub(" ", text or "")))
    body = "\n".join(line.strip() for line in body.splitlines() if line.strip())
    links = tuple(dict.fromkeys(ANY_LINK.findall(text or "")))
    images = tuple(images or ())
    audio = tuple(audio or ())

    if looks_like_credentials(text):
        return None

    # With nothing attached, the text itself has to look like a procedure
    # rather than a message. "will you pin this here so we have it quick just
    # incase" cleared a forty-character bar and became a card.
    if not links and not images and not audio and not _reads_like_a_procedure(body):
        return None

    title = find_title(text) or (body.splitlines()[0][:120] if body else "")
    named_by_hand = bool(title)
    if not title:
        # A placeholder until the page can be asked its own name. Three cards
        # came out of the backfill called "SOP: Drive SOP".
        title = f"{find_kind(text, images=images, audio=audio)} SOP"

    return Sop(
        title=title,
        named_by_hand=named_by_hand,
        kind=find_kind(text, images=images, audio=audio),
        url=links[0] if links else "",
        links=links,
        body=body,
        images=images,
        audio=audio,
    )


# Something written out as an SOP runs to a paragraph, or to numbered steps.
_STEPS = re.compile(r"(?:^|\n)\s*(?:\d{1,2}[.)]|[-*•])\s+", re.MULTILINE)

# Length alone can't tell "will you pin this here so we have it quick just
# incase" from a procedure of the same size. What can is who it is addressed
# to: a procedure describes what to do, a request asks somebody to do it.
_A_REQUEST = re.compile(
    r"^\s*(?:hey|hi|yo|plz|please|can|could|would|will|any(?:one|body)|does\s+any)\b"
    r"|\b(?:can|could|would|will)\s+(?:you|u|someone|somebody|anyone)\b",
    re.IGNORECASE,
)


def _reads_like_a_procedure(body: str) -> bool:
    text = (body or "").strip()
    if not text or _A_REQUEST.search(text) or text.rstrip().endswith("?"):
        return False
    if len(_STEPS.findall(text)) >= 2 and len(text) >= 60:
        return True
    return len(text) >= 110


def card_title(sop: "Sop", *, prefix: str = TITLE_PREFIX) -> str:
    """"SOP: How to Create Internal Ads LeadForm"."""
    name = " ".join((sop.title or "").split())
    return f"{prefix}: {name}" if name else prefix


# ----------------------------------------------------------- the database


def database_schema(name_property: str = "Name") -> dict:
    """The gallery's columns.

    `Summary` is a column rather than only page content on purpose: it is what
    "do we have an SOP about X" is matched against, and Notion will not search
    inside a page's blocks from a database query.
    """
    return {
        name_property: {"title": {}},
        "Link": {"url": {}},
        "Kind": {"rich_text": {}},
        "Summary": {"rich_text": {}},
        "Date": {"date": {}},
    }


EXTRA_COLUMNS = {
    "Link": {"url": {}},
    "Kind": {"rich_text": {}},
    "Summary": {"rich_text": {}},
    "Date": {"date": {}},
}


def map_properties(schema: dict, sop: "Sop", title: str, *, summary: str = "") -> dict:
    """Fill the columns this database actually has, and skip the ones it doesn't.

    The library is usually made by hand before RYTE sees it, so its columns are
    whatever made sense to whoever built it. Writing a property Notion doesn't
    know about fails the entire create - which is exactly what "Date is not a
    property that exists" was - so the database's own schema decides what gets
    sent rather than a shape assumed here.
    """
    properties: dict = {}
    used: set[str] = set()

    for column, spec in (schema or {}).items():
        # Notion answers with {"type": "rich_text", "rich_text": {...}}, but the
        # shape used to *create* a database is just {"rich_text": {}}. Reading
        # both means this works against a real library and against our own spec.
        kind = spec.get("type") or next(iter(spec), "")
        key = " ".join(column.split()).casefold()

        if kind == "title":
            properties[column] = {"title": [{"type": "text", "text": {"content": title[:2000]}}]}
        elif kind == "url" and _is(key, "link") and "link" not in used:
            if sop.url:
                properties[column] = {"url": sop.url}
                used.add("link")
        elif kind == "date":
            if sop.posted_on:
                properties[column] = {"date": {"start": sop.posted_on.isoformat()}}
        elif kind == "rich_text":
            if _is(key, "kind") and sop.kind and "kind" not in used:
                properties[column] = _rich(sop.kind)
                used.add("kind")
            elif _is(key, "summary") and summary.strip() and "summary" not in used:
                # Capped at 2000 by Notion, and the point of this column is
                # being searchable rather than complete.
                properties[column] = _rich(_plain(summary)[:1900])
                used.add("summary")
    return properties


def _is(column_key: str, role: str) -> bool:
    from . import recordings

    return any(alias in column_key for alias in recordings.COLUMN_ALIASES.get(role, (role,)))


def page_properties(sop: "Sop", title: str, *, summary: str = "") -> dict:
    """The row against the schema RYTE would have made. For tests and previews."""
    return map_properties(database_schema(), sop, title, summary=summary)


def _rich(text: str) -> dict:
    return {"rich_text": [{"type": "text", "text": {"content": (text or "")[:2000]}}]}


def _plain(text: str) -> str:
    """Markdown flattened, for a property that has no formatting to show it."""
    return " ".join(_MARKDOWN.sub("", text or "").split())


def page_blocks(sop: "Sop", summary: str = "") -> list[dict]:
    """What's inside the card when you open it."""
    from . import notion, recordings

    blocks: list[dict] = []
    if sop.url:
        blocks.append(notion.bookmark(sop.url))

    details = [f"Kind: {sop.kind}"]
    if sop.posted_by:
        details.append(f"Posted by: {sop.posted_by}")
    if len(sop.links) > 1:
        details.append(f"{len(sop.links)} links")
    if sop.audio:
        details.append(f"{len(sop.audio)} voice note(s)")
    blocks.append(notion.paragraph(" · ".join(details), markdown=False))

    for image in sop.images[:5]:
        blocks.append(notion.image(image))

    if summary.strip():
        blocks.append(notion.heading("Summary"))
        blocks.extend(recordings.summary_blocks(summary))
    elif sop.note:
        blocks.append(notion.paragraph(sop.note))

    if sop.body.strip():
        blocks.append(notion.heading("As posted"))
        for line in sop.body.splitlines():
            if line.strip():
                blocks.append(notion.paragraph(line.strip()))
    return blocks


# ------------------------------------------------- asking for one back


# Words that say somebody is asking, rather than saying which SOP.
_ASKING = {
    "a", "about", "an", "and", "any", "anything", "are", "back", "can", "do",
    "does", "find", "for", "get", "give", "got", "guide", "have", "hey", "how",
    "i", "is", "it", "know", "link", "me", "of", "on", "one", "our", "please",
    "procedure", "pull", "ryte", "send", "share", "show", "sop", "sops",
    "standard", "the", "there", "to", "us", "want", "was", "we", "what",
    "where", "which", "with", "you", "your",
    # Prepositions and articles. Every word has to match for a page to count,
    # so a question carrying "up" or "in" was being asked to find them too.
    "at", "by", "from", "in", "into", "on", "up", "as", "or", "that", "this",
}


def wanted_topic(text: str) -> str:
    """"do we have an SOP about lead forms" -> "lead forms"."""
    cleaned = ANY_LINK.sub(" ", text or "")
    cleaned = _MENTION.sub(" ", cleaned)
    kept = [
        word for word in re.split(r"[^\w'-]+", strip_markdown(cleaned))
        if word and word.casefold() not in _ASKING
    ]
    return " ".join(kept).strip()


def matching_rows(rows: list[dict], topic: str, *, limit: int = 5) -> list[tuple[str, str, str]]:
    """(title, card, link) for SOPs matching what was asked about.

    Titles first, then summaries. Somebody asking about "lead forms" should get
    the card called "How to Create Internal Ads LeadForm" ahead of one that
    only mentions lead forms in passing - but they should still get the second
    one, because half the time that is the answer.
    """
    from . import recordings

    words = [word.casefold() for word in (topic or "").split()]
    found = []
    for row in rows or []:
        title = recordings.row_title(row)
        if not title:
            continue
        entry = (title, str(row.get("url") or ""), _row_text(row, "link"))
        if not words:
            found.append((2, entry))
            continue
        haystack_title = _searchable(title)
        haystack_all = _searchable(
            f"{title} {_row_text(row, 'summary')} {_row_text(row, 'kind')}"
        )
        if all(_hit(word, haystack_title) for word in words):
            found.append((0, entry))
        elif all(_hit(word, haystack_all) for word in words):
            found.append((1, entry))

    found.sort(key=lambda pair: pair[0])
    return [entry for _, entry in found][:limit]


def _searchable(text: str) -> str:
    """Text flattened so a search can find words inside a run-on name.

    "LeadForm" is one word to a human and two to a search, so the haystack
    carries both: the text as written, and the text with its separators
    removed. Without it "lead forms" misses a card called "How to Create
    Internal Ads LeadForm", which is the card.
    """
    lowered = " ".join((text or "").split()).casefold()
    return f"{lowered} {re.sub(r'[^a-z0-9]', '', lowered)}"


def _hit(word: str, haystack: str) -> bool:
    """Whether a word appears, give or take the plural.

    Nobody types the exact form somebody else used. "lead forms" has to find
    "lead form", and "SOP for lead ordering" has to find "Lead Order".
    """
    word = word.strip()
    if not word:
        return True
    if word in haystack:
        return True
    for stem in _stems(word):
        if stem in haystack:
            return True
    # The other direction: "form" typed, "forms" written down.
    return any(f"{word}{suffix}" in haystack for suffix in ("s", "es"))


def _stems(word: str) -> list[str]:
    """A word with its ending taken off, the ways English takes them off.

    English doubles the consonant before -ing, so "setting" reduces to "sett"
    and then to "set" - which is what "Setup" is made of. Without that second
    step, "an SOP setting up dedicated LP" missed a page called "How To Setup
    Dedicated LP", which is the page.
    """
    found = []
    for suffix in ("s", "es", "ing", "ed", "er"):
        if not word.endswith(suffix) or len(word) - len(suffix) < 3:
            continue
        stem = word[: -len(suffix)]
        found.append(stem)
        # "sett" -> "set", "runn" -> "run", "stopp" -> "stop".
        if len(stem) >= 4 and stem[-1] == stem[-2] and stem[-1].isalpha():
            found.append(stem[:-1])
    return found


def _row_text(row: dict, role: str) -> str:
    """A property's text, whatever the column happens to be called.

    The gallery may be made by hand before RYTE ever sees it, so columns are
    matched by what they hold rather than by an exact name.
    """
    from . import recordings

    names = recordings.COLUMN_ALIASES.get(role, (role,))
    for name, value in (row.get("properties") or {}).items():
        if " ".join(name.split()).casefold() not in names:
            continue
        kind = value.get("type")
        if kind == "url":
            return str(value.get("url") or "")
        if kind == "rich_text":
            return "".join(part.get("plain_text", "") for part in value.get("rich_text") or [])
    return ""


# ------------------------------------------- what has already been filed

# Backfilling a channel means reading messages RYTE has already seen, so it has
# to know which ones. Discord's message id is the identity: it never changes and
# it is unique across every channel.

_PREFIX = "sop-message"


def message_key(message_id) -> str:
    return f"{_PREFIX}:{message_id}"


def already_filed(message_id, *, path=None) -> bool:
    from . import recordings

    return message_key(message_id) in recordings.filed_ids(path)


def remember(message_id, *, path=None) -> None:
    from . import recordings

    recordings.remember_filed(message_key(message_id), path)


# ------------------------------------- the SOPs that were already written

# The old library is a Notion page with a great deal inside it, and none of it
# needs to be held at once. Each page is read once, reduced to a title, a link
# and a couple of lines, and that index is what questions are matched against.
# Answering then costs a local lookup and nothing else.

INDEX_NAME = "sop-index.json"


def _index_path(path=None):
    from pathlib import Path

    from .state import _state_dir

    return Path(path) if path else _state_dir() / INDEX_NAME


def load_index(path=None) -> list[dict]:
    import json

    found = _index_path(path)
    if not found.exists():
        return []
    try:
        data = json.loads(found.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [entry for entry in data if isinstance(entry, dict)] if isinstance(data, list) else []


def save_index(entries: list[dict], path=None) -> None:
    import json

    found = _index_path(path)
    found.parent.mkdir(parents=True, exist_ok=True)
    found.write_text(json.dumps(entries, indent=1), encoding="utf-8")


def merge_index(existing: list[dict], fresh: list[dict]) -> list[dict]:
    """Newly read pages replace what was there, by page id.

    Re-reading the library should update it rather than double it, and a page
    that was edited since last time should end up with the newer summary.
    """
    by_id = {entry.get("id"): entry for entry in existing if entry.get("id")}
    for entry in fresh:
        if entry.get("id"):
            by_id[entry["id"]] = entry
    return list(by_id.values())


def index_matches(index: list[dict], topic: str, *, limit: int = 5) -> list[tuple[str, str, str]]:
    """(title, url, "") for indexed pages matching a topic.

    The same rules the gallery search uses - titles first, then summaries, and
    forgiving about plurals - so a question finds an old SOP and a new one
    without being asked differently.
    """
    words = [word.casefold() for word in (topic or "").split()]
    found = []
    for entry in index or []:
        title = str(entry.get("title") or "")
        if not title:
            continue
        item = (title, str(entry.get("url") or ""), "")
        if not words:
            found.append((2, item))
            continue
        in_title = _searchable(title)
        in_all = _searchable(f"{title} {entry.get('summary', '')}")
        if all(_hit(word, in_title) for word in words):
            found.append((0, item))
        elif all(_hit(word, in_all) for word in words):
            found.append((1, item))

    found.sort(key=lambda pair: pair[0])
    return [item for _, item in found][:limit]
