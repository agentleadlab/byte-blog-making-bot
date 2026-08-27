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
    # One controlled experiment: does a future date alone schedule a post?
    "datetest": "datetest",
    # Which posts were written but never made it to GHL.
    "missed": "missed",
    "missing": "missed",
    "leftover": "missed",
    "leftovers": "missed",
    "unposted": "missed",
    # Drop calendar days held by posts that were deleted in GHL.
    "cleanup": "reconcile",
    "reconcile": "reconcile",
    "tidy": "reconcile",
    "sync": "reconcile",
    # What Zoom and Fathom will actually hand over. A call that can't be found
    # looks the same whether the app is misconfigured or the recording simply
    # belongs to someone else's account - this is what tells the two apart.
    "calls": "calls",
    "zoom": "calls",
    "visible": "calls",
    # Sweep Zoom and Fathom now rather than waiting for the next check.
    "sweep": "sweep",
    "catchup": "sweep",
    # Read the SOP channel's history and file what was posted before RYTE
    # was watching it.
    "backfill": "backfill",
    "history": "backfill",
    # Read the SOP page that existed before RYTE did.
    "index": "index",
    "reindex": "index",
    # Which days the blog goes out on, and re-laying what is already booked
    # when that changes.
    "weekends": "weekends",
    "weekend": "weekends",
    "everyday": "weekends",
    "rearrange": "rearrange",
    "reschedule": "rearrange",
    "reshuffle": "rearrange",
    "relay": "rearrange",
    "compact": "rearrange",
    # The daily Trello board. Everything that touches it is said with the word
    # "trello" in front - `trello board`, `trello rollover` - so the board's
    # commands read as one set rather than as loose words that happen to exist.
    # The bare words still work, because breaking what already worked to
    # rename it would be its own small betrayal.
    "board": "board",
    "rollover": "rollover",
    "carryover": "rollover",
    "move": "move",
    "agents": "agents",
    "agent": "agents",
    # The New Agent cards sitting in Done that nobody ticked. RYTE looks twice
    # an afternoon on his own; this is for looking now.
    "unticked": "unticked",
    "unchecked": "unticked",
    # The nightly clear-out of the aged-leads list.
    "archive": "archive",
    # Agents set up on leads they did not order.
    "setups": "setups",
    "setup": "setups",
    "mismatch": "setups",
    # Find out what GHL's update endpoint actually accepts, on a throwaway
    # draft rather than on fifteen live articles.
    "probe": "probe",
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

# "post this today". Today's 10:00 slot has almost always gone by the time
# anybody says it, so the day would be skipped and the answer to "can we get
# this out this afternoon" would be "no, Monday". These words say otherwise.
TODAY_WORDS = {"today", "now", "asap", "tonight", "immediately"}


def wants_today(text: str) -> bool:
    """Whether the message asks for today, links ignored.

    A URL with "now" in it is not somebody asking for today, and neither is
    a video called "Start Now" - only a word they actually typed counts.
    """
    return bool(TODAY_WORDS & set(re.findall(r"[a-z]+", _without_links(text).lower())))


# Command-position only - see the note at the call site.
START_WORDS = ("start", "starting", "resume", "from", "schedulefrom")

# Also command-position only, and for a sharper reason than START_WORDS: a
# YouTube link already means "write a blog post", so filing a recording has to
# be asked for explicitly or the two are indistinguishable.
RECORDING_WORDS = ("recording", "recordings", "salescall", "callrecording")

# Attach an image and get a permanent public URL back. Command-position only:
# "host" and "upload" turn up in ordinary copy briefs.
HOST_WORDS = ("host", "upload", "imageurl")

# Cut an interview into clips. Command-position only for the same reason as
# HOST_WORDS, and a sharper one: "segment your audience" and "clips for the
# reel" are things somebody asks a copywriter for, and reading either as a
# command would answer a copy brief with a transcript.
SEGMENT_WORDS = ("segment", "segments", "clips", "chapters", "cut")

# Send a held post out now instead of on the day it was booked for. Command
# position only, and for the plainest reason of all: "publish" and "post" are
# what the blog pipeline is *about*. "write me a post about X" must not push
# Monday's article live.
PUBLISH_WORDS = ("publish", "publishnow", "release", "golive")

WEEKEND_WORDS = ("weekends", "weekend", "everyday")

# Handing an SOP over from somewhere that isn't the SOP channel. Posting in
# #sop files it silently; saying so anywhere else has to work too, because the
# link is usually already in the conversation that produced it.
FILE_SOP_RE = re.compile(
    r"\b(?:add(?:\s+(?:this|it|that))?\s+(?:to|in|into)\s+(?:the\s+)?sops?"
    r"|(?:file|save|put|log)\s+(?:this|it|that)?\s*(?:as|in|into|under)\s+(?:an?\s+|the\s+)?sops?"
    r"|sop\s+this)\b",
    re.IGNORECASE,
)

# Switching the weekend on or off. The word has to come *after* "weekends",
# because "do we post on weekends?" is a question and the "on" in it means
# nothing - answering it by silently changing the schedule would be the worst
# kind of helpful.
ON_WORDS = {"on", "yes", "enable", "enabled", "include", "included", "true", "daily"}
OFF_WORDS = {"off", "no", "disable", "disabled", "exclude", "excluded", "false", "stop"}

# Said before the word instead: "turn on weekends", "include weekends".
_TURNS_ON = {"enable", "include", "add", "allow", "start"}
_TURNS_OFF = {"disable", "exclude", "remove", "drop", "stop", "skip"}


def weekend_switch(text: str) -> bool | None:
    """True for on, False for off, None when it's a question rather than a change."""
    words = [word.lower() for word in re.findall(r"[a-z]+", text or "")]
    if "everyday" in words or "daily" in words:
        return True

    where = next((i for i, word in enumerate(words) if word in ("weekend", "weekends")), None)
    if where is None:
        return None
    after = set(words[where + 1:])
    before = words[:where]
    turning = {word for word in before if word in _TURNS_ON | _TURNS_OFF}
    # "turn on" / "turn off", which reads as neither word alone.
    for first, second in zip(before, before[1:]):
        if first in ("turn", "switch", "set"):
            turning.add("enable" if second == "on" else "disable" if second == "off" else "")

    if after & ON_WORDS or turning & _TURNS_ON:
        return True
    if after & OFF_WORDS or turning & _TURNS_OFF:
        return False
    return None

# A Zoom or Fathom link has exactly one meaning here, so it doesn't need the
# verb the way a YouTube link does - that one already means "write a blog
# post", which is why `recording` exists at all. Posting a call link and
# getting the help text back is a trap with no upside.
RECORDING_URL_RE = re.compile(
    r"https?://[\w.-]*(?:zoom\.us|fathom\.video)/\S+", re.IGNORECASE
)


# Any link at all, not just the ones we act on - the point is to stop a URL's
# insides being read as instructions, whatever it points at.
ANY_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def _without_links(text: str) -> str:
    return ANY_URL_RE.sub(" ", text or "")


# Asking for a filed recording back. Both halves are required: "recording"
# alone is the filing command, and "send me the link" alone is about anything
# at all.
_WANTS = ("need", "find", "send", "share", "get", "where", "which", "show", "pull", "give", "want")

_THE_THING = ("recording", "recordings", "sales call", "call link", "notion", "video", "videos")

# Words that mean somebody wants copy written. "video" is an alias for the
# script format, so "need the video of Derrick Robison" came back as a written
# script about him - these are what tell the two apart.
_WRITING = {
    "write", "make", "draft", "create", "script", "copy", "hook", "vsl", "ad",
    "email", "sms", "post", "landing", "caption", "about",
}


# The formats RYTE can write. Naming one is what separates "how to write an
# sms" from "how to set up a blog" - the verb doesn't, because procedures are
# written and created too.
_COPY_FORMATS = {
    "sms", "email", "ad", "ads", "vsl", "script", "landing", "social",
    "caption", "hook", "copy",
}


# Asking the SOP library. "sop" is unambiguous enough to stand on its own -
# nobody says it by accident - so unlike a recording this needs no second half.
# Nobody says these by accident, so they settle it on their own.
_SOP_WORDS = ("sop", "sops", "standard operating", "procedure", "process for")

# How people actually ask when they don't use the word. "how do we" was
# recognised and "how to" was not, so "how to set up a blog" fell through to
# the help text. These are less certain, so a named format outranks them.
_ASKING_HOW = (
    "how do we", "how do i", "how to", "how can i", "how does", "where do i",
    "walk me through", "steps for", "steps to", "guide for", "guide to",
)


def _asks_for_an_sop(lowered: str) -> bool:
    without_links = _without_links(lowered)
    # "sop for the new hires <link>" with a link in it is somebody filing one.
    if ANY_URL_RE.search(lowered):
        return False

    if any(phrase in without_links for phrase in _SOP_WORDS):
        return True

    if not any(phrase in without_links for phrase in _ASKING_HOW):
        return False
    # "how to write an sms" wants copy. Only a named format decides it -
    # "create" and "make" are ordinary words in a procedure question, and
    # "how do we create a lead form" is asking for the SOP.
    return not set(re.findall(r"[a-z]+", without_links)) & _COPY_FORMATS


def _asks_for_a_card(lowered: str) -> bool:
    without_links = _without_links(lowered)
    words = set(re.findall(r"[a-z]+", without_links))
    if words & _WRITING:
        return False
    return bool(words & set(_WANTS)) and any(
        phrase in without_links for phrase in _THE_THING
    )


def _opens_with(text: str, words: tuple[str, ...]) -> bool:
    first = re.match(r"\s*([a-zA-Z]+)", text)
    return bool(first) and first.group(1).lower() in words


@dataclass
class MentionRequest:
    action: str  # run | plan | status | schedule | cover | write | learn | corpus | help
    source: str | None = None
    limit: int = 1
    mode: str = "scheduled"
    force: bool = False
    # Give up the 10:00 rule for today only. Ten in the morning has gone by
    # the time anybody decides they want something out today.
    today: bool = False
    kicker: str | None = None
    headline: str | None = None
    format_key: str | None = None  # write: which format; learn: which label
    brief: str | None = None
    # Every link in the message, in the order they were typed. `source` is the
    # first of them, kept because plan and check only ever act on one.
    sources: tuple[str, ...] = ()


def parse(content: str, *, max_batch: int = 10) -> MentionRequest:
    """Read a mention's text into a request. Never raises - falls back to help."""
    from ..formats import find, find_label

    text = ROLE_MENTION_RE.sub(" ", MENTION_RE.sub(" ", content or "")).strip()
    lowered = text.lower()
    # Command words are looked for in what was *typed*, not in the links. A
    # Zoom share link contains "zoom" and a blog URL can contain "status" or
    # "check" - words inside a URL are addresses, not instructions.
    action = _first_action_word(_without_links(lowered))

    # `cover` takes free text, so handle it before the link/number extraction.
    # "trello" names the board and then says what to do with it. On its own it
    # means "show me the board", which is what somebody typing just that wants.
    if _opens_with(text, ("trello",)):
        rest = _strip_word(text, ("trello",))
        after = _first_action_word(rest.lower())
        return MentionRequest(
            action=after
            if after in ("board", "rollover", "move", "agents", "unticked",
                         "archive", "setups")
            else "board",
            brief=rest,
            today=wants_today(rest),
        )

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

    if action == "datetest":
        # `datetest undo` puts the post back rather than running it again.
        return MentionRequest(action="datetest", brief=_strip_word(text, ("datetest",)))

    # "start" and "from" are ordinary words in a copy brief - "a script to start
    # the year strong" must not move the calendar. So they only count as the
    # command when they open the message, and everything after is the day.
    if _opens_with(text, START_WORDS):
        return MentionRequest(action="start", brief=_strip_word(text, START_WORDS))

    # Same rule, sharper reason: a blog post is the thing this bot makes, so
    # "publish" only means *send one out now* when it is the first word.
    if _opens_with(text, PUBLISH_WORDS) and not find_sources(text):
        return MentionRequest(action="publish", brief=_strip_word(text, PUBLISH_WORDS))

    # Asking *for* a recording, rather than handing one over. The difference is
    # a link: "recording <link>" files one, "need the recording for Derrick"
    # fetches one back out of the gallery.
    # Before the question, because "add to sop" contains the word that asks
    # one. Handing an SOP over and asking for one are opposite things.
    if FILE_SOP_RE.search(text):
        return MentionRequest(action="filesop", brief=FILE_SOP_RE.sub(" ", text, count=1).strip())

    if _asks_for_an_sop(lowered):
        return MentionRequest(action="findsop", brief=text)

    if _asks_for_a_card(lowered) and not RECORDING_URL_RE.search(text):
        return MentionRequest(action="findcall", brief=text)

    if _opens_with(text, RECORDING_WORDS):
        return MentionRequest(action="recording", brief=_strip_word(text, RECORDING_WORDS))

    if _opens_with(text, HOST_WORDS):
        return MentionRequest(action="host", brief=_strip_word(text, HOST_WORDS))

    # Before the format check: a YouTube link already means "write a blog post"
    # and a Zoom link already means "file this call", so the verb is what says
    # to cut one up instead.
    if _opens_with(text, SEGMENT_WORDS):
        call = RECORDING_URL_RE.search(text)
        return MentionRequest(
            action="segments",
            source=_find_source(text) or (call.group(0).rstrip(".,;)>") if call else None),
            brief=_strip_word(text, SEGMENT_WORDS),
        )

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

    sources = find_sources(text)

    if action == "calls":
        # With a link it answers the sharper question: not "what can you see"
        # but "why don't you recognise this one".
        found = RECORDING_URL_RE.search(text)
        return MentionRequest(action="calls", brief=found.group(0).rstrip(".,;)>") if found else "")

    # After the format check on purpose: "an email about our weekend sale"
    # writes copy, it doesn't change which days the blog goes out on.
    if action == "weekends":
        # The whole message, not the remainder: whether this is a change or a
        # question turns on where the words sit relative to each other.
        return MentionRequest(action="weekends", brief=text)

    if action in (
        "status", "schedule", "help", "fields", "reconcile", "missed", "sweep",
        "board", "rollover", "backfill", "index", "rearrange", "probe", "agents",
        "unticked", "archive", "setups",
    ):
        # The whole message travels: "rollover general" names which card, and
        # deciding that here would mean teaching this module the board's
        # vocabulary as well.
        return MentionRequest(action=action, brief=text, today=wants_today(text))

    # No YouTube link, but a call link: file it. Checked after the blog sources
    # so that a message carrying both still writes the post.
    if not sources and RECORDING_URL_RE.search(text):
        return MentionRequest(action="recording", brief=text)

    if not sources:
        # A mention with no link and no recognised verb is a greeting or a mistake.
        return MentionRequest(action="help")

    return MentionRequest(
        action="plan" if action == "plan" else "run",
        source=sources[0],
        sources=sources,
        limit=_parse_limit(text, sources, max_batch=max_batch),
        mode=_parse_mode(lowered),
        force=any(word in lowered.split() for word in FORCE_WORDS),
        today=wants_today(text),
    )


def _find_source(text: str) -> str | None:
    """The first YouTube link or playlist id in a message, if there is one."""
    found = find_sources(text)
    return found[0] if found else None


def find_sources(text: str) -> tuple[str, ...]:
    """Every link in the message, in order, without repeats.

    Pasting a dozen links at once is how a week of posts actually arrives -
    they come out of a spreadsheet, not a playlist. Duplicates are dropped
    because a list copied twice should still mean one post per video.
    """
    found: list[str] = []
    for match in URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;)>")
        if url not in found:
            found.append(url)
    if not found:
        for match in BARE_PLAYLIST_RE.finditer(text):
            if match.group(1) not in found:
                found.append(match.group(1))
    return tuple(found)


_ANY_URL = re.compile(r"https?://\S+", re.IGNORECASE)


def _first_format_word(text: str, find) -> object | None:
    """The first word that names a copy format, e.g. sms / email / fb / vsl.

    Links are stripped first. `fathom.video` contains "video", which is an
    alias for the script format, so a bare Fathom link was being read as a
    request to write one - and any URL with "ad" or "social" in it would have
    done the same. A word only counts when someone actually typed it.
    """
    for word in re.findall(r"[a-zA-Z]+", _ANY_URL.sub(" ", text)):
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


def _parse_limit(text: str, sources: tuple[str, ...], *, max_batch: int) -> int:
    """Find a count, ignoring digits that are part of the links themselves.

    With several links and no number, the count *is* the number of links -
    pasting twelve of them and getting one post back would be absurd. A number
    still wins when one is given, so "<12 links> 3" builds three.
    """
    haystack = text
    for source in sources:
        haystack = haystack.replace(source, " ")

    match = COUNT_RE.search(haystack)
    if match:
        found = next(group for group in match.groups() if group is not None)
        return max(1, min(int(found), max_batch))
    return max(1, min(len(sources) or 1, max_batch))


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
> @RYTE `<link>` `<link>` `<link>` — paste as many as you like, one post each
> @RYTE **draft** `<link>` — save to GHL as drafts instead
> @RYTE **preview** `<link>` — build locally, send nothing
> @RYTE **plan** `<link>` — what's queued and when
> @RYTE **schedule** — every post still to go out, and the day it lands
> @RYTE **status** — what's posted, what's next
> @RYTE **cover** Aged, Fresh, Premium | Why Agents Stall
> @RYTE **check** `<link>` — test my GHL and YouTube connections
> @RYTE **segment** `<link>` — cut an interview into clips with timestamps,
> YT titles, website sections and both descriptions (nothing under 4 minutes).
> Works on a Zoom, Fathom or YouTube link, or reply to the message with it
> @RYTE **segment Antonio Bohorquez** — by who's on it. Zoom hands out a
> different share token than its API returns, so the name is what matches
> With `SEGMENTS_DOC_ID` set I write them into the Google Doc too
> @RYTE **calls** — which Zoom and Fathom recordings I can actually read
> @RYTE **fields** — what GHL is really storing on each post
> @RYTE **start** Aug 18 — don't schedule anything before that day
> @RYTE **weekends** on / off — whether Saturday and Sunday are posting days
> @RYTE **rearrange** — pull everything booked onto the earliest days free
> Add **today** to any of those — `@RYTE <link> today` — to use today's
> remaining hours instead of waiting for tomorrow's 10am
> @RYTE **publish** monday — send that day's post out now instead
> @RYTE **cleanup** — free up days held by posts you deleted in GHL
> @RYTE **probe** — ask GHL what it accepts on an update, on a throwaway draft

**Sales call recordings**
> Paste a Zoom or Fathom link — I'll file it in Notion
> @RYTE **recording** `<link>` — same thing, said out loud
> Or reply to the message with the link and just say @RYTE **recording**
> @RYTE **Sales: Derrick Robison** `<link>` — names the card and finds the call
> @RYTE **need the recording for Derrick Robison** — I'll send the card back
> New calls file themselves every 15 minutes — @RYTE **sweep** does it now

**SOPs**\n> Post a link in the SOP channel and I'll file it with a summary
> @RYTE **add to sop** `<link>` — file one from anywhere else\n> @RYTE **do we have an SOP for lead forms?** — I'll find it\n> @RYTE **backfill** — file everything already posted in the channel\n> @RYTE **index** — read the old SOP page so I can answer on it too\n\n**The daily board**
> @RYTE **trello board** — what's on the board today, and what's missing
> @RYTE **trello move today** / **move quality check** / **move done**
> @RYTE **trello agents** — file the new agents waiting in In Que
> @RYTE **trello unticked** — New Agent cards in Done nobody has marked complete
> @RYTE **trello archive** — archive the ticked cards in Aged Leads Order Done
> @RYTE **trello setups** — agents set up on leads they didn't order
> Each one shows which cards would move and waits for the button
> @RYTE **trello rollover** — carry tonight's unfinished items to tomorrow\n> @RYTE **trello rollover general** — one card only, to try it on\n> @RYTE **trello rollover yesterday** — carry a day you held back
> @RYTE **trello rollover skip ads** — keep that card's items off tomorrow tonight
> @RYTE **trello rollover unskip ads** — carry it after all\n> On its own it walks itself: 6am setup card, 9am to Today, 6pm to Quality Check, 8:30pm carry over then Done, 10pm archive\n> Unticked agents in Done get chased at 3:30, 5:30, 6:30 and 7:30
> @RYTE **host** — attach an image, get a permanent public link back
> @RYTE **missed** — posts I wrote but never got an answer on

If YouTube won't give me a transcript, paste it out of the video yourself and \
attach it (`.txt` or `.vtt`) along with the link — I'll write from that.

The more past copy you give me, the more it'll sound like you. Nothing reaches \
the blog until you click **Schedule it**."""
