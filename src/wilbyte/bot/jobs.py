"""Blocking pipeline work, wrapped so the Discord event loop never stalls.

Every method here does network or CPU work and is called via `asyncio.to_thread`
from `client.py`. Keeping them free of Discord types also makes them testable
without a gateway connection.
"""

from __future__ import annotations

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
    return results


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


def diagnose_link(config: Config, link: str) -> list[str]:
    """Whether a pasted link matches a recording, and the tokens compared if not.

    The list of visible calls answers "can RYTE see it". This answers the next
    question, which turned out to be the real one: it can see the call and
    still not recognise the link as pointing at it.
    """
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
    from .. import dailyops, trello

    day = day or board_day(config)
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
            if not walks_today(card, day, step=step):
                continue
            title = str(card.get("name", ""))
            where = walk_to(card, step, day)
            # Named only when it isn't the one on the button, so the setup
            # card going somewhere else is visible before it goes there.
            found.append(title if where == to_name else f"{title} → {where}")
        return found, []
    finally:
        client.close()


def walks_today(card: dict, day: date, *, step: str) -> bool:
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
    """
    from .. import agents, dailyops

    title = str(card.get("name", ""))
    if agents.is_agent_card(title):
        return False
    named = dailyops.parse_card_title(title)
    if named is None:
        return True
    return named[1] <= day if step == "to_done" else named[1] == day


def walk_to(card: dict, step: str, day: date) -> str:
    """Which list a card lands in for one step of the walk, by name.

    Normally the step's own destination. The exception is the setup card: at
    six it goes back to In Que rather than on to Quality Check, so nine the
    next morning puts it in Today again. Agents keep being added to it right
    up to the day it covers, and a card in Quality Check is a card nobody is
    adding to. Once its go-live day has been and gone it walks on with
    everything else.
    """
    from .. import agents, dailyops

    to_name = dailyops.STEP_LISTS[step][1]
    if step != "to_quality_check":
        return to_name
    title = str(card.get("name", ""))
    if agents.is_setup_card(title) and agents.setup_ahead_of(title, day):
        return dailyops.IN_QUE
    return to_name


def walk_board(config: Config, step: str, *, day=None) -> tuple[int, list[str]]:
    """Move everything in one list to the next. (moved, problems).

    The whole list rather than only the four dated cards - the lists *are* the
    day, and leaving the rest behind means somebody still walks the board by
    hand afterwards, which is the thing this replaces. Two exceptions, both in
    `walks_today`: a new agent's card, and a card dated for another day.

    What is in the list is all it touches: a card somebody already dragged
    across is where they wanted it, and nothing goes looking for cards
    elsewhere on the board.
    """
    from .. import dailyops, trello

    day = day or board_day(config)
    from_name, to_name = dailyops.STEP_LISTS[step]
    client = open_trello(config)
    moved, problems = 0, []
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
            if not walks_today(card, day, step=step):
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
    finally:
        client.close()
    return moved, problems


def run_rollover(config: Config, *, day=None, only: str | None = None) -> tuple[int, list[str], list]:
    """Read the board and carry the items over. (moved, problems, flagged).

    The unattended version of what the button does, and the same two things
    stay put: an item whose linked card already reads Done, and one carried
    three nights running. Those come back to be said out loud rather than
    moved while nobody is watching.
    """
    plans, missing, targets = read_rollover(config, day=day, only=only)
    moved, problems = apply_rollover(config, plans, targets, day=day)
    if missing:
        problems.append(f"No card for tomorrow yet: {', '.join(missing)}")
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
        dated = dailyops.cards_for(every_card, day)

        waiting_cards = [
            card for bl in watched.values() if bl is not None
            for card in client.list_cards(str(bl.get("id") or ""))
        ]

        plans = []
        for card in waiting_cards:
            if not agents.is_agent_card(str(card.get("name", ""))):
                continue
            detail = client.card_detail(str(card.get("id") or ""))
            said = "\n".join(
                [str(detail.get("desc") or "")]
                + client.card_comments(str(card.get("id") or ""))
            )
            agent = agents.read_agent({**card, **detail}, text=said, today=day)
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

    if plan.when != "today":
        # Whether they can be done yet turns on whether the card they go on
        # exists, not on how far off the launch is. A Thursday card made on
        # Tuesday is a Thursday card, and the agents for it can go on now.
        card = rules.find_setup_card(every_card, agent.launch)
        make = ""
        if card is None:
            if plan.when != "tomorrow":
                # Nothing to put them on yet. Wait in Franklin's list, and
                # leave one already there exactly where it is rather than
                # moving it to the list it is in.
                plan.move_to = "" if parked else rules.PARKED
                return plan
            # A card made on a Friday covers the whole weekend, because
            # nobody is making one on Saturday.
            span = rules.weekend_span(agent.launch)
            make = rules.setup_title(agent.launch, span[1] if span else None)

        problem = rules.cannot_read(agent, needs_lead_type=True)
        if problem:
            plan.problems.append(problem)
            return plan

        plan.make_card = make
        title = make or str(card.get("name", ""))
        card_id = str(card.get("id") or "") if card else ""
        held = client.card_checklists(card_id) if card else []
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

    # Going live today: the three dated cards, wherever they have got to.
    for kind, people in (
        ("lead_order", None), ("ads", rules.ADS_PEOPLE), ("ops", rules.OPS_PEOPLE),
    ):
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
        if people is None:
            names = [str(c.get("name") or "") for c in held]
            said, landed, could = rules.best_lead_type(agent.said, names)
            label = said or agent.stated or agent.lead_type
            if rules.is_own_setup(label):
                # FB, Instant and Basic are the self-setup ones, and they all
                # go on the one checklist. The lead type is in the line rather
                # than being the checklist it sits on.
                landed, could = rules.OWN_SETUP, []
            if landed is None and len(could) > 1:
                # The card named its leads and not its tier, and two on the
                # board would both fit. Picking one is a guess about somebody's
                # money.
                plan.problems.append(
                    f"“{label}” could be {' or '.join(could)} — "
                    f"the card doesn't say which."
                )
                continue
            plan.steps.append(_step(
                title, card_id, landed or label, agent, held,
                exact=bool(landed), label=label,
            ))
        else:
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
    """
    from .. import agents as rules

    names = {" ".join(str(c.get("name") or "").split()).casefold() for c in held or []}
    return rules.Step(
        card_title=card_title,
        card_id=card_id,
        checklist=checklist,
        item=rules.checklist_item(agent.url, label or agent.lead_type),
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
    """One agent, all of it, or an exception and the card left where it is."""
    from .. import agents as rules

    if plan.make_card:
        made = client.create_card(where[rules.AUTOMATION], plan.make_card)
        card_id = str(made.get("id") or "")
        for step in plan.steps:
            step.card_id = card_id
            step.card_title = plan.make_card
            step.make_checklist = True

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
    """What the 9pm rollover would move, as words. Writes nothing."""
    from .. import dailyops

    plans, missing, _ = read_rollover(config, day=day)
    report = dailyops.summarise(plans)
    if missing:
        report += f"\n⚠ No card for tomorrow yet: {', '.join(missing)}"
    return report


def read_rollover(config: Config, *, day=None, only: str | None = None):
    """(plans, cards missing for tomorrow, where each item goes).

    Read-only, and the same read the write uses - so what gets shown and what
    gets done cannot drift apart. The third value carries the target card ids
    and checklists, because working them out twice is how a rollover ends up
    writing to a card nobody was shown.
    """
    from .. import carried, dailyops

    day = day or board_day(config)
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

        if only:
            today_cards = {k: v for k, v in today_cards.items() if k == only}

        plans, targets = [], {}
        for kind, card in today_cards.items():
            target = tomorrow_cards.get(kind)
            if target is None:
                continue
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
        # the card is sitting right there with the wrong year on it.
        missing = [
            f"{dailyops.CARD_KINDS.get(kind, kind)} ({dailyops.why_missing(cards, kind, tomorrow)})"
            for kind in today_cards
            if kind not in tomorrow_cards
        ]
        return plans, missing, targets
    finally:
        client.close()


def apply_rollover(config: Config, plans, targets, *, day=None) -> tuple[int, list[str]]:
    """Write the carried items onto tomorrow's cards. Returns (moved, problems).

    Only what the plan called `carried` - an item whose linked card already
    reads Done, or one that has been carried three days running, is left where
    it is and raised instead. Those are the two cases where moving it silently
    is the wrong answer, and they are the reason this asks before it writes.

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
                if item.stuck:
                    continue
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
