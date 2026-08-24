"""Slot assignment: one post per weekday at 10:00 AM.

From the walkthrough: "schedule it to the next day where it doesn't have a blog
post yet... the time is always 10 a.m.... The only time we are not uploading blog
is Saturdays and Sundays."
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .config import ScheduleConfig

SATURDAY = 5
SUNDAY = 6


class SchedulerError(RuntimeError):
    """Raised when no valid slot can be produced."""


def is_posting_day(day: date, config: ScheduleConfig) -> bool:
    if not config.weekdays_only:
        return True
    return day.weekday() not in (SATURDAY, SUNDAY)


def next_posting_day(after: date, config: ScheduleConfig) -> date:
    """The first posting day strictly after `after`."""
    day = after + timedelta(days=1)
    for _ in range(14):  # generous bound; a 2-week gap of non-posting days is impossible
        if is_posting_day(day, config):
            return day
        day += timedelta(days=1)
    raise SchedulerError(f"No posting day found within 14 days of {after}.")


def slot_datetime(day: date, config: ScheduleConfig) -> datetime:
    hour, minute = config.hour_minute
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(config.timezone))


def next_open_slots(
    taken_days: set[date],
    count: int,
    config: ScheduleConfig,
    *,
    now: datetime | None = None,
    include_today: bool = False,
) -> list[datetime]:
    """Return the next `count` free weekday 10:00 AM slots.

    `taken_days` are the dates that already hold a scheduled or published post.
    A slot is skipped if it is taken, if it falls on a weekend, or if it is too
    close to now for GHL to accept (`min_lead_minutes`).

    `include_today` gives up the 10:00 rule for today only. Ten in the morning
    has usually gone by the time somebody decides they want something out
    today, and skipping the day for it means the answer to "can we post this
    afternoon" is "no, Monday" - so today's slot becomes the soonest time GHL
    will take instead. Every day after it is back to 10:00.
    """
    if count <= 0:
        return []

    tz = ZoneInfo(config.timezone)
    now = now.astimezone(tz) if now else datetime.now(tz)
    earliest = now + timedelta(minutes=config.min_lead_minutes)

    slots: list[datetime] = []
    day = now.date() - timedelta(days=1)  # next_posting_day() steps forward from here

    # A configured floor wins over the calendar read, because the calendar is
    # what's unreliable: GHL reports some posts without a schedule, and those
    # days look free.
    floor = config.floor
    if floor and floor > day:
        day = floor - timedelta(days=1)

    # Bound the walk so a fully-booked calendar fails loudly instead of hanging.
    for _ in range(count * 10 + 60):
        day = next_posting_day(day, config)
        if day in taken_days:
            continue
        candidate = slot_datetime(day, config)
        if candidate < earliest:
            # Today, asked for on purpose: take the soonest time GHL will
            # accept rather than losing the day. Only if that time is still
            # today - late at night the lead pushes it into tomorrow, and
            # tomorrow already has its own slot coming.
            if include_today and day == now.date() and earliest.date() == day:
                candidate = earliest
            else:
                continue
        slots.append(candidate)
        if len(slots) == count:
            return slots

    raise SchedulerError(
        f"Could only find {len(slots)} of {count} open slots. "
        "The calendar may be booked further out than expected."
    )


# The day a post occupies, most authoritative first.
POST_DATE_FIELDS = (
    "publishedAt",
    "publishDate",
    "publishedDate",
    "scheduledAt",
    "scheduleDate",
    "scheduledDate",
)

# When a post was written or last touched - never when it goes out.
BOOKKEEPING_DATE_FIELDS = frozenset(
    {"createdat", "updatedat", "deletedat", "archivedat", "lastupdatedat"}
)

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]")


def stored_schedule(post: dict) -> datetime | None:
    """The publish date GHL is actually holding for this post, or None.

    GHL does not report a schedule under a predictable key - the posts RYTE
    creates have come back without any of the documented fields. So: try the
    documented names, then scan for any other ISO timestamp on the object and
    take the latest. Bookkeeping fields are excluded by name, because a post
    written last Tuesday for next Thursday is a Thursday post.

    Nothing is inferred here. None means GHL is holding no date at all, which
    is a different fact from "the date is unknown" and is the one worth
    reporting after a create.
    """
    raw = next((post[f] for f in POST_DATE_FIELDS if post.get(f)), None)
    if raw:
        parsed = parse_timestamp(raw)
        if parsed:
            return parsed

    found: list[datetime] = []
    for key, value in post.items():
        if key.lower() in BOOKKEEPING_DATE_FIELDS:
            continue
        # Only ISO-shaped strings. Parsing arbitrary numbers would turn ids
        # into dates and book days at random.
        if not isinstance(value, str) or not _ISO_DATE.match(value):
            continue
        parsed = parse_timestamp(value)
        if parsed:
            found.append(parsed)
    return max(found) if found else None


def post_day(post: dict, tz: ZoneInfo) -> date | None:
    """The day this post occupies, or None.

    Falls back to the creation day when GHL reports no schedule, because the
    caller is asking "is this day free?" and a dateless post that is sitting
    on a day must not make that day look free.
    """
    stamped = stored_schedule(post)
    if stamped:
        return stamped.astimezone(tz).date()

    # Nothing else to go on. A published post's creation day is usually also
    # its posting day, so this is better than treating the day as free.
    created = parse_timestamp(post["createdAt"]) if post.get("createdAt") else None
    return created.astimezone(tz).date() if created else None


def holds_a_day(post: dict) -> bool:
    """Whether this post occupies a day on the calendar at all.

    A draft is not scheduled - it has no date because it is not going out, and
    it never will until somebody schedules it. Same for one that was archived
    or deleted. Counting them costs twice: their creation day reads as taken
    when it is free, and the check reports them as days that might be
    double-booked when there is no day involved.
    """
    if post.get("deleted") or post.get("archived"):
        return False
    return str(post.get("status") or "").upper() != "DRAFT"


def taken_days_from_posts(posts: list[dict], config: ScheduleConfig) -> set[date]:
    """Extract occupied dates from GHL blog posts.

    Accepts the timestamps GHL returns (ISO 8601 or epoch millis, usually UTC)
    and converts them into local posting dates.
    """
    tz = ZoneInfo(config.timezone)
    return {
        day for post in posts
        if holds_a_day(post) and (day := post_day(post, tz)) is not None
    }


def taken_days_from_ledger(entries, config: ScheduleConfig) -> set[date]:
    """The days RYTE has already handed out, from its own record.

    A second source of truth on purpose. GHL does not always report a schedule
    back on a post RYTE created, and when that happens the calendar read says
    the day is free and the next run books straight on top of it. The ledger
    knows what it gave out regardless of what the API says about it.
    """
    tz = ZoneInfo(config.timezone)
    taken: set[date] = set()
    for entry in entries:
        raw = getattr(entry, "scheduled_at", None)
        if not raw:
            continue
        parsed = parse_timestamp(raw)
        if parsed:
            taken.add(parsed.astimezone(tz).date())
    return taken


def parse_timestamp(raw: str | int | float) -> datetime | None:
    """Parse the timestamp formats GHL returns: ISO strings or epoch millis."""
    if isinstance(raw, (int, float)):
        seconds = raw / 1000 if raw > 10_000_000_000 else raw
        return datetime.fromtimestamp(seconds, tz=ZoneInfo("UTC"))
    text = str(raw).strip()
    if not text:
        return None
    if text.isdigit():
        return parse_timestamp(int(text))
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
