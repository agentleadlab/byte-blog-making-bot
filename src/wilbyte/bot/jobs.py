"""Blocking pipeline work, wrapped so the Discord event loop never stalls.

Every method here does network or CPU work and is called via `asyncio.to_thread`
from `client.py`. Keeping them free of Discord types also makes them testable
without a gateway connection.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .. import ghl, pipeline, prefs, youtube
from ..config import Config
from ..models import BlogPost, Transcript, Video
from ..scheduler import next_open_slots, taken_days_from_ledger, taken_days_from_posts
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
    """Connect to GHL and resolve the blog, author and category ids up front."""
    config.secrets.require("ghl_api_token", "ghl_location_id")
    client = ghl.GHLClient(config.secrets.ghl_api_token, config.secrets.ghl_location_id)
    try:
        blog_id, _ = ghl.resolve_blog_id(
            client.list_blog_sites(), config.secrets.ghl_blog_id
        )
        author_id = config.secrets.ghl_author_id or ghl.resolve_by_name(
            client.list_authors(), config.post.author, kind="author"
        )
        category_id = config.secrets.ghl_category_id or ghl.resolve_by_name(
            client.list_categories(), config.post.category, kind="category"
        )
    except Exception:
        client.close()
        raise
    return GHLContext(client, blog_id, author_id, [category_id])


def taken_days(
    context: GHLContext | None, config: Config, ledger: Ledger | None = None
) -> set[date]:
    """Every day already spoken for, from GHL and from RYTE's own ledger.

    Both, because neither alone is complete: GHL holds posts written by hand
    that RYTE never saw, and the ledger holds slots GHL has not reported back a
    schedule for. Missing either one double-books a day.
    """
    days: set[date] = set()
    if context is not None:
        days |= taken_days_from_posts(context.client.list_posts(context.blog_id), config.schedule)
    if ledger is not None:
        days |= taken_days_from_ledger(ledger.entries.values(), config.schedule)
    return days


def upcoming_posts(
    context: GHLContext | None,
    config: Config,
    ledger: Ledger | None = None,
    *,
    limit: int = 15,
) -> list[tuple[date, str]]:
    """(day, title) for everything still to go out, soonest first.

    Same two sources as `taken_days`, and for the same reason: GHL holds the
    posts written by hand, the ledger holds the ones GHL won't report a
    schedule for. GHL wins where both know a post - it's the one that would
    show an edit made in the dashboard.
    """
    from zoneinfo import ZoneInfo

    from ..scheduler import parse_timestamp, post_day

    tz = ZoneInfo(config.schedule.timezone)
    today = datetime.now(tz).date()
    found: dict[str, tuple[date, str]] = {}

    if context is not None:
        for post in context.client.list_posts(context.blog_id):
            day = post_day(post, tz)
            if day is None or day < today:
                continue
            title = post.get("title") or post.get("urlSlug") or "(untitled)"
            found[(post.get("urlSlug") or title).lower()] = (day, title)

    if ledger is not None:
        for entry in ledger.entries.values():
            # Already out. It still holds its day, but it is not "still to go
            # out" and listing it as such makes the count disagree with itself.
            if entry.published_at:
                continue
            parsed = parse_timestamp(entry.scheduled_at) if entry.scheduled_at else None
            if parsed is None:
                continue
            day = parsed.astimezone(tz).date()
            if day < today:
                continue
            found.setdefault(
                (entry.url_slug or entry.title).lower(),
                (day, entry.title or entry.url_slug),
            )

    return sorted(found.values())[:limit]


def waiting_on_captions(link: str) -> tuple[str, str]:
    """(transcript, why not) for a video, without building anything.

    An announcement fires the moment a video goes up, and YouTube has not
    captioned it yet. Trying the whole run to find that out costs three
    messages in the channel to say nothing happened - so the transcript is
    asked for first, and the answer decides whether there is a run at all.

    The text comes back rather than being thrown away, because the run takes a
    transcript directly and fetching it twice would be a minute of nothing for
    no reason.
    """
    try:
        video = youtube.video_from_link(link)
        return youtube.fetch_transcript(video.video_id).text, ""
    except Exception as exc:
        return "", _readable_error(exc)


def _readable_error(exc: Exception) -> str:
    """An error with the terminal escape codes taken out of it."""
    import re as _re

    return " ".join(_re.sub(r"\x1b\[[0-9;]*m|\[[0-9];[0-9]{2}m", "", str(exc)).split())


def resolve_videos(
    source: str, ledger: Ledger, *, limit: int, force: bool, offline: bool = False
) -> tuple[list[Video], int]:
    """Expand a playlist or single-video URL into the videos still to process.

    `offline` skips the metadata lookup, which matters when a transcript was
    supplied by hand precisely because YouTube is refusing this server.
    """
    if youtube.looks_like_playlist(source):
        videos = youtube.list_playlist_videos(source)
    elif offline:
        videos = [youtube.video_from_link(source)]
    else:
        try:
            videos = [youtube.fetch_video(source)]
        except youtube.IngestError:
            # The title is only a hint to the copywriter, so a blocked metadata
            # lookup is no reason to end the run - the id is in the URL. Let it
            # fail later at the transcript, which is the step that actually
            # matters and whose error says what to do about it.
            videos = [youtube.video_from_link(source)]

    if force:
        return videos[:limit], 0

    pending, done = pipeline.select_pending_videos(videos, ledger, limit=limit)
    return pending, len(done)


def open_slots(
    taken: set[date], count: int, config: Config, *, include_today: bool = False
) -> list[datetime]:
    """The next free slots, honouring an earliest day set from Discord.

    Every slot decision goes through here so the floor can't apply in one view
    and not another - a calendar that disagrees with itself is worse than one
    that's simply wrong.
    """
    return next_open_slots(
        taken, count, prefs.apply(config).schedule, include_today=include_today
    )


def resolve_many(
    sources: list[str] | tuple[str, ...],
    ledger: Ledger,
    *,
    limit: int,
    force: bool,
    offline: bool = False,
) -> tuple[list[Video], int]:
    """Expand every link in the message into the videos still to process.

    A week of posts usually arrives as a dozen pasted links rather than a
    playlist, so each one is expanded in turn and the results concatenated in
    the order they were typed. A video named twice makes one post: repeats are
    easy to paste and expensive to publish.
    """
    videos: list[Video] = []
    seen: set[str] = set()
    already_done = 0

    for source in sources:
        if len(videos) >= limit:
            break
        found, done = resolve_videos(
            source, ledger, limit=limit - len(videos), force=force, offline=offline
        )
        already_done += done
        for video in found:
            if video.video_id not in seen:
                seen.add(video.video_id)
                videos.append(video)

    return videos[:limit], already_done


def plan_slots(
    videos: list[Video],
    context: GHLContext | None,
    config: Config,
    ledger: Ledger | None = None,
    *,
    include_today: bool = False,
) -> list[datetime]:
    return open_slots(
        taken_days(context, config, ledger), len(videos), config,
        include_today=include_today,
    )


def run_slots(
    videos: list[Video],
    context: GHLContext | None,
    config: Config,
    ledger: Ledger,
    *,
    include_today: bool = False,
) -> tuple[list[datetime], dict]:
    """(the free slots for this run, {video id: the day it is getting back}).

    A video whose post was deleted and is being run again gets the day that
    post held, when it is still free; the rest share the next free days, and
    never the one held back for it.
    """
    booked = taken_days(context, config, ledger)
    held = held_for(videos, ledger, booked, config)
    pool = open_slots(
        booked | {slot.date() for slot in held.values()}, len(videos) - len(held), config,
        include_today=include_today,
    )
    return pool, held


def deleted_since(context: GHLContext, ledger: Ledger, sources) -> list:
    """The posts for these videos that were deleted in GHL. Forgotten, with
    the day each held remembered for its rerun.

    "i need this remove from blog posting but i will rerun it ... ryte has to
    know that im deleting it that way he'll schedule it again same date".
    Asked only about the videos being run, so it costs one request each and
    only when a video comes round again. A post that has gone out is left
    alone: deleting a published post is taking it down, not redoing it.
    """
    wanted = set()
    for source in sources or ():
        try:
            wanted.add(youtube.video_from_link(str(source)).video_id)
        except Exception:
            continue
    gone = []
    for video_id in wanted:
        entry = ledger.entries.get(video_id)
        if entry is None or entry.published_at or not entry.url_slug:
            continue
        try:
            there = context.client.slug_exists(entry.url_slug)
        except ghl.GHLError:
            continue
        if not there:
            gone.append(entry)
            ledger.forget(video_id, keep_slot=True)
    if gone:
        ledger.save()
    return gone


def held_for(videos: list[Video], ledger: Ledger, taken: set, config: Config) -> dict:
    """{video id: the slot it held before its post was deleted}, for the ones
    whose day is still free and still ahead - so a rerun lands back on it."""
    from zoneinfo import ZoneInfo

    from ..scheduler import parse_timestamp

    tz = ZoneInfo(config.schedule.timezone)
    soonest = datetime.now(tz) + timedelta(minutes=config.schedule.min_lead_minutes)
    held = {}
    for video in videos:
        raw = ledger.freed.get(video.video_id)
        slot = parse_timestamp(raw) if raw else None
        if slot is None:
            continue
        slot = slot.astimezone(tz)
        if slot >= soonest and slot.date() not in taken and slot.date() not in {
            one.date() for one in held.values()
        }:
            held[video.video_id] = slot
    return held


def build(
    video: Video,
    config: Config,
    output_dir: Path,
    *,
    transcript_text: str | None = None,
    related=(),
) -> BlogPost:
    """Transcript -> copy -> title -> cover image. No GHL contact.

    `transcript_text` skips the fetch entirely, which is how an attached
    transcript gets used when YouTube refuses to serve one. `related` is the
    blog's published posts, [(title, url)] - see `link_targets`.
    """
    if transcript_text:
        transcript = Transcript(
            video_id=video.video_id,
            text=youtube.clean_transcript(transcript_text),
            source="manual",
        )
    else:
        transcript = youtube.fetch_transcript(video.video_id)

    return pipeline.build_post(
        video, transcript, config, output_dir=output_dir, report=lambda _: None,
        related=related,
    )


#: How many of the blog's posts are offered to link to. The newest, when
#: there are more: enough to find three related ones in, short enough not to
#: bury the transcript.
LINK_TARGETS = 80


def link_targets(context: "GHLContext | None", config: Config, ledger: Ledger | None = None) -> list:
    """[(title, url)] for the blog's live posts - what an article may link to.

    Only published ones: a link to a post still scheduled is a 404 until its
    day comes. From GHL when it answers, so posts written by hand count too;
    from RYTE's own record of what he published when it doesn't.
    """
    found: dict[str, str] = {}
    if context is not None:
        try:
            for post in context.client.list_posts(context.blog_id):
                slug = str(post.get("urlSlug") or "").strip()
                if not slug or str(post.get("status") or "").upper() != "PUBLISHED":
                    continue
                if post.get("deleted") or post.get("archived"):
                    continue
                found[slug] = str(post.get("title") or slug).strip()
        except Exception:
            import logging

            logging.getLogger("wilbyte.bot").warning(
                "Couldn't list the blog's posts to link to", exc_info=True
            )
    if not found and ledger is not None:
        for entry in ledger.entries.values():
            if entry.published_at and entry.url_slug:
                found[entry.url_slug] = entry.title or entry.url_slug
    pairs = [(title, config.brand.canonical_link(slug)) for slug, title in found.items()]
    return pairs[-LINK_TARGETS:]


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

    published = pipeline.publish_post(
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

    if status == ghl.STATUS_SCHEDULED:
        published.warnings.extend(schedule_warnings(published))
    return published


def schedule_warnings(post: BlogPost) -> list[str]:
    """What would stop RYTE publishing this post when its slot arrives.

    GHL's scheduler never fires on a post its API created, so RYTE publishes
    them itself. That needs two things: the id GHL returned, and the body that
    was sent - its update is a replace and its list endpoint omits the article,
    so the saved payload is the only copy that will still exist by then.
    Missing either is worth saying now rather than at 10am on the day.
    """
    if post.scheduled_at is None:
        return []

    problems = []
    if not post.ghl_post_id:
        problems.append(
            "GHL didn't return an id for this post, so I can't publish it when "
            "its day comes — publish it by hand in the dashboard."
        )
    if not post.ghl_payload_path:
        problems.append(
            "I couldn't save this post's body, so I can't publish it later — "
            "publish it by hand in the dashboard."
        )
    return problems


def next_pending(ledger: Ledger):
    """The soonest post RYTE is still holding and *can* publish, or None."""
    waiting = [e for e in ledger.entries.values() if publishable(e)]
    return min(waiting, key=lambda e: e.scheduled_at) if waiting else None


def publishable(entry) -> bool:
    return bool(
        not entry.published_at and entry.scheduled_at
        and entry.ghl_post_id and entry.payload_path
    )


def stuck_posts(ledger: Ledger) -> list[tuple[str, str]]:
    """(title, why) for scheduled posts RYTE will never be able to publish.

    Skipping these quietly is the same failure as GHL's: the post sits looking
    scheduled and the day passes. Whatever else is wrong, this has to be said
    out loud.
    """
    stuck = []
    for entry in ledger.entries.values():
        if entry.published_at or not entry.scheduled_at or publishable(entry):
            continue
        if not entry.ghl_post_id:
            stuck.append((entry.title or entry.url_slug, "GHL never gave me its post id"))
        else:
            stuck.append((entry.title or entry.url_slug, "I have no saved copy of its body"))
    return stuck


def set_status(context: GHLContext, entry, status: str) -> datetime | None:
    """Re-send a pending post under a different status, keeping its date.

    Used to answer the one question that decides whether RYTE has to be awake
    at 10am: does GHL's blog hide a PUBLISHED post whose date is in the future?
    If it does, the date alone schedules the post and nothing needs to be
    running. If it doesn't, the post appears early and this puts it straight
    back.
    """
    from .. import publisher
    from ..scheduler import parse_timestamp

    payload = publisher.load_payload(entry)
    payload[ghl.POST_FIELDS["status"]] = status
    slot = parse_timestamp(entry.scheduled_at) if entry.scheduled_at else None
    if slot:
        payload[ghl.SCHEDULE_FIELD] = ghl.to_api_timestamp(slot)
    context.client.update_post(entry.ghl_post_id, payload)
    return slot


def pending_posts(config: Config, ledger: Ledger) -> list[tuple[str, str, datetime | None]]:
    """(video id, title, when) for every post RYTE is still holding.

    Soonest first, and only the ones it can actually re-send: an entry without
    a saved payload can't be moved any more than it can be published, because
    an update to GHL is a replace and there is nowhere else to get the body.
    """
    from zoneinfo import ZoneInfo

    from ..scheduler import parse_timestamp

    tz = ZoneInfo(config.schedule.timezone)
    found = []
    for entry in ledger.entries.values():
        if not publishable(entry):
            continue
        when = parse_timestamp(entry.scheduled_at)
        found.append(
            (entry.video_id, entry.title or entry.url_slug, when.astimezone(tz) if when else None)
        )
    # No date sorts last: it has no place in the running order, so it takes
    # whatever slot is left rather than displacing a post that has one.
    return sorted(found, key=lambda item: (item[2] is None, item[2] or datetime.min))


def reschedule_plan(
    config: Config, ledger: Ledger, *, context: GHLContext | None = None,
    include_today: bool = False,
):
    """What re-laying the calendar would do, without writing anything.

    The days these posts already hold are taken out of the reckoning first.
    Otherwise every post blocks its own move - Monday is taken, by the post
    that wants to move off Monday - and the queue never budges.
    """
    from zoneinfo import ZoneInfo

    from .. import rearrange

    tz = ZoneInfo(config.schedule.timezone)
    posts = pending_posts(config, ledger)
    if not posts:
        return []

    booked = taken_days(context, config, ledger) - rearrange.held_days(posts, tz)
    slots = open_slots(booked, len(posts), config, include_today=include_today)
    return rearrange.pair(posts, slots)


def apply_moves(config: Config, ledger: Ledger, context: GHLContext, moves) -> list[str]:
    """Write the new dates to GHL and to the ledger. Returns what failed.

    Each post is saved to the ledger as it goes, one at a time. A run that dies
    halfway then leaves RYTE agreeing with GHL about the posts it managed,
    instead of holding a set of dates nothing else believes in.
    """
    from .. import publisher

    problems = []
    for move in moves:
        if not move.moved:
            continue
        entry = ledger.entries.get(move.video_id)
        if entry is None:
            problems.append(f"{move.title} — I've lost my record of it")
            continue
        payload = None
        try:
            payload = publisher.load_payload(entry)
            payload[ghl.POST_FIELDS["status"]] = ghl.STATUS_SCHEDULED
            payload[ghl.SCHEDULE_FIELD] = ghl.to_api_timestamp(move.now)
            publisher.send_update(context.client, entry, payload)
        except Exception as exc:
            # The whole thing, not a trimmed version, and what was sent with
            # it. When GHL refuses every post in a queue it refuses them for
            # one reason, and that reason was being cut off the end of the
            # message fifteen times over.
            sent = f"\n  sent: {', '.join(sorted(payload))}" if payload else ""
            problems.append(f"{move.title} — {exc}{sent}")
            continue
        entry.scheduled_at = move.now.isoformat()
        ledger.save()
    return problems


def publish_now(config: Config, ledger: Ledger, context: GHLContext, entry) -> datetime:
    """Send one held post out immediately, whatever day it was booked for.

    The date goes out as now rather than being left alone: a PUBLISHED post
    still carrying Monday's date reads as published-on-Monday everywhere it is
    listed, which is not what happened.
    """
    from datetime import timezone as _tz

    from .. import publisher

    now = datetime.now(_tz.utc)
    payload = publisher.load_payload(entry)
    payload[ghl.POST_FIELDS["status"]] = ghl.STATUS_PUBLISHED
    payload[ghl.SCHEDULE_FIELD] = ghl.to_api_timestamp(now)
    publisher.send_update(context.client, entry, payload)

    entry.scheduled_at = now.isoformat()
    ledger.mark_published(entry.video_id)
    ledger.save()
    return now


def held_on(config: Config, ledger: Ledger, day: date) -> list:
    """Every post RYTE is holding for a given day.

    A list rather than one post, because "publish Monday's" has to be able to
    say "there are two" instead of picking one of them.
    """
    from zoneinfo import ZoneInfo

    from ..scheduler import parse_timestamp

    tz = ZoneInfo(config.schedule.timezone)
    found = []
    for entry in ledger.entries.values():
        if entry.published_at or not entry.scheduled_at:
            continue
        when = parse_timestamp(entry.scheduled_at)
        if when and when.astimezone(tz).date() == day:
            found.append(entry)
    return found


def probe_update(config: Config) -> list[str]:
    """Find out which update GHL crashes on, without touching a live post.

    Every move came back 400 with `Cannot read properties of undefined
    (reading 'childTaskError')`. That is not GHL objecting to a field - it is
    a null dereference inside their own code, in something that handles a
    post's scheduling task. So subtracting fields one at a time was the wrong
    experiment; what matters is which *transition* trips it.

    So this walks a throwaway draft through the transitions in order:
    scheduling it, re-dating it while scheduled (which is what a move does),
    unscheduling it, and scheduling it again. If re-dating is the only one
    that fails, then dropping to draft first and scheduling again is the way
    round it - and the last two steps prove that in the same run.

    It ends on DRAFT whatever happens, so nothing is left pointing at the
    blog. There is no delete endpoint, so the post itself stays until somebody
    removes it by hand - a small price for not experimenting on fifteen live
    articles.
    """
    from zoneinfo import ZoneInfo

    context = open_ghl(config)
    lines = []
    try:
        client = context.client
        slug = f"ryte-update-probe-{date.today():%Y%m%d}"
        for suffix in range(2, 40):
            if not client.slug_exists(slug):
                break
            slug = f"ryte-update-probe-{date.today():%Y%m%d}-{suffix}"

        full = ghl.build_post_payload(
            location_id=client.location_id,
            blog_id=context.blog_id,
            title="RYTE update probe — safe to delete",
            content_html="<p>Checking what the update endpoint accepts.</p>",
            description="Delete me.",
            url_slug=slug,
            canonical_link=config.brand.canonical_link(slug),
            author_id=context.author_id,
            category_ids=context.category_ids,
            keywords=[],
            image_url=None,
            image_alt="probe",
            status=ghl.STATUS_DRAFT,
            published_at=None,
        )
        created = client.create_post(full)
        post_id = str(created.get("_id") or created.get("id") or "")
        if not post_id:
            return [f"❌ Couldn't even create the probe post: {created}"]
        lines.append(f"Probe draft created (`{slug}`) — delete it in GHL when we're done.")

        now = datetime.now(ZoneInfo(config.schedule.timezone))
        first = ghl.to_api_timestamp(now + timedelta(days=30))
        second = ghl.to_api_timestamp(now + timedelta(days=31))

        def scheduled(when):
            return {
                **full,
                ghl.POST_FIELDS["status"]: ghl.STATUS_SCHEDULED,
                ghl.SCHEDULE_FIELD: when,
            }

        def drafted():
            return _less({**full, ghl.POST_FIELDS["status"]: ghl.STATUS_DRAFT}, "publishedAt")

        # In order, because each one leaves the post in the state the next one
        # is testing. Step 2 is what a move does; steps 4 and 5 are the way
        # round it, if there is one.
        steps = [
            ("1. draft → scheduled", scheduled(first)),
            ("2. scheduled → scheduled, new date (what a move does)", scheduled(second)),
            ("3. scheduled → draft", drafted()),
            ("4. draft → scheduled, new date", scheduled(second)),
            ("5. same again, no body — just blogId, status, date", {
                "blogId": context.blog_id,
                "status": ghl.STATUS_SCHEDULED,
                ghl.SCHEDULE_FIELD: first,
            }),
        ]
        for label, payload in steps:
            try:
                client.update_post(post_id, payload)
            except Exception as exc:
                lines.append(f"❌ {label}\n   {_short(exc, 240)}")
            else:
                lines.append(f"✅ {label}")

        # However it went, leave it as a draft so nothing points at the blog.
        try:
            client.update_post(post_id, drafted())
        except Exception:
            lines.append("⚠ Couldn't put the probe back to draft — delete it in GHL.")
    finally:
        context.close()
    return lines


def _less(payload: dict, *drop: str) -> dict:
    return {key: value for key, value in payload.items() if key not in drop}


def built_but_not_posted(output_dir: Path, ledger: Ledger) -> list[tuple[str, str]]:
    """(title, video link) for posts RYTE wrote but never got an answer on.

    Every build writes its files before the approval prompt goes up, so a post
    that timed out is still on disk with its source link in `ghl-fields.txt`.
    That is the only record of what a skipped post came from - the ledger only
    learns about a post once it reaches GHL.

    Matching is on the video id from the link, not the folder name: the slug can
    be changed at publish time to avoid a clash, and then the two disagree.
    """
    found: list[tuple[str, str]] = []
    if not output_dir.exists():
        return found

    for fields in sorted(output_dir.glob("*/ghl-fields.txt")):
        title, link = "", ""
        try:
            for line in fields.read_text(encoding="utf-8").splitlines():
                if line.startswith("Title:"):
                    title = line.split(":", 1)[1].strip()
                elif line.startswith("Source video:"):
                    link = line.split(":", 1)[1].strip()
        except OSError:
            continue

        if not link:
            continue
        video_id = link.rstrip("/").rsplit("/", 1)[-1]
        if ledger.has(video_id):
            continue
        found.append((title or fields.parent.name, link))
    return found


def reconcile(context: GHLContext, ledger: Ledger) -> tuple[list, list, list[str]]:
    """Drop ledger entries whose post is no longer in GHL. (gone, kept, problems)

    RYTE keeps its own calendar because GHL's API won't list scheduled posts,
    and the cost of that is drift: delete a post in the dashboard and RYTE goes
    on holding its day forever, leaving a gap in the week nobody can explain.

    The slug endpoint is what makes this safe. It answers for a post in any
    status, so "not there" really means deleted - unlike the post listing,
    which omits scheduled posts entirely and would have this throw away days
    that are legitimately booked.
    """
    gone, kept, problems = [], [], []
    for entry in list(ledger.entries.values()):
        if entry.published_at or not entry.url_slug:
            kept.append(entry)
            continue
        try:
            exists = context.client.slug_exists(entry.url_slug)
        except ghl.GHLError as exc:
            problems.append(f"{entry.title or entry.url_slug}: {_short(exc)}")
            kept.append(entry)
            continue
        if exists:
            kept.append(entry)
        else:
            gone.append(entry)
            ledger.forget(entry.video_id, keep_slot=True)
    if gone:
        ledger.save()
    return gone, kept, problems


def file_recording(config: Config, rec, *, summary: str = "") -> tuple[str, str]:
    """Put one recording in the Notion gallery. Returns (title, page url).

    Creates the database on first use rather than making it a setup step: the
    Sales Calls Recording page is empty, and a gallery view needs a database
    behind it. A second run finds the one that's there instead of making
    another.
    """
    from .. import notion, recordings

    config.secrets.require("notion_token", "notion_recordings_page_id")
    page_id = config.secrets.notion_recordings_page_id
    client = notion.NotionClient(config.secrets.notion_token)
    try:
        # No name: write into the gallery that is already on the page,
        # whatever its owner called it. Asking for one named after our own
        # convention is how a second database appeared beside "‼️ Recordings ‼️".
        database_id = client.find_child_database(page_id)
        if not database_id:
            database_id = client.create_database(
                page_id, recordings.TITLE_PREFIX + "s", recordings.database_schema()
            )

        # The link and the passcode are what a recording *is*, so make sure
        # there is somewhere to put them before writing the row.
        client.add_columns(database_id, recordings.EXTRA_COLUMNS)

        title = recordings.call_title(rec)
        cover_url, icon_url = gallery_art(config, client, page_id)

        created = client.create_page(
            database_id,
            recordings.map_properties(
                (client.database(database_id).get("properties") or {}),
                rec,
                title,
                description=recordings.description_for(rec),
            ),
            children=recordings.page_blocks(rec, summary),
            cover_url=cover_url,
            icon_url=icon_url,
            icon_emoji=None if icon_url else (
                config.secrets.notion_icon_emoji or DEFAULT_CARD_ICON
            ),
        )
    finally:
        client.close()
    return title, str(created.get("url") or "")


# A gallery of identical banners is a gallery you can't skim, so each card gets
# its own cover naming the call. The emoji is the fallback icon: Notion stores
# it, so it never expires and needs nothing uploaded.
DEFAULT_CARD_ICON = "🎙️"


def gallery_art(config: Config, client, page_id: str) -> tuple[str, str]:
    """(cover, icon) for a card, taken from the page the gallery lives on.

    The rest of the workspace already looks a particular way - the SOP cards
    all carry the Headquarters banner - and a card that matches belongs in the
    gallery in a way a bespoke one doesn't. So rather than inventing artwork or
    asking for a file, take what the page itself is wearing.

    The catch is that Notion serves its own uploads from URLs that expire
    within the hour, and a card cover set to one of those is blank by tomorrow.
    So the image is copied once into the GHL media library, which is public and
    permanent, and the result is remembered.
    """
    import logging

    from .. import prefs

    log = logging.getLogger("wilbyte.bot")
    saved = prefs.load()
    cover = config.secrets.notion_cover_url or saved.get("recording_cover_url") or ""
    icon = config.secrets.notion_icon_url or saved.get("recording_icon_url") or ""
    if cover and icon:
        return cover, icon

    try:
        page = client.page(page_id)
    except Exception as exc:
        log.warning("Couldn't read the page's artwork: %s", exc)
        return cover, icon

    changed = False
    if not cover:
        cover = _rehost(config, _asset_url(page.get("cover")), "recordings-cover.png")
        if cover:
            saved["recording_cover_url"] = cover
            changed = True
    if not icon:
        icon = _rehost(config, _asset_url(page.get("icon")), "recordings-icon.png")
        if icon:
            saved["recording_icon_url"] = icon
            changed = True
    if changed:
        prefs.save(saved)
    return cover, icon


def _asset_url(block) -> str:
    """The URL inside a Notion cover/icon object, whichever kind it is."""
    if not isinstance(block, dict):
        return ""
    for key in ("external", "file"):
        url = (block.get(key) or {}).get("url")
        if url:
            return str(url)
    return ""


def _rehost(config: Config, url: str, name: str) -> str:
    """Copy an image to somewhere permanent. Returns "" if it can't be done."""
    if not url:
        return ""

    import logging

    import httpx

    from ..pipeline import DEFAULT_OUTPUT_DIR

    path = DEFAULT_OUTPUT_DIR / "recordings" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        response = httpx.get(url, timeout=60, follow_redirects=True)
        response.raise_for_status()
        path.write_bytes(response.content)
        return host_image(config, path, name=name)
    except Exception as exc:  # artwork is never worth failing a card over
        logging.getLogger("wilbyte.bot").warning("Couldn't re-host %s: %s", name, exc)
        return ""


def host_image(config: Config, path: Path, *, name: str) -> str:
    """Put an image somewhere with a permanent public URL, and return it.

    Notion only accepts an external URL for a cover or icon - it never fetches
    and re-hosts - so a link that expires leaves every card blank a week later.
    The GHL media library is already connected, already public, and already
    where the blog cover images live, so it is the obvious place rather than a
    new account somewhere.
    """
    config.secrets.require("ghl_api_token", "ghl_location_id")
    client = ghl.GHLClient(config.secrets.ghl_api_token, config.secrets.ghl_location_id)
    try:
        return client.upload_media(path, name=name)
    finally:
        client.close()


def zoom_transcript(config: Config, rec) -> str:
    """The transcript behind a Zoom share link, or "" when there isn't one.

    An empty answer is the ordinary case, not a failure: Zoom only writes a
    transcript when audio transcript was enabled *at the time of recording*, so
    a correctly configured account still returns nothing for calls made before
    the setting was turned on.
    """
    from .. import zoom

    client = zoom.ZoomClient(
        config.secrets.zoom_account_id,
        config.secrets.zoom_client_id,
        config.secrets.zoom_client_secret,
    )
    try:
        from .. import recordings

        meetings = client.account_recordings(days=30)
        # The share page names the recording. It is the only thing that ties
        # the pasted link to a particular call, so it is worth one extra
        # request.
        page_topic = client.share_page_topic(rec.url)
        found, how = zoom.choose(
            meetings,
            link=rec.url,
            passcode=rec.passcode,
            page_topic=page_topic,
            filed=recordings.filed_ids(),
        )
        if found is None:
            # Filed with the link and nothing else, on purpose. Guessing put a
            # summary of somebody else's call on a card twice, and a summary
            # under the wrong name is not visibly wrong to whoever reads it.
            if rec.passcode:
                why = (
                    f"the passcode doesn't match any of the {len(meetings)} recordings "
                    "on the account either"
                )
            else:
                why = "and no passcode was posted with it"
            seen = f" The share page called it “{page_topic}”." if page_topic else ""
            rec.note = (
                f"I can't tell which call this link points at, so I've filed it "
                f"without a summary rather than guess — Zoom's API can't resolve "
                f"share links, {why}.{seen}"
            )
            return ""

        rec.matched_by = how
        recordings.remember_filed(found.uid)
        return zoom_read(config, rec, found, client=client)
    finally:
        client.close()


def zoom_read(config: Config, rec, found, *, client=None) -> str:
    """Download one recording's transcript and take the card's names from it."""
    from .. import zoom

    owned = client is None
    if owned:
        client = zoom.ZoomClient(
            config.secrets.zoom_account_id,
            config.secrets.zoom_client_id,
            config.secrets.zoom_client_secret,
        )
    try:
        if not found.has_transcript:
            rec.note = (
                f"Zoom has no transcript for “{found.topic}”. Audio transcript has "
                "to be on *before* a call is recorded — it can't be made afterwards."
            )
        # Carry the meeting details back for the title and the card details.
        rec.topic = found.topic
        rec.host_email = found.host_email

        from ..youtube import parse_captions

        # Downloaded once, read two ways: the raw cues carry the speaker labels
        # that name the card, and the flattened prose is what gets summarised.
        vtt = client.transcript_vtt(found)
        text = parse_captions(vtt)
        # Zoom's API gives a host email and a topic and nothing else, so the
        # names on the card come out of the transcript, which labels every turn.
        closer, guests = zoom.host_and_guests(vtt, found.host_email)
        if closer:
            rec.closer = closer
        if guests:
            rec.guests = guests
        if closer or guests:
            rec.participants = tuple(part for part in (closer, *guests) if part)
        return text
    finally:
        # Only close what this call opened - the caller that passed a client in
        # is still using it.
        if owned:
            client.close()


def fathom_transcript(config: Config, rec) -> str:
    """The transcript of a Fathom-recorded call, or "" when it can't be found.

    Fathom is the better source where both exist: it sits in the meeting as a
    notetaker, so there is no passcode and no transcription setting that might
    have been off at the time.
    """
    from .. import fathom

    from .. import recordings

    client = fathom.FathomClient(config.secrets.fathom_api_key)
    try:
        found, seen = client.look_for(rec.url)
        how = "the link" if found is not None else ""
        if found is None:
            # Say what was actually there rather than "not found". A link that
            # doesn't match is exactly when the response shape matters.
            import logging

            logging.getLogger("wilbyte.bot").warning(
                "No Fathom call matched %s. %s", rec.url, fathom.describe(seen)
            )
            rec.note = f"I couldn't find that call in Fathom. {fathom.describe(seen)}"
            return ""

        rec.matched_by = how
        return fathom_read(config, rec, found, client=client)
    finally:
        client.close()


def fathom_read(config: Config, rec, found, *, client=None) -> str:
    """Take a Fathom call's details and text. Shared by the link and hand-picked
    paths, so a call chosen by name files exactly like one that matched."""
    from .. import fathom, recordings

    owned = client is None
    if owned:
        client = fathom.FathomClient(config.secrets.fathom_api_key)
    try:
        recordings.remember_filed(fathom.meeting_id(found.raw or {}))
        rec.topic = found.title
        rec.closer = found.recorded_by
        rec.guests = found.guests
        if found.participants:
            rec.participants = found.participants
        # Fathom writes its own summary. Prefer it: it costs nothing, it reads
        # the way the team's own tool describes a call, and it means the
        # transcript never has to be fetched at all.
        if found.summary:
            rec.fathom_summary = found.summary
            return ""

        text = fathom.transcript_text(found.raw or {})
        # Listings come back without transcripts to stay inside the rate limit,
        # so a call Fathom hasn't summarised needs one more request.
        return text or client.transcript_for(fathom.meeting_id(found.raw or {}))
    finally:
        if owned:
            client.close()


# Further back than filing looks. A call is filed the day it happens; one being
# cut into clips is whichever interview came up in the meeting.
SEGMENT_LOOKBACK_DAYS = 90


def timed_call_transcript(config: Config, rec) -> tuple[list, str, str, str]:
    """A recording as timed cues, plus its name, its link and its passcode.

    The same matching as filing, so a link resolves to the same call either
    way - but nothing is remembered as filed here. Segmenting is reading, and
    reading a recording twice should not change what happens to it.
    """
    from ..segments import SegmentError

    if rec.platform == "Zoom":
        return _zoom_cues(config, rec)
    if rec.platform == "Fathom":
        return _fathom_cues(config, rec)
    raise SegmentError(
        f"I can read Zoom and Fathom recordings and YouTube videos. "
        f"{rec.platform} isn't one of them."
    )


def file_interview(
    config: Config, *, name: str, description: str, ask: str = ""
) -> tuple[str, list[str]]:
    """Put a cut-up interview on the board. (card url, problems).

    A new card every time, never an edit of one already there: two interviews
    with the same person are two videos, and the second silently overwriting
    the first would lose a set of timestamps nobody has a copy of.
    """
    from .. import dailyops, trello

    client = open_trello(config)
    try:
        lists = client.board_lists(config.secrets.trello_board_id)
        home = trello.find_list(lists, dailyops.MARKETING)
        if home is None:
            return "", [f"The board has no list called {dailyops.MARKETING!r}."]

        try:
            card = client.create_card(str(home.get("id") or ""), name)
        except Exception as exc:
            return "", [f"Couldn't make the card — {_short(exc, 160)}"]

        # The description is a second request, so it can fail on its own. Say
        # so rather than reporting a card that is there and empty as filed.
        problems: list[str] = []
        try:
            client.set_description(str(card.get("id") or ""), description)
        except Exception as exc:
            problems.append(f"Made the card but couldn't write its description — {_short(exc, 160)}")

        # Every interview card needs a thumbnail and somebody had to remember
        # to ask for one. Its own request, so it fails on its own and says so.
        if ask:
            try:
                client.add_comment(str(card.get("id") or ""), ask)
            except Exception as exc:
                problems.append(
                    f"Made the card but couldn't ask for the image — {_short(exc, 160)}"
                )
        return str(card.get("url") or ""), str(card.get("id") or ""), problems
    finally:
        client.close()


def copy_into_doc(
    config: Config, *, title: str, text: str, paragraphs: list | None = None,
) -> tuple[str, list[str]]:
    """Put an interview's copy into the segments doc, as its own tab.

    (link to the tab, problems).

    Franklin keeps "YOUTUBE LINKS FOR WEBSITE POSTING" with a tab per agent
    and pastes the copy in by hand once RYTE has written it. This is that
    paste - the card stays as it is, because the card is what the board runs
    on and the doc is what the website is posted from.

    Not set up is not a problem worth reporting. The segment command worked
    before there was a doc to write into, and a line saying "nobody set
    SEGMENTS_DOC_ID" is about RYTE rather than about the interview.

    A tab already called this is written into rather than duplicated: two runs
    over the same interview should not leave two tabs for somebody to pick
    between.
    """
    from .. import docs as doc

    if not (getattr(config.secrets, "segments_doc_id", "") or "").strip():
        return "", []
    try:
        with doc.open_docs(config.secrets) as writing:
            already = next(
                (one for one in writing.tabs()
                 if one.title.strip().casefold() == title.strip().casefold()),
                None,
            )
            tab = already or writing.add_tab(title)
            problems = (
                [] if already is None else
                [f"There was already a tab called {title!r}, so it went "
                 "underneath what was in it rather than into a second one."]
            )
            # Laid out when it can be, plain when it can't: the formatting is
            # one request that either all lands or none of it does, and the
            # copy reaching the doc matters more than how it looks.
            if paragraphs:
                try:
                    writing.write_rich(tab, paragraphs)
                    return writing.link_to(tab), problems
                except doc.DocsError as exc:
                    problems.append(
                        f"Couldn't lay it out, so it went in plain: {_short(exc, 160)}"
                    )
            writing.write(tab, text if text.endswith("\n") else text + "\n")
            return writing.link_to(tab), problems
    except doc.DocsError as exc:
        return "", [f"Couldn't write it into the doc: {_short(exc, 200)}"]
    except Exception as exc:
        return "", [f"Couldn't write it into the doc: {_short(exc, 200)}"]


def doc_relayout_plan(config: Config, who: str) -> tuple[list, list[str]]:
    """Whether one named tab of the posting doc can go into the new layout.

    ([(tab, paragraphs, text, what it kept)], [why it is left, or which ones
    the name could mean]). Reads only.

    One tab, named - "no i want him to correct specific only". The tab called
    exactly that if there is one; otherwise the one whose name has it in it,
    and only if there is just the one.
    """
    from .. import docs as doc
    from .. import segments as segmenting

    if not (getattr(config.secrets, "segments_doc_id", "") or "").strip():
        return [], ["SEGMENTS_DOC_ID isn't set, so there's no posting doc to tidy."]
    wanted = " ".join(str(who or "").split()).casefold()
    if not wanted:
        return [], ["Which one? `@RYTE fix doc Crystal Clark` - the tab's name."]
    ready, left = [], []
    with doc.open_docs(config.secrets) as reading:
        every = reading.contents()

        def called(tab) -> str:
            return " ".join(tab.title.split()).casefold()

        picked = [one for one in every if called(one[0]) == wanted]
        if not picked:
            picked = [one for one in every if wanted in called(one[0])]
        if not picked:
            return [], [f"No tab called “{who}” in the posting doc."]
        if len(picked) > 1:
            return [], [
                f"More than one tab could be “{who}” - say which: "
                + ", ".join(f"**{one[0].title}**" for one in picked)
            ]
        for tab, text, odd in picked:
            if not text.strip():
                left.append(f"**{tab.title}** — it's empty")
                continue
            if "\nYOUTUBE TITLE\n" in text or text.lstrip().startswith(f"{tab.title} — interview segments"):
                left.append(f"**{tab.title}** — already in the new layout")
                continue
            if odd:
                left.append(f"**{tab.title}** — left alone: {odd}")
                continue
            try:
                paras = segmenting.relayout(tab.title, text)
            except segmenting.SegmentError as exc:
                left.append(f"**{tab.title}** — left alone: {exc}")
                continue
            _payload, found = segmenting.read_back(text)
            links = sum(1 for one in found if one.youtube)
            emoji, photos = segmenting.pictures_in(text)
            ready.append((tab, paras, text, (
                f"{sum(1 for one in found if not one.long_form)} segments"
                + (" + full interview" if any(one.long_form for one in found) else "")
                + (f", {links} YouTube link{'' if links == 1 else 's'} kept" if links else "")
                + (".\nIt has the 👍 💬 🔔 pasted in as little pictures - "
                   "those come back as real emoji" if emoji else "")
                + (f".\n⚠ It also has {photos} other picture{'' if photos == 1 else 's'} "
                   "that would be **removed**" if photos else "")
            )))
    return ready, left


def doc_relayout(config: Config, planned: list) -> tuple[list[str], list[str]]:
    """Rewrite the planned tabs. (what was redone, problems).

    Each tab is read again first and left alone if anybody changed it since
    the list was shown - the plan was made from what it said then.
    """
    from .. import docs as doc

    done, problems = [], []
    with doc.open_docs(config.secrets) as writing:
        now = {tab.tab_id: text for tab, text, _odd in writing.contents()}
        for tab, paras, text, _kept in planned:
            if now.get(tab.tab_id) != text:
                problems.append(f"**{tab.title}** changed since I looked, so I left it - ask again.")
                continue
            try:
                writing.replace_rich(tab, paras)
            except doc.DocsError as exc:
                problems.append(f"**{tab.title}** — {_short(exc, 160)}")
                continue
            done.append(f"[{tab.title}](<{writing.link_to(tab)}>)")
    return done, problems


#: Who cuts the interviews. Tagged on the Videos Need Editing checklist so the
#: job reaches them rather than sitting on a card they would have to think to
#: open. The MG editors are gone - "tag him instead".
EDITORS = ("@vishwmitrashrivastava",)

#: The card in Marketing Department that tracks what is waiting to be cut -
#: "Videos Need Editing" now, YT VID before it. The old one only if the new
#: one isn't there, so a renamed card never drops a job on the floor.
YT_CARD = "Videos Need Editing"
YT_CARD_BEFORE = "YT VID"

#: Whose list on the day's General card an interview goes onto.
CUTS_THEM = "Faith"


def hand_off_interview(
    config: Config, *, card_url: str, card_id: str, day
) -> tuple[list[str], list[str]]:
    """Put the new interview card where the people who act on it will see it.

    (what was done, problems). Three places, in the order they are done by
    hand:

      1. Faith's checklist on the day's General card, so it is on somebody's
         list for today rather than only on a board.
      2. The Videos Need Editing card's checklist, tagging the two editors
         who cut it.
      3. The interview card itself into Done, because being cut up is what the
         card was for and that part is finished.

    A checklist item whose name is a card's URL is rendered by Trello as a
    link to that card, carrying its title and which list it is in - which is
    why the URL is sent as the whole of the name and nothing else.

    Each step is its own request and its own line. Three things that can fail
    separately should not be reported as one thing that worked.
    """
    from .. import dailyops, trello

    done: list[str] = []
    problems: list[str] = []
    client = open_trello(config)
    try:
        lists = client.board_lists(config.secrets.trello_board_id)
        every = [c for bl in lists for c in client.list_cards(str(bl.get("id") or ""))]

        general = dailyops.cards_covering(every, day).get("general")
        if general is None:
            problems.append(f"No General card dated {day:%m/%d/%y} to put it on.")
        else:
            said = _onto_checklist(
                client, str(general.get("id") or ""), CUTS_THEM, card_url
            )
            (done if said.startswith("✅") else problems).append(said)

        def called(name):
            return next(
                (one for one in every
                 if str(one.get("name") or "").strip().casefold() == name.casefold()),
                None,
            )

        waiting = called(YT_CARD) or called(YT_CARD_BEFORE)
        if waiting is None:
            problems.append(f"No card called {YT_CARD!r} (or {YT_CARD_BEFORE!r}) on the board.")
        else:
            said = _onto_checklist(
                client, str(waiting.get("id") or ""), "",
                f"{card_url} {' '.join(EDITORS)}",
            )
            (done if said.startswith("✅") else problems).append(said)

        gone = trello.find_list(lists, dailyops.DONE)
        if gone is None:
            problems.append(f"The board has no list called {dailyops.DONE!r}.")
        else:
            try:
                client.move_card(card_id, str(gone.get("id") or ""))
                done.append(f"✅ Moved the card to {dailyops.DONE}")
            except Exception as exc:
                problems.append(f"Couldn't move it to {dailyops.DONE} — {_short(exc, 140)}")
        return done, problems
    finally:
        client.close()


def _onto_checklist(client, card_id: str, whose: str, item: str) -> str:
    """Add one item to a checklist on a card. One line saying what happened.

    `whose` names the checklist - "Faith" on the General card, where every
    person has their own. Empty means the card has one list and that is the
    one, which is how the YT VID card is kept.
    """
    try:
        checklists = client.card_checklists(card_id)
    except Exception as exc:
        return f"Couldn't read that card's checklists — {_short(exc, 140)}"
    if not checklists:
        return "That card has no checklist to add to."

    if whose:
        found = next(
            (one for one in checklists
             if whose.casefold() in str(one.get("name") or "").casefold()),
            None,
        )
        if found is None:
            return (
                f"No checklist called {whose!r} on that card, so it wasn't added "
                "— the others were left alone rather than guessed between."
            )
    else:
        found = checklists[0]

    try:
        client.add_check_item(str(found.get("id") or ""), item)
    except Exception as exc:
        return f"Couldn't add it to {found.get('name') or 'that checklist'} — {_short(exc, 140)}"
    return f"✅ Added to **{found.get('name') or 'the checklist'}**"


def timed_call_by_name(config: Config, words: str) -> tuple[list, str, str, str]:
    """A recording found by who is on it: cues, name, link and passcode.

    The way round the share link: Zoom's web interface and its API hand out
    different tokens for the same recording, so a pasted link cannot be
    matched - but the topic carries the guest's name on both sides.
    """
    from .. import zoom
    from ..segments import SegmentError
    from ..youtube import parse_timed_captions

    if not (
        config.secrets.zoom_account_id
        and config.secrets.zoom_client_id
        and config.secrets.zoom_client_secret
    ):
        raise SegmentError(
            "Zoom isn't connected here, so I have nothing to search by name."
        )

    client = zoom.ZoomClient(
        config.secrets.zoom_account_id,
        config.secrets.zoom_client_id,
        config.secrets.zoom_client_secret,
    )
    try:
        meetings = client.account_recordings(days=SEGMENT_LOOKBACK_DAYS)
        hits = zoom.search_topics(meetings, words)

        if not hits:
            raise SegmentError(
                f"Nothing on the account in the last {SEGMENT_LOOKBACK_DAYS} days "
                f"is named “{words.strip()}”. I looked through "
                f"{len(meetings)} recording(s).",
                detail=_topic_list(meetings, limit=25),
            )

        # Several is not a failure - a recurring call carries the same name
        # every week - but picking for them would cut the wrong interview.
        if len(hits) > 1:
            raise SegmentError(
                f"{len(hits)} recordings match “{words.strip()}”. Add the date "
                "or more of the name and I'll take that one.",
                detail="\n".join(
                    f"· **{found.topic or '(no topic)'}** {(found.started_at or '')[:10]}"
                    for found in hits[:15]
                ),
            )

        found = hits[0]
        if not found.has_transcript:
            raise SegmentError(
                f"Zoom has no transcript for “{found.topic}”. Audio transcript has "
                "to be on *before* a call is recorded — it can't be made afterwards."
            )
        cues = parse_timed_captions(client.transcript_vtt(found))
        if not cues:
            raise SegmentError(f"Zoom's transcript for “{found.topic}” came back empty.")
        return cues, found.topic, found.share_url, found.passcode
    finally:
        client.close()


def _topic_list(meetings: list[dict], *, limit: int) -> str:
    """What is actually on the account, so a miss is a name you can correct."""
    from .. import zoom

    ordered = sorted(
        meetings or [], key=lambda m: str(m.get("start_time") or ""), reverse=True
    )
    lines = ["What's on the account, newest first:"]
    for meeting in ordered[:limit]:
        found = zoom.as_recording(meeting)
        lines.append(
            f"· **{found.topic or '(no topic)'}** {(found.started_at or '')[:10]}"
        )
    if len(ordered) > limit:
        lines.append(f"-# …and {len(ordered) - limit} more.")
    return "\n".join(lines)


def _zoom_cues(config: Config, rec) -> tuple[list, str, str, str]:
    from .. import zoom
    from ..segments import SegmentError
    from ..youtube import parse_timed_captions

    if not rec.transcribable(config):
        raise SegmentError(
            "Zoom isn't connected here — ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID and "
            "ZOOM_CLIENT_SECRET need to be set before I can read a recording."
        )

    client = zoom.ZoomClient(
        config.secrets.zoom_account_id,
        config.secrets.zoom_client_id,
        config.secrets.zoom_client_secret,
    )
    try:
        meetings = client.account_recordings(days=SEGMENT_LOOKBACK_DAYS)
        page_topic = client.share_page_topic(rec.url)
        found, _how = zoom.choose(
            meetings, link=rec.url, passcode=rec.passcode, page_topic=page_topic
        )
        if found is None:
            seen = f" The share page calls it “{page_topic}”." if page_topic else ""
            read = (
                f"I read the passcode as `{rec.passcode}`"
                if rec.passcode
                else "No passcode was posted with it"
            )
            raise SegmentError(
                "I can't tell which recording that link points at. Zoom's API "
                f"can't resolve share links, so I compared it against the "
                f"{len(meetings)} recording(s) on the account from the last "
                f"{SEGMENT_LOOKBACK_DAYS} days, by name and by passcode, and "
                f"none matched. {read}.{seen}\n\n"
                "If it isn't in the list below, it was recorded on a Zoom "
                "account this app can't read and was only shared with us — open "
                "the share page, download the transcript, and attach the .vtt "
                "here instead.",
                detail=zoom.describe_match(meetings, rec.url, limit=8),
            )
        if not found.has_transcript:
            raise SegmentError(
                f"Zoom has no transcript for “{found.topic}”. Audio transcript has "
                "to be on *before* a call is recorded — it can't be made afterwards."
            )
        cues = parse_timed_captions(client.transcript_vtt(found))
        if not cues:
            raise SegmentError(f"Zoom's transcript for “{found.topic}” came back empty.")
        # The pasted link rather than the API's: they are different tokens for
        # the same recording, and the pasted one is what the team already uses.
        return cues, found.topic, rec.url or found.share_url, rec.passcode or found.passcode
    finally:
        client.close()


def _fathom_cues(config: Config, rec) -> tuple[list, str, str, str]:
    from .. import fathom
    from ..segments import SegmentError
    from ..youtube import Cue

    if not rec.transcribable(config):
        raise SegmentError(
            "Fathom isn't connected here — FATHOM_API_KEY needs to be set before "
            "I can read a call."
        )

    client = fathom.FathomClient(config.secrets.fathom_api_key)
    try:
        found, seen = client.look_for(rec.url)
        if found is None:
            raise SegmentError(
                f"I couldn't find that call in Fathom, going back {len(seen)} calls. "
                "If it's older than that, or was recorded by somebody whose calls "
                f"aren't shared with this Fathom key, I can't see it. {fathom.describe(seen)}"
            )

        meeting = client.meeting_with_transcript(
            fathom.meeting_id(found.raw or {}),
            around=str((found.raw or {}).get("created_at") or ""),
        )
        turns = fathom.timed_turns(meeting or {})
        if not turns:
            raise SegmentError(
                f"Fathom gave me the transcript for “{found.title}” without any "
                "timings on it, and I won't guess where a clip starts. Use the Zoom "
                "recording of the same call, or attach the .vtt."
            )

        # A turn runs until the next one starts. The last has nothing after it,
        # so it gets the time its own words would take to say.
        cues = []
        for index, (start, text) in enumerate(turns):
            if index + 1 < len(turns):
                end = turns[index + 1][0]
            else:
                end = start + len(text.split()) / fathom.WORDS_A_SECOND
            cues.append(Cue(start=start, end=max(end, start + 1), text=text))
        # The guest's name first, then what Fathom called the recording. Fathom
        # titles are the subject, not the person - "OTP Trucker IUL Training" -
        # and the card is filed under whoever was interviewed. Fathom already
        # knows which invitee was external, which is exactly that person.
        named = [
            who for who in (found.guests or ())
            if who and "@" not in who
        ]
        title = f"{named[0]} — {found.title}".strip(" —") if named else found.title
        return cues, title, getattr(found, "url", "") or rec.url, ""
    finally:
        client.close()


def summarise_call(config: Config, rec) -> str:
    """A short write-up of the call, where the recording can actually be read.

    Only attempted for platforms whose transcript is reachable. A Zoom share
    link needs its passcode typed into a browser and Fathom needs a session, so
    those are filed with the link and nothing invented about what was said.
    """
    if not rec.transcribable(config):
        return ""

    text = None
    if rec.platform == "Zoom":
        text = zoom_transcript(config, rec)
    elif rec.platform == "Fathom":
        text = fathom_transcript(config, rec)
    if text is None:
        from .. import youtube

        video = youtube.video_from_link(rec.url)
        text = youtube.fetch_transcript(video.video_id).text
    # Fathom already wrote one. Using it costs nothing and reads the way the
    # team's own tool describes the call.
    if getattr(rec, "fathom_summary", ""):
        return rec.fathom_summary
    return summarise_text(config, text)


def summarise_text(config: Config, text: str) -> str:
    """The write-up itself, given a transcript that has already been fetched.

    Separate from `summarise_call` because a call picked by hand has its
    transcript in hand already, and fetching it twice to summarise it once
    would be silly.

    Every way this can come back empty is turned into something that says so.
    A card filed with no summary and no explanation has now happened three
    times, and each time the reason was somewhere else entirely.
    """
    from ..copywriter import CopywriterError

    if not (text or "").strip():
        raise CopywriterError("the transcript came back empty, so there was nothing to summarise")

    from anthropic import Anthropic

    config.secrets.require("anthropic_api_key")
    client = Anthropic(api_key=config.secrets.anthropic_api_key)
    try:
        response = client.messages.create(
            model=config.copy.model,
            # A 45-minute call summarised properly runs past 1200, and Claude
            # stops mid-sentence rather than shortening itself.
            max_tokens=4000,
            system=(
                "You summarise sales calls for an insurance lead company's internal "
                "library. Be specific and useful to a rep reading it later. Never "
                "invent anything that wasn't said."
            ),
            messages=[{
                "role": "user",
                "content": (
                    "Summarise this sales call. Give a two-sentence overview, then "
                    "sections for: what the prospect wanted, objections raised, how "
                    "they were handled, and what was agreed. Put each section title "
                    "on its own line in **bold**, and use '- ' for bullets.\n\n"
                    f"{text}"
                ),
            }],
        )
    except Exception as exc:  # anthropic's errors are not ours to enumerate
        raise CopywriterError(f"Claude couldn't summarise the call: {_short(exc)}") from exc

    written = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    ).strip()
    if not written:
        raise CopywriterError(
            "Claude returned nothing for that transcript "
            f"(stop reason: {getattr(response, 'stop_reason', 'unknown')})"
        )
    return written


def raw_post_fields(config: Config, *, limit: int = 40) -> list[dict]:
    """Every post exactly as GHL returns it, with the article bodies elided.

    Guessing at schedule field names has now cost three missed publish days.
    This is the ground truth instead: what GHL stores on a post that goes out
    on its day, sitting next to what it stores on one of ours that doesn't.
    Newest first, because the interesting ones are the recent ones.
    """
    context = open_ghl(config)
    try:
        posts = context.client.list_posts(context.blog_id)
    finally:
        context.close()

    from ..scheduler import parse_timestamp

    def when(post: dict):
        stamp = parse_timestamp(post.get("updatedAt") or post.get("createdAt") or "")
        return stamp.timestamp() if stamp else 0.0

    return [_compact(p) for p in sorted(posts, key=when, reverse=True)[:limit]]


def _compact(post: dict, keep: int = 120) -> dict:
    """The same object, minus the 8kb of article HTML that isn't the question."""
    return {
        key: (f"<{len(value)} chars>" if isinstance(value, str) and len(value) > keep else value)
        for key, value in post.items()
    }


def field_lines(posts: list[dict], config: Config) -> list[str]:
    """One line per post: what date GHL is holding, and its status."""
    from zoneinfo import ZoneInfo

    from ..scheduler import stored_schedule

    tz = ZoneInfo(config.schedule.timezone)
    lines = []
    for post in posts:
        held = stored_schedule(post)
        day = held.astimezone(tz).strftime("%b %d %I:%M%p") if held else "— NO DATE —"
        status = str(post.get("status") or "?")[:9]
        title = str(post.get("title") or post.get("urlSlug") or "?")[:34]
        lines.append(f"`{day:>15}` `{status:<9}` {title}")
    return lines


def check_ghl(config: Config) -> list[tuple[bool, str]]:
    """Verify everything the pipeline needs from GHL before a real run."""
    results: list[tuple[bool, str]] = []
    if not (config.secrets.ghl_api_token and config.secrets.ghl_location_id):
        return [(False, "No GHL token or location id set")]

    token = config.secrets.ghl_api_token
    if not token.startswith("pit-"):
        results.append((
            False,
            f"GHL_API_TOKEN starts {token[:4]!r} — a Private Integration token starts "
            "'pit-'. Agency API keys and OAuth client secrets are rejected by the v2 API.",
        ))

    client = ghl.GHLClient(token, config.secrets.ghl_location_id)
    try:
        try:
            sites = client.list_blog_sites()
            results.append((True, f"Connected — {len(sites)} blog site(s) visible"))
        except Exception as exc:
            results.append((False, f"Cannot reach GHL: {_short(exc)}"))
            if "401" in str(exc):
                results.append((
                    False,
                    "401 means the token itself was rejected. Check it was created in "
                    "the Agent Lead Lab sub-account (Settings -> Private Integrations) "
                    "and not at agency level, that it matches GHL_LOCATION_ID, and that "
                    "it was copied whole — they are long and easy to clip.",
                ))
            elif "403" in str(exc):
                # GHL returns 403 for two unrelated problems. Its message says
                # which, so read it rather than guessing at scopes.
                if "location" in str(exc).lower():
                    results.append((
                        False,
                        f"The token belongs to a different sub-account than "
                        f"GHL_LOCATION_ID ({config.secrets.ghl_location_id}). In GHL, open "
                        "the sub-account you made the Private Integration in and read the "
                        "id out of the URL: /v2/location/<THIS>/. Either set "
                        "GHL_LOCATION_ID to that (and GHL_BLOG_ID to a blog site inside "
                        "it), or recreate the integration inside the sub-account you want.",
                    ))
                else:
                    results.append((
                        False,
                        "403 with a valid token usually means a missing scope. It needs "
                        "blogs/post.write, blogs/post-update.write, blogs/check-slug.readonly, "
                        "blogs/category.readonly, blogs/author.readonly, blogs/posts.readonly, "
                        "blogs/list.readonly, medias.write, medias.readonly.",
                    ))
            return results

        try:
            blog_id, note = ghl.resolve_blog_id(sites, config.secrets.ghl_blog_id)
            results.append((True, note or "GHL_BLOG_ID matches a real blog site"))
        except ghl.GHLError as exc:
            results.append((False, str(exc)))
            blog_id = None

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
                results.extend(_undated_posts(posts, config))
            except Exception as exc:
                results.append((False, f"Cannot list existing posts: {exc}"))
    finally:
        client.close()
    return results


def check_recordings(config: Config) -> list[tuple[bool, str]]:
    """Actually call Notion, Zoom and Fathom, rather than checking a key is present.

    A key that is set but wrong looks identical to a working one right up until
    a real recording is posted, which is the worst moment to find out. Each of
    these makes one small request and reports what came back.
    """
    results: list[tuple[bool, str]] = []
    results.extend(_check_notion(config))
    results.extend(_check_zoom(config))
    results.extend(_check_fathom(config))
    results.extend(_check_google_scopes(config))
    results.extend(_check_gmail(config))
    results.extend(_check_docs(config))
    results.extend(_check_tracker(config))
    results.extend(_check_hub(config))
    return results


def _check_hub(config: Config) -> list[tuple[bool, str]]:
    """Whether Nova's read-only hub API answers RYTE's key, and with what."""
    from collections import Counter

    from .. import hub

    if not (getattr(config.secrets, "hub_api_token", "") or "").strip():
        return [(None, "Distro Hub not set up - no HUB_API_TOKEN, so fulfilled orders aren't checked")]
    try:
        found = hub.agents(config.secrets, status="all")
        ended = [one for one in hub.agents(config.secrets, status="ended") if one.fulfilled]
    except Exception as exc:
        return [(False, f"Distro Hub - {_short(exc, 200)}")]
    by = Counter((one.status or "?").casefold() for one in found)
    # Both numbers to hold against the hub's own panel: ALL, and FULFILLED.
    return [(True, f"Distro Hub - {len(found)} agent(s): "
             + ", ".join(f"{many} {status}" for status, many in by.most_common())
             + f" · Fulfilled tab: {len(ended)}")]


def _check_google_scopes(config: Config) -> list[tuple[bool, str]]:
    """What the Google token in .env is actually allowed to do.

    Every refresh token starts "1//" and carries nothing readable, so a new
    one pasted and a new one forgotten look identical from the outside - and
    both look like the scope never having been ticked. Google names the
    scopes on every refresh; this asks it and says them out loud.
    """
    from .. import docs as doc, gsheets

    try:
        creds = gsheets.credentials(config.secrets)
    except gsheets.SheetsError as exc:
        return [(False, f"Google token - {_short(exc, 160)}")]
    instead = (getattr(config.secrets, "gmail_refresh_token", "") or "").strip()
    if instead:
        creds = gsheets.Credentials(
            (getattr(config.secrets, "gmail_client_id", "") or "").strip()
            or creds.client_id,
            (getattr(config.secrets, "gmail_client_secret", "") or "").strip()
            or creds.client_secret,
            instead,
        )
    try:
        scopes = gsheets.granted(creds, which="GMAIL_REFRESH_TOKEN" if instead else "GOOGLE_REFRESH_TOKEN")
    except Exception as exc:
        return [(False, f"Google token - {_short(exc, 160)}")]

    # Every scope something signs in with this token for. A token re-minted
    # with two of the three ticks works for those two and fails the third
    # weeks later, in the middle of whatever needed it.
    needs = {
        doc.SCOPE.rsplit("/", 1)[-1]: "the segments can't be written into the Doc",
        "drive.file": "clear-out pictures can't go into Drive",
        "gmail.readonly": "Payra receipts can't be read for rebuttals",
    }
    missing = [scope for scope in needs if scope not in scopes]
    return [(
        not missing,
        f"Google token grants: {', '.join(scopes) or 'nothing'}"
        + "".join(f" - no **{scope}**, so {needs[scope]}" for scope in missing)
        + (" - mint it again with all three ticked" if missing else ""),
    )]


def _check_gmail(config: Config) -> list[tuple[bool, str]]:
    """Which mailbox the token actually reads, which is not a thing to assume.

    A refresh token belongs to one account. Minted against the wrong one it
    looks exactly like an empty inbox - every search finds nothing and none of
    it is an error - so the receipt and the signed contract quietly stop
    appearing in rebuttals and nobody hears about it until a chargeback.
    """
    from .. import gmail as inbox

    if not (getattr(config.secrets, "gmail_invoice_sender", "") or "").strip():
        return [(None, "Gmail not configured - no invoice or contract in rebuttals")]
    try:
        with inbox.open_gmail(config.secrets) as reading:
            who, many = reading.whoami()
    except inbox.GmailError as exc:
        return [(False, f"Gmail - {_short(exc, 480)}" + _token_hint(config))]
    except Exception as exc:
        return [(False, f"Gmail - {_short(exc, 480)}" + _token_hint(config))]
    return [(True, f"Gmail - reading **{who}** ({many:,} messages)")]


def _token_hint(config: Config, *, env: Path | None = None) -> str:
    """What RYTE is actually holding as GMAIL_REFRESH_TOKEN, without saying it.

    "Rejected" after a new token was pasted is one of four things, and from
    the outside they look the same: the old one still running, the line twice
    in .env with the old one winning, the access token copied instead of the
    refresh token, or a bit of the token lost on the way. The last few
    characters - compared with the end of the one in the Playground - and the
    file itself tell them apart. Never the token.
    """
    from ..config import REPO_ROOT

    held = (getattr(config.secrets, "gmail_refresh_token", "") or "").strip()
    if not held:
        return ""
    said = [f"RYTE is using the one ending **…{held[-4:]}** ({len(held)} characters)"]
    if held.startswith("ya29."):
        said.append("that's an *access* token - the refresh token is the one starting 1//")
    elif not held.startswith("1//"):
        said.append("a refresh token starts 1// - this doesn't")
    if any(ch in held for ch in "\"' "):
        said.append("it has quotes or spaces in it")
    try:
        lines = (env or REPO_ROOT / ".env").read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    times = sum(1 for one in lines if one.strip().startswith("GMAIL_REFRESH_TOKEN="))
    if times > 1:
        said.append(f"GMAIL_REFRESH_TOKEN is in .env {times} times - the last one wins, delete the others")
    return "\n-# " + "; ".join(said)


def _check_tracker(config: Config) -> list[tuple[bool, str]]:
    """Whether the chargeback tracker can be read, and what its columns are.

    Reads the heading row and says it back. The headings are how the row is
    laid out, so seeing them is seeing what a chargeback will actually fill -
    and the alternative is finding out in the middle of one.

    Reads only. Nothing is appended by a check.
    """
    from .. import rebuttal as rules_doc

    if not (getattr(config.secrets, "tracker_sheet_id", "") or "").strip():
        return [(None, "Chargeback tracker not configured - rows stay unwritten")]
    headings, tab, problems, _made = tracker_headings(config)
    if problems:
        return [(False, f"Chargeback tracker - {_short('; '.join(problems), 300)}")]
    if not headings:
        return [(False, "Chargeback tracker - no columns in its first row")]

    # Which columns RYTE recognises, not which happen to carry a value for
    # some made-up dispute: a stub with no transaction date left "Date of
    # Transaction" empty and the check reported it as a column RYTE could not
    # fill, which sent somebody looking for a bug that was not there.
    knows, leaves = rules_doc.columns_known(headings)
    return [(
        True,
        f"Chargeback tracker - **{tab}**, {len(headings)} column(s). "
        f"RYTE fills {len(knows)}: " + ", ".join(knows)
        + (f" · left for you: {', '.join(leaves)}" if leaves else ""),
    )]


def _check_docs(config: Config) -> list[tuple[bool, str]]:
    """Whether the posting doc can actually be reached, and as whom.

    One read of the tab titles - the smallest thing the Docs API will answer -
    because a scope that was not ticked and a doc that was never shared look
    identical until an interview is filed, which is the worst moment to find
    out. Nothing is written.
    """
    from .. import docs as doc

    if not (getattr(config.secrets, "segments_doc_id", "") or "").strip():
        return [(None, "Posting doc not configured — segment copy stays on the card")]
    try:
        with doc.open_docs(config.secrets) as reading:
            tabs = reading.tabs()
    except doc.DocsError as exc:
        return [(False, f"Posting doc — {_short(exc, 480)}")]
    except Exception as exc:
        return [(False, f"Posting doc — {_short(exc, 480)}")]
    return [(
        True,
        f"Posting doc — {len(tabs)} tab(s), latest "
        + (f"“{tabs[-1].title}”" if tabs else "none yet"),
    )]


def _check_notion(config: Config) -> list[tuple[bool, str]]:
    from .. import notion

    if not config.secrets.notion_token:
        return [(None, "Notion not configured — recordings won't be filed")]
    if not config.secrets.notion_recordings_page_id:
        return [(False, "NOTION_TOKEN is set but NOTION_RECORDINGS_PAGE_ID isn't")]

    client = notion.NotionClient(config.secrets.notion_token)
    try:
        database_id = client.find_child_database(config.secrets.notion_recordings_page_id)
        if not database_id:
            return [(
                False,
                "Notion page reachable, but there's no database on it. The gallery "
                "needs to be a database view, not an empty page.",
            )]
        schema = (client.database(database_id).get("properties") or {})
        columns = ", ".join(sorted(schema)) or "(none)"
        return [(True, f"Notion gallery found — columns: {columns}")]
    except Exception as exc:
        return [(False, f"Notion: {_short(exc)}")]
    finally:
        client.close()


def _check_zoom(config: Config) -> list[tuple[bool, str]]:
    from .. import zoom

    secrets = config.secrets
    if not (secrets.zoom_account_id and secrets.zoom_client_id and secrets.zoom_client_secret):
        return [(None, "Zoom not configured — Zoom calls filed without a summary")]

    client = zoom.ZoomClient(
        secrets.zoom_account_id, secrets.zoom_client_id, secrets.zoom_client_secret
    )
    try:
        meetings = client.account_recordings(days=30)
    except Exception as exc:
        return [(False, f"Zoom: {_short(exc)}")]
    finally:
        client.close()

    with_text = [m for m in meetings if zoom.pick_transcript(m.get("recording_files") or [])]
    rows = [(True, f"Zoom connected — {len(meetings)} recording(s) in the last 30 days")]
    if meetings and not with_text:
        rows.append((
            False,
            "None of them has a transcript. Turn on Settings -> Recording -> "
            "Advanced cloud recording -> Create audio transcript. It only applies "
            "to calls recorded after it is switched on.",
        ))
    elif with_text:
        rows.append((True, f"{len(with_text)} of them have transcripts RYTE can read"))
    return rows


# ------------------------------------------------ picking a call by name

# Discord gives an autocomplete three seconds to answer, and asking Zoom for
# ninety recordings takes longer than that - so the list is kept warm and the
# typing filters what is already here.
_CALLS: dict = {"at": None, "items": []}
CALLS_TTL_SECONDS = 600


class Call:
    """One recording, in the shape the picker and the reader both need."""

    def __init__(self, platform: str, uid: str, topic: str, when: str, who: str, raw=None):
        self.platform = platform
        self.uid = uid
        self.topic = topic
        self.when = when
        self.who = who
        self.raw = raw

    @property
    def key(self) -> str:
        # Discord caps an option's value at 100 characters.
        return f"{self.platform}|{self.uid}"[:100]

    @property
    def label(self) -> str:
        return f"{self.topic or '(no topic)'} · {self.when[:10]}"[:100]

    def matches(self, typed: str) -> bool:
        needle = (typed or "").strip().casefold()
        if not needle:
            return True
        return all(
            word in f"{self.topic} {self.when} {self.who} {self.platform}".casefold()
            for word in needle.split()
        )


def call_choices(config: Config, *, force: bool = False) -> list[Call]:
    """Every recording RYTE can read, newest first, cached for a few minutes."""
    from datetime import datetime as _dt

    now = _dt.utcnow()
    if not force and _CALLS["at"] and (now - _CALLS["at"]).total_seconds() < CALLS_TTL_SECONDS:
        return _CALLS["items"]

    found: list[Call] = []
    secrets = config.secrets

    if secrets.zoom_account_id and secrets.zoom_client_id and secrets.zoom_client_secret:
        from .. import zoom

        client = zoom.ZoomClient(
            secrets.zoom_account_id, secrets.zoom_client_id, secrets.zoom_client_secret
        )
        try:
            for meeting in client.account_recordings(days=30):
                item = zoom.as_recording(meeting)
                found.append(
                    Call("zoom", item.uid, item.topic, item.started_at, item.host_email, meeting)
                )
        except Exception:
            # A warm-up failure must never break the command that uses it.
            pass
        finally:
            client.close()

    if secrets.fathom_api_key:
        from .. import fathom

        client = fathom.FathomClient(secrets.fathom_api_key)
        try:
            for meeting in client.meetings():
                item = fathom.as_call(meeting)
                found.append(
                    Call(
                        "fathom", fathom.meeting_id(meeting), item.title,
                        item.started_at, item.recorded_by, meeting,
                    )
                )
        except Exception:
            pass
        finally:
            client.close()

    found.sort(key=lambda item: item.when or "", reverse=True)
    _CALLS["at"], _CALLS["items"] = now, found
    return found


def picker_choices(config: Config, *, limit: int = 25) -> list[Call]:
    """The calls worth offering, newest first.

    Discord allows twenty-five options and a busy day fills them, so what gets
    left out matters as much as what goes in. A call from two days ago fell off
    the end behind standups and calls that were already filed.

    So: nothing already in the gallery, nothing recurring and internal, and
    nothing without a transcript - a card made from one of those has no summary
    anyway. That is thirty-odd rows of noise cleared out of twenty-five slots.
    """
    from .. import recordings

    filed = recordings.filed_ids()
    return [
        call for call in call_choices(config)
        if call.uid not in filed and not is_internal(call.topic) and _has_text(call)
    ][:limit]


def find_choice(config: Config, key: str) -> Call | None:
    for item in call_choices(config):
        if item.key == key:
            return item
    return None


def search_calls(config: Config, typed: str, *, limit: int = 8) -> list[Call]:
    """Recordings whose name contains what somebody typed, newest first.

    The search is the same one a picker would do; it just happens in the
    message rather than in a menu. Typing "derrick" is faster than scrolling
    ninety recordings, and it is the thing people reach for anyway.
    """
    if not (typed or "").strip():
        return []
    return [item for item in call_choices(config) if item.matches(typed)][:limit]


def read_chosen(config: Config, rec, call: Call) -> str:
    """Read the call somebody picked by name, and take the card's details from it."""
    rec.matched_by = "you picking it"
    if call.platform == "fathom":
        from .. import fathom

        return fathom_read(config, rec, fathom.as_call(call.raw or {}))

    from .. import zoom

    return zoom_read(config, rec, zoom.as_recording(call.raw or {}))


def fathom_fields(config: Config, link: str) -> list[str]:
    """Every field Fathom returns for one call, so we can see what is in there.

    The question this answers is whether the recording itself can be fetched -
    "fathom is inaccesible unless our account is used". If their API hands back
    a link to the media, RYTE can put it in Drive and share that instead. If it
    only hands back a link to their player, it cannot, and no amount of code
    will change that.

    Keys, and values only when they look like a link. A call's record carries
    guests' names and email addresses, and none of that is any part of this
    question - so everything else is shown as what kind of thing it is rather
    than as itself.
    """
    from .. import fathom

    key = (config.secrets.fathom_api_key or "").strip()
    if not key:
        return ["No FATHOM_API_KEY in .env, so there is nothing to ask."]

    with fathom.FathomClient(key) as asking:
        call, meetings = asking.find(link)
        if call is None:
            return [
                f"Fathom doesn't show a call at that link. `@RYTE calls` lists "
                f"the {len(meetings)} it will show me."
            ]
        # The record as it came back, which `find` already kept.
        whole = call.raw or {}

    found = [f"**{call.title}** — everything Fathom returns for it:"]
    found += _fields_in(whole or {})
    found.append(
        "-# A link ending .mp4 or .m4a, or a field called anything like "
        "`download`, is what RYTE would need to put it in Drive."
    )
    return found


def _fields_in(data, path: str = "", depth: int = 0) -> list[str]:
    """Key paths and link-shaped values. Everything else by its kind only."""
    if depth > 3:
        return [f"• {path} — …"]
    found = []
    if isinstance(data, dict):
        for name, value in list(data.items())[:40]:
            found += _fields_in(value, f"{path}.{name}" if path else str(name), depth + 1)
    elif isinstance(data, list):
        found.append(f"• {path} — {len(data)} item(s)")
        if data:
            found += _fields_in(data[0], f"{path}[0]", depth + 1)
    else:
        said = str(data or "")
        if said.startswith("http"):
            found.append(f"• {path} — {said}")
        else:
            found.append(f"• {path} — _{type(data).__name__}_")
    return found


def diagnose_link(config: Config, link: str) -> list[str]:
    """Whether a pasted link matches a recording, and the tokens compared if not.

    The list of visible calls answers "can RYTE see it". This answers the next
    question, which turned out to be the real one: it can see the call and
    still not recognise the link as pointing at it.

    Asked of whoever the link belongs to. It used to ask Zoom whatever was
    pasted, so a fathom.video link came back "No match among 143 recording(s)"
    — which is true of Zoom and says nothing at all about the link. This
    function exists so that "I couldn't find it" has one meaning rather than
    two, and answering about the wrong service gave it a third: I didn't look.
    """
    said = (link or "").casefold()
    if "fathom" in said:
        return _fathom_link(config, link)
    if "zoom" in said:
        return _zoom_link(config, link)
    # Neither name in it, so ask both rather than guess.
    return _zoom_link(config, link) + _fathom_link(config, link)


def _fathom_link(config: Config, link: str) -> list[str]:
    """Whether Fathom shows a call at this link."""
    from .. import fathom

    key = (config.secrets.fathom_api_key or "").strip()
    if not key:
        return ["Fathom isn't configured, so there's nothing to match against."]
    try:
        with fathom.FathomClient(key) as asking:
            found, meetings = asking.find(link)
    except Exception as exc:
        return [f"Couldn't ask Fathom: {_short(exc)}"]

    if found is not None:
        return [
            f"✅ Fathom shows that link as **{found.title or '(no title)'}**"
            + (f" ({found.started_at[:10]})" if found.started_at else "")
            + "."
        ]
    return [
        f"❌ Fathom shows no call at that link, among {len(meetings)} it will "
        "show me. `@RYTE calls` lists them.",
    ]


def _zoom_link(config: Config, link: str) -> list[str]:
    """Whether Zoom shows a recording at this link."""
    from .. import zoom

    secrets = config.secrets
    if not (secrets.zoom_account_id and secrets.zoom_client_id and secrets.zoom_client_secret):
        return ["Zoom isn't configured, so there's nothing to match against."]

    client = zoom.ZoomClient(
        secrets.zoom_account_id, secrets.zoom_client_id, secrets.zoom_client_secret
    )
    try:
        meetings = client.account_recordings(days=30)
    except Exception as exc:
        return [f"Couldn't ask Zoom: {_short(exc)}"]
    finally:
        client.close()

    found = zoom.match_share_url(meetings, link)
    if found is not None:
        mark = "has a transcript" if found.has_transcript else "has **no** transcript"
        return [
            f"✅ That link is **{found.topic or '(no topic)'}** "
            f"({(found.started_at or '')[:10]}, {found.host_email}) — it {mark}.",
        ]
    return [
        f"❌ No match among {len(meetings)} recording(s).",
        zoom.describe_match(meetings, link),
    ]


def visible_calls(config: Config, *, limit: int = 15) -> list[str]:
    """Every call RYTE can actually reach, newest first.

    "I couldn't find that call" has two very different causes that look
    identical from Discord: the app is misconfigured, or the recording lives on
    someone else's Zoom account and was only *shared* with us. A list settles
    it - an empty one is a configuration problem, a list that simply doesn't
    include the call you posted is an ownership one.
    """
    from .. import fathom, zoom

    lines: list[str] = []
    secrets = config.secrets

    if secrets.zoom_account_id and secrets.zoom_client_id and secrets.zoom_client_secret:
        client = zoom.ZoomClient(
            secrets.zoom_account_id, secrets.zoom_client_id, secrets.zoom_client_secret
        )
        try:
            meetings = client.account_recordings(days=30)
        except Exception as exc:
            meetings = []
            lines.append(f"**Zoom** — couldn't ask: {_short(exc)}")
        finally:
            client.close()
        if meetings:
            lines.append(f"**Zoom** — {len(meetings)} recording(s) on this account:")
            for meeting in meetings[:limit]:
                found = zoom.as_recording(meeting)
                mark = "📝" if found.has_transcript else "—"
                day = (found.started_at or "")[:10]
                lines.append(f"{mark} {day}  {found.topic or '(no topic)'}  ·  {found.host_email}")
            if len(meetings) > limit:
                lines.append(f"…and {len(meetings) - limit} more")
        elif not any(line.startswith("**Zoom** — couldn't") for line in lines):
            lines.append(
                "**Zoom** — no recordings at all on this account in the last 30 days."
            )

    if secrets.fathom_api_key:
        client = fathom.FathomClient(secrets.fathom_api_key)
        try:
            meetings = client.meetings(include_transcript=False, limit=limit)
        except Exception as exc:
            meetings = []
            lines.append(f"**Fathom** — couldn't ask: {_short(exc)}")
        finally:
            client.close()
        if meetings:
            lines.append(f"**Fathom** — {len(meetings)} call(s):")
            for meeting in meetings[:limit]:
                call = fathom.as_call(meeting)
                lines.append(f"📝 {(call.started_at or '')[:10]}  {call.title or '(untitled)'}")

    if not lines:
        return ["Neither Zoom nor Fathom is configured, so there's nothing to look in."]
    return lines


def _check_fathom(config: Config) -> list[tuple[bool, str]]:
    from .. import fathom

    if not config.secrets.fathom_api_key:
        return [(None, "Fathom not configured — Fathom calls filed without a summary")]

    client = fathom.FathomClient(config.secrets.fathom_api_key)
    try:
        meetings = client.meetings(limit=25)
    except Exception as exc:
        return [(False, f"Fathom: {_short(exc)}")]
    finally:
        client.close()

    if not meetings:
        return [(False, "Fathom connected, but it returned no calls at all.")]
    # The field names matter more than the count: this integration was written
    # without a key to try it against, so the first real response is evidence.
    return [(True, f"Fathom connected — {fathom.describe(meetings)}")]


def check_anthropic(config: Config) -> list[tuple[bool, str]]:
    """Actually call the API, rather than just confirming a key is present.

    A present-but-invalid key looks fine at boot and then fails at the moment
    it matters most - after the transcript is in and the run has started.
    """
    key = config.secrets.anthropic_api_key
    if not key:
        return [(False, "ANTHROPIC_API_KEY is not set")]

    results: list[tuple[bool, str]] = []
    if not key.startswith("sk-ant-"):
        results.append((
            False,
            f"ANTHROPIC_API_KEY starts {key[:7]!r} — a Claude API key starts 'sk-ant-'.",
        ))

    from anthropic import Anthropic

    try:
        client = Anthropic(api_key=key)
        client.messages.create(
            model=config.copy.model,
            max_tokens=1,
            messages=[{"role": "user", "content": "hi"}],
        )
        results.append((True, f"Claude API works ({config.copy.model})"))
    except Exception as exc:
        text = str(exc)
        if "authentication_error" in text or "401" in text:
            results.append((
                False,
                "Claude rejected the key. Make a fresh one at console.anthropic.com "
                "-> Settings -> API Keys, and paste the whole thing — they are long "
                "and easy to clip.",
            ))
        elif "credit balance" in text or "billing" in text.lower():
            results.append((
                False,
                "The key is valid but the account has no credit. Add some at "
                "console.anthropic.com -> Billing.",
            ))
        elif "not_found_error" in text or "model" in text.lower():
            results.append((
                False,
                f"Model {config.copy.model!r} is not available to this key. "
                "Change [copy] model in config/wilbyte.toml.",
            ))
        else:
            results.append((False, f"Claude API call failed: {_short(exc)}"))
    return results


def check_youtube(source: str | None) -> list[tuple[bool, str]]:
    """Verify YouTube is reachable, and that transcripts actually come back.

    This is the step most likely to fail in a datacenter: YouTube blocks many
    cloud IP ranges for transcript requests even when video metadata loads.
    """
    from .. import youtube_api

    results: list[tuple[bool, str]] = []
    if youtube.cookie_file():
        count, signed_in = youtube.cookie_summary()
        if signed_in:
            results.append((True, f"YouTube cookies loaded — {count} cookies, signed in"))
        else:
            results.append((
                False,
                f"YouTube cookies loaded ({count} cookies) but none of them is a login "
                f"cookie, so requests still go out anonymous. Re-export from a "
                f"youtube.com tab where you are signed in, and paste the whole file.",
            ))

    missing = youtube_api.missing_oauth_vars()
    if not missing:
        results.append((True, "Data API configured with OAuth — captions available"))
    elif len(missing) < len(youtube_api.OAUTH_VARS):
        # Half-finished setup: name the gap instead of repeating "not configured".
        results.append((False, "OAuth is half set up — still missing " + ", ".join(missing)))
    elif youtube_api.api_key():
        results.append((
            True,
            "Data API key set (metadata only). Add "
            + ", ".join(youtube_api.OAUTH_VARS)
            + " to read captions too.",
        ))
    else:
        results.append((
            None,
            "No Data API credentials — falling back to scraping, which cloud "
            "hosts get blocked from.",
        ))

    if not source:
        results.append((None, "No link given — add one to test a real fetch"))
        return results
    try:
        if youtube.looks_like_playlist(source):
            videos = youtube.list_playlist_videos(source, limit=3)
            results.append((True, f"Playlist readable — {len(videos)}+ video(s)"))
        else:
            videos = [youtube.fetch_video(source)]
            results.append((True, f"Video readable — {videos[0].title[:60]}"))
    except Exception as exc:
        results.append((False, f"Cannot read from YouTube: {_short(exc)}"))
        if "not a bot" in str(exc) or "cookies" in str(exc).lower():
            results.append((
                False,
                "YouTube is refusing this server outright, not just for transcripts. "
                "Attach the transcript as a .txt with the link and RYTE will skip "
                "YouTube entirely — everything else in the pipeline still works.",
            ))
        return results

    if not missing:
        # Ask the caption endpoint directly. It separates the two failures that
        # otherwise look identical: this account doesn't own the video, versus
        # the video has no caption track at all.
        try:
            tracks = youtube_api.list_captions(videos[0].video_id)
            if tracks:
                kinds = ", ".join(
                    sorted({
                        "auto-generated" if (t.get("snippet") or {}).get("trackKind") == "ASR"
                        else "human-written"
                        for t in tracks
                    })
                )
                results.append((True, f"{len(tracks)} caption track(s) visible — {kinds}"))
            else:
                results.append((
                    False,
                    "No caption tracks on this video. Nothing to transcribe — check "
                    "the video has captions in YouTube Studio.",
                ))
        except Exception as exc:
            results.append((False, f"Caption list refused: {_short(exc)}{_owner_hint(exc)}"))

    try:
        transcript = youtube.fetch_transcript(videos[0].video_id)
        route = {
            "youtube-api": "official API, human-written captions",
            "youtube-api-asr": "official API, auto-generated captions",
            "youtube-ytdlp": "yt-dlp, human-written captions",
            "youtube-ytdlp-asr": "yt-dlp, auto-generated captions",
        }.get(transcript.source, transcript.source)
        results.append((True, f"Transcript works — {transcript.word_count} words ({route})"))
    except Exception as exc:
        results.append((False, f"No transcript: {_short(exc)}"))
    return results


def _undated_posts(posts: list[dict], config: Config) -> list[tuple[bool, str]]:
    """Flag posts whose day can't be read, and say what GHL did send.

    A post GHL reports without any date is a day the scheduler will hand out
    again. Rather than guess at field names a third time, show the keys the
    API actually returned for one of them.
    """
    from zoneinfo import ZoneInfo

    from ..scheduler import holds_a_day, post_day

    tz = ZoneInfo(config.schedule.timezone)
    # Drafts, and anything archived or deleted, are not on the calendar. They
    # have no date because they are not going out, which is not a problem to
    # report - it is what a draft is.
    on_the_calendar = [p for p in posts if holds_a_day(p)]
    undated = [p for p in on_the_calendar if post_day(p, tz) is None]
    if not undated:
        return []

    keys = ", ".join(sorted(undated[0].keys())) or "(nothing at all)"
    return [(
        False,
        f"{len(undated)} post(s) came back with no readable date — those days can "
        f"be double-booked. GHL sent these fields: {keys}",
    )]


def _owner_hint(exc: Exception) -> str:
    """The ownership explanation, but only when the refusal is about permission.

    A rejected refresh token is a credentials mismatch, not an ownership one,
    and pinning it on the wrong Google account costs an hour of re-consenting.
    """
    text = str(exc)
    if "403" in text or "permission" in text.lower() or "forbidden" in text.lower():
        return (
            " Captions are owner-only, so this usually means consent was granted "
            "by an account that doesn't own the channel."
        )
    return ""


def _short(exc: Exception, limit: int = 400) -> str:
    text = " ".join(str(exc).split())
    return text if len(text) <= limit else text[:limit] + "…"


def record(ledger: Ledger, post: BlogPost) -> None:
    ledger.record(
        video_id=post.video.video_id,
        title=post.title,
        url_slug=post.url_slug,
        scheduled_at=post.scheduled_at,
        ghl_post_id=post.ghl_post_id,
        payload_path=post.ghl_payload_path,
    )
    ledger.save()


def find_cards(config: Config, name: str, *, limit: int = 5) -> list[tuple[str, str]]:
    """(title, url) for gallery cards matching a name somebody asked for.

    Filing a recording is only half of it - the gallery exists to be asked. A
    link somebody has to go and dig out by hand is most of the way back to not
    having filed it at all.
    """
    from .. import notion, recordings

    config.secrets.require("notion_token", "notion_recordings_page_id")
    client = notion.NotionClient(config.secrets.notion_token)
    try:
        database_id = client.find_child_database(config.secrets.notion_recordings_page_id)
        if not database_id:
            return []
        rows = client.query_database(database_id)
    finally:
        client.close()

    found = recordings.matching_rows(rows, name)
    # Newest last out of Notion, and the recent one is nearly always the one
    # being asked about.
    return list(reversed(found))[:limit]


# ---------------------------------------------- filing calls without being asked

# Posting a link, then being asked which call it was, is two steps more than
# nobody-does-anything. Zoom and Fathom both know what was recorded and when,
# so RYTE reads them directly and files what it finds. The link stops being an
# identifier and goes back to being what it always was: a link on a card.

# Recurring internal calls. These are recorded every day, have transcripts, and
# are never sales calls - filing them would bury the gallery in standups.
SKIP_TOPICS = (
    "daily team call", "eod team call", "team meeting", "standup", "stand-up",
    "all hands", "weekly sync", "1:1", "one on one", "interview",
)


def is_internal(topic: str, *, skip: tuple[str, ...] = SKIP_TOPICS) -> bool:
    lowered = " ".join((topic or "").split()).casefold()
    return any(phrase in lowered for phrase in skip)


def new_recordings(config: Config, *, within_days: int = 3, limit: int = 10) -> list[Call]:
    """Calls worth filing that aren't in the gallery yet, oldest first.

    Only recent ones: the account holds ninety recordings and the first run
    should file the last few days, not four weeks of history nobody asked for.
    Oldest first so the gallery reads in the order the calls happened.
    """
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    from .. import recordings

    cutoff = (_dt.utcnow() - _td(days=within_days)).date().isoformat()
    filed = recordings.filed_ids()
    found = [
        call for call in call_choices(config, force=True)
        if call.uid not in filed
        and (call.when or "")[:10] >= cutoff
        and not is_internal(call.topic)
        and _has_text(call)
    ]
    found.sort(key=lambda call: call.when or "")
    return found[:limit]


def _has_text(call: Call) -> bool:
    """Whether there is anything to summarise. Fathom always writes one."""
    if call.platform == "fathom":
        return True
    from .. import zoom

    return bool(zoom.pick_transcript((call.raw or {}).get("recording_files") or []))


def file_call(config: Config, call: Call) -> tuple[str, str, str]:
    """Read one call and put it in the gallery. Returns (title, card, note)."""
    from .. import recordings

    link = ""
    if call.platform == "zoom":
        link = str((call.raw or {}).get("share_url") or "")
    else:
        from .. import fathom

        link = fathom.as_call(call.raw or {}).url

    rec = recordings.Recording(
        url=link or "",
        platform="Zoom" if call.platform == "zoom" else "Fathom",
        posted_on=_day_of(call.when),
    )
    try:
        text = read_chosen(config, rec, call)
        summary = rec.fathom_summary or summarise_text(config, text)
    except Exception as exc:
        summary = ""
        rec.note = f"No summary — {_short(exc)}"

    title, url = file_recording(config, rec, summary=summary)
    return title, url, rec.note


def _day_of(when: str):
    from datetime import datetime as _dt

    try:
        return _dt.fromisoformat((when or "").replace("Z", "+00:00")).date()
    except ValueError:
        return None


# ------------------------------------------------------ the daily board

def board_day(config: Config) -> date:
    """What day it is *on the board's clock*, not on the machine's.

    `date.today()` reads the timezone of whatever computer RYTE happens to be
    running on. The board belongs to a team working Eastern, and a Mac set to
    Manila time is already tomorrow by mid-afternoon - so the 26th's cards read
    as today's, the 25th's as yesterday's, and the rollover reports that
    tomorrow has no cards at all while four of them sit in In Que.
    """
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(config.schedule.timezone)).date()


def open_trello(config: Config):
    """A Trello session, or a clear refusal about what is missing."""
    from ..config import ConfigError
    from ..trello import TrelloClient

    secrets = config.secrets
    missing = [
        name for name, value in (
            ("TRELLO_KEY", secrets.trello_key),
            ("TRELLO_TOKEN", secrets.trello_token),
            ("TRELLO_BOARD_ID", secrets.trello_board_id),
        ) if not value
    ]
    if missing:
        raise ConfigError(
            f"The board needs {', '.join(missing)} in .env. The key and token come "
            "from trello.com/power-ups/admin; the board id is the short code in the "
            "board's own URL."
        )
    return TrelloClient(secrets.trello_key, secrets.trello_token)


def board_today(config: Config, *, day=None) -> list[str]:
    """What the board looks like right now: which lists hold which day's cards."""
    from .. import dailyops, trello

    day = day or board_day(config)
    client = open_trello(config)
    try:
        lists = client.board_lists(config.secrets.trello_board_id)
        lines = [f"Going by **{day:%a %b %d, %Y}** on the board's clock."]
        for board_list in lists:
            cards = client.list_cards(str(board_list.get("id") or ""))
            dated = dailyops.daily_cards(cards)
            named = [
                # With the year. Without it a card dated 08/26/25 reads as
                # "08/26" and looks like tomorrow's, right up until the
                # rollover says there is no card for tomorrow.
                f"{dailyops.CARD_KINDS.get(kind, kind)} {when:%m/%d/%y}"
                for (kind, when) in sorted(dated, key=lambda pair: (pair[1], pair[0]))
            ]
            label = str(board_list.get("name") or "(unnamed)")
            lines.append(f"**{label}** — {', '.join(named) if named else 'nothing dated'}")

        missing = dailyops.missing_kinds(
            [card for bl in lists for card in client.list_cards(str(bl.get("id") or ""))], day
        )
        if missing:
            lines.append(
                "⚠ No card for today: "
                + ", ".join(dailyops.CARD_KINDS.get(kind, kind) for kind in missing)
            )
        return lines
    finally:
        client.close()


def moves_waiting(config: Config, step: str, *, day=None) -> tuple[list[str], list[str]]:
    """(card titles that would move, problems). Writes nothing.

    The same read the move itself does, so what gets shown and what gets moved
    cannot disagree.
    """
    from .. import dailyops, rollskip, trello

    day = day or board_day(config)
    held = rollskip.for_day(day)
    from_name, to_name = dailyops.STEP_LISTS[step]
    client = open_trello(config)
    try:
        lists = client.board_lists(config.secrets.trello_board_id)
        source = trello.find_list(lists, from_name)
        if source is None:
            return [], [f"The board has no list called {from_name!r}"]
        if trello.find_list(lists, to_name) is None:
            return [], [f"The board has no list called {to_name!r}"]

        found = []
        for card in client.list_cards(str(source.get("id") or "")):
            if not walks_today(card, day, step=step, held=held):
                continue
            title = str(card.get("name", ""))
            where = walk_to(card, step, day)
            # Named only when it isn't the one on the button, so the setup
            # card going somewhere else is visible before it goes there.
            found.append(title if where == to_name else f"{title} → {where}")

        fetch, notes = setups_to_pull(client, lists, day, step)
        found.extend(
            f"{card.get('name')} → {dailyops.IN_QUE} (for tomorrow)" for card in fetch
        )
        return found, notes
    finally:
        client.close()


def walks_today(card: dict, day: date, *, step: str, held=()) -> bool:
    """Whether the daily walk should move this card at all.

    Everything in the list except two kinds. A new agent's card is in In Que
    waiting to be filed, not waiting to be walked - it leaves by being filed,
    and sweeping it into Today loses it out of the only place anything looks
    for it. And a card dated for another day is not today's: In Que holds
    tomorrow's four from the evening before, and taking them across at nine in
    the morning starts the day a day early.

    The last move is the exception to the date. Nothing arrives in Quality
    Check ahead of its day, so a card dated before today is one that got left
    behind rather than one waiting its turn, and leaving it is how Quality
    Check silts up - "as long as the cards are on quality check ... you move
    them to done".

    Ads and Lead Order are finished at ten rather than at half eight, because
    that is when their work actually stops - so the half eight move leaves
    them where they are and the ten o'clock one takes only those two. A card
    nobody dated goes with the half eight sweep; at ten only the two named
    cards move, because anything else in Quality Check at that hour is
    something somebody left there on purpose.

    A card somebody held back from the carry stays too. Holding its items back
    is saying the work is not finished, and filing the card away in the same
    hour would put it out of sight with the work still on it.
    """
    from .. import agents, dailyops

    title = str(card.get("name", ""))
    if agents.is_agent_card(title):
        return False
    named = dailyops.parse_card_title(title)
    if step not in dailyops.DONE_STEPS:
        return True if named is None else named[1] == day

    late = step == dailyops.LATE_DONE
    if named is None:
        return not late
    if named[0] in set(held):
        return False
    if (named[0] in dailyops.LATE_KINDS) != late:
        return False
    # A card covering several days is finished on the last of them, not the
    # first. "Lead Order 09/05/26-09/07/26" is the Lead Order card on the
    # Saturday, the Sunday and the Monday - filing it away on the Saturday
    # night puts two days of live work out of sight.
    covers = dailyops.card_days(title)
    if len(covers) > 1 and day < max(covers):
        return False
    return named[1] <= day


def setups_to_pull(client, lists, day: date, step: str):
    """The setup cards six in the evening should fetch into In Que. (cards, notes).

    They are made in the Automation Department and sit there until it is their
    turn. In Que is the only list nine the next morning looks in, so a card
    still in Automation at nine spends its whole working day off to the side
    of the board.

    The one that comes over is the one *tomorrow* works on - the card for the
    day after that, because agents are set up the day before they go live.
    Tuesday evening fetches Thursday's card: Wednesday is when it gets worked.

    Anything whose working day has already gone by comes too, as long as its
    agents have not gone live yet. A card nobody made until its own working
    day - RYTE makes one himself when an agent turns up for a day that has no
    card - would otherwise have missed its only chance to be fetched and sat
    in Automation for good. Late and on the board beats on time and invisible.

    Found by the dates in the title rather than by list, because they do not
    always stay where they were made, and left alone once one has reached the
    day's lists: the point is to fetch it, not to drag it back.

    A setup card walks like everything else once it is in the day's lists -
    Today by nine on its working day, Quality Check by six, Done by half
    eight. Nothing here has to put one in Done, and nothing here moves one out
    of the aged-leads list: nothing but an aged-leads order belongs in there,
    so a setup card in it is somebody's slip, and the answer to a slip is to
    say so, not to shuffle it somewhere quietly. It is named in the notes and
    left exactly where it is.
    """
    from .. import agents, dailyops, trello

    if step != "to_quality_check":
        return [], []

    in_que = trello.find_list(lists, dailyops.IN_QUE)
    if in_que is None:
        return [], [f"The board has no list called {dailyops.IN_QUE!r}"]

    already = {
        str((trello.find_list(lists, name) or {}).get("id") or "\0")
        for name in (dailyops.IN_QUE, dailyops.TODAY, dailyops.QUALITY_CHECK, dailyops.DONE)
    }
    aged = str((trello.find_list(lists, dailyops.AGED_DONE) or {}).get("id") or "\0")
    tomorrow = dailyops.next_day(day)
    found, notes = [], []
    for card in (c for bl in lists for c in client.list_cards(str(bl.get("id") or ""))):
        title = str(card.get("name", ""))
        if not agents.is_setup_card(title):
            continue
        if str(card.get("idList") or "") == aged:
            notes.append(
                f"{title} is in {dailyops.AGED_DONE} — nothing but an aged-leads "
                "order belongs in there. Left it where it is."
            )
            continue
        worked = agents.setup_worked_on(title, day)
        starts = agents.setup_starts(title, day)
        if worked is None or worked > tomorrow:
            continue
        # Its agents are already live. That card is history, wherever it sits.
        if starts is None or starts < day:
            continue
        if str(card.get("idList") or "") in already:
            continue
        found.append((starts, card))

    # Furthest out first, because each move goes to the top: the one whose
    # agents go live soonest is moved last and ends up above the rest.
    found.sort(key=lambda pair: pair[0], reverse=True)
    return [card for _starts, card in found], notes


def _people_on(cards: dict, members: list[dict], holds: dict):
    """Board members, with which of the day's cards each keeps a checklist on.

    From the cards themselves rather than from a list in the code. The board
    says who does what - Therese has a checklist on Ops and nowhere else,
    Nicole has one on Ads and one on General - and a rule read off the board
    keeps being right after somebody joins.
    """
    from .. import tagged

    people = {}
    for member in members:
        keeps = {}
        for kind, card in cards.items():
            if kind not in tagged.WORK:
                continue
            names = [
                str(held.get("name") or "")
                for held in holds.get(str(card.get("id") or "")) or []
            ]
            found = tagged.checklist_for(str(member.get("fullName") or ""), names)
            if found:
                keeps[kind] = found
        if not keeps:
            continue
        people[str(member.get("username") or "").casefold()] = tagged.Person(
            username=str(member.get("username") or ""),
            full_name=str(member.get("fullName") or ""),
            keeps=keeps,
            home=tagged.home_for(keeps.values()),
        )
    return people


def suggestions(config: Config) -> tuple[str, list]:
    """What the notebook adds up to. (what to say, what it was built from).

    Suggests. Never acts, and nothing that calls this is allowed to either -
    "dont make him do it just suggest then we'll brainstorm".

    Claude does the reading, because the value is in seeing that four
    separate entries are one problem. When it cannot be reached the sightings
    are listed plainly instead: a list somebody has to think about themselves
    beats a blank message.
    """
    from anthropic import Anthropic

    from .. import copywriter, noticed

    found = noticed.worth_saying(noticed.notes())
    if not found:
        return noticed.nothing_yet(), []

    plain = "\n".join(f"• {noticed.describe(one)}" for one in found[:12])
    if not config.secrets.anthropic_api_key:
        return plain, found

    try:
        client = Anthropic(api_key=config.secrets.anthropic_api_key)
        response = client.messages.create(
            model=config.copy.model,
            max_tokens=1200,
            system=(
                "You keep a lead-generation company's daily board. You are "
                "telling the manager what you have noticed. Be specific and "
                "short. Suggest; never say you have done something."
            ),
            messages=[{"role": "user", "content": noticed.prompt(found)}],
        )
        written = "".join(
            block.text for block in response.content
            if getattr(block, "type", None) == "text"
        ).strip()
    except Exception as exc:
        return f"{plain}\n\n_(I couldn't think it through: {_short(exc, 100)})_", found

    return (written or plain), found


def tags_stamp(config: Config, *, day=None) -> str:
    """A fingerprint of when the day's three cards were last touched.

    One request for the whole board, so the watcher costs one call a tick
    while nothing is happening - which is nearly always. Reading the comments
    and the checklists only happens once this has changed.

    RYTE writing a line changes it too, so the tick after a write does one
    more full read and finds everything already filed. That is the check
    rather than the waste: it says the line landed.
    """
    from .. import dailyops, tagged

    day = day or board_day(config)
    client = open_trello(config)
    try:
        every = client.board_cards(config.secrets.trello_board_id)
        # Tomorrow's cards as well as today's, once they exist. A comment on
        # the 9/12 Ads card at four in the afternoon is a comment the watcher
        # has to wake up for, and it would not have moved today's stamp.
        stamps = []
        for which in days_with_cards(every, day):
            cards = dailyops.cards_covering(every, which)
            stamps += [
                f"{which:%m/%d}{kind}:{cards[kind].get('dateLastActivity') or ''}"
                for kind in sorted(cards) if kind in tagged.WORK
            ]
        return "|".join(stamps)
    finally:
        client.close()


def tags_to_file(config: Config, *, day=None, everything: bool = False) -> tuple[list, list[str]]:
    """The tagged comments that are not on anybody's checklist yet. (tasks, problems).

    Reads only. Every person tagged in a comment or written into a card's
    description, and where each one belongs - which is not the card it was
    said on, see `tagged`.

    Today's cards and any day's after it that already exists. Tomorrow's four
    land in In Que around eleven, so from lunchtime there are two days open at
    once and people comment on both - "if today still theres a card for 9/12
    and theres a comment there, it will be added to 9/12". A comment stays on
    its own day: one on the 9/12 Ads card lands on a 9/12 checklist, never on
    today's. Yesterday's are left alone, because a card in Done is finished.

    One that is already filed is skipped by the comment's id, so running this
    twice in an afternoon adds nothing the first run added.
    """
    from .. import dailyops

    day = day or board_day(config)
    client = open_trello(config)
    problems: list[str] = []
    try:
        lists = client.board_lists(config.secrets.trello_board_id)
        every = _cards_of(client, config.secrets.trello_board_id, lists)
        members = client.board_members(config.secrets.trello_board_id)

        days = days_with_cards(every, day)
        if not days:
            return [], [f"No General, Ops or Ads card dated {day:%m/%d/%y} on the board"]

        from .. import tagged as rules_tags

        tasks = []
        for which in days:
            tasks += _tags_on(config, client, every, members, which, problems)
        if everything:
            # "check if he missed adding anything on checklist from the
            # comments here for all of members, THESE ARE IMPORTATNT" -
            # every tag, reports included: asked for, not run on the clock.
            return tasks, problems
        # A checklist is what is left to do. "Updated email for Killian
        # Cooper" is a line about work that already happened, and it would
        # arrive on somebody's list already true - "not an update on a current
        # tasks".
        return [
            one for one in tasks
            if not rules_tags.an_update(one.summary)
            # The comment itself too: a reading can turn "Connor Kachlic CRM
            # connected" into words that no longer sound finished.
            and not rules_tags.only_a_report(getattr(one.note, "text", "") or "")
        ], problems
    finally:
        client.close()


def days_with_cards(every: list, day) -> list:
    """Today, and any later day that already has one of the three cards.

    Today first, so its list is read and shown before tomorrow's - most of
    what is waiting is today's, and tomorrow's is usually one line somebody
    wrote ahead.
    """
    from .. import dailyops, tagged

    found = {
        when for kind, when in dailyops.daily_cards(every)
        if kind in tagged.WORK and when >= day
    }
    found.add(day)
    return sorted(when for when in found if dailyops.cards_covering(
        every, when
    ).keys() & set(tagged.WORK))


def _tags_on(config, client, every, members, day, problems) -> list:
    """One day's comments and description, read and routed onto that day."""
    from .. import dailyops, noticed, tagged, trello

    cards = dailyops.cards_covering(every, day)
    wanted = {kind: card for kind, card in cards.items() if kind in tagged.WORK}
    if not wanted:
        return []

    holds = {
        str(card.get("id") or ""): client.card_checklists(str(card.get("id") or ""))
        for card in wanted.values()
    }
    people = _people_on(wanted, members, holds)
    # Everybody on the board, checklist or not. What is not in here was
    # never a tag: Therese's confirmations say "@Corbin Simpson" and Faith
    # writes "@jadon", and those are the agents being talked about rather
    # than anybody being given a job. Trello renders them as plain text
    # for exactly that reason.
    on_the_board = {
        str(one.get("username") or "").casefold() for one in members
    } - {""}

    everywhere = [held for group in holds.values() for held in group]

    def held_by(person) -> list:
        """This person's checklists, on every one of the day's cards."""
        mine = set((person.keeps or {}).values())
        return [
            one for group in holds.values() for one in group
            if str(one.get("name") or "").strip() in mine
        ]

    wants, unknown, nothing_said = [], set(), []
    for kind, card in wanted.items():
        card_id = str(card.get("id") or "")
        short = trello.linked_card_id(str(card.get("url") or card.get("shortUrl") or ""))
        for said in client.card_notes(card_id):
            note = tagged.Note(
                comment_id=str(said.get("id") or ""),
                text=str(said.get("text") or ""),
                author=str(said.get("author") or ""),
                card_id=card_id,
                card_short=short or str(card.get("shortLink") or ""),
                card_title=str(card.get("name") or ""),
            )
            # An order already running, topped up. The agent's own card is
            # already on their list, so the line would say again what is
            # there - "if it says ongoing order specifically, dont add".
            if tagged.an_ongoing_order(note.text):
                continue

            # Somebody saying a setup has been done. A confirmation is not a
            # job, and an afternoon of them arrived as forty-four offers to
            # put the same line on the Ops checklist. What they are for is
            # read by the check that watches for agents set up on leads they
            # did not order, which is a different question from this one.
            if tagged.a_setup_confirmation(note.text):
                continue

            # A screenshot and a tag. Trello writes the attachment into the
            # comment as a link to it, so all the words say is "image.png",
            # which went onto KC's list looking exactly like that.
            if tagged.only_a_file(note.text) or _too_vague(note.text):
                # Said only when it was handed to somebody. A picture tagging
                # nobody is nobody's job, and saying so is noise.
                if not tagged.mentioned(note.text):
                    if tagged.only_a_file(note.text):
                        continue
                else:
                    # "BRO IMAGE CONTEXT" - Faith's "can you take a look" on
                    # a screenshot of Michael Greff getting 28 leads with a cap
                    # of 10: the job is in the picture, so the picture is read.
                    seen = _picture_said(config, client, note)
                    if seen:
                        words = _words_of(note.text)
                        note = dataclasses.replace(
                            note, text=f"{seen}" + (f" ({words})" if words else "") + "\n" + note.text)
                    elif tagged.only_a_file(note.text):
                        nothing_said.append(note)
                        continue

            if tagged.everyones_job(note.text):
                # Every checklist on the card it was said on, and asked per
                # checklist: it belongs on all of them, so finding it on
                # Kath's is no reason to leave Nicole's without it.
                for held in holds.get(card_id) or []:
                    if tagged.already_on(note, held):
                        continue
                    wants.append(Want(note, None, kind, str(held.get("name") or "")))

            tags = [
                name for name in tagged.mentioned(note.text)
                if name in on_the_board
            ]

            # Named at the start of a line, without an @. Arnold writes
            # the week as "Jenn = FRIDAY" with the work under it and tags
            # nobody, and those were jobs handed over that nothing wrote
            # down. Whoever was properly tagged is left to the tag, which
            # carries the whole comment rather than one slice of it.
            #
            # The person goes through, not just their checklist's name, so
            # it routes the way a tag does: Jenn's ads work belongs on Ads
            # wherever Arnold wrote it, and being named rather than tagged
            # does not make it everybody's.
            by_list = {
                one.keeps[where]: one
                for one in people.values() for where in one.keeps
            }
            for whose, said in tagged.named_without_tagging(
                note.text, list(by_list)
            ).items():
                person = by_list.get(whose)
                if person is None or person.username.casefold() in tags:
                    continue
                # Asked against this person's own checklists rather than
                # against all of them: one comment can hand work to two
                # people, and Jenn's line already being there is no reason
                # to leave Kath without hers.
                wants.append(Want(
                    note, person, slice=said,
                    filed=tagged.how_many_filed(note, held_by(person)),
                ))

            # A lead schedule is Nicole's whether or not anybody tagged
            # her - "if its schedule like this add to nicole on ads even
            # if not tagged". Only when nobody was: a schedule handed to
            # somebody by name is theirs, and the tag says so.
            if not tags and tagged.a_lead_schedule(note.text):
                keeper = tagged.keeps_the_schedules(people)
                if keeper is None:
                    problems.append(
                        f"“{tagged.trim(note.text)[:40]}” looks like a lead "
                        f"schedule and there's no {tagged.SCHEDULES} checklist "
                        f"on today's {tagged.SCHEDULES_ON} card, so I left it"
                    )
                elif not tagged.already_filed(note, everywhere):
                    wants.append(Want(note, keeper, schedule=True))
                continue

            if not tags:
                continue
            for name in tags:
                if name not in people:
                    unknown.add(name)
                    continue
                person = people[name]
                # Counted against this person's own checklists, on every
                # card rather than this one: the line for a comment on
                # General lands on Ops, so looking at General alone would
                # file it again every afternoon. Counted rather than asked
                # yes or no, because one comment can be three jobs and the
                # first line filed would otherwise stop the other two.
                wants.append(Want(
                    note, person,
                    filed=tagged.how_many_filed(note, held_by(person)),
                ))

    # An ongoing order is not reported. A skip nobody can see is usually the
    # bug, and this one is the exception: it is a rule that was set - "if it
    # says ongoing order specifically, dont add" - rather than something RYTE
    # decided, and the agent's own card is already on the list. Saying it
    # every run is a warning about nothing, and warnings about nothing are how
    # the one that matters stops being read.
    if unknown:
        for name in sorted(unknown):
            _jot(noticed, "no_checklist", f"@{name}")
        problems.append(
            "On the board but with no checklist on today's cards, so I left "
            "them: " + ", ".join(f"@{name}" for name in sorted(unknown))
        )

    # Said so it can be read: "1 said nothing but the name of what was
    # attached to them, so I left them: OTP MTG PLUS relaunch setup sc" was a
    # "huh?". Which file, for whom, and where - one line each.
    for one in nothing_said:
        if tagged.VIA_RYTE.search(one.text or ""):
            # Ryte's own, for somebody: it puts the tagged on their lists itself.
            continue
        file = tagged.attached_name(one.text)
        if len(file) > 80:
            file = file[:77] + "…"
        who = ", ".join(f"@{name}" for name in tagged.mentioned(one.text))
        where = f" · [said here](<{one.link()}>)" if one.card_short else ""
        problems.append(
            f"📎 A comment with just an attachment{f' for {who}' if who else ''} and no "
            f"words — “{file}” — so I didn't put it on a checklist. If it's a job, "
            f"add it yourself{where}"
        )

    tasks = _read_the_tags(
        config, wants, people, wanted, problems, held_by=held_by,
    )
    tasks += _described_tasks(
        client, wanted, holds, people, on_the_board, problems
    )
    return tasks


def _described_tasks(client, cards, holds, people, on_the_board, problems) -> list:
    """The jobs written into the day's card descriptions, one line each.

    Not summarised. A comment gets a summary because it is somebody talking
    and the job is somewhere inside it; a description line is already the job,
    written short by the person handing it over. Keeping the words is also the
    only way to know it has been filed - there is no comment id to match on, so
    the line's own words have to be what says so.

    The cost of that, said plainly rather than hidden: an edited line is a
    different line, so it lands again and the old one stays. Rewriting the
    description rewrites nothing that has already been filed.
    """
    from .. import noticed, tagged, trello

    found = []
    for kind, card in cards.items():
        card_id = str(card.get("id") or "")
        try:
            desc = str(client.card_detail(card_id).get("desc") or "")
        except Exception as exc:
            problems.append(
                f"Couldn't read the description on {card.get('name') or kind}: "
                f"{_short(exc, 120)}"
            )
            continue

        theirs, nobody = tagged.description_tasks(desc)
        if nobody:
            problems.append(
                f"{len(nobody)} line(s) in the {kind} card's description with "
                "nobody tagged for them, so I left them: "
                + "; ".join(_short(said, 40) for said in nobody)
            )

        short = trello.linked_card_id(
            str(card.get("url") or card.get("shortUrl") or "")
        ) or str(card.get("shortLink") or "")
        waiting: dict = {}
        for told in theirs:
            if told.username not in people:
                # Said rather than swallowed. A comment can say "@jadon" about
                # an agent and mean nobody, but a tag in a description was
                # typed to hand work over, and the work is still sitting there
                # unfiled either way.
                #
                # One line per person, not per job: Elisa's block is four jobs
                # and four copies of the same sentence is four lines nobody
                # reads to the end of.
                waiting.setdefault(told.username, []).append(told.text)
                continue

            person = people[told.username]
            where, judged = tagged.where(person, None)
            if where not in cards:
                problems.append(
                    f"{person.full_name or person.username} — “{told.text[:40]}” is "
                    f"{where} work and there's no {where} card today, so I left it"
                )
                continue

            # Against this person's own checklists. One description hands work
            # to five people and their lines are not each other's.
            mine = set(person.keeps.values())
            already = [
                held for group in holds.values() for held in group
                if str(held.get("name") or "").strip() in mine
            ]
            if tagged.already_said(told.text, already):
                continue

            lands = cards.get(where) or {}
            found.append(tagged.Task(
                note=tagged.Note(
                    comment_id="", text=told.text, card_id=card_id,
                    card_short=short, card_title=str(card.get("name") or ""),
                    described=True,
                ),
                person=person, kind=where, checklist=person.keeps[where],
                card_id=str(lands.get("id") or ""),
                card_title=str(lands.get("name") or ""),
                summary=told.text, judged=judged,
            ))

        for username, lines in waiting.items():
            _jot(noticed, "no_checklist", f"@{username}")
            missing = (
                "no checklist on today's cards" if username in on_the_board
                else "not on the board"
            )
            problems.append(
                f"@{username} is in the {kind} card's description with {missing}, "
                f"so {len(lines)} line(s) are still only written there: "
                + "; ".join(_short(said, 40) for said in lines)
            )
    return found


@dataclass
class Want:
    """One comment and one person it might be a job for, before it is read."""

    note: "object"
    person: "object" = None
    #: Filled in only for `@card`, where the card it was said on is the card.
    kind: str = ""
    checklist: str = ""
    #: The part of the comment that is theirs, when they were named not tagged.
    slice: str = ""
    #: True for a lead schedule, which goes on whole and always onto Ads.
    schedule: bool = False
    #: How many lines from this comment are already on their checklists.
    filed: int = 0


#: A tagged picture's job, read once a comment: {comment id: (line, when)}.
#: The tag check runs every half minute.
_PICTURES_SAID: dict = {}
PICTURE_RETRY = timedelta(hours=1)
_TRELLO_FILE = re.compile(r"\((https://trello\.com/1/cards/[^)\s]+/attachments/[^)\s]+/download/[^)\s]+)\)")
_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def _words_of(text: str) -> str:
    """What a comment says, without its tags or the files Trello wrote in."""
    from .. import tagged

    bare = tagged.strip_mentions(re.sub(r"!?\[[^\]]*\]\([^)]*\)", " ", text or ""))
    return " ".join(bare.split()).strip(" -–—:,.")


def _too_vague(text: str) -> bool:
    """A picture tagged with a few words that don't say the job: "can you
    take a look", "see this", "pls check"."""
    return bool(_TRELLO_FILE.search(text or "")) and len(_words_of(text).split()) < 6


def _picture_said(config: Config, client, note) -> str:
    """The job a tagged screenshot hands somebody, in one line, read by
    Claude - or "" when it can't be read. Once a comment."""
    import base64
    import time

    held = _PICTURES_SAID.get(note.comment_id)
    if held is not None and (held[0] or time.time() - held[1] < PICTURE_RETRY.total_seconds()):
        return held[0]
    said = ""
    try:
        pictures = []
        for url in _TRELLO_FILE.findall(note.text or "")[:2]:
            data, kind = client.download(url)
            if kind in _IMAGE_TYPES:
                pictures.append({"type": "image", "source": {
                    "type": "base64", "media_type": kind, "data": base64.b64encode(data).decode()}})
        if pictures and str(getattr(config.secrets, "anthropic_api_key", "") or ""):
            from anthropic import Anthropic

            words = _words_of(note.text)
            ask = ("A teammate on Agent Lead Lab's Trello board tagged somebody on this screenshot"
                   + (f", saying “{words}”" if words else "") + ". In one short line, write the job it hands them "
                   "as an instruction, with the names, numbers and what to check - e.g. \"Michael Greff, capped at "
                   "10 leads a day, got 28 over the weekend - find out why\". Only the line.")
            reply = Anthropic(api_key=config.secrets.anthropic_api_key).messages.create(
                model=config.copy.model, max_tokens=200,
                messages=[{"role": "user", "content": [*pictures, {"type": "text", "text": ask}]}])
            said = " ".join("".join(getattr(one, "text", "") for one in reply.content).split()).strip(' "“”')[:220]
    except Exception:
        logging.getLogger("wilbyte.bot").warning("Couldn't read the tagged picture", exc_info=True)
        said = ""
    _PICTURES_SAID[note.comment_id] = (said, time.time())
    if len(_PICTURES_SAID) > 300:
        for old in sorted(_PICTURES_SAID, key=lambda one: _PICTURES_SAID[one][1])[:100]:
            _PICTURES_SAID.pop(old, None)
    return said


def _read_the_tags(config, wants, people, cards, problems, held_by=None) -> list:
    """Turn wants into tasks, summarised and routed.

    A person means the kind still has to be decided; a kind and checklist
    already filled in means the comment tagged the card and there is nothing
    to decide.

    Claude reads the batch and answers with one entry per job per person -
    several for one comment when it holds several, which is the ordinary case
    on the General card. Tre's says what the money moved to, asks Kath and
    Jenn to set up a call, and gives Kath a list of scene changes: three jobs
    in one comment, and until now one line each for the people it tagged.

    When the reading fails - no key, a bad night at Anthropic - every task is
    still made from the comment's own first line, because a line on the wrong
    checklist is recoverable and a task nobody wrote down is not.
    """
    from .. import tagged

    if not wants:
        return []

    by_id = {}
    for want in wants:
        by_id.setdefault(want.note.comment_id, want.note)

    written = {}
    try:
        written = _ask_about_tags(config, list(by_id.values()), people)
    except Exception as exc:
        problems.append(
            "Couldn't have the comments read, so these are the raw first lines: "
            f"{_short(exc, 120)}"
        )

    tasks = []
    claimed: set = set()
    for want in wants:
        note, person = want.note, want.person
        told_kind, told_list = want.kind, want.checklist
        just_theirs, schedule = want.slice, want.schedule

        # Every job the reading gave this person out of this comment. More
        # than one is normal; none means it fell back to the comment itself.
        mine = _their_jobs(written, note.comment_id, person, people)
        for one in mine:
            claimed.add(id(one))
        for made in _lines_from(
            want, mine, people, cards, problems, schedule=schedule,
            just_theirs=just_theirs, told_kind=told_kind, told_list=told_list,
        ):
            tasks.append(made)

    # Somebody the comment hands work to and nobody tagged. "Mandatory entire
    # amount needs moved over Kath and Jenn setup a discord call" is a job for
    # both of them and tags neither, and only a reading can tell that from
    # "ask Nicole about the budget".
    for comment_id, entries in (written or {}).items():
        note = by_id.get(comment_id)
        if note is None:
            continue
        for entry in entries:
            if id(entry) in claimed:
                continue
            person = tagged.person_named(str(entry.get("person") or ""), people)
            if person is None or not person.keeps:
                continue
            # Only somebody the comment names. The reading guessed: Faith got
            # Tre's Roadmap steps and "Report back to Santiago Villegas" off
            # her own comment - "these are not faith's tasks right?".
            if not _named_in_comment(note.text, person):
                continue
            already = tagged.how_many_filed(
                note, held_by(person) if held_by else []
            )
            same = [
                one for one in (written.get(comment_id) or [])
                if tagged.person_named(str(one.get("person") or ""), people)
                is person
            ]
            if same.index(entry) < already:
                continue
            made = _one_line(note, person, entry, cards, problems)
            if made is not None:
                tasks.append(made)
    return tasks


def _named_in_comment(text: str, person) -> bool:
    """Whether a comment names this person - username, full name, first name
    or what the team calls them ("Kath", "Jenn", "KC") - as a word of its
    own. "Kath and Jenn setup a discord call" names both."""
    from .. import dailyops

    said = str(text or "").casefold()
    username = str(getattr(person, "username", "") or "").casefold()
    names = {username, str(getattr(person, "full_name", "") or "").casefold()}
    first = person.first_name() if hasattr(person, "first_name") else ""
    names.add(str(first or "").casefold())
    names |= {called for called, handle in dailyops.TEAM_HANDLES.items() if handle.casefold() == username}
    return any(name and re.search(rf"(?<![\w@]){re.escape(name)}(?!\w)", said) for name in names)


def _their_jobs(written: dict, comment_id: str, person, people) -> list:
    """The reading's entries for this person on this comment, in order."""
    from .. import tagged

    entries = (written or {}).get(comment_id) or []
    if person is None:
        return entries[:1]
    found = [
        one for one in entries
        if str(one.get("person") or "").casefold() == (person.username or "").casefold()
    ]
    if found:
        return found
    return [
        one for one in entries
        if tagged.person_named(str(one.get("person") or ""), people) is person
    ]


def _one_line(note, person, entry: dict, cards, problems, *, own_card=False):
    """One task for one person from one of the reading's entries, or None.

    `own_card` throws away what the reading made of the kind. It is for work
    the reading was not asked to place - somebody named at the start of a line
    with their week under it - where the person's own card is the answer and
    a kind read off the whole comment put both of them on General.
    """
    from .. import tagged

    summary = " ".join(str(entry.get("summary") or "").split())
    if not summary:
        return None
    kind, judged = tagged.where(
        person, note, judged="" if own_card else str(entry.get("kind") or ""),
    )
    if kind not in cards:
        problems.append(
            f"{person.full_name or person.username} — “{summary}” is {kind} "
            f"work and there's no {kind} card today, so I left it"
        )
        return None
    card = cards.get(kind) or {}
    return tagged.Task(
        note=note, person=person, kind=kind, checklist=person.keeps[kind],
        card_id=str(card.get("id") or ""), card_title=str(card.get("name") or ""),
        summary=summary, judged=judged, is_task=_judged_a_task(entry),
    )


def _judged_a_task(entry: dict) -> bool | None:
    """The reading's is_task, or None when the reading predates asking it."""
    said = (entry or {}).get("is_task")
    return said if isinstance(said, bool) else None


def _lines_from(
    want, mine, people, cards, problems, *, schedule, just_theirs,
    told_kind, told_list,
) -> list:
    """Every task one want turns into. Usually one; sometimes three."""
    from .. import tagged

    note, person = want.note, want.person

    # `@card`: the card it was said on is the card, and every checklist on it
    # gets the same line.
    if person is None:
        said = mine[0] if mine else {}
        summary = str(said.get("summary") or "").strip()
        if not summary or tagged.brief_already(note.text):
            summary = tagged.trim(note.text) or summary
        if not summary:
            return []
        card = cards.get(told_kind) or {}
        return [tagged.Task(
            note=note, person=None, kind=told_kind, checklist=told_list,
            card_id=str(card.get("id") or ""),
            card_title=str(card.get("name") or ""),
            summary=summary, everyone=True, is_task=_judged_a_task(said),
        )]

    # A lead schedule goes on whole and always onto Ads. Not summarised and
    # not cut to nine words: "ANTHONY SINGH (VET)- monday- saturday 9 am- 9
    # pm" trimmed to a line's worth loses the pm, and the hours are the
    # content. "sending leads" is ops in the abstract, and the drip windows
    # are set on the Ads card.
    if schedule:
        summary = tagged.strip_mentions(note.text)
        if not summary:
            return []
        kind, judged = (
            (tagged.SCHEDULES_ON, False) if person.keeps.get(tagged.SCHEDULES_ON)
            else tagged.where(person, note)
        )
        if kind not in cards:
            problems.append(
                f"{person.full_name or person.username} — “{summary}” is {kind} "
                f"work and there's no {kind} card today, so I left it"
            )
            return []
        card = cards.get(kind) or {}
        return [tagged.Task(
            note=note, person=person, kind=kind, checklist=person.keeps[kind],
            card_id=str(card.get("id") or ""),
            card_title=str(card.get("name") or ""),
            # The hours somebody's leads run is always work to set up.
            summary=summary, judged=judged, is_task=True,
        )]

    # Every job the reading gave this person, minus however many lines from
    # this comment are already on their list. Counted rather than asked yes or
    # no: one comment can be three jobs and they all carry the same link, so
    # the first line filed would otherwise stop the other two.
    if mine:
        found = []
        for entry in mine[want.filed:]:
            made = _one_line(
                note, person, entry, cards, problems, own_card=bool(just_theirs),
            )
            if made is not None:
                found.append(made)
        return found

    # Nothing read it. The comment's own first words, or - for somebody named
    # at the start of a line - the words that follow their name.
    if want.filed:
        return []
    summary = tagged.trim(just_theirs) if just_theirs else tagged.trim(note.text)
    made = _one_line(
        note, person, {"summary": summary}, cards, problems, own_card=True,
    )
    return [made] if made is not None else []


#: Held while a batch of comments is being read, so two looks at the same
#: moment wait for one reading rather than making two. The watcher and a
#: hand-typed "@RYTE tags" both run in threads, and both reading the same
#: comment at once is exactly how it came back two different ways.
_READING_TAGS = threading.Lock()


def _ask_about_tags(config: Config, notes: list, people: dict) -> dict:
    """{comment id: [{"person", "summary", "kind"}, ...]}, the same every time.

    The first reading of a comment is kept and reused - see `tagreads`. Only
    comments not read before go to Claude. A reading that fails is not kept,
    so the next look tries again rather than remembering nothing.
    """
    from .. import tagreads

    with _READING_TAGS:
        held = tagreads.load()
        who = tagreads.people_key(people)
        written: dict = {}
        fresh = []
        for note in notes:
            key = tagreads.key_for(note.comment_id, note.text, who)
            if key in held:
                if held[key]:
                    written[note.comment_id] = held[key]
            else:
                fresh.append(note)
        if not fresh:
            return written

        got = _read_tags_fresh(config, fresh, people)
        for note in fresh:
            lines = got.get(note.comment_id) or []
            held[tagreads.key_for(note.comment_id, note.text, who)] = lines
            if lines:
                written[note.comment_id] = lines
        tagreads.save(held)
        return written


def _read_tags_fresh(config: Config, notes: list, people: dict) -> dict:
    """{comment id: [{"person", "summary", "kind"}, ...]}, written by Claude.

    A list rather than one entry, because one comment is often several jobs
    for several people.
    """
    from anthropic import Anthropic

    from .. import tagged

    config.secrets.require("anthropic_api_key")
    client = Anthropic(api_key=config.secrets.anthropic_api_key)
    response = client.messages.create(
        model=config.copy.model,
        max_tokens=2000,
        system=(
            "You turn comments on a team's Trello cards into checklist lines. "
            "Say what the tagged person has to do, in the words the comment "
            "used. Never invent a task the comment does not ask for, and never "
            "give somebody a line about work the comment gave to somebody else. "
            "Mark is_task true only when the comment asks that person to DO "
            "something still to be done. It is false for an update or report "
            "of work already done, an FYI, a question, a thank-you, a reply, "
            "or chatter - those go on nobody's checklist. When unsure, false."
        ),
        tools=[{
            "name": "lines",
            "description": "One line per job per person per comment.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "lines": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "comment_id": {"type": "string"},
                                "person": {"type": "string"},
                                "summary": {"type": "string"},
                                "kind": {"type": "string", "enum": list(tagged.WORK)},
                                "is_task": {
                                    "type": "boolean",
                                    "description": "A job this person still has to do - "
                                                   "not an update, report, FYI, question or chatter.",
                                },
                            },
                            "required": ["comment_id", "person", "summary", "kind", "is_task"],
                        },
                    }
                },
                "required": ["lines"],
            },
        }],
        tool_choice={"type": "tool", "name": "lines"},
        messages=[{"role": "user", "content": tagged.summary_prompt(notes, people)}],
    )
    # Read by the name we asked for. `copywriter._extract_tool_input` looks
    # for "emit_blog_package" and nothing else, so borrowing it meant every
    # batch raised "Model did not call emit_blog_package" and every line fell
    # back to the comment's own first words - which is why a summary came out
    # as "CONNOR SWARTZ has an ongoing order that still need".
    payload = _tool_input(response, "lines")
    written: dict = {}
    for line in payload.get("lines") or []:
        written.setdefault(str(line.get("comment_id") or ""), []).append(line)
    return written


def _tool_input(response, name: str) -> dict:
    """What the model passed to the tool it was told to call."""
    if getattr(response, "stop_reason", None) == "max_tokens":
        raise RuntimeError(
            f"The model ran out of room before finishing {name}."
        )
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == name:
            return dict(block.input)
    raise RuntimeError(
        f"The model didn't call {name} (stop reason: "
        f"{getattr(response, 'stop_reason', 'unknown')})"
    )


def file_tags(config: Config, tasks: list) -> tuple[list[str], list[str]]:
    """Write the lines. (what landed, problems).

    Nothing is created that is not already there: every task names a checklist
    that exists, because it was found by reading that card's checklists in the
    first place.
    """
    client = open_trello(config)
    landed, problems = [], []
    held_by_card: dict[str, list] = {}
    try:
        for task in tasks:
            if not task.card_id:
                problems.append(f"{task.summary} — I lost track of which card that was")
                continue
            if task.card_id not in held_by_card:
                held_by_card[task.card_id] = client.card_checklists(task.card_id)
            found = next(
                (one for one in held_by_card[task.card_id]
                 if str(one.get("name") or "").strip().casefold()
                 == task.checklist.strip().casefold()),
                None,
            )
            if found is None:
                problems.append(
                    f"{task.summary} — no “{task.checklist}” checklist on {task.card_title}"
                )
                continue
            try:
                client.add_check_item(str(found.get("id") or ""), task.item())
            except Exception as exc:
                problems.append(f"{task.summary} — {_short(exc, 160)}")
                continue
            landed.append(f"{task.card_title} · {task.checklist} — {task.summary}")
    finally:
        client.close()
    return landed, problems


def link_setup_on_day(config: Config, *, for_day=None) -> tuple[list[str], list[str]]:
    """Put a day's setup card on that day's Ads and Ops cards. (added, problems).

    Kath, Jenn and Nicole on Ads; Therese on Ops. They are the people doing
    the setting up, and the card they are doing it from is not one of the four
    they have open - a link on their own checklist is how it gets in front of
    them.

    `for_day` is the date on the Ads and Ops cards, and the setup card that
    goes on them is the one worked that same day. Called with tomorrow: the
    four land in In Que around eleven and the setup card was made at six, so
    by then both halves exist and the link is on the card before anybody opens
    it.

    Safe to run twice. A checklist already carrying that card is left alone,
    matched on the card's short id rather than the whole URL.
    """
    from .. import agents as rules
    from .. import dailyops, trello

    day = for_day or dailyops.next_day(board_day(config))
    client = open_trello(config)
    added, problems = [], []
    try:
        lists = client.board_lists(config.secrets.trello_board_id)
        every = [c for bl in lists for c in client.list_cards(str(bl.get("id") or ""))]

        # Every one worked that day: the day before the team's out works its
        # own and the day out's - Friday 10/30 and Saturday-Monday together.
        setups = [
            card for card in every
            if rules.is_setup_card(str(card.get("name", "")))
            and rules.setup_worked_on(str(card.get("name", "")), day) == day
        ]
        dated = dailyops.cards_covering(every, day)
        for setup in setups:
            link = str(setup.get("url") or setup.get("shortUrl") or "")
            short = trello.linked_card_id(link)
            if short:
                _link_setup_onto(client, dated, day, link, short, added, problems)
    finally:
        client.close()
    return added, list(dict.fromkeys(problems))


def _link_setup_onto(client, dated: dict, day, link: str, short: str, added: list, problems: list) -> None:
    """One setup card onto the day's Ads and Ops checklists."""
    from .. import agents as rules
    from .. import dailyops, trello

    for kind, people in (("ads", rules.ADS_PEOPLE), ("ops", rules.OPS_PEOPLE)):
        card = dated.get(kind)
        if card is None:
            problems.append(
                f"No {dailyops.CARD_KINDS.get(kind, kind)} card dated {day:%m/%d/%y}"
            )
            continue
        card_id = str(card.get("id") or "")
        held = client.card_checklists(card_id)
        by_name = {
            " ".join(str(c.get("name") or "").split()).casefold(): c for c in held
        }
        for person in people:
            found = by_name.get(" ".join(person.split()).casefold())
            if found is None:
                # Not created here. The checklists are named by whoever
                # made the card, and "Kath" against a list that calls her
                # "Kathleen" would make a second one nobody reads.
                problems.append(f"{card.get('name')} has no {person!r} checklist")
                continue
            if any(
                trello.linked_card_id(str(item.get("name") or "")) == short
                for item in found.get("checkItems") or []
            ):
                continue
            try:
                client.add_check_item(str(found.get("id") or ""), link)
            except Exception as exc:
                problems.append(
                    f"{card.get('name')} · {person} — {_short(exc, 120)}"
                )
                continue
            added.append(f"{card.get('name')} · {person}")


def _paid_for(desc: str) -> str:
    """What their newest Payra payment was for, found by the card's email,
    then phone, then name - or ""."""
    from .. import payra

    try:
        data = payra.load()
    except Exception:
        return ""
    email = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", desc or "")
    phone = re.search(r"(?im)^\s*phone(?:\s*number)?\s*:\s*([+\d()][\d()\s.\-+]{6,})$", desc or "")
    first = re.search(r"(?im)^\s*first\s*name\s*:\s*(.+)$", desc or "")
    last = re.search(r"(?im)^\s*last\s*name\s*:\s*(.+)$", desc or "")
    name = re.search(r"(?im)^\s*name\s*:\s*(.+)$", desc or "")
    whole = (f"{first.group(1)} {last.group(1)}" if first and last else name.group(1) if name else "").strip()
    for who in (email.group(0) if email else "", phone.group(1) if phone else "", whole):
        found = payra.find(data, who) if who else []
        product = next((str(one.get("product") or "") for one in found if one.get("product")), "")
        if product:
            return product
    return ""


# How far back to look. A confirmation lands hours or a day after the card is
# filed, and `dateLastActivity` moves when somebody comments - so two days
# covers the gap and keeps the check to a handful of cards rather than the
# whole board.
SETUP_HOURS = 48


def wrong_setups(
    config: Config, *, day=None, hours: int = SETUP_HOURS
) -> tuple[list[dict], list[str]]:
    """Agents set up on leads they did not order. (found, problems). Reads only.

    Only the ones going live today or tomorrow. An agent who went live last
    week was either put right at the time or was not, and either way it is not
    tonight's problem - the point of this is to catch a wrong setup while
    there is still time to fix it before the leads start flowing.

    Every list, not just Done. The confirmation lands whenever the setting up
    is finished, and by then the card is usually in Done - but one still
    parked in Franklin's list or sitting in In Que can be set up early, and a
    check that only looks in Done would miss exactly the ones nobody has moved
    on from.

    The activity window is a cheap first cut before any card is opened. A
    confirmation is a comment and a comment moves `dateLastActivity`, so a
    card that has not changed in two days has nothing new to disagree about -
    and the sixty of them cost one request between them rather than sixty.
    """
    from .. import agents, dailyops, trello

    day = day or board_day(config)
    tomorrow = dailyops.next_day(day)
    client = open_trello(config)
    found, problems = [], []
    try:
        lists = client.board_lists(config.secrets.trello_board_id)
        fresh = _since(hours)
        for card in (c for bl in lists for c in client.list_cards(str(bl.get("id") or ""))):
            title = str(card.get("name", ""))
            if not agents.is_agent_card(title):
                continue
            if str(card.get("dateLastActivity") or "") < fresh:
                continue
            card_id = str(card.get("id") or "")
            desc = str(client.card_detail(card_id).get("desc") or "")
            # Both halves when two things were ordered. Catherine Y Barney
            # bought vets and FEX; set up on vets and compared against FEX
            # alone, she was raised as wrong when she was right.
            ordered = agents.stated_orders(desc)
            if not ordered:
                continue
            said = client.card_comments(card_id)
            launch = agents.find_launch(desc, today=day) or agents.find_launch(
                "\n".join(said), today=day
            )
            if launch not in (day, tomorrow):
                continue
            clash = agents.wrong_setup(ordered, said)
            if clash is None:
                continue
            # "it has two leads type, can ryte confirm what they paid for ...
            # before assuming its wrong" - Xenaida Garcia's card says "Lead
            # Type: Text Verified Spanish IUL" and "25 Final Expense Spanish
            # Leads"; she was set up on Spanish FEX. What Payra took the money
            # for decides, and a setup on what was paid for is no alarm.
            setup_kinds = agents.families_in(clash[1])
            paid = _paid_for(desc)
            if paid and setup_kinds and agents.families_in(paid) == setup_kinds:
                continue
            card_also = next((one for one in agents._phrases_in(desc)
                              if one != clash[0] and setup_kinds and agents.families_in(one) == setup_kinds), "")
            # What the confirmation says about the leads away from its opening
            # line. Faith's on Eduardo Munoz opened "OTP VET" and then gave a
            # form slug and a sheet both saying FB Spanish IUL - which is what
            # he ordered. Nothing was set up wrong; a template wasn't finished.
            also = agents.setup_also_said(said)
            found.append({
                **card,
                "agent": agents.agent_name(title),
                "ordered": clash[0],
                "setup": clash[1],
                "also": also,
                # Compared the way the spread compares two spellings, not the
                # way the headline is: a sheet name saying "SPANISH IUL" leaves
                # the tier out, and holding that against it would call a
                # matching body a mismatch.
                "typo": bool(also) and agents.setup_conflict(ordered, also) is None,
                "when": "today" if launch == day else "tomorrow",
                "where": _list_named(lists, str(card.get("idList") or "")),
                "paid": paid,
                "card_also": card_also,
            })
    except Exception as exc:  # one bad card must not lose the rest
        problems.append(_short(exc, 160))
    finally:
        client.close()
    # Today's first: those are the ones with hours left rather than a day.
    found.sort(key=lambda held: held["when"] != "today")
    return found, problems


#: The first pass after a restart looks back a week - "WHAT HAVE YOOU BEEN
#: MISSING" - and every pass after it the usual two days.
ADS_BACKFILL_HOURS = 7 * 24
#: Ads and setup cards this many days either side of today are the ones an
#: agent could already be on.
ADS_LOOK_BACK_DAYS = 14


def setups_onto_ads(
    config: Config, *, day=None, hours: int = SETUP_HOURS
) -> tuple[list[str], list[str]]:
    """Set-up agents nobody put on the Ads checklists. (added, problems).

    "why didnt you add this to nicole's checklist on ads card?" Cody Sturma's
    card had no launch date, so it waited in Franklin (Admin) and never went
    on a checklist - and Therese set him up anyway and he went to Done. Once
    a setup is confirmed on the card, that agent is live work for Ads.

    Only an agent who is on no Ads card and no setup card at all. One filed
    the usual way is already on one of them - the setup card is linked onto
    Ads - and gets nothing more. One launching after today is left to the
    filing, which puts them on their own day's setup card, and so is one
    still in In Que or Today, which the filing is about to pick up.

    Onto today's Ads card, or the next working day's at the weekend, on the
    checklists that are already there. Safe to run twice: the scan finds what
    the last run added.
    """
    from .. import agents, dailyops, tagged, trello

    day = day or board_day(config)
    client = open_trello(config)
    added, problems = [], []
    try:
        lists = client.board_lists(config.secrets.trello_board_id)
        every = _cards_of(client, config.secrets.trello_board_id, lists)
        filing = {
            str(bl.get("id") or "")
            for bl in (trello.find_list(lists, name) for name in (agents.IN_QUE, agents.TODAY))
            if bl is not None
        }
        fresh = _since(hours)
        waiting = []
        for card in every:
            title = str(card.get("name", ""))
            if not agents.is_agent_card(title) or str(card.get("dateLastActivity") or "") < fresh:
                continue
            short = trello.linked_card_id(str(card.get("shortUrl") or card.get("url") or ""))
            if not short:
                continue
            card_id = str(card.get("id") or "")
            try:
                said = client.card_comments(card_id)
                if not (agents.setup_said(said) or any(tagged.a_setup_confirmation(one) for one in said)):
                    continue
                detail = client.card_detail(card_id)
            except Exception as exc:  # one bad card must not lose the rest
                problems.append(f"{agents.agent_name(title)} — {_short(exc, 120)}")
                continue
            desc = str(detail.get("desc") or "")
            agent = agents.read_agent({**card, **detail}, text=desc, comments=tuple(said), today=day)
            launch = agent.launch if agent is not None else agents.find_launch(desc, today=day)
            if launch is not None and (launch > day or str(card.get("idList") or "") in filing):
                continue
            label = (agent.stated or agent.lead_type) if agent is not None else ""
            waiting.append((short, card, label or agents.setup_said(said)))
        if not waiting:
            return [], problems

        # Everything an agent could already be on: the Ads cards and the setup
        # cards of the last fortnight and the week ahead.
        near = []
        for card in every:
            title = str(card.get("name", ""))
            key = dailyops.parse_card_title(title)
            when = key[1] if key and key[0] == "ads" else (
                agents.setup_starts(title, day) if agents.is_setup_card(title) else None
            )
            if when is not None and -ADS_LOOK_BACK_DAYS <= (when - day).days <= dailyops.LOOK_AHEAD:
                near.append(card)
        on_already = {
            trello.linked_card_id(str(item.get("name") or ""))
            for card in near
            for held in client.card_checklists(str(card.get("id") or ""))
            for item in held.get("checkItems") or []
        }
        waiting = [one for one in waiting if one[0] not in on_already]
        if not waiting:
            return [], problems

        ads, when = None, day
        for ahead in range(dailyops.LOOK_AHEAD):
            when = day + timedelta(days=ahead)
            ads = dailyops.cards_covering(every, when).get("ads")
            if ads is not None or when.weekday() < dailyops.SATURDAY:
                break
        if ads is None:
            names = ", ".join(agents.agent_name(str(card.get("name", ""))) for _s, card, _l in waiting)
            return [], problems + [f"No Ads card dated {day:%m/%d/%y} for {names}"]
        held = {
            " ".join(str(c.get("name") or "").split()).casefold(): c
            for c in client.card_checklists(str(ads.get("id") or ""))
        }
        for _short_id, card, label in waiting:
            name = agents.agent_name(str(card.get("name", "")))
            item = agents.checklist_item(str(card.get("shortUrl") or card.get("url") or ""), label)
            for person in agents.ADS_PEOPLE:
                found = held.get(" ".join(person.split()).casefold())
                if found is None:
                    problems.append(f"{ads.get('name')} has no {person!r} checklist")
                    continue
                try:
                    client.add_check_item(str(found.get("id") or ""), item)
                except Exception as exc:
                    problems.append(f"{name} · {person} — {_short(exc, 120)}")
                    continue
                added.append(f"{name} → {ads.get('name')} · {person}")
    except Exception as exc:
        problems.append(_short(exc, 160))
    finally:
        client.close()
    return added, list(dict.fromkeys(problems))


def _since(hours: int) -> str:
    """An ISO timestamp `hours` ago, to compare against dateLastActivity."""
    from datetime import timezone

    return (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _list_named(lists, list_id: str) -> str:
    for held in lists:
        if str(held.get("id") or "") == list_id:
            return str(held.get("name") or "")
    return ""


def aged_to_archive(config: Config) -> tuple[list[dict], list[str]]:
    """The ticked cards in the aged-leads list. (cards, problems). Reads only.

    One list, by name, and nothing else on the board is fetched at all - not
    fetched and then filtered, which is the version that goes wrong when
    somebody edits the filter. A card in any other list is not reachable from
    here.

    Ticked, because the green tick is somebody saying that order is finished.
    One nobody has marked is still somebody's job, and a job that gets
    archived is a job that stops existing.

    A setup card in there is not an aged-leads order at all - it is a card
    filed in the wrong list, and six in the evening moves it to Done. If one
    is still sitting there at ten it is skipped rather than archived: a setup
    card is the record of who went live that day.
    """
    from .. import agents, dailyops, trello

    client = open_trello(config)
    try:
        lists = client.board_lists(config.secrets.trello_board_id)
        aged = trello.find_list(lists, dailyops.AGED_DONE)
        if aged is None:
            return [], [f"The board has no list called {dailyops.AGED_DONE!r}"]

        held = str(aged.get("id") or "")
        return [
            card for card in client.list_cards(held)
            # Belt and braces. The cards came from that list's own endpoint, so
            # this cannot be false - and if Trello ever hands back something
            # else, it is not getting archived on my watch.
            if str(card.get("idList") or held) == held
            and card.get("dueComplete")
            and not agents.is_setup_card(str(card.get("name") or ""))
            # Nor a New Agent card filed in the wrong list - "NEW AGENT-
            # Bernardo Rico" sat in Aged Leads Order Done. It is the record of
            # their setup and belongs in Done, not in the archive.
            and not agents.is_agent_card(str(card.get("name") or ""))
        ], []
    finally:
        client.close()


def archive_aged(config: Config) -> tuple[list[str], list[str]]:
    """Archive them. (titles archived, problems).

    Reads through `aged_to_archive` so what gets shown and what gets archived
    cannot drift. Archived, not deleted - Trello keeps the card and it can be
    brought back, which is the difference between a bad evening and a bad
    week.
    """
    cards, problems = aged_to_archive(config)
    if problems or not cards:
        return [], problems

    client = open_trello(config)
    archived = []
    try:
        for card in cards:
            try:
                client.archive_card(str(card.get("id") or ""))
            except Exception as exc:
                problems.append(f"{card.get('name')} — {_short(exc, 160)}")
                continue
            archived.append(str(card.get("name") or ""))
    finally:
        client.close()
    return archived, problems


def unmarked_agents(
    config: Config, *, day=None, ahead: bool = True
) -> tuple[list[dict], list[str]]:
    """Unticked New Agent cards in Done going live in the next day or so.

    (cards, problems). Each card carries a `when`: "today", "tomorrow", or the
    date when it is further out than that.

    How far ahead is `dailyops.chased_through` - tomorrow on most days, and
    Monday on a Friday, because nobody is at the board over a weekend and the
    Saturday-Monday setup card sets all three of those days up at once.

    `ahead` is what the afternoon check wants and a person asking about one day
    does not. "Unticked 09/03" is a question about the third, and answering it
    about the third and the fourth answers a question nobody asked - the more
    so when the fourth is today.

    The green circle on the card front is how the team says an agent is
    actually set up, and Trello carries it as `dueComplete` whether or not the
    card has a due date. A card in Done without it is work that looks finished
    from across the board and isn't.

    Narrowed to the two days that can still be acted on. Done holds sixty
    cards and most of them went live weeks ago - being reminded about all of
    them is the same as being reminded about none.

    Reads only. Nothing here ticks anything: that is somebody saying they did
    it, which is the whole value of the tick.
    """
    from .. import agents, dailyops, trello

    # The day the labels are said against. The afternoon check passes the day
    # it runs on; a person asking about one day (`ahead=False`) is asking on
    # the real one - Isaiah Wehner, live Monday, read "live today" on Tuesday.
    really = (day or board_day(config)) if ahead else board_day(config)
    day = day or really
    tomorrow = dailyops.next_day(day)
    through = dailyops.chased_through(day) if ahead else day
    client = open_trello(config)
    try:
        lists = client.board_lists(config.secrets.trello_board_id)
        done = trello.find_list(lists, agents.DONE)
        if done is None:
            return [], [f"The board has no list called {agents.DONE!r}"]

        found = []
        for card in client.list_cards(str(done.get("id") or "")):
            if not agents.is_agent_card(str(card.get("name", ""))):
                continue
            if card.get("dueComplete"):
                continue
            card_id = str(card.get("id") or "")
            launch = agents.find_launch(
                str(client.card_detail(card_id).get("desc") or ""), today=day
            )
            if launch is None:
                # Some cards say it only in a comment, and that is another
                # request each - so it is paid for only when the description
                # is silent.
                launch = agents.find_launch(
                    "\n".join(client.card_comments(card_id)), today=day
                )
            if launch is None or not (day <= launch <= through):
                continue
            found.append({
                **card,
                # Today and tomorrow by name; anything further out by its date.
                # "Live today" on an answer about the third, given on the
                # fourth, describes a day it wasn't asked about - and on a
                # Friday reaching to Monday there are three days that are
                # neither today nor tomorrow.
                # Against the real day, not the day asked about: Isaiah
                # Wehner, live Monday, read "live today" on Tuesday morning.
                "when": (
                    "today" if launch == really
                    else "tomorrow" if launch == dailyops.next_day(really)
                    else "yesterday" if dailyops.next_day(launch) == really
                    else f"{launch:%a %b %d}"
                ),
                "launch": launch,
            })
        # Soonest first: those are the ones that have run out of time.
        found.sort(key=lambda held: held["launch"])
        return found, []
    finally:
        client.close()


def ongoing_to_tick(
    config: Config, *, day=None, ahead: bool = True, found=None
) -> tuple[list[dict], list[str]]:
    """The unticked cards going live now whose comments say the order is ongoing.

    (cards, problems). Reads only - `tick_ongoing` does the writing.

    A top-up is not a setup. Therese writes "Justin Henry Najjar has an ongoing
    order that still need to get fulfilled. @nic0l3 kindly bump # of leads to
    his current setup", and there is no new setup to do: the leads go onto a
    drip that is already running. So nobody ever puts the green circle on the
    card, and it sits in Done being chased every afternoon for work that was
    finished before the card was copied.

    The words, not the idea - the same rule the checklists use. A comment about
    an agent already on the board is most of what gets written on these cards,
    and the ones asking to pause a drip or fix a schedule are real setups that
    somebody does have to tick by hand.

    Only the ones already narrowed to today and tomorrow, so the comments are
    read for a handful of cards rather than for the sixty in Done.

    `found` is the unticked list when the caller already has it, so asking
    "which of these are top-ups" costs the comments and not the board twice.
    """
    from .. import tagged

    problems: list = []
    if found is None:
        found, problems = unmarked_agents(config, day=day, ahead=ahead)
    if not found:
        return [], problems

    client = open_trello(config)
    try:
        theirs = []
        for card in found:
            card_id = str(card.get("id") or "")
            try:
                said = client.card_comments(card_id)
            except Exception as exc:
                problems.append(
                    f"Couldn't read the comments on {card.get('name')!r}: "
                    f"{_short(exc, 120)}"
                )
                continue
            note = next(
                (one for one in said if tagged.an_ongoing_order(one)), ""
            )
            if note:
                theirs.append({**card, "because": _short(" ".join(note.split()), 160)})
        return theirs, problems
    finally:
        client.close()


def tick_ongoing(config: Config, cards) -> tuple[list[str], list[str]]:
    """Put the green circle on these cards. (ticked, problems).

    Writes. Reversible - a tick comes off again - but it is four people's live
    board, so nothing calls this without somebody pressing the button first.
    """
    from .. import agents as rules

    if not cards:
        return [], []
    client = open_trello(config)
    try:
        ticked, problems = [], []
        for card in cards:
            title = str(card.get("name") or "")
            try:
                client.tick_card(str(card.get("id") or ""))
            except Exception as exc:
                problems.append(f"Couldn't tick {title!r}: {_short(exc, 120)}")
                continue
            ticked.append(rules.agent_name(title))
        return ticked, problems
    finally:
        client.close()


def make_setup_card(config: Config, *, day=None) -> tuple[str, list[str]]:
    """Make the setup card for the day after tomorrow. (title made, problems).

    Two days out rather than one, because a setup card is worked the day
    before its agents go live: the one made at six this morning is fetched
    into In Que at six this evening, walks into Today at nine tomorrow, and
    tomorrow is the day it gets worked.

    A weekend is one card - "Saturday-Monday 08/29-08/31" - so the agents
    going live on the Sunday are set up on the Friday along with the
    Saturday's. Which means the two mornings after that find the weekend card
    already covering their day and make nothing.

    Returns "" for the title when one already exists, which is the normal
    answer on any morning somebody got there first.
    """
    from .. import agents, trello

    day = day or board_day(config)
    wanted = day + timedelta(days=2)
    client = open_trello(config)
    try:
        lists = client.board_lists(config.secrets.trello_board_id)
        home = trello.find_list(lists, agents.AUTOMATION)
        if home is None:
            return "", [f"The board has no list called {agents.AUTOMATION!r}"]

        # Everywhere, not just the Automation Department: one made yesterday
        # has already been fetched into In Que, and a second one would split
        # the day's agents across two cards.
        every = [c for bl in lists for c in client.list_cards(str(bl.get("id") or ""))]
        if agents.find_setup_card(every, wanted) is not None:
            return "", []

        span = agents.weekend_span(wanted)
        title = agents.setup_title(wanted, span[1] if span else None)
        try:
            client.create_card(str(home.get("id") or ""), title)
        except Exception as exc:
            return "", [f"Couldn't make {title!r} — {_short(exc, 160)}"]
        return title, []
    finally:
        client.close()


def weekend_order_card(config: Config, *, day=None) -> tuple[str, list[str]]:
    """On a Friday, widen the weekend's Lead Order card to the setup card's days.

    (what changed, problems). "" when there was nothing to do, which is every
    day but Friday.

    The setup card runs Saturday to Monday - the agents going live on the
    Sunday are set up on the Friday with the Saturday's - and the card they
    are spread onto has to cover the same days. One Friday it did not: the
    only Lead Order card there was said "09/14/26", the spread asked for a
    card covering the Saturday, and there wasn't one. Eight agents and nowhere
    to write them.

    So the span comes off the setup card rather than being worked out here.
    Whatever days that card's agents go live on are the days its Lead Order
    card is retitled to cover, and the two cannot drift apart.

    It only ever retitles, and only a card that is already there:

    - a card already covering the setup card's days: nothing.
    - a card dated one day of them - the Saturday, or the Monday, or any of
      them: retitled to the whole span. This is the one that matters, because
      a card covering only part of the weekend does not fail. It takes the
      agents for the days it covers and silently loses the rest.
    - no Lead Order card anywhere near the weekend: said out loud, not made.
      A second card beside one that arrives later is worse than a message.
    - more than one across the weekend: left alone and said out loud. Which of
      them is the weekend's is not something to decide from here.

    Nothing on a card is touched but its title. The checklists, the members
    and whatever somebody has already written on it stay exactly as they are.
    """
    from .. import agents, dailyops

    day = day or board_day(config)
    saturday = day + timedelta(days=1)
    if saturday.weekday() != agents.SATURDAY:
        return "", []

    client = open_trello(config)
    try:
        lists = client.board_lists(config.secrets.trello_board_id)
        every = [c for bl in lists for c in client.list_cards(str(bl.get("id") or ""))]

        setup = agents.find_setup_card(every, saturday)
        if setup is None:
            return "", []
        last = agents.setup_ends(str(setup.get("name") or ""), saturday)
        if last is None or last <= saturday:
            return "", []
        span = [
            saturday + timedelta(days=step)
            for step in range((last - saturday).days + 1)
        ]
        title = (
            f"{dailyops.CARD_KINDS['lead_order']} "
            f"{saturday:%m/%d/%y}-{last:%m/%d/%y}"
        )

        found: dict[str, dict] = {}
        for when in span:
            card = dailyops.cards_covering(every, when).get("lead_order")
            if card is not None:
                found[str(card.get("id") or "")] = card

        if not found:
            return "", [
                f"No Lead Order card anywhere across {saturday:%m/%d}-{last:%m/%d}, "
                f"and `{setup.get('name')}` spreads onto one. Make it and I'll "
                "leave the title alone."
            ]
        if len(found) > 1:
            return "", [
                "More than one Lead Order card across "
                f"{saturday:%m/%d}-{last:%m/%d}, so I left them: "
                + "; ".join(str(one.get("name") or "") for one in found.values())
            ]

        one = next(iter(found.values()))
        was = str(one.get("name") or "")
        if set(span) <= set(dailyops.card_days(was)):
            return "", []
        try:
            client.rename_card(str(one.get("id") or ""), title)
        except Exception as exc:
            return "", [
                f"Couldn't retitle {was!r} to {title!r} — {_short(exc, 160)}"
            ]
        return f"{was} → {title}", []
    finally:
        client.close()


#: How long the board's automation gets to turn a tag into a checklist item
#: before RYTE looks, and adds it for whoever has none.
TAG_ITEM_WAIT = 90
_TAG_CHECKS: list = []
_TAG_CHECKS_LOCK = threading.Lock()
#: Written down as well, so an update in the minute and a half after a tag
#: doesn't lose the look - the checklist item was simply never made.
TAG_CHECKS_PATH = None
#: Times a look that broke is tried again before it's given up on - said.
TAG_CHECK_TRIES = 5


def _tag_checks_path():
    from ..state import _state_dir

    return TAG_CHECKS_PATH or (_state_dir() / "tag-checks.json")


def _tag_checks_save() -> None:
    try:
        where = _tag_checks_path()
        where.parent.mkdir(parents=True, exist_ok=True)
        spare = where.with_suffix(".tmp")
        spare.write_text(json.dumps(_TAG_CHECKS, indent=1), encoding="utf-8")
        spare.replace(where)
    except OSError:
        logging.getLogger("wilbyte.bot").warning("Couldn't write down the tag checks", exc_info=True)


def tag_checks_restore() -> int:
    """The looks still to do from before a restart, taken back up. How many."""
    try:
        held = json.loads(_tag_checks_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    with _TAG_CHECKS_LOCK:
        known = {(one.get("card_id"), one.get("comment_id")) for one in _TAG_CHECKS}
        back = [one for one in held if isinstance(one, dict) and one.get("card_id")
                and (one.get("card_id"), one.get("comment_id")) not in known]
        _TAG_CHECKS.extend(back)
    return len(back)


def tag_check_later(**check) -> None:
    """A comment that tagged people, to look at again once the automation's had
    its chance."""
    import time

    with _TAG_CHECKS_LOCK:
        _TAG_CHECKS.append({**check, "at": time.time()})
        _tag_checks_save()


def tag_check_again(check: dict) -> bool:
    """A look that broke - Trello having a moment - put back to try again.
    False once it's been tried enough."""
    import time

    tries = int(check.get("tries") or 0) + 1
    if tries >= TAG_CHECK_TRIES:
        return False
    with _TAG_CHECKS_LOCK:
        _TAG_CHECKS.append({**check, "tries": tries, "at": time.time()})
        _tag_checks_save()
    return True


def tag_checks_due(now: float | None = None) -> list[dict]:
    import time

    now = time.time() if now is None else now
    with _TAG_CHECKS_LOCK:
        due = [one for one in _TAG_CHECKS if now - one["at"] >= TAG_ITEM_WAIT]
        _TAG_CHECKS[:] = [one for one in _TAG_CHECKS if one not in due]
        if due:
            _tag_checks_save()
    return due


def tag_items_fill(config: Config, check: dict) -> list[str]:
    """Each person the comment tagged, on their checklist on that card: left
    alone when an item links the comment already (the automation's), added
    by RYTE when none does. Who was added.

    "are you still adding the trello tags on checklist" - the automation's
    item for Gannon Talken's screenshot was the comment's link and nothing
    else, so Nicole's list said nothing about pausing his leads at night. An
    item that's only the link gets the task put in front of it."""
    client = open_trello(config)
    added = []
    try:
        held = client.card_checklists(check["card_id"])
        by_name = {" ".join(str(one.get("name") or "").split()).casefold(): one for one in held}
        task = " ".join(str(check.get("text") or "").split())
        if not re.sub(r"https?://\S+", "", task).strip(" -–—:"):
            # Only a link and a screenshot: "See the comment" says more.
            task = ""
        for first in check.get("people") or []:
            who = " ".join(str(first).split()).casefold()
            # "Nicole" or "Nicole - Ads": the list with their name as a word.
            found = by_name.get(who) or next(
                (one for name, one in by_name.items() if who and re.search(rf"\b{re.escape(who)}\b", name)), None)
            if found is None:
                # Not on this card: their checklist on another of the day's
                # cards - "theerse is not on ads": tagged on Ads, her list is
                # on Ops. The comment's link takes her to it.
                elsewhere = _their_list_elsewhere(client, config, check["card_id"], who)
                if elsewhere is None:
                    logging.getLogger("wilbyte.bot").warning("Tagged %s, and no card today has a checklist of theirs",
                                                             first)
                    continue
                other_card, found = elsewhere
                if any(check["comment_id"] in str(item.get("name") or "") for item in found.get("checkItems") or []):
                    continue
                client.add_check_item(str(found.get("id") or ""),
                                      f"{task or 'See the comment'} {check.get('link') or ''}".strip())
                added.append(f"{first} (on {other_card})")
                continue
            linked = [item for item in found.get("checkItems") or []
                      if check["comment_id"] in str(item.get("name") or "")]
            if linked:
                for item in linked:
                    named = str(item.get("name") or "")
                    if task and item.get("id") and not re.sub(r"https?://\S+", "", named).strip(" -–—:"):
                        client.set_check_item(check["card_id"], str(item["id"]), name=f"{task} {named.strip()}")
                        added.append(first)
                continue
            line = f"{task or 'See the comment'} {check.get('link') or ''}".strip()
            client.add_check_item(str(found.get("id") or ""), line)
            added.append(first)
    finally:
        client.close()
    return added


def _their_list_elsewhere(client, config: Config, card_id: str, who: str):
    """(card title, checklist) for this person on another of the same day's
    cards - General, Ops, Ads, Lead Order in that order - or None."""
    from .. import dailyops

    try:
        every = client.board_cards(config.secrets.trello_board_id)
    except Exception:
        return None
    here = next((one for one in every if str(one.get("id") or "") == str(card_id)), None)
    key = dailyops.parse_card_title(str((here or {}).get("name") or ""))
    if not key:
        return None
    covering = dailyops.cards_covering(every, key[1])
    for kind in ("general", "ops", "ads", "lead_order"):
        other = covering.get(kind)
        if other is None or str(other.get("id") or "") == str(card_id):
            continue
        try:
            lists = client.card_checklists(str(other.get("id") or ""))
        except Exception:
            continue
        for one in lists:
            name = " ".join(str(one.get("name") or "").split()).casefold()
            if name == who or re.search(rf"\b{re.escape(who)}\b", name):
                return str(other.get("name") or ""), one
    return None


def _card_with_their_checklist(client, card: dict, covering: dict, handles, members) -> dict | None:
    """The day card that has a checklist for somebody tagged - `card` when it
    does, another of the day's cards when only that one does, None when
    none of them do. Ops, Ads, Lead Order, then General."""
    from .. import tagged

    full = {str(one.get("username") or ""): str(one.get("fullName") or "") for one in members or []}
    people = [full.get(one, "") for one in handles if full.get(one)]
    if not people:
        return None

    def theirs(one) -> bool:
        try:
            names = [str(c.get("name") or "") for c in client.card_checklists(str(one.get("id") or ""))]
        except Exception:
            return False
        return any(tagged.checklist_for(person, names) for person in people)

    if theirs(card):
        return card
    # General last: ads or ops work for somebody with no list there goes to
    # the work card that has their list, not back to General.
    for kind in ("ops", "ads", "lead_order", "general"):
        other = covering.get(kind)
        if other is not None and other.get("id") != card.get("id") and theirs(other):
            return other
    return None


def signed(text: str, asked_by: str = "") -> str:
    """A comment Ryte posts for somebody, signed with who asked - it posts as
    Franklin, and "theres no way thats me"."""
    who = " ".join(str(asked_by or "").split())
    return f"{text}\n\n— via Ryte, asked by {who} on Discord" if who else text


def comment_on_daily(
    config: Config, *, kind: str, day: date, text: str, tag=(), pictures=(), asked_by: str = "",
    guessed: bool = False,
) -> tuple[str, str, list[str]]:
    """Say something on one of the four dated cards. (title, url, problems).

    Wherever the card has got to. A comment is about the day's work, and the
    day's work is on that card whether it is still in Today or already in
    Quality Check.

    "and tag nicole" inside the comment is an instruction rather than
    something to say, and it is turned into a real @mention here - where the
    board's member list is already open - because posting the words tags
    nobody and the tag is the whole reason for saying it.
    """
    from .. import agents, dailyops

    client = open_trello(config)
    try:
        lists = client.board_lists(config.secrets.trello_board_id)
        every = [c for bl in lists for c in client.list_cards(str(bl.get("id") or ""))]
        card = dailyops.cards_covering(every, day).get(kind)
        if card is None:
            named = dailyops.CARD_KINDS.get(kind, kind)
            return "", "", [f"No {named} card dated {day:%m/%d/%y} anywhere on the board."]

        members = client.board_members(config.secrets.trello_board_id)
        known = dailyops.who_is_known(members)
        said, handles, strangers = dailyops.tag_asked(text, known)
        # "and tag Therese" on a line of its own, with the thing to say after
        # it - the words out, Therese tagged.
        said, more = dailyops.tag_phrase_out(said, known)
        for who in more:
            if who not in handles:
                handles.append(who)
        # Who was asked to be tagged - not everybody the words name: Faith's
        # "add to trello tag therese" read "(tell Kharyl)" off the screenshot,
        # and Kharyl's General checklist took it to General.
        asked = list(handles)
        # "if they mention any name, @ them on trello" - a teammate named in
        # what's said is tagged as well as one asked for.
        for who in dailyops.people_named(said, members):
            if who not in handles:
                handles.append(who)
        # Whoever was tagged on Discord in the message being put on the board,
        # tagged on Trello too - "@K2: @Franklin put on board tho let's
        # collate all logins" reaches Franklin's checklist only as a tag.
        for name in tag or ():
            who = known.get(" ".join(str(name).split()).casefold())
            if who and who not in asked:
                asked.append(who)
            if who and who not in handles:
                handles.append(who)
            elif not who:
                strangers.append(str(name))
        if handles:
            said = " ".join(f"@{one}" for one in handles) + ("\n" + said if said else "")
        if guessed and handles:
            covering = dailyops.cards_covering(every, day)
            full_names = {str(one.get("username") or ""): str(one.get("fullName") or "") for one in members or []}
            placing = asked or handles
            firsts = [(full_names.get(one) or one).split()[0].casefold() for one in placing]
            ops_people = {one.casefold() for one in agents.OPS_PEOPLE}
            if covering.get("ops") is not None and firsts and all(one in ops_people for one in firsts):
                # Only Ops people tagged - Therese: on Ops, once. Faith's
                # webhook for Aiden Adair went on General, and Therese got it
                # there and again on her Ops list.
                card, kind = covering["ops"], "ops"
            else:
                # Nobody named the card: the one whose checklists have who was
                # tagged. Faith's "add to trello tag therese" went on Ads, where
                # Therese has no checklist - her work is on Ops.
                card = _card_with_their_checklist(client, card, covering, placing, members) or card

        title = str(card.get("name") or "")
        body = said.split("\n", 1)[1] if handles and "\n" in said else ("" if handles else said)
        # "the image has to be attached on the same comment": uploaded to the
        # card - not as its cover - and shown in the comment itself.
        shown, missed = _pictures_onto(client, str(card.get("id") or ""), pictures)
        if shown:
            said = (said + "\n\n" + "\n".join(f"![{one.rsplit('/', 1)[-1]}]({one})" for one in shown)).strip()
        try:
            posted = client.add_comment(str(card.get("id") or ""), signed(said, asked_by)) or {}
        except Exception as exc:
            return title, "", [f"Couldn't comment on {title!r} — {_short(exc, 160)}"]
        logging.getLogger("wilbyte.bot").info("Commented on %s for %s: %s", title, asked_by or "?",
                                              " ".join(said.split())[:120])
        problems = [f"Nobody on the board is called {one} — not tagged." for one in strangers]
        # "why are you creating multiple checklist bro" / "still doing
        # thisssss" - the board's own automation turns a comment that tags
        # someone into a checklist item linking back to it, sometimes well
        # after a few seconds. On the card the comment is on, RYTE never adds
        # its own: that would always be the second.
        trello_does_it = bool(handles)
        if shown:
            problems.append(f"✅ {len(shown)} screenshot(s) in the comment")
        if missed:
            problems.append(f"{missed} screenshot(s) couldn't be put on the card")
        # "...and put it on their respective checklist": each person tagged
        # gets it on their own checklist on the card, by their first name.
        full_of = {str(one.get("username") or ""): " ".join(str(one.get("fullName") or "").split())
                   for one in members or []}
        ops = dailyops.cards_covering(every, day).get("ops") if kind != "ops" else None
        if trello_does_it and posted.get("id"):
            # "why didnt you add this to NICOLE'S CHECKKIST" - a comment with
            # only the tags and a screenshot got no item from the board's
            # automation. Looked at again in a minute and a half; whoever's
            # still without one gets it from RYTE.
            short = str(card.get("shortUrl") or card.get("url") or "")
            tag_check_later(card_id=str(card.get("id") or ""), comment_id=str(posted["id"]),
                            link=f"{short}#comment-{posted['id']}" if short else "",
                            people=[(full_of.get(who) or who).split()[0] for who in handles],
                            text=" ".join(str(body or "").split()))
        for who in handles:
            first = (full_of.get(who) or who).split()[0]
            if trello_does_it:
                problems.append(f"✅ On **{first}**'s checklist — Trello's automation, or me in a minute and a half "
                                f"if it doesn't")
            else:
                line = _onto_checklist(client, str(card.get("id") or ""), first, body or said)
                problems.append(f"✅ On **{first}**'s checklist" if line.startswith("✅")
                                else f"Not on {first}'s checklist: {line}")
            # "therese checklist should be on OPS Card also" - Ops people's
            # checklist on the day's Ops card gets it as well.
            if ops is not None and first.casefold() in {one.casefold() for one in agents.OPS_PEOPLE}:
                line = _onto_checklist(client, str(ops.get("id") or ""), first, body or said)
                problems.append(f"✅ On **{first}**'s checklist on {ops.get('name')}" if line.startswith("✅")
                                else f"Not on {first}'s Ops checklist: {line}")
        return title, str(card.get("url") or ""), problems
    finally:
        client.close()


#: What goes on the Ads card - "this is ads related so checklist is on ads".
_ADS_WORDS = re.compile(
    r"\b(?:caps?|capped|daily\s+cap|leads?\s+(?:per|a)\s+day|/\s*day|pause[ds]?|unpause|resume|turn\s+(?:on|off)|"
    r"sched(?:ule)?[sd]?|drip|ads?|campaigns?|budget|spend|pixel|meta|facebook|fb|widows?|vets?|fex|iul|trucker|"
    r"mtg|otp|states?|replacements?|replaced?|weekdays?|weekends?|mon\s*-\s*fri)\b"
    # Hours - "Alexis Lesme's sched From 10am- 6pm ideally" went on General.
    r"|\b\d{1,2}(?::\d\d)?\s*(?:am|pm)?\s*(?:-|–|to)\s*\d{1,2}(?::\d\d)?\s*(?:am|pm)\b"
    r"|\b(?:after|before|from|until|till)\s+\d{1,2}(?::\d\d)?\s*(?:am|pm)\b", re.IGNORECASE)
#: What goes on the Ops card.
_OPS_WORDS = re.compile(r"\b(?:ringy|crm|sid|token|twilio|a2p|sheet|zap|zapier|webhook|discord\s+channel|"
                        r"sms|text\s+line|number\s+port)\b", re.IGNORECASE)


def trello_ask_kind(config: Config, text: str, pictures=(), found: dict | None = None) -> tuple[str, str]:
    """Which day card an "add to trello" with no card named belongs on, and a
    one-line task for the checklists. ("general" | "ads" | "ops", the line).
    Ads work - caps, pausing, schedules, states, lead types, replacements -
    on Ads; CRM / Ringy / Twilio / sheets / zaps on Ops; the rest General.
    Read by Claude when there's a screenshot or the words don't say; by the
    words alone when Claude can't be asked."""
    said = " ".join(str(text or "").split())
    by_words = "ads" if _ADS_WORDS.search(said) else "ops" if _OPS_WORDS.search(said) else "general"
    if not pictures and len(re.sub(r"@\S+", "", said).split()) >= 3:
        return by_words, said
    try:
        from anthropic import Anthropic

        config.secrets.require("anthropic_api_key")
        client = Anthropic(api_key=config.secrets.anthropic_api_key)
        tool = {"name": "place", "description": "Where this task goes on the team's Trello board.",
                "input_schema": {"type": "object", "properties": {
                    "card": {"type": "string", "enum": ["general", "ads", "ops"], "description": (
                        "ads: lead delivery and ads work - caps, pausing or resuming leads, schedules / hours, "
                        "states, lead types, replacements, campaigns. ops: client setup systems - CRM, Ringy, "
                        "Twilio SID / token, A2P, Google Sheets, Zapier, Discord channels. general: anything else.")},
                    "task": {"type": "string", "description": (
                        "What the tagged teammate has to do, in one line, as an instruction. Read the whole "
                        "screenshot first: in a conversation with a client the task is what the CLIENT asks for "
                        "help with and what the team promised to do about it - not the last thing said, and not a "
                        "side topic like a payment unless the payment is the ask. E.g. a client saying 'We are "
                        "switching CRM once more... I need to update CRM and take care of lead flow... anyone "
                        "available to help today?' and the team replying 'I will have Therese reach out' is "
                        "'Matt is switching CRM - help him update the CRM and lead flow today (reach out in his "
                        "Discord channel)'. Names, numbers, states and deadlines kept.")},
                    "client": {"type": "string", "description": (
                        "The client (agent) this is about, full name, when the words or the screenshot say who - "
                        "the name at the top of a text conversation, or named in the message. Empty when it isn't "
                        "about one client.")}}, "required": ["card", "task", "client"]}}
        content = [{"type": "text", "text": f"Added to Trello by the team:\n{said or '(only a screenshot)'}"}]
        content += [{"type": "image", "source": {"type": "url", "url": url}} for url in list(pictures)[:3]]
        response = None
        # The careful model first: Matt Odierno's CRM ask was read by the
        # quick one as the $1,200 ad spend line above it. Each asked again
        # when Claude is only busy - two screenshots tagged a minute apart
        # left one of them on the card with no task at all.
        import time

        for model in dict.fromkeys((config.copy.model, ORDER_MODEL)):
            for wait in (0, *ORDER_READ_RETRY_WAITS):
                if wait:
                    time.sleep(wait)
                try:
                    response = client.messages.create(
                        model=model, max_tokens=400, tools=[tool], tool_choice={"type": "tool", "name": "place"},
                        messages=[{"role": "user", "content": content}])
                    break
                except Exception as exc:
                    logging.getLogger("wilbyte.bot").warning("Reading the Trello ask with %s failed", model,
                                                             exc_info=True)
                    if not claude_busy(exc):
                        break
            if response is not None:
                break
        if response is None:
            raise RuntimeError("Claude didn't answer")
        got = _tool_input(response, "place")
        if found is not None:
            found["client"] = " ".join(str(got.get("client") or "").split())
        card = got.get("card") if got.get("card") in ("general", "ads", "ops") else by_words
        task = " ".join(str(got.get("task") or said).split())
        if card == "general":
            # The task read off the screenshot saying ads or ops work - "lead
            # replacements", "her existing Google Sheet" - isn't General's.
            card = "ads" if _ADS_WORDS.search(task) else "ops" if _OPS_WORDS.search(task) else card
        return card, task
    except Exception:
        logging.getLogger("wilbyte.bot").warning("Couldn't ask where the Trello ask goes", exc_info=True)
        if found is not None and pictures:
            found["unread"] = True
        return by_words, said


def client_card_for(config: Config, text: str, client: str = "") -> dict | None:
    """The card of the client an "add to trello" is about - "add to his
    current order" was Karl Cook's, and went on the Ads card and nowhere near
    his. Their newest New Agent or Aged Leads card on the board, by the name
    Claude read off it, else by a client's whole name in the words. None when
    it names nobody with a card, or two people who have one."""
    from .. import agents, clearout

    said = " ".join(str(text or "").split()).casefold()
    board = open_trello(config)
    try:
        lists = board.board_lists(config.secrets.trello_board_id)
        cards = _cards_of(board, config.secrets.trello_board_id, lists)
    finally:
        board.close()
    theirs = []
    for card in cards:
        title = str(card.get("name") or "")
        if not (agents.is_agent_card(title) or re.match(r"\s*aged\s+leads?\b", title, re.IGNORECASE)):
            continue
        name = agents.agent_name(title) if agents.is_agent_card(title) else title.split("-", 1)[-1].strip()
        if len(name.split()) < 2:
            continue
        if (client and clearout.name_match(client, name) >= 2) or (
                not client and re.search(rf"(?<!\w){re.escape(name.casefold())}(?!\w)", said)):
            theirs.append((str(card.get("id") or ""), name, card))
    if len({name.casefold() for _id, name, _card in theirs}) != 1:
        return None
    # Newest first: a card's id starts with when it was made.
    return max(theirs, key=lambda one: one[0])[2]


def cards_made_since(config: Config, client: str, *, since, kind: str = "") -> list[dict]:
    """Every New Agent / Aged Leads card for this client made since then,
    newest first - "AGED LEAD - Joshua Holt" made by hand counts too."""
    from .. import agents, clearout

    board = open_trello(config)
    try:
        lists = board.board_lists(config.secrets.trello_board_id)
        cards = _cards_of(board, config.secrets.trello_board_id, lists)
    finally:
        board.close()
    stamp = int(since.timestamp())
    found = []
    for card in cards:
        title = str(card.get("name") or "")
        aged = bool(re.match(r"\s*aged\s+leads?\b", title, re.IGNORECASE))
        if not (agents.is_agent_card(title) or aged) or (kind and (kind == "aged") != aged):
            continue
        name = agents.agent_name(title) if agents.is_agent_card(title) else title.split("-", 1)[-1].strip()
        if clearout.name_match(client, name) < 2:
            continue
        try:
            made = int(str(card.get("id") or "")[:8], 16)
        except ValueError:
            continue
        if made >= stamp:
            found.append((made, card))
    return [card for _made, card in sorted(found, key=lambda one: one[0], reverse=True)]


def card_made_since(config: Config, client: str, *, since, kind: str = "") -> dict | None:
    """The newest New Agent or Aged Leads card for this client made since
    then - by anyone, archived or not - or None. A card's id starts with when
    it was made. Only the `kind` asked for, when one is: Abdullah Bahrani's
    aged payment was taken as made by his New Agent card from the same
    morning, and no Aged Leads card was ever made."""
    found = cards_made_since(config, client, since=since, kind=kind)
    return found[0] if found else None


def comment_on_client_card(config: Config, card: dict, text: str, pictures=(), asked_by: str = "") -> str:
    """The task on the client's own card, with its screenshots. Its link."""
    board = open_trello(config)
    try:
        shown, _missed = _pictures_onto(board, str(card.get("id") or ""), pictures)
        said = text + ("\n\n" + "\n".join(f"![{one.rsplit('/', 1)[-1]}]({one})" for one in shown) if shown else "")
        board.add_comment(str(card.get("id") or ""), signed(said.strip(), asked_by))
        logging.getLogger("wilbyte.bot").info("Commented on %s for %s", card.get("name"), asked_by or "?")
    finally:
        board.close()
    return str(card.get("shortUrl") or card.get("url") or "")


def _pictures_onto(client, card_id: str, urls) -> tuple[list[str], int]:
    """Discord pictures uploaded to a card, not as its cover. (their Trello
    links, how many couldn't be)."""
    import httpx

    shown, missed = [], 0
    for at, url in enumerate(urls or (), start=1):
        try:
            got = httpx.get(url, timeout=30, follow_redirects=True)
            got.raise_for_status()
            if len(got.content) > ORDER_SHOT_MOST_BYTES:
                raise ValueError("too big")
            mime = got.headers.get("content-type", "image/png").split(";")[0]
            name = shot_key(url).rsplit("/", 1)[-1] or f"screenshot-{at}.png"
            made = client.attach_file(card_id, name, got.content, mime, cover=False) or {}
            # The download link Trello shows a picture in a comment by -
            # built from the ids when the answer doesn't carry it.
            link = str(made.get("url") or "")
            if not link and made.get("id"):
                link = f"https://trello.com/1/cards/{made.get('idCard') or card_id}/attachments/{made['id']}/download/{name}"
            if link:
                shown.append(link)
            else:
                missed += 1
        except Exception:
            missed += 1
    return shown, missed


def spread_to_lead_order(
    config: Config, *, day=None
) -> tuple[list[str], list[dict], list[str]]:
    """Put today's setup-card agents onto today's Lead Order card.

    (added, conflicts, problems). A conflict is a line that went on, filed by
    the setup card's wording, onto a checklist the agent's own card disagrees
    with - see `agents.setup_conflict`. Said rather than refused: the line is
    where the setup card says it belongs, and which of the two is wrong is not
    something to decide from here.

    The setup card is filed by who does the setting up; the Lead Order card is
    filed by what was bought. An agent set up in advance only ever reaches the
    first - their New Agent card went to Done the day it was read, so nothing
    puts them on the second when their day finally arrives.

    `day` is the day the agents go live, which is the date on both the setup
    card and the Lead Order card. Running at half eight it defaults to
    tomorrow, because a setup card is worked the day before its agents launch:
    the one finished this evening is headed Saturday-Monday and its agents
    belong on Lead Order 08/29.

    Runs before the cards move to Done, because a card in Done is finished and
    writing onto one after the fact is how a line gets missed.
    """
    from .. import agents as rules, dailyops, noticed

    day = day or dailyops.next_day(board_day(config))
    client = open_trello(config)
    try:
        lists = client.board_lists(config.secrets.trello_board_id)
        every = [c for bl in lists for c in client.list_cards(str(bl.get("id") or ""))]

        setup = rules.find_setup_card(every, day)
        if setup is None:
            return [], [], [
                f"No setup card for the agents going live {day:%m/%d/%y} "
                "anywhere on the board."
            ]

        # A weekend setup card runs Saturday to Monday and its Lead Order card
        # is titled with the Saturday, so a Sunday falls back to the day the
        # setup card starts rather than reporting a card that isn't missing.
        order = dailyops.cards_covering(every, day).get("lead_order")
        starts = rules.setup_starts(str(setup.get("name") or ""), day)
        if order is None and starts is not None and starts != day:
            order = dailyops.cards_covering(every, starts).get("lead_order")
        if order is None:
            # What else is there, not just what isn't. "No Lead Order card
            # dated 09/12/26" is true and useless when the card is sitting in
            # In Que with the wrong year on it, or when the weekend one simply
            # has not been made yet and the answer is to make it.
            return [], [], [
                f"No Lead Order card dated {day:%m/%d/%y} anywhere on the board — "
                + dailyops.why_missing(every, "lead_order", day)
                + "."
            ]

        order_id = str(order.get("id") or "")
        held = client.card_checklists(order_id)
        spreads, problems = rules.plan_spread(
            client.card_checklists(str(setup.get("id") or "")), held
        )
        if not spreads:
            return [], [], problems

        # Read back between writes rather than trusting the plan: one agent's
        # line may have just made the checklist the next one is looking for.
        by_name = {
            " ".join(str(c.get("name") or "").split()).casefold(): str(c.get("id") or "")
            for c in held
        }
        # The checklist names as they are written on the card, for saying which
        # ones a lead type could have meant.
        by_name_shown = [" ".join(str(c.get("name") or "").split()) for c in held]
        named = _by_url(every)
        # The agent's own card by the URL its line links to, so what they
        # ordered can be read against what the setup card called it.
        cards = _cards_by_url(every)
        added: list[str] = []
        conflicts: list[dict] = []
        # Which agent each unplaceable line was about, so a line that could
        # not be read can be told apart from an agent who never got placed.
        # The setup card carries a line per person who worked on them, and
        # Therese writing "OTP IUL Plus" where Nicole writes "Ascend" is one
        # agent, done, with a second wording nobody can match.
        # (agent's card, what to say, the wording that could not be placed).
        # The wording is carried rather than cut back out of the sentence
        # afterwards: doing that took “MONDAY” doesn't match any checklist on
        # Lead Order 09/19/26-09/21/26, stripped the opening quote off it, and
        # printed the rest of the sentence as though it were the lead type.
        stuck: list[tuple[str, str, str]] = []
        placed: set[str] = set()
        # What each placed agent was placed as. A line that couldn't be placed
        # is only "the same order said another way" when it's the same kind of
        # lead: Joseph Antonucci's "30 Mortgage Protection" was left quietly
        # because his Vets, Blue Collar and Trucker lines had gone on.
        placed_as: dict[str, set] = {}
        # The days this Lead Order card is for. An agent whose own card names a
        # different day does not belong on it, whichever setup card they were
        # written on - "THIS ARE SATURDAY LIVE? YOU PUT THEM FRIDAY".
        covers = set(dailyops.card_days(str(order.get("name") or ""))) or {day}
        said_on: dict[str, str] = {}
        for spread in spreads:
            key = " ".join(spread.checklist.split()).casefold()
            who = named.get(spread.url, spread.url)

            # Read before the write, unlike the lead-type check: a line on the
            # wrong day is wrong the moment it is written, and the agent is not
            # lost by leaving it off - the spread for their own day picks them
            # up, and they are named here either way.
            goes = rules.launch_for(
                _card_said(client, cards.get(spread.url), said_on),
                spread.label, today=day,
            )
            # Only a day still to come. A date *earlier* than this card is
            # almost always the agent's old launch still written on their card
            # - a top-up, a re-order, a second product bought weeks later -
            # and refusing those would leave agents with no leads at all,
            # which is worse than the thing this is here to stop.
            if goes is not None and goes > max(covers):
                stuck.append((spread.url, (
                    f"{who} — their card says live {goes:%a %b %d} and "
                    f"{order.get('name')} doesn't reach it, so I left it off"
                ), spread.label))
                continue
            shown_as = spread.checklist
            if key not in by_name:
                # "mortgage" on the setup card could be Standard or Plus. Their
                # own card says which when it says volume (Standard) or high
                # intent (Plus) - "When there's a word volume, put on standard,
                # if high intent plus". Only those two words: "OTP" is on the
                # Standard checklists too, and isn't an answer.
                said_tier = rules.volume_or_intent(spread.label) or rules.volume_or_intent(
                    _card_said(client, cards.get(spread.url), said_on))
                narrowed = (rules.candidates(spread.label, list(by_name_shown), tier=said_tier)
                            if said_tier else [])
                if len(narrowed) == 1 and " ".join(narrowed[0].split()).casefold() in by_name:
                    shown_as = narrowed[0]
                    key = " ".join(shown_as.split()).casefold()
            if key not in by_name and rules.family_of(spread.label) is None \
                    and not rules.qualifiers_of(spread.label):
                # A tier with no lead type - Katrina Hall's "2.0" on the setup
                # card. Their own card's lead type says which leads, when it
                # names one kind; the setup line still says which tier.
                theirs = rules.find_lead_type(_card_said(client, cards.get(spread.url), said_on))
                kinds = rules.families_in(theirs)
                if len(kinds) == 1:
                    whole = f"{spread.label} {theirs}"
                    narrowed = rules.candidates(whole, list(by_name_shown),
                                                tier=rules.tier_of(spread.label) or rules.tier_of(theirs))
                    if len(narrowed) == 1 and " ".join(narrowed[0].split()).casefold() in by_name:
                        shown_as = narrowed[0]
                        key = " ".join(shown_as.split()).casefold()
            if key not in by_name and rules.family_of(spread.label):
                # Still Standard or Plus: what the setup was done as - Therese's
                # "TEXT VERIFIED MORTGAGE PROTECTION PLUS ON DISTRO HUB setup is
                # complete" on Hudson Greaves's card, whose order said only
                # "Fresh Mortgage Protection leads". The same kind of leads only.
                done_as = _setup_comment_said(client, cards.get(spread.url), said_on)
                if done_as and rules.family_of(done_as) == rules.family_of(spread.label) \
                        and rules.tier_of(done_as):
                    narrowed = rules.candidates(spread.label, list(by_name_shown), tier=rules.tier_of(done_as))
                    if len(narrowed) == 1 and " ".join(narrowed[0].split()).casefold() in by_name:
                        shown_as = narrowed[0]
                        key = " ".join(shown_as.split()).casefold()
            if key not in by_name:
                # Never invent one. The checklists on a Lead Order card are the
                # lead types that exist, put there by hand, and a spread that
                # makes its own leaves "22 OTP BC" sitting on the board as
                # though it were a product. An agent RYTE can't place is one
                # for somebody to place.
                # Why it didn't place, not just that it didn't. "Doesn't match
                # any checklist" is wrong when the truth is that it matches
                # two, and it sends somebody looking for a checklist that is
                # sitting right there.
                could_be = rules.candidates(
                    spread.label, list(by_name_shown), tier=rules.tier_of(spread.label)
                )
                if len(could_be) > 1:
                    _jot(
                        noticed, "ambiguous", spread.label,
                        detail="could be " + " or ".join(could_be),
                    )
                    stuck.append((spread.url,
                        f"{who} — “{spread.label}” could be "
                        + " or ".join(f"“{one}”" for one in could_be)
                        + f" on {order.get('name')}. It doesn't say which.",
                        spread.label))
                else:
                    stuck.append((spread.url,
                        f"{who} — “{spread.label}” doesn't match any checklist on "
                        f"{order.get('name')}"
                        + _teach_me(spread.label),
                        spread.label))
                continue
            try:
                client.add_check_item(
                    by_name[key], rules.checklist_item(spread.url, spread.label)
                )
            except Exception as exc:
                problems.append(f"{spread.label} — {_short(exc, 160)}")
                continue
            added.append(f"{named.get(spread.url, spread.label)} — {shown_as}")
            placed.add(spread.url)
            placed_as.setdefault(spread.url, set()).add(rules.family_of(spread.label))

            # Filed by the setup card's wording, which is sometimes thinner
            # than the agent's own card. Read after the write, not before: the
            # line belongs where the setup card says, and this only says so.
            clash = _their_card_disagrees(client, cards.get(spread.url), spread.label)
            if clash is not None:
                ordered, on_setup = clash
                # Which two wordings disagree is the thing worth counting, not
                # which agent it happened to: the same pair coming back every
                # week is a place the two are being written differently.
                _jot(
                    noticed, "conflict", f"{ordered} vs {on_setup}",
                    detail=f"their card says “{ordered}”, the setup card “{on_setup}”",
                )
                conflicts.append({
                    "agent": who,
                    "url": spread.url,
                    "checklist": shown_as,
                    "ordered": ordered,
                    "setup": on_setup,
                })
        if added:
            # Name both cards. The whole failure here was a wrong pairing, and
            # a count alone would have hidden it again.
            added.insert(0, f"**{setup.get('name')}** → **{order.get('name')}**")
        # A line nobody could place, for an agent who got placed off another
        # line, is not something that went wrong - the agent is on the card.
        # Said quietly, because the wording is still drifting and that is
        # worth knowing; but not under "something went wrong", which sends
        # somebody looking for an agent who is already there.
        def placed_already(url: str, label: str) -> bool:
            # A line with a count of its own and a kind of lead the agent
            # wasn't placed as is another order. "ASCEND" on Nicole's copy of
            # the line is the same order, said her way.
            kind = rules.family_of(label)
            other_order = bool(re.match(r"\s*\d", label or "")) and kind is not None \
                and kind not in placed_as.get(url, set())
            return url in placed and not other_order

        problems.extend(said for url, said, label in stuck if not placed_already(url, label))
        also = [label for url, _said, label in stuck if placed_already(url, label)]
        if also:
            problems.append(
                f"{len(also)} line(s) I couldn't read were for agents already "
                "placed from another line on the setup card, so I left them: "
                + "; ".join(f"“{one}”" for one in also)
            )
        return added, conflicts, problems
    finally:
        client.close()


def setup_balance(config: Config, *, day=None, fix: bool = False) -> list[dict]:
    """Each upcoming setup card's person checklists held up against each
    other - "the division of this card doesnt make sense, what did we miss?"
    (55/82 on Saturday-Monday: three copies of one list don't come to 82).
    Per card: {"name", "url", "counts": {person: lines}, "missing": [(person,
    agent, line)], "twice": [(person, agent)], "loose": [(person, line)],
    "added": [(person, agent)]}. With `fix`, a missing line is copied onto the
    checklist it's missing from, as another person has it."""
    from .. import agents as rules

    day = day or board_day(config)
    client = open_trello(config)
    found = []
    try:
        lists = client.board_lists(config.secrets.trello_board_id)
        every = _cards_of(client, config.secrets.trello_board_id, lists)
        named = _by_url(every)
        names_by_key = {rules.card_key(url): title for url, title in named.items()}
        for card in every:
            title = str(card.get("name") or "")
            if not rules.is_setup_card(title):
                continue
            starts = rules.setup_starts(title, day)
            if starts is None or not (-1 <= (starts - day).days <= 7):
                continue
            held = client.card_checklists(str(card.get("id") or ""))
            people = [one for one in held if " ".join(str(one.get("name") or "").split()).casefold()
                      in {person.casefold() for person in rules.SETUP_PEOPLE}]
            if not people:
                continue
            lines: dict[str, dict[str, str]] = {}
            report = {"name": title, "url": str(card.get("shortUrl") or card.get("url") or ""),
                      "counts": {}, "missing": [], "twice": [], "loose": [], "added": []}
            for checklist in people:
                person = str(checklist.get("name") or "")
                mine: dict[str, str] = {}
                items = checklist.get("checkItems") or []
                report["counts"][person] = len(items)
                for item in items:
                    text = str(item.get("name") or "")
                    url, _label = rules.split_item(text)
                    if not url:
                        report["loose"].append((person, text[:80]))
                        continue
                    key = rules.card_key(url)
                    if key in mine:
                        report["twice"].append((person, names_by_key.get(key) or url))
                    mine.setdefault(key, text)
                lines[person] = mine
            everyone = {key: text for mine in lines.values() for key, text in mine.items()}
            for checklist in people:
                person = str(checklist.get("name") or "")
                for key, text in everyone.items():
                    if key in lines[person]:
                        continue
                    who = names_by_key.get(key) or text[:60]
                    report["missing"].append((person, who, text))
                    if fix:
                        try:
                            client.add_check_item(str(checklist.get("id") or ""), text)
                            report["added"].append((person, who))
                        except Exception as exc:
                            report["loose"].append((person, f"couldn't add {who}: {_short(exc, 80)}"))
            # The day's Lead Order card against it - "but this is 22": 21
            # agents on the setup card, 22 lines on the Lead Order card.
            report.update(_lead_order_against(client, every, starts, set(everyone), names_by_key))
            found.append(report)
    finally:
        client.close()
    return found


def _lead_order_against(client, every, day, on_setup: set, names_by_key: dict) -> dict:
    """The Lead Order card covering `day`, held up against the setup card's
    agents: {"lead_order": {name, url, count}, "lo_extra": [agents on it and
    not the setup card], "lo_missing": [the other way], "lo_twice": [(agent,
    how many lines)]}. {} when there's no Lead Order card for the day."""
    from .. import agents as rules
    from .. import dailyops

    card = dailyops.cards_covering(every, day).get("lead_order")
    if card is None:
        return {}
    held = client.card_checklists(str(card.get("id") or ""))
    keys: dict[str, int] = {}
    count = 0
    for checklist in held or []:
        for item in checklist.get("checkItems") or []:
            count += 1
            url, _label = rules.split_item(str(item.get("name") or ""))
            if url:
                key = rules.card_key(url)
                keys[key] = keys.get(key, 0) + 1
    name = lambda key: names_by_key.get(key) or key
    return {
        "lead_order": {"name": str(card.get("name") or ""), "url": str(card.get("shortUrl") or card.get("url") or ""),
                       "count": count},
        "lo_extra": [name(key) for key in keys if key not in on_setup],
        "lo_missing": [name(key) for key in on_setup if key not in keys],
        "lo_twice": [(name(key), many) for key, many in keys.items() if many > 1],
    }


def agent_launch(config: Config, asked: str) -> tuple[list[dict], list[str]]:
    """When the agent somebody named goes live. (found, problems). Reads only.

    The whole board in one request, archived cards included: "when did Faith go
    live" is a fair question about somebody whose card was put away weeks ago,
    and a search that only sees the open lists answers it with silence.

    The description first and the comments only when it is silent, which is the
    same order everything else reads a launch in - a copied card carries the
    old one's comments, and the description is the half that gets rewritten.
    """
    from .. import agents as rules

    name = rules.who_asked_about(asked)
    if not name:
        return [], ["Who do you want the launch date for? Try `@RYTE when did Faith go live`."]

    day = board_day(config)
    client = open_trello(config)
    try:
        every = client.board_cards(config.secrets.trello_board_id, archived=True)
        cards = rules.named_that(name, every)
        if not cards:
            return [], [f"I can't find a New Agent card for “{name}” anywhere on the board."]

        found = []
        for card in cards:
            said = str(card.get("desc") or "")
            launch = rules.find_launch(said, today=day)
            if launch is None:
                launch = rules.find_launch(
                    "\n".join(client.card_comments(str(card.get("id") or ""))), today=day
                )
            found.append({
                **card,
                "agent": rules.agent_name(str(card.get("name") or "")),
                "launch": launch,
            })
        # Soonest first among the ones with a date; anything undated last,
        # because "I don't know" is not an answer to lead with.
        found.sort(key=lambda one: (one["launch"] is None, one["launch"] or day))
        return found, []
    finally:
        client.close()


def _which_tab(titles: list, when) -> tuple[str, str]:
    """(the tab to write in, a problem or ""). One tab a month, or just one.

    The tracker keeps a tab per month - Aug 2026, Sept 2026, Oct 2026 - so a
    chargeback belongs in the month it was logged. Writing every one into
    whichever tab came first would pile the year into August.

    A sheet with no month-shaped tab at all is a sheet kept some other way,
    and its first tab is the answer. A sheet that clearly is kept by month but
    has none for this one is not guessed at: the row would be filed under the
    wrong month, which is worse than not filed, because not filed gets
    noticed.
    """
    from .. import rebuttal as rules_doc

    if not titles:
        return "", "The tracker has no tabs."
    found = rules_doc.monthly_tab(titles, when)
    if found:
        return found, ""
    monthly = [one for one in titles if rules_doc.looks_monthly(one)]
    if not monthly:
        return titles[0], ""
    return "", (
        f"The tracker is kept by month and has no tab for {when:%b %Y}. "
        f"It has: {', '.join(titles)}. Make that month's tab and try again."
    )


def _make_month_tab(writing, sheet: str, titles: list, when) -> tuple[str, str]:
    """Make this month's tab by copying the newest one. (tab, what was done).

    The tracker runs out of tabs. Somebody made Aug through Dec 2026 by hand
    in one sitting, and on the first of January the tracker has no tab for the
    month and a chargeback has nowhere to go - "once its 2027 ryte will make
    them".

    A duplicate of the newest month rather than a fresh tab, so it arrives
    with the columns, the widths, the Arial 12, the date formats and the
    Win/Refunded dropdowns already on it. Then the list's own columns are
    emptied from row 2 down.

    Only the list's columns. The month tab has a second table beside it - the
    deductions-by-closer summary from column G - and what belongs in January's
    copy of that is the sheet owner's business, not a guess made at two in the
    morning by whatever wrote the first chargeback of the year. It comes
    across exactly as it was, and the message says so.
    """
    from .. import rebuttal as rules_doc

    like = rules_doc.latest_month(titles)
    if not like:
        return "", ""
    from_id = next(
        (one.get("sheetId") for one in writing.tabs(sheet)
         if str(one.get("title") or "") == like),
        None,
    )
    if from_id is None:
        return "", ""

    title = rules_doc.month_tab_name(when, titles)
    made = writing.copy_tab(sheet, int(from_id), title)
    if made is None:
        # Somebody made it between the read and now, which is the outcome
        # wanted either way.
        return title, ""

    wide = len(_the_list_columns(
        [str(one) for one in (writing.rows(sheet, f"'{title}'!1:1") or [[]])[0]]
    ))
    if wide:
        writing.clear(sheet, f"'{title}'!A2:{_column_letter(wide)}")
    return title, (
        f"Made **{title}** by copying **{like}** — there was no tab for this "
        f"month. Its list is empty; anything beside the list came across as "
        f"it was, so give that a look."
    )


def tracker_headings(
    config: Config, *, day=None
) -> tuple[list[str], str, list[str], list[str]]:
    """The tracker's own column headings. (headings, tab, problems, notes).

    The sheet is somebody's, with their columns in their order and their
    wording, so the row is built against what is actually there.

    Writes in one case only: a sheet kept by month that has run out of months
    gets the next one made, by copying the last. Said out loud when it
    happens - a tab appearing in somebody's spreadsheet with nobody told is
    the kind of help that gets a bot turned off.
    """
    from .. import gsheets

    sheet = (getattr(config.secrets, "tracker_sheet_id", "") or "").strip()
    if not sheet:
        return [], "", [], []
    sheet = gsheets.sheet_id_in(sheet) or sheet
    when = day or board_day(config)
    try:
        with gsheets.SheetsClient(gsheets.credentials(config.secrets)) as reading:
            # `tabs` already hands back the properties, so reaching into them
            # again found nothing and fell back to a tab called "Sheet1" that
            # does not exist: "Unable to parse range: 'Sheet1'!1:1".
            titles = [str(one.get("title") or "") for one in reading.tabs(sheet)]
            tab, trouble = _which_tab(titles, when)
            note = ""
            if trouble:
                tab, note = _make_month_tab(reading, sheet, titles, when)
                if not tab:
                    return [], "", [trouble], []
            rows = reading.rows(sheet, f"'{tab}'!1:1")
    except Exception as exc:
        return [], "", [f"Couldn't read the tracker: {_short(exc, 200)}"], []

    headings = _the_list_columns([str(one) for one in (rows[0] if rows else [])])
    if not headings:
        return [], tab, [
            "The tracker's first row is empty, so there are no columns to fill."
        ], [note] if note else []
    return headings, tab, [], [note] if note else []


def _the_list_columns(row: list[str]) -> list[str]:
    """The list's own headings, stopping at the first empty column.

    The tracker's month tab holds two tables side by side: the disputes
    themselves in columns A-E, and a deductions-by-closer summary from column
    G. Reading the whole of row 1 made a seven-wide row out of the list's five
    headings, an empty column F and the summary's title - which showed up in
    the preview as a column called "(unnamed)" and one called "Sep 2026
    chargebacks - deductions by closer", both of them always blank.
    """
    found = []
    for one in row:
        if not one.strip():
            break
        found.append(one.strip())
    return found


def _a_free_row(reading, sheet: str, tab: str, wide: int) -> tuple[str, int]:
    """(the range of the first free row under the list, which row it is).

    Read rather than assumed: `put` overwrites, and the one thing that must
    not happen to somebody's tracker is a dispute written over another one.
    Google trims trailing empty rows off a read, so the row after the last one
    it hands back is the first free row of the list - and only the list, since
    the range stops at the list's own last column.
    """
    last = _column_letter(wide)
    used = reading.rows(sheet, f"'{tab}'!A:{last}")
    at = len(used) + 1
    return f"'{tab}'!A{at}:{last}{at}", at


def _column_letter(how_many: int) -> str:
    """The letter of the nth column, counting from 1. 1 is A, 27 is AA."""
    name = ""
    while how_many > 0:
        how_many, over = divmod(how_many - 1, 26)
        name = chr(ord("A") + over) + name
    return name


def already_tracked(config: Config, tab: str, name: str, wide: int, amount: str = "") -> int:
    """The row this dispute is already on in that tab, or 0. Reads only.

    By the whole name in any cell of the list - "Meilee Reddy" and "Meilee
    reddy" are one person, "Meilee" alone is not asked about - and the
    amount, when there is one. Meilee Reddy disputed two charges from the
    same day, $279.45 and $181.12: one person, two rows, and the second was
    turned away as the first.
    """
    from .. import gsheets

    sheet = (getattr(config.secrets, "tracker_sheet_id", "") or "").strip()
    wanted = " ".join(str(name or "").split()).casefold()
    if not sheet or not wanted:
        return 0
    sheet = gsheets.sheet_id_in(sheet) or sheet
    with gsheets.SheetsClient(gsheets.credentials(config.secrets)) as reading:
        rows = reading.rows(sheet, f"'{tab}'!A:{_column_letter(wide)}")
    cents = _cents_in(amount)
    for at, row in enumerate(rows[1:], start=2):
        if not any(" ".join(str(cell).split()).casefold() == wanted for cell in row):
            continue
        if cents is None or any(_cents_in(str(cell)) == cents for cell in row):
            return at
    return 0


def _cents_in(said: str) -> int | None:
    """"$1,035.00" -> 103500. None when it isn't an amount."""
    found = re.fullmatch(r"\s*\$?\s*([\d,]+)(?:\.(\d{1,2}))?\s*", str(said or ""))
    if not found:
        return None
    return int(found.group(1).replace(",", "") or 0) * 100 + int((found.group(2) or "0").ljust(2, "0"))


def track_chargeback(config: Config, tab: str, row: list) -> tuple[str, list[str]]:
    """Write one row into the tracker's list. (where it landed, problems).

    Into the first free row under the list, never over one - the column that
    gets filled in weeks later is the outcome, and that is not RYTE's to touch.

    Not `append`. Appending hands Google a tab and lets it decide where the row
    belongs, and the month tab has two tables on it: the disputes in columns
    A-E and a deductions-by-closer summary from column G. Google picked the
    summary and wrote Juliana Hernandez underneath it, in the summary's
    columns, where every heading above her row meant something else.
    """
    from .. import gsheets

    sheet = (getattr(config.secrets, "tracker_sheet_id", "") or "").strip()
    if not sheet:
        return "", ["No TRACKER_SHEET_ID in .env."]
    sheet = gsheets.sheet_id_in(sheet) or sheet
    try:
        with gsheets.SheetsClient(gsheets.credentials(config.secrets)) as writing:
            where, at = _a_free_row(writing, sheet, tab, len(row))
            writing.put(sheet, where, [[str(one) for one in row]])
            # The look of the sheet is the sheet owner's, not Google's.
            tab_id = next(
                (one.get("sheetId") for one in writing.tabs(sheet)
                 if str(one.get("title") or "") == tab),
                None,
            )
            if tab_id is not None:
                writing.match_row_above(sheet, int(tab_id), at, len(row))
    except Exception as exc:
        return "", [f"Couldn't write to the tracker: {_short(exc, 200)}"]
    return f"{tab}, row {at}", []


def who_to_blacklist(config: Config, asked: str) -> tuple[list[dict], str, list[str]]:
    """Contacts in GHL that look like the person named. (found, tag, problems).

    Reads only. `blacklist_them` does the writing.

    Everything that matches comes back, never just the first. A blacklist tag
    on the wrong contact is a paying client who stops getting leads and is
    never told why, and the only thing standing between that and a typed name
    is somebody reading the list before pressing the button.
    """
    from .. import ghl, rebuttal as rules_doc

    said = " ".join((asked or "").split())
    if not said:
        return [], "", [
            "Who? `@RYTE blacklist Juliana Hernandez`, or their email address."
        ]
    tag = (getattr(config.secrets, "ghl_blacklist_tag", "") or "blacklisted").strip()
    if not (config.secrets.ghl_api_token and config.secrets.ghl_location_id):
        return [], tag, ["GHL isn't set up: GHL_API_TOKEN or GHL_LOCATION_ID missing."]

    looks_like_email = "@" in said
    try:
        with ghl.GHLClient(
            config.secrets.ghl_api_token, config.secrets.ghl_location_id
        ) as asking:
            found = asking.find_contacts(
                email=said if looks_like_email else "",
                name="" if looks_like_email else said,
            )
    except ghl.GHLError as exc:
        return [], tag, [f"Couldn't read GHL: {_short(exc, 200)}"]
    except Exception as exc:
        return [], tag, [f"Couldn't read GHL: {_short(exc, 200)}"]
    return found, tag, []


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_NAMED = re.compile(
    r"\bname\s*:\s*([^\n@:]+?)\s*(?=(?:customer\s+)?e-?mail\b|phone\b|\n|$)",
    re.IGNORECASE,
)


def blacklist_asks(asked: str) -> list[tuple[str, str]]:
    """The people one message asks to blacklist: [(name as given, email)].

    "blacklist these people" with a "Customer Name: / Customer Email:" pair
    for each of them - the block a dispute arrives as. One person per email
    address, named by the last "Name:" between it and the email before it.
    Read the same whether the lines survived or the message came through as
    one line. No email at all is one person, by name, the way it always
    worked.
    """
    said = str(asked or "")
    found, after = [], 0
    for email in _EMAIL.finditer(said):
        names = _NAMED.findall(said[after:email.start()])
        found.append((names[-1].strip() if names else "", email.group(0).rstrip(".")))
        after = email.end()
    seen, unique = set(), []
    for name, email in found:
        if email.casefold() not in seen:
            seen.add(email.casefold())
            unique.append((name, email))
    return unique


def who_to_blacklist_each(config: Config, asks) -> tuple[list[dict], str, list[str]]:
    """Each person looked up by their email. ([{name, email, found}], tag, problems).

    By the email alone: it is the one thing that means that person, and a
    name typed beside it is shown next to what GHL calls them, not matched on.
    """
    from .. import ghl

    tag = (getattr(config.secrets, "ghl_blacklist_tag", "") or "blacklisted").strip()
    if not (config.secrets.ghl_api_token and config.secrets.ghl_location_id):
        return [], tag, ["GHL isn't set up: GHL_API_TOKEN or GHL_LOCATION_ID missing."]
    looked = []
    try:
        with ghl.GHLClient(
            config.secrets.ghl_api_token, config.secrets.ghl_location_id
        ) as asking:
            for name, email in asks:
                looked.append({
                    "name": name, "email": email,
                    "found": asking.find_contacts(email=email),
                })
    except Exception as exc:
        return [], tag, [f"Couldn't read GHL: {_short(exc, 200)}"]
    return looked, tag, []


def blacklist_them(config: Config, contacts, tag: str) -> tuple[list[str], list[str]]:
    """Tag these contacts. (who was tagged, problems).

    Writes. Reversible - the tag comes off from the contact's own page - but
    it is a live CRM that workflows read, so nothing calls this without
    somebody having seen the list first.
    """
    from .. import ghl

    if not contacts:
        return [], []
    done, problems = [], []
    try:
        with ghl.GHLClient(
            config.secrets.ghl_api_token, config.secrets.ghl_location_id
        ) as writing:
            for one in contacts:
                who = _contact_name(one)
                try:
                    writing.add_tags(str(one.get("id") or ""), [tag])
                except Exception as exc:
                    # The token can read contacts and not change them. Said
                    # once, in words, and the rest not tried - every one after
                    # it would be refused the same way.
                    if "not authorized for this scope" in str(exc):
                        problems.append(
                            "GHL's token isn't allowed to edit contacts, so nobody was "
                            "tagged. In GHL: Settings → Private Integrations → RYTE's "
                            "integration → add the **Edit Contacts** scope "
                            "(contacts.write). If GHL gives you a new token, put it in "
                            ".env as GHL_API_TOKEN and restart me."
                        )
                        break
                    problems.append(f"Couldn't tag {who}: {_short(exc, 140)}")
                    continue
                done.append(who)
    except Exception as exc:
        problems.append(f"Couldn't reach GHL: {_short(exc, 140)}")
    return done, problems


def _contact_name(one: dict) -> str:
    """A contact said the way a person would say it."""
    whole = " ".join(
        f"{one.get('firstName') or ''} {one.get('lastName') or ''}".split()
    ) or str(one.get("contactName") or "").strip()
    email = str(one.get("email") or "").strip()
    if whole and email:
        return f"{whole} ({email})"
    return whole or email or str(one.get("id") or "somebody")


#: How far back orders are read, so an earlier order keeps the contract it
#: was signed for and a reorder doesn't borrow it.
CONTRACT_ORDERS_DAYS = 60


def contract_check(config: Config, *, today=None, sync: bool = True) -> tuple[list, list[str]]:
    """Every order going live today or tomorrow, with its contract or none.

    ([Paired], problems). Reads only - the board in one request, and PandaDoc
    for what was signed since last time.
    """
    from .. import agents as rules
    from .. import contracts as matching
    from .. import pandadoc

    key = (getattr(config.secrets, "pandadoc_api_key", "") or "").strip()
    if not key:
        return [], ["PANDADOC_API_KEY isn't in .env."]
    today = today or board_day(config)
    try:
        data = pandadoc.sync(key) if sync else pandadoc.load()
    except pandadoc.PandaDocError as exc:
        return [], [str(exc)]
    # Nothing said while PandaDoc is still being read. A contract whose
    # signers aren't read yet can't be matched to anybody, so every order
    # looked unsigned - twenty-six of them in one message, the morning the
    # read stopped failing and started catching up.
    waiting = sum(1 for one in (data.get("docs") or {}).values() if "emails" not in one)
    if waiting:
        return [], [f"Still reading PandaDoc's contracts ({waiting} to go) - not flagging until they're all in."]
    client = open_trello(config)
    try:
        every = client.board_cards(config.secrets.trello_board_id, archived=True)
    except Exception as exc:
        return [], [f"Couldn't read the board: {_short(exc, 140)}"]
    finally:
        client.close()

    since = datetime.now(timezone.utc) - timedelta(days=CONTRACT_ORDERS_DAYS)
    orders = []
    for card in every:
        title = str(card.get("name") or "")
        made = rules.made_at(str(card.get("id") or ""))
        # New agents only. "aged leads we dont have contracts, only fresh/new
        # agents" - an aged-lead order has nothing to sign.
        if not rules.is_agent_card(title) or made is None or made < since:
            continue
        desc = str(card.get("desc") or "")
        # "no spark dont need contracts" - a Spark buyer signs nothing.
        if rules.from_spark(desc):
            continue
        orders.append(matching.Order(
            card_id=str(card.get("id") or ""), name=rules.agent_name(title),
            email=matching.card_email(desc), made=made,
            url=str(card.get("shortUrl") or card.get("url") or ""),
            launch=rules.find_launch(desc, today=today), closed=bool(card.get("closed")),
        ))
    paired = matching.pair(orders, pandadoc.contracts(data))
    soon = {today, today + timedelta(days=1)}
    return [
        one for one in paired.values()
        if not one.order.closed and one.order.launch in soon
    ], []


def going_live_on(config: Config, day) -> tuple[list[dict], int, list[str]]:
    """Every agent whose card says they go live on this day.

    (found, how many cards carry no date, problems). Reads only.

    "how many people are going live on thursday" is a question about the whole
    board rather than about one person, and it used to fall through to the
    help text.

    One request. The launch date is read out of the description, which is
    where the form writes it, and the comments are not opened - doing that for
    sixty cards is sixty requests to answer one question. A card whose launch
    only ever got said in a comment is therefore not found, so the count of
    cards with no date in them comes back too: a number that is wrong and
    looks right is worse than a number with a caveat on it.
    """
    from .. import agents as rules

    client = open_trello(config)
    try:
        every = client.board_cards(config.secrets.trello_board_id, archived=True)
    except Exception as exc:
        return [], 0, [f"Couldn't read the board: {_short(exc, 140)}"]
    finally:
        client.close()

    # The setup card for that day first. "Agent Setup Going Live Thursday
    # 09/17" is the list Therese keeps by hand, with the lead counts written
    # next to each name, and it is what the team actually works from - reading
    # sixty agent cards to rebuild it is guessing at something somebody has
    # already written down.
    #
    # This year's, newest first. The titles carry no year, so last October's
    # "Going Live Tuesday 10/06" sits archived on the same board and matches
    # just as well - and taken first, it answered "nobody" for a card with
    # fourteen agents on it.
    def made(one):
        return rules.made_at(str(one.get("id") or ""))

    recent = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) - timedelta(days=60)
    setups = sorted(
        (one for one in every
         if rules.is_setup_card(str(one.get("name") or ""))
         and rules.setup_covers(str(one.get("name") or ""), day)
         # An id with no date in it is not a card known to be old.
         and (made(one) is None or made(one) >= recent)),
        key=lambda one: (not one.get("closed"), made(one) or _NEVER), reverse=True,
    )
    for setup in setups:
        found, undated, problems = _off_the_setup_card(config, setup, every)
        if found or problems:
            return found, undated, problems

    found, undated = [], 0
    for card in every:
        title = str(card.get("name") or "")
        if not rules.is_client_card(title):
            continue
        said = str(card.get("desc") or "")
        launch = rules.find_launch(said, today=day)
        if launch is None:
            undated += 1
            continue
        if launch != day:
            continue
        found.append({
            **card,
            "agent": rules.agent_name(title),
            "leads": rules.find_lead_type(said),
            "ticked": bool(card.get("dueComplete")),
        })
    found.sort(key=lambda one: one["agent"].casefold())
    return found, undated, []


def setup_lines_open(config: Config, days) -> tuple[list[dict], list[str]]:
    """The lines on these days' setup cards nobody has checked off yet.

    ([{"agent", "url", "setup_by", "day"}], problems). Not the same claim as
    the green tick on the agent's own card: a checked line is the person who
    set them up saying it's done - Eric Karas and Marcus Uhrich sat unchecked
    on Tuesday's card while "unticked" only looked at the cards themselves.
    Reads only. Each line once, however many days its card covers.
    """
    found, problems, seen = [], [], set()
    for day in days:
        agents_on, _undated, trouble = going_live_on(config, day)
        problems += [one for one in trouble if one not in problems]
        for one in agents_on:
            # Only lines off a setup card say whether they were checked off.
            if "setup_done" not in one or one["setup_done"]:
                continue
            key = (str(one.get("agent") or "").casefold(), str(one.get("setup_by") or ""))
            if key in seen:
                continue
            seen.add(key)
            found.append({
                "agent": one.get("agent"), "url": str(one.get("url") or one.get("shortUrl") or ""),
                "setup_by": str(one.get("setup_by") or ""), "day": day,
            })
    return found, problems


def _off_the_setup_card(config: Config, setup: dict, every: list) -> tuple:
    """Who is on the setup card's checklists, in the order somebody wrote them.

    Each item is a link to the agent's own card followed by what they bought -
    "New Agent - Steve Dass  25 Text Verified Veteran Leads" - so the name and
    the leads are both already there and neither has to be inferred.

    Whether their card is ticked still comes off the card itself, because the
    checklist item being ticked means the setup was done and the green circle
    means somebody said so, and those are different claims.
    """
    from .. import agents as rules

    client = open_trello(config)
    try:
        checklists = client.card_checklists(str(setup.get("id") or ""))
    except Exception as exc:
        return [], 0, [f"Couldn't read the setup card: {_short(exc, 140)}"]
    finally:
        client.close()

    by_url = {str(one.get("url") or ""): one for one in every}
    found = []
    for checklist in checklists:
        for item in checklist.get("checkItems") or []:
            said = str(item.get("name") or "")
            name, leads, url = rules.split_setup_item(said)
            card = by_url.get(url, {})
            # The linked card's own title when the line didn't carry a label.
            name = name or rules.agent_name(str(card.get("name") or ""))
            if not name:
                continue
            found.append({
                **card,
                "agent": name,
                "leads": leads,
                "ticked": bool(card.get("dueComplete")),
                "setup_by": str(checklist.get("name") or ""),
                "setup_done": str(item.get("state") or "") == "complete",
            })
    return found, 0, []


def cards_for_name(config: Config, name: str) -> tuple[list[dict], bool]:
    """Every client card on the board for a name, archived ones too - "did
    we create a card for Fransisco Pedemonte" was answered "I can't go
    rummaging through last week's board". ([{"name", "url", "list",
    "closed", "made"}], whether the match is only a close spelling): the
    name as typed first, else one spelled a letter or two differently -
    Payra had "Fransico", the question "Fransisco"."""
    from difflib import SequenceMatcher

    from .. import agents as rules

    client = open_trello(config)
    try:
        every = client.board_cards(config.secrets.trello_board_id, archived=True)
        try:
            lists = {str(one.get("id")): str(one.get("name") or "")
                     for one in client.board_lists(config.secrets.trello_board_id)}
        except Exception:
            lists = {}
        if not rules.named_that(name, every):
            # Archived cards the whole-board read leaves out.
            try:
                every = every + [one for one in client.search_cards(config.secrets.trello_board_id, name)
                                 if str(one.get("id")) not in {str(card.get("id")) for card in every}]
            except Exception:
                pass
        try:
            checklists = client.board_checklists(config.secrets.trello_board_id)
        except Exception:
            checklists = []
    finally:
        client.close()
    found, close = rules.named_that(name, every), False
    if not found:
        wanted = [word for word in re.findall(r"[a-z]+", name.casefold()) if len(word) >= 3]

        def near(word, theirs):
            return any(SequenceMatcher(None, word, one).ratio() >= 0.8 for one in theirs)

        for card in every:
            title = str(card.get("name") or "")
            if not rules.is_client_card(title):
                continue
            theirs = re.findall(r"[a-z]+", rules.agent_name(title).casefold())
            # Every word close to one of theirs, the last name included.
            if wanted and all(near(word, theirs) for word in wanted):
                found.append(card)
        close = bool(found)
    out = []
    for card in found:
        made = rules.made_at(str(card.get("id") or ""))
        out.append({"name": str(card.get("name") or ""), "url": str(card.get("shortUrl") or card.get("url") or ""),
                    "list": lists.get(str(card.get("idList") or ""), ""), "closed": bool(card.get("closed")),
                    "made": made, "on": filed_on(card, checklists, every)})
    out.sort(key=lambda one: one["made"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return out, close


def filed_on(card: dict, checklists: list[dict], cards: list[dict]) -> list[dict]:
    """The checklists an agent's card is filed on - "where did ryte file
    this": Therese looked for James Henning Jr, Morgan Rosenow and Alexander
    Manchester on yesterday's Ops list, and their cards were already in Done.
    An item linking the card, or naming the agent in full. [{"card", "list",
    "done"}]."""
    from .. import agents as rules

    link = re.search(r"trello\.com/c/([A-Za-z0-9]+)", str(card.get("shortUrl") or card.get("url") or ""))
    short = link.group(1) if link else ""
    agent = " ".join(rules.agent_name(str(card.get("name") or "")).split()).casefold()
    titles = {str(one.get("id")): str(one.get("name") or "") for one in cards or []}
    found = []
    for checklist in checklists or []:
        on = str(checklist.get("idCard") or "")
        if on == str(card.get("id")) or rules.is_client_card(titles.get(on, "")):
            continue
        for item in checklist.get("checkItems") or []:
            said = str(item.get("name") or "")
            if (short and short in said) or (len(agent.split()) >= 2 and agent in " ".join(said.split()).casefold()):
                found.append({"card": titles.get(on, "a card"), "list": str(checklist.get("name") or ""),
                              "done": item.get("state") == "complete"})
                break
    return found


def _cards_searched(client, board_id: str, name: str, every: list[dict]) -> tuple[list[dict], bool]:
    """(client cards for a name, whether only a close spelling) - Trello's
    search first, then every card's name spelled a letter or two off."""
    from difflib import SequenceMatcher

    from .. import agents as rules

    try:
        searched = client.search_cards(board_id, name)
    except Exception:
        searched = []
    found = [card for card in rules.named_that(name, searched)]
    if found:
        return found[:6], False
    wanted = [word for word in re.findall(r"[a-z]+", name.casefold()) if len(word) >= 3]
    if not wanted:
        return [], False

    def near(word, theirs):
        return any(SequenceMatcher(None, word, one).ratio() >= 0.8 for one in theirs)

    seen, close = set(), []
    for card in [*every, *searched]:
        title = str(card.get("name") or "")
        if str(card.get("id")) in seen or not rules.is_client_card(title):
            continue
        seen.add(str(card.get("id")))
        if all(near(word, re.findall(r"[a-z]+", rules.agent_name(title).casefold())) for word in wanted):
            close.append(card)
    return close[:6], bool(close)


def agent_sheet(config: Config, asked: str) -> tuple[list[dict], list[str]]:
    """The setup sheet for the agent somebody named. (found, problems). Reads only.

    The sheet is already on the card - Therese and Faith post it as "Sheet
    link:" when the setup is finished. Getting at it meant finding the card
    first, which is the part that costs a person a minute and RYTE a request.
    """
    from .. import agents as rules

    names = rules.who_wants_sheets(asked)
    if not names:
        return [], ["Whose sheet do you want? Try `@RYTE sheet for Faith`."]

    client = open_trello(config)
    try:
        every = client.board_cards(config.secrets.trello_board_id, archived=True)
        found, problems = [], []
        for name in names:
            cards = rules.named_that(name, every)
            if not cards:
                # Not in a card's title: named in one - an agency, a business,
                # the person a card is for - "Livia Rose" wasn't a title.
                words = [word for word in re.split(r"\W+", name.casefold()) if word]
                cards = [card for card in every if words and rules.is_client_card(str(card.get("name") or ""))
                         and all(re.search(rf"(?<!\w){re.escape(word)}(?!\w)", str(card.get("desc") or "").casefold())
                                 for word in words)][:6]
            close = []
            if not cards:
                # Trello's own search - archived cards the whole-board read
                # leaves out - then a name spelled a letter or two off.
                cards, close = _cards_searched(client, config.secrets.trello_board_id, name, every)
            if not cards:
                problems.append(f"I can't find a New Agent card for “{name}” anywhere on the board - not in a "
                                f"card's title, what's written on one, Trello's search, or a close spelling.")
                continue
            if close:
                problems.append(f"No card says “{name}” exactly - these are the closest: "
                                + ", ".join(rules.agent_name(str(one.get("name") or "")) for one in cards[:3]) + ".")
            theirs = []
            for card in cards:
                links = rules.sheet_links(rules.in_order_written(client.card_comments(str(card.get("id") or ""))))
                theirs.append({
                    **card,
                    "agent": rules.agent_name(str(card.get("name") or "")),
                    "sheets": links,
                })
            # The ones that have a sheet first: an agent set up twice has two,
            # and an agent not set up yet has none and is not the answer to
            # lead with.
            theirs.sort(key=lambda one: not one["sheets"])
            found += theirs
        # One name and nothing for it: said as before. Several, and the ones
        # found are given with the ones that weren't.
        if not found:
            return [], problems
        return found, problems
    finally:
        client.close()


def rebuttal_evidence(config: Config, dispute) -> "object":
    """What the board and the sheet remember about this customer.

    Reads only, and gathers nothing it cannot point at. A proof with no
    evidence behind it comes back empty and is left out of the document -
    writing around a gap is how a rebuttal gets taken apart.
    """
    from .. import agents as rules, gsheets, rebuttal as rules_doc

    found = rules_doc.Gathered()
    name = dispute.customer_name
    if not name:
        found.holes.append("No customer name, so I couldn't look anything up.")
        return found

    client = open_trello(config)
    try:
        every = client.board_cards(config.secrets.trello_board_id, archived=True)
        cards = rules.named_that(name, every) or _cards_by_contact(dispute, every)
    except Exception as exc:
        found.holes.append(f"Couldn't read the board: {_short(exc, 120)}")
        return found
    finally:
        client.close()
    # A client with several orders has a card for each: the one made nearest
    # the disputed charge is the order being disputed.
    paid = dispute.paid()
    if paid is not None and len(cards) > 1:
        def distance(card):
            made = rules.made_at(str(card.get("id") or ""))
            return abs((made.date() - paid).days) if made else 10**6
        cards = sorted(cards, key=distance)

    # No card is a hole in the document, not the end of gathering. The receipt
    # is in Gmail and has nothing to do with the board, and returning here
    # meant one missing card quietly took the paid invoice with it - Juliana
    # Hernandez's rebuttal asked Franklin to go and find an invoice RYTE could
    # already see, because her card was titled "AGED LEAD -" and got skipped.
    agent, said, body, detail = None, [], "", {}
    if not cards:
        found.holes.append(
            f"No card for “{name}” anywhere on the board, so the order "
            "details and the sheet had to be left out."
        )
    else:
        card = cards[0]
        client = open_trello(config)
        try:
            detail = client.card_detail(str(card.get("id") or ""))
            # The dated form. A rebuttal turns on when things happened, and
            # the day the sheet was handed over is on the comment that handed
            # it over - Nicole posted Juliana Hernandez's at 1:10 PM on the
            # 28th and said "delivered" five minutes later.
            notes = client.card_notes(str(card.get("id") or ""))
            said = [str(one.get("text") or "") for one in notes]
        except Exception as exc:
            found.holes.append(f"Couldn't read their card: {_short(exc, 120)}")
            detail, said, notes = {}, [], []
        finally:
            client.close()
        found.delivered_on = _when_delivered(notes, rules)
        # An aged-leads re-order has no contract to go looking for: those are
        # signed by new agents at setup, and a returning client ordering more
        # leads signs nothing.
        found.aged = rules.is_order_card(str(card.get("name") or ""))

    body = str(detail.get("desc") or "")
    if cards:
        agent = rules.read_agent(
            {**cards[0], **detail}, text=body, comments=tuple(said),
            # Their launch date is months back. Read against the day they were
            # charged, not today, or "Monday" reads as next Monday.
            today=dispute.paid() or date.today(),
        )
    ordered = []
    if agent is not None:
        if agent.stated or agent.lead_type:
            ordered.append(f"Package: {agent.stated or agent.lead_type}")
        if agent.launch:
            ordered.append(f"Launch date: {agent.launch:%B %d, %Y}")
    if body.strip():
        ordered.append("From their onboarding card:\n" + body.strip())
    found.invoice = "\n".join(ordered)

    # The setup confirmations. Therese and Faith post these as the work is
    # finished, with the date and the sheet on them - which is the delivery
    # being confirmed in writing, in the words of the person who did it.
    confirmations = [one for one in said if str(one).strip()]
    if confirmations:
        found.delivery = "\n\n---\n".join(confirmations[:6])

    links = rules.sheet_links(said)
    if not links and cards:
        found.holes.append(
            "No sheet link on their card, so the delivered leads had to be "
            "left out. Paste the sheet link into the command if you have it."
        )
    elif links:
        found.sheet, trouble = _read_lead_sheet(config, links[0], gsheets)
        if trouble:
            found.holes.append(trouble)
        else:
            # The one line that answers "did they get what they paid for":
            # ordered against delivered, counted rather than argued.
            proof = _proof_line(config, links[0][1], agent)
            if proof:
                found.sheet = f"Proof of delivery: {proof}\n" + found.sheet

    # Payra's own record of the charge, from the copy RYTE keeps through its
    # API: the payment, the invoice it paid and when that was sent to them.
    record, trouble = _payra_record(dispute)
    if record:
        found.invoice = (found.invoice + "\n\n" if found.invoice else "") + record
        # An aged-lead order says so on its invoice, card or no card - and
        # signs no contract, so none is asked for.
        if re.search(r"\baged\s+leads?\b", record, re.IGNORECASE):
            found.aged = True
    if trouble:
        found.holes.append(trouble)

    # And the payment confirmation, out of the inbox it was emailed to: the
    # email the customer was sent. The customer's own name is in the subject
    # line of it, which is what makes it findable.
    receipt, trouble = _payment_receipt(config, dispute)
    if receipt:
        found.invoice = (found.invoice + "\n\n" if found.invoice else "") + receipt
    elif trouble:
        found.holes.append(trouble)

    # And the signed contract: PandaDoc's own PDF for this order, found by the
    # customer's name and made theirs by the email on the dispute. The inbox
    # only when PandaDoc isn't set up.
    said, pdf, called, trouble = (
        ("", b"", "", "") if found.aged else _signed_contract(config, dispute)
    )
    if said:
        found.contract = said
    if pdf:
        found.contract_pdf, found.contract_name = pdf, called
    if trouble:
        found.holes.append(trouble)

    found.timeline = _timeline(dispute, agent, found)
    return found


def client_cards(name: str, cards: list[dict]) -> list[dict]:
    """The cards of the client with this name - by the whole name, not a
    word of it. "dylan_rankin-vet" and "Dylan Rankin" are the person Dylan
    Rankin; "Dylan" alone is whoever is called exactly that, not every Dylan.
    """
    from .. import agents as rules
    from .. import clearout

    person = clearout.person_in(name)
    if not person:
        return []
    return [
        one for one in cards or []
        if rules.is_client_card(str(one.get("name") or ""))
        and clearout.tidy(rules.agent_name(str(one.get("name") or ""))) == person
    ]


def different_people(cards: list[dict]) -> bool:
    """Whether cards under one name are two people, not one client's orders.

    A name can't tell two Maria Lopezes apart; the phone number on each card
    can. Two cards whose numbers have nothing in common are two people. A
    card with no number says nothing either way - repeat orders are copied
    from the last card, number and all.
    """
    from .. import smsreplies

    numbers = [
        smsreplies.phones_in(str(one.get("desc") or "")) for one in cards
    ]
    numbers = [one for one in numbers if one]
    return any(
        not (numbers[at] & numbers[other])
        for at in range(len(numbers)) for other in range(at + 1, len(numbers))
    )


def sheet_for_agent(config: Config, name: str) -> tuple[str, list[str]]:
    """The delivered-leads sheet for one agent, off their own card. (link, problems).

    The same place the rebuttal finds it: the setup confirmations Therese and
    Faith leave on the New Agent card, each carrying the sheet for that round.
    The last one is the one that counts - a setup gets redone and every round
    leaves its own link.

    Archived cards too. An agent being closed down is one whose card went to
    Done months ago.
    """
    from .. import agents as rules

    if not (name or "").strip():
        return "", ["No name to look up."]

    from .. import clearout

    client = open_trello(config)
    try:
        every = client.board_cards(config.secrets.trello_board_id, archived=True)
        cards = client_cards(name, every)
        if not cards:
            return "", [f"No New Agent card for “{name}” anywhere on the board."]
        if different_people(cards):
            # Two clients with the one name - the cards say so by carrying
            # different phone numbers. Their sheet in ALL CLIENTS is the
            # record of what they had; somebody else's is worse than none,
            # and the channel's own link is looked for next.
            return "", [
                f"More than one client on the board is called “{name}” (their "
                "cards have different phone numbers) - not guessing which."
            ]
        # A repeat client has a card per order; the newest is the round they
        # were on when they stopped.
        newest = max(cards, key=lambda one: (
            rules.is_agent_card(str(one.get("name") or "")),
            rules.made_at(str(one.get("id") or "")) or _NEVER,
        ))
        said = rules.in_order_written(client.card_comments(str(newest.get("id") or "")))
    except Exception as exc:
        return "", [f"Couldn't read their card: {_short(exc, 140)}"]
    finally:
        client.close()

    links = rules.sheet_links(said)
    if not links:
        return "", [f"No sheet link in the comments on “{name}”'s card."]
    return links[-1][1], []


def _proof_line(config: Config, link: str, agent) -> str:
    """"Ordered 25, delivered 27 by 10/02/2026." for a rebuttal, or ""."""
    from .. import delivery

    counted, trouble = _count_sheet(config, link)
    if counted is None:
        return ""
    ordered = (agent.stated or agent.lead_type) if agent is not None else ""
    return delivery.for_a_dispute(delivery.Delivery(
        agent="", ordered=ordered, many=delivery.how_many(ordered),
        launch=agent.launch if agent is not None else None, counted=counted,
    ))


def _count_sheet(config: Config, link: str):
    """(what the lead sheet holds, a problem or ""). The first tab - where
    the leads land - read to two thousand rows."""
    from .. import delivery, gsheets

    sheet_id = gsheets.sheet_id_in(link)
    if not sheet_id:
        return None, f"Couldn't read a sheet id out of {link}"
    try:
        with gsheets.SheetsClient(gsheets.credentials(config.secrets)) as client:
            tabs = client.tabs(sheet_id)
            if not tabs:
                return None, "That sheet has no tabs I can read."
            title = str((tabs[0] or {}).get("title") or "Sheet1")
            rows = client.rows(sheet_id, f"'{title}'!A1:Z2000")
    except Exception as exc:
        return None, f"Couldn't read the lead sheet: {_short(exc, 120)}"
    return delivery.count_rows(rows, tab=title), ""


def _delivery_of(config: Config, card: dict, client, every: list):
    """One order card against its sheet. `client` is an open Trello client;
    `every` is the board, for a sheet only an earlier card of theirs has."""
    from .. import agents as rules
    from .. import delivery

    title = str(card.get("name") or "")
    desc = str(card.get("desc") or "")
    made = rules.made_at(str(card.get("id") or ""))
    one = delivery.Delivery(
        agent=rules.agent_name(title), card_id=str(card.get("id") or ""),
        card_url=str(card.get("shortUrl") or card.get("url") or ""),
    )
    try:
        said = client.card_comments(one.card_id)
    except Exception as exc:
        one.problems.append(f"Couldn't read their card: {_short(exc, 120)}")
        said = []
    day = made.date() if made else board_day(config)
    agent = rules.read_agent(card, text=desc, comments=tuple(said), today=day)
    if agent is not None:
        one.ordered, one.launch = agent.stated or agent.lead_type, agent.launch
    else:
        named = rules.named_lead_types(desc)
        one.ordered = named[-1] if named else ""
        one.launch = rules.find_launch(desc, today=day)
    one.many = delivery.how_many(one.ordered)

    links = rules.sheet_links(rules.in_order_written(said))
    if not links:
        # A re-order often fills the sheet from the first order, and the link
        # is only on that card.
        for older in sorted(client_cards(one.agent, every),
                            key=lambda c: rules.made_at(str(c.get("id") or "")) or _NEVER, reverse=True):
            if str(older.get("id") or "") == one.card_id:
                continue
            try:
                links = rules.sheet_links(rules.in_order_written(
                    client.card_comments(str(older.get("id") or ""))))
            except Exception:
                links = []
            if links:
                break
    if not links:
        one.problems.append("No sheet link on their card.")
        return one
    one.sheet = links[-1][1]
    one.counted, trouble = _count_sheet(config, one.sheet)
    if trouble:
        one.problems.append(trouble)
    return one


def hub_check(config: Config, agents: list) -> list:
    """Each hub agent's fulfilled order against their sheet. [hub.Checked].
    Reads only: the sheets."""
    from .. import hub

    import time

    done = []
    for at, agent in enumerate(agents):
        if at:
            # Paced: two reads a sheet, and Google allows about a minute's
            # worth of sixty.
            time.sleep(HUB_SHEET_PAUSE)
        one = hub.Checked(agent=agent)
        if not agent.sheet_url:
            one.problems.append("the hub has no sheet link for them")
        else:
            one.counted, trouble = _count_lead_tab(config, agent.sheet_url, agent.lead_type)
            if trouble:
                one.problems.append(trouble)
        done.append(one)
    return done


#: Seconds between one lead sheet and the next in a hub check.
HUB_SHEET_PAUSE = 1.0

#: Tabs looked at on one lead sheet.
MOST_TABS = 8

#: What a lead type or a tab title can be named for. A tab named for a type
#: this order isn't - "IUL" for a mortgage order - is another order's.
_FAMILIES = {
    "vet": {"vet", "vets", "veteran", "veterans"},
    "iul": {"iul"},
    "trucker": {"trucker", "truckers"},
    "mortgage": {"mortgage", "mtg", "mp"},
    "fex": {"fex", "final", "expense"},
    "spanish": {"spanish"},
    "widow": {"widow", "widows"},
}


def _families(said: str) -> set:
    words = set(re.findall(r"[a-z]+", (said or "").casefold()))
    return {family for family, names in _FAMILIES.items() if words & names}


def _count_lead_tab(config: Config, link: str, lead_type: str = ""):
    """(the leads on the tab this order's are on, a problem or "").

    Not just the first tab. Joevanny Astorga's came back "the sheet has 0"
    with 22 leads on it - on a tab that isn't the first. Every tab is
    counted; of those with leads, the one named for this lead type ("Trucker",
    "Mortgage"), else the one with the most.
    """
    from .. import delivery, gsheets

    sheet_id = gsheets.sheet_id_in(link)
    if not sheet_id:
        return None, f"Couldn't read a sheet id out of {link}"
    try:
        with gsheets.SheetsClient(gsheets.credentials(config.secrets)) as client:
            titles = [title for title in (str((one or {}).get("title") or "")
                                          for one in client.tabs(sheet_id)) if title][:MOST_TABS]
            # Every tab in one read: 51 sheets a tab at a time ran into
            # Google's limit ("rate-limiting us (429)") two sheets from the end.
            read = client.rows_of(sheet_id, [f"'{title}'!A1:Z2000" for title in titles])
            counts = [delivery.count_rows(rows, tab=title) for title, rows in zip(titles, read)]
    except Exception as exc:
        return None, f"Couldn't read the lead sheet: {_short(exc, 120)}"
    if not counts:
        return None, "That sheet has no tabs I can read."
    ordered = _families(lead_type)
    # Abraham's mortgage order is not the 19 leads on his IUL tab.
    theirs = [one for one in counts
              if one.rows and not (ordered and _families(one.tab) and not (_families(one.tab) & ordered))]
    if not theirs:
        return counts[0] if len(counts) == 1 else delivery.Counted(tab=""), ""
    return max(theirs, key=lambda one: (len(_families(one.tab) & ordered), one.rows)), ""


def delivered_for(config: Config, name: str) -> tuple[list, list[str]]:
    """Proof of delivery for one agent: each of their orders, newest first,
    against its sheet. ([Delivery], problems). Reads only."""
    from .. import agents as rules

    if not (name or "").strip():
        return [], ["Whose? `@RYTE delivered Jay Rodriguez`."]
    client = open_trello(config)
    try:
        every = client.board_cards(config.secrets.trello_board_id, archived=True)
        cards = client_cards(name, every)
        if not cards:
            return [], [f"No card for “{name}” anywhere on the board."]
        if different_people(cards):
            return [], [f"More than one client on the board is called “{name}” (their cards "
                        "have different phone numbers) - not guessing which."]
        cards.sort(key=lambda c: rules.made_at(str(c.get("id") or "")) or _NEVER, reverse=True)
        return [_delivery_of(config, card, client, every) for card in cards[:3]], []
    except Exception as exc:
        return [], [f"Couldn't read the board: {_short(exc, 140)}"]
    finally:
        client.close()


def delivery_check(config: Config, *, today=None) -> tuple[list, list[str]]:
    """Every order that should be all in by now and isn't. ([Delivery], problems).

    Orders live between a week and three weeks ago with a count on the card.
    The launch date off the description, as `going_live_on` reads it, so only
    the cards in that window are opened.
    """
    from .. import agents as rules
    from .. import delivery

    today = today or board_day(config)
    client = open_trello(config)
    try:
        every = client.board_cards(config.secrets.trello_board_id, archived=True)
        short = []
        for card in every:
            title = str(card.get("name") or "")
            made = rules.made_at(str(card.get("id") or ""))
            if not rules.is_client_card(title) or made is None:
                continue
            if (today - made.date()).days > delivery.LOOK_BACK_DAYS + 30:
                continue
            launch = rules.find_launch(str(card.get("desc") or ""), today=made.date())
            if launch is None:
                continue
            age = (today - launch).days
            if not (delivery.DONE_WITHIN_DAYS <= age <= delivery.LOOK_BACK_DAYS):
                continue
            one = _delivery_of(config, card, client, every)
            if one.many is not None and one.short_by() and one.due(today):
                short.append(one)
        return short, []
    except Exception as exc:
        return [], [f"Couldn't read the board: {_short(exc, 140)}"]
    finally:
        client.close()


def paid_not_set_up(config: Config, *, now=None) -> tuple[list, list[str]]:
    """Payments from the last three days with no card on the board for them,
    or a card that still says nothing about when they go live.
    ([paidsetup.Unset], problems). Reads only."""
    from .. import agents as rules
    from .. import paidsetup, payraapi

    now = now or datetime.now(timezone.utc)
    data = payraapi.load()
    if not data.get("invoices"):
        return [], ["I haven't read any Payra invoices yet - is PAYRA_API_TOKEN in .env?"]
    payments = paidsetup.recent_payments(data, now=now)
    if not payments:
        return [], []
    client = open_trello(config)
    try:
        every = client.board_cards(config.secrets.trello_board_id, archived=True)
        found = []
        for paid in payments:
            cards = paidsetup.their_cards(paid, every)
            if not cards:
                found.append(paidsetup.Unset(paid=paid, why="no card"))
                continue
            card = paidsetup.own_card(paid, cards)
            if card is None:
                # Only an earlier order's card - a plan renewing, most likely.
                continue
            if not rules.is_agent_card(str(card.get("name") or "")):
                # An aged-lead order goes out as it is - no launch date to
                # set. Only a new agent's setup waits on one.
                continue
            made = rules.made_at(str(card.get("id") or ""))
            day = made.date() if made else paid.paid_at.date()
            if rules.find_launch(str(card.get("desc") or ""), today=day) is not None:
                continue
            # The launch date gets added in a comment about as often as not.
            said = client.card_comments(str(card.get("id") or ""))
            if rules.find_launch("\n".join(said), today=day) is not None:
                continue
            found.append(paidsetup.Unset(
                paid=paid, why="no launch date",
                card_url=str(card.get("shortUrl") or card.get("url") or ""),
                card_title=str(card.get("name") or ""),
            ))
        return found, []
    except Exception as exc:
        return [], [f"Couldn't read the board: {_short(exc, 140)}"]
    finally:
        client.close()


def itinerary_calendar(config: Config, link: str, *, today=None):
    """A trip itinerary sheet as a calendar file. (ics text, the sheet's name,
    itinerary.Read, problems). Reads only."""
    from .. import gsheets, itinerary

    sheet_id = gsheets.sheet_id_in(link)
    if not sheet_id:
        return "", "", None, ["That doesn't look like a Google Sheets link."]
    today = today or board_day(config)
    try:
        with gsheets.SheetsClient(gsheets.credentials(config.secrets)) as client:
            name = client.title(sheet_id) or "Itinerary"
            gid = gsheets.gid_in(link)
            tab = client.tab_named(sheet_id, gid) if gid else ""
            if tab:
                tried = [tab]
            else:
                # No tab in the link: the first one is "Airbnbs", and the
                # schedule is on "Cebu Itinerary". Every tab, the ones named
                # like a schedule first, and the first that reads as one.
                titles = [str(one.get("title") or "") for one in client.tabs(sheet_id)]
                tried = sorted(titles, key=lambda title: not re.search(
                    r"itinerar|schedule|agenda|plan", title, re.IGNORECASE))
            found, used = None, ""
            for title in tried:
                rows = client.rows(sheet_id, f"'{title}'!A1:Z600")
                read = itinerary.read(rows, today=today)
                if read.events:
                    found, used = read, title
                    break
                found = found or read
    except Exception as exc:
        return "", "", None, [f"Couldn't read the sheet: {_short(exc, 160)} - is it shared with RYTE's Google account?"]
    if found is None or not found.events:
        return "", name, found, [
            "No tab with a Time and an Activity column and rows under them to put in a calendar"
            + (f" (looked at: {', '.join(tried)})." if tried else ".")]
    return itinerary.ics(found.events, name=name), (f"{name} · {used}" if used and len(tried) > 1 else name), found, []


def sheet_titles(config: Config, links) -> dict:
    """{link: the spreadsheet's name} for these links. Read only; a sheet that
    can't be opened is left out rather than guessed at."""
    from .. import gsheets

    found = {}
    try:
        with gsheets.SheetsClient(gsheets.credentials(config.secrets)) as client:
            for link in links:
                sheet_id = gsheets.sheet_id_in(link)
                if not sheet_id:
                    continue
                try:
                    found[link] = client.title(sheet_id)
                except Exception:
                    continue
    except Exception:
        return {}
    return found


def _collection_key(row) -> tuple:
    """A Ryte Collection row less its date - the same channel and sheet on
    another day is the same row."""
    return tuple(
        " ".join(str(cell).split()).casefold() for cell in row or []
        if str(cell).strip() and not re.fullmatch(r"\d{1,4}[/-]\d{1,2}[/-]\d{1,4}", str(cell).strip())
    )


def collect_client(config: Config, plan, *, when) -> tuple[str, list[str]]:
    """Put one line in the Ryte Collection tab. (what tab it went on, problems).

    That tab by name, not by whichever one happened to be open when the link
    was copied. It is named for RYTE on purpose - "that way we know if hes the
    one deleting stuff" - so a row in it is a row RYTE put there, and the gid
    in a pasted link is exactly the sort of thing that quietly points at the
    Masterlist instead.

    Laid out against the tab's own headings. Appended, never written over: the
    tab is a record of every client who has been closed down, and nothing here
    has any business changing a line already in it.
    """
    from .. import clearout, gsheets

    link = (getattr(config.secrets, "clients_sheet_link", "") or "").strip()
    sheet_id = gsheets.sheet_id_in(link)
    if not sheet_id:
        return "", [
            "CLIENTS_SHEET_LINK in .env isn't a spreadsheet link, so there is "
            "nowhere to collect them."
        ]

    try:
        with gsheets.SheetsClient(gsheets.credentials(config.secrets)) as client:
            titles = [
                str((one.get("properties") or one).get("title") or "")
                for one in client.tabs(sheet_id)
            ]
            wanted = clearout.COLLECTION_TAB.casefold()
            tab = next(
                (one for one in titles if " ".join(one.split()).casefold() == wanted),
                "",
            )
            # Whatever the link pointed at, when there is no tab by that name.
            # A spreadsheet somebody set up before the tab was named should
            # still collect rather than refuse.
            tab = tab or client.tab_named(sheet_id, gsheets.gid_in(link))
            if not tab:
                return "", [
                    f"No tab called “{clearout.COLLECTION_TAB}” in that "
                    "spreadsheet, and the gid in CLIENTS_SHEET_LINK doesn't "
                    "match one either. It has: "
                    + ", ".join(f"“{one}”" for one in titles[:10])
                ]
            headings = [
                str(one).strip() for one in (
                    client.rows(sheet_id, f"'{tab}'!1:1") or [[]]
                )[0]
            ]
            # A row for each sheet kept - "or just save both sheet".
            from dataclasses import replace

            rows = ([clearout.row_for(plan, when=when, headings=headings)]
                    + [clearout.row_for(replace(plan, sheet=other, also_sheets=[]), when=when, headings=headings)
                       for other in getattr(plan, "also_sheets", []) or []])
            # Not twice: a clear-out stopped after its rows went in and run
            # again wrote conor_mckenna-vet's two rows a second time. A row
            # whose channel and sheet are already there, whatever the day,
            # isn't written again.
            have = {_collection_key(one) for one in client.rows(sheet_id, f"'{tab}'!A:Z") or []}
            rows = [one for one in rows if _collection_key(one) not in have]
            if not rows:
                return tab, []
            where = client.append(sheet_id, tab, rows)
            # A row appended under a heading row arrives wearing the heading's
            # clothes - bold and centred - and the sheet link wraps to six
            # lines, which takes the whole row with it.
            tab_id = next(
                (
                    (one.get("properties") or one).get("sheetId")
                    for one in client.tabs(sheet_id)
                    if str((one.get("properties") or one).get("title") or "") == tab
                ),
                None,
            )
            if tab_id is not None:
                _restyle(client, sheet_id, tab_id, where, bold=False, wrap="CLIP")
    except Exception as exc:
        return "", [f"Couldn't write the sheet: {_short(exc, 140)}"]
    return tab, []


#: A channel isn't quiet if anything - a message, a delivered lead - landed in
#: it within this long. "check himself if the channel is really 1 month older".
QUIET_AT_LEAST_DAYS = 35


def quiet_check(newest, now) -> tuple[bool, str]:
    """Whether the channel's newest message, of any kind, is old enough."""
    if newest is None:
        return True, "Quiet: nothing in the channel at all"
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    days = (now - newest).days
    if days < QUIET_AT_LEAST_DAYS:
        return False, (f"Not quiet: the newest message in the channel is from "
                       f"{newest:%b %-d} — {days} day(s) ago, under a month")
    return True, f"Quiet: the newest message is from {newest:%b %-d, %Y} ({days} days ago)"


#: An order card this recent means a client still buying, whatever their
#: channel says - their channel is not RYTE's to delete on its own.
#: The same 35 days as quiet: 24 of 36 were left at 60, each with a card
#: from five or six weeks back - "can you check why isnt he deleting this".
RECENT_ORDER_DAYS = 35

#: The board, read once for a run of ten rather than once a channel.
_BOARD_READ: dict = {}
_BOARD_READ_LOCK = threading.Lock()
BOARD_READ_FOR = timedelta(minutes=10)


def _board_for_a_while(config: Config) -> list:
    now = datetime.now(timezone.utc)
    with _BOARD_READ_LOCK:
        held = _BOARD_READ.get("cards")
        if held is not None and now - _BOARD_READ["at"] < BOARD_READ_FOR:
            return held
    client = open_trello(config)
    try:
        cards = client.board_cards(config.secrets.trello_board_id, archived=True)
    finally:
        client.close()
    with _BOARD_READ_LOCK:
        _BOARD_READ.update(cards=cards, at=now)
    return cards


def recent_order(config: Config, channel_name: str, *, now=None) -> tuple[bool, str]:
    """(fine to go, said): no order card for this person in the last two
    months. Not being able to read the board is not fine - a channel is
    only deleted on its own when RYTE knows they've stopped ordering."""
    from .. import agents as rules
    from .. import clearout

    now = now or datetime.now(timezone.utc)
    try:
        cards = _board_for_a_while(config)
    except Exception as exc:
        return False, f"Couldn't read the board to see if they still order: {_short(exc, 140)}"
    since = now - timedelta(days=RECENT_ORDER_DAYS)
    found = []
    for card in cards or []:
        title = str(card.get("name") or "")
        made = rules.made_at(str(card.get("id") or ""))
        if not rules.is_client_card(title) or made is None or made < since:
            continue
        if clearout.same_person(channel_name, rules.agent_name(title)):
            found.append(f"{title} ({made:%b %-d}) <{card.get('shortUrl') or card.get('url') or ''}>")
    if found:
        return False, (f"They have an order card from the last {RECENT_ORDER_DAYS} days: "
                       + "; ".join(found[:3]))
    return True, f"No order card for them in the last {RECENT_ORDER_DAYS} days"


def row_check(config: Config, plan) -> tuple[bool, str, bool]:
    """Whether Ryte Collection really has this clear-out's row, with the right
    sheet on it. (fine, said, missing altogether - so it can be written again)."""
    from .. import clearout

    try:
        rows, problems = cleared_out(config)
    except Exception as exc:
        return False, f"Couldn't read Ryte Collection back: {_short(exc, 120)}", False
    if problems:
        return False, "Couldn't read Ryte Collection back: " + "; ".join(problems), False
    channel = (plan.channel.name if plan.channel else "").casefold()
    mine = [row for row in rows if str(row.get("channel") or "").casefold() == channel]
    if not mine:
        return False, f"Ryte Collection has no row for #{plan.channel.name}", True
    if plan.sheet and not any(clearout.same_sheet(str(row.get("sheet") or ""), plan.sheet) for row in mine):
        return False, (f"Ryte Collection's row for #{plan.channel.name} has a different sheet "
                       "from the one kept"), False
    also = list(getattr(plan, "also_sheets", []) or [])
    lost = [one for one in also
            if not any(clearout.same_sheet(str(row.get("sheet") or ""), one) for row in mine)]
    if lost:
        return False, (f"Ryte Collection has no row for #{plan.channel.name} with its other sheet "
                       f"({lost[0]})"), False
    return True, (f"Ryte Collection has #{plan.channel.name}'s row" + (" with its sheet" if plan.sheet else "")
                  + (f", and its other sheet{'s' if len(also) > 1 else ''}" if also else "")), False


def sheet_check(config: Config, plan) -> tuple[bool, str]:
    """Whether the sheet kept opens, and is this channel's leads by its name."""
    from .. import clearout

    if not plan.sheet:
        return True, "No sheet to check"
    titled = sheet_titles(config, [plan.sheet])
    title = titled.get(plan.sheet)
    if not title:
        return False, "The sheet kept wouldn't open - check the link before the channel goes"
    wanted = clearout.kinds_in(plan.channel.name if plan.channel else "")
    kinds = clearout.kinds_in(title)
    if wanted and kinds and not (wanted & kinds):
        return False, (f"The sheet kept is “{title}”, which isn't this channel's leads "
                       f"({', '.join(sorted(wanted))})")
    # The other sheet kept with it has only to open - it is the card's, and
    # can be another order's leads.
    also = list(getattr(plan, "also_sheets", []) or [])
    if also:
        opened = sheet_titles(config, also)
        shut = [one for one in also if not opened.get(one)]
        if shut:
            return False, f"The other sheet kept wouldn't open: {shut[0]}"
        return True, f"The sheet opens: “{title}”, and the other kept: " + ", ".join(f"“{opened[one]}”" for one in also)
    return True, f"The sheet opens: “{title}”"


def picture_check(config: Config, plan, link: str) -> tuple[bool, str]:
    """Whether a picture is really in Drive, as a picture, in their folder."""
    import re as _re

    from .. import clearout, drive

    found = _re.search(r"/d/([A-Za-z0-9_-]{10,})", link or "")
    if not found:
        return False, "A picture has no Drive link"
    try:
        with drive.open_drive(config.secrets) as reading:
            meta = reading.about(found.group(1))
            if not meta or meta.get("trashed"):
                return False, f"“{meta.get('name') or link}” isn't in Drive any more"
            if not str(meta.get("mimeType") or "").startswith("image/") or int(meta.get("size") or 0) < 5000:
                return False, f"“{meta.get('name')}” isn't a proper picture"
            parent = (meta.get("parents") or [""])[0]
            folder = reading.about(parent) if parent else {}
    except Exception as exc:
        return False, f"Couldn't check the picture in Drive: {_short(exc, 120)}"
    if str(folder.get("name") or "") != clearout.their_folder(plan):
        return False, (f"“{meta.get('name')}” is in “{folder.get('name') or 'the top folder'}”, "
                       f"not their folder “{clearout.their_folder(plan)}”")
    return True, f"“{meta.get('name')}” is in their folder “{folder.get('name')}”"


def keep_the_picture(
    config: Config, page: str, called: str, *, into: str = "",
) -> tuple[str, list[str]]:
    """Photograph a conversation and put it in Drive. (link, problems).

    The channel is about to stop existing, so this is the only copy there will
    be of what was said in it - which is why it is a picture rather than a
    paragraph somebody wrote about it.

    `into` is the client's own folder inside the one from .env, so what is
    kept reads as their conversation rather than as loose pictures with every
    other client's mixed in.
    """
    import tempfile

    from .. import drive

    with tempfile.TemporaryDirectory() as folder:
        where = Path(folder)
        html_path, png_path = where / "convo.html", where / "convo.png"
        html_path.write_text(page, encoding="utf-8")
        try:
            _photograph(html_path, png_path)
        except Exception as exc:
            return "", [f"Couldn't render the conversation: {_short(exc, 140)}"]

        trouble = []
        try:
            with drive.open_drive(config.secrets) as uploading:
                theirs = ""
                if into:
                    try:
                        theirs = uploading.folder_named(into)
                    except Exception as exc:
                        # Into the top folder rather than nowhere. A picture
                        # in the wrong place is something to tidy up; a
                        # channel deleted without one is gone.
                        trouble.append(
                            f"Couldn't make their folder, so this went in the "
                            f"top one: {_short(exc, 120)}"
                        )
                got = uploading.put(png_path, name=called, into=theirs)
        except drive.DriveError as exc:
            return "", [str(exc)]
        except Exception as exc:
            return "", [f"Couldn't upload to Drive: {_short(exc, 140)}"]
    return got.link(), trouble


#: Wide enough that a line of conversation is not wrapped into noise, and the
#: height is whatever the messages come to.
PICTURE_WIDTH = 900


def _photograph(html_path: Path, png_path: Path) -> None:
    """One page, full height, as a PNG."""
    from playwright.sync_api import sync_playwright

    from .. import cover

    launch: dict = {"args": ["--no-sandbox", "--disable-dev-shm-usage"]}
    found = cover._chromium_executable()
    if found:
        launch["executable_path"] = found

    with sync_playwright() as playing:
        browser = playing.chromium.launch(**launch)
        try:
            page = browser.new_page(viewport={"width": PICTURE_WIDTH, "height": 1200})
            page.goto(html_path.resolve().as_uri())
            page.wait_for_function(
                "document.documentElement.dataset.ready === '1'", timeout=10_000
            )
            # Cut to what is on it. `full_page` is never shorter than the
            # viewport, so three messages came out as three messages and a
            # thousand pixels of empty purple underneath them.
            #
            # The body, and only the body. `documentElement.scrollHeight` is
            # never less than the viewport - asked of this very page it says
            # 1200 where the body says 277 - so taking the larger of the two
            # was asking the question and then throwing the answer away.
            tall = page.evaluate(
                "Math.ceil(document.body ? Math.max("
                "document.body.scrollHeight,"
                " document.body.getBoundingClientRect().bottom) : 0)"
            )
            # Rounded up here as well as in the page. Rounding down loses the
            # bottom of the last message, which is the one worth reading.
            import math

            page.set_viewport_size({
                "width": PICTURE_WIDTH,
                "height": max(1, min(math.ceil(float(tall or 0)) or 1200, 20_000)),
            })
            page.screenshot(path=str(png_path), full_page=True)
        finally:
            browser.close()


def _cards_by_contact(dispute, every: list) -> list:
    """The client cards carrying the email or phone on their Payra invoices -
    Meilee Reddy's card wasn't found by her name, and the rebuttal went out
    without her order, her setup or her delivered sheet."""
    from .. import agents as rules
    from .. import payraapi
    from ..contracts import card_email
    from ..smsreplies import phones_in

    emails, phones = payraapi.contacts_for(
        payraapi.load(), name=dispute.customer_name, email=dispute.customer_email)
    if dispute.customer_email:
        emails.add(str(dispute.customer_email).casefold())
    if not emails and not phones:
        return []
    return [
        card for card in every or []
        if rules.is_client_card(str(card.get("name") or ""))
        and (card_email(str(card.get("desc") or "")) in emails
             or phones & phones_in(str(card.get("desc") or "")))
    ]


def _payra_record(dispute) -> tuple[str, str]:
    """Payra's record of the disputed charge. (what it says, a problem or "").

    Quiet when RYTE holds nothing from Payra - not connected is a hole about
    RYTE, not about the dispute.
    """
    from .. import payraapi

    data = payraapi.load()
    if not data.get("invoices") and not data.get("payments"):
        return "", ""
    said, sure = payraapi.for_dispute(
        data, name=dispute.customer_name, email=dispute.customer_email,
        amount=dispute.amount, paid_on=dispute.paid(), card=dispute.card,
    )
    if not said:
        return "", (f"Nothing in Payra for “{dispute.customer_name}” in the last year, "
                    "so Payra's record had to be left out.")
    if not sure:
        return said, (
            f"None of “{dispute.customer_name}”'s Payra payments is "
            f"{dispute.amount or 'the disputed amount'} on "
            f"{dispute.transaction_date or 'the transaction date'} - the most recent "
            "is in the document; check it is the right charge."
        )
    return said, ""


def paid_invoice_pdf(who: str, config: Config | None = None) -> tuple[bytes, list[dict], str]:
    """Their paid invoices from Payra as one PDF. (pdf, the invoices, a problem).

    Payra's API gives the invoice as data, not as a document, so the document
    is made here from what it gives - every figure Payra's, and saying so.
    """
    from .. import payraapi

    data = payraapi.load()
    found = payraapi.paid_invoices(data, who) if data.get("invoices") else []
    if not found:
        # Payra's API only lists about the last week. What it emailed goes
        # back as far as the inbox does.
        emailed, trouble = _payra_emails(config, who) if config is not None else ([], "")
        if emailed:
            return _print_pdf(_emails_html(emailed, who)), [], ""
        if not data.get("invoices"):
            return b"", [], ("I don't have anything from Payra yet — `@RYTE payra status` "
                             "says whether it's connected.")
        said = _no_paid_invoice(data, who)
        return b"", [], said + (f"\n⚠ {trouble}" if trouble else "")
    made_on = f"{date.today():%B} {date.today().day}, {date.today().year}"
    return _print_pdf(payraapi.invoice_html(found, made_on=made_on)), found, ""


def _payra_emails(config: Config, who: str) -> tuple[list, str]:
    """Payra's payment confirmations for this name or invoice number, out of
    the inbox they were emailed to. (the emails, a problem or "").

    Only confirmations - "Payment Error", "Invoice Request" and the rest come
    from the same address and none of them says anybody paid. Searched as the
    exact phrase, so it is theirs and not a namesake's.
    """
    from .. import gmail as inbox

    if not (getattr(config.secrets, "gmail_invoice_sender", "") or "").strip():
        return [], ""
    try:
        with inbox.open_gmail(config.secrets) as reading:
            found = reading.invoices_for(who)
    except Exception as exc:
        return [], f"Couldn't read Payra's emails: {_short(exc, 140)}"
    return [one for one in found if "payment confirmation" in one.subject.casefold()], ""


#: The fields of Payra's confirmation, in the order the email gives them.
#: The body arrives as one line with runs of spaces between label and value.
RECEIPT_LABELS = (
    "Reference #", "Paid", "Customer", "Payment Method", "Invoice #", "Subtotal",
    "Discounts", "Total", "Other Fees", "Total Paid",
)


def receipt_fields(body: str) -> list[tuple[str, str]]:
    """(label, value) off a Payra confirmation, each label once."""
    import re

    bits = [" ".join(one.split()) for one in re.split(r"\s{3,}|\n", str(body or "")) if one.strip()]
    found, seen = [], set()
    for at, bit in enumerate(bits[:-1]):
        if bit in RECEIPT_LABELS and bit not in seen and bits[at + 1] not in RECEIPT_LABELS:
            seen.add(bit)
            found.append((bit, bits[at + 1]))
    return found


def _emails_html(emails: list, who: str) -> str:
    """Payra's confirmation emails as a printable page, one to a sheet.

    As they were sent - Payra's own layout, logo and all - when the email has
    its designed version, which is the stronger thing to hand a bank: it is
    visibly Payra's document, not RYTE's retelling of it. The fields laid out
    in a table when it hasn't. Each is headed with what it is and when it was
    sent.
    """
    import html
    import re

    styles, pages = [], []
    for one in emails[:10]:
        meta = (
            "<div class=ryte-meta>Payment confirmation from Payra — as emailed · "
            f"{html.escape(one.subject)} · sent {html.escape(one.when)}</div>"
        )
        designed = str(getattr(one, "html", "") or "")
        if designed.strip():
            designed = re.sub(r"<script\b.*?</script>", "", designed, flags=re.IGNORECASE | re.DOTALL)
            for style in re.findall(r"<style\b[^>]*>.*?</style>", designed, re.IGNORECASE | re.DOTALL):
                if style not in styles:
                    styles.append(style)
            inside = re.search(r"<body\b[^>]*>(.*)</body>", designed, re.IGNORECASE | re.DOTALL)
            pages.append(f"<section class=ryte-page>{meta}{inside.group(1) if inside else designed}</section>")
            continue
        fields = receipt_fields(one.body)
        text = str(one.body or "").split("Powered by PAYRA")[0].strip()
        rows = "".join(
            f"<tr><th>{html.escape(label)}</th><td>{html.escape(value)}</td></tr>"
            for label, value in fields
        )
        pages.append(
            f"<section class='ryte-page ryte-plain'>{meta}<h1>{html.escape(one.subject)}</h1>"
            + (f"<table>{rows}</table>" if rows else "")
            + f"<div class=ryte-label>The email as written</div><pre>{html.escape(text)}</pre></section>"
        )
    return (
        "<!doctype html><html><head><meta charset=utf-8>" + "".join(styles) + "<style>"
        "@page { size: Letter; margin: 0.5in; }"
        "body { margin: 0; }"
        ".ryte-page { page-break-after: always; } .ryte-page:last-child { page-break-after: auto; }"
        ".ryte-meta { font: 8.5pt/1.4 -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif; color: #666; "
        "border-bottom: 1px solid #ddd; padding-bottom: 6px; margin-bottom: 12px; }"
        ".ryte-plain { font: 11pt/1.45 -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif; color: #1b1b1f; }"
        ".ryte-plain h1 { font-size: 16pt; margin: 4px 0 12px; }"
        ".ryte-label { font-size: 8.5pt; text-transform: uppercase; letter-spacing: .06em; color: #666; margin-top: 18px; }"
        ".ryte-plain table { border-collapse: collapse; width: 100%; }"
        ".ryte-plain th, .ryte-plain td { text-align: left; padding: 7px 10px; border-bottom: 1px solid #ddd; }"
        ".ryte-plain th { width: 36%; color: #555; font-weight: 600; }"
        ".ryte-plain pre { white-space: pre-wrap; font: 9pt/1.5 -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif; "
        "color: #666; border-top: 1px solid #ddd; padding-top: 8px; }"
        f"</style></head><body>{''.join(pages)}</body></html>"
    )


def _no_paid_invoice(data: dict, who: str) -> str:
    """What was found instead, so "none" is never a guess."""
    from .. import payraapi

    theirs = payraapi.theirs_listed(data, who)
    held = len(data.get("invoices") or {})
    when = payraapi.status(data).split(" from Payra, last read ")[-1].split(".")[0] \
        if data.get("synced_at") else "not yet"
    if not theirs:
        close = payraapi.near(data, who)
        said = (f"No Payra invoice for “{who}” among the {held:,} I hold (last read {when}).")
        if close:
            return (said + " Closest names I have:\n" + "\n".join(f"• {one}" for one in close)
                    + "\nAsk with their email or the invoice number — `@RYTE invoice INV-18089`.")
        return (said + " The whole name as it is in Payra, their email, their phone number "
                "or the invoice number finds them.")
    listed = "\n".join(
        f"• #{one.get('number') or '?'} — {payraapi._money(one.get('total'))}, dated "
        f"{payraapi._spelled(one.get('invoice_date'))} — no payment I can see on it"
        for one in theirs
    )
    return (f"I have {len(theirs)} Payra invoice{'' if len(theirs) == 1 else 's'} for “{who}” "
            f"but can't see a payment on {'it' if len(theirs) == 1 else 'any of them'}:\n{listed}\n"
            "-# Payra may still be reading its year of records again after an update — "
            "try again in a few minutes.")


def _print_pdf(html: str) -> bytes:
    """A page of HTML as a PDF, through the same Chromium the covers use."""
    from playwright.sync_api import sync_playwright

    from .. import cover

    launch: dict = {"args": ["--no-sandbox", "--disable-dev-shm-usage"]}
    found = cover._chromium_executable()
    if found:
        launch["executable_path"] = found
    with sync_playwright() as playing:
        browser = playing.chromium.launch(**launch)
        try:
            # No scripts: an email's page is for looking at. Its images - Payra's
            # logo - are waited for, but not forever.
            page = browser.new_page(java_script_enabled=False)
            try:
                page.set_content(html, wait_until="networkidle", timeout=20_000)
            except Exception:
                import logging

                logging.getLogger("wilbyte.bot").info(
                    "An image in the page didn't load in time; printing without it")
            return page.pdf(format="Letter", print_background=True,
                            margin={"top": "0.6in", "bottom": "0.6in", "left": "0.6in", "right": "0.6in"})
        finally:
            browser.close()


def _payment_receipt(config: Config, dispute) -> tuple[str, str]:
    """Payra's confirmation for this customer. (what it says, a problem or "").

    Not set up is not a problem worth putting in the document: the rebuttal
    stood without it before there was an inbox to read, and a hole that says
    "nobody configured Gmail" is a hole about RYTE rather than about the
    dispute.
    """
    from .. import gmail as inbox

    if not (getattr(config.secrets, "gmail_invoice_sender", "") or "").strip():
        return "", ""
    try:
        with inbox.open_gmail(config.secrets) as reading:
            found = reading.invoices_for(dispute.customer_name)
    except inbox.GmailError as exc:
        return "", f"Couldn't read the payment confirmation: {_short(exc, 140)}"
    except Exception as exc:
        return "", f"Couldn't read the payment confirmation: {_short(exc, 140)}"

    # The confirmation, not the failures. "Payment Error", "Invoice Request",
    # "Payment request" and "Payments Summary" all come from the same address
    # and none of them is proof that anybody paid anything.
    paid = [one for one in found if "payment confirmation" in one.subject.casefold()]
    if not paid:
        return "", (
            f"No Payra payment confirmation for “{dispute.customer_name}” in the "
            "inbox, so the receipt had to be left out."
        )

    # The one being disputed, not the most recent. Jay Rodriguez has three -
    # July, September 3rd and September 14th - and handing the acquirer a
    # receipt for a charge nobody is arguing about is worse than handing them
    # none: it is the wrong document under a heading that says it is the right
    # one.
    one, sure = _the_disputed_one(paid, dispute)
    said = f"{one.subject}\n{one.when}\n\n{one.body}".strip()
    if sure:
        return said, ""
    return said, (
        f"“{dispute.customer_name}” has {len(paid)} payment confirmations and "
        f"none of them matches {dispute.amount or 'the disputed amount'} on "
        f"{dispute.transaction_date or 'the transaction date'}. The most "
        "recent one is in the document - check it is the right charge."
    )


#: Said instead of attaching a stamped copy.
DEMO_COPY = (
    "PandaDoc's API gave a copy stamped “for demo purposes only and is not intended "
    "for legal use” on every page, so it was left out - a bank reviewer would read "
    "that line. Download the signed PDF from PandaDoc yourself and attach it."
)


def _pandadoc_contract(config: Config, name: str, *, email: str = "", paid=None,
                       for_dispute: bool = False):
    """(says, PDF, filename, problem) from PandaDoc itself, or None when PandaDoc
    isn't set up - so the inbox can be tried instead."""
    from .. import pandadoc

    key = (getattr(config.secrets, "pandadoc_api_key", "") or "").strip()
    if not key:
        return None
    try:
        found, how, problem = pandadoc.find_signed(key, name=name, email=email, paid=paid)
        if found is None:
            return "", b"", "", problem
        with pandadoc.PandaDoc(key) as reading:
            pdf = reading.pdf(found.doc_id)
    except pandadoc.PandaDocError as exc:
        return "", b"", "", f"Couldn't get the contract from PandaDoc: {_short(exc, 140)}"
    except Exception as exc:
        return "", b"", "", f"Couldn't get the contract from PandaDoc: {_short(exc, 140)}"
    said = (
        f"{found.title}\nSigned {found.signed:%B %-d, %Y} in PandaDoc by "
        f"{', '.join(found.emails) or 'the client'}"
    )
    problem = "" if how == "email" or not email else (
        f"The PandaDoc contract for {name} was sent to {', '.join(found.emails) or 'no email'}, "
        f"not {email} - check it's theirs before it goes in."
    )
    called = re.sub(r"[^\w .()-]+", "", found.title).strip() or "contract"
    if pandadoc.stamped_demo(pdf):
        if for_dispute:
            return said, b"", "", DEMO_COPY
        problem = (problem + " " if problem else "") + (
            "⚠ This copy is stamped “for demo purposes only” by PandaDoc's API - "
            "don't use it in a dispute; download it from PandaDoc instead."
        )
    return said, pdf, f"{called}.pdf", problem


def _signed_contract(config: Config, dispute) -> tuple[str, bytes, str, str]:
    """The signed contract for this customer, out of the inbox it was sent to.

    (what it says, the PDF, what to call it, a problem or "").

    Only works if the signed PDF arrives by email, and on this account it does
    not: "pandadoc doesnt send contract on gmail, we have to download it on
    pandadoc". Kept because the rebuttal is quiet when it finds nothing, and
    because a forwarded contract is still a contract - but the way to the PDF
    is PandaDoc's own download, which needs its API key.

    The PDF when there is one, because the no-chargeback clause is in the
    document rather than in the notification. The body is the fallback: it
    carries who signed and when, which is most of what the timeline wants.
    """
    # Quiet when nobody set it up: the rebuttal stood without the contract
    # before there was an inbox to read, and a hole saying "nobody configured
    # Gmail" is a hole about RYTE rather than about the dispute. Asked for by
    # name, the same silence would be a wrong answer - see `signed_contract_for`.
    # PandaDoc first: it is where the signed PDF actually is.
    from_pandadoc = _pandadoc_contract(
        config, dispute.customer_name, email=dispute.customer_email, paid=dispute.paid(),
        for_dispute=True,
    )
    if from_pandadoc is not None:
        said, pdf, called, problem = from_pandadoc
        if problem.startswith("No signed contract"):
            problem = problem.rstrip(".") + ", so it had to be left out."
        return said, pdf, called, problem
    if not (getattr(config.secrets, "gmail_contract_sender", "") or "").strip():
        return "", b"", "", ""
    said, pdf, called, problem = signed_contract_for(config, dispute.customer_name)
    if problem.startswith("No signed contract"):
        problem = problem.rstrip(".") + ", so it had to be left out."
    return said, pdf, called, problem


def signed_contract_for(config: Config, name: str) -> tuple[str, bytes, str, str]:
    """The signed contract for somebody, by name. (says, PDF, filename, problem).

    "contract of David Pereira." The finding was already written, inside the
    rebuttal; asked for plainly it fell through to the help text.

    Not quiet when the inbox is not set up. Somebody asked for a document by
    name, and saying nothing is indistinguishable from there being no
    contract - which is a different thing to find out.
    """
    from .. import gmail as inbox

    who = " ".join(str(name or "").split())
    if not who:
        return "", b"", "", "Whose contract? Try `@RYTE contract of David Pereira`."
    from_pandadoc = _pandadoc_contract(config, who)
    if from_pandadoc is not None:
        return from_pandadoc
    if not (getattr(config.secrets, "gmail_contract_sender", "") or "").strip():
        # Not "set GMAIL_CONTRACT_SENDER". That was the advice until Franklin
        # said what PandaDoc actually does: "pandadoc doesnt send contract on
        # gmail, we have to download it on pandadoc". Pointing at a setting
        # that cannot find anything is worse than saying plainly that this
        # part does not reach PandaDoc yet.
        return "", b"", "", (
            "I can't reach PandaDoc contracts yet — PandaDoc doesn't email the "
            "signed PDF, it has to be downloaded there. Download it and attach "
            "it to the rebuttal."
        )
    try:
        with inbox.open_contracts(config.secrets) as reading:
            found = reading.invoices_for(who)
            if not found:
                return "", b"", "", f"No signed contract for “{who}” in the inbox."
            # The completed one. PandaDoc emails at every step - sent, viewed,
            # a reminder - and only the completed one is the signed document.
            done = [
                one for one in found
                if any(word in one.subject.casefold()
                       for word in ("completed", "signed", "countersigned"))
            ] or found
            one = done[0]
            said = f"{one.subject}\n{one.when}\n\n{one.body}".strip()
            for called, attachment_id in one.files:
                if called.casefold().endswith(".pdf"):
                    return said, reading.download(one.message_id, attachment_id), called, ""
            return said, b"", "", (
                f"The contract email for “{who}” has no PDF on it. Download the "
                "completed PDF from PandaDoc for the clause."
            )
    except inbox.GmailError as exc:
        return "", b"", "", f"Couldn't read the signed contract: {_short(exc, 140)}"
    except Exception as exc:
        return "", b"", "", f"Couldn't read the signed contract: {_short(exc, 140)}"


def _the_disputed_one(paid: list, dispute):
    """The confirmation for the charge being disputed. (email, certain?).

    Matched on what the acquirer's notice actually carries: the amount, and
    the day it was paid. Either one alone is enough - a customer who pays the
    same amount monthly is told apart by the date, and one whose date reads
    differently on the two systems is told apart by the amount.
    """
    from .. import rebuttal as rules_doc

    digits = "".join(ch for ch in str(dispute.amount or "") if ch.isdigit() or ch == ".")
    when = dispute.paid()
    spelled = f"{when:%B %-d, %Y}" if when else ""

    for one in paid:
        body = one.body or ""
        by_money = bool(digits) and digits in body.replace(",", "")
        by_day = bool(spelled) and spelled in body
        if by_money and by_day:
            return one, True
    for one in paid:
        body = one.body or ""
        if (digits and digits in body.replace(",", "")) or (spelled and spelled in body):
            return one, True
    return paid[0], False


def _read_lead_sheet(config: Config, link: str, gsheets) -> tuple[str, str]:
    """What the delivered sheet shows. (what it says, a problem or "").

    The rows, and more to the point the client's own notes down the side of
    them: a customer cannot log call outcomes and appointment times on leads
    he never received.
    """
    import re

    from .. import gsheets as sheets_mod

    sheet_id = sheets_mod.sheet_id_in(link) if hasattr(sheets_mod, "sheet_id_in") else ""
    if not sheet_id:
        found = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})", str(link or ""))
        sheet_id = found.group(1) if found else ""
    if not sheet_id:
        return "", f"Couldn't read a sheet id out of {link}"

    try:
        client = sheets_mod.SheetsClient(sheets_mod.credentials(config.secrets))
    except Exception as exc:
        return "", f"Couldn't open Google Sheets: {_short(exc, 120)}"
    try:
        tabs = client.tabs(sheet_id)
        if not tabs:
            return "", "That sheet has no tabs I can read."
        title = str((tabs[0] or {}).get("title") or "Sheet1")
        rows = client.rows(sheet_id, f"{title}!A1:Z400")
    except Exception as exc:
        return "", f"Couldn't read the lead sheet: {_short(exc, 120)}"
    finally:
        client.close()

    if not rows:
        return "", "The lead sheet is empty."

    heads = [str(one) for one in rows[0]]
    body = [row for row in rows[1:] if any(str(cell).strip() for cell in row)]
    written = [
        f"Tab: {title}",
        f"Columns: {', '.join(heads)}",
        f"Leads delivered: {len(body)}",
    ]
    # The client's own writing, which is the part that proves they had it.
    notes = []
    for row in body:
        for cell in row[len(heads) - 3:] if len(row) > 3 else []:
            said = str(cell).strip()
            if len(said) > 8 and not said.replace("-", "").isdigit():
                notes.append(said)
    if notes:
        written.append(
            f"Notes the client wrote on the leads ({len(notes)}):\n"
            + "\n".join(f"- {one}" for one in notes[:25])
        )
    return "\n".join(written), ""


def _when_delivered(notes, rules) -> str:
    """The day the sheet was handed over, off the comment that handed it over.

    Nicole posts the link and then says "delivered", so the link is the one
    that dates the handover. The oldest such comment rather than the newest:
    a top-up months later is not when the first delivery happened.
    """
    dated = []
    for one in notes or []:
        when = str(one.get("when") or "")[:10]
        if when and rules.sheet_links([str(one.get("text") or "")]):
            dated.append(when)
    return min(dated) if dated else ""


def _timeline(dispute, agent, found) -> list:
    """The dated spine of the document, from what was actually established."""
    when = []
    paid = dispute.paid()
    if paid:
        when.append((f"{paid:%m/%d/%Y}", f"Charged {dispute.amount}"))
    # The handover. Without it the spine of the document is a charge and a
    # chargeback with nothing in between, which is the half that answers the
    # dispute - Juliana Hernandez's timeline was one line long.
    #
    # The sheet is shared before the launch and fills from it. Said as
    # "delivered" beside a later line saying "leads begin delivery", it read
    # as the document contradicting itself about the one fact it is there to
    # establish: David Pereira's said 09/04 delivered and 09/07 beginning.
    handed = (getattr(found, "delivered_on", "") or "").split("-")
    shared = (
        f"{handed[1]}/{handed[2]}/{handed[0]}" if len(handed) == 3 else ""
    )
    launched = f"{agent.launch:%m/%d/%Y}" if agent is not None and agent.launch else ""
    if shared and shared == launched:
        # One day, one line. Two lines on the same date reading around each
        # other is what started this.
        when.append((
            shared,
            "Delivery sheet shared with the cardholder and leads begin "
            "landing in it",
        ))
    else:
        if shared:
            when.append((
                shared,
                "Delivery sheet shared with the cardholder, confirmed in writing",
            ))
        if launched:
            when.append((
                launched, "Launch date — leads begin landing in that sheet",
            ))
    disputed = dispute.disputed()
    if disputed:
        waited = dispute.days_waited()
        when.append((
            f"{disputed:%m/%d/%Y}",
            f"Chargeback filed{f' — {waited} days later' if waited else ''}",
        ))
    when.sort(key=lambda one: one[0][-4:] + one[0][:5])
    return when


# What a signed contract looks like when it comes out of PandaDoc. Checked
# before the invoice words, because a services agreement states a price and
# says "amount due" in its payment terms - and a contract filed as an invoice
# is the one exhibit the rebuttal most needs, under the wrong heading.
#
# The completion certificate is the strongest of these: PandaDoc staples it to
# the back of a completed document, and it carries the signing date, the
# reference and the audit trail - which is the whole reason the contract is
# worth attaching.
SIGNED = (
    "pandadoc", "completion certificate", "signature certificate",
    "electronically signed", "document completed", "audit trail",
    "no-chargeback", "no chargeback",
)

AGREEMENT = ("agreement", "acuerdo", "terms of service", "this contract")

RECEIPT = ("invoice", "amount due", "payment confirmation", "you just got paid")


def _what_pdf_is(text: str) -> str:
    """"contract", "invoice" or "" for a PDF, from the words in it.

    Order matters. A services agreement quotes a price and has payment terms
    in it, so looking for invoice words first files the signed contract as a
    receipt - and the contract is the exhibit carrying the signing date and
    the no-chargeback clause, which is the point of attaching it at all.
    """
    low = " ".join((text or "").casefold().split())
    if not low:
        return ""
    if any(word in low for word in SIGNED):
        return "contract"
    front = low[:3000]
    if any(word in front for word in AGREEMENT):
        return "contract"
    if any(word in front for word in RECEIPT):
        return "invoice"
    return ""


def sort_exhibits(config: Config, exhibits: list) -> list:
    """Work out what each attached file is, by looking at it.

    Rather than by asking somebody to label six screenshots. A PDF is read as
    text; an image is looked at. Anything RYTE cannot place stays "other" and
    goes in at the end under its own heading rather than into a proof it might
    not belong to.
    """
    import base64
    import logging
    import re

    from anthropic import Anthropic

    from .. import rebuttal as rules_doc

    log = logging.getLogger("wilbyte.bot")
    for one in exhibits:
        if one.is_pdf():
            one.text = _pdf_text(one.data)
            one.kind = _what_pdf_is(one.text) or one.kind

    pictures = [one for one in exhibits if one.is_image() and one.data]
    if not pictures or not config.secrets.anthropic_api_key:
        return exhibits

    kinds = "\n".join(f"- {name}: {what}" for name, what in rules_doc.EXHIBITS.items())
    content = []
    for number, picture in enumerate(pictures[:12], start=1):
        content.append({"type": "text", "text": f"Image {number}:"})
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": _media_type(picture.name),
                "data": base64.b64encode(picture.data).decode("ascii"),
            },
        })
    content.append({
        "type": "text",
        "text": (
            "These are exhibits for a chargeback rebuttal, to be read by "
            "somebody at a card acquirer.\n\nFor each image give me three "
            f"things.\n\n1. kind — which of these it is:\n{kinds}\n"
            "If you are not sure, say `other` rather than guessing.\n\n"
            "2. caption — at most twelve words saying what it shows.\n\n"
            "3. transcript — everything legible in the image, in English. For "
            "a conversation: one line per message as `TIME  Speaker: what they "
            "said`, in order, keeping the date headers. Translate anything "
            "that is not English and translate it faithfully — the person "
            "reading this will not speak it. Say `[voice note, 0:42]` for a "
            "voice message rather than inventing what was in it. For a "
            "spreadsheet or a document, describe the columns and quote the "
            "rows that matter. For a payment or invoice record, quote every field "
            "and its value exactly as written - an AVS result, an "
            "authorisation response and a billing address decide cases, "
            "and a value copied loosely is worth nothing. Never write a "
            "word that is not in the image.\n\n"
            "Answer as:\n\n"
            "IMAGE 1\nkind: texts\ncaption: ...\ntranscript:\n<lines>\n\n"
            "IMAGE 2\n...\n\nNothing else."
        ),
    })

    try:
        client = Anthropic(api_key=config.secrets.anthropic_api_key)
        response = client.messages.create(
            model=config.copy.model,
            max_tokens=8000,
            messages=[{"role": "user", "content": content}],
        )
        said = "".join(
            block.text for block in response.content
            if getattr(block, "type", None) == "text"
        )
    except Exception:
        log.exception("Couldn't look at the exhibits; leaving them unsorted")
        return exhibits

    for number, block in _read_images(said).items():
        if not 1 <= number <= len(pictures):
            continue
        picture = pictures[number - 1]
        kind = str(block.get("kind") or "").strip().casefold()
        picture.kind = kind if kind in rules_doc.EXHIBITS else "other"
        picture.caption = str(block.get("caption") or "").strip()[:90]
        picture.transcript = str(block.get("transcript") or "").strip()
    return exhibits


def _read_images(said: str) -> dict:
    """Claude's answer about the exhibits, as {image number: {field: value}}."""
    import re

    found: dict[int, dict] = {}
    number = 0
    field = ""
    for line in (said or "").splitlines():
        heading = re.match(r"\s*IMAGE\s+(\d+)\s*$", line, re.IGNORECASE)
        if heading:
            number = int(heading.group(1))
            found.setdefault(number, {})
            field = ""
            continue
        if not number:
            continue
        named = re.match(r"\s*(kind|caption|transcript)\s*:\s*(.*)$", line, re.IGNORECASE)
        if named:
            field = named.group(1).casefold()
            found[number][field] = named.group(2).strip()
            continue
        if field:
            found[number][field] = (found[number].get(field, "") + "\n" + line).strip()
    return found


def _pdf_text(data: bytes) -> str:
    """The words out of a PDF, or "" when it can't be read."""
    import io
    import logging

    log = logging.getLogger("wilbyte.bot")
    try:
        from pypdf import PdfReader
    except ImportError:
        log.warning("pypdf isn't installed, so attached PDFs are not read")
        return ""
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages[:12])
    except Exception:
        log.exception("Couldn't read an attached PDF")
        return ""


def _media_type(name: str) -> str:
    low = (name or "").lower()
    if low.endswith(".png"):
        return "image/png"
    if low.endswith(".gif"):
        return "image/gif"
    if low.endswith(".webp"):
        return "image/webp"
    return "image/jpeg"


def write_rebuttal(config: Config, dispute, found, exhibits, *, into) -> "object":
    """Have it written, then build the file. Returns where the file went."""
    from anthropic import Anthropic

    from .. import rebuttal as rules_doc, rebuttaldoc

    # What the attached contract and invoice actually say, so the proofs about
    # them quote the document rather than describing it.
    for one in exhibits:
        if one.kind == "contract" and one.text and not found.contract:
            found.contract = one.text[:6000]
        if one.kind == "invoice" and one.text:
            found.invoice = (found.invoice + "\n\n" + one.text[:3000]).strip()

    # Lettered before the writing, so the argument can cite Exhibit A and
    # mean the same thing the back of the document does. The same screenshot
    # twice is one exhibit.
    exhibits[:] = rules_doc.letter_them(rules_doc.only_once(exhibits))

    config.secrets.require("anthropic_api_key")
    client = Anthropic(api_key=config.secrets.anthropic_api_key)
    response = client.messages.create(
        model=config.copy.model,
        max_tokens=4000,
        system=(
            "You write chargeback rebuttals for a lead-generation company, to "
            "be submitted to a card acquirer. Every sentence must trace to the "
            "evidence you were given. Never state a fact, date or sum that is "
            "not in it. A short true section beats a long padded one."
        ),
        messages=[{
            "role": "user",
            "content": rules_doc.writing_prompt(dispute, found, exhibits),
        }],
    )
    said = "".join(
        block.text for block in response.content
        if getattr(block, "type", None) == "text"
    )
    written = _split_written(said)
    return rebuttaldoc.build(dispute, written, found, exhibits, into=into)


def _split_written(said: str) -> dict:
    """Claude's answer, kept whole.

    It used to be cut up by the evidence name each section was labelled with,
    to be dropped under fixed proof headings. The document argues in its own
    numbered sections now, so the writing is laid out as written - splitting
    it was how "**attached files**" ended up printed with its asterisks
    showing in the middle of a section it did not belong to.
    """
    import re

    text = (said or "").strip()
    # The message table is pulled out and set as a table; everything else is
    # laid out as written.
    found = re.search(
        r"^\s*KEY\s+MESSAGES\s*[:.]?\s*$(.*?)(?=^\s*(?:CONCLUSION|ARGUMENT|SUMMARY|TIMELINE)\s*[:.]?\s*$|\Z)",
        text, re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    if not found:
        return {"body": text}
    return {
        "body": (text[:found.start()] + text[found.end():]).strip(),
        "messages": found.group(1).strip(),
    }


def _jot(noticed, kind: str, subject: str, *, detail: str = "") -> None:
    """Write a sighting in the notebook, and never fail because of it.

    Noticing is a side effect of doing the work. A notebook that cannot be
    written - a read-only disk, a half-written file - must not stop an agent
    being filed, so this swallows everything.
    """
    try:
        noticed.note(kind, subject, detail=detail)
    except Exception:  # noticing is never worth breaking the work over
        log.debug("Couldn't write %s/%s to the notebook", kind, subject, exc_info=True)


def _teach_me(label: str) -> str:
    """" — I don't know what "STNDRD" means; …", or "" when nothing is unknown.

    The moment a lead type can't be placed is the moment somebody knows why,
    and it is the only moment they are looking. Asking then costs nothing and
    means the next card with that word on it files itself.
    """
    from .. import agents as rules, noticed

    unknown = rules.words_it_cannot_place(label)
    if not unknown:
        return ""
    word = unknown[0]
    # Written down as well as said. Asking in the moment only works if
    # somebody is looking at that moment; "PHX STNDRD" stopped him four times
    # in a week and each fix started with a person noticing a screenshot.
    _jot(noticed, "unplaced", word, detail=f"seen on “{label}”")
    return (
        f"\n   I don't know what “{word}” means. "
        f"`@RYTE words {word} = standard` (or plus, iul, spanish…) and I'll remember."
    )


def _cards_by_url(cards: list[dict]) -> dict[str, str]:
    """Card id by URL, both the short and the long one.

    Same reason `_by_url` keeps both: a checklist item stores whichever URL was
    copied, and looking the card up by the other one silently finds nothing.
    """
    found: dict[str, str] = {}
    for card in cards or []:
        card_id = str(card.get("id") or "")
        if not card_id:
            continue
        for field in ("url", "shortUrl"):
            where = str(card.get(field) or "")
            if where:
                found[where] = card_id
    return found


# How far back to look for lines on the wrong day. A Lead Order card older
# than this is long finished and its leads are long delivered, so what it says
# is history rather than something to fix.
WRONG_DAY_BACK = 14


#: Monday is 0, so Friday is 4. Named rather than written as a number in the
#: middle of a list comprehension.
FRIDAY = 4


def days_watched(day) -> list:
    """The days whose Lead Order cards are worth watching from `day`.

    Today and tomorrow, because both are open at once from mid-morning and a
    line written onto either is wrong the moment it lands. And on a Friday the
    weekend as well - "do the same day lead order, and next day, and next
    following days whenever its friday" - because Saturday, Sunday and Monday
    are all set up on the Friday and all spread onto one card.
    """
    ahead = 3 if day.weekday() == FRIDAY else 1
    return [day + timedelta(days=step) for step in range(ahead + 1)]


def wrong_day_lines(
    config: Config, *, day=None, back: int = WRONG_DAY_BACK, only=None
):
    """Lines sitting on a Lead Order card for a day the agent isn't live.

    (findings, problems). Reads only - nothing is moved, ticked or removed.

    The spread refuses to write these now, but the ones written before it
    started asking are still sitting there, and every one of them is an order
    of leads on the wrong day. This is the sweep for those: every Lead Order
    card of the last fortnight, every linked line on it, each agent's own card
    read for the day that order goes live.

    Only a launch still ahead of the card counts. The first run of this found
    twenty-nine lines and twenty-five of them were an agent's old launch date
    still sitting on their card - a top-up, a re-order, a product bought weeks
    after the first one. "45 more OTP vets" is not an agent going live; the
    date on that card is the day they went live in the first place.

    An agent card that names no day is not a finding either. Most say when
    once and plenty say nothing.
    """
    from .. import agents as rules
    from .. import dailyops, trello

    day = day or board_day(config)
    client = open_trello(config)
    findings, problems = [], []
    try:
        lists = client.board_lists(config.secrets.trello_board_id)
        every = [c for bl in lists for c in client.list_cards(str(bl.get("id") or ""))]
        cards = _cards_by_url(every)
        named = _by_url(every)
        said_on: dict[str, str] = {}

        wanted = set(only) if only else None
        orders = []
        for card in every:
            found = dailyops.parse_card_title(str(card.get("name") or ""))
            if not found or found[0] != "lead_order":
                continue
            covers = set(dailyops.card_days(str(card.get("name") or "")))
            if not covers:
                continue
            # `only` is the watcher, looking at the days still to be worked.
            # Without it this is the sweep, looking back over the fortnight.
            if wanted is not None:
                if covers & wanted:
                    orders.append((card, covers))
            elif max(covers) >= day - timedelta(days=back):
                orders.append((card, covers))

        if not orders:
            if wanted is not None:
                return [], []
            return [], [f"No Lead Order cards in the last {back} days on the board"]

        for card, covers in sorted(
            orders, key=lambda pair: min(pair[1]), reverse=True
        ):
            try:
                held = client.card_checklists(str(card.get("id") or ""))
            except Exception as exc:
                problems.append(
                    f"Couldn't read {card.get('name')}: {_short(exc, 120)}"
                )
                continue
            for checklist in held:
                for item in checklist.get("checkItems") or []:
                    line = str(item.get("name") or "")
                    short = trello.linked_card_id(line)
                    if not short:
                        continue
                    url = next(
                        (one for one in cards if short in one), ""
                    )
                    if not url:
                        continue
                    label = rules.item_lead_type(line)
                    goes = rules.launch_for(
                        _card_said(client, cards.get(url), said_on),
                        label, today=min(covers),
                    )
                    if goes is None or goes <= max(covers):
                        continue
                    findings.append({
                        "card": str(card.get("name") or ""),
                        "checklist": str(checklist.get("name") or ""),
                        "agent": named.get(url, url),
                        "label": label,
                        "live": goes,
                        "ticked": str(item.get("state") or "") == "complete",
                    })
        return findings, problems
    finally:
        client.close()


#: Agent cards as last read for the live-date check: {card id: (last
#: activity, (launch, note))}. Read again only once the card has been touched.
_LAUNCH_READS: dict = {}

#: How far ahead the setup cards are read back.
LIVE_MOVED_AHEAD = 7


#: Off until the hub field that is the go-live day is known: `start_date`
#: isn't - every agent came back "Sat Oct 24", and Bryson Fabrizio's hub page
#: says 2026-10-26 where `start_date` gave the 24th.
HUB_DAY_CHECK = False


def _hub_start_days(config: Config, today) -> list[tuple[str, date]] | None:
    """(name, start date) for every hub agent not done, starting from a week
    back - None when the hub can't be read, so nothing's said about it."""
    from .. import hub

    if not HUB_DAY_CHECK or not str(getattr(config.secrets, "hub_api_token", "") or "").strip():
        return None
    try:
        found = hub.agents(config.secrets, status="all")
    except Exception as exc:
        logging.getLogger("wilbyte.bot").warning("Couldn't read the hub's start dates: %s", _short(exc, 120))
        return None
    return [(one.name, one.start_date) for one in found
            if one.start_date is not None and one.start_date >= today - timedelta(days=7)
            and str(one.status or "").casefold() not in hub.DONE_WORDS]


def _hub_day_off(hub_days, name: str, launch, today=None) -> date | None:
    """The day the hub has them starting when it isn't `launch` - None when
    it's right, they aren't on it, or it couldn't be read. Any of their
    orders on that day is right. One started already is leads flowing - a
    re-order on the same record: Jessica Aguirre's Oct 17 under her Friday
    order - and isn't a wrong day."""
    from .. import clearout

    if not hub_days or not name:
        return None
    theirs = sorted({day for who, day in hub_days if clearout.name_match(name, who) >= 2
                     and (today is None or day >= today)})
    if not theirs or launch in theirs:
        return None
    return min(theirs, key=lambda day: abs((day - launch).days))


def live_dates_moved(config: Config, *, today=None) -> tuple[list, list[str]]:
    """Agents on a setup card for a day they no longer go live on.
    ([livemoved.Moved], problems). Reads only."""
    from zoneinfo import ZoneInfo

    from .. import agents as rules
    from .. import livemoved

    today = today or board_day(config)
    zone = config.schedule.timezone
    days = [today + timedelta(days=n) for n in range(LIVE_MOVED_AHEAD + 1)]
    recent = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc) - timedelta(days=30)
    # The hub's own start dates - what it's set for. Not the setup comments:
    # Nova writes "Ready to go live Sat Oct 24" under Therese's name for
    # agents set up right on the hub for Sun and Mon - "these were setup
    # correctly on hub but nova commented incorrectly".
    hub_days = _hub_start_days(config, today)
    client = open_trello(config)
    found, problems = [], []
    try:
        every = client.board_cards(config.secrets.trello_board_id)
        by_url = {}
        for card in every:
            for field in ("url", "shortUrl"):
                if card.get(field):
                    by_url[str(card[field])] = card
        setups = [
            card for card in every
            if rules.is_setup_card(str(card.get("name") or ""))
            and any(rules.setup_covers(str(card.get("name") or ""), day) for day in days)
            and (rules.made_at(str(card.get("id") or "")) or recent) >= recent
        ]
        for setup in setups:
            title = str(setup.get("name") or "")
            try:
                checklists = client.card_checklists(str(setup.get("id") or ""))
            except Exception as exc:
                problems.append(f"Couldn't read {title}: {_short(exc, 120)}")
                continue
            for checklist in checklists:
                for item in checklist.get("checkItems") or []:
                    name, _leads, url = rules.split_setup_item(str(item.get("name") or ""))
                    card = by_url.get(url)
                    if card is None:
                        continue
                    card_id = str(card.get("id") or "")
                    touched = str(card.get("dateLastActivity") or "")
                    held = _LAUNCH_READS.get(card_id)
                    if held is not None and held[0] == touched and len(held[1]) == 3:
                        launch, note, setup_day = held[1]
                    else:
                        made = rules.made_at(card_id)
                        try:
                            notes = client.card_notes(card_id)
                        except Exception as exc:
                            problems.append(f"Couldn't read {name}'s card: {_short(exc, 120)}")
                            continue
                        launch, note = livemoved.current_launch(
                            notes, str(card.get("desc") or ""),
                            made=made.date() if made else today, timezone=zone,
                        )
                        setup_day = None
                        _LAUNCH_READS[card_id] = (touched, (launch, note, setup_day))
                    if launch is None:
                        continue
                    agent_name = name or rules.agent_name(str(card.get("name") or ""))
                    setup_day = _hub_day_off(hub_days, agent_name, launch, today)
                    hub_wrong = setup_day is not None and setup_day != launch
                    if rules.setup_covers(title, launch) and not hub_wrong:
                        continue
                    when = livemoved._local((note or {}).get("when"), ZoneInfo(zone))
                    found.append(livemoved.Moved(
                        agent=name or rules.agent_name(str(card.get("name") or "")),
                        card_url=str(card.get("shortUrl") or card.get("url") or url),
                        setup_card=title, checklist=str(checklist.get("name") or ""), live=launch,
                        who=str((note or {}).get("author") or ""),
                        when=f"{when:%b %-d %-I:%M %p}" if when else "",
                        said=str((note or {}).get("text") or ""),
                        setup_card_id=str(setup.get("id") or ""),
                        checklist_id=str(checklist.get("id") or ""),
                        item_id=str(item.get("id") or ""),
                        item_name=str(item.get("name") or ""),
                        ticked=str(item.get("state") or "") == "complete",
                        setup_day=setup_day if hub_wrong else None,
                        hub_only=rules.setup_covers(title, launch),
                    ))
        return found, problems
    except Exception as exc:
        return [], [f"Couldn't read the board: {_short(exc, 140)}"]
    finally:
        client.close()


def move_live_line(config: Config, moved) -> tuple[list[str], list[str]]:
    """Move an agent whose go-live day changed: their line off the old setup
    card onto the new day's, on the same person's list - and the same on the
    Lead Order cards if they've been spread already. (done, problems).

    "yes add the button to move it". Each line is added where it goes before
    it comes off where it was, so a failure part way leaves it in two places
    rather than none. Nothing is made up: no setup card, Lead Order card or
    list for the new day, and that part is left and said.
    """
    from .. import agents as rules
    from .. import dailyops, trello

    done, problems = [], []
    day = moved.live
    recent = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) - timedelta(days=30)
    client = open_trello(config)
    try:
        every = client.board_cards(config.secrets.trello_board_id)

        def same_list(checklists, name):
            return next((one for one in checklists
                         if str(one.get("name") or "").strip().casefold() == name.strip().casefold()), None)

        # The setup card.
        targets = sorted(
            (card for card in every
             if rules.is_setup_card(str(card.get("name") or ""))
             and rules.setup_covers(str(card.get("name") or ""), day)
             and str(card.get("id") or "") != moved.setup_card_id
             and (rules.made_at(str(card.get("id") or "")) or recent) >= recent),
            key=lambda card: rules.made_at(str(card.get("id") or "")) or _NEVER, reverse=True,
        )
        if not targets:
            problems.append(f"There's no setup card for {day:%a %b %d} yet - make it and press again.")
            return done, problems
        target = targets[0]
        theirs = same_list(client.card_checklists(str(target.get("id") or "")), moved.checklist)
        if theirs is None:
            problems.append(f"{target.get('name')} has no {moved.checklist} list, so I left it where it is.")
            return done, problems
        still = same_list(client.card_checklists(moved.setup_card_id), moved.checklist) or {}
        line = next((one for one in still.get("checkItems") or [] if str(one.get("id") or "") == moved.item_id), None)
        if line is None:
            problems.append(f"{moved.agent} isn't on {moved.setup_card} any more - somebody moved them already.")
            return done, problems
        # Unticked where it lands: the setup's to be checked again for the new
        # day - "it should unticked those ticked card as well".
        client.add_check_item(str(theirs.get("id") or ""), moved.item_name, checked=False)
        client.remove_check_item(moved.checklist_id, moved.item_id)
        done.append(f"✅ Setup: {moved.setup_card} → **{target.get('name')}** ({moved.checklist})")

        # The Lead Order cards, if the spread already put them on the old day.
        short = trello.linked_card_id(moved.card_url) or ""
        old_title = moved.setup_card
        olds = [card for card in every
                if (dailyops.parse_card_title(str(card.get("name") or "")) or ("",))[0] == "lead_order"
                and any(rules.setup_covers(old_title, one)
                        for one in dailyops.card_days(str(card.get("name") or "")))]
        new_order = dailyops.cards_covering(every, day).get("lead_order")
        for old in olds:
            for checklist in client.card_checklists(str(old.get("id") or "")):
                for item in checklist.get("checkItems") or []:
                    said = str(item.get("name") or "")
                    if not short or trello.linked_card_id(said) != short:
                        continue
                    if new_order is None:
                        problems.append(f"No Lead Order card for {day:%a %b %d} yet, so their line is still "
                                        f"on {old.get('name')}.")
                        continue
                    place = same_list(client.card_checklists(str(new_order.get("id") or "")),
                                      str(checklist.get("name") or ""))
                    if place is None:
                        problems.append(f"{new_order.get('name')} has no “{checklist.get('name')}” list, so "
                                        f"their line is still on {old.get('name')}.")
                        continue
                    client.add_check_item(str(place.get("id") or ""), said, checked=False)
                    client.remove_check_item(str(checklist.get("id") or ""), str(item.get("id") or ""))
                    done.append(f"✅ Lead Order: {old.get('name')} → **{new_order.get('name')}** "
                                f"({checklist.get('name')})")

        # The Ads and Ops cards of the old day - "yes add ads/ops to the move
        # button too". An agent going live today is on today's Ads and Ops
        # lists; one going live later is on that day's setup card, which is
        # linked onto its Ads card. So: onto today's when the new day is
        # today, otherwise off the old day's - unless somebody already ticked
        # it, which is work done and theirs to clear.
        try:
            today = board_day(config)
        except AttributeError:
            today = date.today()
        for kind in ("ads", "ops"):
            olds_kind = [card for card in every
                         if (dailyops.parse_card_title(str(card.get("name") or "")) or ("",))[0] == kind
                         and any(rules.setup_covers(old_title, one)
                                 for one in dailyops.card_days(str(card.get("name") or "")))]
            new_card = dailyops.cards_covering(every, day).get(kind) if day == today else None
            for old in olds_kind:
                for checklist in client.card_checklists(str(old.get("id") or "")):
                    for item in checklist.get("checkItems") or []:
                        said = str(item.get("name") or "")
                        if not short or trello.linked_card_id(said) != short:
                            continue
                        if day == today:
                            if new_card is None or str(new_card.get("id") or "") == str(old.get("id") or ""):
                                continue
                            place = same_list(client.card_checklists(str(new_card.get("id") or "")),
                                              str(checklist.get("name") or ""))
                            if place is None:
                                problems.append(f"{new_card.get('name')} has no “{checklist.get('name')}” list, "
                                                f"so their line is still on {old.get('name')}.")
                                continue
                            client.add_check_item(str(place.get("id") or ""), said, checked=False)
                            client.remove_check_item(str(checklist.get("id") or ""), str(item.get("id") or ""))
                            done.append(f"✅ {dailyops.CARD_KINDS.get(kind, kind)}: {old.get('name')} → "
                                        f"**{new_card.get('name')}** ({checklist.get('name')})")
                        else:
                            client.remove_check_item(str(checklist.get("id") or ""), str(item.get("id") or ""))
                            done.append(f"✅ Off **{old.get('name')}** ({checklist.get('name')}) — they're on "
                                        f"the {day:%a %m/%d} setup card now")

        # And the card itself says the new day: Chance Duncan's "Launch Date:
        # Wednesday, October 21" stayed after "leads flowing starting
        # tomorrow" - "this should update the launch date".
        theirs_card = next((card for card in every if short and trello.linked_card_id(
            str(card.get("shortUrl") or card.get("url") or "")) == short), None)
        if theirs_card is not None and hasattr(client, "card_detail"):
            said = f"{day:%A, %B} {day.day}"
            try:
                desc = str(client.card_detail(str(theirs_card.get("id") or "")).get("desc") or "")
                line = re.search(r"^([ \t]*Launch\s*Date[ \t]*:)[ \t]*(.*)$", desc,
                                 re.MULTILINE | re.IGNORECASE)
                if line and " ".join(line.group(2).split()).casefold() != said.casefold():
                    client.set_description(str(theirs_card.get("id") or ""),
                                           desc[:line.start()] + f"{line.group(1)} {said}" + desc[line.end():])
                    done.append(f"✅ Their card: Launch Date → **{said}**")
            except Exception as exc:
                problems.append(f"Moved, but couldn't change the Launch Date on their card: {_short(exc, 120)}")
        return done, problems
    except Exception as exc:
        problems.append(f"Couldn't finish moving {moved.agent}: {_short(exc, 140)}")
        return done, problems
    finally:
        client.close()


def live_move_followup(config: Config, moved) -> tuple[list[str], list[str]]:
    """After an agent's day moved: their card unticked - the setup's to be
    checked again - and Therese told on it to put the hub right: "@therese,
    this person live date changed to ___ please update the hub". (done,
    problems)."""
    from .. import dailyops, trello

    done, problems = [], []
    short = trello.linked_card_id(moved.card_url) or ""
    client = open_trello(config)
    try:
        every = client.board_cards(config.secrets.trello_board_id)
        card = next((one for one in every if short and trello.linked_card_id(
            str(one.get("shortUrl") or one.get("url") or "")) == short), None)
        if card is None:
            return done, [f"Couldn't find {moved.agent}'s card to untick and comment on."]
        card_id = str(card.get("id") or "")
        if card.get("dueComplete"):
            client.tick_card(card_id, done=False)
            done.append(f"✅ {moved.agent}'s card unticked")
        known = dailyops.who_is_known(client.board_members(config.secrets.trello_board_id))
        therese = known.get("therese")
        day = f"{moved.live:%A, %B} {moved.live.day}"
        said = (f"@{therese} " if therese else "Therese - ") + f"{moved.agent}'s live date changed to **{day}**"
        if moved.setup_day is not None:
            said += f" - the setup says {moved.setup_day:%A, %B} {moved.setup_day.day}"
        said += ". Please update the hub."
        client.add_comment(card_id, said)
        done.append(f"✅ Told Therese on their card to update the hub to {day}"
                    + ("" if therese else " (nobody on the board answers to Therese - not tagged)"))
        return done, problems
    except Exception as exc:
        return done, problems + [f"Couldn't untick or comment on {moved.agent}'s card: {_short(exc, 140)}"]
    finally:
        client.close()


def describe_wrong_days(findings, *, most: int = 20) -> str:
    """What the sweep found, as one message."""
    if not findings:
        return "📅 Every line on the Lead Order cards is on a day its agent goes live. 👍"
    lines = [
        f"• **{one['card']} · {one['checklist']}** — {one['agent']} "
        f"“{one['label']}” is live {one['live']:%a %b %d}"
        + (" *(ticked)*" if one["ticked"] else "")
        for one in findings[:most]
    ]
    if len(findings) > most:
        lines.append(f"…and {len(findings) - most} more.")
    return (
        f"📅 {len(findings)} line(s) on a Lead Order card for a day the agent "
        "isn't live:\n" + "\n".join(lines)
        + "\nNothing moved — these are for somebody to move."
    )


def _card_said(client, card_id, cache: dict) -> str:
    """An agent card's description, read once however many lines they have.

    A card that can't be read comes back empty rather than raising: the spread
    is the thing that matters and Trello having a bad second is not a reason
    to stop it.
    """
    if not card_id:
        return ""
    if card_id not in cache:
        try:
            cache[card_id] = str(client.card_detail(card_id).get("desc") or "")
        except Exception:
            cache[card_id] = ""
    return cache[card_id]


def _setup_comment_said(client, card_id, cache: dict) -> str:
    """The lead type an agent card's Distro Hub confirmation says was set up,
    read once - "" when there's none, or the comments can't be read."""
    from .. import agents

    if not card_id:
        return ""
    key = f"setup:{card_id}"
    if key not in cache:
        try:
            cache[key] = agents.setup_said(client.card_comments(card_id))
        except Exception:
            cache[key] = ""
    return cache[key]


def _their_card_disagrees(client, card_id, on_setup: str):
    """(what their card says, what the setup card says), or None.

    One read of the agent's own card per line written, which is why it happens
    after the placement rather than for every agent considered. A card that
    can't be read is not a conflict - saying one because Trello had a bad
    second is how a real one stops being looked at.

    A card naming several orders is checked against all of them. Bethany
    Candace Herndon bought "15 OTP Trucker + 15 OTP Vets", was correctly given
    a line under each, and the trucker line was then read against her vets
    order and called a disagreement. Somebody who bought two things disagrees
    with neither of them.
    """
    from .. import agents as rules

    if not card_id:
        return None
    try:
        said = str(client.card_detail(card_id).get("desc") or "")
    except Exception:
        return None

    orders = rules.ordered_lead_types(said)
    if not orders:
        return rules.setup_conflict(rules.stated_lead_type(said), on_setup)
    if any(rules.setup_conflict(one, on_setup) is None for one in orders):
        return None
    # It matches none of them, so it is a real disagreement - and the whole
    # order is what to show, not whichever half was compared last.
    return rules.stated_orders(said), on_setup


def _by_url(cards: list[dict]) -> dict[str, str]:
    """Card name by URL, both the short and the long one.

    A checklist item stores whichever URL was copied, and "PHX 2.0 → PHX 2.0"
    is not something anybody can check. The agent's name is.
    """
    named: dict[str, str] = {}
    for card in cards or []:
        title = str(card.get("name") or "")
        for field in ("url", "shortUrl"):
            found = str(card.get(field) or "")
            if found:
                named[found] = title
    return named


def unspread_lead_order(
    config: Config, *, day=None, dry: bool = True
) -> tuple[list[str], list[str]]:
    """Take back off a Lead Order card the lines that came from a setup card.

    (what would go / went, problems). Reads only unless `dry` is False.

    The undo for a spread that landed on the wrong card, and the rule is only
    ever about *which* setup card. Matching a setup card at all proves nothing:
    every line the spread writes correctly matches one, which is the whole
    point of it. Asked to remove everything that matched any setup card, this
    took thirty wrong lines and twenty-seven right ones with them.

    So: the setup card that belongs with this Lead Order card is the one whose
    agents go live on its day. Lines matching *that* card stay. Lines matching
    some other setup card - agents whose day is not this card's day - go.
    """
    from .. import agents as rules, dailyops

    day = day or board_day(config)
    client = open_trello(config)
    try:
        lists = client.board_lists(config.secrets.trello_board_id)
        every = [c for bl in lists for c in client.list_cards(str(bl.get("id") or ""))]

        order = dailyops.cards_covering(every, day).get("lead_order")
        if order is None:
            return [], [], [
                f"No Lead Order card dated {day:%m/%d/%y} anywhere on the board."
            ]

        belongs = rules.find_setup_card(every, day)
        if belongs is None:
            return [], [
                f"No setup card covers {day:%m/%d/%y}, so I can't tell which lines "
                f"on that Lead Order card belong there and which don't."
            ]

        mine: set[str] = set()
        others: set[str] = set()
        for card in every:
            if not rules.is_setup_card(str(card.get("name") or "")):
                continue
            into = mine if card.get("id") == belongs.get("id") else others
            for checklist in client.card_checklists(str(card.get("id") or "")):
                for item in checklist.get("checkItems") or []:
                    text = " ".join(str(item.get("name") or "").split())
                    if text:
                        into.add(text)
        # A line on both cards is one this card's own agents own. The same
        # agent can be listed on two setup cards, and the day decides.
        others -= mine

        named = _by_url(every)
        found, problems = [], []
        for checklist in client.card_checklists(str(order.get("id") or "")):
            where = str(checklist.get("name") or "")
            for item in checklist.get("checkItems") or []:
                text = " ".join(str(item.get("name") or "").split())
                if text not in others:
                    continue
                who = named.get(rules.split_item(text)[0], text)
                if dry:
                    found.append(f"{who} — {where}")
                    continue
                try:
                    client.remove_check_item(
                        str(checklist.get("id") or ""), str(item.get("id") or "")
                    )
                except Exception as exc:
                    problems.append(f"{who} — {_short(exc, 160)}")
                    continue
                found.append(f"{who} — {where}")
        if found:
            found.insert(
                0,
                f"**{order.get('name')}** — keeping everything from "
                f"**{belongs.get('name')}**",
            )
        return found, problems
    finally:
        client.close()


def note_setup_on_lead_order(client, lists, setup_card: dict, day: date) -> str | None:
    """Put a setup card's link on the Lead Order card for the day it covers.

    The setup card goes to Done with the rest of Quality Check, ticked or not.
    This is the thread back to it: its agents go live on that day, so the Lead
    Order card for that day is the one somebody is looking at when they need
    to know who was set up and what they were sold - or which boxes never got
    ticked.

    Appended, never replaced. The description already carries the people it
    concerns, and losing that would be worse than not adding the link.
    Returns a problem to report, or None.
    """
    from .. import agents, dailyops, trello

    title = str(setup_card.get("name", ""))
    starts = agents.setup_starts(title, day)
    if starts is None:
        return None

    every = [c for bl in lists for c in client.list_cards(str(bl.get("id") or ""))]
    target = dailyops.cards_covering(every, starts).get("lead_order")
    if target is None:
        return f"{title} — no Lead Order card dated {starts:%m/%d/%y} to link it on"

    link = str(setup_card.get("url") or setup_card.get("shortUrl") or "")
    short = trello.linked_card_id(link)
    if not short:
        return None

    target_id = str(target.get("id") or "")
    try:
        held = str(client.card_detail(target_id).get("desc") or "")
        # By the card's own short id, not the whole URL: the slug on the end
        # is the title at the time it was pasted, and a link somebody added by
        # hand has a different one for the same card.
        if short in held:
            return None
        client.set_description(target_id, f"{held.rstrip()}\n\n{link}" if held.strip() else link)
    except Exception as exc:
        return f"{title} — couldn't link it on {target.get('name')}: {_short(exc, 120)}"
    return None


def walk_to(card: dict, step: str, day: date) -> str:
    """Which list a card lands in for one step of the walk, by name.

    Normally the step's own destination. The exception is a setup card that
    somebody dragged into Today before its working day: it goes back to In Que
    rather than on to Quality Check, so nine on the right morning puts it in
    Today again. A card in Quality Check is a card nobody is adding agents to.

    On its own working day it walks on with everything else - by six the
    setting up is done and its agents go live in the morning.
    """
    from .. import agents, dailyops

    to_name = dailyops.STEP_LISTS[step][1]
    if step != "to_quality_check":
        return to_name
    title = str(card.get("name", ""))
    if not agents.is_setup_card(title):
        return to_name
    worked = agents.setup_worked_on(title, day)
    return dailyops.IN_QUE if worked is not None and worked > day else to_name


def walk_board(config: Config, step: str, *, day=None) -> tuple[int, list[str]]:
    """Move everything in one list to the next. (moved, problems).

    The whole list rather than only the four dated cards - the lists *are* the
    day, and leaving the rest behind means somebody still walks the board by
    hand afterwards, which is the thing this replaces. Two exceptions, both in
    `walks_today`: a new agent's card, and a card dated for another day.

    What is in the list is nearly all it touches. Two things reach outside it,
    both in the evening: the setup card tomorrow works on is fetched from
    wherever it was made (`setups_to_pull`), and a setup card on its way to
    Done gets its link put on the Lead Order card for the day its agents go
    live (`note_setup_on_lead_order`).
    """
    from .. import agents, dailyops, rollskip, trello

    day = day or board_day(config)
    held = rollskip.for_day(day)
    from_name, to_name = dailyops.STEP_LISTS[step]
    client = open_trello(config)
    moved, problems = 0, []
    finished_setups = []
    try:
        lists = client.board_lists(config.secrets.trello_board_id)
        source = trello.find_list(lists, from_name)
        target = trello.find_list(lists, to_name)
        if source is None or target is None:
            missing = from_name if source is None else to_name
            return 0, [f"The board has no list called {missing!r}"]

        # Backwards, because each one goes to the top: move the last card
        # first and the first card ends up above it. The list arrives in the
        # order it left in rather than reversed.
        for card in reversed(client.list_cards(str(source.get("id") or ""))):
            if not walks_today(card, day, step=step, held=held):
                continue
            where = walk_to(card, step, day)
            landing = target if where == to_name else trello.find_list(lists, where)
            if landing is None:
                problems.append(
                    f"{card.get('name')} — the board has no list called {where!r}"
                )
                continue
            try:
                client.move_card(str(card.get("id") or ""), str(landing.get("id") or ""))
            except Exception as exc:
                problems.append(f"{card.get('name')} — {_short(exc, 160)}")
                continue
            moved += 1
            if step == "to_done" and agents.is_setup_card(str(card.get("name", ""))):
                finished_setups.append(card)

        # After the moves, so the card is already in Done when the Lead Order
        # card starts pointing at it.
        for card in finished_setups:
            problem = note_setup_on_lead_order(client, lists, card, day)
            if problem:
                problems.append(problem)

        # Last, so they land on top of In Que rather than under tomorrow's four.
        fetch, notes = setups_to_pull(client, lists, day, step)
        problems.extend(notes)
        for card in fetch:
            in_que = trello.find_list(lists, dailyops.IN_QUE)
            try:
                client.move_card(str(card.get("id") or ""), str(in_que.get("id") or ""))
                moved += 1
            except Exception as exc:
                problems.append(f"{card.get('name')} — {_short(exc, 160)}")
    finally:
        client.close()
    return moved, problems


def run_rollover(config: Config, *, day=None, only=None) -> tuple[int, list[str], list]:
    """Read the board and carry the items over. (moved, problems, flagged).

    The unattended version of what the button does. Everything unticked moves;
    `flagged` is the part of it worth a line in the night's message, which is
    an item that has been carried for days. Said, and moved with the rest.

    A card somebody held back is named in the problems, so the night's message
    says what did not happen as well as what did - a silent skip is a card
    nobody notices is still sitting there tomorrow.
    """
    from .. import dailyops, rollskip

    day = day or board_day(config)
    plans, missing, targets, ahead = read_rollover(config, day=day, only=only)
    moved, problems = apply_rollover(config, plans, targets, day=day)
    if ahead:
        # Not tomorrow's card. Said plainly, because "carried 14 items" reads
        # as onto tomorrow and this is the one night a week it isn't.
        landed = ", ".join(
            f"{dailyops.CARD_KINDS.get(kind, kind)} → {when:%a %b %d}"
            for kind, when in sorted(ahead.items())
        )
        problems.append(f"No card for tomorrow, so these went onto the next one: {landed}")
    if missing:
        problems.append(f"Nowhere to carry: {', '.join(missing)}")
    held = rollskip.for_day(day)
    if held:
        named = ", ".join(dailyops.CARD_KINDS.get(k, k) for k in held)
        problems.append(f"Held back at your request: {named}")
    flagged = [item for plan in plans for item in plan.needs_a_look]
    return moved, problems, flagged


# ------------------------------------------------- new agents going live


def read_agents(config: Config, *, day=None):
    """What should happen to every new agent card waiting in In Que.

    Reads only. The board is walked once and everything each plan needs
    travels with it, so what gets shown and what gets done cannot drift.
    """
    from .. import agents, dailyops, trello

    day = day or board_day(config)
    tomorrow = day + timedelta(days=1)
    client = open_trello(config)
    try:
        lists = client.board_lists(config.secrets.trello_board_id)
        by_name = {" ".join(str(bl.get("name") or "").split()).casefold(): bl for bl in lists}
        # In Que is where they land, Franklin's list is where they wait, and
        # Today is where one ends up if somebody drags it there - a card in a
        # list nothing watches is a card nothing will ever do anything about.
        watched = {
            name: trello.find_list(lists, name)
            for name in (agents.IN_QUE, agents.TODAY, agents.PARKED)
        }
        if watched[agents.IN_QUE] is None:
            return [], {}, [f"The board has no list called {agents.IN_QUE!r}"]
        parked_id = str((watched[agents.PARKED] or {}).get("id") or "\0")

        every_card = _cards_of(client, config.secrets.trello_board_id, lists)
        # Covering, not "dated": a Lead Order card titled 08/29-08/31 is the
        # Lead Order card on all three of those days.
        dated = dailyops.cards_covering(every_card, day)

        # Sifted out of the board read rather than fetched again. The watched
        # lists are already in `every_card`, and three more round trips is
        # three more seconds between a card landing and it being filed.
        waiting = {
            str(bl.get("id") or "") for bl in watched.values() if bl is not None
        }
        waiting_cards = [
            card for card in every_card if str(card.get("idList") or "") in waiting
        ]

        plans, typos = [], []
        for card in waiting_cards:
            title = str(card.get("name", ""))
            if not agents.is_agent_card(title):
                continue
            # Filed anyway, and said out loud. A title one letter out is
            # invisible to everything that reads the board, and tolerating it
            # in silence means it stays wrong and the next one is too.
            wrong = agents.misspelled_agent(title)
            if wrong:
                typos.append(
                    f"“{title}” says {wrong.upper()} where it should say AGENT. "
                    "Filed anyway — worth fixing on the card."
                )
            # One request for the comments and for whether this card was
            # copied from another - see `agents.still_being_written`. Both
            # read again only when the card has changed.
            detail, (said, copied) = _card_read(client, card)
            agent = agents.read_agent(
                {**card, **detail},
                text=str(detail.get("desc") or ""),
                comments=tuple(said),
                copied=copied,
                today=day,
            )
            if agent is None:
                continue
            plans.append(
                _plan_for(
                    client, agent, day=day, tomorrow=tomorrow, dated=dated,
                    every_card=every_card,
                    parked=str(card.get("idList") or "") == parked_id,
                )
            )

        # And the ones moved to Done before they were filed - Kevin Westbrook
        # and Isaiah Wright, set up by Nova and moved there inside a minute,
        # were never put on Ads or Ops. Made in the last day, on no setup,
        # Ads or Ops card, and not live yet.
        for card in _done_unfiled(client, lists, every_card, day):
            detail, (said, copied) = _card_read(client, card)
            agent = agents.read_agent({**card, **detail}, text=str(detail.get("desc") or ""),
                                      comments=tuple(said), copied=copied, today=day)
            if agent is None or agent.launch is None or agent.launch < day:
                continue
            plan = _plan_for(client, agent, day=day, tomorrow=tomorrow, dated=dated,
                             every_card=every_card, parked=False)
            if plan.doable:
                plan.move_to = ""
                plans.append(plan)

        where = {
            name: str(by_name[name.casefold()].get("id") or "")
            for name in (agents.PARKED, agents.AUTOMATION, agents.DONE)
            if name.casefold() in by_name
        }
        missing = [
            f"The board has no list called {name!r}"
            for name in (agents.PARKED, agents.AUTOMATION, agents.DONE)
            if name not in where
        ]
        # Deliberately not folded into `missing`. That list means the board is
        # not shaped the way the filing needs and nothing can be done at all,
        # and the handler stops on it - so a misspelled title would have
        # stopped every agent on the board from being filed, which is worse
        # than the typo by a wide margin.
        return plans, where, missing, typos
    finally:
        client.close()


#: New Agent cards in Done looked at for being filed: {card id: last activity}
#: - each looked at again only when it changes.
_DONE_LOOKED: dict = {}
#: How new a card in Done has to be to be looked at.
DONE_UNFILED_HOURS = 24
#: An unfiled one in Done is looked at again this often - it may still be
#: being written, and the filing waits for that.
DONE_RETRY_SECONDS = 120
_DONE_RETRY: dict = {}


def _done_unfiled(client, lists, every_card, day) -> list[dict]:
    """New Agent cards in Done, made in the last DONE_UNFILED_HOURS, whose
    link is on no setup, Ads or Ops card - filed by nobody."""
    from .. import agents, dailyops, trello

    done = trello.find_list(lists, agents.DONE)
    if done is None:
        return []
    since = datetime.now(timezone.utc) - timedelta(hours=DONE_UNFILED_HOURS)
    fresh = [card for card in every_card
             if str(card.get("idList") or "") == str(done.get("id") or "")
             and agents.is_agent_card(str(card.get("name") or ""))
             and (card_made_at(str(card.get("id") or "")) or since) > since
             and _DONE_LOOKED.get(str(card.get("id") or "")) != str(card.get("dateLastActivity") or "")]
    # Not filed yet: looked at again every couple of minutes, not every pass.
    import time as _time

    now = _time.monotonic()
    fresh = [card for card in fresh if now - _DONE_RETRY.get(str(card.get("id") or ""), -1e9) >= DONE_RETRY_SECONDS]
    if not fresh:
        return []
    for card in fresh:
        _DONE_RETRY[str(card.get("id") or "")] = now
    # Every line on the setup cards and the next week's Ads and Ops cards.
    holders = [card for card in every_card if agents.is_setup_card(str(card.get("name") or ""))]
    for ahead in range(0, 8):
        covering = dailyops.cards_covering(every_card, day + timedelta(days=ahead))
        holders += [covering[kind] for kind in ("ads", "ops") if covering.get(kind)]
    lines = []
    for holder in {str(one.get("id") or ""): one for one in holders}.values():
        try:
            for checklist in client.card_checklists(str(holder.get("id") or "")):
                lines += [str(item.get("name") or "") for item in checklist.get("checkItems") or []]
        except Exception:
            return []  # can't tell - file nobody twice
    found = []
    for card in fresh:
        links = {str(card.get(key) or "") for key in ("url", "shortUrl")} - {""}
        short = trello.linked_card_id(str(card.get("shortUrl") or card.get("url") or ""))
        if short:
            links.add(short)
        if any(link in line for line in lines for link in links):
            # Filed: not looked at again until it changes.
            _DONE_LOOKED[str(card.get("id") or "")] = str(card.get("dateLastActivity") or "")
        else:
            found.append(card)
    return found


def _cards_of(client, board_id, lists) -> list[dict]:
    """Every card in these lists, top to bottom - in one request where the
    client can, a request a list where it can't."""
    if hasattr(client, "cards_in") and board_id:
        return client.cards_in(str(board_id), lists)
    return [card for bl in lists for card in client.list_cards(str(bl.get("id") or ""))]


#: Waiting agent cards as last read: {card id: (last activity, read at,
#: detail, story)}. They are read every twenty seconds and change rarely.
_CARD_READS: dict = {}
_CARD_READS_LOCK = threading.Lock()

#: Read again after this long even if Trello says nothing has changed.
CARD_READ_SECONDS = 300


def _card_read(client, card: dict, *, now: float | None = None):
    """(detail, (said, copied)) for a waiting agent card.

    The same as last time while the card's last activity is the same - an
    edited description or a new comment changes it - and read again anyway
    every five minutes. A card parked for a week was read twice every twenty
    seconds, which was most of RYTE's Trello traffic.
    """
    import time

    now = time.time() if now is None else now
    card_id = str(card.get("id") or "")
    activity = str(card.get("dateLastActivity") or "")
    with _CARD_READS_LOCK:
        held = _CARD_READS.get(card_id)
    if held and activity and held[0] == activity and now - held[1] < CARD_READ_SECONDS:
        return held[2], held[3]
    detail = client.card_detail(card_id)
    story = client.card_story(card_id)
    with _CARD_READS_LOCK:
        _CARD_READS[card_id] = (activity, now, detail, story)
        if len(_CARD_READS) > 500:
            for old in sorted(_CARD_READS, key=lambda one: _CARD_READS[one][1])[:100]:
                _CARD_READS.pop(old, None)
    return detail, story


def _plan_for(client, agent, *, day, tomorrow, dated, every_card, parked=False):
    """One agent's plan, with the cards and checklists it needs already found."""
    from .. import agents as rules
    from .. import dailyops

    plan = rules.AgentPlan(agent=agent, when=agent.when(day))

    # Already set up: today's job whatever the date says, or with no date at
    # all, and done already for everybody but Nicole and Kathleen - theirs are
    # left for them to tick.
    if agent.set_up:
        plan.when = "today"
        if not (agent.lead_type or agent.stated):
            plan.problems.append(rules.cannot_read(
                dataclasses.replace(agent, launch=day), needs_lead_type=True))
            return plan
        _same_day_steps(client, plan, agent, day=day, dated=dated,
                        every_card=every_card, label=agent.stated)
        left = {one.casefold() for one in rules.SET_UP_LEFT_FOR}
        ticked = []
        for step in plan.steps:
            step.checked = " ".join(step.checklist.split()).casefold() not in left
            if step.checked and step.checklist not in ticked:
                ticked.append(step.checklist)
        said = "already set up — " + (
            f"ticked for {', '.join(ticked)}, " if ticked else "")
        said += "left for Nicole and Kathleen"
        plan.note = f"{said}; {plan.note}" if plan.note else said
        plan.move_to = rules.DONE
        return plan

    # Without a launch date there is nothing to decide. It still needs a
    # person, and it waits in Franklin's list while it does - "move it to my
    # section and he'll try again when launch date is available". His list is
    # read on every pass exactly like In Que, so the moment the date is filled
    # in the card is picked up and filed, with nobody having to remember it
    # was waiting. Left where it is if it is already there.
    if agent.launch is None:
        plan.problems.append(rules.cannot_read(agent, needs_lead_type=False))
        plan.park_to = "" if parked else rules.PARKED
        return plan

    # Before anything is decided on the date, because everything is: which
    # setup card they go on, and whether they park or get filed now.
    if agent.note:
        plan.problems.append(agent.note)
        return plan

    # Two orders on two days are two jobs, each done by its own date. Garret
    # Sekelsky bought "25 OTP Vets - live Friday, September 11" and "25 OTP
    # FEX - live Saturday, September 12", and both went onto Friday's card
    # because the card was read for one launch date. Alex Olsen's "15 OTP VET
    # - Live Tuesday" and "21 OTP WIDOW - Live Monday EOD" landed on the
    # Monday: the Tuesday order is the Tuesday setup card's, and the Monday
    # one is the same-day job on Monday's Ops and Ads - it had been looking
    # for a Monday setup card, and held the whole card up for want of one.
    orders = rules.dated_orders(agent.said, today=day)
    if rules.on_several_days(orders):
        problem = rules.cannot_read(agent, needs_lead_type=True)
        if problem:
            plan.problems.append(problem)
            return plan
        waiting = []
        for order, when in orders:
            when = when or agent.launch
            if when <= day:
                _same_day_steps(client, plan, agent, day=day, dated=dated,
                                every_card=every_card, label=order)
                continue
            its_card = rules.find_setup_card(every_card, when)
            if its_card is None:
                waiting.append(f"{order} — live {when:%a %b %d}")
                continue
            where = str(its_card.get("name", ""))
            its_id = str(its_card.get("id") or "")
            its_held = client.card_checklists(its_id)
            for person in rules.SETUP_PEOPLE:
                plan.steps.append(_step(
                    where, its_id, person, agent, its_held,
                    exact=True, label=order, when=when,
                ))
        if waiting and not plan.steps and not plan.problems:
            # Nothing to put any of it on yet: waits in Franklin's list, as a
            # card with one date does, and is picked up when the cards exist.
            plan.move_to = "" if parked else rules.PARKED
            return plan
        if waiting:
            # Nothing is filed while an order is still waiting, and the card
            # stays put, so it is all picked up together when its card exists.
            # Moving to Done here would take the card away with an order
            # still unfiled on it.
            plan.problems.append(
                f"{agent.name} — no setup card yet for "
                + "; ".join(waiting)
            )
            plan.move_to = "" if parked else rules.PARKED
        elif not plan.problems:
            plan.move_to = rules.DONE
        return plan

    if plan.when != "today":
        # Whether they can be done yet turns on whether the card they go on
        # exists, not on how far off the launch is. A Thursday card made on
        # Tuesday is a Thursday card, and the agents for it can go on now.
        card = rules.find_setup_card(every_card, agent.launch)
        if card is None:
            # The day's own Ads and Ops cards, when they're already up and no
            # setup card covers it any more - Thomas DeCarlo, Sean Unzueta and
            # Justice Hartford, live Monday, came in on the weekend after the
            # Saturday-Monday setup card was done and gone, and waited in
            # Franklin's list for a card that was never coming. Done the way
            # a same-day agent is.
            theirs = dailyops.cards_covering(every_card, agent.launch)
            if theirs.get("ads") and theirs.get("ops") and (agent.launch - day).days <= 3:
                problem = rules.cannot_read(agent, needs_lead_type=True)
                if problem:
                    plan.problems.append(problem)
                    return plan
                _same_day_steps(client, plan, agent, day=agent.launch, dated=theirs,
                                every_card=every_card, label=agent.stated)
                plan.move_to = rules.DONE
                return plan
            # Nothing to put them on yet. The setup cards are made on their
            # own schedule at eleven; making a second one here would split a
            # day's agents across two cards, which is worse than waiting.
            # Wait in Franklin's list, and leave one already there exactly
            # where it is rather than moving it to the list it is in.
            plan.move_to = "" if parked else rules.PARKED
            return plan

        problem = rules.cannot_read(agent, needs_lead_type=True)
        if problem:
            plan.problems.append(problem)
            return plan

        title = str(card.get("name", ""))
        card_id = str(card.get("id") or "")
        held = client.card_checklists(card_id)
        for person in rules.SETUP_PEOPLE:
            plan.steps.append(_step(
                title, card_id, person, agent, held, exact=True,
                label=agent.stated,
            ))
        plan.move_to = rules.DONE
        return plan

    problem = rules.cannot_read(agent, needs_lead_type=True)
    if problem:
        plan.problems.append(problem)
        return plan

    _same_day_steps(client, plan, agent, day=day, dated=dated,
                    every_card=every_card, label=agent.stated)
    plan.move_to = rules.DONE
    return plan


def _same_day_steps(client, plan, agent, *, day, dated, every_card, label) -> None:
    """Onto the Ads and Ops cards: the job for somebody going live today.

    Not the Lead Order card. Somebody turning up on the morning of their own
    launch is a card the team is working now, not an order to be filed under
    its lead type - "when we have a new agent going live same day ... dont do
    that anymore, just add it on ops and ads". The Lead Order card is still
    written by `spread_to_lead_order`, from the setup card, when it is asked
    for.

    At the weekend there are no Ops or Ads cards - nobody is at the board -
    and a same-day card that lands on a Saturday ("add it to his current
    order") is the next working day's job. Derrick Sepe's sat in In Que all
    weekend for want of an Ads card dated Saturday. Only at the weekend: on a
    weekday a missing card is one Zapier missed, and is said.
    """
    from .. import agents as rules
    from .. import dailyops

    if day.weekday() >= dailyops.SATURDAY and not (dated.get("ads") and dated.get("ops")):
        for ahead in range(1, dailyops.LOOK_AHEAD):
            when = day + timedelta(days=ahead)
            there = dailyops.cards_covering(every_card, when)
            if there.get("ads") and there.get("ops"):
                dated = there
                plan.note = (f"no Ops or Ads cards at the weekend, so onto "
                             f"{when:%A}'s ({when:%m/%d})")
                break
    for kind, people in (("ads", rules.ADS_PEOPLE), ("ops", rules.OPS_PEOPLE)):
        card = dated.get(kind)
        if card is None:
            said = (f"No {dailyops.CARD_KINDS.get(kind, kind)} card dated "
                    f"{day:%m/%d/%y} anywhere on the board")
            if said not in plan.problems:
                plan.problems.append(said)
            continue
        card_id = str(card.get("id") or "")
        held = client.card_checklists(card_id)
        title = str(card.get("name", ""))
        for person in people:
            plan.steps.append(_step(
                title, card_id, person, agent, held, exact=True,
                label=label,
            ))


def _step(card_title, card_id, checklist, agent, held, *, exact, label="", when=None):
    """One line onto one checklist, knowing whether that checklist exists.

    `label` is what the line says the leads are, which is not always the Lead
    Type field: a card whose body names the tier outright is filed by what the
    body said, and the line should say the same thing.

    A setup card covering several days gets the day on the end of the line.
    The Friday card runs Saturday to Monday, and three days of agents on one
    checklist all look alike until you open each card to find out which is
    which.
    """
    from .. import agents as rules

    names = {" ".join(str(c.get("name") or "").split()).casefold() for c in held or []}
    return rules.Step(
        card_title=card_title,
        card_id=card_id,
        checklist=checklist,
        item=rules.checklist_item(
            agent.url,
            label or agent.lead_type,
            day=rules.day_label(when or agent.launch) if rules.spans_days(card_title) else "",
        ),
        make_checklist=" ".join(checklist.split()).casefold() not in names,
    )


def apply_agents(config: Config, plans, where) -> tuple[int, list[str]]:
    """Carry the plans out. (agents filed, problems).

    Each agent is finished before the next is started, and the card only moves
    once every line it needed is on. A half-filed agent that got moved to Done
    is one nobody will ever notice is half-filed.
    """
    from .. import agents as rules

    client = open_trello(config)
    filed, problems = 0, []
    try:
        for plan in plans:
            if not plan.doable:
                continue
            try:
                _carry_out(client, plan, where)
            except Exception as exc:
                problems.append(f"{plan.agent.name} — {_short(exc, 160)}")
                continue
            filed += 1
    finally:
        client.close()
    return filed, problems


def park_agents(config: Config, plans, where) -> tuple[list[str], list[str]]:
    """Move the cards that need a person to where they wait. (moved, problems).

    Not filing. Nothing is written onto a checklist and nothing is marked
    done: the card is put where Franklin will see it, and his list is read on
    every pass, so the day somebody fills in the launch date it is picked up
    on its own.

    One card failing to move is said and the rest still move. A card that
    stays in In Que is where it started, which is the same place it would be
    if this had never run.
    """
    wanted = [one for one in plans if one.park_to and one.park_to in where]
    if not wanted:
        return [], []

    client = open_trello(config)
    moved, problems = [], []
    try:
        for plan in wanted:
            try:
                client.move_card(plan.agent.card_id, where[plan.park_to])
            except Exception as exc:
                problems.append(
                    f"{plan.agent.name} — couldn't move the card: {_short(exc, 160)}"
                )
                continue
            moved.append(plan.agent.name)
    finally:
        client.close()
    return moved, problems


def _carry_out(client, plan, where):
    """One agent, all of it, or an exception and the card left where it is.

    Nothing here makes a card. The setup cards are made on their own schedule
    at eleven and the daily four by Zapier - RYTE writes onto them and moves
    them, and an agent whose card does not exist yet waits rather than getting
    a second one made for them.
    """
    from .. import agents as rules

    for step in plan.steps:
        # Read again rather than trusting the plan: an agent filed a minute
        # ago may have made the very checklist this one is looking for.
        held = client.card_checklists(step.card_id)
        by_name = {
            " ".join(str(c.get("name") or "").split()).casefold(): str(c.get("id") or "")
            for c in held or []
        }
        # Per checklist, not per card. The same line goes to Therese and to
        # Kathleen and to Nicole, and a set of every item on the whole card
        # makes the second and third look like duplicates of the first.
        already = {
            (
                " ".join(str(c.get("name") or "").split()).casefold(),
                " ".join(str(item.get("name") or "").split()),
            )
            for c in held or [] for item in c.get("checkItems") or []
        }
        key = " ".join(step.checklist.split()).casefold()
        if (key, " ".join(step.item.split())) in already:
            continue
        if key not in by_name:
            made = client.create_checklist(step.card_id, step.checklist)
            by_name[key] = str(made.get("id") or "")
        if step.checked:
            client.add_check_item(by_name[key], step.item, checked=True)
        else:
            client.add_check_item(by_name[key], step.item)

    if plan.move_to and plan.move_to in where:
        client.move_card(plan.agent.card_id, where[plan.move_to])


def rollover_plan(config: Config, *, day=None) -> str:
    """What the evening rollover would move, as words. Writes nothing."""
    from .. import dailyops

    plans, missing, _, _ = read_rollover(config, day=day)
    report = dailyops.summarise(plans)
    if missing:
        report += f"\n⚠ No card for tomorrow yet: {', '.join(missing)}"
    return report


def read_rollover(config: Config, *, day=None, only=None, skip=None):
    """(plans, cards missing for tomorrow, where each item goes).

    Read-only, and the same read the write uses - so what gets shown and what
    gets done cannot drift apart. The third value carries the target card ids
    and checklists, because working them out twice is how a rollover ends up
    writing to a card nobody was shown.
    """
    from .. import carried, dailyops, rollskip

    day = day or board_day(config)
    # Cards somebody told to stay put tonight. Read here rather than passed
    # in, so the eight o'clock run and the button honour the same instruction.
    held = set(rollskip.for_day(day) if skip is None else skip)
    tomorrow = dailyops.next_day(day)
    client = open_trello(config)
    try:
        lists = client.board_lists(config.secrets.trello_board_id)
        cards = [
            card for bl in lists for card in client.list_cards(str(bl.get("id") or ""))
        ]
        today_cards = dailyops.cards_for(cards, day)
        tomorrow_cards = dailyops.cards_for(cards, tomorrow)
        history = carried.history()

        # One kind ("rollover ads"), or several (the two-o'clock step).
        if only:
            wanted = {only} if isinstance(only, str) else set(only)
            today_cards = {k: v for k, v in today_cards.items() if k in wanted}
        if held:
            today_cards = {k: v for k, v in today_cards.items() if k not in held}

        plans, targets = [], {}
        carried_onto: dict[str, date] = {}
        for kind, card in today_cards.items():
            # Tomorrow's card when there is one, and otherwise the next one
            # there is. On a Friday there is no Saturday General card, and
            # refusing to carry leaves the work on a card that goes to Done
            # twenty minutes later.
            found = dailyops.carry_onto(cards, kind, tomorrow)
            if found is None:
                continue
            target, lands_on = found
            carried_onto[kind] = lands_on
            target_id = str(target.get("id") or "")
            target_lists = client.card_checklists(target_id)
            plans.append(
                dailyops.plan_rollover(
                    kind,
                    source_card=card,
                    source_checklists=client.card_checklists(str(card.get("id") or "")),
                    target_card=target,
                    target_checklists=target_lists,
                    history=history,
                )
            )
            targets[kind] = (target_id, target_lists)

        # Named with a reason. "No card for tomorrow" is true and useless when
        # the card is sitting right there with the wrong year on it - and it is
        # only worth saying at all about a kind with nowhere to go, now that a
        # missing tomorrow is carried onto the next day instead.
        missing = [
            f"{dailyops.CARD_KINDS.get(kind, kind)} ({dailyops.why_missing(cards, kind, tomorrow)})"
            for kind in today_cards
            if kind not in carried_onto
        ]
        # Which kinds landed on a day that isn't tomorrow, so the night's
        # message can say so rather than looking like it moved them silently.
        ahead = {
            kind: when for kind, when in carried_onto.items() if when != tomorrow
        }
        return plans, missing, targets, ahead
    finally:
        client.close()


def apply_rollover(config: Config, plans, targets, *, day=None) -> tuple[int, list[str]]:
    """Write the carried items onto tomorrow's cards. Returns (moved, problems).

    Everything still unticked goes across. An item that has been carried for
    days is named in the report and moved with the rest: work not done is work
    not done, and a card that decides for itself which of it to leave behind is
    a card the team has to check by hand.

    A checklist item that links to another card is stored as that card's URL,
    and Trello renders the name and badge from it. So the raw `name` goes
    across untouched: copying the rendered label produces dead text, and the
    badge stops updating.

    An item already on tomorrow's card is not added again. Running this twice
    in an evening is somebody checking their work, and the cost of not
    guarding it is sixty-two items becoming a hundred and twenty-four - which
    nobody would unpick by hand, they would just delete the card.

    What is already there is read again here rather than taken from the plan.
    The plan is a snapshot from whenever somebody last looked, and a button
    sitting unclicked in a channel is exactly how it goes stale: run one card
    on its own, then press the older all-four button, and that card's items go
    on twice.
    """
    from .. import carried, dailyops
    from ..trello import no_secrets

    day = day or board_day(config)
    client = open_trello(config)
    moved, problems = 0, []
    counted: list[str] = []
    try:
        for plan in plans:
            target_id, _stale = targets.get(plan.kind, ("", []))
            if not target_id:
                problems.append(f"{dailyops.CARD_KINDS.get(plan.kind, plan.kind)}: no card for tomorrow")
                continue

            try:
                target_lists = client.card_checklists(target_id)
            except Exception as exc:
                problems.append(
                    f"{dailyops.CARD_KINDS.get(plan.kind, plan.kind)} — "
                    f"couldn't re-read tomorrow's card: {_short(exc, 160)}"
                )
                continue

            by_person = {
                " ".join(str(c.get("name") or "").split()).casefold(): str(c.get("id") or "")
                for c in target_lists or []
            }
            # Compared as they're written onto the board: a line with a key in
            # it goes on with the key hidden, and held up against the line as
            # it was it never matched - "[hidden — a key or token]" went onto
            # Therese's General checklist once for every run.
            already = {
                (
                    " ".join(str(c.get("name") or "").split()).casefold(),
                    " ".join(no_secrets(str(existing.get("name") or "")).split()),
                )
                for c in target_lists or []
                for existing in c.get("checkItems") or []
            }
            for item in plan.carried:
                key = " ".join(item.person.split()).casefold()
                if (key, " ".join(no_secrets(item.name).split())) in already:
                    continue
                already.add((key, " ".join(no_secrets(item.name).split())))
                try:
                    if key not in by_person:
                        made = client.create_checklist(target_id, item.person)
                        by_person[key] = str(made.get("id") or "")
                    client.add_check_item(by_person[key], item.name)
                except Exception as exc:
                    problems.append(f"{item.person} — {_short(exc, 160)}")
                    continue
                moved += 1
                counted.append(dailyops.item_key(plan.kind, item.person, item.name))
    finally:
        client.close()

    # Counted after the writes, so a run that failed halfway doesn't age items
    # it never moved.
    if counted:
        carried.record(counted, day)
    return moved, problems


def remove_duplicate_items(config: Config, *, day=None) -> tuple[list[str], list[str]]:
    """The same line twice on one person's checklist, on the day cards from
    today on - the second and after taken off, the first left as it is.
    "WHAT ARE THESE? SI THIS RYTE?": the hidden Auth/SID line, once for every
    carry-over. Only exact copies on the same checklist. (removed, problems)."""
    from .. import dailyops

    day = day or board_day(config)
    client = open_trello(config)
    removed, problems = [], []
    try:
        lists = client.board_lists(config.secrets.trello_board_id)
        every = _cards_of(client, config.secrets.trello_board_id, lists)
        for card in every:
            key = dailyops.parse_card_title(str(card.get("name") or ""))
            if not key or key[1] < day - timedelta(days=3):
                continue
            for checklist in client.card_checklists(str(card.get("id") or "")):
                seen = set()
                for item in checklist.get("checkItems") or []:
                    name = " ".join(str(item.get("name") or "").split())
                    if name not in seen:
                        seen.add(name)
                        continue
                    try:
                        client.remove_check_item(str(checklist.get("id") or ""), str(item.get("id") or ""))
                    except Exception as exc:
                        problems.append(f"{card.get('name')} · {checklist.get('name')} — {_short(exc, 100)}")
                        continue
                    removed.append(f"{card.get('name')} · {checklist.get('name')} — {name[:70]}")
    finally:
        client.close()
    return removed, problems


# ---------------------------------------------------------- the SOP library

SOP_ICON = "📘"


def sop_summary(config: Config, sop) -> str:
    """A short write-up of what an SOP covers, from whatever can be read.

    The summary is not decoration here - it is what "do we have an SOP about
    lead forms" gets matched against, so a card without one is a card nobody
    finds. Every source that can be read is read; the ones that can't say so.
    """
    from ..copywriter import CopywriterError

    material: list[str] = []
    if sop.body.strip():
        material.append(f"What was written with it:\n{sop.body.strip()}")

    # Whether anything of substance was read, as opposed to a page describing
    # itself. A card summarised from og: tags alone comes out as a paragraph
    # about how there is nothing to summarise, which is honest and useless.
    read_something = bool(sop.body.strip())

    if sop.kind == "YouTube":
        try:
            video = youtube.video_from_link(sop.url)
            if not sop.named_by_hand and getattr(video, "title", ""):
                sop.title = video.title[:120]
            material.append(youtube.fetch_transcript(video.video_id).text)
            read_something = True
        except Exception as exc:
            sop.note = f"Couldn't read the video: {_short(exc)}"
    elif sop.kind == "Loom":
        spoken, told = loom_spoken(sop)
        if spoken:
            material.append(f"What was said in the video:\n{spoken}")
            read_something = True
        elif told:
            sop.note = told
    elif sop.url:
        described = describe_page(sop.url)
        if described:
            material.append(f"What the page says about itself:\n{described}")
            # Nobody typed a heading, so the page's own name is the card's.
            # Otherwise a bare Google Docs link files as "SOP: Drive SOP", and
            # three of those are indistinguishable.
            if not sop.named_by_hand:
                sop.title = described.splitlines()[0][:120]
        elif not sop.body.strip() and not sop.images:
            sop.note = (
                f"{sop.kind} pages can't be read from here, so this is filed under "
                "its title and link."
            )

    if sop.audio and not material:
        sop.note = "A voice note can't be transcribed from here — filed under its title."

    if not material and not sop.images:
        return ""

    if not read_something and not sop.images:
        # Nothing was read, so there is nothing to summarise. One line saying
        # what it is beats four paragraphs saying what it isn't, and it is
        # what a search will match on.
        return f"{sop.kind} recording: {sop.title}. Not transcribed — watch the link."

    try:
        summary = write_sop_summary(config, sop, "\n\n".join(material))
    except CopywriterError as exc:
        sop.note = f"No summary — {exc}"
        return ""

    # Last and best chance at a name. A recording nobody titled leaves the
    # card called "SOP: Loom SOP", and once the thing has been read, what it
    # turned out to be about beats every other guess at what to call it.
    if not sop.named_by_hand:
        from .. import sops as sops_rules

        named = sops_rules.headline(summary)
        if named:
            sop.title = named
    return summary


def loom_spoken(sop) -> tuple[str, str]:
    """(what was said, what to say if it wasn't) for a Loom link.

    Loom captions every video and serves them publicly, so most of these read
    properly. The ones that don't are private, or too new to have been
    processed, and the difference is worth putting on the card - "ask whoever
    posted it to make it shareable" is actionable and "no summary" isn't.
    """
    from .. import loom

    try:
        spoken = loom.transcript(sop.url)
    except loom.LoomError as exc:
        return "", (
            f"Couldn't read the Loom — {_short(exc, 160)}. If it's private, "
            "sharing it with anyone-with-the-link is enough."
        )

    # Loom's own name for the video, or failing that the name its share page
    # publishes. Either beats the placeholder; the summary may beat both, and
    # gets the last word once it has been written.
    if not sop.named_by_hand:
        described = describe_page(sop.url).splitlines()
        named = loom.title(sop.url) or (described[0] if described else "")
        if named:
            sop.title = named[:120]

    if not spoken:
        return "", "Loom has no captions for this one yet — filed under its title and link."
    return spoken, ""


def describe_page(url: str, *, timeout: float = 15.0) -> str:
    """A page's own title and description, for a link that can't be read properly.

    Loom, Drive and the rest all publish og: tags. It is not a transcript, but
    "Creating Lead Form (Internal Strategy)" is a great deal more findable than
    a bare URL.
    """
    import html as _html
    import re as _re

    import httpx

    try:
        response = httpx.get(
            url,
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; RYTE/1.0)", "Accept": "text/html"},
        )
    except httpx.HTTPError:
        return ""
    if response.status_code >= 400:
        return ""

    found = []
    for prop in ("og:title", "og:description", "description"):
        match = _re.search(
            rf"<meta[^>]+(?:property|name)=[\"']{prop}[\"'][^>]+content=[\"']([^\"']+)",
            response.text,
            _re.IGNORECASE,
        )
        if match:
            # og: tags are HTML, so "&" arrives as "&amp;" - and went straight
            # onto a card called "Buying Your GHL Phone Number &amp; Calling
            # Numbers".
            text = " ".join(_html.unescape(match.group(1)).split())
            if text and text.casefold() not in ("undefined", "none") and text not in found:
                found.append(text)
    return "\n".join(found)[:2000]


def write_sop_summary(config: Config, sop, material: str) -> str:
    """Ask Claude what this procedure covers, reading screenshots where there are any."""
    from ..copywriter import CopywriterError

    from anthropic import Anthropic

    config.secrets.require("anthropic_api_key")
    client = Anthropic(api_key=config.secrets.anthropic_api_key)

    content: list[dict] = []
    for url in sop.images[:4]:
        content.append({"type": "image", "source": {"type": "url", "url": url}})
    content.append({
        "type": "text",
        "text": (
            f"This was posted in our team's SOP channel, titled “{sop.title}” "
            f"({sop.kind}).\n\n{material}\n\n"
            "Write a short entry for an internal SOP library. Open with one or "
            "two sentences saying what this procedure is for and when somebody "
            "would need it, then '- ' bullets for the steps or key points. Put "
            "any section title on its own line in **bold**. Say only what the "
            "material supports — if it is thin, keep the entry short rather "
            "than inventing steps."
        ),
    })

    try:
        response = client.messages.create(
            model=config.copy.model,
            max_tokens=2000,
            system=(
                "You write entries for an internal SOP library at a lead-generation "
                "company. Be concrete and practical. Never invent a step that isn't "
                "in the material you were given."
            ),
            messages=[{"role": "user", "content": content}],
        )
    except Exception as exc:
        raise CopywriterError(f"Claude couldn't read it: {_short(exc)}") from exc

    written = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    ).strip()
    if not written:
        raise CopywriterError(
            f"Claude returned nothing (stop reason: {getattr(response, 'stop_reason', 'unknown')})"
        )
    return written


def file_sop(config: Config, sop, *, summary: str = "") -> tuple[str, str]:
    """Put one SOP in the Notion library. Returns (title, page url)."""
    from .. import notion, sops

    config.secrets.require("notion_token", "notion_sop_page_id")
    page_id = config.secrets.notion_sop_page_id
    client = notion.NotionClient(config.secrets.notion_token)
    try:
        database_id = client.find_child_database(page_id)
        if not database_id:
            database_id = client.create_database(
                page_id, "SOPs", sops.database_schema()
            )
        client.add_columns(database_id, sops.EXTRA_COLUMNS)

        title = sops.card_title(sop)
        cover_url, icon_url = gallery_art(config, client, page_id)
        created = client.create_page(
            database_id,
            sops.map_properties(
                (client.database(database_id).get("properties") or {}),
                sop,
                title,
                summary=summary,
            ),
            children=sops.page_blocks(sop, summary),
            cover_url=cover_url,
            icon_url=icon_url,
            icon_emoji=None if icon_url else SOP_ICON,
        )
    finally:
        client.close()
    return title, str(created.get("url") or "")


def find_sops(config: Config, topic: str, *, limit: int = 5) -> list["sops.Hit"]:
    """SOPs matching a topic somebody asked about."""
    from .. import notion, sops

    config.secrets.require("notion_token", "notion_sop_page_id")
    client = notion.NotionClient(config.secrets.notion_token)
    try:
        database_id = client.find_child_database(config.secrets.notion_sop_page_id)
        if not database_id:
            return []
        rows = client.query_database(database_id)
    finally:
        client.close()
    return sops.matching_rows(rows, topic, limit=limit)


# --------------------------------- reading the SOPs that already existed

# The old library holds a great deal, and none of it needs to be held at once.
# Each page is read once, reduced to a couple of lines, and the index is what
# questions are matched against afterwards.

INDEX_MAX_PAGES = 150
INDEX_MAX_DEPTH = 3


def walk_library(client, page_id: str, *, depth: int = 0, seen=None) -> list[tuple[str, str]]:
    """(page id, title) for every page under a Notion page, breadth first.

    Bounded on both axes. A library that nests four deep and runs to hundreds
    of pages is a library where the top three levels are the useful ones, and
    an unbounded walk is how a one-off read turns into an afternoon.
    """
    from .. import notion

    seen = seen if seen is not None else set()
    found: list[tuple[str, str]] = []
    if depth > INDEX_MAX_DEPTH or len(seen) >= INDEX_MAX_PAGES:
        return found

    for block in client.children(page_id):
        if len(seen) >= INDEX_MAX_PAGES:
            break
        kind = block.get("type")
        block_id = str(block.get("id") or "")

        if kind == "child_page" and block_id not in seen:
            seen.add(block_id)
            title = str((block.get("child_page") or {}).get("title") or "").strip()
            found.append((block_id, title or "(untitled)"))
            found.extend(walk_library(client, block_id, depth=depth + 1, seen=seen))

        elif kind == "child_database":
            for row in client.query_database(block_id):
                row_id = str(row.get("id") or "")
                if not row_id or row_id in seen:
                    continue
                seen.add(row_id)
                found.append((row_id, notion.page_title(row).strip() or "(untitled)"))

    return found


def summarise_page(config: Config, title: str, text: str) -> str:
    """Two lines saying what a page covers, for matching a question against.

    Short on purpose. This is an index entry, not a replacement for the page -
    somebody who asks gets the link and reads the real thing.
    """
    from ..copywriter import CopywriterError

    if not (text or "").strip():
        return ""

    from anthropic import Anthropic

    config.secrets.require("anthropic_api_key")
    client = Anthropic(api_key=config.secrets.anthropic_api_key)
    try:
        response = client.messages.create(
            model=config.copy.model,
            max_tokens=300,
            system=(
                "You write one-line index entries for an internal SOP library. "
                "Say what the document covers and when somebody would need it. "
                "Two sentences at most. No preamble."
            ),
            messages=[{"role": "user", "content": f"Page title: {title}\n\n{text[:12000]}"}],
        )
    except Exception as exc:
        raise CopywriterError(f"Claude couldn't read “{title}”: {_short(exc)}") from exc

    return "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    ).strip()


def index_library(config: Config, page_id: str, *, limit: int = 40) -> tuple[int, int, int]:
    """Read the old SOP page and index what's in it.

    Returns (indexed, skipped, remaining). Pages already indexed are skipped,
    so this can be run again to carry on where it stopped.
    """
    from .. import notion, sops

    config.secrets.require("notion_token")
    index = sops.load_index()
    known = {entry.get("id") for entry in index}

    client = notion.NotionClient(config.secrets.notion_token)
    try:
        pages = walk_library(client, page_id)
        todo = [(pid, title) for pid, title in pages if pid not in known]

        fresh: list[dict] = []
        for page_id_, title in todo[:limit]:
            try:
                text = client.page_text(page_id_)
                summary = summarise_page(config, title, text) if text.strip() else ""
            except Exception as exc:
                log_warning(f"Couldn't index “{title}”: {_short(exc)}")
                continue
            fresh.append({
                "id": page_id_,
                "title": title,
                "url": f"https://www.notion.so/{page_id_.replace('-', '')}",
                "summary": summary,
            })
    finally:
        client.close()

    if fresh:
        sops.save_index(sops.merge_index(index, fresh))
    return len(fresh), len(pages) - len(todo), max(0, len(todo) - limit)


def log_warning(message: str) -> None:
    import logging

    logging.getLogger("wilbyte.bot").warning("%s", message)


# ------------------------------------------------- the Levinson monthly report


def open_sheets(config: Config):
    """A Google Sheets session, or a clear refusal about what is missing."""
    from .. import gsheets

    return gsheets.SheetsClient(gsheets.credentials(config.secrets))


def tab_titled(client, sheet_id: str, wanted: str) -> str:
    """The tab's name, given either its name or the gid out of its URL.

    The gid is the half of the URL somebody copies, and asking them to also
    find the tab name at the bottom of the window is asking for a typo.
    """
    from .. import gsheets

    said = " ".join((wanted or "").split())
    tabs = client.tabs(sheet_id)
    if said.isdigit():
        for one in tabs:
            if str(one.get("sheetId")) == said:
                return str(one.get("title") or "")
        raise gsheets.SheetsError(
            f"That spreadsheet has no tab with gid {said}. It has: "
            + ", ".join(str(one.get('title')) for one in tabs)
        )
    if said:
        return said
    # No tab named at all: the first one, which is what a one-tab sheet means.
    return str(tabs[0].get("title") or "") if tabs else ""


def levinson_members(config: Config) -> tuple[list, list[str]]:
    """Everyone Levinson sent us. (members, notes about the reading).

    Two sources on purpose. The GoHighLevel tag is applied automatically when
    somebody comes through their page, so it is the one that cannot be
    forgotten; the opt-in sheet is kept by hand, so it catches anybody the tag
    missed. Either one working alone still produces a report, and the note
    says which one didn't.
    """
    from .. import ghl, gsheets, levinson

    found: list = []
    notes: list[str] = []

    tag = config.secrets.levinson_tag
    if config.secrets.ghl_api_token and config.secrets.ghl_location_id:
        try:
            with ghl.GHLClient(
                config.secrets.ghl_api_token, config.secrets.ghl_location_id
            ) as client:
                for contact in client.contacts_tagged(tag):
                    found.append(
                        levinson.Member(
                            name=str(
                                contact.get("contactName")
                                or " ".join(
                                    part for part in (
                                        contact.get("firstName"), contact.get("lastName")
                                    ) if part
                                )
                            ).strip(),
                            email=str(contact.get("email") or ""),
                            phone=str(contact.get("phone") or ""),
                            source="ghl",
                        )
                    )
        except Exception as exc:
            notes.append(f"Couldn't read the `{tag}` contacts from GHL — {_short(exc, 160)}")

    sheet_id = config.secrets.levinson_optin_sheet_id
    if sheet_id:
        try:
            with open_sheets(config) as client:
                rows = client.rows(sheet_id, "A1:C1000")
        except Exception as exc:
            notes.append(f"Couldn't read the opt-in sheet — {_short(exc, 160)}")
        else:
            for row in rows[1:]:
                name, email, phone = (list(row) + ["", "", ""])[:3]
                # "NEW LEADS ---" is a divider somebody typed, not an agent.
                if not (email or "").strip() or "@" not in email:
                    continue
                found.append(
                    levinson.Member(
                        name=name.strip(), email=email.strip(), phone=phone.strip(),
                        source="sheet",
                    )
                )

    if not found and not notes:
        notes.append(
            "No Levinson members found. Set LEVINSON_OPTIN_SHEET_ID, or check "
            f"that contacts carry the `{tag}` tag."
        )
    return found, notes


def write_levinson(config: Config, batches) -> tuple[int, list[str]]:
    """Append each month's rows to that month's tab. (written, problems).

    `batches` is [((year, month), lines), ...]. One session for all of them,
    and the month decides the tab: writing every month onto whichever tab was
    configured once put June's rows under August.

    Appending only. The tab is somebody's, and the worst a bug here can do is
    put a row at the bottom that anybody can delete.
    """
    from .. import gsheets, levinson

    sheet_id = config.secrets.levinson_sheet_id
    if not sheet_id:
        return 0, ["LEVINSON_SHEET_ID isn't set in .env."]

    written, problems = 0, []
    try:
        with open_sheets(config) as client:
            tabs = {
                str(one.get("title") or ""): one.get("sheetId")
                for one in client.tabs(sheet_id)
            }
            for (year, month), lines in batches:
                if not lines:
                    continue
                title = levinson.pick_tab(list(tabs), year, month)
                if title is None:
                    title = levinson.new_tab_name(list(tabs), year, month)
                    tabs[title] = client.add_tab(sheet_id, title)
                    # A brand new tab has no headings, so it gets the ones the
                    # rest of the sheet uses rather than a shape of its own -
                    # bold, the way a heading row looks everywhere else.
                    heads = _headings_anywhere(client, sheet_id, list(tabs))
                    if heads:
                        where = client.append(sheet_id, title, [heads])
                        _restyle(client, sheet_id, tabs[title], where, bold=True)

                count, said = _append_month(
                    client, sheet_id, title, tabs.get(title), lines
                )
                written += count
                problems.extend(said)
    except gsheets.SheetsError as exc:
        return written, problems + [str(exc)]
    except Exception as exc:
        return written, problems + [f"Couldn't write the report — {_short(exc, 200)}"]
    return written, problems


def _headings_anywhere(client, sheet_id: str, titles: list[str]) -> list[str]:
    """The heading row the sheet already uses, off whichever tab has one."""
    from .. import levinson

    for title in titles:
        try:
            rows = client.rows(sheet_id, f"{title}!1:1")
        except Exception:
            continue
        heads = rows[0] if rows else []
        if levinson.readable(heads):
            return heads
    return []


def _restyle(
    client, sheet_id: str, tab_id, where: str, *, bold: bool, wrap: str = ""
) -> None:
    """Make the rows just written look like rows rather than headings."""
    from .. import gsheets

    span = gsheets.rows_in(where)
    if tab_id is None or span is None:
        return
    first, last = span
    client.restyle(sheet_id, int(tab_id), first, last, bold=bold, wrap=wrap)


def _append_month(client, sheet_id: str, title: str, tab_id, lines: list):
    """One month onto one tab, skipping what is already on it."""
    from .. import levinson

    rows = client.rows(sheet_id, f"{title}!1:1")
    headings = rows[0] if rows else []
    if not levinson.readable(headings):
        return 0, [
            f"The tab `{title}` has no Name or Email column — its first row "
            f"reads {headings or 'nothing'}. Refusing to write to it."
        ]

    where = {levinson.known(head): at for at, head in enumerate(headings)}
    date_at, email_at = where.get("date"), where.get("email")

    seen = set()
    for row in client.rows(sheet_id, f"{title}!A2:Z5000"):
        email = (
            row[email_at].strip().lower()
            if email_at is not None and email_at < len(row) else ""
        )
        when = (
            row[date_at].strip() if date_at is not None and date_at < len(row) else ""
        )
        if email:
            seen.add((when, email))

    fresh = []
    for line in lines:
        mark = (
            f"{line.paid_on:%m/%d/%Y}" if date_at is not None else "",
            line.email.lower(),
        )
        if mark in seen:
            continue
        seen.add(mark)
        fresh.append(levinson.row_for(line, headings))

    where = client.append(sheet_id, title, fresh)
    written = len(fresh)
    # Sheets copies the format of the row above, so rows appended under a
    # lone heading row arrive bold and centred.
    _restyle(client, sheet_id, tab_id, where, bold=False)
    said = []
    if date_at is None and written:
        # Without a date on the row there is nothing to tell one payment from
        # the next, so the second order from the same agent reads as a
        # duplicate of the first and is skipped.
        said.append(
            f"`{title}` has no Date column, so a second purchase by the same "
            "agent can't be told from the first and won't be added. Add a "
            "**Date** heading and RYTE starts filling it."
        )
    return written, said


# ------------------------------------------------ replying the way Faith does

#: How far back the first read of Faith's texts goes - a year, so what comes
#: round once a season is in it.
RING_FIRST_DAYS = 365

#: Only agent texts newer than this get a suggestion. The first run after
#: setting this up would otherwise ping about every agent who ever had the
#: last word.
RING_FRESH_HOURS = 12

#: How long an agent has to have stopped typing before a suggestion goes out.
#: They send "hey", then the question, then "?" - and a draft answering "hey"
#: is a ping about nothing.
RING_SETTLE_SECONDS = 90

#: Held around every read and write of the remembered texts. The watcher and
#: a typed "@RYTE respond" both run in threads.
_RING_FILE = threading.Lock()


def _ring_iso(when) -> str:
    """The timestamp shape RingCentral takes: UTC, milliseconds, a Z."""
    from datetime import timezone

    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _ring_when(said: str):
    from datetime import timezone

    try:
        when = datetime.fromisoformat(str(said).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def ring_catch_up(config: Config, *, now=None) -> tuple[dict, list[str]]:
    """Read what Faith's extension sent and received since last time.

    (what is remembered, problems). Deep the first time, then only what is
    new, with ten minutes' overlap: a text that landed while the last read was
    in flight, stamped a moment before it, is not one to lose.
    """
    from datetime import timezone

    from .. import ringcentral, ringtexts, smsreplies

    now = now or datetime.now(timezone.utc)
    with _RING_FILE:
        data = ringtexts.load()
        last = _ring_when(ringtexts.newest(data))
        # Read from the start again, once, if what is remembered was read
        # before outgoing texts carried the number they went from. Without
        # it Tre's texts from (412) and Faith's from (878) cannot be told
        # apart, and the months already read would go on teaching Tre's way
        # of writing as hers. The re-read replaces each text by its id.
        sent = [one for one in data.get("texts") or [] if not one.get("inbound")]
        if sent and not any("sender" in one for one in sent):
            last = None
        since = (last - timedelta(minutes=10)) if last else (
            now - timedelta(days=RING_FIRST_DAYS)
        )
        try:
            with ringcentral.open_ring(config.secrets) as reading:
                records = reading.texts(since=_ring_iso(since))
                owner = reading.owner()
                # The line's own numbers, for telling it apart from an agent.
                # Worth having, not worth failing the read over.
                try:
                    numbers = reading.own_numbers()
                except Exception:
                    import logging

                    logging.getLogger("wilbyte.bot").warning(
                        "Couldn't list the line's own numbers", exc_info=True
                    )
                    numbers = []
        except ringcentral.RingError as exc:
            return data, [str(exc)]
        except Exception as exc:
            return data, [f"Couldn't read RingCentral: {_short(exc, 160)}"]
        texts = [one for one in (smsreplies.from_record(r) for r in records) if one]
        ringtexts.keep(data, texts)
        if owner:
            data["owner"] = owner
        if numbers:
            data["numbers"] = numbers
        if last is None:
            # This read went all the way back already.
            data["reach"] = RING_FIRST_DAYS
        _reach_back(config, data, now=now)
        _learn_what_was_sent(config, data, now=now)
        ringtexts.save(data)
        return data, []


def _reach_back(config: Config, data: dict, *, now) -> None:
    """Once: read what is older than the oldest text remembered, back to
    RING_FIRST_DAYS.

    The first read went back 120 days and kept six thousand texts; both
    have been raised, and the months before them are still on RingCentral.
    Once rather than every minute - `reach` says how far back has been read
    - and a read that fails is tried again on the next pass, costing that
    pass nothing.
    """
    import logging

    from .. import ringcentral, ringtexts, smsreplies

    if int(data.get("reach") or 0) >= RING_FIRST_DAYS:
        return
    texts = data.get("texts") or []
    oldest = _ring_when(texts[0].get("at")) if texts else None
    since = now - timedelta(days=RING_FIRST_DAYS)
    if oldest is not None and oldest > since:
        try:
            with ringcentral.open_ring(config.secrets) as reading:
                records = reading.texts(since=_ring_iso(since), until=_ring_iso(oldest))
        except Exception:
            logging.getLogger("wilbyte.bot").warning(
                "Couldn't read the older texts yet; trying again next time", exc_info=True
            )
            return
        ringtexts.keep(data, [one for one in (smsreplies.from_record(r) for r in records) if one])
    data["reach"] = RING_FIRST_DAYS


def _learn_what_was_sent(config: Config, data: dict, *, now) -> None:
    """Grade every suggestion against what Faith then actually sent."""
    from .. import smsreplies

    pending = list(data.get("pending") or [])
    if not pending:
        return
    lessons, waiting = smsreplies.lessons_from(
        pending, ring_texts(config, data), now=_ring_iso(now),
        gone_after=_ring_iso(now - timedelta(days=LESSON_WAIT_DAYS)),
    )
    data["lessons"] = (list(data.get("lessons") or []) + lessons)[-KEEP_LESSONS:]
    data["pending"] = waiting


def ring_texts(config: Config, data: dict) -> list:
    """What is remembered, as Texts, with the team's own marked.

    The team is anything texting in under the line's own name or numbers -
    Tre's cell is labelled "Arnold Tarpley" too - anybody named in
    RINGCENTRAL_TEAM, and anything sent from a number that is not Faith's.
    Worked out each time rather than stored, so a name added to .env applies
    to the months already read.
    """
    from .. import smsreplies

    names = [str(data.get("owner") or "")] + [
        one for one in str(
            getattr(getattr(config, "secrets", None), "ringcentral_team", "") or ""
        ).split(",")
    ]
    texts = [smsreplies.Text.from_dict(one) for one in data.get("texts") or []]
    faith = str(
        getattr(getattr(config, "secrets", None), "ringcentral_faith_number", "") or ""
    ).strip() or smsreplies.faiths_number(texts)
    return smsreplies.mark_team(texts, names, data.get("numbers") or [], faith)


def ring_waiting(config: Config, *, now=None) -> tuple[list, dict, list[str]]:
    """Agent texts still waiting on a reply, and not yet pinged about.

    ([(agent, name, [texts])], what is remembered, problems).
    """
    from datetime import timezone

    from .. import ringtexts, smsreplies

    now = now or datetime.now(timezone.utc)
    data, problems = ring_catch_up(config, now=now)
    texts = ring_texts(config, data)
    since = _ring_iso(now - timedelta(hours=RING_FRESH_HOURS))
    settled = _ring_iso(now - timedelta(seconds=RING_SETTLE_SECONDS))
    found = [
        (agent, name, tail)
        for agent, name, tail in smsreplies.waiting(texts, since=since)
        if tail[-1].at <= settled and not ringtexts.was_pinged(data, tail[-1].id)
    ]
    return found, data, problems


def ring_pinged(message_id: str, *, pending: dict | None = None) -> None:
    """Remember that Franklin has been pinged about this text - and what was
    suggested, so what Faith then sends can grade it."""
    from .. import ringtexts

    with _RING_FILE:
        data = ringtexts.load()
        ringtexts.pinged(data, message_id)
        if pending:
            data["pending"] = (list(data.get("pending") or []) + [pending])[-200:]
        ringtexts.save(data)


def _card_text(card: dict) -> str:
    """The agent's card, as a draft or a lesson is shown it."""
    return (
        "WHAT THE TEAM'S BOARD SAYS ABOUT THIS AGENT - their New Agent "
        f"card for {card.get('agent') or 'them'}, in the "
        f"\"{card.get('list') or '?'}\" list, last touched "
        f"{card.get('touched') or '?'}:\n{card.get('desc') or ''}"
        + (f"\n\nSetup confirmation on the card:\n{card['setup']}"
           if card.get("setup") else "")
        + (f"\n\nTheir lead sheet: {card['sheet']}" if card.get("sheet") else "")
    )


def _said_by(one) -> str:
    return "Team" if one.team else "Agent" if one.inbound else "Faith"


def draft_like_faith(
    config: Config, *, asked: str, done: list, thread: list = (), name: str = "",
    agent: str = "", card: dict | None = None, history: list = (),
    lessons: list = (), playbook: str = "", learned: list = (),
    links: list = (), look: "RingLookups | None" = None,
) -> dict:
    """The reply Faith would send. {"reply", "why", "blanks", "examples", "checked"}.

    Drafted, never sent - there is nothing here or in `ringcentral` that can
    send. Faith's past exchanges are the examples: the ones most like this
    text for how she handles it, and a few of her newest for how she writes.

    Nothing invented. A reply that tells an agent a launch date or a credit
    nobody agreed to is worse than no suggestion, because it reads exactly as
    confident as a right one - the rebuttal learned that with an email address
    and a card number taken off RYTE's own example. So anything only the team
    knows is a bracketed blank, listed for Franklin to fill in.
    """
    from anthropic import Anthropic

    from .. import smsreplies

    said = " ".join(str(asked or "").split())
    if not said:
        raise ValueError("There's no message to reply to.")
    examples = smsreplies.closest(list(done), said, agent=smsreplies.digits(agent))
    if not examples:
        raise ValueError(
            "I have none of Faith's replies to learn from yet, so anything I "
            "wrote would be my voice, not hers."
        )

    lines = []
    for at, one in enumerate(examples, start=1):
        lines.append(
            f"EXAMPLE {at}\nAgent: {one.asked}\nFaith: {one.answered}"
            + (f"\n(Why she answered that way: {one.why})" if one.why else "")
        )
    so_far = [
        f"{'Team' if one.team else 'Agent' if one.inbound else 'Faith'}: {one.said}"
        for one in thread or []
    ]
    if not so_far or not so_far[-1].endswith(said):
        so_far.append(f"Agent: {said}")
    sections = []
    if playbook:
        sections.append(
            "FAITH'S PLAYBOOK - how she handles agents, written from her own "
            f"replies:\n\n{playbook}"
        )
    if learned:
        sections.append(
            "WHAT YOU HAVE LEARNED FROM HER CORRECTIONS - standing rules, each "
            "from a suggestion she or Franklin did not send as written:\n"
            + "\n".join(f"- {one}" for one in learned)
        )
    if card:
        sections.append(_card_text(card))
    if links:
        sections.append(
            "LINKS FAITH SENDS - each to several agents, so hers to send again "
            "when she would:\n" + "\n".join(
                f"- {one['what']}: {one['link']} - sent {one['times']} times to "
                f"{one['agents']} agents; last with: \"{one['said'][:200]}\""
                for one in links
            )
        )
    # Every link in the conversation is opened before anything is written:
    # an agent's "this one?" under a pasted link is a question about what is
    # behind the link, and the draft should know what that is.
    if look is not None:
        from .. import smslinks

        seen = []
        for one in list(thread or [])[-8:] + list(history or [])[-4:]:
            for link in smslinks.links_in(one.said):
                if link not in seen:
                    seen.append(link)
        opened = [f"{link}\n{look.run('open_link', {'link': link})}" for link in seen[-LINKS_OPENED:]]
        if opened:
            sections.append(
                "WHAT THE LINKS IN THESE TEXTS ARE - opened with RYTE's own "
                "access just now:\n\n" + "\n\n".join(opened)
            )
    if history:
        sections.append(
            "EARLIER TEXTS WITH THIS AGENT, in other conversations:\n"
            + "\n".join(
                f"{'Team' if one.team else 'Agent' if one.inbound else 'Faith'}: {one.said}"
                for one in history
            )
        )
    if lessons:
        sections.append(
            "WHERE EARLIER SUGGESTIONS WERE WRONG - what was suggested, and "
            "what Faith actually sent instead, and why. Follow what she did:\n\n"
            + "\n\n".join(
                f"Agent: {one.get('asked')}\nSuggested: {one.get('suggested')}\n"
                f"Faith sent: {one.get('sent') or '(nothing by text)'}"
                + (f"\nFranklin said: {one['note']}" if one.get("note") else "")
                + (f"\nWhy: {one['why']}" if one.get("why") else "")
                for one in lessons
            )
        )
    prompt = (
        ("\n\n---\n".join(sections) + "\n\n---\n" if sections else "")
        + "Here is how Faith has answered agents before:\n\n"
        + "\n\n".join(lines)
        + "\n\n---\nThe conversation now, oldest first"
        + (f" (the agent is {name})" if name else "")
        + ":\n\n" + "\n".join(so_far)
        + "\n\nWrite the reply Faith would send to the agent's last message. "
        "Lines marked Team are colleagues texting into the thread - an agent "
        "being handed over, say - and are context, not the agent."
    )

    config.secrets.require("anthropic_api_key")
    client = Anthropic(api_key=config.secrets.anthropic_api_key)
    system = (
            "You help Franklin, the general manager of Agent Lead Lab, answer "
            "text messages from insurance agents who buy leads from the "
            "company. Faith on the customer service team normally answers "
            "them, and Franklin wants each reply to read as Faith would write "
            "it. Nothing you write is sent: Franklin reads it and decides.\n\n"
            "Study her examples for how she writes - length, greeting and "
            "sign-off, capitals, punctuation, emoji, how warm or brief - and "
            "for how she handles each kind of message: what she asks for, what "
            "she commits to, what she will not promise, and when she says she "
            "will check with the team. Then write the one reply she would send.\n\n"
            "Never state a fact that is not in the conversation, in the "
            "agent's card from the team's board, or plainly a standing habit "
            "in her examples: no dates, times, prices, lead counts, launch "
            "days, refunds, credits or promises from anywhere else. The card "
            "can describe an older order - a client who orders again keeps "
            "the old card, and one long sitting in Done may be months old - so "
            "use it only where it plainly answers this message. Where the "
            "reply needs a fact nothing here settles, write a bracketed blank "
            "like [launch date] and list it in blanks. Where a correction shows "
            "Faith answering differently from a suggestion, do what she did, "
            "and follow the rules learned from her corrections - what Franklin "
            "said about a suggestion outranks everything else. The reasons "
            "given beside her examples are why she answered that way: answer "
            "for the same reasons, not only in the same words. If the message "
            "needs a decision only a person can make "
            "- a refund, a complaint about the leads, an upset agent - still "
            "draft her holding reply, and say so in why. Match her, not a "
            "customer-service script: if she writes two lines, write two lines."
    )
    if look is not None:
        system += (
            "\n\nBefore you write, think through what this message turns on, "
            "and check it rather than guess. You have RYTE's own read-only "
            "access: open any link (lead sheets, Trello cards, Stripe payment "
            "links, Loom videos, web pages), find the agent's card on the "
            "team's Trello board, their payments in Payra and their payments "
            "and invoices in Stripe - the company takes both - their "
            "contact in GoHighLevel, and search every text the line has sent "
            "or received. Leads not coming, or the wrong ones: open their sheet "
            "and see when the last lead landed and what it was. A payment or "
            "an invoice: Payra and Stripe both. Launch day, states or setup: the card. "
            "Something another agent may have said too: search the texts. A "
            "few look-ups, not a survey - then reply. What you find is fact "
            "you may use, and say in why what you checked. Faith's standing "
            "links may be sent where she would send them; an agent's own "
            "sheet link only to that agent."
        )
    reply_tool = {
            "name": "reply",
            "description": "The reply Faith would send, and what it rests on.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "reply": {"type": "string"},
                    "why": {
                        "type": "string",
                        "description": "One short line: which of her habits or "
                                       "past replies this follows.",
                    },
                    "blanks": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Each bracketed blank Franklin has to fill.",
                    },
                },
                "required": ["reply", "why", "blanks"],
            },
    }
    got = _think_then_reply(
        client, model=config.copy.model, system=system, prompt=prompt,
        reply_tool=reply_tool, look=look,
    )
    reply = str(got.get("reply") or "").strip()
    if not reply:
        raise ValueError("The draft came back empty.")
    return {
        "reply": reply,
        "why": " ".join(str(got.get("why") or "").split()),
        "blanks": _listed(got.get("blanks")),
        "examples": len(examples),
        "checked": list(look.checked) if look is not None else [],
    }


def _listed(said) -> list[str]:
    """A list Claude was asked for, however it came back. Once it came back
    as one string, and the card listed it a letter at a time."""
    if isinstance(said, str):
        try:
            parsed = json.loads(said)
        except ValueError:
            parsed = None
        said = parsed if isinstance(parsed, list) else [said]
    return [str(one).strip() for one in said or [] if str(one).strip()]


#: Look-ups a draft may make before it has to reply. Each is one more round
#: trip to Claude; a draft that wants more than this is surveying.
LOOKUPS = 5

#: Links in the conversation opened before drafting.
LINKS_OPENED = 3


def _think_then_reply(client, *, model: str, system: str, prompt: str,
                      reply_tool: dict, look=None) -> dict:
    """Ask for the reply, letting Claude look things up first.

    Without look-ups, one call that has to reply. With them, Claude may call
    RYTE's read-only look-ups a few times, each answer handed back, and must
    reply on the last turn whatever it has found by then.
    """
    # The same tools on every turn, and the conversation cached as it grows:
    # each look-up sends everything before it again, and a cached read of it
    # is a tenth of the price. Changing the tools would throw the cache away.
    tools = [reply_tool] + (look.tools() if look is not None else [])
    messages = [{"role": "user", "content": prompt}]
    for turn in range(LOOKUPS + 1):
        last = look is None or turn == LOOKUPS
        response = client.messages.create(
            model=model, max_tokens=1500, system=system, tools=tools,
            tool_choice={"type": "tool", "name": "reply"} if last else {"type": "any"},
            messages=messages, cache_control={"type": "ephemeral"},
        )
        blocks = list(getattr(response, "content", None) or [])
        calls = [one for one in blocks if getattr(one, "type", "") == "tool_use"]
        answer = next((one for one in calls if one.name == reply_tool["name"]), None)
        if answer is not None:
            return dict(answer.input or {})
        if not calls:
            break
        messages.append({"role": "assistant", "content": [
            {"type": "tool_use", "id": one.id, "name": one.name, "input": one.input}
            if getattr(one, "type", "") == "tool_use"
            else {"type": "text", "text": str(getattr(one, "text", "") or "")}
            for one in blocks
            if getattr(one, "type", "") == "tool_use" or getattr(one, "text", "")
        ]})
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": one.id,
             "content": look.run(one.name, dict(one.input or {}))}
            for one in calls
        ]})
    raise ValueError("Claude didn't write a reply.")


#: The biggest screenshot sent to be read. Claude takes five megabytes an image.
RING_SHOT_BYTES = 5 * 1024 * 1024


def read_texts_off(config: Config, shots: list, *, line_name: str = "") -> list[dict]:
    """The conversation in a RingCentral screenshot, oldest first.

    [{"side": "agent" | "faith" | "team", "name", "text"}]. "@Ryte respond"
    with a picture of a thread, for a conversation Franklin is looking at
    and wants an answer to now.

    Told how the app draws a thread rather than left to guess: blue on the
    right is the line sending, which is Faith; grey on the left is coming in;
    and coming in under the line's own name is Tre's cell, which RingCentral
    labels "Arnold Tarpley" too - a colleague handing an agent over, not the
    agent.
    """
    import base64

    from anthropic import Anthropic

    pictures = [
        {"type": "image", "source": {
            "type": "base64", "media_type": _media_type(name),
            "data": base64.b64encode(data).decode("ascii"),
        }}
        for name, data in shots if data
    ]
    if not pictures:
        raise ValueError("There was no picture to read.")
    own = line_name or "the line's own name"

    config.secrets.require("anthropic_api_key")
    client = Anthropic(api_key=config.secrets.anthropic_api_key)
    response = client.messages.create(
        model=config.copy.model,
        max_tokens=2000,
        system=(
            "You read screenshots of text conversations in the RingCentral "
            "app and write down every message in them, exactly as written."
        ),
        tools=[{
            "name": "conversation",
            "description": "Every message in the screenshot, top to bottom.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "messages": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "side": {"type": "string",
                                         "enum": ["agent", "faith", "team"]},
                                "name": {"type": "string"},
                                "text": {"type": "string"},
                            },
                            "required": ["side", "name", "text"],
                        },
                    }
                },
                "required": ["messages"],
            },
        }],
        tool_choice={"type": "tool", "name": "conversation"},
        messages=[{"role": "user", "content": pictures + [{
            "type": "text",
            "text": (
                "Write down every message in this conversation, oldest first.\n"
                "- Blue bubbles on the right were SENT from the line: side "
                "\"faith\".\n"
                f"- Grey bubbles on the left came IN. If the name above them is "
                f"\"{own}\", that is a colleague texting into the thread from "
                "another of the line's numbers: side \"team\". Anybody else "
                "coming in is the agent: side \"agent\".\n"
                "- A message that is only an emoji or a sticker is that emoji "
                "(\"👍\"). A link is its address.\n"
                "- Leave out the member list, dates, times, and the app itself."
            ),
        }]}],
    )
    got = _tool_input(response, "conversation")
    found = []
    for one in got.get("messages") or []:
        text = " ".join(str(one.get("text") or "").split())
        side = str(one.get("side") or "")
        if text and side in ("agent", "faith", "team"):
            found.append({"side": side, "name": str(one.get("name") or "").strip(),
                          "text": text})
    return found


# ------------------------------------------- what RYTE knows about the agent

#: How much of a card's description goes into a draft. The form's fields and
#: the order lines are at the top; the rest is states and notes.
CARD_CHARS = 1500

#: How long a suggestion waits for Faith's own answer before it is dropped as
#: something that was settled another way.
LESSON_WAIT_DAYS = 3

#: How many of what-RYTE-said-and-what-she-sent are kept.
KEEP_LESSONS = 150

#: How often Faith's playbook is written again from her replies.
PLAYBOOK_DAYS = 7

#: This many new lessons since the playbook was written, and it is written
#: again without waiting out the week.
PLAYBOOK_NEW_RULES = 10

#: Corrections worked out per minute's read. One Claude call each.
STUDY_LESSONS = 3

#: Her past exchanges studied per call, and one call per minute's read: her
#: whole history is read through within a couple of hours of starting, then
#: each new one as it settles.
REASON_BATCH = 20

#: An answer this old is finished. She often answers in two or three texts.
REASON_SETTLE_MINUTES = 60


#: Older than any card Trello ever made - for a card whose id says nothing.
_NEVER = datetime.fromtimestamp(0, tz=timezone.utc)


def agent_on_the_board(config: Config, number: str, name: str = "") -> dict | None:
    """The agent's New Agent card, found by the number texting or by name.

    {"agent", "list", "desc", "setup", "sheet", "url", "touched"} or None.
    Context clues: what they bought, when they go live, which states, whether
    the setup is done. "When do my leads start?" answered from the card is
    the answer; answered from nothing it is a [launch date] for Franklin to
    go and look up.

    The newest card when there are several - a client who orders again gets
    a card copied from the last one - and the list it sits in, so a card put
    away in Done months ago reads as the old order it is.
    """
    from .. import agents, smsreplies, tagged

    wanted = smsreplies.digits(number)
    client = open_trello(config)
    try:
        every = client.board_cards(config.secrets.trello_board_id, archived=True)
        cards = [one for one in every if agents.is_agent_card(str(one.get("name") or ""))]
        found = [
            one for one in cards
            if wanted and wanted in smsreplies.phones_in(str(one.get("desc") or ""))
        ]
        if not found and name.strip():
            found = [one for one in agents.named_that(name, cards)]
        if not found:
            return None
        # By when the card was made, not when it was last touched: RYTE keeps
        # the unticked ones floated to the top of Done, and moving an old
        # order's card there touches it without making it the newest order.
        card = max(found, key=lambda one: (
            agents.made_at(str(one.get("id") or "")) or _NEVER,
            str(one.get("dateLastActivity") or ""),
        ))
        lists = {
            str(one.get("id") or ""): str(one.get("name") or "")
            for one in client.board_lists(config.secrets.trello_board_id)
        }
        said = client.card_comments(str(card.get("id") or ""))
    finally:
        client.close()

    setup = next((one for one in said if tagged.a_setup_confirmation(one)), "")
    sheets = agents.sheet_links(agents.in_order_written(said))
    return {
        "agent": agents.agent_name(str(card.get("name") or "")),
        "list": "archived" if card.get("closed") else lists.get(str(card.get("idList") or ""), ""),
        "desc": str(card.get("desc") or "")[:CARD_CHARS],
        "setup": setup[:600],
        "sheet": sheets[-1][1] if sheets else "",
        "url": str(card.get("shortUrl") or card.get("url") or ""),
        "touched": str(card.get("dateLastActivity") or "")[:10],
    }


def faith_playbook(config: Config, done: list, learned: list = (), links: list = ()) -> str:
    """How Faith handles agents, written down from her own replies.

    Examples show how she answered one message. This is the part above them:
    what she does with each kind of message across hundreds of them - what
    she asks for, what she promises and never promises, when she hands it to
    the team, how she writes at all. Written by Claude from an even spread of
    her history, used by every draft, and posted so Franklin can say where
    it is wrong.
    """
    from anthropic import Anthropic

    from .. import smsreplies

    picked = smsreplies.sample_for_playbook(list(done))
    if len(picked) < 20:
        raise ValueError("Too few of her replies to write a playbook from.")
    shown = "\n\n".join(
        f"Agent: {one.asked[:300]}\nFaith: {one.answered[:400]}"
        + (f"\n(Why: {one.why})" if getattr(one, "why", "") else "")
        for one in picked
    )
    corrected = (
        "\n\n---\nAnd what was learned where drafts written like her were not "
        "sent as written - these are right, and the playbook must agree with "
        "them:\n" + "\n".join(f"- {one}" for one in learned)
        if learned else ""
    )
    if links:
        corrected += (
            "\n\n---\nThe links she sends, most sent first - how many times, "
            "to how many different agents, and her newest text with it in:\n"
            + "\n".join(
                f"- {one['what']}: {one['link']} - {one['times']} times, "
                f"{one['agents']} agents: \"{one['said'][:200]}\""
                for one in links
            )
        )
    config.secrets.require("anthropic_api_key")
    client = Anthropic(api_key=config.secrets.anthropic_api_key)
    response = client.messages.create(
        model=config.copy.model,
        max_tokens=3000,
        system=(
            "You study how one customer service person, Faith, texts insurance "
            "agents who buy leads from Agent Lead Lab, and write down how she "
            "does it so someone else can answer the way she would. Only what "
            "the examples show - never a policy, price or promise they don't."
        ),
        messages=[{"role": "user", "content": (
            f"Here are {len(picked)} real exchanges, spread across her history:"
            f"\n\n{shown}{corrected}\n\n---\nWrite Faith's playbook in plain markdown:\n"
            "1. **How she writes** - length, greeting, sign-off, capitals, "
            "punctuation, emoji, how she uses the agent's name.\n"
            "2. **Each kind of message agents send** - group them into the kinds "
            "that actually come up (invoices, launch dates, pausing, states, "
            "lead quality, thanks, and whatever else is there). For each: what "
            "she does, what she asks for, what she commits to and what she "
            "won't, when she hands it to the team, why she handles it that "
            "way, and two or three of her real phrasings, quoted.\n"
            "3. **Links she sends** - each of her standing links, what it is "
            "for and when she sends it; and which links are one agent's own, "
            "like their lead sheet.\n"
            "4. **Things she never does.**\n"
            "Keep it under 1,200 words."
        )}],
    )
    text = "".join(
        getattr(block, "text", "") for block in response.content
        if getattr(block, "type", "") == "text"
    ).strip()
    if not text:
        raise ValueError("The playbook came back empty.")
    return text


def ring_playbook_due(data: dict, *, now=None) -> bool:
    """Whether Faith's playbook needs writing - never written, a week old, or
    behind on what her corrections have taught since."""
    from datetime import timezone

    held = data.get("playbook") or {}
    made = _ring_when(held.get("made") or "")
    if not held.get("text") or made is None or held.get("stale"):
        return True
    now = now or datetime.now(timezone.utc)
    newer = [
        one for one in data.get("lessons") or []
        if one.get("rule") and str(one.get("at") or "") > str(held.get("made") or "")
    ]
    return now - made > timedelta(days=PLAYBOOK_DAYS) or len(newer) >= PLAYBOOK_NEW_RULES


def ring_playbook_stale() -> None:
    """Have the playbook written again from everything, on the next pass."""
    from .. import ringtexts

    with _RING_FILE:
        data = ringtexts.load()
        data["playbook"] = {**(data.get("playbook") or {}), "stale": True}
        ringtexts.save(data)


def ring_keep_playbook(text: str, replies: int) -> None:
    """Remember a freshly written playbook."""
    from datetime import timezone

    from .. import ringtexts

    with _RING_FILE:
        data = ringtexts.load()
        data["playbook"] = {
            "text": text, "made": _ring_iso(datetime.now(timezone.utc)),
            "from": replies,
        }
        ringtexts.save(data)


def ring_context(config: Config, data: dict, texts: list, *, agent: str,
                 name: str = "", key: str = "", asked: str = "") -> dict:
    """Everything a draft should know besides the conversation itself.

    {"card", "history", "lessons", "playbook", "learned", "links", "look"}. The card is looked up on the
    board and a board that will not answer costs the draft its context, not
    the draft - a suggestion without the launch date is still a suggestion.
    """
    import logging

    from .. import smslinks, smsreplies

    card = None
    if agent or name:
        try:
            card = agent_on_the_board(config, agent, name)
        except Exception:
            logging.getLogger("wilbyte.bot").warning(
                "Couldn't look the agent up on the board", exc_info=True
            )
    return {
        "card": card,
        "history": smsreplies.history(texts, agent, besides=key) if agent else [],
        "lessons": smsreplies.pick_lessons(list(data.get("lessons") or []), asked),
        "playbook": str((data.get("playbook") or {}).get("text") or ""),
        "learned": smsreplies.rules_from(list(data.get("lessons") or [])),
        "links": smslinks.standing(texts),
        "look": RingLookups(config, texts, agent=agent, name=name),
    }


# ------------------------------------------------------ learning why she did


def _lesson_id(lesson: dict) -> str:
    """Which suggestion a lesson grades: the conversation and the moment."""
    return f"{lesson.get('key') or lesson.get('agent') or ''}|{lesson.get('asked_at') or lesson.get('at') or ''}"


def explain_the_change(config: Config, lesson: dict, *, before: list = (),
                       after: list = (), card: dict | None = None,
                       opened: list = ()) -> dict:
    """Why a suggestion was not sent as written. {"why", "rule", "kind"}.

    What was sent says what she did; this is the part a draft can carry to
    the next agent - that she knew the launch date off the card, that she
    never says "paused" to somebody behind on an invoice, that she called
    instead. What Franklin said about it, when he said something, is the
    reason, and is not second-guessed.
    """
    from anthropic import Anthropic

    parts = []
    if card:
        parts.append(_card_text(card))
    if before:
        parts.append("THE CONVERSATION BEFORE IT, oldest first:\n" + "\n".join(
            f"{_said_by(one)}: {one.said[:400]}" for one in before
        ))
    parts.append(f"THE AGENT'S MESSAGE:\n{lesson.get('asked') or ''}")
    parts.append(f"RYTE SUGGESTED:\n{lesson.get('suggested') or ''}")
    parts.append(
        "WHAT WAS ACTUALLY SENT:\n" + (
            lesson.get("sent") or "(nothing was texted back - it was handled "
            "some other way, or needed no text)"
        )
    )
    if after:
        parts.append("WHAT CAME AFTER, oldest first:\n" + "\n".join(
            f"{_said_by(one)}: {one.said[:400]}" for one in after
        ))
    if opened:
        parts.append("WHAT THE LINKS IN IT ARE, opened just now:\n\n" + "\n\n".join(
            f"{link}\n{said}" for link, said in opened
        ))
    if lesson.get("note"):
        parts.append(f"FRANKLIN SAID ABOUT THE SUGGESTION:\n{lesson['note']}")

    config.secrets.require("anthropic_api_key")
    client = Anthropic(api_key=config.secrets.anthropic_api_key)
    response = client.messages.create(
        model=config.copy.model,
        max_tokens=600,
        system=(
            "RYTE drafts replies to insurance agents' text messages the way "
            "Faith, on Agent Lead Lab's customer service team, would write "
            "them. Franklin, the general manager, reads each draft, and Faith "
            "- or Franklin, from her line - sends the real reply. This draft "
            "was not sent as written. Work out why, so the next draft gets it "
            "right.\n\n"
            "If Franklin said why, that is the reason: explain it in his terms "
            "and do not second-guess it. Otherwise look for what the draft "
            "missed: a fact she knew that it did not (from the card or the "
            "earlier texts), something it promised that she would not, an "
            "action she took instead of words (a call, a handover to the "
            "team), a link she sent or left out and what is behind it, a "
            "question she asked first, length, tone, timing - or "
            "only wording, when that is all it was. Use what came after as a "
            "clue to how her answer landed. Never invent a reason the texts "
            "do not support."
        ),
        tools=[{
            "name": "lesson",
            "description": "Why the draft was not sent, and what to do next time.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "why": {
                        "type": "string",
                        "description": "One or two plain sentences to Franklin: "
                                       "why she wrote something different.",
                    },
                    "rule": {
                        "type": "string",
                        "description": "One standing instruction for future "
                                       "drafts that would have got this right. "
                                       "General enough for other agents: no "
                                       "names, dates or amounts from this "
                                       "conversation.",
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["facts", "promise", "action", "handoff", "link",
                                 "question", "length", "tone", "timing",
                                 "wording", "other"],
                    },
                },
                "required": ["why", "rule", "kind"],
            },
        }],
        tool_choice={"type": "tool", "name": "lesson"},
        messages=[{"role": "user", "content": "\n\n".join(parts)}],
    )
    got = _tool_input(response, "lesson")
    why = " ".join(str(got.get("why") or "").split())
    if not why:
        raise ValueError("The lesson came back empty.")
    return {
        "why": why,
        "rule": " ".join(str(got.get("rule") or "").split()),
        "kind": str(got.get("kind") or "other"),
    }


def explain_her_replies(config: Config, batch: list) -> dict:
    """Why she answered each of these the way she did. {exchange id: why}.

    `batch` is [(Exchange, [the texts before it])]. Every past reply of hers,
    read with the conversation it was in: a draft shown "Faith: I'll check
    with the team!" copies the words; shown that she says it whenever an
    agent asks about a refund, it knows when to.
    """
    from anthropic import Anthropic

    if not batch:
        return {}
    shown = []
    for at, (one, before) in enumerate(batch, start=1):
        so_far = "\n".join(f"{_said_by(text)}: {text.said[:300]}" for text in before)
        shown.append(
            f"EXCHANGE {at}\n{so_far or 'Agent: ' + one.asked[:600]}\n"
            f"FAITH ANSWERED: {one.answered[:600]}"
        )
    config.secrets.require("anthropic_api_key")
    client = Anthropic(api_key=config.secrets.anthropic_api_key)
    response = client.messages.create(
        model=config.copy.model,
        max_tokens=4000,
        system=(
            "You are studying how Faith, on Agent Lead Lab's customer service "
            "team, answers text messages from insurance agents who buy leads, "
            "so that drafts can answer the way she would and for the same "
            "reasons. For each exchange, say in one sentence why she answered "
            "the way she did: what in the agent's message or the conversation "
            "before it led her there - what she was asking for, promising or "
            "holding back, whether she handed it to the team, why so short or "
            "so warm. The reason, not a summary of her words. Only what the "
            "texts support; when it is simply how she always answers that, "
            "say so."
        ),
        tools=[{
            "name": "reasons",
            "description": "Why she answered each exchange the way she did.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "reasons": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "n": {"type": "integer"},
                                "why": {"type": "string"},
                            },
                            "required": ["n", "why"],
                        },
                    },
                },
                "required": ["reasons"],
            },
        }],
        tool_choice={"type": "tool", "name": "reasons"},
        messages=[{"role": "user", "content": "\n\n".join(shown)}],
    )
    got = _tool_input(response, "reasons")
    found = {}
    for item in got.get("reasons") or []:
        try:
            at = int(item.get("n"))
        except (TypeError, ValueError, AttributeError):
            continue
        why = " ".join(str(item.get("why") or "").split())
        if 1 <= at <= len(batch) and why:
            found[batch[at - 1][0].id] = why
    if not found:
        raise ValueError("None of her replies came back explained.")
    # One skipped is remembered as studied: asked again every minute, an
    # exchange Claude will not explain is a bill that never ends.
    return {one.id: found.get(one.id, "") for one, _before in batch}


def ring_study(config: Config, *, now=None) -> dict:
    """A little more learning, each minute. {"explained", "studied", "problems"}.

    The corrections first, a few at a time - why a suggestion was not sent,
    worked out from the conversation, the card and whatever Franklin said -
    then the next batch of her past replies, newest first, until her whole
    history has been read. Claude is asked with the file let go, so a ping
    is never held up by the studying.
    """
    import logging
    from datetime import timezone

    from .. import ringtexts, smsreplies

    from .. import smslinks

    log = logging.getLogger("wilbyte.bot")
    now = now or datetime.now(timezone.utc)
    with _RING_FILE:
        data = ringtexts.load()
    texts = ring_texts(config, data)
    problems: list[str] = []

    explained, tried = {}, set()
    for one in smsreplies.needs_explaining(list(data.get("lessons") or []))[:STUDY_LESSONS]:
        key, at = str(one.get("key") or ""), str(one.get("at") or "")
        card = None
        try:
            card = agent_on_the_board(config, str(one.get("agent") or ""),
                                      str(one.get("name") or ""))
        except Exception:
            log.warning("Couldn't look the agent up for a lesson", exc_info=True)
        try:
            links = smslinks.links_in(f"{one.get('sent') or ''} {one.get('suggested') or ''}")
            explained[_lesson_id(one)] = explain_the_change(
                config, one, card=card,
                opened=[(link, read_link(config, link)) for link in links[:LINKS_OPENED]],
                before=smsreplies.before(texts, key, str(one.get("asked_at") or at)),
                after=smsreplies.after(texts, key, at),
            )
        except Exception as exc:
            tried.add(_lesson_id(one))
            problems.append(f"Couldn't work out a lesson: {_short(exc, 160)}")

    reasons = {}
    batch = smsreplies.unexplained(
        smsreplies.exchanges(texts), data.get("reasons") or {},
        settled=_ring_iso(now - timedelta(minutes=REASON_SETTLE_MINUTES)),
        most=REASON_BATCH,
    )
    if batch:
        try:
            reasons = explain_her_replies(config, [
                (one, smsreplies.before(texts, one.key, one.at, most=6)) for one in batch
            ])
        except Exception as exc:
            problems.append(f"Couldn't study her replies: {_short(exc, 160)}")

    new = []
    if explained or tried or reasons:
        with _RING_FILE:
            data = ringtexts.load()
            for one in data.get("lessons") or []:
                lesson = _lesson_id(one)
                if lesson in explained:
                    one.update(explained[lesson])
                    new.append(one)
                elif lesson in tried:
                    one["tries"] = int(one.get("tries") or 0) + 1
            if reasons:
                known = {one.id for one in smsreplies.exchanges(ring_texts(config, data))}
                held = {**(data.get("reasons") or {}), **reasons}
                data["reasons"] = {at: why for at, why in held.items() if at in known}
            ringtexts.save(data)
    return {"explained": new, "studied": len(reasons), "problems": problems}


def ring_posted(ring_id: str, discord_id, *, lesson: str = "", echo: bool = False) -> None:
    """Remember which Discord message a suggestion - or a lesson - went out
    as, so Franklin replying to it can be matched back to it. `echo` is its
    copy on the team's screen: a reply there teaches the same."""
    from .. import ringtexts

    with _RING_FILE:
        data = ringtexts.load()
        for one in (data.get("lessons") if lesson else data.get("pending")) or []:
            if (_lesson_id(one) if lesson else str(one.get("id") or "")) == (lesson or ring_id):
                if echo:
                    one["echoes"] = (list(one.get("echoes") or []) + [str(discord_id)])[-4:]
                else:
                    one["lesson_post" if lesson else "posted"] = str(discord_id)
        ringtexts.save(data)


def ring_posts(data: dict) -> set:
    """Every Discord message a reply to would be Franklin teaching RYTE."""
    ids = set()
    for one in list(data.get("pending") or []) + list(data.get("lessons") or []):
        for field in ("posted", "lesson_post"):
            if str(one.get(field) or "").isdigit():
                ids.add(int(one[field]))
        ids |= {int(echo) for echo in one.get("echoes") or [] if str(echo).isdigit()}
    return ids


def ring_told(discord_id, note: str) -> str | None:
    """What Franklin said, replying to a suggestion or a lesson.

    "lesson", "ping" or None if the message was neither. On a lesson it
    replaces RYTE's guess at why, which is worked out again in his terms; on
    a suggestion Faith has not answered yet, it waits for her answer and
    goes with it - or, if she never texts back, is the lesson on its own.
    """
    from .. import ringtexts

    note = " ".join(str(note or "").split())
    if not note:
        return None
    wanted = str(discord_id)
    with _RING_FILE:
        data = ringtexts.load()
        for one in data.get("lessons") or []:
            if wanted in (str(one.get("posted") or ""), str(one.get("lesson_post") or "")) \
                    or wanted in [str(echo) for echo in one.get("echoes") or []]:
                one["note"] = " / ".join(bit for bit in (one.get("note"), note) if bit)
                for guess in ("why", "rule", "kind", "tries"):
                    one.pop(guess, None)
                ringtexts.save(data)
                return "lesson"
        for one in data.get("pending") or []:
            if wanted in [str(one.get("posted") or "")] + [str(echo) for echo in one.get("echoes") or []]:
                one["note"] = " / ".join(bit for bit in (one.get("note"), note) if bit)
                ringtexts.save(data)
                return "ping"
    return None


# ------------------------------------------------ looking things up, read-only

#: How much of one look-up goes back to Claude.
LOOKUP_CHARS = 3000


class RingLookups:
    """What a draft may check before it is written. Read-only, every one.

    Each look-up answers in words, and a look-up that fails says so in words
    rather than raising: "Stripe wouldn't let me look" is something to reply
    around, and a draft lost because the GHL token expired is not. `checked`
    is the trail, shown under the ping so Franklin sees what it rests on.
    """

    def __init__(self, config: Config, texts: list, *, agent: str = "", name: str = ""):
        self.config, self.texts = config, list(texts or [])
        self.agent, self.name = agent, name
        self.checked: list[str] = []

    def tools(self) -> list[dict]:
        def tool(name, what, **fields):
            return {
                "name": name, "description": what,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        key: {"type": kind, "description": said}
                        for key, (kind, said) in fields.items()
                    },
                    "required": [key for key, (kind, _) in fields.items() if kind == "string"],
                },
            }

        return [
            tool("open_link", "Open a link and say what is behind it: a lead "
                 "sheet's newest rows, a Trello card, a Stripe payment link and "
                 "who paid it, a Loom video's words, a web page.",
                 link=("string", "The link, as it was sent.")),
            tool("trello_card", "The agent's New Agent card on the team's "
                 "Trello board: what they ordered, launch day, states, which "
                 "list it is in, setup confirmation, sheet link.",
                 who=("string", "Their name or phone number.")),
            tool("stripe_customer", "The agent in Stripe: their invoices "
                 "(paid or not, how much, when) and recent payments.",
                 who=("string", "Their email, phone number or full name.")),
            tool("payra_payments", "The agent in Payra: their invoices - "
                 "number, total, dates, and each payment made on it with its "
                 "status - and their payments.",
                 who=("string", "Their phone number, email or full name.")),
            tool("ghl_contact", "The agent's contact in GoHighLevel: email, "
                 "phone, tags, when they were added.",
                 who=("string", "Their phone number, email or full name.")),
            tool("search_texts", "Search every text the line has sent or "
                 "received for these words, newest first.",
                 words=("string", "Words that must all appear."),
                 this_agent_only=("boolean", "Only this agent's texts.")),
        ]

    def run(self, name: str, asked: dict) -> str:
        from .. import smsreplies

        said = " ".join(str(
            asked.get("link") or asked.get("who") or asked.get("words") or ""
        ).split())
        try:
            if name == "open_link":
                got = read_link(self.config, said)
                self.checked.append(_what_link(said))
            elif name == "trello_card":
                card = agent_on_the_board(
                    self.config, smsreplies.digits(said) if smsreplies.digits(said) else "",
                    "" if smsreplies.digits(said) else said,
                )
                got = (_card_text(card) + f"\nCard: {card.get('url')}") if card else (
                    f"No New Agent card on the board for {said}."
                )
                self.checked.append("their Trello card" if card else "Trello (no card)")
            elif name == "stripe_customer":
                got = stripe_customer(said)
                self.checked.append("Stripe")
            elif name == "ghl_contact":
                got = ghl_contact(self.config, said)
                self.checked.append("GHL")
            elif name == "payra_payments":
                got = payra_payments(said)
                self.checked.append("Payra")
            elif name == "search_texts":
                from .. import smslinks

                found = smslinks.search(
                    self.texts, said,
                    agent=self.agent if asked.get("this_agent_only") else "",
                )
                got = "\n".join(
                    f"{one.at[:10]} {one.name or _said_by(one)} "
                    f"({_said_by(one)}): {one.said[:300]}" for one in found
                ) or f"No texts with \"{said}\" in them."
                self.checked.append(f"past texts for “{said[:30]}”")
            else:
                return f"There is no look-up called {name}."
        except Exception as exc:
            # In the trail too: "checked Stripe (couldn't)" under a ping says
            # the key needs a permission, where silence would say it looked.
            self.checked.append({
                "trello_card": "Trello", "stripe_customer": "Stripe", "payra_payments": "Payra",
                "ghl_contact": "GHL", "search_texts": "past texts",
            }.get(name, "a link") + " (couldn't)")
            return f"Couldn't look that up: {_short(exc, 200)}"
        got = str(got or "")
        return got[:LOOKUP_CHARS] + ("…" if len(got) > LOOKUP_CHARS else "")


def _what_link(link: str) -> str:
    from .. import smslinks

    kind, what = smslinks.kind_of(link)
    return {"sheet": "the lead sheet", "trello": "the Trello card",
            "stripe_pay": "the Stripe link", "loom": "the Loom"}.get(kind, what)


def read_link(config: Config, link: str) -> str:
    """What is behind a link, in words, opened with RYTE's own access.

    Never raises: a link that cannot be opened says why, and the draft goes
    on without it.
    """
    from .. import smslinks

    kind, what = smslinks.kind_of(link)
    try:
        if kind == "sheet":
            return f"{what} - {_read_sheet(config, link)}"
        if kind == "trello":
            return f"{what} - {_read_trello_link(config, link)}"
        if kind == "loom":
            from .. import loom

            words = loom.transcript(link)
            return f"{what} called \"{loom.title(link)}\" - it says: {words[:1500] or '(no words)'}"
        if kind == "stripe_pay":
            return f"{what} - {_read_payment_link(link)}"
        if kind == "stripe_invoice":
            return (f"{what}. An invoice link can't be looked up by itself - "
                    "look the agent up in Stripe to see whether it is paid.")
        if kind in ("doc", "drive"):
            return (f"{what}. RYTE's Google access doesn't open these, so what "
                    "is in it is not known.")
        return f"{what} - {_read_page(link)}"
    except Exception as exc:
        return f"{what} - couldn't open it: {_short(exc, 200)}"


#: Rows of a sheet shown: the header and the newest.
SHEET_ROWS = 8


def _read_sheet(config: Config, link: str) -> str:
    """The sheet's name, tabs, header, how many rows, and the newest few.

    For "I haven't gotten any leads": the newest row says when the last one
    landed and what it was, which is the whole answer.
    """
    from .. import gsheets

    sheet = gsheets.sheet_id_in(link)
    if not sheet:
        return "not a spreadsheet link RYTE can read."
    client = open_sheets(config)
    try:
        tabs = client.tabs(sheet)
        tab = client.tab_named(sheet, gsheets.gid_in(link)) if gsheets.gid_in(link) else ""
        tab = tab or (str(tabs[0].get("title") or "") if tabs else "")
        rows = [row for row in client.rows(sheet, f"'{tab}'!A1:Z5000") if any(c.strip() for c in row)]
    finally:
        client.close()
    if not rows:
        return f"tab \"{tab}\" is empty. Tabs: {', '.join(str(one.get('title')) for one in tabs)}."
    head, body = rows[0], rows[1:]

    def line(row):
        return " | ".join(cell.strip()[:40] for cell in row)[:300]

    return (
        f"tab \"{tab}\" of {len(tabs)} ({', '.join(str(one.get('title')) for one in tabs[:12])}). "
        f"{len(body)} rows under the header.\nHeader: {line(head)}\n"
        f"Newest {min(SHEET_ROWS, len(body))} rows, last is newest:\n"
        + "\n".join(line(row) for row in body[-SHEET_ROWS:])
    )


def _read_trello_link(config: Config, link: str) -> str:
    found = re.search(r"trello\.com/c/([A-Za-z0-9]+)", link)
    if not found:
        return "not a card link."
    client = open_trello(config)
    try:
        card = client.card_detail(found.group(1))
        lists = {
            str(one.get("id") or ""): str(one.get("name") or "")
            for one in client.board_lists(config.secrets.trello_board_id)
        }
        said = client.card_comments(found.group(1))[:5]
    finally:
        client.close()
    return (
        f"\"{card.get('name')}\" in the \"{lists.get(str(card.get('idList') or ''), '?')}\" "
        f"list.\n{str(card.get('desc') or '')[:CARD_CHARS]}"
        + ("\nNewest comments:\n" + "\n".join(f"- {one[:300]}" for one in said) if said else "")
    )


def _read_payment_link(link: str) -> str:
    """What a Stripe payment link sells, and who has paid through it."""
    from .. import smslinks, stripepay

    wanted = smslinks.same_link(link)
    found, after = None, None
    for _page in range(5):
        params = {"limit": 100, **({"starting_after": after} if after else {})}
        got = stripepay._call("GET", "payment_links", params=params)
        for one in got.get("data") or []:
            if smslinks.same_link(one.get("url") or "") == wanted:
                found = one
                break
        if found or not got.get("has_more") or not got.get("data"):
            break
        after = got["data"][-1]["id"]
    if found is None:
        return "not one of this Stripe account's payment links."
    items = stripepay._call("GET", f"payment_links/{found['id']}/line_items").get("data") or []
    paid = stripepay._call(
        "GET", "checkout/sessions", params={"payment_link": found["id"], "limit": 5},
    ).get("data") or []
    sells = ", ".join(
        f"{one.get('quantity') or 1} × {one.get('description')} "
        f"({stripepay.dollars(int(one.get('amount_total') or 0))})" for one in items
    )
    return (
        f"sells {sells or '?'}; {'active' if found.get('active') else 'switched off'}.\n"
        + ("Paid through it, newest first:\n" + "\n".join(
            f"- {_stamp(one.get('created'))} {((one.get('customer_details') or {}).get('name') or '?')}"
            f" <{(one.get('customer_details') or {}).get('email') or '?'}> - {one.get('status')}"
            f", {one.get('payment_status')}" for one in paid
        ) if paid else "Nobody has paid through it yet.")
    )


def _stamp(seconds) -> str:
    from datetime import timezone

    try:
        return datetime.fromtimestamp(int(seconds), timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return "?"


def stripe_customer(who: str) -> str:
    """The agent in Stripe: their newest invoices and payments."""
    from .. import smsreplies, stripepay

    who = " ".join(str(who or "").split())
    if not who:
        return "Nobody to look up."
    digits = smsreplies.digits(who)
    if "@" in who:
        query = f"email:'{who}'"
    elif len(digits) == 10:
        query = f"phone:'+1{digits}'"
    else:
        query = f"name~'{who.replace(chr(39), '')}'"
    people = stripepay._call("GET", "customers/search", params={"query": query, "limit": 3}).get("data") or []
    if not people:
        return f"No Stripe customer for {who}."
    said = []
    for one in people:
        invoices = stripepay._call("GET", "invoices", params={"customer": one["id"], "limit": 5}).get("data") or []
        charges = stripepay._call("GET", "charges", params={"customer": one["id"], "limit": 5}).get("data") or []
        said.append(
            f"{one.get('name') or '?'} <{one.get('email') or '?'}> {one.get('phone') or ''}".strip()
            + "\nInvoices, newest first:\n" + ("\n".join(
                f"- {_stamp(inv.get('created'))} #{inv.get('number') or '?'} {inv.get('status')}: "
                f"due {stripepay.dollars(int(inv.get('amount_due') or 0))}, paid "
                f"{stripepay.dollars(int(inv.get('amount_paid') or 0))}"
                + (f", due by {_stamp(inv.get('due_date'))}" if inv.get("due_date") else "")
                for inv in invoices
            ) or "- none")
            + "\nPayments, newest first:\n" + ("\n".join(
                f"- {_stamp(ch.get('created'))} {stripepay.dollars(int(ch.get('amount') or 0))} "
                f"{ch.get('status')}{' refunded' if ch.get('refunded') else ''}"
                f" - {ch.get('description') or ''}".rstrip(" -")
                for ch in charges
            ) or "- none")
        )
    return "\n\n".join(said)


def ghl_contact(config: Config, who: str) -> str:
    """The agent's GoHighLevel contact - one search, never the whole walk."""
    from .. import ghl, smsreplies

    who = " ".join(str(who or "").split())
    config.secrets.require("ghl_api_token", "ghl_location_id")
    email = who.casefold() if "@" in who else ""
    phone = smsreplies.digits(who) if len(smsreplies.digits(who)) == 10 else ""
    name = "" if (email or phone) else who.casefold()

    def them(one):
        # A phone saved as "+1 312…" is the same phone as "312…".
        if phone:
            return smsreplies.digits(one.get("phone") or "") == phone
        return ghl._is_them(one, email, "", name)

    with ghl.GHLClient(config.secrets.ghl_api_token, config.secrets.ghl_location_id) as client:
        found = [one for one in client._ask_for(phone or who) if them(one)]
    if not found:
        return f"No GHL contact for {who}."
    return "\n".join(
        f"{' '.join(str(one.get(k) or '') for k in ('firstName', 'lastName')).strip() or one.get('contactName') or '?'}"
        f" <{one.get('email') or '?'}> {one.get('phone') or ''} - added "
        f"{str(one.get('dateAdded') or '?')[:10]}; tags: {', '.join(one.get('tags') or []) or 'none'}"
        for one in found[:3]
    )


#: A web page read for a draft: this much of it.
PAGE_BYTES = 300_000


def _read_page(link: str) -> str:
    """A public web page's title and opening words.

    Only public addresses: the link came from a text anybody can send, and
    this runs on the office Mac, so nothing on its own network is fetched -
    every hop of a redirect is checked the same way.
    """
    import html as htmlmod
    import ipaddress
    import socket
    from urllib.parse import urljoin, urlsplit

    import httpx

    url = link
    for _hop in range(4):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return "not a web address RYTE opens."
        try:
            addresses = {one[4][0] for one in socket.getaddrinfo(parts.hostname, None)}
        except OSError:
            return "that address doesn't exist."
        if not addresses or not all(ipaddress.ip_address(one.split("%")[0]).is_global for one in addresses):
            return "not a public address, so not opened."
        with httpx.stream("GET", url, timeout=10, follow_redirects=False,
                          headers={"User-Agent": "Mozilla/5.0 RYTE"}) as response:
            if response.is_redirect and response.headers.get("location"):
                url = urljoin(url, response.headers["location"])
                continue
            if response.status_code >= 400:
                return f"the page said HTTP {response.status_code}."
            body = b""
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) >= PAGE_BYTES:
                    break
            break
    else:
        return "it redirects too many times."
    page = body.decode("utf-8", "replace")
    title = re.search(r"<title[^>]*>(.*?)</title>", page, re.IGNORECASE | re.DOTALL)
    described = re.search(
        r"<meta[^>]+(?:name|property)=[\"'](?:og:)?description[\"'][^>]+content=[\"']([^\"']*)",
        page, re.IGNORECASE,
    )
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", page)
    text = " ".join(htmlmod.unescape(re.sub(r"<[^>]+>", " ", text)).split())
    return (
        (f"\"{' '.join(htmlmod.unescape(title.group(1)).split())}\". " if title else "")
        + (f"{htmlmod.unescape(described.group(1))}. " if described else "")
        + (f"It reads: {text[:1200]}" if text else "")
    ).strip() or "an empty page."


# ------------------------------------------------ asking how Faith does it

#: Words in "what does Faith send when agents ask…" that say nothing about
#: what is being asked.
_ASKING_WORDS = frozenset(
    "faith send sends sent say says tell tells text texts reply replies respond "
    "responds answer answers use uses usually normally always give gives agent "
    "agents ask asks asking asked they them their when what which does how link "
    "links".split()
)


def faith_answers(config: Config, done: list, question: str) -> dict:
    """How Faith has handled something, from her own replies.

    {"answer", "matched", "links", "terms"}. Her exchanges about it are found
    first - by search terms Claude suggests, because agents say "submit my
    sale", "where do I put my app", "wrote a policy" - and the links in her
    answers are counted here, exactly. Claude then sums up what she does, from
    those exchanges and nothing else.
    """
    from anthropic import Anthropic

    from .. import smslinks, smsreplies

    question = " ".join(str(question or "").split())
    if not question:
        raise ValueError("Ask me what you want to know - like what Faith sends "
                         "when agents ask where to submit a sale.")
    config.secrets.require("anthropic_api_key")
    client = Anthropic(api_key=config.secrets.anthropic_api_key)

    terms = []
    try:
        response = client.messages.create(
            model=config.copy.model, max_tokens=300,
            system=(
                "Turn a question about how a customer service person answers "
                "insurance agents' texts into search terms for finding those "
                "texts: the words and short phrases agents would actually type "
                "when asking it, including misspellings and slang. Lowercase."
            ),
            tools=[{
                "name": "terms", "description": "Search terms, most likely first.",
                "input_schema": {
                    "type": "object",
                    "properties": {"terms": {"type": "array", "items": {"type": "string"}}},
                    "required": ["terms"],
                },
            }],
            tool_choice={"type": "tool", "name": "terms"},
            messages=[{"role": "user", "content": question}],
        )
        terms = [str(one) for one in _tool_input(response, "terms").get("terms") or [] if str(one).strip()][:15]
    except Exception:
        import logging

        logging.getLogger("wilbyte.bot").warning("Couldn't suggest search terms", exc_info=True)
    if not terms:
        terms = sorted(smsreplies.words_in(question) - _ASKING_WORDS)

    matched = smsreplies.about(list(done), terms)
    if not matched:
        return {"answer": "", "matched": [], "links": [], "terms": terms}
    links = smslinks.tally(matched)
    shown = "\n\n".join(
        f"{one.at[:10]} - Agent: {one.asked[:300]}\nFaith: {one.answered[:500]}"
        + (f"\n(Why: {one.why})" if one.why else "")
        for one in sorted(matched, key=lambda swap: swap.at, reverse=True)
    )
    counted = "\n".join(
        f"- {one['link']} ({one['what']}) - in {one['times']} of these answers, last {one['last'][:10]}"
        for one in links[:8]
    ) or "- none"
    response = client.messages.create(
        model=config.copy.model, max_tokens=700,
        system=(
            "Franklin, the general manager of Agent Lead Lab, is asking how "
            "Faith on the customer service team handles something agents text "
            "about. Answer from the real exchanges given and nothing else: "
            "what she sends or says, quoting her exact words and the exact "
            "link, how consistently, and whether it has changed over time. "
            "Some exchanges will be about something else that happened to "
            "share a word - ignore those. If they do not show her handling "
            "it, say so plainly. The link counts are exact; use them. Short: "
            "a few lines, for Discord."
        ),
        messages=[{"role": "user", "content": (
            f"Franklin asks: {question}\n\nLinks in her answers below, counted:\n"
            f"{counted}\n\nHer exchanges that might be about it, newest first:\n\n{shown}"
        )}],
    )
    answer = "".join(
        getattr(block, "text", "") for block in response.content
        if getattr(block, "type", "") == "text"
    ).strip()
    if not answer:
        raise ValueError("The answer came back empty.")
    return {"answer": answer, "matched": matched, "links": links, "terms": terms}


# ------------------------------------------- "does this sound like Faith?"


def faith_voice_check(config: Config, draft: str, done: list, *, playbook: str = "",
                      learned: list = ()) -> dict:
    """Franklin's own reply, held up to how Faith writes.

    {"verdict", "differences", "rewrite", "facts", "examples"}. Her
    exchanges most like this one are the yardstick - for what she says about
    it as much as how - with a few of her newest for how she writes now. The
    rewrite keeps Franklin's facts; one her replies say otherwise about is
    named in "facts", not quietly changed. Nothing is sent.
    """
    from anthropic import Anthropic

    from .. import smsreplies

    draft = str(draft or "").strip()
    if not draft:
        raise ValueError("Put the reply under the question: `@Ryte does this sound like Faith?` "
                         "then your message.")
    examples = smsreplies.closest(list(done), draft, most=6, recent=4)
    if not examples:
        raise ValueError("I have none of Faith's replies to compare with yet.")
    shown = "\n\n".join(
        f"EXAMPLE {at}\nAgent: {one.asked}\nFaith: {one.answered}"
        for at, one in enumerate(examples, start=1)
    )
    sections = []
    if playbook:
        sections.append(f"FAITH'S PLAYBOOK - how she handles agents, from her own replies:\n\n{playbook}")
    if learned:
        sections.append("STANDING RULES FROM HER CORRECTIONS:\n" + "\n".join(f"- {one}" for one in learned))
    tool = {
        "name": "voice_check",
        "description": "How close the draft is to how Faith writes, and the draft in her voice.",
        "input_schema": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": ["yes", "close", "no"],
                            "description": "yes: she could have sent it as is. close: small changes. no: it reads like someone else."},
                "differences": {"type": "array", "items": {"type": "string"}, "description": (
                    "What is unlike her, most noticeable first, each short and concrete - greeting, "
                    "length, wording she never uses, wording she always uses, punctuation, emoji, "
                    "what she would add or leave out. Point at her examples. Empty when nothing is off.")},
                "rewrite": {"type": "string", "description": (
                    "The same reply as Faith would send it - same facts and the same ask, her "
                    "words and shape. Only facts from the draft or plainly her standing habit.")},
                "facts": {"type": "array", "items": {"type": "string"}, "description": (
                    "Anything the draft says that her replies say differently - a timeline, a "
                    "payment rule, a link, a policy - quoting what she says. Empty when none. "
                    "Never facts from anywhere else.")},
            },
            "required": ["verdict", "differences", "rewrite", "facts"],
        },
    }
    config.secrets.require("anthropic_api_key")
    client = Anthropic(api_key=config.secrets.anthropic_api_key)
    response = client.messages.create(
        model=config.copy.model, max_tokens=1200,
        system=(
            "Franklin, the general manager of Agent Lead Lab, wrote a text to an insurance agent "
            "and wants to know whether it sounds like Faith, who normally answers them. Compare it "
            "with her real replies: how she writes - length, greeting, sign-off, capitals, "
            "punctuation, emoji, how warm or brief - and what she says about the same thing. Be "
            "honest and specific, and don't nitpick: if it already sounds like her, say so. Nothing "
            "here is sent; Franklin decides."
        ),
        tools=[tool], tool_choice={"type": "tool", "name": "voice_check"},
        messages=[{"role": "user", "content": (
            ("\n\n---\n".join(sections) + "\n\n---\n" if sections else "")
            + f"Faith's replies most like this one, and her newest:\n\n{shown}\n\n---\n"
            f"Franklin's draft:\n{draft}"
        )}],
    )
    got = _tool_input(response, "voice_check")
    rewrite = str(got.get("rewrite") or "").strip()
    if not rewrite:
        raise ValueError("The check came back empty.")
    return {
        "verdict": str(got.get("verdict") or "close"),
        "differences": [str(one) for one in got.get("differences") or [] if str(one).strip()][:6],
        "rewrite": rewrite,
        "facts": [str(one) for one in got.get("facts") or [] if str(one).strip()][:4],
        "examples": len(examples),
    }


# -------------------------------------------- unticked cards on top of Done

#: Cards floated per pass, at most. The first pass can find weeks of them.
FLOAT_AT_MOST = 60


def float_unticked(config: Config) -> tuple[list[str], list[str]]:
    """Keep every unticked New Agent card at the top of Done. (moved, problems).

    "i want ryte to always move unticked new agent card above / on top" - the
    green tick is how the team says an agent is set up, and one without it
    sitting under a hundred and fifty finished cards is one nobody sees.

    Only what is out of place moves: the unticked cards already on top stay
    where they are, and the ones below the first ticked card go up above
    them, keeping their order among themselves. Once ticked, a card is left
    where it is - the next unticked one below it floats past it.
    """
    from .. import agents, trello

    client = open_trello(config)
    try:
        lists = client.board_lists(config.secrets.trello_board_id)
        done = trello.find_list(lists, agents.DONE)
        if done is None:
            return [], [f"The board has no list called {agents.DONE!r}"]
        done_id = str(done.get("id") or "")
        cards = client.list_cards(done_id)
        unticked = [
            not card.get("dueComplete") and agents.is_agent_card(str(card.get("name") or ""))
            for card in cards
        ]
        top = next((at for at, waiting in enumerate(unticked) if not waiting), len(cards))
        below = [card for at, card in enumerate(cards) if at > top and unticked[at]]
        below = below[:FLOAT_AT_MOST]
        # Bottom one first, so each goes on top of the last and they end up
        # in the order they were in.
        for card in reversed(below):
            client.move_card(str(card.get("id") or ""), done_id, position="top")
        return [str(card.get("name") or "") for card in below], []
    finally:
        client.close()



def cleared_out(config: Config) -> tuple[list[dict], list[str]]:
    """Every clear-out RYTE has done, from the Ryte Collection tab. (rows,
    problems). Each row {"name", "sheet", "channel", "when"} by its heading.
    Reads only."""
    from .. import clearout, gsheets

    link = (getattr(config.secrets, "clients_sheet_link", "") or "").strip()
    sheet_id = gsheets.sheet_id_in(link)
    if not sheet_id:
        return [], ["CLIENTS_SHEET_LINK in .env isn't a spreadsheet link."]
    with gsheets.SheetsClient(gsheets.credentials(config.secrets)) as client:
        titles = [str((one.get("properties") or one).get("title") or "") for one in client.tabs(sheet_id)]
        tab = next((one for one in titles
                    if " ".join(one.split()).casefold() == clearout.COLLECTION_TAB.casefold()), "")
        if not tab:
            return [], [f"No tab called “{clearout.COLLECTION_TAB}” in that spreadsheet."]
        rows = client.rows(sheet_id, f"'{tab}'!A1:Z5000")
    if not rows:
        return [], []
    return clearout.read_rows(rows), []



def payra_payments(who: str) -> str:
    """The agent in Payra: their invoices and what was paid on each, from the
    API copy; and the payments the channel announced, when there are any."""
    from .. import payra, payraapi

    said = []
    account = payraapi.account(payraapi.load(), who)
    if account:
        said.append(account)
    found = payra.find(payra.load(), who)
    if found and not account:
        said.append("Payra payments announced in the payments channel, newest first:\n"
                    + payra.described(found))
    return "\n\n".join(said) or f"No Payra invoice or payment on record for {who}."


def payra_sync(config: Config) -> dict:
    """Read what changed in Payra since last time. {kind: how many}."""
    from .. import payraapi

    token = str(getattr(config.secrets, "payra_api_token", "") or "").strip()
    site = str(getattr(config.secrets, "payra_site_id", "") or "").strip()
    # One at a time: the half-hourly sync and an aged order's look-up both
    # read and write the same file.
    with _PAYRA_SYNCING:
        return _payra_sync(config, token, site)


_PAYRA_SYNCING = threading.Lock()


def _payra_sync(config: Config, token: str, site: str) -> dict:
    from .. import payraapi

    data = payraapi.load()
    try:
        # Three minutes a page: a long stretch asked for at once takes Payra
        # well over the one minute this used to wait.
        with payraapi.PayraClient(token, site, timeout=180.0) as client:
            counts = payraapi.sync(data, client)
        data["synced_at"] = payraapi.when(datetime.now(timezone.utc))
        data.pop("failed", None)
    except Exception as exc:
        data["failed"] = " ".join(str(exc).split())[:200]
        data["failed_at"] = payraapi.when(datetime.now(timezone.utc))
        raise
    finally:
        # Whatever was read before a failure is kept, and so is where it got to.
        payraapi.save(data)
    return counts


# ------------------------------------------------------------------ Payra API


def payra_time(when: datetime, *, zone: str = "Z") -> str:
    """A time as Payra takes it: "2026-08-26T21:55:55.000Z", UTC, milliseconds."""
    when = when.astimezone(timezone.utc)
    return when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}" + zone


def payra_times(when: datetime) -> list[str]:
    """The ways of writing a time Payra might take, likeliest first. Its
    error says "YYYY-MM-DDTHH:mm:ss.SSS" and refused that written exactly so."""
    return [
        payra_time(when),
        payra_time(when, zone="+00:00"),
        payra_time(when, zone=""),
        when.astimezone(timezone.utc).strftime("%Y-%m-%d"),
    ]


#: How far back "payra find" starts. A year asked for at once takes Payra
#: longer than RYTE will wait; a month comes back in seconds.
PAYRA_FIND_DAYS = 150
PAYRA_FIND_PAGES = 40
#: Seconds a page may take. The ordinary sync gives up at sixty.
PAYRA_FIND_WAIT = 240.0


def payra_find(config: Config, target: str, *, now=None, clock=None) -> str:
    """Walk Payra's invoice list page by page looking for one invoice - by its
    number, or the whole name or email on it - keeping every invoice read on
    the way, and saying what each page held. GET only.

    INV-18089 was paid in Payra and never reached RYTE's copy: a year asked
    for in one go timed out, so the history behind the last month never came.
    """
    import time

    from .. import payraapi
    from ..clearout import name_match

    clock = clock or time.monotonic
    token = str(getattr(config.secrets, "payra_api_token", "") or "").strip()
    site = str(getattr(config.secrets, "payra_site_id", "") or "").strip()
    now = now or datetime.now(timezone.utc)
    wanted = " ".join(str(target or "").split()).strip(" .,!?;:").lstrip("#")
    if not wanted:
        return "What should I look for? `@RYTE payra find INV-18089` - or a whole name or email."

    def is_it(record) -> bool:
        one = payraapi.slim_invoice(record)
        if str(one.get("number") or "").casefold() == wanted.casefold():
            return True
        if "@" in wanted:
            return one.get("email") == wanted.casefold()
        return name_match(wanted, one.get("name", "")) >= 2

    data = payraapi.load()
    after = payraapi.when(now - timedelta(days=PAYRA_FIND_DAYS))
    lines, found, read = [], [], 0
    try:
        with payraapi.PayraClient(token, site, timeout=PAYRA_FIND_WAIT) as client:
            for page in range(1, PAYRA_FIND_PAGES + 1):
                started = clock()
                got = client.changed("invoices", after)
                took = clock() - started
                records = got.get("records") or []
                read += len(records)
                for record in records:
                    one = payraapi.slim_invoice(record)
                    if one["id"]:
                        data["invoices"][one["id"]] = one
                    if is_it(record):
                        found.append(one)
                stamps = sorted(str(one.get("updated_at") or "") for one in records if one.get("updated_at"))
                lines.append(
                    f"• Page {page}: from {payraapi._spelled(after)} · {got.get('records_matched', '?')} "
                    f"matched, {len(records)} returned (limit {got.get('limit', '?')}) · "
                    + (f"{payraapi._spelled(stamps[0])} to {payraapi._spelled(stamps[-1])}" if stamps else "no dates")
                    + f" · {took:.0f}s"
                )
                onward = str(got.get("next_updated_after") or "")
                if found or not records or not onward or onward == after:
                    break
                after = onward
    except Exception as exc:
        lines.append(f"• Stopped: {' '.join(str(exc).split())[:160]}")
    # The list only reaches back about a week. One invoice asked for by its
    # id is the other way in, for when the number is one.
    if not found and " " not in wanted and "@" not in wanted:
        try:
            with payraapi.PayraClient(token, site, timeout=PAYRA_FIND_WAIT) as client:
                record = client.one("invoice", wanted)
            if record:
                one = payraapi.slim_invoice(record)
                data["invoices"][one["id"]] = one
                found.append(one)
                lines.append(f"• Asked for “{wanted}” by id: found")
            else:
                lines.append(f"• Asked for “{wanted}” by id: Payra has no invoice with that id")
        except Exception as exc:
            lines.append(f"• Asked for “{wanted}” by id: {' '.join(str(exc).split())[:120]}")
    # Whatever was read is kept - the copy is fuller for it either way.
    payraapi.save(data)

    head = (f"🔎 Payra's invoices from {payraapi._spelled(payraapi.when(now - timedelta(days=PAYRA_FIND_DAYS)))}, "
            f"{read:,} read:\n" + "\n".join(lines[-12:]))
    if not found:
        return head + (
            f"\n\n**“{wanted}” isn't in them.** Payra's list only goes back about a week, so "
            "an older invoice won't be there — `@RYTE invoice` looks in Payra's emails instead. "
            "Every invoice read is now in my copy."
        )
    said = "; ".join(
        f"#{one.get('number') or '?'} {one.get('name') or '?'} {payraapi._money(one.get('total'))}, "
        f"dated {payraapi._spelled(one.get('invoice_date'))}"
        for one in found[:4]
    )
    return head + (f"\n\n✅ **Found:** {said}. It's in my copy now — `@RYTE invoice "
                   f"{found[0].get('number') or wanted}` sends it.")


def payra_reach(config: Config, *, now=None) -> str:
    """How far back Payra's invoice list really goes, and in what order it
    pages - read, never guessed. GET only; only counts and dates are said.

    RYTE held 489 invoices for a year when 381 changed in the last month
    alone, and nothing in "payra status" could say why.
    """
    from .. import payraapi

    token = str(getattr(config.secrets, "payra_api_token", "") or "").strip()
    site = str(getattr(config.secrets, "payra_site_id", "") or "").strip()
    now = now or datetime.now(timezone.utc)
    lines = ["**How far back Payra's invoices go** (a year asked for, two pages read):"]
    with payraapi.PayraClient(token, site) as client:
        after = payraapi.when(now - timedelta(days=payraapi.FIRST_DAYS))
        for page in (1, 2):
            got = client.changed("invoices", after)
            records = got.get("records") or []
            stamps = [str(one.get("updated_at") or "") for one in records if one.get("updated_at")]
            order = ("oldest first" if stamps == sorted(stamps) else
                     "newest first" if stamps == sorted(stamps, reverse=True) else "in no order")
            lines.append(
                f"• Page {page}: asked from {payraapi._spelled(after)}, Payra used "
                f"{payraapi._spelled(got.get('updated_after_applied')) if got.get('updated_after_applied') else '—'}"
                f"{' (capped)' if got.get('updated_after_capped') else ''} · "
                f"{got.get('records_matched', '?')} matched, {len(records)} returned, limit "
                f"{got.get('limit', '?')} · "
                + (f"{payraapi._spelled(min(stamps))} to {payraapi._spelled(max(stamps))}, {order}"
                   if stamps else "no dates")
                + f" · next from {payraapi._spelled(got.get('next_updated_after')) if got.get('next_updated_after') else '—'}"
            )
            onward = str(got.get("next_updated_after") or "")
            if not records or not onward or onward == after:
                break
            after = onward
    return "\n".join(lines)


def payra_probe(config: Config) -> tuple[str, str]:
    """Read Payra's API docs and try the token on what they name. Reads only.

    (what was found, the docs as one markdown file). Every request here is a
    GET; the token is sent only to the addresses the docs themselves give,
    and what comes back is described by its field names, never its values.
    """
    from urllib.parse import quote

    import httpx

    from .. import payradocs

    headers = {"User-Agent": "Mozilla/5.0 RYTE"}
    seen, queue, pages = set(), [payradocs.DOCS_START], []
    with httpx.Client(timeout=20, follow_redirects=True, headers=headers) as web:
        while queue and len(pages) < payradocs.MOST_PAGES:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            try:
                got = web.get(url)
            except httpx.HTTPError as exc:
                pages.append((url, f"(couldn't open: {_short(exc, 120)})"))
                continue
            if got.status_code >= 400:
                pages.append((url, f"(HTTP {got.status_code})"))
                continue
            pages.append((url, payradocs.page_text(got.text)))
            queue += [one for one in payradocs.doc_links(got.text, url) if one not in seen]

    everything = "\n\n".join(text for _url, text in pages)
    docs = "\n\n---\n\n".join(f"# {url}\n\n{text}" for url, text in pages)
    calls = payradocs.calls_in(everything)
    bases = payradocs.api_bases(everything)
    auth = [
        line for line in everything.splitlines()
        if re.search(r"authoriz|bearer|api[\s_-]?key|x-api|token", line, re.IGNORECASE)
    ][:4]

    lines = [
        f"📄 Read **{len(pages)}** page(s) of Payra's docs"
        + (f" — they name **{len(calls)}** API call(s)" if calls else
           " — but found no API calls in them (the docs may only draw in a browser)"),
    ]
    if bases:
        lines.append("API address: " + ", ".join(f"`{one}`" for one in bases))
    if auth:
        lines.append("How the docs say to send the token:\n" + "\n".join(f"> {one[:200]}" for one in auth))

    token = str(getattr(config.secrets, "payra_api_token", "") or "").strip()
    site = str(getattr(config.secrets, "payra_site_id", "") or "").strip()
    # Payra's own words: "updated_after format, must be YYYY-MM-DDTHH:mm:ss.SSS"
    # - and it refused that written exactly so. Each likely way is tried on
    # the first list, and the one it takes is used for the rest.
    choices = payra_times(datetime.now(timezone.utc) - timedelta(days=30))
    tries = payradocs.worth_trying(calls, bases, site_id=site, since=choices[0])
    reads = [path for method, path in calls if method == "GET"]
    if reads:
        lines.append("Read-only calls in the docs:\n" + "\n".join(f"• `GET {one}`" for one in reads[:15]))
    if any("{site_id}" in one for one in reads) and not site:
        lines.append(
            "⚠ Those need your Payra **site id** - PAYRA_SITE_ID isn't in .env. "
            "Ask Payra for it (api-support@hyfin.app), and whether "
            "integrations.hyfin_api_access is turned on for the site."
        )
    if not token:
        lines.append("⚠ PAYRA_API_TOKEN isn't in .env, so I didn't try anything with it.")
    elif not tries:
        lines.append("Nothing to try the token on yet — the docs didn't give a read-only address I could fill in.")
    else:
        lines.append(f"Tried the token on {len(tries)} read-only address(es) — GET only, nothing changed:")
        with httpx.Client(timeout=20, follow_redirects=False) as api:
            listed = next((url for url in tries if "updated_after=" in url), "")
            if listed:
                taken = ""
                for choice in choices:
                    # "+" in an address is a space; the time goes encoded.
                    url = listed.replace(choices[0], quote(choice, safe=":"))
                    try:
                        got = api.get(url, headers={"x-access-token": token, "Accept": "application/json"})
                    except httpx.HTTPError:
                        break
                    if not (got.status_code == 400 and "format" in str(getattr(got, "text", "")).casefold()):
                        taken = choice
                        break
                if taken:
                    lines.append(f"Payra takes times written like `{taken}`.")
                    tries = [url.replace(choices[0], quote(taken, safe=":")) for url in tries]
                else:
                    lines.append("⚠ Payra refused every way of writing the time I tried: "
                                 + ", ".join(f"`{one}`" for one in choices))
            for url in tries:
                try:
                    # Payra's own words: "Every call to the API requires an
                    # x-access-token property be added to the request headers".
                    got = api.get(url, headers={
                        "x-access-token": token, "Accept": "application/json",
                    })
                except httpx.HTTPError as exc:
                    lines.append(f"• `{url}` — couldn't reach it: {_short(exc, 100)}")
                    continue
                shape = ""
                if got.status_code < 300:
                    try:
                        shape = " — " + payradocs.shape_of(got.json())[:600]
                    except ValueError:
                        shape = " — (not JSON)"
                mark = "✅" if got.status_code < 300 else "🔒" if got.status_code in (401, 403) else "⚠"
                said = ""
                if got.status_code >= 300:
                    try:
                        said = " — " + "; ".join(str(one) for one in (got.json().get("errors") or []))[:200]
                    except (ValueError, AttributeError):
                        said = ""
                lines.append(f"• {mark} `{url}` — HTTP {got.status_code}{shape}{said}")
    return "\n".join(lines), docs


# ------------------------------------------------------------ website videos


def _wordpress(config: Config):
    from .. import website

    secrets = config.secrets
    return website.WordPress(
        str(getattr(secrets, "wordpress_url", "") or "").strip(),
        str(getattr(secrets, "wordpress_user", "") or "").strip(),
        str(getattr(secrets, "wordpress_app_password", "") or "").strip(),
    )


def website_check(config: Config) -> str:
    """Whether RYTE can write to the site, and what list it would start from."""
    from .. import website

    with _wordpress(config) as site:
        me = site.me()
        held = site.load()
        if held is None:
            held = website.parse_live(site.live_page())
            where = "the videos on the home page right now (I'll keep the list from the first one you add)"
        else:
            where = "the list I keep"
    roles = ", ".join(me.get("roles") or []) or "?"
    featured = (held.get("featured") or {}).get("name") or "nobody"
    return (f"✅ Signed in to <{site.url}> as **{me.get('name') or me.get('slug') or '?'}** ({roles}).\n"
            f"Reading {where}: **{featured}** featured, {len(held.get('videos') or [])} more in the grid.")


def testimonial_draft(config: Config, link: str, *, name: str = "", quote: str = "",
                      about: str = "") -> dict:
    """The interview's name and a quote for the site, and the paragraph about
    it for the Full Interviews page, before anything is added.
    {"id", "title", "name", "quote", "about"}. Any one given is used as given;
    "about" is "" when the captions couldn't be read to write it."""
    from .. import website

    video_id = youtube.extract_video_id(link)
    try:
        title = youtube.fetch_video(video_id).title
    except Exception:
        title = ""
    transcript, problem = "", ""
    if not (name and quote and about):
        transcript, problem = waiting_on_captions(link)
    if not (name and quote):
        if not transcript and not title:
            raise ValueError(f"I couldn't read that video: {problem or 'no title and no captions'}")
        picked = _pick_testimonial(config, title, transcript)
        name = name or picked.get("name", "")
        quote = quote or picked.get("quote", "")
    if not name:
        raise ValueError("I couldn't tell whose interview it is - add `name: Their Name` to the message.")
    name = " ".join(name.split())
    if not about and transcript:
        try:
            about = _write_about(config, title, name, transcript)
        except Exception:
            import logging

            logging.getLogger("wilbyte.bot").warning("Couldn't write the Full Interviews paragraph", exc_info=True)
            about = ""
    return {"id": video_id, "title": title, "name": name,
            "quote": website.quote_marks(quote), "about": " ".join(str(about or "").split())}


#: How the Full Interviews page already describes an interview - the model
#: for the paragraph RYTE writes for a new one.
ABOUT_EXAMPLE = (
    "Emanuel Nazco walks through his entire path in life insurance, starting with the $15,000 "
    "to $20,000 he lost as a new agent chasing final expense leads he couldn't place. He "
    "explains what changed when he started qualifying budget properly, moved into veteran and "
    "mortgage protection leads, and built a three-option presentation that shows clients what "
    "the money actually does. He covers his dial cadence, his rapport process, instant "
    "approvals, post-sale follow-up, and how he handles the best-time-to-contact field on fresh "
    "leads. For any agent who wants a complete look at how a high-premium veteran writer runs his day."
)


def _write_about(config: Config, title: str, name: str, transcript: str) -> str:
    """The paragraph beside the video on the Full Interviews page."""
    from anthropic import Anthropic

    config.secrets.require("anthropic_api_key")
    client = Anthropic(api_key=config.secrets.anthropic_api_key)
    tool = {
        "name": "about",
        "description": "The paragraph about this interview for the Full Interviews page.",
        "input_schema": {
            "type": "object",
            "properties": {"about": {"type": "string", "description": (
                "One paragraph, 80 to 110 words, third person, present tense, starting with the "
                "agent's full name. What they went through and what they explain - specific "
                "numbers, products and methods only as said in the transcript - ending with who "
                "should watch it. Use the agent's name and pronouns as the interview gives them; "
                "if it doesn't make them clear, use their name or they/them. No quotes, no hype "
                "words, no emojis.")}},
            "required": ["about"],
        },
    }
    response = client.messages.create(
        model=config.copy.model, max_tokens=600,
        system=("You describe full-length interviews with life insurance agents for Agent Lead "
                "Lab's Full Interviews page. Match this example's length and tone exactly:\n\n"
                + ABOUT_EXAMPLE + "\n\nOnly facts the transcript gives. Never the host, Arnold Tarpley."),
        tools=[tool], tool_choice={"type": "tool", "name": "about"},
        messages=[{"role": "user", "content": f"Agent: {name}\nTitle: {title}\n\nTranscript:\n{(transcript or '')[:60000]}"}],
    )
    got = next((one.input for one in response.content if getattr(one, "type", "") == "tool_use"), {}) or {}
    return " ".join(str(got.get("about") or "").split())


def _pick_testimonial(config: Config, title: str, transcript: str) -> dict:
    """Whose interview it is, and the one line worth putting on the site."""
    from anthropic import Anthropic

    config.secrets.require("anthropic_api_key")
    client = Anthropic(api_key=config.secrets.anthropic_api_key)
    tool = {
        "name": "testimonial",
        "description": "The agent interviewed, and one line of theirs for the website.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The interviewed agent's name as the title gives it - first and last when both are given. Never the host, Arnold Tarpley."},
                "quote": {"type": "string", "description": "One sentence or two, 10 to 30 words, in the agent's own words from the transcript - lightly tidied of ums and repeats, never reworded. Punchy and specific: a result, a lesson, a turning point. No quote marks."},
            },
            "required": ["name", "quote"],
        },
    }
    response = client.messages.create(
        model=config.copy.model, max_tokens=400,
        system=("You pick testimonials for Agent Lead Lab's website: interviews with life "
                "insurance agents who buy its leads. Pick the line a sceptical agent would stop "
                "scrolling for. Only words the agent actually said."),
        tools=[tool], tool_choice={"type": "tool", "name": "testimonial"},
        messages=[{"role": "user", "content": f"Title: {title}\n\nTranscript:\n{(transcript or '')[:30000]}"}],
    )
    got = next((one.input for one in response.content if getattr(one, "type", "") == "tool_use"), {}) or {}
    return {"name": str(got.get("name") or ""), "quote": str(got.get("quote") or "")}


def testimonial_lookup(config: Config, said: str) -> tuple[dict, str]:
    """Which interview on the site "remove ..." means. (it, what's featured
    if it goes) - or a WebsiteError saying why not."""
    from .. import website

    with _wordpress(config) as site:
        held = site.load() or website.parse_live(site.live_page())
    found = website.find(held, said)
    if not found:
        raise website.WebsiteError(f"There's no interview on the website for “{said}”.")
    if len(found) > 1:
        raise website.WebsiteError(
            f"{len(found)} interviews on the website are called “{said}” - send the YouTube link instead."
        )
    _left, featured = website.remove(held, found[0]["id"])
    return found[0], featured


def testimonial_take_down(config: Config, video_id: str) -> str:
    """Take one interview off the site. What happened, said."""
    from .. import website

    with _wordpress(config) as site:
        held = site.load() or website.parse_live(site.live_page())
        gone = next((one for one in website.find(held, video_id)), None)
        if gone is None:
            return "It's already off the website."
        was_featured = (held.get("featured") or {}).get("id") == video_id
        updated, featured = website.remove(held, video_id)
        site.save(updated)
    said = (f"🗑 **{gone['name']}** is off the website."
            + (f" **{featured}** is the featured interview again." if was_featured and featured else ""))
    # And off the Success Stories page's full interviews, which follow the
    # website's list - one that's gone from one shouldn't stay on the other.
    try:
        from .. import successpage

        with _wordpress(config) as site:
            success, _started = _success_list(site)
            left, went = successpage.remove(success, video_id)
            if went:
                _success_save(site, left)
                said += "\n🗑 Off the Success Stories page too."
    except Exception:
        import logging

        logging.getLogger("wilbyte.bot").warning("Couldn't check the Success Stories page", exc_info=True)
    try:
        if full_page_take_down(config, video_id):
            said += "\n🗑 Off the Full Interviews page too, if it was there."
    except Exception:
        import logging

        logging.getLogger("wilbyte.bot").warning("Couldn't update the Full Interviews page", exc_info=True)
    return said


def full_page_list(site) -> dict:
    """What RYTE keeps for the Full Interviews page - empty to start."""
    from .. import fullpage

    return fullpage._clean(site.load(slug=fullpage.DATA_SLUG, mark=fullpage.MARK) or fullpage.empty())


def _full_page_save(site, data: dict) -> None:
    from .. import fullpage

    site.save(data, slug=fullpage.DATA_SLUG, title=fullpage.DATA_TITLE, mark=fullpage.MARK, what=fullpage.WHAT)


def site_code_publish(config: Config) -> list[str]:
    """Each page script RYTE keeps on leadlabcrm.com brought up to date with
    its copy here - written only when it changed. A line each."""
    from .. import sitecode

    lines = []
    with _wordpress(config) as site:
        for name in sitecode.SCRIPTS:
            js = sitecode.code(name)
            held = site.load(slug=sitecode.slug(name), mark=sitecode.MARK) or {}
            if held.get("js") == js:
                lines.append(f"✅ The {name} script on the site is the current one.")
                continue
            site.save({"js": js}, slug=sitecode.slug(name), title=f"Page script: {name} (RYTE)",
                      mark=sitecode.MARK, what=f"the {name} page script")
            lines.append(f"✅ Put the current {name} script on the site.")
    return lines


def full_page_add(config: Config, video: dict) -> None:
    """The interview first on the Full Interviews page."""
    from .. import fullpage

    with _wordpress(config) as site:
        _full_page_save(site, fullpage.add(full_page_list(site), {
            "id": video["id"], "title": video.get("title") or f"{video['name']} - Full Interview",
            "name": video["name"], "about": video["about"]}))


def full_page_take_down(config: Config, video_id: str) -> bool:
    """The interview off the Full Interviews page, whoever built its row.
    Whether anything changed."""
    from .. import fullpage

    with _wordpress(config) as site:
        held = full_page_list(site)
        if video_id in held["gone"]:
            return False
        _full_page_save(site, fullpage.remove(held, video_id))
    return True


def success_plan(config: Config, who: str) -> tuple[str, list[dict], list[str]]:
    """The segments in one posting-doc tab that can go on the Success Stories
    page. (the tab's name, [items], [skipped]) - or a SuccessError."""
    from .. import docs as doc
    from .. import successpage

    if not (getattr(config.secrets, "segments_doc_id", "") or "").strip():
        raise successpage.SuccessError("SEGMENTS_DOC_ID isn't set, so there's no posting doc to read.")
    wanted = " ".join(str(who or "").split()).casefold()
    if not wanted:
        raise successpage.SuccessError("Whose? `@RYTE post segments Karyn Giles` - their tab's name.")
    with doc.open_docs(config.secrets) as reading:
        every = reading.contents()
    called = lambda tab: " ".join(tab.title.split()).casefold()  # noqa: E731
    picked = [one for one in every if called(one[0]) == wanted] or [
        one for one in every if wanted in called(one[0])
    ]
    if not picked:
        # "post segments Crsytal Clark" - a slip of the keys is still her
        # tab, when it's close to one tab and no other.
        import difflib

        close = difflib.get_close_matches(wanted, [called(one[0]) for one in every], n=2, cutoff=0.8)
        if len(close) == 1 or (len(close) == 2 and difflib.SequenceMatcher(None, wanted, close[0]).ratio()
                               - difflib.SequenceMatcher(None, wanted, close[1]).ratio() >= 0.1):
            picked = [one for one in every if called(one[0]) == close[0]]
    if not picked:
        raise successpage.SuccessError(f"No tab called “{who}” in the posting doc.")
    if len(picked) > 1:
        raise successpage.SuccessError(
            f"More than one tab could be “{who}” - say which: "
            + ", ".join(f"**{one[0].title}**" for one in picked)
        )
    tab, text, _odd = picked[0]
    items, skipped, waiting = successpage.from_tab(tab.title, text)
    if waiting:
        found, missing = _find_on_channel(config, items, waiting)
        items += found
        skipped += missing
    # In the doc's order, whichever way each link was found.
    order = {one["what"]: at for at, one in enumerate(items + waiting)}
    items.sort(key=lambda one: order.get(one["what"], 0))
    # What's already on the page is said before the button, not after it -
    # the button's count is what will actually go up.
    try:
        with _wordpress(config) as site:
            held, _started = _success_list(site)
        _left, fresh, already = successpage.add(held, items)
        items = fresh
        skipped += [f"{one['what']} — {one['why']}, so not again" for one in already]
    except Exception:
        pass  # checked again when it is written
    return tab.title, items, skipped


#: Uploads read when looking for a segment by its title - months of clips.
CHANNEL_UPLOADS_READ = 300


def _channel_id(said: str) -> str:
    """A UC... id, from itself or from an @handle."""
    from .. import youtube_api

    if said.startswith("UC"):
        return said
    got = youtube_api._get("channels", {"part": "id", "forHandle": said})
    return str(((got.get("items") or [{}])[0]).get("id") or "")


def _find_on_channel(config: Config, items: list[dict], waiting: list[dict]):
    """Segments with no link in the doc, looked for by their exact title on
    the channels: the one the interview's own video is on first, then the ones
    in YOUTUBE_CHANNEL_ID in the order given. ([found], [why each other
    one wasn't])."""
    from .. import successpage, youtube_api

    sure = next((one["id"] for one in items if one.get("id")), "")
    channels: list[str] = []
    try:
        if sure:
            channels.append(str(((youtube_api.get_video(sure) or {}).get("snippet") or {}).get("channelId") or ""))
        for said in successpage.channels_in(getattr(config.secrets, "youtube_channel_id", "") or ""):
            channels.append(_channel_id(said))
        channels = [one for one in dict.fromkeys(channels) if one.startswith("UC")]
        if not channels:
            return [], [f"{one['what']} — no YouTube link yet" for one in waiting]
        by_channel = [
            [{"id": (it.get("contentDetails") or {}).get("videoId")
                    or ((it.get("snippet") or {}).get("resourceId") or {}).get("videoId"),
              "title": (it.get("snippet") or {}).get("title") or ""}
             for it in youtube_api.list_playlist_items("UU" + one[2:], limit=CHANNEL_UPLOADS_READ)]
            for one in channels
        ]
    except Exception as exc:
        return [], [f"{one['what']} — no YouTube link yet (couldn't read the channel: {_short(exc, 80)})"
                    for one in waiting]
    return successpage.match_channels(waiting, by_channel)


def _success_list(site):
    """(the list RYTE keeps, whether it was just started from the live page)."""
    from .. import successpage

    held = site.load(slug=successpage.DATA_SLUG, mark=successpage.MARK)
    if held and isinstance(held.get("sections"), list):
        return held, False
    return successpage.parse_live(site.live_page(successpage.PAGE_URL)), True


def _success_save(site, data) -> None:
    from .. import successpage

    site.save(data, slug=successpage.DATA_SLUG, title=successpage.DATA_TITLE,
              mark=successpage.MARK, what=successpage.WHAT)


def success_post(config: Config, items: list[dict]) -> str:
    """Put these on the Success Stories page, each first in its section."""
    from .. import successpage

    with _wordpress(config) as site:
        held, started = _success_list(site)
        updated, went, already = successpage.add(held, items)
        if went:
            _success_save(site, updated)
    lines = [f"✅ **{one['title']}** → {one['section']}" for one in went]
    lines += [f"• Not added: {one['title']} — {one['why']}" for one in already]
    if started and went:
        lines.append("-# First one: I started from the videos already on the page.")
    # And first on each section's own training page, with the doc's paragraph.
    if went:
        try:
            lines += training_pages_add(config, went)
        except Exception as exc:
            lines.append(f"-# Couldn't update the training pages: {_short(exc, 120)}")
    return "\n".join(lines) or "Nothing to add."


def training_pages_add(config: Config, items: list[dict]) -> list[str]:
    """Each video first on its section's own training page - one that has
    its WEBSITE DESCRIPTION in the doc. What happened, a line each."""
    from .. import fullpage

    placed = [one for one in items if one.get("about") and one.get("section") in fullpage.PAGES]
    lines = [f"-# Not on the {one['section']} page: no WEBSITE DESCRIPTION for {one['what']} in the doc."
             for one in items if not one.get("about") and one.get("section") in fullpage.PAGES]
    if not placed:
        return lines
    with _wordpress(config) as site:
        held = full_page_list(site)
        # Last first, so the doc's order is the order they show in.
        for one in reversed(placed):
            held = fullpage.add(held, {"id": one["id"], "title": one["title"], "name": one.get("who") or "",
                                       "about": one["about"]}, one["section"])
        _full_page_save(site, held)
    pages = list(dict.fromkeys(one["section"] for one in placed))
    return lines + ["✅ First on its training page too: " + ", ".join(f"**{one}**" for one in pages)]


def success_lookup(config: Config, said: str) -> list[dict]:
    """Where a YouTube link is on the Success Stories page - [] when nowhere."""
    from .. import successpage

    vid = successpage.video_id(said)
    if not vid:
        raise successpage.SuccessError("Send the video's YouTube link: `@RYTE success remove https://youtu.be/…`")
    with _wordpress(config) as site:
        held, _started = _success_list(site)
    _left, gone = successpage.remove(held, vid)
    return gone


def success_take_down(config: Config, said: str) -> str:
    from .. import successpage

    vid = successpage.video_id(said)
    with _wordpress(config) as site:
        held, _started = _success_list(site)
        updated, gone = successpage.remove(held, vid)
        if not gone:
            return "It's already off the Success Stories page."
        _success_save(site, updated)
    said = "🗑 Off the Success Stories page: " + "; ".join(
        f"**{one['t']}** ({one['section']})" for one in gone
    )
    # And off that section's own training page.
    try:
        from .. import fullpage

        sections = [one for one in dict.fromkeys(row["section"] for row in gone) if one in fullpage.PAGES]
        if sections:
            with _wordpress(config) as site:
                held = full_page_list(site)
                for one in sections:
                    held = fullpage.remove(held, vid, one)
                _full_page_save(site, held)
            said += "\n🗑 Off its training page too."
    except Exception as exc:
        said += f"\n-# Couldn't update the training pages: {_short(exc, 120)}"
    return said


def _youtube_title(video_id: str) -> str:
    """A video's own title, from YouTube's oEmbed - no key, one small GET."""
    import httpx

    try:
        got = httpx.get("https://www.youtube.com/oembed",
                        params={"url": f"https://youtu.be/{video_id}", "format": "json"}, timeout=15)
        return str(got.json().get("title") or "") if got.status_code == 200 else ""
    except Exception:
        return ""


def full_interviews_plan(config: Config) -> tuple[dict, dict, list[dict], list[dict]]:
    """The Success Stories page's full interviews, put in the order of the
    website's own interview list - its featured one, then the rest, newest
    first. (the list, its updated version, what would be added, what's left
    out and why). Reads only."""
    from .. import segments, successpage

    with _wordpress(config) as site:
        interviews = site.load() or {}
        held, _started = _success_list(site)
    known = {v.get("id"): v for row in held.get("sections") or [] for v in row.get("v") or []}
    ordered = []
    for one in [interviews.get("featured")] + list(interviews.get("videos") or []):
        if not isinstance(one, dict) or not one.get("id") or any(o["id"] == one["id"] for o in ordered):
            continue
        name = str(one.get("name") or "").strip()
        title = str((known.get(one["id"]) or {}).get("t") or "") or _youtube_title(one["id"]) \
            or f"{name} — Full Interview"
        ordered.append({"section": segments.FULL_INTERVIEWS, "id": one["id"], "title": title,
                        "who": name, "what": name or one["id"]})
    updated, added, left = successpage.follow(held, segments.FULL_INTERVIEWS, ordered)
    return held, updated, added, left


def full_interviews_save(config: Config, updated: dict) -> None:
    with _wordpress(config) as site:
        _success_save(site, updated)


def testimonial_publish(config: Config, video: dict) -> str:
    """Put the interview on the site as the featured one. What happened, said."""
    from .. import website

    with _wordpress(config) as site:
        held = site.load()
        started = held is None
        if started:
            held = website.parse_live(site.live_page())
        updated, moved = website.add(held, video)
        site.save(updated)
    said = (f"✅ **{video['name']}** is the featured interview on the website now"
            + (f", and **{moved}** moved to the top of the grid" if moved else "") + ".")
    # And first under Agent Success Full Interviews on the Success Stories
    # page, which is meant to be the newest interviews too.
    try:
        _held, success, added, _left = full_interviews_plan(config)
        if added:
            full_interviews_save(config, success)
            said += "\n✅ First under **Agent Success Full Interviews** on the Success Stories page too."
    except Exception as exc:
        said += f"\n-# Couldn't put it on the Success Stories page: {_short(exc, 120)}"
    # And first on the Full Interviews page, which needs its paragraph.
    if video.get("about"):
        try:
            full_page_add(config, video)
            said += "\n✅ First on the **Full Interviews** page too."
        except Exception as exc:
            said += f"\n-# Couldn't put it on the Full Interviews page: {_short(exc, 120)}"
    else:
        said += ("\n-# Not on the Full Interviews page: there's no paragraph about it. Send it again "
                 "with `about: …` after the quote.")
    if started:
        said += (f"\n-# First one: I started from the {len(held.get('videos') or [])} interviews "
                 "already on the page.")
    return said


# ------------------------------------------- the setups, against the hub

#: How far back a payment counts as this order's.
SETUP_PAID_DAYS = 30


def read_setup_thread(config: Config, agent, messages: list[dict], *, today) -> dict:
    """What a client's thread says they bought and how it's set up - read by
    Claude, screenshots included - and whether the hub's hours fit it.
    `messages` are {"who", "when", "text", "images"} oldest first."""
    from anthropic import Anthropic

    config.secrets.require("anthropic_api_key")
    client = Anthropic(api_key=config.secrets.anthropic_api_key)
    lines, images = [], []
    for one in messages[-60:]:
        lines.append(f"[{one.get('when', '')}] {one.get('who', '?')}: {one.get('text', '')}")
        images += list(one.get("images") or [])
    hub_said = (f"Lead type {agent.lead_type or '?'}; order size {agent.ordered}; daily cap "
                f"{agent.daily_cap or 0} (0 means the hub paces it); states "
                f"{', '.join(agent.states) or 'not given'}; hours {agent.schedule or 'not given'}; "
                f"live date {agent.start_date or 'not given'}; status {agent.status or '?'}.")
    tool = {
        "name": "setup",
        "description": "What the thread says this client bought and how their setup should be.",
        "input_schema": {
            "type": "object",
            "properties": {
                "lead_type": {"type": "string", "description": "The leads bought, as the thread says it ('Text-Verified Veteran', 'Trucker IUL', 'OTP Vets'). Empty if it doesn't say."},
                "leads": {"type": "integer", "description": "How many leads were bought. 0 if it doesn't say."},
                "daily_cap": {"type": "integer", "description": "A most-per-day the client asked for. 0 if none was asked."},
                "states": {"type": "array", "items": {"type": "string"}, "description": "Two-letter state codes asked for. Empty if not said."},
                "hours": {"type": "string", "description": "When leads should arrive, as asked ('9-12:30pm CST Mon-Fri, no weekends'). Empty if not said."},
                "start_date": {"type": "string", "description": "The go-live / start date as YYYY-MM-DD, worked out from the message dates for words like 'today' or 'tomorrow'. Empty if not said."},
                "same_as_before": {"type": "boolean", "description": "Whether it says the setup is the same as their last order."},
                "hours_match": {"type": "string", "enum": ["yes", "no", "unclear"], "description": "Whether the hub's hours fit the hours asked. 'unclear' when either side doesn't say."},
                "hours_why": {"type": "string", "description": "When hours_match is 'no': what was asked against what the hub has, one short line."},
                "live": {"type": "boolean", "description": "Whether somebody said they are live now."},
                "this_order": {"type": "boolean", "description": (
                    "Whether anything in the thread is about THIS order - the one the hub has, by its lead "
                    "type and live date. False when the thread only talks about their other or earlier "
                    "orders; then leave every other field empty.")},
                "thread_about": {"type": "string", "description": (
                    "When this_order is false: what the thread IS about, under 15 words - "
                    "'their 09/24 OTP Vet order, 25 leads'.")},
            },
            "required": ["this_order", "lead_type", "leads", "daily_cap", "states", "hours", "start_date",
                         "same_as_before", "hours_match", "live"],
        },
    }
    content = [{"type": "text", "text": (
        f"Today is {today:%Y-%m-%d}. The client is {agent.name}.\n\nThe order being checked - the hub's "
        f"setup for it: {hub_said}\n\n"
        "The thread, oldest first:\n" + "\n".join(lines))}]
    for url in images[:4]:
        content.append({"type": "image", "source": {"type": "url", "url": url}})
    try:
        response = client.messages.create(
            model=config.copy.model, max_tokens=700,
            system=("You read Agent Lead Lab's team threads about a client's lead order: what they "
                    "bought and how the delivery should be set up. Use only what the thread says - "
                    "messages and screenshots. A client's thread often holds several orders - earlier "
                    "ones, or two bought together (Final Expense and Mortgage). Read ONLY what's about "
                    "the order the hub has: its lead type and live date. Never fill a field from another "
                    "order. Later messages about this order override earlier ones. Leave a field empty "
                    "rather than guess."),
            tools=[tool], tool_choice={"type": "tool", "name": "setup"},
            messages=[{"role": "user", "content": content}],
        )
    except Exception:
        if not images:
            raise
        # A screenshot Claude couldn't fetch shouldn't cost the whole read.
        response = client.messages.create(
            model=config.copy.model, max_tokens=700,
            system="You read Agent Lead Lab's team threads about a client's lead order. Only what the thread says.",
            tools=[tool], tool_choice={"type": "tool", "name": "setup"},
            messages=[{"role": "user", "content": content[:1]}],
        )
    return _tool_input(response, "setup")


def paid_by_zelle(agent, posts) -> bool:
    """Whether a post in the Zelle channel (#new-payment) names them - their
    whole name, or the hub's email. `posts` are the last month's, as text."""
    from .. import clearout

    email = str((agent.raw or {}).get("email") or "").strip().casefold()
    for text in posts or []:
        said = str(text or "")
        if (email and email in said.casefold()) or any(
                clearout.name_match(agent.name, line) >= 2 for line in said.splitlines() if line.strip()):
            return True
    return False


def paid_for(agent, *, now=None, zelle=None) -> bool | None:
    """Whether they've paid in the last month: Payra - by the hub's email,
    else their whole name - or a Zelle payment posted in #new-payment. None
    when Payra hasn't been read and Zelle doesn't show it either."""
    from .. import clearout, payraapi

    if zelle and paid_by_zelle(agent, zelle):
        return True
    data = payraapi.load()
    if not data.get("invoices"):
        return None
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=SETUP_PAID_DAYS)
    email = str((agent.raw or {}).get("email") or "").strip().casefold()
    payments = []
    for invoice in (data.get("invoices") or {}).values():
        theirs = (email and str(invoice.get("email") or "").strip().casefold() == email) or \
            clearout.name_match(agent.name, str(invoice.get("name") or "")) >= 2
        if theirs:
            payments += payraapi.payments_on(data, invoice)
    # A charge with no invoice behind it, under their name.
    payments += [one for one in (data.get("payments") or {}).values()
                 if clearout.name_match(agent.name, str(one.get("customer") or "")) >= 2]
    for payment in payments:
        if not payraapi._went_through(payment):
            continue
        try:
            when = datetime.fromisoformat(str(payment.get("paid_on") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
        if when >= since:
            return True
    return False


def _ryte_card(client, config):
    """(the Ryte Checklist card, its checklists by name). Found by name on the
    board; the two checklists RYTE uses are made if missing."""
    from .. import setupcheck

    cards = client.board_cards(config.secrets.trello_board_id)
    wanted = setupcheck.CARD_NAME.casefold()
    # The newest, when there's more than one - "Ryte Checklist" remade fresh
    # leaves the old one on the board. A Trello id starts with when it was made.
    named = [one for one in cards if " ".join(str(one.get("name") or "").split()).casefold() == wanted]
    card = max(named, key=lambda one: str(one.get("id") or "")) if named else None
    if card is None:
        raise ValueError(f"There's no “{setupcheck.CARD_NAME}” card on the board - it's where I keep the setups.")
    lists = {str(one.get("name") or "").strip().casefold(): one
             for one in client.card_checklists(str(card["id"]))}
    for name in (setupcheck.SETUP_LIST, setupcheck.CONCERNS_LIST):
        if name.casefold() not in lists:
            made = client.create_checklist(str(card["id"]), name)
            made.setdefault("checkItems", [])
            lists[name.casefold()] = made
    return card, lists


def _franklin_tag(client, config) -> str:
    """"@username" for Franklin on the board, so a comment notifies him."""
    named = (getattr(config.secrets, "trello_franklin", "") or "").strip().lstrip("@")
    if named:
        return f"@{named}"
    try:
        for one in client.board_members(config.secrets.trello_board_id):
            if "franklin" in one.get("fullName", "").casefold() or "franklin" in one.get("username", "").casefold():
                return f"@{one['username']}"
    except Exception:
        pass
    return ""


def sync_ryte_card(config: Config, results: list, *, said_before: dict) -> tuple[list, dict, list[str]]:
    """The Ryte Checklist card brought up to date with these checks.

    An item a client under Agent Setup - added once, ticked when every check
    passes and unticked if one stops passing. What's wrong goes under Concerns
    once, ticked there when it's put right, and a comment tagging Franklin
    says it the first time. (newly wrong [(title, lines, thread link)], what
    has been said now, problems)."""
    from .. import setupcheck

    client = open_trello(config)
    try:
        card, lists = _ryte_card(client, config)
        card_id = str(card["id"])
        setup = lists[setupcheck.SETUP_LIST.casefold()]
        concerns = lists[setupcheck.CONCERNS_LIST.casefold()]
        items = {" ".join(str(one.get("name") or "").split()): one for one in setup.get("checkItems") or []}
        raised = {" ".join(str(one.get("name") or "").split()): one for one in concerns.get("checkItems") or []}
        tag = None
        fresh, said_now = [], dict(said_before)
        for result in results:
            agent = result.agent
            title = setupcheck.item_title(agent.name, agent.lead_type, agent.start_date)
            stem = title.split(" - ")[0] + " - "
            item = items.get(title)
            if item is None:
                # Same order, a new live date: the item is renamed, not doubled.
                older = next((one for name, one in items.items() if name.startswith(stem)
                              and said_before.get(setupcheck.key_of(agent), {}).get("title") == name), None)
                if older is not None:
                    client.set_check_item(card_id, str(older["id"]), name=title)
                    item = older
                else:
                    item = client.add_check_item(str(setup["id"]), title)
                items[title] = item
            done = str(item.get("state") or "") == "complete"
            if done != result.all_good:
                client.set_check_item(card_id, str(item["id"]), done=result.all_good)

            lines = setupcheck.concern_lines(result)
            for line in lines:
                if line in raised:
                    continue
                # The same problem said differently - "no thread" becoming
                # "doesn't mention this order" - is that line reworded, not
                # one ticked as put right and another added.
                kind = setupcheck.concern_kind(line)
                same = next((name for name, one in raised.items()
                             if setupcheck.concern_kind(name) == kind and name not in lines
                             and str(one.get("state") or "") != "complete"), None)
                if same is not None:
                    item = raised.pop(same)
                    client.set_check_item(card_id, str(item["id"]), name=line)
                    raised[line] = item
                else:
                    raised[line] = client.add_check_item(str(concerns["id"]), line)
            # Put right since: ticked off under Concerns, left there to see.
            for name, one in raised.items():
                if name.startswith(title + ":") and name not in lines and str(one.get("state") or "") != "complete":
                    client.set_check_item(card_id, str(one["id"]), done=True)
                    one["state"] = "complete"

            key = setupcheck.key_of(agent)
            # Said once a kind: a reworded one isn't news.
            before = {setupcheck.concern_kind(line) for line in said_before.get(key, {}).get("lines") or []}
            new = [line for line in lines if setupcheck.concern_kind(line) not in before]
            if new:
                if tag is None:
                    tag = _franklin_tag(client, config)
                link = result.link
                client.add_comment(card_id, "\n".join(
                    [f"{tag} ⚠ **{title}**".strip()] + [f"• {line.split(': ', 1)[1]}" for line in new]
                    + ([f"See: {link}"] if link else [])))
                fresh.append((title, new, link))
            said_now[key] = {"title": title, "lines": lines}
        return fresh, said_now, []
    except Exception as exc:
        return [], said_before, [f"Couldn't update the Ryte Checklist card: {_short(exc, 160)}"]
    finally:
        client.close()


def ryte_card_note(config: Config, text: str) -> None:
    """A comment on the Ryte Checklist card tagging Franklin."""
    client = open_trello(config)
    try:
        card, _lists = _ryte_card(client, config)
        tag = _franklin_tag(client, config)
        client.add_comment(str(card["id"]), f"{tag} {text}".strip())
    finally:
        client.close()


# ------------------------------------------- what the team asks each other

def read_team_request(config: Config, *, asker: str, asked: list[str], text: str,
                      images: list[str], where: str, forced: bool = False) -> dict:
    """Whether a message in the Growth server asks the people it tags to do a
    piece of work, and the work in a line. {"task": bool, "title": str}.
    `forced` is Franklin saying "track this": it's a task, just name it."""
    from anthropic import Anthropic

    config.secrets.require("anthropic_api_key")
    client = Anthropic(api_key=config.secrets.anthropic_api_key)
    tool = {
        "name": "request",
        "description": "Whether this is a job for the people tagged, and what the job is.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "boolean", "description": (
                    "True when it asks the tagged people to do something that will be finished at some "
                    "point - set a cap, change a schedule, add states, send something, fix a setup. False "
                    "for chat, thanks, announcements, FYIs, questions that only need an answer, and "
                    "meeting reminders.")},
                "about_agent": {"type": "boolean", "description": (
                    "True when the job is about a client agent and their leads - their setup, cap, states, "
                    "schedule, sheet, replacements, launch, payment, pause, a message to them. False for "
                    "the team's own work: marketing, content, ads, videos, hiring, tools, admin.")},
                "title": {"type": "string", "description": (
                    "The job in under 12 words, the client's name first when there is one: "
                    "'Michael Greff - set a 10/day cap', 'Lauren Huddleston - add TX to states'. "
                    "When the message doesn't name them, the post a thread hangs off usually does "
                    "(a payment's NAME). Read the screenshots for what's being asked.")},
            },
            "required": ["task", "about_agent", "title"],
        },
    }
    content = [{"type": "text", "text": (
        f"In {where}, {asker} wrote this, tagging {', '.join(asked) or 'the whole team (@here or a role)'}:\n\n{text}"
        + ("\n\n(It's been marked as a task - just name it.)" if forced else ""))}]
    for url in images[:3]:
        content.append({"type": "image", "source": {"type": "url", "url": url}})
    system = ("You read Agent Lead Lab's team chat - a lead-generation company: clients buy leads, "
              "the team sets up their delivery (caps, states, hours) on the hub. Say whether a message "
              "hands somebody a job, and name the job.")
    try:
        response = client.messages.create(model=config.copy.model, max_tokens=200, system=system,
                                          tools=[tool], tool_choice={"type": "tool", "name": "request"},
                                          messages=[{"role": "user", "content": content}])
    except Exception:
        if not images:
            raise
        response = client.messages.create(model=config.copy.model, max_tokens=200, system=system,
                                          tools=[tool], tool_choice={"type": "tool", "name": "request"},
                                          messages=[{"role": "user", "content": content[:1]}])
    got = _tool_input(response, "request")
    title = " ".join(str(got.get("title") or "").split())[:120]
    # "as long as its agents related": the team's own work isn't tracked.
    return {"task": (bool(got.get("task")) and bool(got.get("about_agent"))) or forced,
            "title": title or "Team request"}


#: Items a checklist holds before the next one's started - Trello refuses
#: past 200 ("CHECKLIST_TOO_MANY_CHECK_ITEMS"), and every team request since
#: Concerns filled up was lost to it.
TASKS_PER_LIST = 195


def _tasks_list(client, config, *, full: bool = False):
    """(card id, the checklist team requests go on - Concerns, or "Concerns
    (2)", "(3)"... once one's full) - made if there's no room. `full`: the
    newest one was refused for being full, whatever its count says."""
    from .. import teamtasks

    card, _lists = _ryte_card(client, config)
    base = teamtasks.TASKS_LIST.casefold()
    ours = []
    for one in client.card_checklists(str(card["id"])):
        name = " ".join(str(one.get("name") or "").split()).casefold()
        if name == base:
            ours.append((1, one))
        else:
            numbered = re.fullmatch(re.escape(base) + r"\s*\((\d+)\)", name)
            if numbered:
                ours.append((int(numbered.group(1)), one))
    ours.sort(key=lambda pair: pair[0])
    if ours:
        number, newest = ours[-1]
        if not full and len(newest.get("checkItems") or []) < TASKS_PER_LIST:
            return str(card["id"]), newest
        return str(card["id"]), client.create_checklist(str(card["id"]), f"{teamtasks.TASKS_LIST} ({number + 1})")
    return str(card["id"]), client.create_checklist(str(card["id"]), teamtasks.TASKS_LIST)


def team_task_add(config: Config, task: dict) -> str:
    """The request as an item on the Ryte Checklist, under Concerns - the
    next Concerns checklist when that one's full. Its item id."""
    from .. import teamtasks, trello

    client = open_trello(config)
    try:
        _card_id, tasks = _tasks_list(client, config)
        try:
            item = client.add_check_item(str(tasks["id"]), teamtasks.item_name(task))
        except trello.TrelloError as exc:
            if "TOO_MANY_CHECK_ITEMS" not in str(exc):
                raise
            _card_id, tasks = _tasks_list(client, config, full=True)
            item = client.add_check_item(str(tasks["id"]), teamtasks.item_name(task))
        return str(item["id"])
    finally:
        client.close()


def ryte_card_items(config: Config) -> dict:
    """{item id: "complete" | "incomplete"} for every item on the Ryte
    Checklist card - what the card says, ticked by hand or not."""
    client = open_trello(config)
    try:
        card, _lists = _ryte_card(client, config)
        found = {}
        for checklist in client.card_checklists(str(card["id"])):
            for item in checklist.get("checkItems") or []:
                found[str(item.get("id") or "")] = str(item.get("state") or "")
        return found
    finally:
        client.close()


def team_task_done(config: Config, task: dict) -> None:
    """Ticked, with who did it and when. Only ever an item RYTE added."""
    from .. import teamtasks

    if not task.get("item_id"):
        return
    client = open_trello(config)
    try:
        card, _lists = _ryte_card(client, config)
        client.set_check_item(str(card["id"]), str(task["item_id"]), done=True,
                              name=teamtasks.done_name(task))
    finally:
        client.close()


# ------------------------------------------- orders made into cards

def order_template(config: Config, kind: str) -> dict:
    """The example card for this kind of order: {"id", "name", "desc", "url"}.
    Raises when there isn't one set or it can't be read."""
    from .. import ordercards

    link = ordercards.templates().get(kind) or ""
    card_id = ordercards.card_id_of(link)
    if not card_id:
        raise ValueError(f"There's no example {ordercards.KINDS[kind]} card set - "
                         f"`@Ryte {'new agent' if kind == 'fresh' else 'aged'} template <trello link>`.")
    client = open_trello(config)
    try:
        card = client.card_full(card_id)
    finally:
        client.close()
    return {"id": str(card.get("id") or card_id), "name": str(card.get("name") or ""),
            "desc": str(card.get("desc") or ""), "url": str(card.get("shortUrl") or link)}


#: The model that reads an order - fast: "it should not take too long ...
#: as long as the emoji pop ups ... it should create the card". Picking eight
#: fields out of a payment and a few notes doesn't need the writer's model.
ORDER_MODEL = "claude-haiku-5-5"

#: Seconds to wait before asking again when Claude was busy or unreachable.
ORDER_READ_RETRY_WAITS = (5, 20)


def claude_busy(exc) -> bool:
    """Whether a failed call to Claude is worth making again: overloaded, rate
    limited, a server error, a timeout or the connection dropping - not a
    request it refused."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status in (408, 409, 429) or status >= 500
    name = type(exc).__name__
    return any(word in name for word in ("Connection", "Timeout", "Overloaded", "InternalServer", "RateLimit"))

#: A new agent's card description, field by field - "Do this template".
#: An aged leads card's description - "Aged Leads - Name of Agent" and
#: Franklin's fields for it.
AGED_FIELDS = (
    ("name", "Name"),
    ("email", "Email"),
    ("phone", "Phone Number"),
    ("package", "Lead Type"),
    ("leads", "Number of Leads Requested"),
    ("states", "States"),
    ("agency", "Agency"),
    ("notes", "Additional Notes"),
)

ORDER_FIELDS = (
    ("name", "Name"),
    ("email", "Email"),
    ("phone", "Phone Number"),
    ("states", "States"),
    ("package", "Lead Type/Package Selected"),
    ("leads", "Number of Leads Requested"),
    ("launch", "Launch Date"),
    # "i want ryte to add them on the aged leads and fresh cards" - from the
    # client's New Lead Request in #lead-lab when the team doesn't say.
    ("agency", "Agency"),
    ("notes", "Additional Notes"),
)


#: What a field says when it says nothing - "Number of Leads Requested:
#: <UNKNOWN>" is blank.
_SAYS_NOTHING = {"unknown", "<unknown>", "n/a", "na", "none", "not provided", "not specified", "not given",
                 "not stated", "not mentioned", "-", "--", "?", "null"}

#: Packages bought as ad spend - "Phx Standard 350" - with no lead count and
#: no cap: "it doesnt have nymber of leads because its ads spend".
_AD_SPEND = re.compile(r"\b(?:phx|phnx|phoenix)\b|\bad\s*spend\b", re.IGNORECASE)
NO_CAP = "No cap (ad spend)"

#: The agencies that buy ad spend, as the card names them - "Ascend now is
#: lifoff but yes its ascend".
AD_AGENCIES = (("PHNX", re.compile(r"\b(?:phx|phnx|phoenix)\b", re.IGNORECASE)),
               ("Ascend", re.compile(r"\b(?:ascend|lift\s*off)\b", re.IGNORECASE)),
               ("Spark", re.compile(r"\bspark\b", re.IGNORECASE)))
#: A week's ad spend from this much up is Plus; under it, Standard.
AD_PLUS_FROM = 1000


def ad_spend_package(source: str, amount: str = "") -> str:
    """"PHNX Standard", "Ascend Plus", "Spark Standard" - the agency the order
    names and the tier its weekly ad spend puts it in ("less than 1000 usd
    per week ... standard ... 1000usd and above, it will be plus"). "" when
    the order names no agency: Matthew Odierno's card said Phoenix when the
    order said Liftoff Ascend."""
    said = str(source or "")
    agency = next((name for name, pattern in AD_AGENCIES if pattern.search(said)), "")
    if not agency:
        return ""
    spend = re.search(r"\$\s*([\d,]+(?:\.\d+)?)\s*(?:ad\s*spend|/\s*w(?:ee)?k|per\s+week|a\s+week|weekly)", said,
                      re.IGNORECASE) or re.search(r"\b(?:ad\s*spend|phx|phnx|phoenix|ascend|spark)\D{0,20}\$\s*([\d,]+)", said,
                                                  re.IGNORECASE)
    try:
        weekly = float((spend.group(1) if spend else amount or "").replace(",", ""))
    except ValueError:
        weekly = None
    if weekly is None:
        return f"{agency} (ad spend)"
    return f"{agency} {'Plus' if weekly >= AD_PLUS_FROM else 'Standard'}"


#: A line that is one of the card's own fields - "Email: ...", "States Leads
#: In: ...", "Number of Requested Leads: 50" - or the invoice's product blurb.
_FIELD_LINE = re.compile(
    r"^\s*(?:payra invoice note:\s*)?(?:name|e-?mail|phone(?:\s*number)?|lead\s*type(?:\s*/\s*package(?:\s*selected)?)?|"
    r"package(?:\s*selected)?|states?(?:\s*leads?\s*in)?|number\s*of\s*(?:requested\s*)?leads(?:\s*requested)?|"
    r"leads\s*requested|launch\s*date|agency|lines|description)\s*:", re.IGNORECASE)


def _beyond_fields(note: str, fields: dict) -> str:
    """A note as it was written - "can you add the additional notes as it is?
    unless its image, if its text as it is". The team's own notes whole.
    The invoice's less its form lines ("Email: ...", "States Leads In: ...")
    that the card has above - Jad Nour Samman's Additional Notes were his
    invoice twice over, every field again; the rest of it word for word."""
    if not re.match(r"\s*payra invoice note:", str(note or ""), re.IGNORECASE):
        # A form pasted into the thread is the card twice over too - its
        # "Name: ..." lines go, and every other line stays as written.
        return "\n".join(line for line in str(note or "").splitlines() if not _FIELD_LINE.match(line)).strip()
    kept = []
    for line in str(note or "").splitlines():
        text = re.sub(r"^\s*payra invoice note:\s*", "", line, flags=re.IGNORECASE)
        if _FIELD_LINE.match(text):
            continue
        kept.append(text)
    said = "\n".join(kept).strip()
    # Said where it's from: Bryson Butt's card read "Launch Monday" over the
    # team's "live thursday october 22" - two launch dates, one of them what
    # he typed on the invoice before the team spoke to him.
    if said and re.match(r"\s*payra invoice note:", str(note or ""), re.IGNORECASE):
        said = f"From the Payra invoice: {said}"
    return said


_A_NAME = re.compile(r"^\s*([A-Z][a-z'’\-]+(?:\s+[A-Z][a-z'’\-]+){1,2})\s*[.,]?\s*$")
_NOT_NAME_WORDS = re.compile(
    r"\b(?:same|states?|live|today|tomorrow|leads?|vets?|veterans?|iul|fex|otp|aged|fresh|text|verified|mortgage|"
    r"protection|trucker|blue|collar|spanish|setup|set|agency|thank|thanks|please|monday|tuesday|wednesday|"
    r"thursday|friday|saturday|sunday|january|february|march|april|may|june|july|august|september|october|"
    r"november|december|paid|zelle|card|order|new|agent|done|signed|nationwide|all|except|only|motion|"
    r"notes?|name|email|phone|number|ringy|integration|discount|code|invoice)\b", re.IGNORECASE)


#: An order line with who it's for after a dash - "50 Aged Vets - Santiago
#: Basualdo", which Garret Sekelsky paid for and went out as his.
_DASH_NAME = re.compile(r"^\s*\d[\d,]*\s+[^\n\-–—]{2,40}?\s+[-–—]\s*"
                        r"([A-Z][a-z'’\-]+(?:\s+[A-Z][a-z'’\-]+){1,2})\s*[.,]?\s*$")


def name_in_notes(notes) -> str:
    """The one person the team's notes name on a line of its own - "Camden
    Misquez" under "7 OTP VETS" - or after an order line's dash - "50 Aged
    Vets - Santiago Basualdo" - or "" for none or more than one."""
    found = []
    for note in notes or ():
        for line in str(note or "").splitlines():
            match = _A_NAME.match(line) or _DASH_NAME.match(line)
            if not match or _NOT_NAME_WORDS.search(match.group(1)):
                continue
            name = " ".join(match.group(1).split())
            if name.casefold() not in {one.casefold() for one in found}:
                found.append(name)
    return found[0] if len(found) == 1 else ""


#: The invoice form's labels, and the card field each one is.
_FORM_LABELS = (
    ("states", r"states?(?:\s*leads?\s*in)?|states?\s*(?:wanted|requested)"),
    ("package", r"lead\s*type(?:\s*/\s*package(?:\s*selected)?)?|package(?:\s*selected)?"),
    ("leads", r"number\s*of\s*(?:requested\s*)?leads(?:\s*requested)?|leads\s*requested|how\s*many\s*leads"),
    ("email", r"e-?mail"),
    ("phone", r"phone(?:\s*number)?"),
)
#: An age bucket in a lead type - "180+ days", "30-90", "90 - 180".
_AGE_BUCKET = re.compile(r"\b\d{2,3}\s*(?:\+|[-–]\s*\d{2,3})")


_LEAD_WORD = re.compile(r"\b(?:aged|vets?|veterans?|fex|final\s+expense|iul|mtg|mortgage|widows?|truckers?|"
                        r"blue\s+collar|spanish|leads?)\b", re.IGNORECASE)


def count_beside_lead_type(text: str) -> int | None:
    """How many leads, off a line that says so beside the lead type - Jake
    Scott's "90, 30-90 aged vets" was held for "how many leads". The age
    range comes off first so its numbers aren't the count; a line with a
    phone, or more than one number left, says nothing. The first such line."""
    for line in str(text or "").splitlines():
        if not _LEAD_WORD.search(line) or re.search(r"\d{3}[-.\s]\d{3}[-.\s]\d{4}|\$", line):
            continue
        rest = _AGE_BUCKET.sub(" ", line)
        numbers = re.findall(r"(?<![\d/:.])(\d{1,4})(?![\d/:.%])", rest)
        if len(numbers) == 1 and int(numbers[0]) > 0:
            return int(numbers[0])
    return None


def invoice_form(text: str) -> dict:
    """The invoice's form lines - "States Leads In: all but new york", "Lead
    Type: Aged Veteran - 180+ days [AGED - $1/Lead]" - as {field: value}, the
    first of each. Values as written, less the price tag in brackets."""
    found = {}
    for line in str(text or "").splitlines():
        bare = re.sub(r"^\s*>+\s?", "", line).strip(" *•\t")
        for key, label in _FORM_LABELS:
            match = re.match(rf"(?:{label})\s*:\s*(.+)$", bare, re.IGNORECASE)
            if match and key not in found:
                value = re.sub(r"\s*\[[^\]]*\$[^\]]*\]\s*", " ", match.group(1))
                value = " ".join(value.split()).strip(" .")
                if value and value.casefold() not in _SAYS_NOTHING:
                    found[key] = value
                break
    return found


_MONTH_NUMBER = {name: at for at, name in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"), start=1)}


def _starts_on(text: str):
    """The day a post's own "Starts" line gives - Spark's "Starts October 25,
    2026" - or None."""
    found = re.search(r"^\W*starts?\b\W*(?:on\W*)?([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b",
                      str(text or ""), re.IGNORECASE | re.MULTILINE)
    if not found:
        return None
    month = next((at for name, at in _MONTH_NUMBER.items() if name.startswith(found.group(1).casefold()[:3])), None)
    try:
        return date(int(found.group(3)), month, int(found.group(2))) if month else None
    except ValueError:
        return None


_DISCORD_TIME = re.compile(r"<t:(-?\d{1,12})(?::([tTdDfFR]))?>")


def discord_times(text: str, timezone_name: str = "America/New_York") -> str:
    """Discord's own dates - "<t:1792900800:D>", which the app shows as
    October 25, 2026 - written out. Capture Info's Spark posts came with
    them, and FND Life's "Starts" wasn't read as a date at all."""
    from zoneinfo import ZoneInfo

    try:
        zone = ZoneInfo(timezone_name or "America/New_York")
    except Exception:
        zone = ZoneInfo("America/New_York")

    def written(found):
        try:
            when = datetime.fromtimestamp(int(found.group(1)), tz=timezone.utc).astimezone(zone)
        except (OverflowError, OSError, ValueError):
            return found.group(0)
        day = f"{when:%B} {when.day}, {when.year}"
        if found.group(2) in ("t", "T"):
            return f"{when:%I:%M %p}".lstrip("0")
        if found.group(2) in ("f", "F"):
            return f"{day} {when:%I:%M %p}".replace(" 0", " ")
        return day

    return _DISCORD_TIME.sub(written, str(text or ""))


#: Capture Info's one-time order on Spark.
SPARK_ORDER = re.compile(r"\blead\s+order\s+on\s+spark\b|\bone-?time\s+pack\b", re.IGNORECASE)


def spark_post(text: str) -> bool:
    """Capture Info's post of something bought on Spark - a pool membership
    or a one-time order."""
    said = str(text or "")
    return bool(re.search(r"\bspark\b", said, re.IGNORECASE)) and bool(
        re.search(r"pool\s+member", said, re.IGNORECASE) or SPARK_ORDER.search(said))


def spark_pool(text: str) -> dict:
    """A Spark pool membership's own terms: {"pool": "IUL - Volume", "leads":
    "38-50"} - the pool bought and the leads a week it expects. {} when it
    isn't a Spark pool member post. FND Life's $700/wk went out as "Spark
    Standard", "No cap (ad spend)" - read as Phoenix-style ad spend."""
    said = str(text or "")
    if not spark_post(said):
        return {}
    found = {}
    if SPARK_ORDER.search(said):
        # "New lead order on Spark" - a one-time pack: "veteran-widows — 20
        # leads · one-time pack" for Soar Life Agency got no card.
        found["one_time"] = True
        kind = re.search(r"^\W*lead\s+type\W*([a-z]+(?:-[a-z]+)*)\s*$", said, re.IGNORECASE | re.MULTILINE)
        if kind:
            found["pool"] = " ".join(one.capitalize() for one in kind.group(1).split("-")) + " - one-time pack"
        count = re.search(r"^\W*order\W*(\d[\d,]*)\s*leads?\b", said, re.IGNORECASE | re.MULTILINE)
        if count:
            found["leads"] = count.group(1).replace(",", "")
        states = re.search(r"^\W*(?i:states?)\W*([A-Z]{2}(?:\s*,\s*[A-Z]{2})*)\s*$", said, re.MULTILINE)
        if states:
            found["states"] = ", ".join(re.findall(r"[A-Z]{2}", states.group(1)))
    pool = re.search(r"^\W*pool\b(?!\s*member)\W*(.+)$", said, re.IGNORECASE | re.MULTILINE)
    if pool and pool.group(1).strip():
        found["pool"] = " ".join(pool.group(1).split())
    # "Buyer Everlife" is the field's label and its value - the card went out
    # as "New Agent - Buyer Everlife". The buyer is Everlife.
    buyer = re.search(r"^\W*buyer\b(?!\s*e-?mail)[\s:*-]*(.+)$", said, re.IGNORECASE | re.MULTILINE)
    if buyer and buyer.group(1).strip():
        found["name"] = " ".join(buyer.group(1).split())
    email = re.search(r"^\W*buyer\s*e-?mail\W*([^\s@]+@[^\s]+)", said, re.IGNORECASE | re.MULTILINE)
    if email:
        found["email"] = email.group(1).strip(" *")
    starts = _starts_on(said)
    if starts is not None:
        found["starts"] = starts
    weekly = re.search(r"expected\s+leads\s*/\s*w(?:ee)?k\W*(\d+)(?:\s*(?:[-–—]|to)\s*(\d+))?", said, re.IGNORECASE)
    if weekly:
        found["leads"] = "-".join(one for one in weekly.groups() if one)
    return found


def post_form(text: str) -> dict:
    """A payment post's own lines - "EMAIL: ...", "PHONE: ...", "STATES:
    ...", "LEADS: ..." - as {field: value}, the first post in the text only
    (the one gavelled), never the team's notes under it."""
    post = []
    for line in str(text or "").splitlines():
        if post and re.match(r"\s*\[\d\d/\d\d \d\d:\d\d[AP]M\]", line):
            break  # the next message
        post.append(line)
    said = "\n".join(post)
    if not re.search(r"new\s+payment", said, re.IGNORECASE):
        return {}
    found = {}
    for key, label in (("email", r"e-?mail"), ("phone", r"phone"), ("states", r"states?"), ("leads", r"leads")):
        match = re.search(rf"^\W*(?:{label})\W*:\**\s*(.+)$", said, re.IGNORECASE | re.MULTILINE)
        if match:
            value = " ".join(match.group(1).split()).strip(" *")
            if value and value.casefold() not in _SAYS_NOTHING:
                found[key] = value
    return found


def _states_split(said: str, leads: str = "") -> str:
    """A split written next to the states, kept: Torian Lollis's "80
    Louisiana, 20 Texas" went on his card as "LA, TX" and lost it. Counts
    that add up to 100 and aren't the order's count are shares: "LA 80%, TX
    20%". "" when the states aren't each given a number."""
    pairs = []
    for bit in re.split(r"\s*(?:,|;|/|\band\b|&|\n)\s*", str(said or ""), flags=re.IGNORECASE):
        if not bit.strip():
            continue
        codes = _state_codes_named(bit)
        numbers = re.findall(r"(?<![\d.])(\d{1,4})(?:\s*%)?(?![\d.])", bit)
        if len(codes) != 1 or len(numbers) != 1:
            return ""
        pairs.append((codes[0], int(numbers[0])))
    if len(pairs) < 2:
        return ""
    total = sum(count for _code, count in pairs)
    ordered = re.search(r"\d+", str(leads or ""))
    shares = "%" in str(said) or (total == 100 and not (ordered and int(ordered.group(0)) == 100))
    return ", ".join(f"{code} {count}{'%' if shares else ''}" for code, count in pairs)


def _form_states(said: str) -> str:
    """The form's states the way the card writes them: "all but new york" ->
    "All states except NY", "texas, florida" -> "TX, FL"; anything else as
    written."""
    from .. import ordercards

    if re.search(r"\b(?:all|every|nation\s*-?\s*wide)\b", said, re.IGNORECASE):
        if ordercards._ALL_BUT.search(said):
            left_out = _state_codes_named(ordercards._ALL_BUT.split(said, maxsplit=1)[-1])
            if left_out:
                return "All states except " + ", ".join(left_out)
        return said
    codes = _state_codes_named(said)
    return ", ".join(codes) if codes else said


def with_invoice_form(fields: dict, form: dict) -> dict:
    """The card's fields with what the invoice's form says where the reading
    left it out or lost part of it. The form is what the client filled in."""
    fields = dict(fields)
    if not form:
        return fields
    states = form.get("states")
    if states:
        written = _form_states(states)
        have = str(fields.get("states") or "")
        from .. import ordercards

        # Empty, or the exclusions lost: "all but new york" read as "All".
        lost = ordercards._ALL_BUT.search(written) and not ordercards._ALL_BUT.search(have)
        split = _states_split(states, str(form.get("leads") or fields.get("leads") or ""))
        if split and not re.search(r"\d", have):
            # The numbers beside the states lost: "80 Louisiana, 20 Texas".
            fields["states"] = split
        elif not have.strip() or lost:
            fields["states"] = written
    package = form.get("package")
    if package:
        have = str(fields.get("package") or "")
        bucket = _AGE_BUCKET.search(package)
        if not have.strip() or (bucket and not _AGE_BUCKET.search(have)):
            fields["package"] = package
    leads = form.get("leads")
    if leads and not re.search(r"\d", str(fields.get("leads") or "")) and re.search(r"\d", leads):
        fields["leads"] = re.search(r"\d[\d,]*", leads).group(0)
    for key in ("email", "phone"):
        if form.get(key) and not str(fields.get(key) or "").strip():
            fields[key] = form[key]
    return fields


_ANY_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_ANY_PHONE = re.compile(r"(?<![\w])(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}(?![\w])")


def with_unlabelled_contact(fields: dict, notes, text: str) -> dict:
    """An email and phone the notes give on lines of their own, with no
    "Email:" in front - Aiden Adair's invoice note had "+1 (801) 651-8641"
    and "Aiden.adair@tryeverlife.com" and his card went out with neither.
    Only when the card has none, only one that isn't the payer's, and only
    when the notes give exactly one."""
    from .. import ordercards as _cards

    payer = post_form(text)
    payer_email = str(payer.get("email") or "").casefold()
    payer_phone = re.sub(r"\D", "", str(payer.get("phone") or ""))[-10:]
    said = "\n".join(str(one) for one in notes or ())
    fields = dict(fields)
    # The payer's own, on a card for somebody else, is no better than none.
    payer_name = _cards.paid_by(text)
    # Not a business paying: Montaser Aidah's "Victory Freedom Llc" email is his.
    if payer_name and fields.get("name") and not _cards.same_person(payer_name, str(fields["name"])) \
            and not _cards._BUSINESS.search(payer_name):
        if payer_email and str(fields.get("email") or "").casefold() == payer_email:
            fields["email"] = ""
        if payer_phone and re.sub(r"\D", "", str(fields.get("phone") or ""))[-10:] == payer_phone:
            fields["phone"] = ""
    if not str(fields.get("email") or "").strip():
        emails = list(dict.fromkeys(one.strip(".") for one in _ANY_EMAIL.findall(said)
                                    if one.strip(".").casefold() != payer_email))
        if len({one.casefold() for one in emails}) == 1:
            fields["email"] = emails[0]
    if not str(fields.get("phone") or "").strip():
        phones = {}
        for one in _ANY_PHONE.findall(said):
            digits = re.sub(r"\D", "", one)[-10:]
            if len(digits) == 10 and digits != payer_phone:
                phones.setdefault(digits, " ".join(one.split()))
        if len(phones) == 1:
            fields["phone"] = next(iter(phones.values()))
    return fields


def order_fields(got: dict, notes: list[str] = ()) -> dict:
    """Claude's reading of an order, tidied: nothing that only says it doesn't
    know, an ad-spend package's lead count as no cap, and Additional Notes as
    the team wrote them in the thread - "the thread message replied from the
    team should be added on the additional notes"."""
    fields = {}
    for key in dict.fromkeys(key for key, _label in ORDER_FIELDS + AGED_FIELDS):
        value = " ".join(str(got.get(key) or "").split())
        fields[key] = "" if value.casefold().strip(" .") in _SAYS_NOTHING else value
    if _AD_SPEND.search(fields["package"]) and not re.search(r"\d", fields["leads"]):
        fields["leads"] = NO_CAP
    given = bool(notes)
    notes = [kept for kept in (_beyond_fields(one, fields) for one in notes or ()) if kept]
    if given and not notes:
        # Every note only said what the fields do - nothing to add.
        fields["notes"] = ""
    if notes:
        # Once each: the same note in the thread and on the message it
        # hangs off read twice on Bailey Lawrence's card.
        notes = list(dict.fromkeys(str(one).strip() for one in notes if str(one).strip()))
        said = "\n".join(notes)
        # "they sometimes add screenshot as additional notes" - each one as
        # what it says, where it was posted.
        shots = [" ".join(str(one or "").split()) for one in got.get("screenshots") or []]

        def written(found):
            at = int(found.group(1)) - 1
            return f"📷 {shots[at]}" if at < len(shots) and shots[at] else "📷 (screenshot)"

        fields["notes"] = re.sub(r"\[screenshot (\d+)\]", written, said)
    return fields


def order_desc(fields: dict, kind: str = "fresh") -> str:
    """The description in Franklin's layout - every label there, blank after
    it when the order doesn't say. Additional Notes last, line by line as the
    team wrote them."""
    lines = []
    for key, label in (AGED_FIELDS if kind == "aged" else ORDER_FIELDS):
        value = str(fields.get(key) or "").strip()
        if key == "notes" and value:
            # "add space from additional notes to the pasted notes" - Trello
            # folds blank lines into one, so the gap is lines holding only a
            # no-break space.
            # One spacer: Trello puts paragraph space either side of it, so it
            # reads as the two lines asked for - two spacers read as four.
            lines.append(f"{label}:\n\n\u00a0\n\n{value}")
        else:
            lines.append(f"{label}: {' '.join(value.split())}".rstrip())
    return "\n".join(lines)


_ZELLE_POST = re.compile(r"new\s+payment\W{0,6}\|\W{0,6}zelle\b|\bmethod\W{0,6}:\W{0,6}zelle\b", re.IGNORECASE)
_ZELLE_BANK = re.compile(r"\bmethod\W{0,6}:\W{0,6}zelle\s*\(([^)\n]{1,30})\)", re.IGNORECASE)


def zelle_note(text: str) -> str:
    """"Paid via Zelle (Chase)" when the payment post is a Zelle one - Nova's
    "New Payment | Zelle ... METHOD: Zelle (Chase)" - else ""."""
    if not _ZELLE_POST.search(str(text or "")):
        return ""
    bank = _ZELLE_BANK.search(str(text or ""))
    return f"Paid via Zelle ({' '.join(bank.group(1).split())})" if bank else "Paid via Zelle"


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"\+?\d[\d\s().-]{8,16}\d")
_NAME_LINE = re.compile(r"^\W*name\W*:\**\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_AGENCY_LINE = re.compile(r"^\W*agency\W*:\**[ \t]*(.*?)[ \t]*$", re.IGNORECASE | re.MULTILINE)


def _who_in(text: str) -> tuple[set, set, set]:
    """(emails, phones as their last ten digits, full names) a post gives."""
    said = str(text or "")
    emails = {one.casefold() for one in _EMAIL.findall(said)}
    phones = {digits[-10:] for digits in (re.sub(r"\D", "", one) for one in _PHONE.findall(said)) if len(digits) >= 10}
    names = {" ".join(one.replace("*", "").split()).casefold() for one in _NAME_LINE.findall(said)}
    return emails, phones, {one for one in names if len(one.split()) >= 2}


def lead_request_for(posts, text: str) -> str:
    """The client's New Lead Request in #lead-lab - the first of `posts`
    (newest first) with their email, phone or full name and an agency
    filled in - or ""."""
    emails, phones, names = _who_in(text)
    for post in posts or ():
        if not request_agency(post):
            continue
        their_emails, their_phones, their_names = _who_in(post)
        if emails & their_emails or phones & their_phones or names & their_names:
            return str(post)
    return ""


def request_agency(post: str) -> str:
    """"Agency: FFL" off a New Lead Request, or "" when it's blank."""
    found = _AGENCY_LINE.search(str(post or ""))
    said = " ".join((found.group(1) if found else "").replace("*", "").split())
    return "" if said.casefold().strip(" .") in _SAYS_NOTHING else said


#: Agencies the team names - and every agency a New Lead Request in
#: #lead-lab has given, added as they're read.
KNOWN_AGENCIES = ("AEP", "FFL", "Wolfpack", "Uprise", "Fearless", "PHNX", "Phoenix", "Ascend", "Liftoff", "Spark",
                  "Roadmap")

#: "ROADMAP AGENCY  LIVE OCT 19", "Roadmap agency", "Agency: Roadmap" - the
#: agency named the way the team writes it, known or not. Benny Beniamin's
#: card went out blank with "ROADMAP AGENCY" in his notes.
_NAMED_AGENCY = (
    re.compile(r"^\s*(?:agency|imo)\s*[:\-–—]\s*(?P<name>[A-Za-z][\w&.' ]{1,40}?)\s*[.!]*\s*$", re.IGNORECASE),
    re.compile(r"^\s*(?P<name>[A-Za-z][\w&.']*(?:\s+[A-Za-z][\w&.']*){0,3}?)\s+(?:agency|imo)\b\s*[-–—,:]?\s*"
               r"(?:(?:is\s+)?(?:going\s+)?(?:live|launch\w*|start\w*)\b.*)?$", re.IGNORECASE),
)
#: Words a "<name> agency" can't start with: "my current agency", "the agency".
_NOT_A_NAME = re.compile(r"^(?:the|my|his|her|their|our|your|a|an|current|new|same|no|any|which|what|this|that|"
                         r"other|old|previous|i|he|she|they|leaving|left|from|with|for|of|at|in|to|via|by)\b",
                         re.IGNORECASE)


#: The payment post's own lines - who paid, how much, the invoice's number
#: and date - which say nothing about when anybody goes live.
_POST_LINE = re.compile(
    r"^\W*(?:name|email|phone|amount|product|invoice|subtotal|total|automatic dual pricing|"
    r"discount[^:]*|\$\d+/lead off|paid on|date|due)\W*:", re.IGNORECASE)


_STAMPED = re.compile(r"^\[(\d\d)/(\d\d) \d\d:\d\d[AP]M\]", re.MULTILINE)
_TODAY_WORD = re.compile(r"\b(?:today|tonight|tdy)\b", re.IGNORECASE)
_TOMORROW_WORD = re.compile(r"\b(?:tomorrow|tmrw?|tmw|tomorow|tommorow)\b", re.IGNORECASE)


def _days_said(text: str, today) -> list[tuple[int, object]]:
    """(where, the day) for every "[10/18 09:40PM]" stamp in the order's text,
    in the year that puts it on or before today."""
    found = []
    for stamp in _STAMPED.finditer(str(text or "")):
        try:
            day = date(today.year, int(stamp.group(1)), int(stamp.group(2)))
        except ValueError:
            continue
        if day > today + timedelta(days=1):
            day = day.replace(year=today.year - 1)
        found.append((stamp.start(), day))
    return found


def _written_on(text: str, note: str, today):
    """The day a note was written: the stamp before where it is in the text."""
    first = next((line.strip() for line in str(note or "").splitlines() if line.strip()), "")
    at = str(text or "").find(first) if first else -1
    day = today
    for where, said in _days_said(text, today):
        if at < 0 or where > at:
            break
        day = said
    return day


def _named_days(said: str, written) -> str:
    """"today" and "tomorrow" as the dates they were, from the day written."""
    from .. import ordercards

    said = ordercards.INVOICE_BOILERPLATE.sub(" ", str(said or ""))
    if written is None:
        return said
    tomorrow = written + timedelta(days=1)
    said = _TOMORROW_WORD.sub(f"{tomorrow:%B} {tomorrow.day}", said)
    return _TODAY_WORD.sub(f"{written:%B} {written.day}", said)


def _named_days_by_stamp(text: str, today) -> str:
    """The order's text with each note's today / tomorrow as dates, by its
    own stamp."""
    text = str(text or "")
    stamps = _days_said(text, today)
    if not stamps:
        return text
    out, last = [text[:stamps[0][0]]], len(text)
    for at, (where, day) in enumerate(stamps):
        end = stamps[at + 1][0] if at + 1 < len(stamps) else last
        out.append(_named_days(text[where:end], day))
    return "".join(out)


def launch_written(when, text: str, *, today) -> bool:
    """Whether the order names this launch day: its weekday, its date, or
    today / tomorrow when that's what it is - outside the payment post's own
    lines and the times RYTE stamps on notes."""
    from .. import ordercards

    # Each note's today / tomorrow as the date it meant, then without stamps.
    said = _named_days_by_stamp(ordercards.INVOICE_BOILERPLATE.sub(" ", str(text or "")), today)
    said = re.sub(r"^\[\d\d/\d\d \d\d:\d\d[AP]M\][^:]*:\s*", "", said, flags=re.MULTILINE)
    said = "\n".join(line for line in said.splitlines() if not _POST_LINE.match(line)).casefold()
    names = [f"{when:%A}".casefold(), f"{when:%a}".casefold()]
    if when.weekday() == 1:
        names.append("tues")
    if when.weekday() == 3:
        names += ["thur", "thurs"]
    if any(re.search(rf"\b{name}\b", said) for name in names):
        return True
    month, day = f"{when:%B}".casefold(), when.day
    if re.search(rf"\b(?:{month}|{month[:3]})\.?\s+{day}(?:st|nd|rd|th)?\b", said) \
            or re.search(rf"(?<![\d/]){when.month}/0?{day}(?![\d])|(?<![\d/])0{when.month}/0?{day}(?![\d])", said):
        return True
    if when == today and re.search(r"\b(?:today|tonight|asap|same\s*day|right\s+away|now)\b", said):
        return True
    if when == today + timedelta(days=1) and re.search(r"\b(?:tomorrow|tmrw?|tmw)\b", said):
        return True
    return False


#: How somebody paid, not who they're with: Bailey Lawrence's "BANK - live
#: Monday October 26" went on his card as Agency: BANK.
_PAID_HOW = {"bank", "banktransfer", "ach", "wire", "zelle", "cash", "cashapp", "venmo", "paypal", "stripe", "payra",
             "card", "creditcard", "debitcard", "check", "cheque", "invoice", "paid", "unpaid", "pending"}


def agency_usable(name: str) -> bool:
    """Whether what a request put as the agency names one: not a letter -
    Bram Gutierrez's "F" - unless it's one the team knows; never how they
    paid."""
    said = re.sub(r"[^A-Za-z0-9]", "", str(name or ""))
    if said.casefold() in _PAID_HOW:
        return False
    if said.casefold() in {re.sub(r"[^A-Za-z0-9]", "", one).casefold() for one in KNOWN_AGENCIES}:
        return True
    return len(said) >= 2


def agency_said(text: str, agencies=()) -> str:
    """The agency when the team writes it on a line of its own - Kharyl's
    "aep" at the foot of Gavin Mathieu's notes, "AEP @Nicole" - as the
    agency is spelled, or ""."""
    # Names only - not "F", not "i am leaving my current agency at this
    # time" - or any line saying just that would be read as the agency.
    known = {" ".join(str(one).split()).casefold(): " ".join(str(one).split())
             for one in (*agencies, *KNOWN_AGENCIES)
             if len(re.sub(r"[^A-Za-z0-9]", "", str(one or ""))) >= 3 and len(str(one).split()) <= 4}
    for line in reversed(str(text or "").splitlines()):
        said = re.sub(r"^\[\d\d/\d\d \d\d:\d\d[AP]M\][^:]*:\s*", "", line)
        said = re.sub(r"@\S+", "", said)
        said = re.sub(r"(?i)\b(?:agency|imo)\b\s*[:-]?", "", said)
        said = " ".join(said.strip(" *_-–—:.,!").split()).casefold()
        if said in known:
            return known[said]
        # "AEP LIVE  MON, OCT 19" - the agency, then when they go live.
        for name, spelled in known.items():
            if re.match(rf"{re.escape(name)}\s*[-–—:,]?\s+(?:is\s+)?(?:going\s+)?(?:live|launch\w*|start\w*)\b", said):
                return spelled
    for line in reversed(str(text or "").splitlines()):
        bare = re.sub(r"^\[\d\d/\d\d \d\d:\d\d[AP]M\][^:]*:\s*", "", line)
        bare = " ".join(re.sub(r"@\S+", "", bare).strip(" *_").split())
        for pattern in _NAMED_AGENCY:
            found = pattern.match(bare)
            name = " ".join(found.group("name").split()).strip(" .") if found else ""
            if name and not _NOT_A_NAME.match(name) and agency_usable(name) \
                    and name.casefold().strip(" .") not in _SAYS_NOTHING:
                return known.get(name.casefold(), name)
    return ""


def order_card_fill_agency(config: Config, card_id: str, agency: str) -> str:
    """"Agency:" left blank on a card, filled in. Only a blank one: an agency
    somebody has written in is theirs. The new description, or "" when
    there was no blank to fill."""
    client = open_trello(config)
    try:
        desc = str(client.card_detail(card_id).get("desc") or "")
        blank = re.search(r"^([ \t]*Agency[ \t]*:)[ \t]*$", desc, re.MULTILINE | re.IGNORECASE)
        if not blank:
            return ""
        desc = desc[:blank.start()] + f"{blank.group(1)} {agency}" + desc[blank.end():]
        client.set_description(card_id, desc)
        return desc
    finally:
        client.close()


def _same_contact_as_payer(text: str, notes, name: str) -> bool:
    """Whether the note naming `name` gives the payment's own email or phone
    - the payer under a nickname, not somebody else."""
    paid_email = re.search(r"^\W*EMAIL:\**\s*([\w.+-]+@[\w-]+(?:\.[\w-]+)+)", text or "", re.MULTILINE | re.IGNORECASE)
    paid_phone = re.search(r"^\W*PHONE:\**\s*([+\d()][\d()\s.\-+]{6,})", text or "", re.MULTILINE | re.IGNORECASE)
    paid_digits = re.sub(r"\D", "", paid_phone.group(1))[-10:] if paid_phone else ""
    for said in notes or ():
        if not re.search(rf"name\s*:\s*{re.escape(name)}\b", said, re.IGNORECASE):
            continue
        email = re.search(r"e-?mail\W*:?\s*([\w.+-]+@[\w-]+(?:\.[\w-]+)+)", said, re.IGNORECASE)
        if email and paid_email and email.group(1).casefold() == paid_email.group(1).casefold():
            return True
        for phone in re.findall(r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}", said):
            if paid_digits and len(paid_digits) == 10 and re.sub(r"\D", "", phone)[-10:] == paid_digits:
                return True
    return False


#: An order that starts as soon as it can - so a day that's passed is today.
_ASAP = re.compile(r"\basap\b|\bas\s+soon\s+as\b|\bright\s+away\b|\bonce\s+(?:he|she|they|it)(?:'s|\s+is)?\s+"
                   r"sign|\bwhen\s+(?:he|she|they)\s+signs?\b|\bonce\s+signed\b", re.IGNORECASE)


def read_order(config: Config, *, text: str, images: list[str], today, notes: list[str] = (),
               request: str = "", agencies=()) -> dict:
    """An order as posted in Discord - the payment and the team's notes -
    read by Claude into the card's fields. {"kind": "fresh"|"aged", "client",
    "fields", "name", "desc", "missing": [labels not given]}."""
    from anthropic import Anthropic

    # Nova's posts quote the invoice's notes: "> Same states" went on a card
    # as the States, quote mark and all.
    text = re.sub(r"^(\s*)>+[ \t]?", r"\1", str(text or ""), flags=re.MULTILINE)
    zone = getattr(getattr(config, "schedule", None), "timezone", "") or "America/New_York"
    text = discord_times(text, zone)
    notes = [discord_times(one, zone) for one in notes or ()]
    config.secrets.require("anthropic_api_key")
    client = Anthropic(api_key=config.secrets.anthropic_api_key)
    properties = {
        "kind": {"type": "string", "enum": ["fresh", "aged"], "description": (
            "'aged' for aged leads (the product says Aged Leads, or the notes say aged); 'fresh' for a new "
            "agent's leads - VET, OTP, FEX, MTG, IUL, Trucker and the rest.")},
        "name": {"type": "string", "description": (
            "The client's full name - who the leads are for. When the notes name a person on a line of their "
            "own ('7 OTP VETS / Bailey Lawrence / Same states'), that person is the client, even when somebody "
            "else paid.")},
        "email": {"type": "string", "description": "Their email."},
        "phone": {"type": "string", "description": "Their phone number, as written."},
        "states": {"type": "string", "description": (
            "The states, as two-letter codes separated by commas: 'TX, AL, CA'. When the order leaves states "
            "OUT - 'no ny ak no ha', 'all but NY', 'except TX' - write 'All states except NY, AK, HI': never list "
            "the left-out states as if they were the ones ordered. 'ha' is Hawaii (HI).")},
        "package": {"type": "string", "description": (
            "The lead type / package bought, e.g. 'Text-Verified Trucker IUL (OTP)'. The notes win over the "
            "payment's product line when they're more exact. An order split across lead types names each with "
            "its count: '50 Aged VET 180+ + 50 Aged Mortgage Protection 180+'. Ad spend is bought through an "
            "agency - PHNX (Phoenix), Ascend (also called Liftoff) or Spark: the agency the order names and "
            "the tier, 'Ascend Plus' at $1,000 a week or more, 'PHNX Standard' under it. Never name an agency "
            "the order doesn't.")},
        "leads": {"type": "string", "description": (
            "How many leads were BOUGHT, just the number: '20'. Never a daily cap - 'CAP 5', 'cap of 5 leads "
            "per day', '10/day' is how many a day, not how many in all: '24-trucker IULs ... cap of 5 leads "
            "per day' is 24. A split order: the total with the split, '100 (50 + 50)'. "
            "Ad-spend packages (Phoenix / 'Phx Standard 350' and the like) buy ad spend, not a lead count: put "
            "'No cap (ad spend)'. Empty if it isn't said.")},
        "launch": {"type": "string", "description": (
            "The launch / go-live date as a date: 'Tuesday, October 13'. Work out 'today' / 'tomorrow' from "
            "when the note was written. Only a date the order gives for going live / launching / starting "
            "leads. Empty when nothing says when they launch - never the day it was paid or posted, and never "
            "the day somebody is to reach out, call or follow up (\"reach out tomorrow\" is not a launch "
            "date). \"Setup for Monday\" / \"set up on Monday\" IS the launch day - the day the agent is set up "
            "for.")},
        "screenshots": {"type": "array", "items": {"type": "string"}, "description": (
            "For each screenshot the team posted in the notes - '[screenshot 1]' is the first image shown, "
            "'[screenshot 2]' the second - what it means for the setup, written as an instruction to the "
            "team, not copied out word for word: a client's text 'can you make sure I don't get any leads "
            "after 9pm' is 'No leads after 9pm — the client asked (screenshot attached)'. Keep the details "
            "that matter: states, numbers, dates, hours, caps. One entry each, in order. When a screenshot "
            "asks for nothing, say in a few words what it shows. Empty when there are none.")},
        "launch_iso": {"type": "string", "description": "The launch date as YYYY-MM-DD, or empty."},
        "agency": {"type": "string", "description": (
            "The agency or IMO they're with, when the notes say - 'UPRISE', 'FEARLESS', 'AEP', 'FFL', "
            "'ROADMAP AGENCY'. A short agency name on a line of its own at the end of the notes ('aep', 'AEP "
            "@Nicole') is the agency. Never a person's name: a first and last name on a line of its own "
            "('Bailey Lawrence') is who the leads are for - the client - not the agency. Empty if not said.")},
        "notes": {"type": "string", "description": (
            "Anything else the team said that matters for the setup - schedule, hours, a cap, same setup as "
            "before, who to tell. Short. Empty if nothing.")},
        "parts": {"type": "array", "description": (
            "When one order buys more than one line - aged leads and new-agent leads (fresh / OTP / "
            "text-verified, set up to go live) together, OR several aged lines (different ages or lead types: "
            "'Aged Vets 30-90', 'Aged Vets 90-180', 'Aged FEX 180+'): every line of the order, in the order "
            "written, each as its own part, with THAT line's own count and THAT line's own states - never the "
            "states of every line put together. Match each count to its line from the invoice's lines and "
            "notes. An aged order in packets or batches with different states ('1 packet: AR, CA ...', '2 "
            "packets of this: AR, GA ...', '2 other packets all states except ny ak ha') is a part per set of "
            "states, with its own count - 600 in 6 packets is 100 a packet. Empty when the order is one line."),
            "items": {"type": "object", "properties": {
                "kind": {"type": "string", "enum": ["fresh", "aged"]},
                "package": {"type": "string", "description": "That line's lead type, as written: "
                                                             "'Aged Vets 180+', '25 OTP IULs' without the count."},
                "leads": {"type": "string", "description": "That line's count, just the number: '5659'."},
                "states": {"type": "string", "description": (
                    "That line's states as two-letter codes, 'Nationwide' when it says so, or empty when the "
                    "states aren't given yet ('waiting for states'). When the line splits its count by state, "
                    "keep each state's count: '25 NC, 25 GA, 25 FL, 25 TX'.")},
                "launch_iso": {"type": "string", "description": (
                    "A fresh line's go-live date as YYYY-MM-DD when the order gives one for that line, else empty.")},
                "name": {"type": "string", "description": (
                    "Who THAT line is for, when the order sends its lines to different people: '43 OTP Vets to "
                    "Shahan Uddin ... 100 Aged Vets to Mohammad Kanan'. Empty when every line is for the client.")},
            }, "required": ["kind", "package", "leads", "states", "launch_iso"]}},
    }
    tool = {"name": "order", "description": "The order's details for its Trello card.",
            "input_schema": {"type": "object", "properties": properties, "required": list(properties)}}
    content = [{"type": "text", "text": (
        f"Today is {today:%A, %B %d, %Y}.\n\nThe order, as posted in Discord - the payment, then the team's "
        f"notes, oldest first:\n{text}")}]
    for url in images[:6]:
        content.append({"type": "image", "source": {"type": "url", "url": url}})
    system = ("You read Agent Lead Lab's orders, as posted in Discord, for the Trello card the team works from. "
              "Use only what the payment and the notes say, screenshots included; later notes override earlier "
              "ones. Leave a field empty rather than guess.")
    import logging
    import os

    def asking(model, said):
        return client.messages.create(model=model, max_tokens=2000, system=system, tools=[tool],
                                      tool_choice={"type": "tool", "name": "order"},
                                      messages=[{"role": "user", "content": said}])

    import time

    response, last = None, None
    for model in dict.fromkeys((os.getenv("ORDER_MODEL") or ORDER_MODEL, config.copy.model)):
        # Again after a wait when it was Claude busy or the line dropping -
        # "Claude didn't answer" on an order at 10:08 Oct 19, both models in
        # the same second, and the card left for somebody to gavel again.
        for wait in (0, *ORDER_READ_RETRY_WAITS):
            if wait:
                time.sleep(wait)
            try:
                response = asking(model, content)
                break
            except Exception as exc:
                last = exc
                logging.getLogger("wilbyte.bot").warning("Reading the order with %s failed", model, exc_info=True)
                if not claude_busy(exc):
                    break
        if response is not None:
            break
    if response is None:
        if not images:
            raise RuntimeError("Couldn't read the order - Claude didn't answer"
                               + (f" ({_short(last, 140)})" if last is not None else "") + ".")
        # A screenshot Claude couldn't fetch shouldn't cost the whole read.
        response = asking(config.copy.model, content[:1])
    got = _tool_input(response, "order")
    # The invoice's notes written into the payment post since Oct 19 - Mario
    # Gonzalez's own "Hey Faith this is Mario ..." - on the card as written,
    # like the invoice note looked up for an older post.
    from .. import ordercards as _posted

    _number, on_the_post = _posted.post_invoice(text)
    if on_the_post and not any(str(one).startswith("Payra invoice note:") and not same_charge_note(one)
                               for one in notes or ()):
        notes = [f"Payra invoice note: {on_the_post}", *(notes or ())]
    fields = order_fields(got, notes)
    spark = spark_pool(text)
    if spark:
        # A pool membership buys leads a week from a pool, not ad spend.
        fields["package"] = spark.get("pool") or fields["package"]
        for key in ("name", "email"):
            if spark.get(key):
                fields[key] = spark[key]
        if fields.get("leads") == NO_CAP or not re.search(r"\d", str(fields.get("leads") or "")) or (
                spark.get("one_time") and spark.get("leads")):
            fields["leads"] = spark.get("leads", "")
        if spark.get("states"):
            # As Spark wrote them: what the buyer bought.
            fields["states"] = spark["states"]
    elif fields.get("leads") == NO_CAP or _AD_SPEND.search(str(fields.get("package") or "")):
        # Ad spend: the agency the order names, and Standard or Plus.
        paid = re.search(r"AMOUNT:\**\s*\$?\s*([\d,]+(?:\.\d+)?)", text, re.IGNORECASE)
        named = ad_spend_package(text, paid.group(1) if paid else "")
        if named:
            fields["package"] = named
    kind = "aged" if str(got.get("kind") or "").casefold() == "aged" else "fresh"
    # "if it says overflow, take it to aged leads ... it'll be treated as new
    # agent but on aged leads" - a New Agent card, in Aged Leads Order Done.
    overflow = kind == "fresh" and bool(_posted.OVERFLOW.search(
        "\n".join([text, str(fields.get("package") or ""), *map(str, notes or ())])))
    # The invoice's own form, read as written - Claude's reading left
    # Mackenzie Ho's "States Leads In: all but new york" out and put the
    # product line's "Text-Verified Aged Veteran Leads" for her "Aged Veteran
    # - 180+ days".
    form = invoice_form("\n".join(str(one)[len("Payra invoice note:"):] for one in notes or ()
                                  if str(one).startswith("Payra invoice note:") and not same_charge_note(one)))
    fields = with_invoice_form(fields, form)
    # And the payment post's own labelled lines - Nova's Zelle post for Jordan
    # Holmes had EMAIL, PHONE and every state, and his card had none of them.
    fields = with_invoice_form(fields, post_form(text))
    client_name = fields["name"]
    # The team's notes naming somebody on a line of its own, who isn't who
    # paid: the card is theirs, and the payer's email and phone aren't.
    # Garret Sekelsky paid for "7 OTP VETS / Camden Misquez" and the card
    # went out as Garret's. It's held as somebody else paying all the same -
    # Tony Moderno's notes were about Preston Bridges by mistake.
    from .. import ordercards as _cards

    payer = _cards.paid_by(text)
    # The team's notes in the thread, and the ones written on the invoice -
    # in the payment post itself since Oct 19: Garret Sekelsky's second, "7
    # OTP VETS / Bailey Lawrence / Same states", went out as his card with
    # Bailey Lawrence as the Agency.
    _number, on_post = _cards.post_invoice(text)
    named = name_in_notes([one for one in notes or () if not str(one).startswith("Payra invoice note:")]
                          + ([on_post] if on_post else []))
    # "Name: Hunter Kahle" written in the notes is who it's for: a name
    # guessed off a line of its own never beats it, and a name read that
    # isn't one written is put right - his card went out as "Only Motion".
    written = []
    for said in [*(str(one) for one in notes or ()), on_post or ""]:
        for found in re.finditer(r"^\W*(?:client\s+|agent\s+|full\s+)?name\s*:\s*([A-Za-z][A-Za-z'’.\- ]{2,40}?)\s*$",
                                 said, re.MULTILINE | re.IGNORECASE):
            if found.group(1).strip() not in written:
                written.append(found.group(1).strip())
    if written:
        if named and not any(_cards.same_person(named, one) for one in written):
            named = ""
        # The payer under another name - Jacob Martin's notes said "Name:
        # jacoby" with his own email, and the card went out as jacoby with no
        # email or phone. Same email or phone as the payment: the payer.
        if len(written) == 1 and payer and not _cards.same_person(payer, written[0]) \
                and _same_contact_as_payer(text, [*(str(one) for one in notes or ()), on_post or ""], written[0]):
            written = [payer]
            fields["name"] = client_name = payer
            for key, label in (("email", "EMAIL"), ("phone", "PHONE")):
                found = re.search(rf"^\W*{label}:\**\s*(\S[^\n]*)", text, re.MULTILINE | re.IGNORECASE)
                if found and not str(fields.get(key) or "").strip():
                    fields[key] = found.group(1).strip(" *")
        # Not when the order says it's for somebody else: Hayden Keltner's
        # "Name: Hayden Keltner ... for his agent Matthew Stewart".
        if len(written) == 1 and client_name and not _cards.same_person(client_name, written[0]) \
                and not _cards.paid_for_by(text + "\n" + "\n".join(map(str, notes or ())), client_name):
            fields["name"] = client_name = written[0]
            # Their email and phone from the same note, not the payer's:
            # Shelby Guest's card had Tony Moderno's.
            theirs = next((said for said in [*(str(one) for one in notes or ()), on_post or ""]
                           if re.search(rf"name\s*:\s*{re.escape(written[0])}", said, re.IGNORECASE)), "")
            email = re.search(r"^\W*e-?mail\W*:?\s*([\w.+-]+@[\w-]+\.[\w.]+)", theirs, re.MULTILINE | re.IGNORECASE)
            phone = re.search(r"^\W*phone(?:\s*number)?\W*:?\s*([+\d()][\d()\s.\-+]{6,})$", theirs,
                              re.MULTILINE | re.IGNORECASE)
            paid = (_cards.paid_by(text), text.casefold(), re.sub(r"\D", "", text))
            somebody_else = bool(paid[0]) and not _cards.same_person(written[0], paid[0])
            for key, found in (("email", email), ("phone", phone)):
                value = str(fields.get(key) or "")
                digits = re.sub(r"\D", "", value)[-10:]
                payers = (key == "email" and bool(value) and value.casefold() in paid[1]) or (
                    key == "phone" and len(digits) == 10 and digits in paid[2])
                if found:
                    fields[key] = found.group(1).strip()
                elif somebody_else and payers:
                    fields[key] = ""
    if named and _cards.same_person(str(fields.get("agency") or ""), named) \
            and str(fields.get("agency") or "").casefold() == named.casefold():
        fields["agency"] = ""
    if named and payer and _cards.same_person(client_name, payer) and not _cards.same_person(named, payer):
        paid = text.casefold()
        fields["name"] = client_name = named
        for key in ("email", "phone"):
            value = str(fields.get(key) or "")
            digits = re.sub(r"\D", "", value)[-10:]
            from_post = (key == "email" and value and value.casefold() in paid) or (
                key == "phone" and len(digits) == 10 and digits in re.sub(r"\D", "", text))
            if from_post and not any(value.casefold() in str(one).casefold() or
                                     (digits and digits in re.sub(r"\D", "", str(one))) for one in notes or ()):
                fields[key] = ""
    fields = with_unlabelled_contact(fields, notes, text)
    launch_iso = str(got.get("launch_iso") or "")[:10]
    if not re.match(r"\d{4}-\d{2}-\d{2}$", launch_iso):
        launch_iso = ""
    # What the order says, to hold what was read up against - the text, and
    # what each screenshot was read as.
    source = text + "\n" + "\n".join(str(one or "") for one in got.get("screenshots") or [])
    from .. import ordercards

    if fields.get("launch") and not ordercards.says_when(source):
        # Nothing said when they launch: blank, not the day it was paid -
        # Blaze Lubbers' card said "Wednesday, October 14" from nothing.
        fields["launch"], launch_iso = "", ""
    launch_conflict = []
    if fields.get("launch"):
        # The day worked out here, and its name from the date - Nicolas
        # Sanmiguel's "push for launch tomorrow" on a Friday came out
        # "Friday, October 17", and the 17th is a Saturday.
        from .. import agents

        stamps = r"^\[\d\d/\d\d \d\d:\d\d[AP]M\][^:]*:\s*"
        when = None
        # The team's notes first, the Payra invoice's after them: Michael
        # Panza's thread said "Live Saturday October 17" and his card went out
        # Monday, October 19.
        team = [str(one) for one in notes or () if not str(one).startswith("Payra invoice note:")]
        invoice = [str(one) for one in notes or () if str(one).startswith("Payra invoice note:")]
        # "tomorrow" from the day it was written, not the day it's read:
        # Matthew Tarafa's "Launch tomorrow 1am EST", written on the Sunday,
        # went on his card as Tuesday. A note by the day its stamp says; the
        # invoice by the day it was paid.
        paid_on = _days_said(text, today)[0][1] if _days_said(text, today) else today
        team = [_named_days(one, _written_on(text, one, today)) for one in team]
        invoice = [_named_days(one, paid_on) for one in invoice]
        # The latest note that names a day is the day - Bailey Lawrence's
        # card kept Tre's "live tomorrow" (10/21) over Faith's later "BANK -
        # live Monday October 26". Two different days said is said too:
        # "are we supposed to guess this".
        each = []
        for one in team:
            try:
                found = agents.find_launch(ordercards.INVOICE_BOILERPLATE.sub(
                    " ", re.sub(stamps, "", one, flags=re.MULTILINE)), today=today)
            except Exception:
                found = None
            if found is not None:
                each.append(found)
        if len(set(each)) > 1:
            launch_conflict = list(dict.fromkeys(each))
        when = each[-1] if each else None
        for said in () if when else ("\n".join(team), "\n".join(invoice), _named_days_by_stamp(text, today)):
            try:
                when = agents.find_launch(ordercards.INVOICE_BOILERPLATE.sub(
                    " ", re.sub(stamps, "", said, flags=re.MULTILINE)), today=today)
            except Exception:
                when = None
            if when:
                break
        if when is None and launch_iso:
            try:
                when = date.fromisoformat(launch_iso)
            except ValueError:
                when = None
            # Claude's day only when the order names it - Travis Compere's
            # card said "Tuesday, October 20" off "not sure who this is but
            # they aren't setup" and a payment post with a date on it.
            if when is not None and not launch_written(when, source, today=today):
                when = None
                fields["launch"], launch_iso = "", ""
        if when is not None:
            fields["launch"], launch_iso = f"{when:%A, %B} {when.day}", when.isoformat()
    if launch_iso and not spark:
        # "start asap" / "once signs he is live today" written Saturday and
        # gavelled Monday: the day it was written is gone, and asap is the
        # first day it can be - today. Geno Glaze's was held as Sat 10/24,
        # "too far from today".
        try:
            read_as = date.fromisoformat(launch_iso)
        except ValueError:
            read_as = None
        if read_as is not None and read_as < today and _ASAP.search(source):
            fields["launch"], launch_iso = f"{today:%A, %B} {today.day}", today.isoformat()
            launch_conflict = []
    if spark.get("starts") is not None:
        # The membership starts when Spark says - never a day read elsewhere.
        starts = spark["starts"]
        fields["launch"], launch_iso = f"{starts:%A, %B} {starts.day}", starts.isoformat()
    elif spark.get("one_time"):
        # Bought and paid: filled from today.
        fields["launch"], launch_iso = f"{today:%A, %B} {today.day}", today.isoformat()
    if not launch_iso:
        # A Spark membership's own start - "Starts October 25, 2026" - which
        # the reading took for nothing: FND Life's card had no launch date and
        # sat waiting.
        starts = _starts_on(text)
        if starts is not None:
            fields["launch"], launch_iso = f"{starts:%A, %B} {starts.day}", starts.isoformat()
    if spark_post(text):
        # A Spark buyer's agency is Spark - "on their agency field, obv put
        # spark". FND Life's card went out with it blank.
        fields["agency"] = "Spark"
    if fields.get("agency") and not agency_usable(fields["agency"]):
        fields["agency"] = ""
    if not fields.get("agency"):
        # "aep" on a line of its own in the notes is the agency - Gavin
        # Mathieu's card went out with it blank.
        fields["agency"] = agency_said("\n".join([text, *notes]), agencies)
    agency = request_agency(request)
    unclear = ""
    if agency and not agency_usable(agency):
        # Bram Gutierrez's request said "Agency: F" and his card went out
        # with it: "what is the agency of this". Left blank, and said.
        unclear, agency = agency, ""
    if agency and not fields.get("agency"):
        # Where the agency is said: the client's New Lead Request in
        # #lead-lab - "he doesnt know where to get them".
        fields["agency"] = agency
        # The agency only: the rest of the request is what they asked for
        # once, not this order - Collin Williams' listed IN, MD and NC, and
        # his card was held for states his order never named.
        source += f"\nAgency: {agency}"
    zelle = zelle_note(text)
    if zelle and zelle.casefold() not in str(fields.get("notes") or "").casefold():
        # "add on additional notes on the cards both aged and fresh that if
        # they are zelle payments".
        fields["notes"] = "\n".join(one for one in (zelle, str(fields.get("notes") or "").strip()) if one)
    if kind == "aged" and not re.search(r"\d", str(fields.get("leads") or "")):
        # "180+ days ALL" over the stock list, and "37 leads" of the 90-180:
        # no total written down, so it was left blank and Alexis Lesme's
        # card was held for want of a count. All of them is a number.
        counted = ordercards.all_counted(source) or count_beside_lead_type(
            "\n".join([*(str(one) for one in notes or ()), text]))
        if counted:
            fields["leads"] = str(counted)
    parts = [{key: " ".join(str((one or {}).get(key) or "").split())
              for key in ("kind", "package", "leads", "states", "launch_iso", "name")}
             for one in got.get("parts") or [] if isinstance(one, dict)]
    # States read as the ones the order leaves out: "no ny ak no ha" went on
    # Shahan Uddin's card as "States: NY, AK".
    said_all = "\n".join([source, *(str(one) for one in notes or ())])
    fields["states"] = states_as_codes(fields.get("states") or "")
    for one in parts:
        one["states"] = states_as_codes(one.get("states") or "")
    fields["states"] = ordercards.states_left_out_fixed(fields.get("states") or "", said_all)
    # A state read in that the order never says, the rest its own list: off.
    fields["states"], dropped = ordercards.states_off_the_order(fields["states"], source)
    for one in parts:
        one["states"] = ordercards.states_left_out_fixed(one.get("states") or "", said_all)
        if not one.get("name"):
            one.pop("name", None)
    # Each line aged or not by its own words, not the payment's: Skyler
    # Liljenquist's "100 - $1/lead (180+) Mortgage protection" and "10 - Text
    # Verified MTG PLUS" were both read as aged - the product said Aged - and
    # went on one Aged Leads card.
    for one in parts:
        own = line_kind(one.get("package") or "")
        if own:
            one["kind"] = own
    # Other invoices the same charge paid: a line each, as their own forms
    # say - Dawson Clevenger's 10 of the 30-90 and 20 of the 90-180 in seven
    # states, besides the 130 of the 180+ in Idaho his post showed.
    also = [str(one) for one in notes or () if same_charge_note(one)]
    if also:
        source += "\n" + "\n".join(also)
        forms = [form] + [invoice_form(one) for one in also]
        if all(one.get("package") and re.search(r"\d", str(one.get("leads") or "")) for one in forms):
            parts = [{"kind": "aged" if re.search(r"\baged\b", one["package"], re.IGNORECASE) else "fresh",
                      "package": one["package"], "leads": re.search(r"\d[\d,]*", one["leads"]).group(0),
                      "states": _form_states(one.get("states") or ""),
                      "launch_iso": "" if re.search(r"\baged\b", one["package"], re.IGNORECASE) else launch_iso}
                     for one in forms]
    if not {"aged", "fresh"} <= {one["kind"] for one in parts}:
        # Read as one card when its own lead type says both: Allen Coleman's
        # "20 Fresh OTP Mtg PRO + 20 Aged Mtg Pro 30-90" went out as one New
        # Agent card, and no Aged Leads card was made.
        parts = parts_from_package(fields, launch_iso) or parts
    # Several agents in the one order: a card each - see `agent_cards`.
    agents = order_agents("\n".join(
        [str(one) for one in notes or () if not str(one).startswith("Payra invoice note:")]
        + ([_cards.post_invoice(text)[1]] if _cards.post_invoice(text)[1] else [])))
    # Lines with states of their own and no names: whose each is, worked out
    # before the card's made - Colby Whittaker's three lines of 15 Fresh Vets.
    lines = order_lines(text) if not (agents and kind == "fresh") else []
    return {"kind": kind, "client": client_name, "fields": fields, "launch_iso": launch_iso, "source": source,
            **({"agents": agents} if agents and kind == "fresh" else {}),
            **({"lines": lines} if lines else {}),
            **({"states_dropped": dropped} if dropped else {}),
            **({"launch_conflict": [one.isoformat() for one in launch_conflict]} if launch_conflict else {}),
            **({"agency_unclear": unclear} if unclear and not fields.get("agency") else {}),
            **({"overflow": True} if overflow else {}),
            "parts": [one for one in parts if one["kind"] in ("fresh", "aged") and one["package"]],
            "name": f"{'New Agent' if kind == 'fresh' else 'Aged Leads'} - {client_name or 'New client'}",
            "desc": order_desc(fields, kind), "due": "",
            "missing": [label for key, label in (AGED_FIELDS if kind == "aged" else ORDER_FIELDS)
                        if key not in ("notes", "agency") and not fields.get(key)]}


def _count_of(said: str) -> int | None:
    found = re.search(r"\d[\d,]*", str(said or ""))
    return int(found.group(0).replace(",", "")) if found else None


_PART_JOIN = re.compile(r"\s+\+\s+|\s*&\s*(?=\d)|,\s*(?=\d)|\s+and\s+(?=\d)|\s*;\s*", re.IGNORECASE)
_FRESH_WORD = re.compile(r"\bfresh\b|\botp\b|text[\s-]*verified|\bnew\s+agent\b", re.IGNORECASE)


#: What makes an order line aged in its own words: the word, an age range
#: ("180+", "30-90 days"), or aged pricing ("$1/lead" - fresh leads are $35 on).
_AGED_LINE = re.compile(r"\baged\b|\b\d{2,3}\s*\+|\b\d{2,3}\s*[-–]\s*\d{2,3}\s*(?:days?\b|\)|$)|"
                        r"\(\s*\d{2,3}\s*[-–]\s*\d{2,3}\s*\)|\$\s*(?:[1-9]|1[0-5])(?:\.\d+)?\s*/\s*lead",
                        re.IGNORECASE)


def notes_say_aged(notes: str) -> bool:
    """Whether the team's own notes say aged in so many words - the word and
    an age: "400 aged spanish iul 180+ days". Not the invoice's lines."""
    from .. import ordercards

    return ordercards.said_aged(notes)


def line_kind(package: str) -> str:
    """"aged", "fresh", or "" when an order line's own words don't say."""
    package = str(package or "")
    aged = bool(_AGED_LINE.search(package))
    fresh = bool(_FRESH_WORD.search(package)) and not re.search(r"\baged\b", package, re.IGNORECASE)
    if aged and not fresh:
        return "aged"
    if fresh and not aged:
        return "fresh"
    # "Text-Verified Aged ..." is aged; a fresh word beside an age range alone
    # says nothing either way.
    if aged and re.search(r"\baged\b", package, re.IGNORECASE):
        return "aged"
    return ""


def parts_from_package(fields: dict, launch_iso: str = "") -> list[dict]:
    """The order's lines out of a lead type that joins an aged line and a
    new-agent one - "20 Fresh OTP Mtg PRO + 20 Aged Mtg Pro 30-90" - each
    with its count, the order's states and, for the new-agent ones, its
    launch day. [] unless every line is plainly one or the other and there's
    one of each."""
    pieces = [one.strip() for one in _PART_JOIN.split(str(fields.get("package") or "")) if one.strip()]
    if len(pieces) < 2:
        return []
    parts = []
    for piece in pieces:
        count = re.match(r"\s*(\d[\d,]*)\s*(?:[-–—:x×]\s*)?(?=\S)", piece)
        kind = line_kind(piece)
        if not kind:
            return []
        parts.append({"kind": kind, "package": piece[count.end():].strip(" -–—") if count else piece,
                      "leads": count.group(1).replace(",", "") if count else "",
                      "states": str(fields.get("states") or ""),
                      "launch_iso": launch_iso if kind == "fresh" else ""})
    return parts if {"aged", "fresh"} <= {one["kind"] for one in parts} else []


def _once_each(lines) -> list[str]:
    """Lines in order, a repeat left out - the invoice's note and the
    thread saying the same GHL location id twice over."""
    seen, kept = set(), []
    for line in lines:
        key = " ".join(re.sub(r"^\s*From the Payra invoice:\s*", "", str(line)).split()).casefold()
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        kept.append(str(line))
    return kept


def split_order(card: dict, *, today=None) -> list[dict]:
    """An order that buys aged leads and new-agent leads at once, as the
    cards it is: one Aged Leads card with every aged line, and a New Agent
    card for each new-agent line - "yes build it that way" (Patrick Moreno:
    four aged lines delivered today, 34 OTP Vets waiting on states, 25 OTP
    IULs live Sunday). [] when the order isn't one of those."""
    from .. import agents as agents_mod, ordercards

    parts = [dict(one) for one in card.get("parts") or []]
    kinds = [one.get("kind") for one in parts]
    # Several aged lines on their own are one Aged Leads card too, each line
    # with its own states: Mateo Lyon's 163 Vets 30-90, 33 Vets 90-180 (Texas
    # only) and 167 FEX 180+ went out with every state on every line - "this
    # cost us MONEY".
    if not ({"aged", "fresh"} <= set(kinds) or (kinds.count("aged") >= 2 and "fresh" not in kinds)):
        return []
    for one in parts:
        # "25 OTP IULs" is the count and the lead type: the count has its own
        # field, and the lead type reads the same on every card without it.
        bare = re.sub(r"^\s*\d[\d,]*\s+", "", one.get("package") or "")
        if bare and bare != one["package"]:
            one["leads"] = one.get("leads") or re.match(r"\s*([\d,]+)", one["package"]).group(1)
            one["package"] = bare
    base = dict(card.get("fields") or {})
    client = card.get("client") or base.get("name") or "New client"
    source = str(card.get("source") or "")
    aged = [one for one in parts if one["kind"] == "aged"]
    fresh = [one for one in parts if one["kind"] == "fresh"]

    def line(one):
        count = _count_of(one.get("leads"))
        return (f"{count:,} " if count is not None else "") + one["package"] + (
            f" — {one['states']}" if one.get("states") else " — states to come")

    zelle = [bit for bit in str(base.get("notes") or "").splitlines() if bit.startswith("Paid via Zelle")]
    out = []
    fields = dict(base, launch="")
    counts = [_count_of(one.get("leads")) for one in aged]
    total = sum(one for one in counts if one is not None)
    fields["package"] = " + ".join(one["package"] for one in aged)
    fields["leads"] = (f"{total:,}" if len(aged) == 1 else
                       f"{total:,} (" + " + ".join(f"{one:,}" for one in counts if one is not None) + ")")
    places = list(dict.fromkeys(one.get("states") or "" for one in aged))
    fields["states"] = places[0] if len(places) == 1 else "; ".join(
        f"{one['package']}: {one.get('states') or 'to come'}" for one in aged)
    also = [line(one) for one in fresh]
    # The team's notes, less what's about the new-agent lines - "OTP VETS -
    # waiting for states", "Live on Sunday October 18" read on the aged card
    # as if the aged leads went live Sunday.
    theirs = {word.casefold() for one in fresh for word in re.findall(r"[A-Za-z]{3,}", one["package"])
              if word.casefold() not in {"leads", "lead", "and", "the", "text", "verified"}}
    # Only the words that are the new-agent lines' own: "Vets" is on both.
    theirs -= {word.casefold() for one in aged for word in re.findall(r"[A-Za-z]{3,}", one["package"])}

    kinds_named = {word.casefold() for one in parts for word in re.findall(r"[A-Za-z]{3,}", one["package"])} - {
        "leads", "lead", "and", "the", "text", "verified", "fresh", "new", "agent", "days", "day"}

    def for_new_agent(bit: str) -> bool:
        """A note line the New Agent card needs: not about the aged leads,
        not a day (its own Launch Date says that), not another line's count."""
        bare = re.sub(r"^\s*From the Payra invoice:\s*", "", bit)
        if re.search(r"\baged\b|\b(?:live|launch\w*|go(?:es|ing)?\s+live)\b", bare, re.IGNORECASE):
            return False
        # About one of the order's lines - "OTP VETS - waiting for states" -
        # is that line's, not every card's.
        if {word.casefold() for word in re.findall(r"[A-Za-z]{3,}", bare)} & kinds_named:
            return False
        # A line that's only states is the card's States field already.
        if _state_codes_named(bare) and not re.sub(r"(?i)\b(?:and|states?)\b|[,\s.&/-]", "",
                                                   re.sub("|".join(re.escape(name) for name in
                                                                   sorted(ordercards.US_STATES.values(),
                                                                          key=len, reverse=True)),
                                                          "", bare, flags=re.IGNORECASE)):
            return False
        return True

    def about_aged(bit: str) -> bool:
        words = {word.casefold() for word in re.findall(r"[A-Za-z]{3,}", bit)}
        return not (words & theirs) and not re.search(r"\b(?:live|launch\w*|go(?:es|ing)?\s+live)\b", bit, re.IGNORECASE)

    said = "\n".join(_once_each(bit for bit in str(base.get("notes") or "").splitlines()
                                if not bit.startswith("Paid via Zelle") and about_aged(bit)))
    fields["notes"] = "\n".join(
        # Only the aged lines - "ON AGED LEADS, DONT ADD THE FRESH ORDER ON THE
        # DESCRIPTION, but on fresh add both".
        [*zelle, "Order lines - each with its own states:" if not fresh else "Order lines:",
         *(f"• {line(one)}" for one in aged)]
        # Notes that are the invoice's already say so - Skyler Liljenquist's
        # read "From the thread:" over "From the Payra invoice:".
        + (["", *([] if said.strip().startswith("From the Payra invoice:") else ["From the thread:"]), said.strip()]
           if said.strip() else []))
    aged_for = _line_for(aged, client)
    if aged_for != client:
        fields = _for_somebody_else(fields, aged_for)
    out.append({"kind": "aged", "client": aged_for, "fields": fields, "name": f"Aged Leads - {aged_for}",
                "desc": order_desc(fields, "aged"), "launch_iso": "", "source": source})
    says_when = ordercards.says_when(source)
    today = today or date.today()

    def own_day(one):
        try:
            when = date.fromisoformat(one.get("launch_iso") or "") if says_when else None
        except ValueError:
            return None
        return when if when is not None and launch_written(when, source, today=today) else None

    # The order's own day for its new-agent lines when none of them has one
    # of its own: Jack Collins' "60 aged vet 30-90 / 20 fresh vet / ROADMAP
    # AGENCY LIVE OCT 19" - his New Agent card went out with no launch date.
    # Not when some lines have their own day: Patrick Moreno's 34 OTP Vets
    # waiting on states weren't live the Sunday his IULs were.
    whole = None
    if says_when and not any(own_day(one) for one in fresh):
        try:
            whole = date.fromisoformat(str(card.get("launch_iso") or ""))
        except ValueError:
            whole = None
    for one in fresh:
        fields = dict(base)
        count = _count_of(one.get("leads"))
        fields["package"] = one["package"]
        fields["leads"] = f"{count}" if count is not None else ""
        codes = ordercards._codes(one.get("states") or "")
        # "All states except NY, AK, HI" is three states left out, not three
        # ordered.
        fields["states"] = (" ".join(str(one.get("states")).split()) if ordercards._ALL_BUT.search(
            str(one.get("states") or "")) else ", ".join(codes) if codes else "")
        when = own_day(one) or whole or ""
        fields["launch"] = f"{when:%A, %B} {when.day}" if when else ""
        # Nothing of the other lines - no lead type, count or day: the filing
        # reads a card's notes, and listing them made this one card read as
        # three orders on two days. But the thread's setup notes, yes: Syed
        # Tahir Ahmed's GHL location id, API key and "fresh leads go to this
        # pipeline" were on his Aged card only - "therese and nicole have
        # different setup".
        setup = [bit for bit in _once_each(str(base.get("notes") or "").splitlines())
                 if bit.strip() and not bit.startswith("Paid via Zelle") and for_new_agent(bit)]
        # Which of them the invoice's note said, not the thread: its lines run
        # on under "From the Payra invoice:" until a blank line - Jared
        # Flanigan's "Paid by: Tony Moderno ..." went on as "From the thread:".
        invoiced, under = set(), False
        for bit in str(base.get("notes") or "").splitlines():
            if bit.startswith("From the Payra invoice:"):
                under = True
            elif not bit.strip():
                under = False
            if under:
                invoiced.add(bit)
        if setup and all(bit in invoiced for bit in setup) and not setup[0].startswith("From the Payra invoice:"):
            setup = ["From the Payra invoice:", *setup]
        others = [other for other in fresh if other is not one]
        rest = " and ".join(filter(None, ["the Aged Leads card" if aged else "",
                                          "its own New Agent card" + ("s" if len(others) > 1 else "") if others else ""]))
        fields["notes"] = "\n".join(
            [*zelle, *([] if codes else ["No states yet - they'll be posted in the thread."]),
             f"Part of a bigger order of {client}'s - the rest of it is on {rest}."]
            # The whole order on the New Agent card - "on fresh add both" -
            # the aged lines under a heading the filing doesn't read.
            + (["", agents_mod.AGED_REFERENCE, *(f"• {line(other)}" for other in aged), ""] if aged else [])
            + (["", *([] if setup[0].startswith("From the Payra invoice:") else ["From the thread:"]), *setup]
               if setup else []))
        fresh_for = _line_for([one], client)
        if fresh_for != client:
            fields = _for_somebody_else(fields, fresh_for)
        out.append({"kind": "fresh", "client": fresh_for, "fields": fields, "name": f"New Agent - {fresh_for}",
                    "desc": order_desc(fields, "fresh"), "launch_iso": when.isoformat() if when else "",
                    "source": source})
    return out


def _line_for(lines: list[dict], client: str) -> str:
    """Who order lines are for when the order sends them to somebody of their
    own - Faith's "43 OTP Vets to Shahan Uddin ... 100 Aged Vets ... to
    Mohammad Kanan" - else the client."""
    from .. import ordercards

    named = list(dict.fromkeys(" ".join(str(one.get("name") or "").split()) for one in lines))
    if len(named) == 1 and named[0] and not ordercards.same_person(named[0], client):
        return named[0]
    return client


def _for_somebody_else(fields: dict, name: str) -> dict:
    """A card's fields for another person on the same order: their name, and
    not the client's email or phone - an email with their name in it is
    theirs (Shahan Uddin's "shahanuddin2005@...")."""
    words = [one for one in re.findall(r"[a-z]{3,}", name.casefold())]
    email = str(fields.get("email") or "")
    mine = bool(words) and any(one in email.split("@")[0].casefold() for one in words)
    return dict(fields, name=name, email=email if mine else "", phone=fields.get("phone") if mine else "")


#: A line of dashes - "* - - - - - -" - between one agent and the next.
_AGENT_BREAK = re.compile(r"^[\s*•·]*(?:-\s*){5,}$|^[\s*•·]*[-–—_=]{5,}\s*$")
_LABELLED = re.compile(r"^\s*(?P<label>name|e-?mail|number|phone(?:\s*number)?|states?|order\s*type|"
                       r"lead\s*type|package|webhook(?:\s*url)?)\s*(?:[:\-–]\s*|\s+(?=\S))(?P<value>.*)$",
                       re.IGNORECASE)
_PHONE_SAID = re.compile(r"\+?1?[\s\-.(]*\d{3}[\s\-.)]*\d{3}[\s\-.]*\d{4}")


def states_as_codes(states: str) -> str:
    """States written out in full, as the codes the card uses: Marc
    Williams' "Colorado, Connecticut, Idaho, ..." was held as "isn't a list
    of states". Only when every item is one state; anything else as it was."""
    from .. import ordercards

    said = str(states or "")
    if not said.strip() or re.search(r"\d", said) or ordercards._ALL_BUT.search(said):
        return said
    bits = [one for one in re.split(r"\s*(?:,|;|\n|/|&|\band\b)\s*", said, flags=re.IGNORECASE) if one.strip()]
    codes = []
    for bit in bits:
        found = _state_codes_named(bit)
        if len(found) != 1:
            return said
        codes.append(found[0])
    return ", ".join(dict.fromkeys(codes))


def _state_codes_named(said: str) -> list[str]:
    """Two-letter codes for the states a line names, spelled out or not -
    "South,Carolina", "Washington DC" and "West Virginia" included."""
    from .. import ordercards

    text = str(said or "")
    names = sorted(((name, code) for code, name in ordercards.US_STATES.items()), key=lambda one: -len(one[0]))
    found, taken = [], [False] * len(text)

    def take(pattern, code):
        for match in re.finditer(pattern, text, re.IGNORECASE):
            if any(taken[match.start():match.end()]):
                continue
            taken[match.start():match.end()] = [True] * (match.end() - match.start())
            found.append((match.start(), code))

    take(r"\bwashington[\s,]*d\.?\s*c\b\.?", "DC")
    for name, code in names:
        take(r"\b" + r"[\s,]+".join(map(re.escape, name.split())) + r"\b", code)
    for match in re.finditer(r"\b[A-Z]{2}\b", text):
        if match.group(0) in ordercards.US_STATES and not any(taken[match.start():match.end()]):
            found.append((match.start(), match.group(0)))
    codes = []
    for _at, code in sorted(found):
        if code not in codes:
            codes.append(code)
    return codes


def _is_name_line(line: str) -> bool:
    found = _LABELLED.match(str(line or "").strip(" *•\t"))
    return bool(found) and found.group("label").casefold() == "name"


def order_agents(text: str) -> list[dict]:
    """Several agents in one order, each with their own name, email, phone,
    states, lead count and webhook - Colby Whittaker paid once for Gabriel
    Lucas, Sawyer Wetzel, Justin Bailey, Dillon Willis and himself, 28 or 42
    Fresh Vets each. [] unless at least two are plainly there."""
    blocks, current = [], []
    for line in str(text or "").splitlines():
        if _AGENT_BREAK.match(line):
            blocks.append(current)
            current = []
            continue
        if _is_name_line(line) and any(_is_name_line(one) or re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", one)
                                       for one in current):
            # A second "Name:" with no line of dashes between: the next agent.
            blocks.append(current)
            current = []
        current.append(line)
    blocks.append(current)
    agents = []
    for block in blocks:
        one = {"name": "", "email": "", "phone": "", "states": "", "order": "", "webhook": ""}
        last = ""
        for line in block:
            bare = line.strip(" *•\t")
            if not bare:
                continue
            labelled = _LABELLED.match(bare)
            label = labelled.group("label").casefold() if labelled else ""
            value = labelled.group("value").strip() if labelled else ""
            if not labelled and last.startswith("state") and _state_codes_named(bare):
                # Justin Bailey's states ran onto a second line.
                one["states"] += ", " + bare
                continue
            last = label
            if label == "name" and not one["name"]:
                one["name"] = " ".join(value.split())
            elif label.startswith("state") and not one["states"]:
                one["states"] = value
            elif (label == "number" or label.startswith("phone")) and not one["phone"]:
                found = _PHONE_SAID.search(value)
                one["phone"] = found.group(0).strip(" -.") if found else value
            elif label in ("order type", "lead type", "package") or label.startswith("order"):
                one["order"] = one["order"] or value
            elif label.startswith("webhook"):
                url = re.search(r"https?://\S+", value)
                one["webhook"] = one["webhook"] or (url.group(0) if url else "")
                one["webhook_how"] = value[url.end():].strip() if url else ""
            elif re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", bare) and not one["email"]:
                one["email"] = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", bare).group(0)
            elif _PHONE_SAID.search(bare) and not one["phone"] and not re.search(r"https?://", bare):
                one["phone"] = _PHONE_SAID.search(bare).group(0).strip(" -.")
            elif not one["name"] and _A_NAME.match(bare) and not _NOT_NAME_WORDS.search(bare):
                one["name"] = " ".join(bare.split())
        if not (one["name"] and (one["order"] or one["states"]) and (one["email"] or one["phone"])):
            continue
        # Once each: Colby Whittaker's five came out as eight cards - the
        # list was in the notes twice over. The same name or email is the
        # same agent, and what one copy is missing the other fills in.
        same = next((had for had in agents
                     if had["name"].casefold() == one["name"].casefold()
                     or (one["email"] and had["email"].casefold() == one["email"].casefold())), None)
        if same is not None:
            for key, value in one.items():
                if value and not same.get(key):
                    same[key] = value
            continue
        agents.append(one)
    return agents if len(agents) >= 2 else []


def agent_cards(card: dict, *, today=None) -> list[dict]:
    """An order for several agents as its cards: a New Agent card each, with
    their own details, the order's launch day, and who paid. []  unless the
    order is one of those."""
    from .. import ordercards

    agents = list(card.get("agents") or [])
    if len(agents) < 2:
        return []
    base = dict(card.get("fields") or {})
    source = str(card.get("source") or "")
    payer = ordercards.paid_by(source) or card.get("client") or ""
    paid = re.search(r"AMOUNT:\**\s*\$?\s*([\d,]+(?:\.\d+)?)", source, re.IGNORECASE)
    zelle = [bit for bit in str(base.get("notes") or "").splitlines() if bit.startswith("Paid via Zelle")]
    launch = ""
    try:
        launch = date.fromisoformat(str(card.get("launch_iso") or "")).isoformat()
    except ValueError:
        launch = ""
    out = []
    for one in agents:
        count = re.match(r"\s*(\d[\d,]*)\s*(.*)$", one["order"] or "")
        states = one["states"]
        if re.search(r"\ball\b", states, re.IGNORECASE):
            shown = " ".join(states.split())
        else:
            shown = ", ".join(_state_codes_named(states))
        fields = dict(base, name=one["name"], email=one["email"], phone=one["phone"], states=shown,
                      package=(count.group(2).strip() if count else one["order"]) or base.get("package", ""),
                      leads=count.group(1).replace(",", "") if count else "",
                      launch=(f"{date.fromisoformat(launch):%A, %B} {date.fromisoformat(launch).day}" if launch else ""))
        fields["notes"] = "\n".join([
            *zelle,
            *([f"Webhook URL: {one['webhook']}" + (f" {one['webhook_how']}" if one.get("webhook_how") else "")]
              if one["webhook"] else []),
            f"Paid by {payer} - one payment for {len(agents)} agents"
            + (f" (${paid.group(1)})" if paid else "") + ".",
            # Matched by its states, the invoice naming nobody: said, so it's
            # looked at before the setup.
            *([f"The invoice line didn't name the agent - matched to {one['name']} by its states "
               f"({one['matched_on']}). Check before setting up."] if one.get("matched_on") else [])])
        out.append({"kind": "fresh", "client": one["name"], "fields": fields, "name": f"New Agent - {one['name']}",
                    "desc": order_desc(fields, "fresh"), "launch_iso": launch, "source": source,
                    "agents_of": len(agents)})
    return out


_PACKET_TOTAL = re.compile(r"(?<![\d$.,])(\d[\d,]*)\s+aged\b", re.IGNORECASE)
_PACKET_COUNT = re.compile(r"(?<![\d$.,])(\d{1,2})\s+(?:separate\s+)?(?:google\s+sheets?|packets?|packages?|batch(?:es)?)\b",
                           re.IGNORECASE)


def _packet_counts(source: str, counts) -> bool:
    """Whether the lines' counts are the order's packets: Mohammad Kanan's
    "600 Aged Vets ... 6 separate google sheets", a packet 100, read as 100,
    100, 100, 100 and 200 - each held as "isn't in the order"."""
    counts = [one for one in counts if one is not None]
    totals = {int(one.replace(",", "")) for one in _PACKET_TOTAL.findall(source or "")}
    packets = {int(one) for one in _PACKET_COUNT.findall(source or "")}
    for total in totals:
        for many in packets:
            if many < 2 or total % many or sum(counts) != total:
                continue
            each = total // many
            if all(one % each == 0 for one in counts):
                return True
    return False


def split_doubts(cards: list[dict], *, today=None) -> list[str]:
    """What doesn't add up in a split order: each line's count and states
    have to be in the order as written, and each card gets what a single
    card's checked for - "make sure that this task ryte doing is not gonna
    cost us anymore money": the lead type against the payment, the launch
    day, the agency, and an aged card's states, count and lead type, since
    it goes out as soon as it's made."""
    from datetime import date as _date

    from .. import ordercards, setupcheck

    if not cards:
        return []
    today = today or _date.today()
    source = str(cards[0].get("source") or "")
    wrong = []
    # Who paid, against who it's for - a split order went round the check a
    # single card has: Tony Moderno paid, the thread said "Name: Shelby
    # Guest", and both cards went out as Tony's. Not for an order of several
    # agents, which is one person paying for them all.
    if not cards[0].get("agents_of"):
        wrong += ordercards.payer_doubts(source, str(cards[0].get("client") or ""))[0]
    # "5 agents" said, and that many cards - not eight.
    many = re.search(r"\b(\d{1,2})\s+agents\b", source, re.IGNORECASE)
    if cards[0].get("agents_of") and many and int(many.group(1)) != len(cards):
        wrong.append(f"The order says {many.group(1)} agents, and I read {len(cards)}: "
                     + ", ".join(one["client"] for one in cards))
    for card in cards:
        fields = card.get("fields") or {}
        name, kind = card["name"], card["kind"]
        counts = ([_count_of(bit) for bit in re.findall(r"\d[\d,]*", fields.get("leads") or "")]
                  if kind == "aged" else [_count_of(fields.get("leads"))])
        # A line's count made of its states' own: Skyler Liljenquist's 32
        # Mortgage Protection 30-90 is FL 18 + VA 14, each said in the order,
        # and "32 isn't in the order" held a right card.
        by_state = set()
        for bit in str(fields.get("states") or "").split(";"):
            said = [int(one) for one in re.findall(r"(?<![\d-])(\d{1,5})(?![\d+-])", bit.rsplit(":", 1)[-1])]
            if len(said) >= 2 and all(ordercards.count_said(one, source) for one in said):
                by_state.add(sum(said))
        packed = _packet_counts(source, counts[1:] if len(counts) > 1 else counts)
        for count in counts[1:] if len(counts) > 1 else counts:
            if count is not None and not ordercards.count_said(count, source, total=True) and count not in by_state \
                    and not packed:
                wrong.append(f"{name}: {count:,} leads isn't in the order")
            elif count is not None and ordercards.only_a_cap(count, source):
                wrong.append(f"{name}: {count:,} is the daily cap the order gives, not how many they bought")
        states = " ".join(str(fields.get("states") or "").split())
        # "ha" for Hawaii, left out, is said: "All states except NY, AK, HI".
        said_out = set(ordercards.left_out_states(source)) if ordercards._ALL_BUT.search(states) else set()
        for code in ordercards._codes(states):
            if code not in said_out and not ordercards._said_state(code, source):
                wrong.append(f"{name}: {code} isn't in the order")
        for bit in states.split(";"):
            if not ordercards._ALL_BUT.search(bit):
                wrong += [f"{name}: {one}" for one in ordercards._ordered_but_left_out(
                    ordercards._codes(bit.rsplit(":", 1)[-1]), source)]
        listed = bool(ordercards._codes(states) or re.search(r"\ball\b", states, re.IGNORECASE)
                      or ordercards.SAME_STATES.search(states) or ordercards._SAME_ANSWER.match(states))
        if kind == "aged":
            # Out the door the moment it's in Aged Leads Order Done.
            if not states or re.search(r"\bto come\b", states, re.IGNORECASE):
                wrong.append(f"{name}: no states for "
                             + (", ".join(bit.split(":")[0].strip() for bit in states.split(";")
                                          if re.search(r"\bto come\b", bit, re.IGNORECASE)) or "it")
                             + " — an aged card goes out as soon as it's made")
            elif not listed:
                wrong.append(f"{name}: “{states}” isn't a list of states")
            if not re.search(r"\d", str(fields.get("leads") or "")):
                wrong.append(f"{name}: no lead count — an aged card goes out as soon as it's made")
            if not str(fields.get("package") or "").strip():
                wrong.append(f"{name}: no lead type — an aged card goes out as soon as it's made")
        elif states and not listed:
            wrong.append(f"{name}: “{states}” isn't a list of states")
        # A screenshot read with a guess in it: "(the text reads 'my', taken
        # as Wyoming)".
        guessed = re.search(r"\([^()]*\b(?:taken\s+as|assumed|unclear|illegible)\b[^()]*\)",
                            str(fields.get("notes") or ""), re.IGNORECASE)
        if guessed:
            said = (f"A screenshot was read with a guess: “{guessed.group(0).strip('()')}” - check it before it "
                    "goes out")
            if said not in wrong:
                wrong.append(said)
        wrong += [f"{name}: {one}" for one in ordercards.agency_doubts(
            str(fields.get("package") or ""), str(fields.get("leads") or ""), source)]
        wrong += [f"{name}: {one}" for one in ordercards.launch_doubts(
            str(card.get("launch_iso") or ""), source, kind=kind, today=today)]
    # Aged against what the payment was for: an aged payment needs an aged
    # card, and one that isn't aged can't be only aged cards. Not the lead
    # type itself - Patrick Moreno's $12,091 for six lines showed Payra's
    # one product, "Text-Verified Trucker IUL", which none of them was.
    product = re.search(r"PRODUCT:\**\s*([^\n]*)", source, re.IGNORECASE)
    product = " ".join(product.group(1).split()) if product else ""
    if product and not re.match(r"^[—\-–(]", product):
        kinds = {card["kind"] for card in cards}
        aged_paid = bool(re.search(r"\baged\b", product, re.IGNORECASE))
        if aged_paid and "aged" not in kinds:
            wrong.append(f"Lead type: the payment is for “{product}” - aged - but no card here is aged")
        family, _subtypes = setupcheck.product_of(product)
        if not aged_paid and family and kinds == {"aged"}:
            wrong.append(f"Lead type: the payment is for “{product}” - not aged - but every card here is aged")
    return wrong


def order_card_make(config: Config, kind: str, card: dict, *, list_name: str = "") -> dict:
    """The card, made new - "create a new card not copy" - in its list, with
    the order's title and description. No due date: "that way it wont be
    red". In `list_name` instead when given - an aged order paid by bank
    waits in Franklin (Admin). {"id", "url", "list"}."""
    from .. import ordercards, trello

    client = open_trello(config)
    try:
        # An Overflow order is a new agent's card that lives with the aged.
        place = "aged" if card.get("overflow") else kind
        wanted = (config.secrets.order_fresh_list if place == "fresh" else config.secrets.order_aged_list) or ""
        lists = client.board_lists(config.secrets.trello_board_id)
        where = trello.find_list(lists, list_name) if list_name else None
        if list_name and where is None:
            raise ValueError(f"There's no “{list_name}” list on the board.")
        where = where or ordercards.list_for(place, lists, wanted=wanted)
        if where is None:
            raise ValueError(f"There's no {'Queue' if place == 'fresh' else 'Aged Leads Order Done'} list on the board"
                             + (f" called “{wanted}”" if wanted else "") + ".")
        # "when creating a new aged leads card, JUST THE AGED LEADS, put them
        # below the list" - new agents stay on top.
        made = _create_once(client, str(where["id"]), card["name"],
                            position="bottom" if place == "aged" and not list_name else "top")
        card_id = str(made.get("id") or "")
        out = {"id": card_id, "url": str(made.get("shortUrl") or made.get("url") or ""),
               "list": str(where.get("name") or "")}
        # Made is made: a description that won't go on is said with the
        # card's link, not "couldn't make the card" - that left a blank card
        # on the board and a second one on the next gavel.
        trouble = _described(client, card_id, card)
        if trouble:
            out["trouble"] = trouble
        return out
    finally:
        client.close()


#: Waits before putting a card's description on again.
DESC_PAUSES = (2.0, 5.0)


def _described(client, card_id: str, card: dict) -> list[str]:
    """The card's description put on, and read back to be sure it's what was
    written and the title is the order's - "he cant fail this card
    creation". What's wrong with it, or []."""
    import time

    from .. import trello

    wanted_desc = str(card.get("desc") or "")
    if not wanted_desc:
        return []
    last = None
    for pause in (0.0, *DESC_PAUSES):
        if pause:
            time.sleep(pause)
        try:
            client.set_description(card_id, wanted_desc)
            last = None
            break
        except Exception as exc:
            last = exc
    if last is not None:
        return [f"its description didn't go on ({_short(last, 120)}) — the card is there with only its title"]
    reading = getattr(client, "card_detail", None)
    if reading is None:
        return []

    def flat(text):
        return " ".join(str(text or "").split())

    for attempt in range(2):
        try:
            there = reading(card_id) or {}
        except Exception:
            logging.getLogger("wilbyte.bot").warning("Couldn't read the new card back", exc_info=True)
            return []
        trouble = []
        if flat(there.get("name")) != flat(card.get("name")):
            trouble.append(f"its title reads “{there.get('name')}”, not “{card.get('name')}”")
        if flat(there.get("desc")) != flat(trello.no_secrets(wanted_desc)):
            trouble.append("its description on Trello isn't what I wrote")
        if not trouble or attempt:
            return trouble
        try:
            if any("title" in one for one in trouble):
                client.rename_card(card_id, str(card.get("name") or ""))
            client.set_description(card_id, wanted_desc)
        except Exception as exc:
            return trouble + [f"putting it right failed ({_short(exc, 120)})"]
    return []


#: Waits before asking Trello again to make a card it answered 5xx to.
CREATE_PAUSES = (5.0, 15.0, 30.0)


def _create_once(client, list_id: str, name: str, *, now=None, position: str = "top") -> dict:
    """A card made, through Trello's "having a moment": Blake Settle's came
    back HTTP 503 and wasn't made. A 5xx can still have made it, so the list
    is looked at for it before asking again - one card, never two."""
    import time

    from .. import trello

    started = (now or datetime.now(timezone.utc)) - timedelta(seconds=30)
    for pause in (*CREATE_PAUSES, None):
        try:
            return client.create_card(list_id, name, position=position)
        except trello.TrelloError as exc:
            if pause is None or not re.search(r"HTTP 5\d\d", str(exc)):
                raise
        time.sleep(pause)
        for one in client.list_cards(list_id):
            try:
                made = datetime.fromtimestamp(int(str(one.get("id") or "")[:8], 16), timezone.utc)
            except ValueError:
                continue
            if str(one.get("name") or "") == name and made >= started:
                return one
    raise AssertionError("unreachable")  # pragma: no cover


def order_card_move(config: Config, card_id: str, kind: str) -> str:
    """The card moved to its kind's list - In Que, or Aged Leads Order
    Done. The list's name."""
    from .. import ordercards

    client = open_trello(config)
    try:
        wanted = (config.secrets.order_fresh_list if kind == "fresh" else config.secrets.order_aged_list) or ""
        where = ordercards.list_for(kind, client.board_lists(config.secrets.trello_board_id), wanted=wanted)
        if where is None:
            raise ValueError(f"There's no {'Queue' if kind == 'fresh' else 'Aged Leads Order Done'} list on the board.")
        client.move_card(card_id, str(where["id"]), position="bottom" if kind == "aged" else "top")
        return str(where.get("name") or "")
    finally:
        client.close()


#: The largest screenshot carried onto a card.
ORDER_SHOT_MOST_BYTES = 10 * 1024 * 1024


def shot_key(url: str) -> str:
    """A Discord picture by where it is, not the expiry Discord tacks on."""
    return str(url or "").split("?", 1)[0]


def order_card_attach(config: Config, card_id: str, urls: list[str], *, done: tuple = ()) -> list[str]:
    """The team's screenshots uploaded onto the card - "add the text and the
    image as well". Uploaded, not linked: Discord's links run out. Those in
    `done` are already there. The keys of the ones attached now."""
    import logging

    import httpx

    attached = []
    todo = [url for url in urls if shot_key(url) not in set(done)]
    if not todo:
        return attached
    client = open_trello(config)
    try:
        for at, url in enumerate(todo, start=1):
            try:
                got = httpx.get(url, timeout=30, follow_redirects=True)
                got.raise_for_status()
            except Exception as exc:
                logging.getLogger("wilbyte.bot").warning("Couldn't fetch a screenshot for the card: %s", exc)
                continue
            if len(got.content) > ORDER_SHOT_MOST_BYTES:
                continue
            mime = got.headers.get("content-type", "image/png").split(";")[0]
            name = shot_key(url).rsplit("/", 1)[-1] or f"screenshot-{at}.png"
            client.attach_file(card_id, name, got.content, mime)
            attached.append(shot_key(url))
    finally:
        client.close()
    return attached


def order_card_open(config: Config, card_id: str) -> bool:
    """Whether a card is still on the board - not archived, not deleted."""
    client = open_trello(config)
    try:
        try:
            card = client._request("GET", f"/cards/{card_id}", params={"fields": "closed"})
        except Exception as exc:
            if "404" in str(exc) or "not found" in str(exc).casefold():
                return False
            raise
        return not (card or {}).get("closed")
    finally:
        client.close()


def order_card_update(config: Config, card_id: str, *, written: str, desc: str, name: str = "",
                      commented: tuple = ()) -> tuple[str, list[str]]:
    """New notes onto a card RYTE made. Its description is rewritten only when
    it's still what RYTE wrote; when somebody has edited it by hand, what's
    new goes in a comment instead - each line once. ("updated" / "commented"
    / "same", the lines commented)."""
    from .. import ordercards

    client = open_trello(config)
    try:
        now = client.card_detail(card_id)
        if ordercards.same_text(now.get("desc", ""), desc):
            return "same", []
        if ordercards.same_text(now.get("desc", ""), written):
            client.set_description(card_id, desc)
            if name and not ordercards.same_text(now.get("name", ""), name):
                client.rename_card(card_id, name)
            return "updated", []
        before = {" ".join(line.split()) for line in commented}
        new = [line for line in ordercards.changed_lines(now.get("desc", ""), desc)
               if " ".join(line.split()) not in before]
        if not new:
            return "same", []
        client.add_comment(card_id, "📝 New in Discord since this card was made:\n" + "\n".join(new))
        return "commented", new
    finally:
        client.close()


def hub_task_verdict(config: Config, *, task: str, records: list[str], today) -> dict:
    """Whether the hub shows a team request done: "set lead schedule to
    7am-7pm PST" against Abdullah Hatem's hub record. `records` are the hub's
    fields for every order of theirs, emails and phones already blanked.
    {"verdict": "done" | "not_done" | "unclear", "why": one line}."""
    from anthropic import Anthropic

    config.secrets.require("anthropic_api_key")
    client = Anthropic(api_key=config.secrets.anthropic_api_key)
    tool = {
        "name": "verdict",
        "description": "Whether the hub's record shows this request done.",
        "input_schema": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": ["done", "not_done", "unclear"], "description": (
                    "'done' only when a field on the hub plainly shows it - the schedule set to those hours, "
                    "the states added, the cap changed, the agent set up and scheduled. 'not_done' when the "
                    "hub plainly shows otherwise, or has no record of them for a setup. 'unclear' when the "
                    "request is about something the hub doesn't hold (ads, a contract, a message to them).")},
                "why": {"type": "string", "description": "One short line: the hub field and what it says."},
            },
            "required": ["verdict", "why"],
        },
    }
    said = "\n\n".join(f"--- Order {at} ---\n{one}" for at, one in enumerate(records, start=1)) or "(no record on the hub)"
    response = client.messages.create(
        model=config.copy.model, max_tokens=300,
        system=("You check Agent Lead Lab's lead-delivery hub for whether the team did what was asked for a "
                "client. Read only the hub's fields. Times may be written differently (7am-7pm PST is "
                "07:00-19:00 America/Los_Angeles); match the meaning, not the wording. Never say done on a guess."),
        tools=[tool], tool_choice={"type": "tool", "name": "verdict"},
        messages=[{"role": "user", "content": f"Today is {today:%Y-%m-%d}.\n\nAsked of the team: {task}\n\nThe hub:\n{said}"}],
    )
    got = _tool_input(response, "verdict")
    verdict = str(got.get("verdict") or "").casefold()
    return {"verdict": verdict if verdict in ("done", "not_done", "unclear") else "unclear",
            "why": " ".join(str(got.get("why") or "").split())[:200]}



#: How each kind of order card's title starts.
_CARD_TITLE = {"fresh": r"\s*new\s*agent\b", "aged": r"\s*aged\s*leads?\b"}


def new_agent_cards(config: Config, kind: str = "fresh") -> list[dict]:
    """The board's open New Agent (or Aged Leads) cards - {"id", "name",
    "url"} - for not making a card for somebody who has one, by hand or not."""
    client = open_trello(config)
    try:
        cards = client.board_cards(config.secrets.trello_board_id)
    finally:
        client.close()
    return [{"id": str(one.get("id") or ""), "name": str(one.get("name") or ""),
             "url": str(one.get("shortUrl") or one.get("url") or "")}
            for one in cards if re.match(_CARD_TITLE[kind], str(one.get("name") or ""), re.IGNORECASE)]


def order_cards_since(config: Config, since) -> list[dict]:
    """Every New Agent and Aged Leads card made since `since`, archived ones
    too - an aged card's ticked and archived by evening, and the audit
    took Boston Farr's for missing. {"id", "name", "url", "closed"}."""
    client = open_trello(config)
    try:
        cards = client.board_cards(config.secrets.trello_board_id, archived=True)
    finally:
        client.close()
    found = []
    for one in cards:
        name = str(one.get("name") or "")
        made = card_made_at(str(one.get("id") or ""))
        if made is None or made < since:
            continue
        if any(re.match(pattern, name, re.IGNORECASE) for pattern in _CARD_TITLE.values()):
            found.append({"id": str(one.get("id") or ""), "name": name, "closed": bool(one.get("closed")),
                          "url": str(one.get("shortUrl") or one.get("url") or ""), "desc": str(one.get("desc") or "")})
    return found


def card_made_at(card_id: str):
    """When a Trello card was made - its id starts with the time, in seconds."""
    try:
        return datetime.fromtimestamp(int(str(card_id)[:8], 16), tz=timezone.utc)
    except (TypeError, ValueError):
        return None


#: How recent a client's New Agent card has to be for another payment to be
#: the same order - "bram paid alot of time for the same order".
SAME_ORDER_DAYS = 7
#: Aged leads are bought again and again - only a payment the same day is
#: the same order.
SAME_AGED_ORDER = timedelta(hours=24)


def recent_card_for(config: Config, name: str, *, now=None, kind: str = "fresh") -> dict | None:
    """Their card from the last week (a day, for aged leads), if there is one
    - another payment then goes on it rather than making a second card."""
    now = now or datetime.now(timezone.utc)
    window = SAME_AGED_ORDER if kind == "aged" else timedelta(days=SAME_ORDER_DAYS)
    cards = [one for one in new_agent_cards(config, kind)
             if (card_made_at(one["id"]) or now) >= now - window]
    return has_new_agent_card(name, cards)


def order_payment_comment(config: Config, card_id: str, text: str) -> None:
    client = open_trello(config)
    try:
        client.add_comment(card_id, text)
    finally:
        client.close()


def has_new_agent_card(name: str, cards: list[dict]) -> dict | None:
    """Their New Agent card, by their whole name in its title."""
    from .. import clearout

    for one in cards:
        title = re.sub(r"^\s*(?:new\s*agent|aged\s*leads?)\s*[-–—:]*\s*", "", one["name"], flags=re.IGNORECASE)
        if clearout.name_match(name, title) >= 2:
            return one
    return None



#: Payra read straight for the last two days is kept this long and shared -
#: every order card, recheck and bank check reading it afresh drew
#: "HTTP 429: Access to this resource has been throttled".
PAYRA_FRESH_SECONDS = 90
#: Pages of the last two days read at most - oldest first, so a page short
#: and the newest payment is the one missed.
PAYRA_RECENT_PAGES = 25
#: How far back a payment not in the shared copy is read for, fresh.
PAYRA_JUST_PAID = timedelta(hours=6)
#: After a 429, the saved copy for this long before asking Payra again.
PAYRA_THROTTLED_SECONDS = 300
_PAYRA_RECENT: dict = {}
_PAYRA_RECENT_LOCK = threading.Lock()


def payra_recent(config: Config, kind: str, *, now=None) -> dict:
    """The last two days of Payra's `kind` ("invoices" / "payments"), slim,
    by id - read now at most every PAYRA_FRESH_SECONDS, shared by everything
    that asks, and the half-hourly saved copy while Payra says to wait."""
    import time

    from .. import payraapi

    token = str(getattr(config.secrets, "payra_api_token", "") or "").strip()
    site = str(getattr(config.secrets, "payra_site_id", "") or "").strip()
    saved = lambda: dict(payraapi.load().get(kind) or {})  # noqa: E731
    if not (token and site):
        return saved()
    with _PAYRA_RECENT_LOCK:
        held = _PAYRA_RECENT.get(kind)
        if held and time.monotonic() - held[0] < PAYRA_FRESH_SECONDS:
            return dict(held[1])
        if time.monotonic() < _PAYRA_RECENT.get("_throttled", 0):
            return saved()
        now = now or datetime.now(timezone.utc)
        slim = payraapi.SLIM[kind]
        found = {}
        try:
            with payraapi.PayraClient(token, site, timeout=60.0) as client:
                after = payraapi.when(now - timedelta(days=2))
                # Oldest first: five pages left a busy two days' newest out.
                for _page in range(PAYRA_RECENT_PAGES):
                    got = client.changed(kind, after)
                    for record in got.get("records") or []:
                        one = slim(record)
                        if one["id"]:
                            found[one["id"]] = one
                    onward = str(got.get("next_updated_after") or "")
                    if not got.get("records") or not onward or onward == after:
                        break
                    after = onward
        except Exception as exc:
            if "429" in str(exc):
                _PAYRA_RECENT["_throttled"] = time.monotonic() + PAYRA_THROTTLED_SECONDS
            logging.getLogger("wilbyte.bot").warning("Couldn't read Payra's %s - the saved copy instead: %s",
                                                     kind, _short(exc, 120))
            return saved()
        _PAYRA_RECENT[kind] = (time.monotonic(), found)
        return dict(found)


def payra_invoice_note(config: Config, *, name: str, email: str = "", amount: str = "", product: str = "",
                       now=None) -> str:
    return payra_invoice_pick(config, name=name, email=email, amount=amount, product=product, now=now)[0]


def payra_invoice_pick(config: Config, *, name: str, email: str = "", amount: str = "", product: str = "",
                       now=None, taken=(), number: str = "", paid_at=None) -> tuple[str, str]:
    """The note on the Payra invoice behind a payment just posted - "can ryte
    access agents state for aged leads on summit pay invoice internal note".
    Payra read now, so the invoice paid a minute ago is there. Matched by
    email, else whole name, paid in the last two days, the total the payment
    was when there's a choice. "" when there's no note to read."""
    from .. import clearout, payraapi

    token = str(getattr(config.secrets, "payra_api_token", "") or "").strip()
    site = str(getattr(config.secrets, "payra_site_id", "") or "").strip()
    if not (token and site):
        return "", ""
    now = now or datetime.now(timezone.utc)
    taken = {str(one) for one in taken or () if one}
    # The last two days from Payra - current to the minute and shared - rather
    # than the saved copy alone, which the half-hourly read fills.
    data = {"invoices": payra_recent(config, "invoices", now=now)}
    email = str(email or "").strip().casefold()
    wanted = payraapi._cents(amount)
    found = []
    # The one the post names, when it names one: no guessing by name and
    # total.
    wanted_number = re.sub(r"[^0-9]", "", str(number or ""))
    if wanted_number:
        for held_as, invoice in (data.get("invoices") or {}).items():
            theirs = re.sub(r"[^0-9]", "", str(invoice.get("number") or ""))
            if theirs == wanted_number:
                return invoice_said(invoice), str(number).upper()
    # The invoice the payment itself was put against. Wiser Charles paid
    # $103.50 twice on Oct 16 - INV-20057, California, and INV-20058, eight
    # states - and both cards got the eight: the same total can't tell two
    # invoices apart, and Payra's payment record can.
    paid_against = _invoice_paid_against(config, name=name, amount=amount, paid_at=paid_at, now=now,
                                         invoices=data.get("invoices") or {}, taken=taken)
    if paid_against is not None:
        return invoice_said(paid_against[0]), paid_against[1]
    for held_as, invoice in (data.get("invoices") or {}).items():
        theirs = (email and invoice.get("email") == email) or clearout.name_match(name, invoice.get("name", "")) >= 2
        if not theirs or not invoice_said(invoice):
            continue
        # One invoice, one payment: Ahmed Ghazi paid INV-20039 (225 leads)
        # and INV-20040 (200) in one charge, and both cards read the 200.
        invoice_id = str(invoice.get("id") or invoice.get("number") or held_as)
        # Taken by its id, or by the number a payment post named it by.
        numbered = re.sub(r"[^0-9]", "", str(invoice.get("number") or ""))
        if invoice_id in taken or (numbered and any(re.fullmatch(r"INV-?0*" + numbered.lstrip("0"), one, re.IGNORECASE) for one in taken)):
            continue
        try:
            made = datetime.fromisoformat(str(invoice.get("created") or invoice.get("updated") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        made = made if made.tzinfo else made.replace(tzinfo=timezone.utc)
        if now - made > timedelta(days=2):
            continue
        # Not one for something else they bought: Ryan Leach's VET and IUL
        # invoices were both $103.50, and the VET payment got the IUL note.
        if product and _other_product(product, invoice_said(invoice)):
            continue
        total = payraapi._cents(invoice.get("total"))
        same = wanted is not None and total == wanted
        # Nearest the payment when none is exact: Payra posts what was put on
        # the invoice ($787.50), the total adds the dual-pricing fee ($815.06).
        off = abs(total - wanted) if wanted is not None and total is not None else float("inf")
        found.append((same, -off, made, invoice, invoice_id))
    if not found:
        return "", ""
    found.sort(key=lambda one: (one[0], one[1], one[2]), reverse=True)
    return invoice_said(found[0][3]), found[0][4]


#: How far a Payra payment's time can be from its post's and still be it.
PAYMENT_POST_GAP = timedelta(hours=3)


def _invoice_paid_against(config: Config, *, name: str, amount: str, paid_at, now, invoices: dict, taken):
    """(invoice, its id) for the Payra payment behind a post: theirs, the same
    amount, paid nearest the post's time, against an invoice nobody else has.
    None when the post's time isn't known or no payment says."""
    from .. import clearout, payraapi

    if paid_at is None or not name:
        return None
    paid_at = paid_at if paid_at.tzinfo else paid_at.replace(tzinfo=timezone.utc)
    try:
        payments = payra_recent(config, "payments", now=now)
    except Exception:
        return None
    wanted = payraapi._cents(amount)
    by_id = {str(one.get("id") or held_as): one for held_as, one in invoices.items()}
    by_number = {re.sub(r"[^0-9]", "", str(one.get("number") or "")): one for one in invoices.values()
                 if one.get("number")}
    found = []
    for payment in payments.values():
        if payment.get("refund") or clearout.name_match(name, payment.get("customer", "")) < 2:
            continue
        if wanted is not None and payraapi._cents(payment.get("amount")) != wanted:
            continue
        try:
            paid = datetime.fromisoformat(str(payment.get("paid_on") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        paid = paid if paid.tzinfo else paid.replace(tzinfo=timezone.utc)
        gap = abs(paid - paid_at)
        if gap > PAYMENT_POST_GAP:
            continue
        named = [by_id.get(str(one)) for one in [*(payment.get("invoice_ids") or []), payment.get("invoice_id")]
                 if one]
        number = re.sub(r"[^0-9]", "", str(payment.get("invoice_number") or ""))
        if number:
            named.append(by_number.get(number))
        for invoice in named:
            if invoice is None:
                continue
            invoice_id = str(invoice.get("id") or invoice.get("number") or "")
            numbered = re.sub(r"[^0-9]", "", str(invoice.get("number") or ""))
            if invoice_id in taken or (numbered and any(
                    re.fullmatch(r"INV-?0*" + numbered.lstrip("0"), one, re.IGNORECASE) for one in taken)):
                continue
            found.append((gap, invoice, invoice_id))
            break
    if not found:
        return None
    found.sort(key=lambda one: one[0])
    return found[0][1], found[0][2]


#: Payra's card fee - "Automatic Dual Pricing" - put on at the charge, not
#: on the invoice: Dawson Clevenger's $130, $56 and $84 invoices were
#: charged $279.45.
CARD_FEE = 0.035


def charge_covers(invoice_cents: int, charged_cents: int, *, many: int = 1) -> bool:
    """Whether invoices coming to `invoice_cents` are what was charged -
    as they are, or with the card fee on top."""
    slack = 2 * max(many, 1) + 1
    return (abs(invoice_cents - charged_cents) <= slack
            or abs(round(invoice_cents * (1 + CARD_FEE)) - charged_cents) <= slack)


def charge_left(charged_cents: int, invoice_cents: int) -> int:
    """What of a charge the invoices don't account for, the card fee aside."""
    if invoice_cents and charged_cents <= round(invoice_cents * (1 + CARD_FEE)) + 2:
        return 0
    return max(charged_cents - round(invoice_cents * (1 + CARD_FEE)), 0) if invoice_cents else charged_cents


#: How an invoice paid in the same charge as the post's own is put in the
#: order's notes - a Payra invoice note, marked as another one's.
SAME_CHARGE_NOTE = "Payra invoice note: (paid in the same charge"


def same_charge_note(note) -> bool:
    return str(note or "").startswith(SAME_CHARGE_NOTE)


def same_charge_said(invoice: dict) -> str:
    """An invoice paid in the same charge, as the order's notes put it."""
    total = invoice.get("total")
    money = f", ${float(total):,.2f}" if isinstance(total, (int, float)) else ""
    return (f"{SAME_CHARGE_NOTE} - {invoice.get('number') or 'another invoice'}{money})\n"
            + (invoice_said(invoice) or "(no note on it)"))


def payra_same_charge(config: Config, *, name: str, email: str = "", amount: str = "", number: str = "",
                      rest: int | None = None, paid_at=None, now=None, invoices=None, payments=None) -> list[dict]:
    """The other invoices one card charge paid besides the one its post
    names - Dawson Clevenger's $279.45 paid INV-20313, INV-20314 and
    INV-20315, and Nova posted only the first. The Payra payment's own
    invoices when it lists them; else theirs, from the last two days, whose
    totals come to the rest of the charge (`rest`, in cents). Slim invoices,
    oldest first; [] when none are found. `invoices` and `payments` to look
    in, by id, instead of Payra's last two days."""
    from itertools import combinations

    from .. import clearout, payraapi

    now = now or datetime.now(timezone.utc)
    invoices = payra_recent(config, "invoices", now=now) if invoices is None else invoices
    digits = lambda one: re.sub(r"[^0-9]", "", str(one or "")).lstrip("0")  # noqa: E731
    own = digits(number)
    by_id = {str(one.get("id") or held_as): one for held_as, one in invoices.items()}
    by_number = {digits(one.get("number")): one for one in invoices.values() if one.get("number")}
    email = str(email or "").strip().casefold()

    def order(found):
        return sorted({str(one.get("id") or one.get("number")): one for one in found}.values(),
                      key=lambda one: (digits(one.get("number")).zfill(12), str(one.get("created") or "")))

    wanted = payraapi._cents(amount)
    if paid_at is not None and name and wanted is not None:
        paid_at = paid_at if paid_at.tzinfo else paid_at.replace(tzinfo=timezone.utc)
        best = None
        payments = payra_recent(config, "payments", now=now) if payments is None else payments
        for payment in payments.values():
            if payment.get("refund") or clearout.name_match(name, payment.get("customer", "")) < 2:
                continue
            if payraapi._cents(payment.get("amount")) != wanted:
                continue
            try:
                paid = datetime.fromisoformat(str(payment.get("paid_on") or "").replace("Z", "+00:00"))
            except ValueError:
                continue
            paid = paid if paid.tzinfo else paid.replace(tzinfo=timezone.utc)
            if abs(paid - paid_at) <= PAYMENT_POST_GAP and (best is None or abs(paid - paid_at) < best[0]):
                best = (abs(paid - paid_at), payment)
        if best is not None:
            payment = best[1]
            named = [by_id.get(str(one)) for one in [*(payment.get("invoice_ids") or []), payment.get("invoice_id")]
                     if one]
            named.append(by_number.get(digits(payment.get("invoice_number"))))
            found = [one for one in named if one is not None and digits(one.get("number")) != own]
            if found:
                return order(found)
    if not rest or rest <= 0:
        return []
    # Not listed on the payment: theirs, made in the last two days, whose
    # totals add up to what the charge has left over.
    theirs = []
    for one in invoices.values():
        if digits(one.get("number")) == own or not one.get("active", True):
            continue
        if not ((email and one.get("email") == email) or clearout.name_match(name, one.get("name", "")) >= 2):
            continue
        try:
            made = datetime.fromisoformat(str(one.get("created") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        made = made if made.tzinfo else made.replace(tzinfo=timezone.utc)
        # Made in the two days before it was paid.
        paid = paid_at or now
        paid = paid if paid.tzinfo else paid.replace(tzinfo=timezone.utc)
        if timedelta(hours=-1) <= paid - made <= timedelta(days=2) and payraapi._cents(one.get("total")):
            theirs.append(one)
    theirs = order(theirs)[-8:]
    for size in range(1, len(theirs) + 1):
        for some in combinations(theirs, size):
            if charge_covers(sum(payraapi._cents(one.get("total")) for one in some), rest, many=size):
                return list(some)
    return []


def payra_kept(config: Config, kind: str, *, now=None) -> dict:
    """Payra's `kind` as far back as the saved copy goes, with the last two
    days read now over it. By id, slim."""
    from .. import payraapi

    try:
        kept = dict(payraapi.load().get(kind) or {})
    except Exception:
        kept = {}
    try:
        kept.update(payra_recent(config, kind, now=now))
    except Exception:
        logging.getLogger("wilbyte.bot").warning("Couldn't read Payra's %s now - the saved copy only", kind)
    return kept


def charges_of_several(config: Config, *, since, posts=(), now=None) -> list[dict]:
    """Every card charge since then that paid more than one invoice - "check
    for other missed ones" after Dawson Clevenger's. From Payra's payments
    that list two invoices or more, and from payment posts charged more than
    the invoice they show (`posts`: {"name", "email", "amount", "number",
    "total", "paid_at", "link"}, totals in cents). [{"name", "amount",
    "paid_at", "link", "invoices": [slim], "unplaced": cents}], newest last."""
    from .. import payraapi

    now = now or datetime.now(timezone.utc)
    since = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    invoices = payra_kept(config, "invoices", now=now)
    payments = payra_kept(config, "payments", now=now)
    digits = lambda one: re.sub(r"[^0-9]", "", str(one or "")).lstrip("0")  # noqa: E731
    by_id = {str(one.get("id") or held_as): one for held_as, one in invoices.items()}
    by_number = {digits(one.get("number")): one for one in invoices.values() if one.get("number")}
    found, seen = [], set()

    def when(stamp):
        try:
            got = datetime.fromisoformat(str(stamp or "").replace("Z", "+00:00"))
        except ValueError:
            return None
        return got if got.tzinfo else got.replace(tzinfo=timezone.utc)

    def add(entry):
        key = tuple(sorted(digits(one.get("number")) or str(one.get("id")) for one in entry["invoices"]))
        if key in seen and not entry.get("unplaced"):
            return
        seen.add(key)
        found.append(entry)

    for payment in payments.values():
        paid = when(payment.get("paid_on"))
        if payment.get("refund") or paid is None or paid < since:
            continue
        named = [by_id.get(str(one)) for one in [*(payment.get("invoice_ids") or []), payment.get("invoice_id")] if one]
        named.append(by_number.get(digits(payment.get("invoice_number"))))
        named = list({str(one.get("id") or one.get("number")): one for one in named if one is not None}.values())
        # Paid together only when they come to the charge: Gavin Casella's
        # $1,035 ad spend named two subscription invoices of $1,000 and $1,050.
        together = sum(payraapi._cents(one.get("total")) or 0 for one in named)
        if len(named) >= 2 and charge_covers(together, payraapi._cents(payment.get("amount")) or 0, many=len(named)):
            add({"name": str(payment.get("customer") or named[0].get("name") or ""), "amount": payment.get("amount"),
                 "paid_at": paid, "link": "", "unplaced": 0,
                 "invoices": sorted(named, key=lambda one: digits(one.get("number")).zfill(12))})
    for post in posts or ():
        charged, total = post.get("amount_cents"), post.get("total")
        if charged is None or total is None or charged - total <= 100:
            continue
        others = payra_same_charge(config, name=post.get("name", ""), email=post.get("email", ""),
                                   amount=post.get("amount", ""), number=post.get("number", ""),
                                   rest=charged - total, paid_at=post.get("paid_at"), now=now,
                                   invoices=invoices, payments=payments)
        own = by_number.get(digits(post.get("number")))
        covered = sum(payraapi._cents(one.get("total")) or 0 for one in others)
        add({"name": post.get("name", ""), "amount": charged / 100, "paid_at": post.get("paid_at"),
             "link": post.get("link", ""), "invoices": ([own] if own else []) + others,
             "unplaced": charge_left(charged - total, covered)})
    found.sort(key=lambda one: one.get("paid_at") or since)
    return found


def agency_from_earlier_cards(config: Config, *, name: str = "", email: str = "") -> tuple[str, str] | None:
    """(agency, the card it's on) off this client's newest earlier card that
    names one - Jacob Martin's aged re-order went out with no Agency. Their
    email on the card first, else their whole name in its title. None when
    no earlier card says."""
    from .. import agents as rules, clearout

    email = str(email or "").strip().casefold()
    client = open_trello(config)
    try:
        every = client.board_cards(config.secrets.trello_board_id, archived=True)
    finally:
        client.close()
    found = []
    for card in every:
        title = str(card.get("name") or "")
        if not rules.is_client_card(title):
            continue
        desc = str(card.get("desc") or "")
        theirs = (email and email in desc.casefold()) or (
            name and clearout.name_match(name, rules.agent_name(title)) >= 2)
        if not theirs:
            continue
        said = re.search(r"^\W*Agency\W*:[ \t]*(\S[^\n]*)$", desc, re.MULTILINE | re.IGNORECASE)
        agency = said.group(1).strip(" *") if said else ""
        # Not another client's name: Garret Sekelsky's card had "Agency:
        # Bailey Lawrence" - one of his agents - and it went on the next.
        if agency and agency_usable(agency) and not any(
                clearout.name_match(agency, rules.agent_name(str(other.get("name") or ""))) >= 2
                for other in every if rules.is_client_card(str(other.get("name") or ""))):
            found.append((rules.made_at(str(card.get("id") or "")), agency, title))
    if not found:
        return None
    found.sort(key=lambda one: one[0] or datetime.min.replace(tzinfo=timezone.utc))
    return found[-1][1], found[-1][2]


def invoice_line_count(config: Config, number: str) -> tuple[int, str] | None:
    """(how many leads, the line) off an invoice's one aged line - its
    quantity. None when the invoice isn't found, has several lines for
    leads, or a quantity that isn't a count (1 package)."""
    from .. import payraapi

    digits = re.sub(r"[^0-9]", "", str(number or "")).lstrip("0")
    if not digits:
        return None
    invoice = next((one for one in payra_kept(config, "invoices").values()
                    if re.sub(r"[^0-9]", "", str(one.get("number") or "")).lstrip("0") == digits), None)
    lines = [one for one in (invoice or {}).get("lines") or [] if isinstance(one, dict)]
    leads = [one for one in lines if re.search(r"\blead", str(payraapi._first(one, "name", "description", "item",
                                                                               "product", "title") or ""), re.I)]
    if len(leads) != 1:
        return None
    many = payraapi._first(leads[0], "quantity", "qty", "count")
    try:
        count = int(float(str(many)))
    except (TypeError, ValueError):
        return None
    if count <= 1 or float(str(many)) != count:
        return None
    return count, payraapi.line_text(leads[0])


def invoice_on_a_card(invoice: dict, client_name: str, cards: list[dict]) -> dict | None:
    """The board card an invoice's order is on - theirs, with its count and
    (aged) its age written on it - or None. `cards` with their "desc"."""
    from .. import clearout

    form = invoice_form(invoice_said(invoice) or "")
    count = re.search(r"\d[\d,]*", str(form.get("leads") or "")) or re.search(
        r"×\s*(\d[\d,]*)", invoice_said(invoice) or "")
    count = count.group(count.lastindex or 0) if count else ""
    bucket = _AGE_BUCKET.search(str(form.get("package") or ""))
    for card in cards:
        who = re.split(r"\s+[-–—]\s+", str(card.get("name") or ""), maxsplit=1)[-1]
        if clearout.name_match(client_name, who) < 2:
            continue
        desc = str(card.get("desc") or "")
        if count and not re.search(rf"(?<![\d,]){re.escape(count)}(?![\d,])", desc):
            continue
        flat = lambda said: re.sub(r"\s+", "", str(said)).replace("–", "-")  # noqa: E731
        if bucket and flat(bucket.group(0)) not in flat(desc):
            continue
        return card
    return None


def _other_product(paid_for: str, invoice: str) -> bool:
    """Whether an invoice is for a different lead type than the payment - by
    what it says was bought (its "Lead Type:" line, else its note), never by
    Payra's line blurb: "The leads may include people interested in Indexed
    Universal Life, Final Expense" read Frank Nocera's FEX invoice as IUL,
    and his aged card was never made."""
    from .. import setupcheck

    if re.match(r"^[—\-–(]", paid_for.strip()):
        return False
    lines = [line.strip() for line in str(invoice or "").splitlines() if line.strip()]
    said = [line for line in lines if re.match(r"(?:payra invoice note:\s*)?lead\s*type", line, re.IGNORECASE)]
    said = said or [line for line in lines if not re.match(r"(?:lines|description)\s*:", line, re.IGNORECASE)]
    if not said:
        return False
    text = "\n".join(said)
    # Aged against new-agent leads, whatever the family: Riley Noah's $1,987.20
    # Text-Verified Trucker IUL was read off his "Aged IUL - 30-90 days"
    # invoice from the day before - IUL both, and nothing alike.
    aged_paid = bool(re.search(r"\baged\b", paid_for, re.IGNORECASE))
    if not aged_paid and re.search(r"\baged\b", text, re.IGNORECASE):
        return True
    if aged_paid and re.search(r"\bfresh\b|text[\s-]*verified|\botp\b", text, re.IGNORECASE) \
            and not re.search(r"\baged\b", text, re.IGNORECASE):
        return True
    return setupcheck.same_product(paid_for, text) is False and setupcheck.same_product(text, paid_for) is False


def invoice_said(invoice: dict) -> str:
    """What an invoice says about the order: its note, its description and
    its lines - "the information is on invoice" (Blaze Lubbers)."""
    from .. import payraapi

    note = str(invoice.get("notes") or "").strip()
    about = str(invoice.get("description") or "").strip()
    lines = [payraapi.line_text(one) for one in invoice.get("lines") or []]
    lines = [one for one in lines if one.strip()]
    if not (note or about):
        return ""
    return "\n".join(bit for bit in (
        note, f"Description: {about}" if about and about not in note else "",
        "Lines: " + "; ".join(lines) if lines else "") if bit)



def payra_invoices_since(config: Config, since) -> list[dict]:
    """Every Payra invoice changed since then, slim."""
    from .. import payraapi

    token = str(getattr(config.secrets, "payra_api_token", "") or "").strip()
    site = str(getattr(config.secrets, "payra_site_id", "") or "").strip()
    if not (token and site):
        raise RuntimeError("No Payra token or site in .env, so the invoices can't be read.")
    found = []
    with payraapi.PayraClient(token, site, timeout=60.0) as client:
        after = payraapi.when(since)
        for _page in range(30):
            got = client.changed("invoices", after)
            found += [payraapi.slim_invoice(record) for record in got.get("records") or []]
            onward = str(got.get("next_updated_after") or "")
            if not got.get("records") or not onward or onward == after:
                break
            after = onward
    return found


def _one_line_wrong(said: str, desc: str, *, counts_ok=()) -> tuple[list[str], str]:
    """A one-line aged order's invoice form against its card: (what's wrong,
    what it should be). Nothing to compare - no form on the invoice, or
    states it doesn't name ("same as before") - is nothing wrong."""
    from .. import ordercards

    form = invoice_form(said)
    if not form:
        return [], ""

    def field(label):
        found = re.search(rf"^\W*{label}[^:\n]*:\s*(.*)$", desc, re.MULTILINE | re.IGNORECASE)
        return " ".join(found.group(1).split()) if found else ""

    states, package, leads = field("States"), field("Lead Type"), field("Number of Leads")
    wrong, should = [], []
    asked = form.get("states", "")
    if asked:
        written = _form_states(asked)
        should.append(written)
        split = _states_split(asked, form.get("leads", ""))
        if ordercards._ALL_BUT.search(written):
            left_out = set(_state_codes_named(ordercards._ALL_BUT.split(written, maxsplit=1)[-1]))
            card_out = set(_state_codes_named(ordercards._ALL_BUT.split(states, maxsplit=1)[-1])) \
                if ordercards._ALL_BUT.search(states) else set()
            if left_out != card_out:
                wrong.append(f"the invoice says {written}, the card doesn't leave the same states out")
        elif not re.search(r"\b(?:all|every|nation\s*-?\s*wide)\b", written, re.IGNORECASE):
            want = set(_state_codes_named(written))
            have = set(_state_codes_named(states))
            if want and want != have:
                wrong.append(f"the states aren't the invoice's ({', '.join(sorted(want))})")
            elif split and not re.search(r"\d", states):
                should[-1] = split
                wrong.append(f"the invoice splits the states ({split}) and the card doesn't")
    # The count, when the invoice gives one count for one line.
    counts = re.findall(r"^\W*number\s*of\s*(?:requested\s*)?leads[^:\n]*:\s*(\d[\d,]*)", said,
                        re.IGNORECASE | re.MULTILINE)
    have_count = re.search(r"\d[\d,]*", leads)
    if (len(counts) == 1 and have_count and int(counts[0].replace(",", "")) != int(have_count.group(0).replace(",", ""))
            and int(have_count.group(0).replace(",", "")) not in set(counts_ok)):
        wrong.append(f"the invoice is for {counts[0]} leads, the card {have_count.group(0)}")
    if len(counts) == 1:
        should.append(f"{counts[0]} leads")
    # The age range: 180+ bought, 30-90 on the card is somebody else's leads.
    want_age = _AGE_BUCKET.search(form.get("package", ""))
    have_age = _AGE_BUCKET.search(package)
    if form.get("package"):
        should.append(form["package"])
    if want_age and have_age and re.sub(r"\s", "", want_age.group(0)) != re.sub(r"\s", "", have_age.group(0)):
        wrong.append(f"the invoice is for {want_age.group(0)} days, the card says {have_age.group(0)}")
    return wrong, " · ".join(one for one in should if one)


#: Aged cards Franklin has looked at and called right - "this was done" -
#: so the check doesn't name them again.
AGED_OK_PATH = None


def _aged_ok_path() -> Path:
    from ..state import _state_dir

    return AGED_OK_PATH or (_state_dir() / "aged-lines-ok.json")


def aged_lines_ok() -> set:
    try:
        held = json.loads(_aged_ok_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {str(one) for one in held} if isinstance(held, list) else set()


def aged_lines_mark_ok(text: str) -> list[str]:
    """The Trello cards linked in `text` remembered as checked. Their codes."""
    codes = list(dict.fromkeys(re.findall(r"trello\.com/c/([A-Za-z0-9]{6,12})", text or "")))
    if codes:
        where = _aged_ok_path()
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(json.dumps(sorted(aged_lines_ok() | set(codes))), encoding="utf-8")
    return codes


def check_aged_lines(config: Config, *, days: int = 7, now=None, invoices=None) -> tuple[list[str], int]:
    """"@Ryte check aged lines" - every Aged Leads card from the last `days`,
    archived ones too, against its Payra invoice: an invoice that gives each
    line its own states, and a card that has them as one list, or not line
    for line, or a count the invoice doesn't have. Mateo Lyon's Vets 90-180
    were Texas only and went out with nineteen states. A one-line invoice:
    its states, split ("80 Louisiana, 20 Texas"), count and age. Reads, writes
    nothing. (what's wrong, card by card; how many cards were looked at)."""
    from .. import clearout, ordercards

    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    if invoices is None:
        invoices = payra_invoices_since(config, since - timedelta(days=3))
    board = open_trello(config)
    try:
        cards = board.board_cards(config.secrets.trello_board_id, archived=True)
    finally:
        board.close()

    def made_at(item, key):
        try:
            when = datetime.fromisoformat(str(item.get(key) or "").replace("Z", "+00:00"))
        except ValueError:
            return None
        return when if when.tzinfo else when.replace(tzinfo=timezone.utc)

    flagged, looked = [], 0
    checked = aged_lines_ok()
    for card in cards or []:
        title = " ".join(str(card.get("name") or "").split())
        if not re.match(r"\s*aged\s+leads?\b", title, re.IGNORECASE):
            continue
        code = (re.search(r"/c/([A-Za-z0-9]+)", str(card.get("shortUrl") or card.get("url") or "")) or [None, ""])[1]
        if checked & {code, str(card.get("id") or "")} - {""}:
            continue
        try:
            made = datetime.fromtimestamp(int(str(card.get("id") or "")[:8], 16), tz=timezone.utc)
        except ValueError:
            continue
        if made < since:
            continue
        looked += 1
        desc = str(card.get("desc") or "")
        client = title.split("-", 1)[-1].strip()
        email = (re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", desc) or [None])[0] or ""
        states = (re.search(r"^\W*States\W*:\s*(.*)$", desc, re.MULTILINE | re.IGNORECASE) or [None, ""])[1]
        package = (re.search(r"^\W*Lead Type[^:\n]*:\s*(.*)$", desc, re.MULTILINE | re.IGNORECASE) or [None, ""])[1]
        # Every invoice of theirs from around then, nearest the card first; the
        # card is right if it's right for any of them. Patrick Moreno had more
        # than one order that week, and his card was held up against the wrong
        # one.
        near = []
        for invoice in invoices:
            theirs = (email and str(invoice.get("email") or "").casefold() == email.casefold()) or \
                clearout.name_match(client, str(invoice.get("name") or "")) >= 2
            when = made_at(invoice, "created")
            if theirs and when is not None and made - timedelta(days=3) <= when <= made + timedelta(days=1):
                near.append((abs((made - when).total_seconds()), invoice))
        first_wrong = None
        # A count the client's invoices come to together is right too -
        # Preston Pennington's 200 on two invoices of 100.
        their_counts = [int(one.replace(",", "")) for _gap, invoice in near
                        for one in re.findall(r"^\W*number\s*of\s*(?:requested\s*)?leads[^:\n]*:\s*(\d[\d,]*)",
                                              invoice_said(invoice), re.IGNORECASE | re.MULTILINE)]
        counts_ok = set(their_counts) | ({sum(their_counts)} if len(their_counts) > 1 else set())
        card_families = _families(package + " " + title)
        for _gap, invoice in sorted(near, key=lambda one: one[0]):
            said = invoice_said(invoice)
            lines = ordercards._lines_with_states(said)
            # The aged lines only: Patrick Moreno's 34 OTP Vets and 25 OTP IULs
            # are on New Agent cards of their own, and flagged his Aged card.
            if any(re.search(r"\baged\b", kind, re.IGNORECASE) for kind, _codes in lines):
                lines = [(kind, codes) for kind, codes in lines if re.search(r"\baged\b", kind, re.IGNORECASE)]
            if len(lines) < 2 or len({frozenset(codes) for _kind, codes in lines}) < 2:
                # One line: its states, split, count and age against the card
                # - "make sure ryte dont miss anything". Torian Lollis's "80
                # Louisiana, 20 Texas" and Mackenzie Ho's "all but new york"
                # weren't looked at, being one line each. Their New Agent
                # invoice from the same week isn't this card's.
                if not re.search(r"\baged\b", said, re.IGNORECASE):
                    continue
                # Their Vets card against their FEX invoice is another order's
                # - Shyanne Tower's second card was held up against her first.
                form_families = _families(invoice_form(said).get("package", ""))
                if card_families and form_families and not card_families & form_families:
                    continue
                wrong, should = _one_line_wrong(said, desc, counts_ok=counts_ok)
                if not wrong:
                    first_wrong = None
                    break
                if first_wrong is None:
                    number = invoice.get("number") or invoice.get("id")
                    first_wrong = (f"**{title}** <{card.get('shortUrl') or card.get('url') or ''}> — {number}: "
                                   + "; ".join(wrong) + f"\n   Should be: {should}"
                                   + f"\n   Card has: {' '.join(states.split()) or '(no states)'} · "
                                   + f"{' '.join(package.split()) or '(no lead type)'}")
                continue
            wrong = []
            if ";" not in states:
                wrong.append("its lines' states are one list on the card")
            else:
                on_card = sorted(sorted(ordercards._codes(bit)) for bit in states.split(";") if bit.strip())
                if on_card != sorted(sorted(codes) for _kind, codes in lines):
                    wrong.append("the states on the card aren't the invoice's, line for line")
            numbers = {int(one.replace(",", "")) for one in re.findall(r"\d[\d,]*", said)}
            numbers |= {int(float(str(line.get("qty") or line.get("quantity") or 0)))
                        for line in invoice.get("lines") or [] if isinstance(line, dict)
                        if str(line.get("qty") or line.get("quantity") or "").replace(".", "", 1).isdigit()}
            for each in ordercards._COUNT_OF.finditer(package):
                if int(each.group(1)) not in numbers:
                    wrong.append(f"{each.group(1)} in the lead type isn't on the invoice")
            if not wrong:
                first_wrong = None
                break
            if first_wrong is None:
                number = invoice.get("number") or invoice.get("id")
                first_wrong = (
                    f"**{title}** <{card.get('shortUrl') or card.get('url') or ''}> — {number}: " + "; ".join(wrong)
                    + "\n   Should be: " + " · ".join(f"{kind}: {' '.join(codes)}" for kind, codes in lines)
                    + f"\n   Card has: {' '.join(states.split()) or '(no states)'}")
        if first_wrong:
            flagged.append(first_wrong)
    return flagged, looked


def payra_invoice_note_fields(config: Config, name: str, *, now=None) -> list[tuple[str, list]]:
    """"@Ryte invoice note Aiden Larsen" - each of their invoices from the
    last week, newest first, with every note-like field on it as Payra has it.
    [(invoice number and total, [(field, text)])]."""
    from .. import clearout, payraapi

    token = str(getattr(config.secrets, "payra_api_token", "") or "").strip()
    site = str(getattr(config.secrets, "payra_site_id", "") or "").strip()
    now = now or datetime.now(timezone.utc)
    found = []
    with payraapi.PayraClient(token, site, timeout=60.0) as client:
        after = payraapi.when(now - timedelta(days=7))
        for _page in range(10):
            got = client.changed("invoices", after)
            for record in got.get("records") or []:
                one = payraapi.slim_invoice(record)
                if clearout.name_match(name, one["name"]) >= 2:
                    found.append((one["created"], f"#{one['number'] or one['id']} · ${one['total']}",
                                  payraapi.note_fields(record)))
            onward = str(got.get("next_updated_after") or "")
            if not got.get("records") or not onward or onward == after:
                break
            after = onward
    found.sort(reverse=True)
    return [(label, fields) for _when, label, fields in found[:5]]


_MONTH_DAY = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b|\b(\d{1,2})/(\d{1,2})\b",
                        re.IGNORECASE)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def _month_day(said: str):
    """(month, day) out of "Friday, October 16" or "10/16" - or None."""
    found = _MONTH_DAY.search(str(said or ""))
    if not found:
        return None
    if found.group(1):
        return _MONTHS.index(found.group(1)[:3].casefold()) + 1, int(found.group(2))
    return int(found.group(3)), int(found.group(4))


def _desc_field(desc: str, *labels: str) -> str:
    for label in labels:
        found = re.search(rf"^\s*{label}\s*:[ \t]*(.*)$", str(desc or ""), re.IGNORECASE | re.MULTILINE)
        if found and found.group(1).strip():
            return found.group(1).strip()
    return ""


def same_order(config: Config, existing: dict, card: dict) -> bool:
    """Whether another payment is for the order that already has this card -
    "same person different orders different live dates" are not. The same
    lead type and the same launch date, as far as either says: a payment
    with nothing new said about it goes on the card it belongs to."""
    from .. import setupcheck

    client = open_trello(config)
    try:
        desc = str(client.card_detail(existing["id"]).get("desc") or "")
    finally:
        client.close()
    fields = card.get("fields") or {}
    had_type = _desc_field(desc, r"Lead Type/Package Selected", r"Lead Type")
    # What the payment post says was bought, as well as what was read: Ryan
    # Leach's VET payment was read off his IUL invoice and went on the IUL
    # card as "another payment for this order".
    paid_for = re.search(r"PRODUCT:\**\s*([^\n]*)", str(card.get("source") or ""), re.IGNORECASE)
    paid_for = " ".join(paid_for.group(1).split()) if paid_for else ""
    for said in (fields.get("package") or "", paid_for):
        if had_type and said and setupcheck.same_product(said, had_type) is False \
                and setupcheck.same_product(had_type, said) is False:
            return False
    had_launch = _month_day(_desc_field(desc, r"Launch Date"))
    new_launch = _month_day(fields.get("launch") or "") or _month_day(card.get("launch_iso", "")[5:].replace("-", "/"))
    if had_launch and new_launch and had_launch != new_launch:
        return False
    return True



# ------------------------------------------------ paid by bank, not settled

#: How Payra names a bank payment - ACH, e-check, a checking account. A card
#: is Visa, Mastercard, Amex, Discover.
_BANK_PAID = re.compile(r"(?<![a-z])(?:ach|bank\w*|e-?check|checking|savings|eft)(?![a-z])", re.IGNORECASE)
#: A bank payment's money in: "captured doesnt mean settled".
_SETTLED = re.compile(r"\b(?:settled|completed?|succeeded|deposited|funded)\b", re.IGNORECASE)
#: A bank payment that won't arrive.
_UNPAID = re.compile(r"fail|declin|void|error|cancel|reject|return|refund|chargeback|reversed", re.IGNORECASE)


def payment_by_bank(payment: dict | None) -> bool:
    if not payment:
        return False
    return bool(_BANK_PAID.search(f"{payment.get('how', '')} {payment.get('kind', '')}"))


def payment_settled(payment: dict | None) -> bool:
    return bool(payment) and bool(_SETTLED.search(str(payment.get("status") or "")))


def payment_failed(payment: dict | None) -> bool:
    return bool(payment) and (bool(payment.get("refund"))
                              or bool(_UNPAID.search(str(payment.get("status") or ""))))


def payra_payment_for(config: Config, *, name: str, amount: str = "", now=None) -> dict | None:
    """The Payra payment behind a payment post just made: theirs by whole
    name, in the last two days, the same amount when there's one - Luke
    Mertz's $200 by bank was voided and his $207 by card went through, the
    same invoice both times. Payra read now; the saved copy if it can't be."""
    from .. import clearout, payraapi

    token = str(getattr(config.secrets, "payra_api_token", "") or "").strip()
    site = str(getattr(config.secrets, "payra_site_id", "") or "").strip()
    if not (token and site and name):
        return None
    now = now or datetime.now(timezone.utc)
    wanted = payraapi._cents(amount)

    def theirs(payments):
        found = []
        for payment in payments.values():
            if payment.get("refund") or clearout.name_match(name, payment.get("customer", "")) < 2:
                continue
            try:
                paid = datetime.fromisoformat(str(payment.get("paid_on") or "").replace("Z", "+00:00"))
            except ValueError:
                continue
            paid = paid if paid.tzinfo else paid.replace(tzinfo=timezone.utc)
            if now - paid > timedelta(days=2):
                continue
            same = wanted is not None and payraapi._cents(payment.get("amount")) == wanted
            found.append((same, paid, payment))
        if wanted is not None and any(one[0] for one in found):
            found = [one for one in found if one[0]]
        if not found:
            return None
        found.sort(key=lambda one: one[1], reverse=True)
        return found[0][2]

    payments = payra_recent(config, "payments", now=now)
    found = theirs(payments)
    if found is None:
        # Not in the shared copy - read up to 90 seconds before they paid.
        # Remi Sampson's card payment was in Payra, and the card said it
        # couldn't be found. The last few hours, read now.
        fresh = payra_since(config, "payments", now - PAYRA_JUST_PAID)
        found = theirs(fresh)
        if found is None:
            logging.getLogger("wilbyte.bot").info(
                "No Payra payment for %s (%s) among %d recent and %d just read", name, amount or "any amount",
                len(payments), len(fresh))
    return found


def payra_since(config: Config, kind: str, since: datetime) -> dict:
    """Payra's `kind` changed since `since`, read now - not the shared copy.
    {} when it can't be read."""
    from .. import payraapi

    token = str(getattr(config.secrets, "payra_api_token", "") or "").strip()
    site = str(getattr(config.secrets, "payra_site_id", "") or "").strip()
    if not (token and site):
        return {}
    found = {}
    try:
        with payraapi.PayraClient(token, site, timeout=60.0) as client:
            after = payraapi.when(since)
            for _page in range(PAYRA_RECENT_PAGES):
                got = client.changed(kind, after)
                for record in got.get("records") or []:
                    one = payraapi.SLIM[kind](record)
                    if one["id"]:
                        found[one["id"]] = one
                onward = str(got.get("next_updated_after") or "")
                if not got.get("records") or not onward or onward == after:
                    break
                after = onward
    except Exception as exc:
        logging.getLogger("wilbyte.bot").warning("Couldn't read Payra's %s just now: %s", kind, _short(exc, 120))
    return found


def payra_payment_now(config: Config, payment_id: str) -> dict | None:
    """One payment as Payra has it now - for whether it's settled. Asked for
    by its id; else as the half-hourly read last saw it, which picks up a
    status changing because the record's updated."""
    from .. import payraapi

    token = str(getattr(config.secrets, "payra_api_token", "") or "").strip()
    site = str(getattr(config.secrets, "payra_site_id", "") or "").strip()
    try:
        with payraapi.PayraClient(token, site, timeout=60.0) as client:
            record = client.one("payment", payment_id)
        if record:
            return payraapi.slim_payment(record)
    except Exception:
        logging.getLogger("wilbyte.bot").info("Couldn't read payment %s by its id", payment_id, exc_info=True)
    return (payraapi.load().get("payments") or {}).get(payment_id)


def order_bank_comment(config: Config, card_id: str, text: str) -> None:
    client = open_trello(config)
    try:
        client.add_comment(card_id, text)
    finally:
        client.close()


def order_bank_settled(config: Config, card_id: str, text: str) -> str:
    """A held aged card, its bank payment settled: moved from Franklin
    (Admin) to Aged Leads Order Done - only from there, so a card somebody
    has moved on already isn't pulled back - with `text` as a comment.
    "moved", or "left" when it wasn't in Franklin (Admin)."""
    from .. import agents, ordercards, trello

    client = open_trello(config)
    try:
        lists = client.board_lists(config.secrets.trello_board_id)
        parked = trello.find_list(lists, agents.PARKED)
        aged = ordercards.list_for("aged", lists, wanted=config.secrets.order_aged_list or "")
        if aged is None:
            raise ValueError("There's no Aged Leads Order Done list on the board.")
        here = str(client.card_full(card_id).get("idList") or "")
        moved = parked is not None and here == str(parked.get("id") or "")
        if moved:
            client.move_card(card_id, str(aged["id"]), position="bottom")
        client.add_comment(card_id, text if moved else text.split(" — ", 1)[0] + ".")
        return "moved" if moved else "left"
    finally:
        client.close()


_STATES_LINE = re.compile(r"^[ \t]*States[ \t]*:[ \t]*(.*)$", re.IGNORECASE | re.MULTILINE)


def card_states(desc: str) -> str:
    """The States line of a card's description, as it is now."""
    found = _STATES_LINE.search(str(desc or ""))
    return " ".join(found.group(1).split()) if found else ""


def states_recheck(source: str, cards: list[dict]) -> list[str]:
    """What's wrong with the states on an order's cards, held up against what
    its post and thread say - "were losing moneyyyyyyyy because of this":
    states the order leaves out sent as the ones ordered (Shahan Uddin's "no
    ny ak no ha" as "NY, AK"), and an aged order in packets sent as one list.
    `cards`: {"name", "kind", "states", "url"}. A line each."""
    from .. import ordercards

    found = []
    packets = ordercards.PACKETS.findall(source)
    in_packets = len(packets) >= 2 or any(ordercards._count_word(one) >= 2 for one in packets)
    for card in cards:
        states = str(card.get("states") or "")
        wrong = []
        for bit in states.split(";"):
            if not ordercards._ALL_BUT.search(bit):
                wrong += ordercards._ordered_but_left_out(ordercards._codes(bit.rsplit(":", 1)[-1]), source)
        if card.get("kind") == "aged" and in_packets and ";" not in states:
            wrong.append("the order is in packets with their own states, and the card has one list")
        if wrong:
            found.append(f"⚠ **{card.get('name')}** — States: {states or '(none)'} — " + "; ".join(
                dict.fromkeys(one.replace("States: ", "") for one in wrong)) + f" {card.get('url') or ''}".rstrip())
    return found


#: A line of a Payra invoice, as Nova writes it in the post's notes: "Fresh
#: Vets: 15 @ $40.00, 15% off — states LA, OK, KS".
_INVOICE_LINE = re.compile(
    r"^[\s>*•-]*(?P<package>[A-Za-z][A-Za-z0-9 +&/().'-]{1,60}?)\s*:\s*(?P<count>\d[\d,]*)\s*(?:x|@)\s*\$?[\d,.]+"
    r"[^\n—–]*?[—–-]\s*states?\s*:?\s*(?P<states>[^\n]+)$", re.IGNORECASE | re.MULTILINE)


def order_lines(text: str) -> list[dict]:
    """The invoice's lines with their own states, when it has two or more
    that differ - Colby Whittaker's three "Fresh Vets: 15 @ $40.00 — states
    ..." with three different lists. {"package", "leads", "states", "codes"}.
    [] when there's one, or they all say the same."""
    lines = []
    for found in _INVOICE_LINE.finditer(str(text or "")):
        codes = _state_codes_named(found.group("states"))
        if not codes:
            continue
        lines.append({"package": " ".join(found.group("package").split()),
                      "leads": found.group("count").replace(",", ""), "states": ", ".join(codes), "codes": codes})
    if len(lines) < 2 or len({frozenset(one["codes"]) for one in lines}) < 2:
        return []
    return lines


def _states_alike(one, other) -> float:
    one, other = set(one), set(other)
    return len(one & other) / len(one | other) if one and other else 0.0


#: How alike a line's states have to be to an agent's card to be theirs.
LINE_MATCH = 0.85


def lines_to_agents(lines: list[dict], earlier: list[dict], payer: str) -> list[dict]:
    """Each line of an order for several agents, matched to the agent whose
    earlier card has its states - Colby Whittaker's lines carry no names, but
    each is one of the agents he pays for. `earlier`: New Agent cards, {"name",
    "desc"}, newest last. As agents for `agent_cards`, or [] unless every line
    is one agent's, and a different agent's each."""
    from .. import ordercards

    payer = " ".join(str(payer or "").split())
    if not payer:
        return []
    theirs = {}
    for card in earlier:
        desc = str(card.get("desc") or "")
        name = re.sub(r"^\s*new\s*agent\s*[-–—:]\s*", "", str(card.get("name") or ""), flags=re.IGNORECASE).strip()
        if not name or not (ordercards.same_person(name, payer)
                            or re.search(rf"paid\s+by\s+{re.escape(payer)}\b", desc, re.IGNORECASE)):
            continue
        codes = _state_codes_named(card_states(desc))
        if not codes:
            continue
        field = lambda label: (re.search(rf"^[ \t]*{label}[ \t]*:[ \t]*(\S[^\n]*)$", desc, re.IGNORECASE | re.MULTILINE)
                               or [None, ""])[1].strip()
        theirs[name.casefold()] = {"name": name, "email": field(r"e-?mail"), "phone": field(r"phone(?:\s*number)?"),
                                   "codes": codes}
    used, agents = set(), []
    for line in lines:
        scored = sorted(((_states_alike(line["codes"], one["codes"]), key) for key, one in theirs.items()
                         if key not in used), reverse=True)
        if not scored or scored[0][0] < LINE_MATCH or (len(scored) > 1 and scored[1][0] == scored[0][0]):
            return []
        key = scored[0][1]
        used.add(key)
        one = theirs[key]
        agents.append({"name": one["name"], "email": one["email"], "phone": one["phone"], "states": line["states"],
                       "order": f"{line['leads']} {line['package']}", "webhook": "",
                       "matched_on": f"{scored[0][0]:.0%} of the states on their last card"})
    return agents


def payer_agent_cards(config: Config, payer: str, *, days: int = 180) -> list[dict]:
    """The New Agent cards of the last `days` that are the payer's own or say
    they paid for them, oldest first."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    cards = [one for one in order_cards_since(config, since) if re.match(_CARD_TITLE["fresh"], one["name"], re.I)]
    cards.sort(key=lambda one: card_made_at(one["id"]) or since)
    return cards


def invoice_lead_lines(config: Config, number: str) -> tuple[list[dict], float | None]:
    """An invoice's lines for leads, {"qty", "each", "text"} each, and the
    invoice's own total - ([], None) when it isn't found."""
    from .. import payraapi

    digits = re.sub(r"[^0-9]", "", str(number or "")).lstrip("0")
    if not digits:
        return [], None
    invoice = next((one for one in payra_kept(config, "invoices").values()
                    if re.sub(r"[^0-9]", "", str(one.get("number") or "")).lstrip("0") == digits), None)
    if not invoice:
        return [], None
    found = []
    for one in invoice.get("lines") or []:
        if not isinstance(one, dict) or not re.search(r"\blead", str(payraapi._first(
                one, "name", "description", "item", "product", "title") or ""), re.I):
            continue
        try:
            qty = float(str(payraapi._first(one, "quantity", "qty", "count")))
            each = float(str(payraapi._first(one, "price", "rate", "unit_price", "unit_amount", "amount")))
        except (TypeError, ValueError):
            return [], None
        found.append({"qty": qty, "each": each, "text": payraapi.line_text(one)})
    total = None
    for key in ("total", "amount_due", "amount", "subtotal"):
        try:
            total = float(str(payraapi._first(invoice, key)))
            break
        except (TypeError, ValueError):
            continue
    return found, total


def _age_of(package: str) -> int | None:
    found = _AGE_BUCKET.search(str(package or ""))
    return int(re.match(r"\d+", found.group(0)).group(0)) if found else None


def parts_from_invoice_lines(fields: dict, lines: list[dict]) -> list[dict]:
    """An aged order's lines straight off its invoice, when the notes give no
    counts and each invoice line is a product of its own - Jaylen Morgan's
    45 "Text-Verified Aged Veteran Leads" and 50 "Text-Verified Aged Final
    Expense Leads", the note only "Aged Vets and Aged FEX". The order's
    states on each. [] unless every line is aged, counted and different."""
    parts = []
    for line in lines:
        name = " ".join(str(line.get("text") or "").split(" × ")[0].split())
        qty = line.get("qty")
        if not name or line_kind(name) != "aged" or not qty or qty != int(qty) or qty < 2:
            return []
        parts.append({"kind": "aged", "package": name, "leads": str(int(qty)),
                      "states": str(fields.get("states") or ""), "launch_iso": ""})
    names = {one["package"].casefold() for one in parts}
    return parts if len(parts) >= 2 and len(names) == len(parts) else []


#: How far apart two lines' price per lead have to be to tell their ages
#: apart by it.
PRICE_GAP = 1.2


def counts_by_price(parts: list[dict], lines: list[dict], total: float | None) -> list[dict]:
    """Each aged line's count off the invoice, when the invoice's lines are
    all the same product and the notes give only the ages - Edwin Alvarado's
    400, 55 and 45 of "Text-Verified Aged Mortgage Protection Leads" for
    30-90, 90-180 and 180+, in an order of their own. The older, the cheaper:
    the dearest line a lead is the youngest age. The parts with their counts,
    or [] unless every line pairs plainly."""
    aged = [one for one in parts if one.get("kind") == "aged"]
    if len(aged) < 2 or len(aged) != len(parts) or len(lines) != len(aged) or total is None:
        return []
    if any(re.search(r"\d", str(one.get("leads") or "")) and str(one.get("leads")).strip("0 ") for one in aged):
        return []
    ages = [_age_of(one.get("package")) for one in aged]
    if None in ages or len(set(ages)) != len(ages):
        return []
    # A line's price is its total when they add up to the invoice's, its
    # price a lead when those times the counts do - card fee or not.
    near = lambda one, other: other and abs(one - other) / other <= 0.05  # noqa: E731
    if near(sum(one["each"] for one in lines), total):
        per_lead = [one["each"] / one["qty"] for one in lines]
    elif near(sum(one["each"] * one["qty"] for one in lines), total):
        per_lead = [one["each"] for one in lines]
    else:
        return []
    if any(one["qty"] < 1 or one["qty"] != int(one["qty"]) for one in lines):
        return []
    dearest = sorted(range(len(lines)), key=lambda at: per_lead[at], reverse=True)
    if any(per_lead[dearest[at]] < per_lead[dearest[at + 1]] * PRICE_GAP for at in range(len(dearest) - 1)):
        return []
    youngest = sorted(range(len(aged)), key=lambda at: ages[at])
    out = [dict(one) for one in parts]
    for part_at, line_at in zip(youngest, dearest):
        out[part_at]["leads"] = str(int(lines[line_at]["qty"]))
        out[part_at]["priced"] = f"${per_lead[line_at]:,.2f}/lead"
    return out


def setups_into_today(config: Config, today, through) -> tuple[list[str], list[str]]:
    """Every setup card for a launch from tomorrow through `through`, into
    Today - the team is out on the days that would have worked them. (a line
    each, problems)."""
    from .. import agents, dailyops, trello

    client = open_trello(config)
    try:
        lists = client.board_lists(config.secrets.trello_board_id)
        home = trello.find_list(lists, agents.TODAY)
        if home is None:
            return [], [f"The board has no list called {agents.TODAY!r}"]
        names = {str(one.get("id")): str(one.get("name") or "") for one in lists}
        every = [c for bl in lists for c in client.list_cards(str(bl.get("id") or ""))]
        lines, problems, seen = [], [], set()
        day = dailyops.next_day(today)
        while day <= through:
            card = agents.find_setup_card(every, day)
            if card is None:
                problems.append(f"⚠ No setup card covers {day:%a %b %-d} yet - one comes at 6 AM two days ahead.")
            elif str(card.get("id")) not in seen:
                seen.add(str(card.get("id")))
                where = names.get(str(card.get("idList") or ""), "")
                url = str(card.get("shortUrl") or card.get("url") or "")
                if where in (agents.TODAY, dailyops.QUALITY_CHECK, agents.DONE):
                    lines.append(f"→ **{card.get('name')}** is already in {where} <{url}>")
                else:
                    try:
                        client.move_card(str(card.get("id")), str(home.get("id")))
                        lines.append(f"→ **{card.get('name')}** moved from {where or '?'} into Today <{url}>")
                    except Exception as exc:
                        problems.append(f"⚠ Couldn't move {card.get('name')!r} — {_short(exc, 120)}")
            day = dailyops.next_day(day)
        return lines, problems
    finally:
        client.close()

