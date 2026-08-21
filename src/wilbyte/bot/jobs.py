"""Blocking pipeline work, wrapped so the Discord event loop never stalls.

Every method here does network or CPU work and is called via `asyncio.to_thread`
from `client.py`. Keeping them free of Discord types also makes them testable
without a gateway connection.
"""

from __future__ import annotations

from datetime import date, datetime
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


def open_slots(taken: set[date], count: int, config: Config) -> list[datetime]:
    """The next free slots, honouring an earliest day set from Discord.

    Every slot decision goes through here so the floor can't apply in one view
    and not another - a calendar that disagrees with itself is worse than one
    that's simply wrong.
    """
    return next_open_slots(taken, count, prefs.apply(config).schedule)


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
) -> list[datetime]:
    return open_slots(taken_days(context, config, ledger), len(videos), config)


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
        database_id = client.find_child_database(page_id, recordings.TITLE_PREFIX + "s")
        if not database_id:
            database_id = client.create_database(
                page_id, recordings.TITLE_PREFIX + "s", recordings.database_schema()
            )

        rows = client.query_database(database_id)
        number = recordings.next_number(recordings.row_titles(rows))
        title = recordings.title_for(number)
        cover_url = card_cover(config, rec, title)

        created = client.create_page(
            database_id,
            recordings.page_properties(rec, title),
            children=recordings.page_blocks(rec, summary),
            cover_url=cover_url,
            icon_url=config.secrets.notion_icon_url,
            icon_emoji=config.secrets.notion_icon_emoji or DEFAULT_CARD_ICON,
        )
    finally:
        client.close()
    return title, str(created.get("url") or "")


# A gallery of identical banners is a gallery you can't skim, so each card gets
# its own cover naming the call. The emoji is the fallback icon: Notion stores
# it, so it never expires and needs nothing uploaded.
DEFAULT_CARD_ICON = "🎙️"


def card_cover(config: Config, rec, title: str) -> str:
    """Render a cover for this call and return a permanent URL for it.

    Uses the same renderer as the blog covers, so the gallery matches the
    brand without anyone having to find and attach an image. Failing to make
    one is not worth failing the card over - a plain card beats no card - so
    this returns "" and lets the entry go in without it.
    """
    from .. import cover as cover_mod
    from ..models import CoverPlan
    from ..pipeline import DEFAULT_OUTPUT_DIR

    if config.secrets.notion_cover_url:
        return config.secrets.notion_cover_url

    plan = CoverPlan(kicker="AGENT LEAD LAB", headline=(rec.topic or title).upper())
    path = DEFAULT_OUTPUT_DIR / "recordings" / f"{_slug(title)}.png"
    try:
        cover_mod.render_cover(plan, config, path)
        return host_image(config, path, name=path.name)
    except Exception as exc:  # rendering or upload - neither should lose the card
        import logging

        logging.getLogger("wilbyte.bot").warning("No cover for %s: %s", title, exc)
        return ""


def _slug(text: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "-", (text or "card").lower()).strip("-") or "card"


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
        found = client.find(rec.url)
        if found is None:
            return ""
        # Carry the meeting details back for the title and the card details.
        rec.topic = found.topic
        rec.host_email = found.host_email
        return client.transcript(found)
    finally:
        client.close()


def fathom_transcript(config: Config, rec) -> str:
    """The transcript of a Fathom-recorded call, or "" when it can't be found.

    Fathom is the better source where both exist: it sits in the meeting as a
    notetaker, so there is no passcode and no transcription setting that might
    have been off at the time.
    """
    from .. import fathom

    client = fathom.FathomClient(config.secrets.fathom_api_key)
    try:
        found, seen = client.find(rec.url)
        if found is None:
            # Say what was actually there rather than "not found". A link that
            # doesn't match is exactly when the response shape matters.
            import logging

            logging.getLogger("wilbyte.bot").warning(
                "No Fathom call matched %s. %s", rec.url, fathom.describe(seen)
            )
            return ""
        rec.topic = found.title
        if found.participants:
            rec.participants = found.participants
        return fathom.transcript_text(found.raw or {})
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
    if not text.strip():
        return ""

    from anthropic import Anthropic

    config.secrets.require("anthropic_api_key")
    client = Anthropic(api_key=config.secrets.anthropic_api_key)
    response = client.messages.create(
        model=config.copy.model,
        max_tokens=1200,
        system=(
            "You summarise sales calls for an insurance lead company's internal "
            "library. Be specific and useful to a rep reading it later. Never "
            "invent anything that wasn't said."
        ),
        messages=[{
            "role": "user",
            "content": (
                "Summarise this sales call. Give a two-sentence overview, then "
                "bullets for: what the prospect wanted, objections raised, how "
                "they were handled, and what was agreed. Use '- ' for bullets.\n\n"
                f"{text}"
            ),
        }],
    )
    return "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    ).strip()


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

    from ..scheduler import post_day

    tz = ZoneInfo(config.schedule.timezone)
    undated = [p for p in posts if post_day(p, tz) is None]
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
