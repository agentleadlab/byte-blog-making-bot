"""Byte command line.

    wilbyte doctor                        check config + credentials + GHL access
    wilbyte plan --playlist <url>         show what would be posted, and when
    wilbyte run --playlist <url> -n 3     write, render, and schedule 3 posts
    wilbyte run --video <url> --dry-run   one video, print the payload, send nothing
    wilbyte cover --kicker .. --headline ..   re-render a cover image only
    wilbyte bot                           run the Discord bot
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from . import cover as cover_mod
from . import ghl, pipeline, youtube
from .config import Config, ConfigError, load_config
from .copywriter import CopywriterError
from .models import CoverPlan, Video
from .pipeline import DEFAULT_OUTPUT_DIR, PipelineError
from .scheduler import SchedulerError, next_open_slots, taken_days_from_posts
from .state import Ledger
from .youtube import IngestError


class CLIError(RuntimeError):
    """A user-facing error; printed without a traceback."""


# --------------------------------------------------------------------- helpers


def _open_client(config: Config) -> ghl.GHLClient:
    config.secrets.require("ghl_api_token", "ghl_location_id")
    return ghl.GHLClient(config.secrets.ghl_api_token, config.secrets.ghl_location_id)


def _resolve_ids(client: ghl.GHLClient, config: Config) -> tuple[str, list[str]]:
    """Author id and category ids, from env overrides or by name lookup."""
    author_id = config.secrets.ghl_author_id
    if not author_id:
        author_id = ghl.resolve_by_name(client.list_authors(), config.post.author, kind="author")

    category_id = config.secrets.ghl_category_id
    if not category_id:
        category_id = ghl.resolve_by_name(
            client.list_categories(), config.post.category, kind="category"
        )
    return author_id, [category_id]


def _taken_days(client: ghl.GHLClient, config: Config, blog_id: str) -> set[date]:
    posts = client.list_posts(blog_id)
    return taken_days_from_posts(posts, config.schedule)


def _collect_videos(args, ledger: Ledger) -> tuple[list[Video], list[Video]]:
    if args.video:
        video = youtube.fetch_video(args.video)
        if ledger.has(video.video_id) and not args.force:
            raise CLIError(
                f"{video.video_id} is already in the ledger. Re-run with --force to redo it."
            )
        return [video], []

    videos = youtube.list_playlist_videos(args.playlist)
    if args.force:
        pending = videos[: args.limit] if args.limit else videos
        return pending, []
    return pipeline.select_pending_videos(videos, ledger, limit=args.limit)


# -------------------------------------------------------------------- commands


def cmd_doctor(args) -> int:
    config = load_config()
    print(f"config:      {config.path}")
    print(f"category:    {config.post.category}")
    print(f"author:      {config.post.author}")
    print(f"schedule:    {config.schedule.time} {config.schedule.timezone}, "
          f"{'weekdays only' if config.schedule.weekdays_only else 'every day'}")
    print(f"cover size:  {config.cover.width}x{config.cover.height}")
    print(f"model:       {config.copy.model}")

    print("\ncredentials:")
    for name in ("anthropic_api_key", "ghl_api_token", "ghl_location_id", "ghl_blog_id"):
        value = getattr(config.secrets, name)
        secret = name.endswith(("_key", "_token"))
        if not value:
            shown = "-"
        elif secret:
            shown = f"{value[:6]}..."  # never print a full credential
        else:
            shown = value
        print(f"  [{'ok ' if value else 'MISSING'}] {name.upper():<20} {shown}")

    if not (config.secrets.ghl_api_token and config.secrets.ghl_location_id):
        print("\nSkipping GHL checks - no token/location set.")
        return 0

    print("\nGHL:")
    with _open_client(config) as client:
        sites = client.list_blog_sites()
        print(f"  blog sites: {len(sites)}")
        for site in sites:
            site_id = site.get("_id") or site.get("id")
            marker = "  <- GHL_BLOG_ID" if site_id == config.secrets.ghl_blog_id else ""
            print(f"    {site_id}  {site.get('name') or site.get('title') or '?'}{marker}")

        author_id, category_ids = _resolve_ids(client, config)
        print(f"  author id:   {author_id}")
        print(f"  category id: {category_ids[0]}")

        if config.secrets.ghl_blog_id:
            taken = _taken_days(client, config, config.secrets.ghl_blog_id)
            print(f"  days already holding a post: {len(taken)}")
            upcoming = sorted(d for d in taken if d >= date.today())
            if upcoming:
                print(f"  next booked days: {', '.join(str(d) for d in upcoming[:8])}")
    return 0


def cmd_plan(args) -> int:
    config = load_config()
    ledger = Ledger.load()
    pending, done = _collect_videos(args, ledger)

    if done:
        print(f"{len(done)} video(s) already processed - skipping.")
    if not pending:
        print("Nothing pending. The playlist is fully processed.")
        return 0

    taken: set[date] = set()
    if config.secrets.ghl_api_token and config.secrets.ghl_location_id and config.secrets.ghl_blog_id:
        with _open_client(config) as client:
            taken = _taken_days(client, config, config.secrets.ghl_blog_id)
        print(f"Read {len(taken)} occupied day(s) from GHL.\n")
    else:
        print("No GHL credentials - slots are computed against an empty calendar.\n")

    slots = next_open_slots(taken, len(pending), config.schedule)
    print(f"{len(pending)} post(s) to create:\n")
    for video, slot in zip(pending, slots):
        print(f"  {pipeline.format_slot(slot)}")
        print(f"    {video.title}")
        print(f"    {video.short_url}\n")
    return 0


def cmd_run(args) -> int:
    config = load_config()
    ledger = Ledger.load()
    output_dir = Path(args.output) if args.output else DEFAULT_OUTPUT_DIR

    pending, done = _collect_videos(args, ledger)
    if done:
        print(f"{len(done)} video(s) already processed - skipping.")
    if not pending:
        print("Nothing to do.")
        return 0

    if args.transcript and len(pending) > 1:
        raise CLIError("--transcript applies to a single video; use --video with it.")

    status = ghl.STATUS_DRAFT if args.draft else ghl.STATUS_SCHEDULED
    client = None
    author_id = ""
    category_ids: list[str] = []
    taken: set[date] = set()
    blog_id = config.secrets.ghl_blog_id or ""

    if not args.local_only:
        config.secrets.require("ghl_blog_id")
        client = _open_client(config)
        author_id, category_ids = _resolve_ids(client, config)
        taken = _taken_days(client, config, blog_id)
        print(f"Read {len(taken)} occupied day(s) from GHL.")

    slots = next_open_slots(taken, len(pending), config.schedule)
    transcript_path = Path(args.transcript) if args.transcript else None
    failures = 0

    try:
        for video, slot in zip(pending, slots):
            print(f"\n[{video.video_id}] {video.title}")
            try:
                transcript = pipeline.resolve_transcript(video, transcript_path=transcript_path)
                post = pipeline.build_post(
                    video, transcript, config, output_dir=output_dir
                )
                post.scheduled_at = slot
                print(f"  slot: {pipeline.format_slot(slot)}")

                if client:
                    post.url_slug = pipeline.ensure_unique_slug(client, post.url_slug)
                    post.canonical_link = config.brand.canonical_link(post.url_slug)
                    if config.cover.alt_text_source == "url_slug":
                        post.cover_alt_text = post.url_slug
                    pipeline.publish_post(
                        post, config, client,
                        blog_id=blog_id,
                        author_id=author_id,
                        category_ids=category_ids,
                        status=status,
                        dry_run=args.dry_run,
                    )

                for warning in post.warnings:
                    print(f"  ! {warning}")

                if not args.dry_run and not args.local_only:
                    ledger.record(
                        video_id=video.video_id,
                        title=post.title,
                        url_slug=post.url_slug,
                        scheduled_at=post.scheduled_at,
                        ghl_post_id=post.ghl_post_id,
                    )
                    ledger.save()

            except (IngestError, CopywriterError, cover_mod.CoverError, ghl.GHLError,
                    PipelineError) as exc:
                failures += 1
                print(f"  FAILED: {exc}", file=sys.stderr)
                continue
    finally:
        if client:
            client.close()

    processed = len(pending) - failures
    print(f"\nDone. {processed} post(s) built, {failures} failed. Output: {output_dir}")
    if args.dry_run:
        print("Dry run - nothing was sent to GHL and the ledger was not updated.")
    return 1 if failures else 0


def cmd_bot(args) -> int:
    from .bot import run_bot

    config = load_config()
    print("Starting the Discord bot. Ctrl-C to stop.")
    run_bot(config)
    return 0


def cmd_cover(args) -> int:
    config = load_config()
    plan = CoverPlan(kicker=args.kicker.upper(), headline=args.headline.upper())
    out = Path(args.output)
    cover_mod.render_cover(plan, config, out, keep_html=args.keep_html)
    print(f"Wrote {out}")
    return 0


# ----------------------------------------------------------------------- parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wilbyte",
        description="Turn Agent Lead Lab YouTube videos into scheduled GHL blog posts.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check config, credentials, and GHL access").set_defaults(
        func=cmd_doctor
    )

    def add_source(p):
        source = p.add_mutually_exclusive_group(required=True)
        source.add_argument("--playlist", help="YouTube playlist URL or id")
        source.add_argument("--video", help="a single YouTube video URL or id")
        p.add_argument("-n", "--limit", type=int, help="max videos to process")
        p.add_argument("--force", action="store_true", help="reprocess videos already in the ledger")

    plan = sub.add_parser("plan", help="show what would be posted and when")
    add_source(plan)
    plan.set_defaults(func=cmd_plan)

    run = sub.add_parser("run", help="write, render, and schedule posts")
    add_source(run)
    run.add_argument("--transcript", help="use this transcript file instead of fetching from YouTube")
    run.add_argument("--output", help=f"output directory (default: {DEFAULT_OUTPUT_DIR})")
    run.add_argument("--draft", action="store_true", help="create posts as DRAFT instead of SCHEDULED")
    run.add_argument("--dry-run", action="store_true", help="build everything, print the payload, send nothing")
    run.add_argument("--local-only", action="store_true", help="skip GHL entirely; just write files locally")
    run.set_defaults(func=cmd_run)

    sub.add_parser("bot", help="run the Discord bot").set_defaults(func=cmd_bot)

    cover_cmd = sub.add_parser("cover", help="render a cover image from two lines of text")
    cover_cmd.add_argument("--kicker", required=True, help="the highlighted 3-5 word line")
    cover_cmd.add_argument("--headline", required=True, help="the big line underneath")
    cover_cmd.add_argument("-o", "--output", default="cover.png")
    cover_cmd.add_argument("--keep-html", action="store_true", help="keep the intermediate HTML")
    cover_cmd.set_defaults(func=cmd_cover)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (CLIError, ConfigError, IngestError, CopywriterError, cover_mod.CoverError,
            ghl.GHLError, PipelineError, SchedulerError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
