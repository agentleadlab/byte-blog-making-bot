"""Cut an interview transcript into publishable clips.

This replaces the step where Franklin pastes a transcript into Claude with the
segmenting prompt and copies the answer out by hand.

Two things are deliberately not left to the model. The boilerplate - the
strategy-session link, the two follow links, the Like/Comment/Subscribe line -
is appended here verbatim, because a model asked to reproduce four URLs
across a dozen segments will eventually get one subtly wrong and nobody
proofreads a link that looks right. And the four-minute floor is arithmetic on
the timestamps rather than a rule the model is asked to respect: a clip that
comes back at 3:40 is dropped here, and said out loud, instead of being
published because the instructions asked nicely.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, REPO_ROOT
from .youtube import Cue, length_of, seconds_of, timestamp

PROMPT_PATH = REPO_ROOT / "prompts" / "segmenting.md"

# The ten places a clip can be filed on the site. Spelled exactly as they are
# on the website, because this string is pasted straight into the doc.
SECTIONS = (
    "Agent Success Full Interviews",
    "Mortgage Protection Training",
    "Final Expense Training",
    "IUL Training",
    "Veteran Training",
    "The Blueprint To Building Your Own Insurance System",
    "Why Agent Lead Lab",
    "Agent's Expectations",
    "Effective Strategies For Prospect Engagement",
    "Aged Leads",
)

# Under this a clip does not get published, so it does not get emitted either.
MIN_SECONDS = 4 * 60

# Goes on the end of every YouTube description, exactly as written.
BOILERPLATE = (
    "If you're an agency owner looking to make a million dollars a month, and "
    "build an internal lead system for your entire agency apply here: "
    "https://agentleadlab.com/strategysession\n"
    "VISIT US https://agentleadlab.com/\n"
    "Follow Us on Instagram https://www.instagram.com/agentleadlab_/\n"
    "👍 Like • 💬 Comment • 🔔 Subscribe"
)

ALWAYS_TAGGED = "agentleadlab"


class SegmentError(RuntimeError):
    """Raised when segmenting fails or comes back unusable.

    `detail` carries evidence too long for the error line - what was actually
    compared when a link didn't match, say. "I couldn't find that recording"
    has three quite different causes and they are indistinguishable from
    Discord unless the strings are printed.
    """

    def __init__(self, message: str, *, detail: str = ""):
        super().__init__(message)
        self.detail = detail


@dataclass
class Segment:
    """One clip, ready to paste."""

    start: float
    end: float
    yt_title: str
    website_section: str
    hook: str
    bullets: list[str] = field(default_factory=list)
    closing: str = ""
    hashtags: list[str] = field(default_factory=list)
    website_description: str = ""
    long_form: bool = False

    @property
    def seconds(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def range(self) -> str:
        # An en dash between the two, the way the doc has always had it.
        return f"{timestamp(self.start)}–{timestamp(self.end)}"

    @property
    def heading(self) -> str:
        kind = "LONG-FORM / FULL INTERVIEW" if self.long_form else "SEGMENT"
        return f"{kind} ({self.range}) — {length_of(self.seconds)}"

    @property
    def yt_description(self) -> str:
        """The hook, the bullets, the closing line, then the fixed block."""
        parts = [self.hook.strip()]
        if self.bullets:
            parts.append("\n".join(f"• {bullet}" for bullet in self.bullets))
        if self.closing.strip():
            parts.append(self.closing.strip())
        parts.append(BOILERPLATE)
        parts.append(" ".join(f"#{tag}" for tag in self.hashtags))
        return "\n\n".join(part for part in parts if part)

    def as_text(self) -> str:
        """The whole entry in the shape it goes into the doc."""
        return (
            f"{self.heading}\n"
            f"{self.yt_title} (YT Title)\n\n"
            f"{self.website_section} (Website section)\n\n"
            f"(YT Description) {self.yt_description}\n\n"
            f"(Website Description) {self.website_description}"
        )


SEGMENT_TOOL = {
    "name": "emit_segments",
    "description": "Return the long-form entry and every clip cut from the interview.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "One paragraph on the interview as a whole - who the guest is "
                    "and what ground the conversation covers. Sits above everything else."
                ),
            },
            "pull_quote": {
                "type": "string",
                "description": "The single best line the guest says, verbatim, no quote marks.",
            },
            "segments": {
                "type": "array",
                "description": (
                    "The long-form entry first, then the clips in the order they "
                    "happen. Nothing shorter than four minutes."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["long-form", "segment"],
                            "description": "'long-form' for the whole interview, once, first.",
                        },
                        "start": {
                            "type": "string",
                            "description": "HH:MM:SS, copied from a line in the transcript.",
                        },
                        "end": {
                            "type": "string",
                            "description": "HH:MM:SS, copied from a line in the transcript.",
                        },
                        "yt_title": {"type": "string"},
                        "website_section": {"type": "string", "enum": list(SECTIONS)},
                        "hook": {
                            "type": "string",
                            "description": "The opening 1-3 sentences of the YouTube description.",
                        },
                        "bullets": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "3-8 things the clip covers. No bullet characters, no trailing punctuation.",
                            "minItems": 3,
                        },
                        "closing": {
                            "type": "string",
                            "description": "One short line after the bullets. Omit rather than pad.",
                        },
                        "hashtags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "4-6 tags, lowercase, no '#'. 'agentleadlab' last.",
                        },
                        "website_description": {
                            "type": "string",
                            "description": "3-5 sentences for the site, ending with who it's for.",
                        },
                    },
                    "required": [
                        "kind", "start", "end", "yt_title", "website_section",
                        "hook", "bullets", "website_description",
                    ],
                },
            },
        },
        "required": ["segments"],
    },
}


def load_prompt(path: Path | None = None) -> str:
    prompt_path = path or PROMPT_PATH
    if not prompt_path.exists():
        raise SegmentError(f"Segmenting prompt not found: {prompt_path}")
    return prompt_path.read_text(encoding="utf-8")


# One marker per line would be a third of the tokens spent on timestamps and
# would bury the words. A marker every few lines is enough to pin a boundary to
# within a couple of seconds, which is closer than anybody cuts by hand.
MARK_EVERY = 5


def as_marked_transcript(cues: list[Cue], *, every: int = MARK_EVERY) -> str:
    """The transcript with a timestamp in front of every few lines.

    The model can only give back a timestamp it was shown, so this is what
    makes the ranges real rather than guessed.
    """
    lines: list[str] = []
    for index, cue in enumerate(cues):
        if index % every == 0:
            lines.append(f"[{timestamp(cue.start)}] {cue.text}")
        else:
            lines.append(cue.text)
    return "\n".join(lines)


def build_user_message(cues: list[Cue], *, title: str = "", url: str = "") -> str:
    heading = f"Video title: {title}\n" if title else ""
    link = f"YouTube link: {url}\n" if url else ""
    ends = timestamp(cues[-1].end) if cues else "00:00:00"
    return (
        "Cut this interview into segments.\n\n"
        f"{heading}{link}"
        f"The transcript runs to {ends}.\n\n"
        "TRANSCRIPT (each timestamp marks the line it sits on):\n"
        f"{as_marked_transcript(cues)}\n\n"
        "Call emit_segments with the long-form entry and every clip."
    )


def _clean_tag(tag: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(tag).lower().lstrip("#"))


def _clean_bullet(text: str) -> str:
    """Strip the bullet character the model adds back despite being asked not to."""
    return re.sub(r"^\s*(?:[-*•·]|\d+[.)])\s*", "", str(text)).strip().rstrip(".;,")


def _match_section(raw: str) -> str:
    """The website section, spelled the way the site spells it.

    The enum in the schema is not quite a guarantee: an apostrophe comes back
    curled often enough that "Agent's Expectations" and "Agent’s Expectations"
    are two different strings, and only one of them is a section.
    """
    wanted = re.sub(r"[^a-z]", "", (raw or "").lower())
    for section in SECTIONS:
        if re.sub(r"[^a-z]", "", section.lower()) == wanted:
            return section
    raise SegmentError(f"'{raw}' is not one of the website sections.")


def parse_segments(payload: dict) -> tuple[list[Segment], list[Segment]]:
    """(the ones that make the cut, the ones too short to publish).

    Both are returned rather than the short ones being dropped quietly: a clip
    that came back at 3:40 is a boundary the model got wrong, and knowing which
    one is how you fix it.
    """
    raw_segments = payload.get("segments") or []
    if not raw_segments:
        raise SegmentError("No segments came back.")

    keep: list[Segment] = []
    short: list[Segment] = []
    for raw in raw_segments:
        segment = _one_segment(raw)
        if segment.long_form or segment.seconds >= MIN_SECONDS:
            keep.append(segment)
        else:
            short.append(segment)

    if not keep:
        raise SegmentError(
            f"Every segment came back under {MIN_SECONDS // 60} minutes, so there is "
            "nothing to publish."
        )
    _trim_cold_open(keep)
    return keep, short


# How much dead air at the top of a recording counts as a cold open rather than
# content. Anything longer than this is the interview starting, and cutting it
# off the full-interview upload would lose something real.
COLD_OPEN_SECONDS = 60


def _trim_cold_open(keep: list[Segment]) -> None:
    """Start the full interview where the first clip does, not at 00:00:00.

    The first clip is placed at a boundary the model judged worth starting on,
    so the seconds before it are the greetings and the mic check. Only a short
    gap is trimmed: a first clip that starts ten minutes in means the opening
    was interview, just not clippable, and the full upload should keep it.
    """
    clips = [segment for segment in keep if not segment.long_form]
    if not clips:
        return
    opens = min(clip.start for clip in clips)
    for segment in keep:
        if segment.long_form and 0 <= opens - segment.start <= COLD_OPEN_SECONDS:
            segment.start = opens


def _one_segment(raw: dict) -> Segment:
    try:
        start = seconds_of(str(raw["start"]))
        end = seconds_of(str(raw["end"]))
    except (KeyError, ValueError) as exc:
        raise SegmentError(f"A segment came back without usable timestamps: {raw!r}") from exc

    if end <= start:
        raise SegmentError(
            f"Segment '{raw.get('yt_title', '?')}' ends at or before it starts "
            f"({raw.get('start')} to {raw.get('end')})."
        )

    tags = [tag for tag in (_clean_tag(t) for t in raw.get("hashtags") or []) if tag]
    if ALWAYS_TAGGED in tags:
        tags.remove(ALWAYS_TAGGED)
    tags.append(ALWAYS_TAGGED)

    return Segment(
        start=start,
        end=end,
        yt_title=str(raw.get("yt_title") or "").strip(),
        website_section=_match_section(str(raw.get("website_section") or "")),
        hook=str(raw.get("hook") or "").strip(),
        bullets=[b for b in (_clean_bullet(x) for x in raw.get("bullets") or []) if b],
        closing=str(raw.get("closing") or "").strip(),
        hashtags=tags,
        website_description=str(raw.get("website_description") or "").strip(),
        long_form=str(raw.get("kind") or "").strip().lower() == "long-form",
    )


def too_short_note(short: list[Segment]) -> str:
    """Told back to the model: which segments missed the floor, and by how much."""
    named = "\n".join(
        f"· {segment.range} ({length_of(segment.seconds)}) — {segment.yt_title}"
        for segment in short
    )
    return (
        f"These came back under {MIN_SECONDS // 60} minutes, so they can't be "
        f"published and their part of the video is lost:\n{named}\n\n"
        "Emit the complete set again with every one of them folded into the "
        "segment beside it or extended to the next natural boundary. Merging two "
        "adjacent short ones into a single longer segment is usually the right "
        "move — rewrite the title and both descriptions to cover the whole of the "
        "new span. Do not simply drop them: the video has to stay covered end to "
        "end. Keep the segments that were already long enough as they are."
    )


# One retry. The floor is arithmetic the model can check itself, so being told
# exactly which ranges missed it fixes almost every case; a second retry has
# nothing new to say and costs another full pass over the transcript.
RETRIES = 1


def generate_segments(
    cues: list[Cue],
    config: Config,
    *,
    title: str = "",
    url: str = "",
    prompt_path: Path | None = None,
    retries: int = RETRIES,
) -> tuple[dict, list[Segment], list[Segment]]:
    """Call Claude and return (payload, segments to keep, segments too short).

    A segment that lands under the floor is handed back to the model rather
    than dropped on the first answer. Four of seven came back at three and a
    half minutes on the first real interview, and dropping them left four holes
    in a video that is supposed to be covered end to end - which is a worse
    outcome than the long segment it should have been folded into.
    """
    if not cues:
        raise SegmentError("The transcript came back with no lines in it.")

    config.secrets.require("anthropic_api_key")

    from anthropic import Anthropic

    client = Anthropic(api_key=config.secrets.anthropic_api_key)
    system = [{
        "type": "text",
        "text": load_prompt(prompt_path),
        "cache_control": {"type": "ephemeral"},
    }]
    messages = [{
        "role": "user",
        "content": build_user_message(cues, title=title, url=url),
    }]

    for attempt in range(retries + 1):
        last = attempt == retries
        try:
            response = client.messages.create(
                model=config.copy.model,
                max_tokens=MAX_TOKENS,
                system=system,
                tools=[SEGMENT_TOOL],
                tool_choice={"type": "tool", "name": "emit_segments"},
                messages=messages,
            )
        except Exception as exc:
            raise SegmentError(f"Anthropic request failed: {exc}") from exc

        block = _tool_block(response)
        payload = dict(block.input)
        try:
            keep, short = parse_segments(payload)
        except SegmentError:
            # Everything came back short. That is exactly the case worth
            # asking again about, so it is only fatal on the last attempt.
            if last:
                raise
            keep, short = [], parse_lengths(payload)

        if not short or last:
            return payload, keep, short

        messages = messages + [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": too_short_note(short),
            }]},
        ]

    raise SegmentError("Segmenting produced nothing.")  # unreachable


def parse_lengths(payload: dict) -> list[Segment]:
    """Every segment in a payload, however short - for reporting one back."""
    return [_one_segment(raw) for raw in payload.get("segments") or []]


# A 40-minute interview comes back as a dozen entries with a description each,
# which is well past what the blog settings allow for one article.
MAX_TOKENS = 16000


def _tool_block(response):
    """The emit_segments call itself - its id is what a retry replies to."""
    if getattr(response, "stop_reason", None) == "max_tokens":
        raise SegmentError(
            "The model ran out of room part-way through, so the last segments are "
            "missing. Try it again."
        )
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "emit_segments":
            return block
    raise SegmentError(
        "Model did not call emit_segments. Raw stop reason: "
        f"{getattr(response, 'stop_reason', 'unknown')}"
    )


# What a Zoom topic carries besides the person's name: "Antonio Bohorquez -
# llamada con Agent Lead Lab", "Maddy Grundig's Zoom Meeting".
_DASH = re.compile(r"\s[-–—|]\s")
_MEETING_WORDS = re.compile(
    r"(?:'s|’s)?\s*\b(?:zoom\s+)?(?:meeting|call|interview|strategy\s+session|session)\b.*$",
    re.IGNORECASE,
)


def _says_nothing(part: str) -> bool:
    """Whether a piece of a topic is only meeting words - "Strategy Session"."""
    return not _MEETING_WORDS.sub("", part).strip(" -–—|,")


def client_name(topic: str) -> str:
    """The person's name out of whatever the recording was called.

    A dashed topic is a name on one side and whatever the person scheduling it
    typed on the other, and the name is not reliably first: "Strategy Session -
    Maddy Grundig" filed as "Strategy Session Interview" because the rule
    assumed it was. So the side that is *not* purely meeting words wins, and
    only if both sides are does the first one stand.

    Trimming is never allowed to leave nothing behind. A card called
    "Interview" names nobody, so a topic with no name in it comes back whole.
    """
    text = " ".join((topic or "").split())
    if not text:
        return ""

    parts = [part.strip(" -–—|,") for part in _DASH.split(text)]
    parts = [part for part in parts if part]
    named = [part for part in parts if not _says_nothing(part)]
    if named:
        text = named[0]
    elif parts:
        text = parts[0]

    return _MEETING_WORDS.sub("", text).strip(" -–—|,") or text


def card_title(topic: str) -> str:
    """"Maddy Grundig" -> "Maddy Grundig Interview"."""
    name = client_name(topic)
    if not name:
        return "Interview"
    return name if name.casefold().endswith("interview") else f"{name} Interview"


def as_card(segments: list[Segment], *, link: str = "", passcode: str = "") -> str:
    """The card's description: the recording, then every stamp and title.

    An index, not the copy. Whoever opens this card wants to see what the
    interview was cut into and be able to go and watch it; the descriptions
    themselves are in Discord to be pasted where they are actually used.

    The link goes at the top with its passcode under it, because a timestamp is
    only useful once you have the video open - and a list of stamps you have to
    scroll past to reach the link has it backwards.
    """
    lines: list[str] = []
    if link:
        lines.append(link)
    if passcode:
        # No blank line between them: the passcode belongs to the link above it,
        # and Trello renders the pair the way it was pasted by hand.
        if lines:
            lines[-1] = f"{lines[-1]}\nPasscode: {passcode}"
        else:
            lines.append(f"Passcode: {passcode}")

    number = 0
    for segment in segments:
        if segment.long_form:
            lines.append(f"LONG-FORM / FULL INTERVIEW ({segment.range}) {segment.yt_title}")
            continue
        number += 1
        lines.append(f"SEGMENT {number} ({segment.range}) {segment.yt_title}")
    return "\n\n".join(lines)


def opening(payload: dict, *, kept: int, short: list[Segment]) -> str:
    """What goes above the segments: the summary, the quote, and any drops."""
    lines: list[str] = []
    summary = str(payload.get("summary") or "").strip()
    quote = str(payload.get("pull_quote") or "").strip().strip('"“”')
    if summary:
        lines.append(summary)
    if quote:
        lines.append(f'"{quote}"')

    clips = kept - 1 if kept else 0
    lines.append(f"**{clips} segment{'' if clips == 1 else 's'}** plus the full interview.")

    if short:
        dropped = ", ".join(f"{s.range} ({length_of(s.seconds)})" for s in short)
        lines.append(
            f"-# Left out for running under {MIN_SECONDS // 60} minutes: {dropped}"
        )
    return "\n\n".join(lines)
