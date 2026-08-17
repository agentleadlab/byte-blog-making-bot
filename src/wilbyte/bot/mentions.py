"""Turn an @mention into a pipeline action.

    @RYTE https://youtube.com/playlist?list=PL... 3          -> run, limit 3
    @RYTE draft https://youtu.be/abc                         -> run as draft
    @RYTE plan https://youtube.com/playlist?list=PL...       -> plan
    @RYTE status                                             -> status
    @RYTE cover Aged, Fresh, Premium | Why Agents Stall      -> cover

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
    # What is actually on the calendar, as opposed to how many days are taken.
    "schedule": "schedule",
    "scheduled": "schedule",
    "calendar": "schedule",
    "upcoming": "schedule",
    "cover": "cover",
    "thumbnail": "cover",
    "thumb": "cover",
    "learn": "learn",
    "train": "learn",
    "remember": "learn",
    "ingest": "learn",
    "study": "learn",
    "corpus": "corpus",
    "library": "corpus",
    "memory": "corpus",
    "knowledge": "corpus",
    # What GHL is really storing on each post, for when it accepts a schedule
    # date and then doesn't keep it.
    "fields": "fields",
    "raw": "fields",
    "inspect": "fields",
    # Note: "test" stays a MODE word (preview), not a check alias.
    "check": "check",
    "diagnose": "check",
    "doctor": "check",
    "checkup": "check",
    "help": "help",
    "commands": "help",
    "hi": "help",
    "hello": "help",
    "hey": "help",
}

# Words that introduce a brief and shouldn't survive into it.
_BRIEF_LEAD_INS = re.compile(
    r"^(?:write|make|draft|give|create|do|need|want)\s+(?:me\s+)?(?:an?\s+|some\s+)?",
    re.IGNORECASE,
)
_BRIEF_CONNECTORS = re.compile(r"^(?:copy|about|for|on|re|that|to)\b[:,]?\s*", re.IGNORECASE)

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
    action: str  # run | plan | status | schedule | cover | write | learn | corpus | help
    source: str | None = None
    limit: int = 1
    mode: str = "scheduled"
    force: bool = False
    kicker: str | None = None
    headline: str | None = None
    format_key: str | None = None  # write: which format; learn: which label
    brief: str | None = None


def parse(content: str, *, max_batch: int = 10) -> MentionRequest:
    """Read a mention's text into a request. Never raises - falls back to help."""
    from ..formats import find, find_label

    text = ROLE_MENTION_RE.sub(" ", MENTION_RE.sub(" ", content or "")).strip()
    lowered = text.lower()
    action = _first_action_word(lowered)

    # `cover` takes free text, so handle it before the link/number extraction.
    if action == "cover":
        return _parse_cover(text)

    if action == "learn":
        # The label is optional: "learn sms" with files attached.
        remainder = _strip_word(text, ("learn", "train", "remember", "ingest", "study"))
        return MentionRequest(
            action="learn",
            format_key=find_label(remainder.split()[0]) if remainder.split() else None,
        )

    if action == "corpus":
        return MentionRequest(action="corpus")

    if action == "check":
        # A link is optional, but with one the check can prove YouTube works.
        return MentionRequest(action="check", source=_find_source(text))

    # A format word means "write me one of these", and wins over a link so that
    # "email about the new playlist <link>" writes an email, not a blog post.
    fmt = _first_format_word(text, find)
    if fmt:
        return MentionRequest(
            action="write",
            format_key=fmt.key,
            brief=_extract_brief(text, fmt),
        )

    source = _find_source(text)

    if action in ("status", "schedule", "help", "fields"):
        return MentionRequest(action=action)

    if not source:
        # A mention with no link and no recognised verb is a greeting or a mistake.
        return MentionRequest(action="help")

    return MentionRequest(
        action="plan" if action == "plan" else "run",
        source=source,
        limit=_parse_limit(text, source, max_batch=max_batch),
        mode=_parse_mode(lowered),
        force=any(word in lowered.split() for word in FORCE_WORDS),
    )


def _find_source(text: str) -> str | None:
    """The YouTube link or bare playlist id in a message, if there is one."""
    url_match = URL_RE.search(text)
    if url_match:
        return url_match.group(0).rstrip(".,;)>")
    bare = BARE_PLAYLIST_RE.search(text)
    return bare.group(1) if bare else None


def _first_format_word(text: str, find) -> object | None:
    """The first word that names a copy format, e.g. sms / email / fb / vsl."""
    for word in re.findall(r"[a-zA-Z]+", text):
        fmt = find(word)
        if fmt:
            return fmt
    return None


def _extract_brief(text: str, fmt) -> str:
    """Everything the user said minus the format word and any lead-in verb."""
    pattern = re.compile(
        r"\b(?:" + "|".join(re.escape(w) for w in (fmt.key, *fmt.aliases)) + r")\b",
        re.IGNORECASE,
    )
    brief = re.sub(r"\s+", " ", pattern.sub(" ", text, count=1)).strip()
    brief = _BRIEF_LEAD_INS.sub("", brief).strip()
    brief = _BRIEF_CONNECTORS.sub("", brief).strip()
    return brief.strip(" -–—:,")


def _strip_word(text: str, words: tuple[str, ...]) -> str:
    pattern = re.compile(r"\b(?:" + "|".join(words) + r")\b", re.IGNORECASE)
    return re.sub(r"\s+", " ", pattern.sub(" ", text, count=1)).strip()


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


HELP_TEXT = """**Hi, I'm RYTE** 🤖 — I write copy in Agent Lead Lab's voice.

**Write me something**
> @RYTE **sms** about the OTP leads going live Monday
> @RYTE **email** we're raising aged lead prices next month
> @RYTE **ad** for agents stuck at 20 leads a week
> @RYTE **script** hook for a reel on cost per booked appointment
> Also: **landing**, **social**

**Teach me your voice** — attach files and say
> @RYTE **learn** — .txt, .md, .csv, .json; add a word like `sms` to label them
> @RYTE **corpus** — what I've learned so far

**Blog posts from YouTube**
> @RYTE `<playlist link>` **3** — write the next 3 posts
> @RYTE **draft** `<link>` — save to GHL as drafts instead
> @RYTE **preview** `<link>` — build locally, send nothing
> @RYTE **plan** `<link>` — what's queued and when
> @RYTE **schedule** — every post still to go out, and the day it lands
> @RYTE **status** — what's posted, what's next
> @RYTE **cover** Aged, Fresh, Premium | Why Agents Stall
> @RYTE **check** `<link>` — test my GHL and YouTube connections
> @RYTE **fields** — what GHL is really storing on each post

If YouTube won't give me a transcript, paste it out of the video yourself and \
attach it (`.txt` or `.vtt`) along with the link — I'll write from that.

The more past copy you give me, the more it'll sound like you. Nothing reaches \
the blog until you click **Schedule it**."""
