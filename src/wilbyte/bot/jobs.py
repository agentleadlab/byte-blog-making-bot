"""Blocking pipeline work, wrapped so the Discord event loop never stalls.

Every method here does network or CPU work and is called via `asyncio.to_thread`
from `client.py`. Keeping them free of Discord types also makes them testable
without a gateway connection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
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


def build(
    video: Video,
    config: Config,
    output_dir: Path,
    *,
    transcript_text: str | None = None,
) -> BlogPost:
    """Transcript -> copy -> title -> cover image. No GHL contact.

    `transcript_text` skips the fetch entirely, which is how an attached
    transcript gets used when YouTube refuses to serve one.
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
            ledger.forget(entry.video_id)
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
        seen = client.meetings()
        found, how = fathom.choose(seen, link=rec.url, filed=recordings.filed_ids())
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


def copy_into_doc(config: Config, *, title: str, text: str) -> tuple[str, list[str]]:
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
            writing.write(tab, text if text.endswith("\n") else text + "\n")
            return writing.link_to(tab), (
                [] if already is None else
                [f"There was already a tab called {title!r}, so it went "
                 "underneath what was in it rather than into a second one."]
            )
    except doc.DocsError as exc:
        return "", [f"Couldn't write it into the doc: {_short(exc, 200)}"]
    except Exception as exc:
        return "", [f"Couldn't write it into the doc: {_short(exc, 200)}"]


#: Who cuts the interviews. Tagged on the YT VID checklist so the job reaches
#: them rather than sitting on a card they would have to think to open.
EDITORS = ("@mgproductions7", "@mgvideoeditors")

#: The card in Marketing Department that tracks what is waiting to be cut.
YT_CARD = "YT VID"

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
      2. The YT VID card's checklist, tagging the two editors who cut it.
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

        waiting = next(
            (one for one in every
             if str(one.get("name") or "").strip().casefold() == YT_CARD.casefold()),
            None,
        )
        if waiting is None:
            problems.append(f"No card called {YT_CARD!r} on the board.")
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
        seen = client.meetings()
        found, _how = fathom.choose(seen, link=rec.url)
        if found is None:
            raise SegmentError(f"I couldn't find that call in Fathom. {fathom.describe(seen)}")

        meeting = client.meeting_with_transcript(fathom.meeting_id(found.raw or {}))
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
    results.extend(_check_docs(config))
    return results


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
        return [(False, f"Posting doc — {_short(exc, 240)}")]
    except Exception as exc:
        return [(False, f"Posting doc — {_short(exc, 240)}")]
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


def tags_to_file(config: Config, *, day=None) -> tuple[list, list[str]]:
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
        every = [c for bl in lists for c in client.list_cards(str(bl.get("id") or ""))]
        members = client.board_members(config.secrets.trello_board_id)

        days = days_with_cards(every, day)
        if not days:
            return [], [f"No General, Ops or Ads card dated {day:%m/%d/%y} on the board"]

        tasks = []
        for which in days:
            tasks += _tags_on(config, client, every, members, which, problems)
        return tasks, problems
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

            # A screenshot and a tag. Trello writes the attachment into the
            # comment as a link to it, so all the words say is "image.png",
            # which went onto KC's list looking exactly like that.
            if tagged.only_a_file(note.text):
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

    if nothing_said:
        problems.append(
            f"{len(nothing_said)} said nothing but the name of what was "
            "attached to them, so I left them: "
            + "; ".join(tagged.trim(one.text)[:30] for one in nothing_said)
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
        summary=summary, judged=judged,
    )


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
            summary=summary, everyone=True,
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
            summary=summary, judged=judged,
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


def _ask_about_tags(config: Config, notes: list, people: dict) -> dict:
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
            "give somebody a line about work the comment gave to somebody else."
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
                            },
                            "required": ["comment_id", "person", "summary", "kind"],
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

        setup = next(
            (
                card for card in every
                if rules.is_setup_card(str(card.get("name", "")))
                and rules.setup_worked_on(str(card.get("name", "")), day) == day
            ),
            None,
        )
        if setup is None:
            return [], []

        link = str(setup.get("url") or setup.get("shortUrl") or "")
        short = trello.linked_card_id(link)
        if not short:
            return [], []

        dated = dailyops.cards_covering(every, day)
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
    finally:
        client.close()
    return added, problems


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
            })
    except Exception as exc:  # one bad card must not lose the rest
        problems.append(_short(exc, 160))
    finally:
        client.close()
    # Today's first: those are the ones with hours left rather than a day.
    found.sort(key=lambda held: held["when"] != "today")
    return found, problems


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

    day = day or board_day(config)
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
                "when": (
                    "today" if launch == day
                    else "tomorrow" if ahead and launch == tomorrow
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


def comment_on_daily(
    config: Config, *, kind: str, day: date, text: str
) -> tuple[str, str, list[str]]:
    """Say something on one of the four dated cards. (title, url, problems).

    Wherever the card has got to. A comment is about the day's work, and the
    day's work is on that card whether it is still in Today or already in
    Quality Check.
    """
    from .. import dailyops

    client = open_trello(config)
    try:
        lists = client.board_lists(config.secrets.trello_board_id)
        every = [c for bl in lists for c in client.list_cards(str(bl.get("id") or ""))]
        card = dailyops.cards_covering(every, day).get(kind)
        if card is None:
            named = dailyops.CARD_KINDS.get(kind, kind)
            return "", "", [f"No {named} card dated {day:%m/%d/%y} anywhere on the board."]

        title = str(card.get("name") or "")
        try:
            client.add_comment(str(card.get("id") or ""), text)
        except Exception as exc:
            return title, "", [f"Couldn't comment on {title!r} — {_short(exc, 160)}"]
        return title, str(card.get("url") or ""), []
    finally:
        client.close()


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
        stuck: list[tuple[str, str]] = []
        placed: set[str] = set()
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
                )))
                continue
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
                        + f" on {order.get('name')}. It doesn't say which."))
                else:
                    stuck.append((spread.url,
                        f"{who} — “{spread.label}” doesn't match any checklist on "
                        f"{order.get('name')}"
                        + _teach_me(spread.label)))
                continue
            try:
                client.add_check_item(
                    by_name[key], rules.checklist_item(spread.url, spread.label)
                )
            except Exception as exc:
                problems.append(f"{spread.label} — {_short(exc, 160)}")
                continue
            added.append(f"{named.get(spread.url, spread.label)} — {spread.checklist}")
            placed.add(spread.url)

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
                    "checklist": spread.checklist,
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
        problems.extend(said for url, said in stuck if url not in placed)
        also = [said for url, said in stuck if url in placed]
        if also:
            problems.append(
                f"{len(also)} line(s) I couldn't read were for agents already "
                "placed from another line on the setup card, so I left them: "
                + "; ".join(
                    said.split(" — ", 1)[-1].split(" could be")[0].strip("“”")
                    for said in also
                )
            )
        return added, conflicts, problems
    finally:
        client.close()


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


def agent_sheet(config: Config, asked: str) -> tuple[list[dict], list[str]]:
    """The setup sheet for the agent somebody named. (found, problems). Reads only.

    The sheet is already on the card - Therese and Faith post it as "Sheet
    link:" when the setup is finished. Getting at it meant finding the card
    first, which is the part that costs a person a minute and RYTE a request.
    """
    from .. import agents as rules

    name = rules.who_wants_a_sheet(asked)
    if not name:
        return [], ["Whose sheet do you want? Try `@RYTE sheet for Faith`."]

    client = open_trello(config)
    try:
        every = client.board_cards(config.secrets.trello_board_id, archived=True)
        cards = rules.named_that(name, every)
        if not cards:
            return [], [f"I can't find a New Agent card for “{name}” anywhere on the board."]

        found = []
        for card in cards:
            links = rules.sheet_links(client.card_comments(str(card.get("id") or "")))
            found.append({
                **card,
                "agent": rules.agent_name(str(card.get("name") or "")),
                "sheets": links,
            })
        # The ones that have a sheet first: an agent set up twice has two, and
        # an agent not set up yet has none and is not the answer to lead with.
        found.sort(key=lambda one: not one["sheets"])
        return found, []
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
        cards = rules.named_that(name, every)
    except Exception as exc:
        found.holes.append(f"Couldn't read the board: {_short(exc, 120)}")
        return found
    finally:
        client.close()

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

    # The payment confirmation, out of the inbox it was emailed to. Payra has
    # no API, so this is the receipt: the reference, the day it was paid, the
    # card it was paid with and the total. The customer's own name is in the
    # subject line of it, which is what makes it findable.
    receipt, trouble = _payment_receipt(config, dispute)
    if receipt:
        found.invoice = (found.invoice + "\n\n" if found.invoice else "") + receipt
    elif trouble:
        found.holes.append(trouble)

    # And the signed contract, out of the same inbox. PandaDoc emails the
    # completed document with the PDF on it, which is the way to a contract
    # that their API is not without a paid plan.
    said, pdf, called, trouble = _signed_contract(config, dispute)
    if said:
        found.contract = said
    if pdf:
        found.contract_pdf, found.contract_name = pdf, called
    if trouble:
        found.holes.append(trouble)

    found.timeline = _timeline(dispute, agent, found)
    return found


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

    client = open_trello(config)
    try:
        every = client.board_cards(config.secrets.trello_board_id, archived=True)
        cards = rules.named_that(name, every)
        if not cards:
            return "", [f"No New Agent card for “{name}” anywhere on the board."]
        said = client.card_comments(str(cards[0].get("id") or ""))
    except Exception as exc:
        return "", [f"Couldn't read their card: {_short(exc, 140)}"]
    finally:
        client.close()

    links = rules.sheet_links(said)
    if not links:
        return "", [f"No sheet link in the comments on “{name}”'s card."]
    return links[-1][1], []


def collect_client(config: Config, row: list) -> tuple[str, list[str]]:
    """Put one line in the ALL CLIENTS tab. (what tab it went on, problems).

    Appended, never written over: the tab is a record of every client who has
    been closed down, and nothing here has any business changing a line that
    is already in it.
    """
    from .. import gsheets

    link = (getattr(config.secrets, "clients_sheet_link", "") or "").strip()
    sheet_id = gsheets.sheet_id_in(link)
    if not sheet_id:
        return "", [
            "CLIENTS_SHEET_LINK in .env isn't a spreadsheet link, so there is "
            "nowhere to collect them."
        ]

    try:
        with gsheets.SheetsClient(gsheets.credentials(config.secrets)) as client:
            tab = client.tab_named(sheet_id, gsheets.gid_in(link))
            if not tab:
                return "", [
                    "No tab in that spreadsheet with the gid in "
                    "CLIENTS_SHEET_LINK - paste the link again with the right "
                    "tab open."
                ]
            client.append(sheet_id, tab, [row])
    except Exception as exc:
        return "", [f"Couldn't write the sheet: {_short(exc, 140)}"]
    return tab, []


def keep_the_picture(config: Config, page: str, called: str) -> tuple[str, list[str]]:
    """Photograph a conversation and put it in Drive. (link, problems).

    The channel is about to stop existing, so this is the only copy there will
    be of what was said in it - which is why it is a picture rather than a
    paragraph somebody wrote about it.
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

        try:
            with drive.open_drive(config.secrets) as uploading:
                got = uploading.put(png_path, name=called)
        except drive.DriveError as exc:
            return "", [str(exc)]
        except Exception as exc:
            return "", [f"Couldn't upload to Drive: {_short(exc, 140)}"]
    return got.link(), []


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
            page.screenshot(path=str(png_path), full_page=True)
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


def _signed_contract(config: Config, dispute) -> tuple[str, bytes, str, str]:
    """The signed contract for this customer, out of the inbox it was sent to.

    (what it says, the PDF, what to call it, a problem or "").

    PandaDoc's production API is behind a sales call on this account and the
    sandbox key only reaches sandbox documents - but a completed document is
    emailed to the owner with the PDF on it, and that email is the contract.
    No webhook, no public address for a Mac that sleeps, and nothing to keep
    running between PandaDoc and here.

    The PDF when there is one, because the no-chargeback clause is in the
    document rather than in the notification. The body is the fallback: it
    carries who signed and when, which is most of what the timeline wants.
    """
    from .. import gmail as inbox

    if not (getattr(config.secrets, "gmail_contract_sender", "") or "").strip():
        return "", b"", "", ""
    try:
        with inbox.open_contracts(config.secrets) as reading:
            found = reading.invoices_for(dispute.customer_name)
            if not found:
                return "", b"", "", (
                    f"No signed contract for “{dispute.customer_name}” in the "
                    "inbox, so it had to be left out."
                )
            # The completed one. PandaDoc emails at every step - sent, viewed,
            # a reminder - and only the completed one is the signed document.
            done = [
                one for one in found
                if any(word in one.subject.casefold()
                       for word in ("completed", "signed", "countersigned"))
            ] or found
            one = done[0]
            said = f"{one.subject}\n{one.when}\n\n{one.body}".strip()
            for name, attachment_id in one.files:
                if name.casefold().endswith(".pdf"):
                    return said, reading.download(one.message_id, attachment_id), name, ""
            return said, b"", "", (
                f"The contract email for “{dispute.customer_name}” has no PDF on "
                "it, so only what it says is in the document. Download the "
                "completed PDF from PandaDoc and attach it for the clause."
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
    if agent is not None and agent.launch:
        when.append((f"{agent.launch:%m/%d/%Y}", "Launch date, leads begin delivery"))
    # The handover. Without it the spine of the document is a charge and a
    # chargeback with nothing in between, which is the half that answers the
    # dispute - Juliana Hernandez's timeline was one line long.
    handed = (getattr(found, "delivered_on", "") or "").split("-")
    if len(handed) == 3:
        when.append((
            f"{handed[1]}/{handed[2]}/{handed[0]}",
            "Leads delivered by shared spreadsheet, confirmed in writing",
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
            "rows that matter. Never write a word that is not in the image.\n\n"
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
    # mean the same thing the back of the document does.
    exhibits[:] = rules_doc.letter_them(exhibits)

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

        every_card = [
            card for bl in lists for card in client.list_cards(str(bl.get("id") or ""))
        ]
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

        plans = []
        for card in waiting_cards:
            if not agents.is_agent_card(str(card.get("name", ""))):
                continue
            detail = client.card_detail(str(card.get("id") or ""))
            # One request for the comments and for whether this card was
            # copied from another - see `agents.still_being_written`.
            said, copied = client.card_story(str(card.get("id") or ""))
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
        return plans, where, missing
    finally:
        client.close()


def _plan_for(client, agent, *, day, tomorrow, dated, every_card, parked=False):
    """One agent's plan, with the cards and checklists it needs already found."""
    from .. import agents as rules
    from .. import dailyops

    plan = rules.AgentPlan(agent=agent, when=agent.when(day))

    # Without a launch date there is nothing to decide, and parking it would
    # be a guess. That one always needs a person.
    if agent.launch is None:
        plan.problems.append(rules.cannot_read(agent, needs_lead_type=False))
        return plan

    # Before anything is decided on the date, because everything is: which
    # setup card they go on, and whether they park or get filed now.
    if agent.note:
        plan.problems.append(agent.note)
        return plan

    if plan.when != "today":
        # Whether they can be done yet turns on whether the card they go on
        # exists, not on how far off the launch is. A Thursday card made on
        # Tuesday is a Thursday card, and the agents for it can go on now.
        card = rules.find_setup_card(every_card, agent.launch)
        if card is None:
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

        # Two orders on two days are two setup cards. Garret Sekelsky bought
        # "25 OTP Vets - live Friday, September 11" and "25 OTP FEX - live
        # Saturday, September 12", and both went onto Friday's card because
        # the card was read for one launch date and both orders filed against
        # it. The Saturday leads would have been set up a day early.
        dated = rules.dated_orders(agent.said, today=day)
        if rules.on_several_days(dated):
            waiting = []
            for order, when in dated:
                its_card = (
                    card if when in (None, agent.launch)
                    else rules.find_setup_card(every_card, when)
                )
                if its_card is None:
                    waiting.append(f"{order} — live {when:%a %b %d}")
                    continue
                where = str(its_card.get("name", ""))
                its_id = str(its_card.get("id") or "")
                its_held = client.card_checklists(its_id)
                for person in rules.SETUP_PEOPLE:
                    plan.steps.append(_step(
                        where, its_id, person, agent, its_held,
                        exact=True, label=order,
                    ))
            if waiting:
                # Filed what could be filed and stayed put, so the rest is
                # picked up when its card exists. Moving to Done here would
                # take the card away with an order still unfiled on it.
                plan.problems.append(
                    f"{agent.name} — no setup card yet for "
                    + "; ".join(waiting)
                )
                plan.move_to = "" if parked else rules.PARKED
            else:
                plan.move_to = rules.DONE
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

    # Going live today: Ads and Ops, and not the Lead Order card. Somebody
    # turning up on the morning of their own launch is a card the team is
    # working now, not an order to be filed under its lead type - "when we
    # have a new agent going live same day ... dont do that anymore, just add
    # it on ops and ads". The Lead Order card is still written by
    # `spread_to_lead_order`, from the setup card, when it is asked for.
    for kind, people in (("ads", rules.ADS_PEOPLE), ("ops", rules.OPS_PEOPLE)):
        card = dated.get(kind)
        if card is None:
            plan.problems.append(
                f"No {dailyops.CARD_KINDS.get(kind, kind)} card dated "
                f"{day:%m/%d/%y} anywhere on the board"
            )
            continue
        card_id = str(card.get("id") or "")
        held = client.card_checklists(card_id)
        title = str(card.get("name", ""))
        for person in people:
            plan.steps.append(_step(
                title, card_id, person, agent, held, exact=True,
                label=agent.stated,
            ))

    plan.move_to = rules.DONE
    return plan


def _step(card_title, card_id, checklist, agent, held, *, exact, label=""):
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
            day=rules.day_label(agent.launch) if rules.spans_days(card_title) else "",
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
            already = {
                (
                    " ".join(str(c.get("name") or "").split()).casefold(),
                    " ".join(str(existing.get("name") or "").split()),
                )
                for c in target_lists or []
                for existing in c.get("checkItems") or []
            }
            for item in plan.carried:
                key = " ".join(item.person.split()).casefold()
                if (key, " ".join(item.name.split())) in already:
                    continue
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


def _restyle(client, sheet_id: str, tab_id, where: str, *, bold: bool) -> None:
    """Make the rows just written look like rows rather than headings."""
    from .. import gsheets

    span = gsheets.rows_in(where)
    if tab_id is None or span is None:
        return
    first, last = span
    client.restyle(sheet_id, int(tab_id), first, last, bold=bold)


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
