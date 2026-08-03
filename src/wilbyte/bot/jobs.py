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


def check_ghl(config: Config) -> list[tuple[bool, str]]:
    """Verify everything the pipeline needs from GHL before a real run."""
    results: list[tuple[bool, str]] = []
    if not (config.secrets.ghl_api_token and config.secrets.ghl_location_id):
        return [(False, "No GHL token or location id set")]

    client = ghl.GHLClient(config.secrets.ghl_api_token, config.secrets.ghl_location_id)
    try:
        try:
            sites = client.list_blog_sites()
            results.append((True, f"Connected — {len(sites)} blog site(s) visible"))
        except Exception as exc:
            results.append((False, f"Cannot reach GHL: {exc}"))
            return results

        blog_id = config.secrets.ghl_blog_id
        if not blog_id:
            results.append((False, "GHL_BLOG_ID is not set"))
        elif any(str(s.get("_id") or s.get("id")) == blog_id for s in sites):
            results.append((True, "GHL_BLOG_ID matches a real blog site"))
        else:
            names = ", ".join(
                f"{s.get('name') or s.get('title') or '?'} = {s.get('_id') or s.get('id')}"
                for s in sites[:5]
            )
            results.append((False, f"GHL_BLOG_ID not among your sites. Found: {names}"))

        for kind, wanted, lister in (
            ("author", config.post.author, client.list_authors),
            ("category", config.post.category, client.list_categories),
        ):
            try:
                found = ghl.resolve_by_name(lister(), wanted, kind=kind)
                results.append((True, f"{kind.title()} {wanted!r} found ({found})"))
            except Exception as exc:
                results.append((False, str(exc)))

        if blog_id:
            try:
                posts = client.list_posts(blog_id)
                days = taken_days_from_posts(posts, config.schedule)
                results.append((True, f"Read {len(posts)} existing post(s), {len(days)} day(s) booked"))
            except Exception as exc:
                results.append((False, f"Cannot list existing posts: {exc}"))
    finally:
        client.close()
    return results


def check_youtube(source: str | None) -> list[tuple[bool, str]]:
    """Verify YouTube is reachable, and that transcripts actually come back.

    This is the step most likely to fail in a datacenter: YouTube blocks many
    cloud IP ranges for transcript requests even when video metadata loads.
    """
    if not source:
        return [(None, "No link given — add one to test YouTube access")]

    results: list[tuple[bool, str]] = []
    try:
        if youtube.looks_like_playlist(source):
            videos = youtube.list_playlist_videos(source, limit=3)
            results.append((True, f"Playlist readable — {len(videos)}+ video(s)"))
        else:
            videos = [youtube.fetch_video(source)]
            results.append((True, f"Video readable — {videos[0].title[:60]}"))
    except Exception as exc:
        results.append((False, f"Cannot read from YouTube: {_short(exc)}"))
        return results

    try:
        transcript = youtube.fetch_transcript(videos[0].video_id)
        results.append((True, f"Transcript works — {transcript.word_count} words"))
    except Exception as exc:
        results.append((False, f"No transcript: {_short(exc)}"))
    return results


def _short(exc: Exception, limit: int = 220) -> str:
    text = " ".join(str(exc).split())
    return text if len(text) <= limit else text[:limit] + "…"


def record(ledger: Ledger, post: BlogPost) -> None:
    ledger.record(
        video_id=post.video.video_id,
        title=post.title,
        url_slug=post.url_slug,
        scheduled_at=post.scheduled_at,
        ghl_post_id=post.ghl_post_id,
    )
    ledger.save()
