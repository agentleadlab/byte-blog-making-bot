"""Slot assignment: one post per weekday at 10:00 AM.

From the walkthrough: "schedule it to the next day where it doesn't have a blog
post yet... the time is always 10 a.m.... The only time we are not uploading blog
is Saturdays and Sundays."
"""

from __future__ import annotations

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
) -> list[datetime]:
    """Return the next `count` free weekday 10:00 AM slots.

    `taken_days` are the dates that already hold a scheduled or published post.
    A slot is skipped if it is taken, if it falls on a weekend, or if it is too
    close to now for GHL to accept (`min_lead_minutes`).
    """
    if count <= 0:
        return []

    tz = ZoneInfo(config.timezone)
    now = now.astimezone(tz) if now else datetime.now(tz)
    earliest = now + timedelta(minutes=config.min_lead_minutes)

    slots: list[datetime] = []
    day = now.date() - timedelta(days=1)  # next_posting_day() steps forward from here

    # Bound the walk so a fully-booked calendar fails loudly instead of hanging.
    for _ in range(count * 10 + 60):
        day = next_posting_day(day, config)
        if day in taken_days:
            continue
        candidate = slot_datetime(day, config)
        if candidate < earliest:
            continue
        slots.append(candidate)
        if len(slots) == count:
            return slots

    raise SchedulerError(
        f"Could only find {len(slots)} of {count} open slots. "
        "The calendar may be booked further out than expected."
    )


# The day a post occupies, most authoritative first. A scheduled post carries a
# future date under one of these; `createdAt` is the last resort because for a
# scheduled post it is the day it was *written*, which books the wrong day.
POST_DATE_FIELDS = (
    "publishedAt",
    "publishDate",
    "publishedDate",
    "scheduledAt",
    "scheduleDate",
    "scheduledDate",
    "createdAt",
)


def taken_days_from_posts(posts: list[dict], config: ScheduleConfig) -> set[date]:
    """Extract occupied dates from GHL blog posts.

    Accepts the timestamps GHL returns (ISO 8601 or epoch millis, usually UTC)
    and converts them into local posting dates.
    """
    tz = ZoneInfo(config.timezone)
    taken: set[date] = set()
    for post in posts:
        raw = next((post[f] for f in POST_DATE_FIELDS if post.get(f)), None)
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
