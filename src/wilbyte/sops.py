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
        found = re.sub(r"<@[!&]?\d+>", " ", found).strip(" :-–—")
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


def find_sop(text: str, *, images=(), audio=()) -> Sop | None:
    """Read a posted message into an SOP, or None if there is nothing in it.

    A message with no link, no file and nothing but "nice one" is chatter, and
    filing chatter is how a library stops being worth searching.
    """
    body = strip_markdown(ANY_LINK.sub(" ", re.sub(r"<@[!&]?\d+>", " ", text or "")))
    body = "\n".join(line.strip() for line in body.splitlines() if line.strip())
    links = tuple(dict.fromkeys(ANY_LINK.findall(text or "")))
    images = tuple(images or ())
    audio = tuple(audio or ())

    if not links and not images and not audio and len(body) < 40:
        return None

    title = find_title(text) or (body.splitlines()[0][:120] if body else "")
    if not title:
        title = f"{find_kind(text, images=images, audio=audio)} SOP"

    return Sop(
        title=title,
        kind=find_kind(text, images=images, audio=audio),
        url=links[0] if links else "",
        links=links,
        body=body,
        images=images,
        audio=audio,
    )


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
}


def wanted_topic(text: str) -> str:
    """"do we have an SOP about lead forms" -> "lead forms"."""
    cleaned = ANY_LINK.sub(" ", text or "")
    cleaned = re.sub(r"<@[!&]?\d+>", " ", cleaned)
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
    for suffix in ("s", "es", "ing", "ed"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            if word[: -len(suffix)] in haystack:
                return True
    # The other direction: "form" typed, "forms" written down.
    return any(f"{word}{suffix}" in haystack for suffix in ("s", "es"))


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
