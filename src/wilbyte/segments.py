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
    return keep, short


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


def generate_segments(
    cues: list[Cue],
    config: Config,
    *,
    title: str = "",
    url: str = "",
    prompt_path: Path | None = None,
) -> tuple[dict, list[Segment], list[Segment]]:
    """Call Claude and return (payload, segments to keep, segments too short)."""
    if not cues:
        raise SegmentError("The transcript came back with no lines in it.")

    config.secrets.require("anthropic_api_key")

    from anthropic import Anthropic

    client = Anthropic(api_key=config.secrets.anthropic_api_key)

    try:
        response = client.messages.create(
            model=config.copy.model,
            max_tokens=MAX_TOKENS,
            system=[{
                "type": "text",
                "text": load_prompt(prompt_path),
                "cache_control": {"type": "ephemeral"},
            }],
            tools=[SEGMENT_TOOL],
            tool_choice={"type": "tool", "name": "emit_segments"},
            messages=[{
                "role": "user",
                "content": build_user_message(cues, title=title, url=url),
            }],
        )
    except Exception as exc:
        raise SegmentError(f"Anthropic request failed: {exc}") from exc

    payload = _tool_input(response)
    keep, short = parse_segments(payload)
    return payload, keep, short


# A 40-minute interview comes back as a dozen entries with a description each,
# which is well past what the blog settings allow for one article.
MAX_TOKENS = 16000


def _tool_input(response) -> dict:
    if getattr(response, "stop_reason", None) == "max_tokens":
        raise SegmentError(
            "The model ran out of room part-way through, so the last segments are "
            "missing. Try it again."
        )
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "emit_segments":
            return dict(block.input)
    raise SegmentError(
        "Model did not call emit_segments. Raw stop reason: "
        f"{getattr(response, 'stop_reason', 'unknown')}"
    )


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
