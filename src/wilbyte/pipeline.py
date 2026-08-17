"""End-to-end orchestration: YouTube video -> scheduled GHL blog post."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import copywriter, cover, ghl, selection, youtube
from .config import Config, REPO_ROOT
from .models import BlogPost, CopyPackage, Transcript, Video
from .state import Ledger

DEFAULT_OUTPUT_DIR = Path(os.getenv("WILBYTE_OUTPUT_DIR") or REPO_ROOT / "out")

Reporter = Callable[[str], None]


class PipelineError(RuntimeError):
    """Raised when a post cannot be assembled."""


def build_post(
    video: Video,
    transcript: Transcript,
    config: Config,
    *,
    output_dir: Path,
    report: Reporter = print,
) -> BlogPost:
    """Generate the copy, pick the title, and render the cover image.

    Everything here is local - nothing is sent to GHL. Safe to run and inspect
    before committing to a schedule.
    """
    report(f"  writing copy for {video.video_id} ({transcript.word_count} transcript words)")
    copy = copywriter.generate_copy(video, transcript, config)
    return assemble_post(video, copy, config, output_dir=output_dir, report=report)


def assemble_post(
    video: Video,
    copy: CopyPackage,
    config: Config,
    *,
    output_dir: Path,
    report: Reporter = print,
) -> BlogPost:
    """Turn a CopyPackage into a BlogPost with a rendered cover image."""
    title, title_note = selection.choose_title(copy)
    report(f"  title: {title.text!r} [{title_note}]")
    if "manual look" in title_note:
        report("    ! every headline option echoed the article H1")

    cover_plan = selection.plan_cover(copy, title, config)
    report(f"  cover: {cover_plan.kicker!r} / {cover_plan.headline!r}")

    post_dir = output_dir / copy.url_slug
    post_dir.mkdir(parents=True, exist_ok=True)

    cover_path = post_dir / "cover.png"
    cover.render_cover(cover_plan, config, cover_path)
    report(f"  cover image: {cover_path}")

    alt_text = copy.url_slug if config.cover.alt_text_source == "url_slug" else title.text

    post = BlogPost(
        video=video,
        copy=copy,
        title=title.text,
        url_slug=copy.url_slug,
        canonical_link=config.brand.canonical_link(copy.url_slug),
        description=copy.meta_description,
        category=config.post.category,
        author=config.post.author,
        keywords=list(config.post.keywords),
        cover_plan=cover_plan,
        cover_image_path=str(cover_path),
        cover_alt_text=alt_text,
    )
    if "manual look" in title_note:
        post.warnings.append(f"headline selection: {title_note}")
    if "manual look" in cover_plan.source_note:
        post.warnings.append(f"cover text: {cover_plan.source_note}")
    # An article with no tags at all publishes as a wall of plain text - or
    # worse, as its own markup printed down the page. Cheap to check, and it
    # shipped once already.
    if "<" not in post.copy.article_html:
        post.warnings.append(
            "The article body has no HTML tags — it will publish as plain text."
        )

    _write_local_artifacts(post, post_dir)
    return post


def _write_local_artifacts(post: BlogPost, post_dir: Path) -> None:
    """Write the paste-ready files, so a run is reviewable without opening GHL."""
    (post_dir / "post.html").write_text(post.copy.article_html, encoding="utf-8")
    copywriter.dump_package(post.copy, post_dir / "copy.json")
    (post_dir / "ghl-fields.txt").write_text(
        "\n".join(
            [
                f"Title:            {post.title}",
                f"URL slug:         {post.url_slug}",
                f"Category:         {post.category}",
                f"Author:           {post.author}",
                f"Keywords:         {', '.join(post.keywords)}",
                f"Canonical link:   {post.canonical_link}",
                f"Post description: {post.description}",
                f"Cover alt text:   {post.cover_alt_text}",
                f"Cover kicker:     {post.cover_plan.kicker}",
                f"Cover headline:   {post.cover_plan.headline}",
                f"Source video:     {post.video.short_url}",
                "",
                "Headline options the copywriter produced:",
                *[
                    f"  {i}. {h.text} ({h.char_count} chars)"
                    for i, h in enumerate(post.copy.headline_options, start=1)
                ],
                "",
                f"Article H1:       {post.copy.article_h1}",
                "",
                "Keyword map:",
                post.copy.keyword_map or "  (none)",
                "",
                "Internal links:",
                post.copy.internal_link_notes or "  (none)",
            ]
        ),
        encoding="utf-8",
    )


def publish_post(
    post: BlogPost,
    config: Config,
    client: ghl.GHLClient,
    *,
    blog_id: str,
    author_id: str,
    category_ids: list[str],
    status: str,
    dry_run: bool = False,
    report: Reporter = print,
) -> BlogPost:
    """Upload the cover image and create the post in GHL."""
    image_url = post.cover_image_url

    if not dry_run and post.cover_image_path and not image_url:
        image_url = client.upload_media(
            Path(post.cover_image_path), name=f"{post.url_slug}.png"
        )
        post.cover_image_url = image_url
        report(f"  cover uploaded: {image_url}")

    payload = ghl.build_post_payload(
        location_id=client.location_id,
        blog_id=blog_id,
        title=post.title,
        content_html=post.copy.article_html,
        description=post.description,
        url_slug=post.url_slug,
        canonical_link=post.canonical_link,
        author_id=author_id,
        category_ids=category_ids,
        keywords=post.keywords,
        image_url=image_url,
        image_alt=post.cover_alt_text,
        status=status,
        published_at=post.scheduled_at,
    )

    if dry_run:
        report("  [dry-run] POST /blogs/posts payload:")
        preview = dict(payload)
        body_key = ghl.POST_FIELDS["content"]
        preview[body_key] = f"<{len(payload.get(body_key, ''))} chars of HTML>"
        report("    " + json.dumps(preview, indent=2).replace("\n", "\n    "))
        return post

    created = client.create_post(payload)
    post.ghl_post_id = str(created.get("_id") or created.get("id") or "") or None
    report(f"  created GHL post {post.ghl_post_id or '(id not returned)'}")
    return post


def ensure_unique_slug(client: ghl.GHLClient, url_slug: str, *, report: Reporter = print) -> str:
    """Append -2, -3, ... if the slug is already taken on this location."""
    if not client.slug_exists(url_slug):
        return url_slug
    for suffix in range(2, 12):
        candidate = f"{url_slug}-{suffix}"
        if not client.slug_exists(candidate):
            report(f"  slug {url_slug!r} taken, using {candidate!r}")
            return candidate
    raise PipelineError(f"Could not find a free slug based on {url_slug!r}.")


def resolve_transcript(
    video: Video, *, transcript_path: Path | None, report: Reporter = print
) -> Transcript:
    if transcript_path:
        report(f"  using manual transcript: {transcript_path}")
        return youtube.load_manual_transcript(video.video_id, transcript_path)
    return youtube.fetch_transcript(video.video_id)


def select_pending_videos(
    videos: list[Video], ledger: Ledger, *, limit: int | None
) -> tuple[list[Video], list[Video]]:
    """Split the playlist into (to process, already done)."""
    pending = [v for v in videos if not ledger.has(v.video_id)]
    done = [v for v in videos if ledger.has(v.video_id)]
    if limit is not None:
        pending = pending[:limit]
    return pending, done


def format_slot(moment: datetime) -> str:
    return moment.strftime("%a %b %d, %Y at %I:%M %p %Z")
