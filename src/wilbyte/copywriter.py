"""Turn a transcript into a CopyPackage via the Anthropic API.

This replaces the manual step where Wil pastes the transcript + YouTube link
into the "Organic Post AI Copywriter / Copywriting Frank" Claude project and
waits for the copy to come back.

The house style lives in `prompts/copywriting_frank.md`, not in this file - swap
that file for the real project instructions and the pipeline picks it up.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import Config, ConfigError, REPO_ROOT
from .models import CopyPackage, Headline, Transcript, Video

PROMPT_PATH = REPO_ROOT / "prompts" / "copywriting_frank.md"

# Structured output contract. Using a tool schema (rather than "reply with JSON")
# means the model retries on its own if it produces a malformed shape.
BLOG_PACKAGE_TOOL = {
    "name": "emit_blog_package",
    "description": "Return the finished blog post and all of its SEO metadata.",
    "input_schema": {
        "type": "object",
        "properties": {
            "article_h1": {
                "type": "string",
                "description": "The H1 headline used at the top of the article body.",
            },
            "article_html": {
                "type": "string",
                "description": (
                    "The full article as semantic HTML, starting with the <h1>. "
                    "Allowed tags: h1,h2,h3,p,ul,ol,li,strong,em,blockquote,a."
                ),
            },
            "headline_options": {
                "type": "array",
                "description": (
                    "Exactly 3 headline options, 40-60 chars each. Options 2 and 3 "
                    "must be angled differently from article_h1."
                ),
                "items": {"type": "string"},
                "minItems": 3,
                "maxItems": 3,
            },
            "meta_title": {"type": "string", "description": "<=60 characters."},
            "meta_description": {
                "type": "string",
                "description": "100-160 characters. Pasted verbatim into GHL 'Post description'.",
            },
            "url_slug": {
                "type": "string",
                "description": "lowercase-hyphenated, 3-6 words, no leading slash.",
            },
            "keyword_map": {
                "type": "string",
                "description": "Primary / secondary / long-tail keywords and their placement.",
            },
            "internal_link_notes": {
                "type": "string",
                "description": "Which Agent Lead Lab posts to link to; hub or spoke.",
            },
            "word_count": {
                "type": "integer",
                "description": "Approximate word count of the article body.",
            },
        },
        "required": [
            "article_h1",
            "article_html",
            "headline_options",
            "meta_title",
            "meta_description",
            "url_slug",
        ],
    },
}


class CopywriterError(RuntimeError):
    """Raised when copy generation fails or returns unusable output."""


def load_system_prompt(path: Path | None = None) -> str:
    """The operating brief, followed by every reference document.

    The brief is deliberately first: it carries the blog-specific rules and the
    compliance limits, and it says outright that it wins over anything in the
    reference material. The reference docs are read in filename order, so
    numbering them controls what the model sees first.

    Dropping another `.md` into `prompts/reference/` is all it takes to teach
    RYTE something new - no code change, no redeploy beyond a restart.
    """
    prompt_path = path or PROMPT_PATH
    if not prompt_path.exists():
        raise CopywriterError(f"Copywriter prompt not found: {prompt_path}")

    parts = [prompt_path.read_text(encoding="utf-8")]
    for doc in sorted((prompt_path.parent / "reference").glob("*.md")):
        parts.append(f"\n\n---\n\n<!-- {doc.name} -->\n\n{doc.read_text(encoding='utf-8')}")
    return "".join(parts)


def build_user_message(video: Video, transcript: Transcript) -> str:
    """Mirror what Wil pastes by hand: the transcript, then the YouTube link."""
    # The title is omitted when unknown - it's a hint, not a requirement, and an
    # empty label reads to the model as a real (blank) title.
    title_line = f"YouTube title: {video.title}\n" if video.title else ""
    return (
        "Write the Agent Lead Lab blog post for this video.\n\n"
        f"{title_line}"
        f"YouTube link: {video.short_url}\n\n"
        "TRANSCRIPT:\n"
        f"{transcript.text}\n\n"
        "Call emit_blog_package with the finished post and its metadata."
    )


def generate_copy(
    video: Video,
    transcript: Transcript,
    config: Config,
    *,
    prompt_path: Path | None = None,
) -> CopyPackage:
    """Call Claude and return a validated CopyPackage."""
    config.secrets.require("anthropic_api_key")

    from anthropic import Anthropic

    client = Anthropic(api_key=config.secrets.anthropic_api_key)
    system_prompt = load_system_prompt(prompt_path)

    try:
        response = client.messages.create(
            model=config.copy.model,
            max_tokens=config.copy.max_tokens,
            # The brief plus its reference documents is ~18k tokens and identical
            # on every post, so cache it. Within a batch the second and third
            # posts read it at a tenth of the price instead of paying full
            # freight three times over.
            system=[{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            tools=[BLOG_PACKAGE_TOOL],
            tool_choice={"type": "tool", "name": "emit_blog_package"},
            messages=[{"role": "user", "content": build_user_message(video, transcript)}],
        )
    except Exception as exc:
        raise CopywriterError(f"Anthropic request failed for {video.video_id}: {exc}") from exc

    payload = _extract_tool_input(response)
    return parse_copy_package(payload, config)


def _extract_tool_input(response) -> dict:
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "emit_blog_package":
            return dict(block.input)
    raise CopywriterError(
        "Model did not call emit_blog_package. Raw stop reason: "
        f"{getattr(response, 'stop_reason', 'unknown')}"
    )


def parse_copy_package(payload: dict, config: Config) -> CopyPackage:
    """Validate and normalize the model's structured output.

    Length overruns are corrected here rather than rejected - a 62-character meta
    title should not cost a full regeneration.
    """
    missing = [
        key
        for key in ("article_h1", "article_html", "headline_options", "meta_title",
                    "meta_description", "url_slug")
        if not payload.get(key)
    ]
    if missing:
        raise CopywriterError(f"Copy package missing fields: {', '.join(missing)}")

    options = [str(h).strip() for h in payload["headline_options"] if str(h).strip()]
    if len(options) < 2:
        raise CopywriterError(
            f"Need at least 2 distinct headline options, got {len(options)}. "
            "The blog title has to be able to differ from the article H1."
        )

    package = CopyPackage(
        article_h1=str(payload["article_h1"]).strip(),
        article_html=str(payload["article_html"]).strip(),
        headline_options=[Headline(text=o) for o in options],
        meta_title=_truncate_clean(str(payload["meta_title"]).strip(), config.copy.meta_title_max),
        meta_description=_truncate_clean(
            str(payload["meta_description"]).strip(), config.copy.meta_description_max
        ),
        url_slug=normalize_slug(str(payload["url_slug"])),
        keyword_map=str(payload.get("keyword_map") or "").strip(),
        internal_link_notes=str(payload.get("internal_link_notes") or "").strip(),
        word_count=int(payload.get("word_count") or 0),
    )
    if not package.word_count:
        package.word_count = _estimate_word_count(package.article_html)
    return package


def normalize_slug(slug: str) -> str:
    """`/Insurance Lead Progression-Roadmap/` -> `insurance-lead-progression-roadmap`."""
    import re

    slug = slug.strip().strip("/").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    if not slug:
        raise CopywriterError("URL slug normalized to an empty string.")
    return slug


def _truncate_clean(text: str, limit: int) -> str:
    """Trim to `limit` on a word boundary rather than mid-word."""
    if len(text) <= limit:
        return text
    cut = text[:limit].rstrip()
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:-—")


def _estimate_word_count(html: str) -> int:
    import re

    return len(re.sub(r"<[^>]+>", " ", html).split())


def dump_package(package: CopyPackage, path: Path) -> None:
    """Persist the raw copy package next to the rendered outputs, for review."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(package.to_dict(), indent=2), encoding="utf-8")
