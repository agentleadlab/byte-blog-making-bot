"""The kinds of copy Byte writes, and the shape each one takes.

Adding a format here is all it takes to teach Byte a new one - the corpus
labels, the Discord commands, and the structured output schema are all derived
from this table.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Field:
    """One piece of a variant, e.g. an email's subject line."""

    key: str
    label: str
    guidance: str
    max_chars: int | None = None
    multiline: bool = False


@dataclass
class Format:
    key: str
    label: str
    aliases: tuple[str, ...]
    description: str
    guidance: str
    fields: list[Field]
    variants: int = 3

    def matches(self, word: str) -> bool:
        word = word.lower().strip()
        return word == self.key or word in self.aliases

    def output_schema(self) -> dict:
        """JSON Schema for the model's structured output."""
        properties = {}
        for f in self.fields:
            spec = {"type": "string", "description": f.guidance}
            if f.max_chars:
                spec["description"] += f" Hard limit {f.max_chars} characters."
            properties[f.key] = spec

        return {
            "type": "object",
            "properties": {
                "variants": {
                    "type": "array",
                    "description": f"{self.variants} distinct options, each a different angle.",
                    "items": {
                        "type": "object",
                        "properties": properties,
                        "required": [f.key for f in self.fields],
                    },
                    "minItems": 1,
                    "maxItems": self.variants,
                },
                "notes": {
                    "type": "string",
                    "description": (
                        "One or two sentences: which past pieces you drew the voice from, "
                        "and what angle separates the variants."
                    ),
                },
            },
            "required": ["variants"],
        }


SMS = Format(
    key="sms",
    label="SMS",
    aliases=("text", "texts", "textmessage", "mms"),
    description="A text message to a lead or list",
    guidance=(
        "Under 160 characters so it sends as a single segment. Conversational, "
        "lowercase-friendly, like a real person typing. One idea, one ask. No "
        "emoji unless the past copy uses them. Never invent a discount or a "
        "guarantee. Include a natural opt-out only if the examples do."
    ),
    fields=[
        Field("body", "Message", "The full text message.", max_chars=160),
    ],
    variants=4,
)

EMAIL = Format(
    key="email",
    label="Email",
    aliases=("emails", "broadcast", "newsletter", "blast"),
    description="A marketing or nurture email",
    guidance=(
        "Subject line under 50 characters, curiosity or benefit led, no clickbait "
        "the body doesn't pay off. Preview text complements the subject rather "
        "than repeating it. Body is short paragraphs, one idea each, with a "
        "single clear call to action. Write like one person emailing another."
    ),
    fields=[
        Field("subject", "Subject", "The subject line.", max_chars=50),
        Field("preview_text", "Preview", "Inbox preview text.", max_chars=90),
        Field("body", "Body", "The email body, plain text with line breaks.", multiline=True),
    ],
    variants=3,
)

AD = Format(
    key="ad",
    label="Ad",
    aliases=("ads", "facebook", "fb", "instagram", "ig", "meta", "paid"),
    description="A Facebook or Instagram ad",
    guidance=(
        "Primary text hooks in the first line, before the 'See more' cut. Headline "
        "is short and concrete. Speak to the agent's actual problem, not to "
        "'insurance professionals'. No income claims, no guaranteed results - "
        "describe what the offer is, not what the reader will earn."
    ),
    fields=[
        Field("primary_text", "Primary text", "The main ad copy.", multiline=True),
        Field("headline", "Headline", "The bold headline under the creative.", max_chars=40),
        Field("description", "Description", "The link description.", max_chars=30),
    ],
    variants=3,
)

LANDING = Format(
    key="landing",
    label="Landing page",
    # "vsl" belongs to script - a video sales letter is something you write to
    # be spoken, even though it lives on a landing page.
    aliases=("lp", "page", "optin", "funnel"),
    description="A landing or opt-in page",
    guidance=(
        "Headline states the outcome. Subhead names who it's for and the "
        "mechanism. Bullets are proof and specifics, not adjectives. One CTA, "
        "repeated, never competing with a second offer."
    ),
    fields=[
        Field("headline", "Headline", "The main headline.", max_chars=70),
        Field("subhead", "Subhead", "One sentence under the headline.", max_chars=140),
        Field("bullets", "Bullets", "3-5 benefit bullets, one per line.", multiline=True),
        Field("cta", "CTA", "The button text.", max_chars=30),
    ],
    variants=3,
)

SCRIPT = Format(
    key="script",
    label="Script",
    aliases=("scripts", "video", "reel", "short", "vo", "voiceover", "vsl"),
    description="A short video or call script",
    guidance=(
        "Hook lands in the first three seconds and names a specific pain or "
        "number. Body teaches one thing. Close is a single ask. Written to be "
        "spoken aloud - contractions, short sentences, no clauses to trip on."
    ),
    fields=[
        Field("hook", "Hook", "The opening line.", max_chars=120),
        Field("body", "Body", "The middle of the script.", multiline=True),
        Field("cta", "Close", "The closing ask.", max_chars=140),
    ],
    variants=3,
)

SOCIAL = Format(
    key="social",
    label="Social post",
    # No "post" alias on purpose - it collides with "write me 3 posts".
    aliases=("organic", "linkedin", "caption"),
    description="An organic social post",
    guidance=(
        "Opens with a line that stops the scroll on its own. Short lines, plenty "
        "of white space. Teaches something concrete before it asks for anything."
    ),
    fields=[
        Field("body", "Post", "The full post.", multiline=True),
    ],
    variants=3,
)

FORMATS: list[Format] = [SMS, EMAIL, AD, LANDING, SCRIPT, SOCIAL]
BY_KEY = {f.key: f for f in FORMATS}

# The blog pipeline is its own thing - listed so corpus pieces can be labelled
# "blog" without the writer trying to handle them here.
CORPUS_ONLY_FORMATS = ("blog",)
ALL_CORPUS_LABELS = tuple(BY_KEY) + CORPUS_ONLY_FORMATS


def find(word: str | None) -> Format | None:
    """Resolve a format from a user's word - 'texts', 'fb', 'vsl' all work."""
    if not word:
        return None
    word = word.lower().strip().rstrip(".,:;!?")
    for fmt in FORMATS:
        if fmt.matches(word):
            return fmt
    return None


def find_label(word: str | None) -> str | None:
    """Like `find`, but also accepts corpus-only labels such as 'blog'."""
    if not word:
        return None
    cleaned = word.lower().strip().rstrip(".,:;!?")
    if cleaned in CORPUS_ONLY_FORMATS:
        return cleaned
    fmt = find(cleaned)
    return fmt.key if fmt else None


def guess_label(text: str, *, filename: str = "") -> str:
    """Best-effort label for a piece of copy with no explicit format.

    Filename wins when it says so; otherwise fall back to shape - the signals
    are deliberately conservative, and 'unsorted' is a fine answer.
    """
    from_name = _label_from_filename(filename)
    if from_name:
        return from_name

    stripped = text.strip()
    lowered = stripped.lower()

    if "subject:" in lowered[:200] or "subject line:" in lowered[:200]:
        return "email"
    if len(stripped) <= 200 and "\n" not in stripped.strip():
        return "sms"
    if any(marker in lowered[:300] for marker in ("primary text:", "headline:", "ad copy:")):
        return "ad"
    if stripped.count("\n") > 12 and len(stripped) > 1500:
        return "blog"
    return "unsorted"


def _label_from_filename(filename: str) -> str | None:
    name = filename.lower()
    for label in ALL_CORPUS_LABELS:
        if label in name:
            return label
    for fmt in FORMATS:
        if any(alias in name for alias in fmt.aliases):
            return fmt.key
    return None
