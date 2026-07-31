"""Data structures passed between pipeline stages."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Video:
    """A YouTube video from the source playlist."""

    video_id: str
    title: str
    url: str
    playlist_index: int | None = None
    duration_seconds: int | None = None

    @property
    def short_url(self) -> str:
        """The youtu.be form Wil pastes into the Claude project."""
        return f"https://youtu.be/{self.video_id}"


@dataclass
class Transcript:
    video_id: str
    text: str
    source: str  # "youtube" | "manual"

    @property
    def word_count(self) -> int:
        return len(self.text.split())


@dataclass
class Headline:
    text: str

    @property
    def char_count(self) -> int:
        return len(self.text)

    @property
    def word_count(self) -> int:
        return len(self.text.split())


@dataclass
class CopyPackage:
    """Everything the Copywriting Frank prompt produces for one video."""

    article_h1: str
    article_html: str
    headline_options: list[Headline]
    meta_title: str
    meta_description: str
    url_slug: str
    keyword_map: str = ""
    internal_link_notes: str = ""
    word_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["headline_options"] = [h.text for h in self.headline_options]
        return data


@dataclass
class CoverPlan:
    """The two text fragments that go on the cover image."""

    kicker: str  # small highlighted line, 3-5 words
    headline: str  # the big line underneath
    source_note: str = ""


@dataclass
class BlogPost:
    """A fully assembled post, ready to hand to GHL."""

    video: Video
    copy: CopyPackage
    title: str  # chosen headline option (never equal to article H1)
    url_slug: str
    canonical_link: str
    description: str
    category: str
    author: str
    keywords: list[str]
    cover_plan: CoverPlan
    cover_image_path: str | None = None
    cover_image_url: str | None = None
    cover_alt_text: str = ""
    scheduled_at: datetime | None = None
    ghl_post_id: str | None = None
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        when = self.scheduled_at.strftime("%a %b %d, %Y %I:%M %p") if self.scheduled_at else "unscheduled"
        return (
            f"{self.title}\n"
            f"  slug:      /{self.url_slug}\n"
            f"  video:     {self.video.short_url}\n"
            f"  scheduled: {when}\n"
            f"  cover:     {self.cover_plan.kicker!r} / {self.cover_plan.headline!r}"
        )
