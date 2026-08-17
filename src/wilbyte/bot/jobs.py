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


def plan_slots(
    videos: list[Video],
    context: GHLContext | None,
    config: Config,
    ledger: Ledger | None = None,
) -> list[datetime]:
    return next_open_slots(taken_days(context, config, ledger), len(videos), config.schedule)


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
        published.warnings.extend(confirm_scheduled(published, context, config))
    return published


# A post is not queryable the instant it is created - GHL indexes it a beat
# later, and a single read-back raced that and reported "not found" instead of
# reporting the date.
READBACK_TRIES = 4
READBACK_DELAY = 2.0


def find_post(
    context: GHLContext,
    *,
    url_slug: str,
    post_id: str | None,
    tries: int = READBACK_TRIES,
    delay: float = READBACK_DELAY,
    sleep=None,
) -> tuple[dict | None, str, list[dict]]:
    """Look a just-created post up again, allowing for indexing lag.

    Returns (post, problem, everything). Matching prefers the id GHL handed
    back at create and falls back to the slug, because a post can be created
    and then have its slug adjusted, but its id never moves. The full list
    comes back too - the posts that do publish are the reference for what a
    post that doesn't is missing.
    """
    import time

    sleep = sleep or time.sleep
    problem = ""
    posts: list[dict] = []
    for attempt in range(max(1, tries)):
        if attempt:
            sleep(delay)
        try:
            posts = context.client.list_posts(context.blog_id)
        except ghl.GHLError as exc:
            problem = f"Couldn't confirm with GHL that the schedule stuck: {exc}"
            continue
        for candidate in posts:
            if post_id and ghl.post_key(candidate) == post_id:
                return candidate, "", posts
        for candidate in posts:
            if (candidate.get("urlSlug") or "").lower() == url_slug.lower():
                return candidate, "", posts
        problem = "GHL didn't return this post right after creating it — check the dashboard."
    return None, problem, posts


def schedule_payload(post: BlogPost, context: GHLContext) -> dict:
    """The full post body again, with the schedule date on it.

    A partial PUT is tempting and wrong: GHL's update endpoint replaces the
    post, so anything left out is anything lost.
    """
    return ghl.build_post_payload(
        location_id=context.client.location_id,
        blog_id=context.blog_id,
        title=post.title,
        content_html=post.copy.article_html,
        description=post.description,
        url_slug=post.url_slug,
        canonical_link=post.canonical_link,
        author_id=context.author_id,
        category_ids=context.category_ids,
        keywords=post.keywords,
        image_url=post.cover_image_url,
        image_alt=post.cover_alt_text,
        status=ghl.STATUS_SCHEDULED,
        published_at=post.scheduled_at,
    )


def confirm_scheduled(
    post: BlogPost, context: GHLContext, config: Config, *, sleep=None
) -> list[str]:
    """Check GHL recorded the date, repair it if not, and say so if it can't.

    GHL has accepted a scheduled post without complaint, stored no date, and
    then never published it - three posts sat as SCHEDULED and silently missed
    their days. A create that returns 201 is not evidence the post will go out.

    So read it back, and when the date is missing, send it again as an update:
    an API that drops a field on create sometimes honours it on update, and
    that is cheap to try. If even that doesn't take, report the field names GHL
    did return - the next fix needs the real schema, not another guess.
    """
    from zoneinfo import ZoneInfo

    from ..scheduler import stored_schedule

    if post.scheduled_at is None:
        return []

    tz = ZoneInfo(config.schedule.timezone)
    wanted = post.scheduled_at.date()

    def landed(stored: dict) -> bool:
        held = stored_schedule(stored)
        return held is not None and held.astimezone(tz).date() == wanted

    stored, problem, others = find_post(
        context, url_slug=post.url_slug, post_id=post.ghl_post_id, sleep=sleep
    )
    if stored is None:
        return [problem]
    if landed(stored):
        return []

    if post.ghl_post_id:
        try:
            context.client.update_post(post.ghl_post_id, schedule_payload(post, context))
        except ghl.GHLError as exc:
            return [_stuck(stored, others, wanted, tz, f"and the repair was refused: {_short(exc)}")]
        again, _, others = find_post(
            context, url_slug=post.url_slug, post_id=post.ghl_post_id, sleep=sleep
        )
        if again is not None:
            if landed(again):
                return []
            stored = again

    return [_stuck(stored, others, wanted, tz, "and sending it again didn't take")]


def _stuck(stored: dict, others: list[dict], wanted: date, tz, why: str) -> str:
    """One warning, carrying the evidence needed to fix the schema for good.

    Both halves matter. The fields on our post say what GHL kept; the fields a
    *working* post has and ours doesn't say what it wants - and there are
    posts on this blog that publish on their day, so the answer is already
    sitting in the same response.
    """
    from ..scheduler import stored_schedule

    held = stored_schedule(stored)
    has = held.astimezone(tz).strftime("%a %b %d") if held else "no date at all"
    note = (
        f"GHL saved this as scheduled but is holding {has}, not "
        f"{wanted.strftime('%a %b %d')}, {why}. Set the date by hand in the dashboard "
        f"or it won't publish."
    )
    return f"{note} {_field_evidence(stored, others)}"


def _field_evidence(stored: dict, others: list[dict], limit: int = 320) -> str:
    """What our post has, and what the posts that do publish have that it lacks."""
    ours = set(stored.keys())
    working: set[str] = set()
    for other in others:
        if other is stored:
            continue
        if stored_has_date(other):
            working |= set(other.keys())

    text = f"Fields on ours: {_names(sorted(ours), limit)}."
    extra = sorted(working - ours)
    if extra:
        text += f" Fields the dated posts have and ours doesn't: {_names(extra, limit)}."
    return text


def stored_has_date(post: dict) -> bool:
    from ..scheduler import stored_schedule

    return stored_schedule(post) is not None


def _names(names: list[str], limit: int) -> str:
    """Join field names, trimmed - a warning has to fit in a Discord embed."""
    if not names:
        return "(none)"
    text = ", ".join(names)
    return text if len(text) <= limit else text[:limit].rsplit(", ", 1)[0] + ", …"


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
    )
    ledger.save()
