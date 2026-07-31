"""Blocking pipeline work, wrapped so the Discord event loop never stalls.

Every method here does network or CPU work and is called via `asyncio.to_thread`
from `client.py`. Keeping them free of Discord types also makes them testable
without a gateway connection.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from .. import ghl, pipeline, youtube
from ..config import Config
from ..models import BlogPost, Video
from ..scheduler import next_open_slots, taken_days_from_posts
from ..state import Ledger


class GHLContext:
    """A GHL session plus the ids the pipeline needs, resolved once per run."""

    def __init__(self, client: ghl.GHLClient, blog_id: str, author_id: str, category_ids: list[str]):
        self.client = client
        self.blog_id = blog_id
        self.author_id = author_id
        self.category_ids = category_ids

    def close(self) -> None:
        self.client.close()


def open_ghl(config: Config) -> GHLContext:
    """Connect to GHL and resolve the author/category ids up front."""
    config.secrets.require("ghl_api_token", "ghl_location_id", "ghl_blog_id")
    client = ghl.GHLClient(config.secrets.ghl_api_token, config.secrets.ghl_location_id)
    try:
        author_id = config.secrets.ghl_author_id or ghl.resolve_by_name(
            client.list_authors(), config.post.author, kind="author"
        )
        category_id = config.secrets.ghl_category_id or ghl.resolve_by_name(
            client.list_categories(), config.post.category, kind="category"
        )
    except Exception:
        client.close()
        raise
    return GHLContext(client, config.secrets.ghl_blog_id, author_id, [category_id])


def taken_days(context: GHLContext | None, config: Config) -> set[date]:
    if context is None:
        return set()
    return taken_days_from_posts(context.client.list_posts(context.blog_id), config.schedule)


def resolve_videos(
    source: str, ledger: Ledger, *, limit: int, force: bool
) -> tuple[list[Video], int]:
    """Expand a playlist or single-video URL into the videos still to process."""
    if youtube.looks_like_playlist(source):
        videos = youtube.list_playlist_videos(source)
    else:
        videos = [youtube.fetch_video(source)]

    if force:
        return videos[:limit], 0

    pending, done = pipeline.select_pending_videos(videos, ledger, limit=limit)
    return pending, len(done)


def plan_slots(
    videos: list[Video], context: GHLContext | None, config: Config
) -> list[datetime]:
    return next_open_slots(taken_days(context, config), len(videos), config.schedule)


def build(video: Video, config: Config, output_dir: Path) -> BlogPost:
    """Transcript -> copy -> title -> cover image. No GHL contact."""
    transcript = youtube.fetch_transcript(video.video_id)
    return pipeline.build_post(
        video, transcript, config, output_dir=output_dir, report=lambda _: None
    )


def publish(
    post: BlogPost, config: Config, context: GHLContext, *, status: str
) -> BlogPost:
    """Upload the cover, dedupe the slug, and create the post."""
    post.url_slug = pipeline.ensure_unique_slug(
        context.client, post.url_slug, report=lambda _: None
    )
    post.canonical_link = config.brand.canonical_link(post.url_slug)
    if config.cover.alt_text_source == "url_slug":
        post.cover_alt_text = post.url_slug

    return pipeline.publish_post(
        post,
        config,
        context.client,
        blog_id=context.blog_id,
        author_id=context.author_id,
        category_ids=context.category_ids,
        status=status,
        dry_run=False,
        report=lambda _: None,
    )


def record(ledger: Ledger, post: BlogPost) -> None:
    ledger.record(
        video_id=post.video.video_id,
        title=post.title,
        url_slug=post.url_slug,
        scheduled_at=post.scheduled_at,
        ghl_post_id=post.ghl_post_id,
    )
    ledger.save()
