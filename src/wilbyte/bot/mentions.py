"""Turn an @mention into a pipeline action.

    @Wil Byte https://youtube.com/playlist?list=PL... 3          -> run, limit 3
    @Wil Byte draft https://youtu.be/abc                         -> run as draft
    @Wil Byte plan https://youtube.com/playlist?list=PL...       -> plan
    @Wil Byte status                                             -> status
    @Wil Byte cover Aged, Fresh, Premium | Why Agents Stall      -> cover

Deliberately forgiving about word order - a link plus a number in any
arrangement is the common case, and it should just work.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MENTION_RE = re.compile(r"<@!?\d+>")
ROLE_MENTION_RE = re.compile(r"<@&\d+>")
URL_RE = re.compile(r"https?://(?:www\.|m\.)?(?:youtube\.com|youtu\.be)/\S+", re.IGNORECASE)
BARE_PLAYLIST_RE = re.compile(r"\b((?:PL|UU|OL|FL|RD)[A-Za-z0-9_-]{10,})\b")
# "3", "3 posts", "x3", "limit 3", "next 3". The x-prefixed form needs its own
# branch because there is no word boundary between the "x" and the digit.
COUNT_RE = re.compile(
    r"\bx(\d{1,2})\b"
    r"|(?:\blimit\s+|\bnext\s+)(\d{1,2})\b"
    r"|\b(\d{1,2})\b(?:\s*(?:posts?|videos?|vids?))?"
)

ACTION_WORDS = {
    "plan": "plan",
    "preview": "plan",
    "queue": "plan",
    "status": "status",
    "state": "status",
    "ledger": "status",
    "cover": "cover",
    "thumbnail": "cover",
    "thumb": "cover",
    "help": "help",
    "commands": "help",
    "hi": "help",
    "hello": "help",
    "hey": "help",
}

MODE_WORDS = {
    "draft": "draft",
    "drafts": "draft",
    "dry": "preview",
    "dryrun": "preview",
    "local": "preview",
    "test": "preview",
}

FORCE_WORDS = {"force", "again", "redo", "rerun", "anyway"}


@dataclass
class MentionRequest:
    action: str  # run | plan | status | cover | help
    source: str | None = None
    limit: int = 1
    mode: str = "scheduled"
    force: bool = False
    kicker: str | None = None
    headline: str | None = None


def parse(content: str, *, max_batch: int = 10) -> MentionRequest:
    """Read a mention's text into a request. Never raises - falls back to help."""
    text = ROLE_MENTION_RE.sub(" ", MENTION_RE.sub(" ", content or "")).strip()
    lowered = text.lower()

    # `cover` takes free text, so handle it before the link/number extraction.
    if _first_action_word(lowered) == "cover":
        return _parse_cover(text)

    url_match = URL_RE.search(text)
    source = url_match.group(0).rstrip(".,;)>") if url_match else None
    if not source:
        bare = BARE_PLAYLIST_RE.search(text)
        source = bare.group(1) if bare else None

    action = _first_action_word(lowered)
    if action in ("status", "help"):
        return MentionRequest(action=action)

    if not source:
        # A mention with no link and no recognised verb is a greeting or a mistake.
        return MentionRequest(action="help")

    if action != "plan":
        action = "run"

    return MentionRequest(
        action=action,
        source=source,
        limit=_parse_limit(text, source, max_batch=max_batch),
        mode=_parse_mode(lowered),
        force=any(word in lowered.split() for word in FORCE_WORDS),
    )


def _first_action_word(lowered: str) -> str | None:
    for word in re.findall(r"[a-z]+", lowered):
        if word in ACTION_WORDS:
            return ACTION_WORDS[word]
    return None


def _parse_mode(lowered: str) -> str:
    for word in re.findall(r"[a-z]+", lowered):
        if word in MODE_WORDS:
            return MODE_WORDS[word]
    return "scheduled"


def _parse_limit(text: str, source: str | None, *, max_batch: int) -> int:
    """Find a count, ignoring digits that are part of the URL itself."""
    haystack = text.replace(source, " ") if source else text
    match = COUNT_RE.search(haystack)
    if not match:
        return 1
    found = next(group for group in match.groups() if group is not None)
    return max(1, min(int(found), max_batch))


def _parse_cover(text: str) -> MentionRequest:
    """`cover <kicker> | <headline>`, or a single line split at the last colon."""
    body = re.sub(r"\b(cover|thumbnail|thumb)\b", " ", text, count=1, flags=re.IGNORECASE).strip()

    if "|" in body:
        kicker, _, headline = body.partition("|")
    elif ":" in body:
        kicker, _, headline = body.rpartition(":")
    else:
        return MentionRequest(action="cover", kicker=None, headline=body.strip() or None)

    return MentionRequest(
        action="cover",
        kicker=kicker.strip() or None,
        headline=headline.strip() or None,
    )


HELP_TEXT = """**Hi, I'm Wil Byte** 🤖 — mention me with a YouTube link and I'll get to work.

**Things you can say**
> @Wil Byte `<playlist link>` **3** — write the next 3 posts
> @Wil Byte **draft** `<link>` — save them to GHL as drafts instead
> @Wil Byte **preview** `<link>` — build them locally, send nothing
> @Wil Byte **plan** `<link>` — just show me what's queued and when
> @Wil Byte **status** — what's posted, what's next
> @Wil Byte **cover** Aged, Fresh, Premium | Why Agents Stall — render a cover

Slash commands work too: `/run` `/plan` `/status` `/cover`.

I'll always show you the post before anything goes live — nothing reaches the \
blog until you click **Schedule it**."""
