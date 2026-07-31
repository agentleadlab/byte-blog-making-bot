from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from wilbyte.scheduler import (
    SchedulerError,
    is_posting_day,
    next_open_slots,
    next_posting_day,
    parse_timestamp,
    taken_days_from_posts,
)

CT = ZoneInfo("America/Chicago")


def at(y, m, d, hh=9, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=CT)


def test_weekends_are_not_posting_days(config):
    assert is_posting_day(date(2026, 8, 14), config.schedule)  # Friday
    assert not is_posting_day(date(2026, 8, 15), config.schedule)  # Saturday
    assert not is_posting_day(date(2026, 8, 16), config.schedule)  # Sunday
    assert is_posting_day(date(2026, 8, 17), config.schedule)  # Monday


def test_friday_rolls_forward_to_monday(config):
    assert next_posting_day(date(2026, 8, 14), config.schedule) == date(2026, 8, 17)


def test_slots_are_10am_on_consecutive_weekdays(config):
    slots = next_open_slots(set(), 3, config.schedule, now=at(2026, 8, 12, 9))

    assert [s.date() for s in slots] == [date(2026, 8, 12), date(2026, 8, 13), date(2026, 8, 14)]
    assert all((s.hour, s.minute) == (10, 0) for s in slots)


def test_slots_skip_days_that_already_have_a_post(config):
    """Wil's case: booked through Aug 11, so the next open day is Aug 12."""
    taken = {date(2026, 8, d) for d in (5, 6, 7, 10, 11)}
    slots = next_open_slots(taken, 3, config.schedule, now=at(2026, 8, 4, 9))

    assert [s.date() for s in slots] == [date(2026, 8, 4), date(2026, 8, 12), date(2026, 8, 13)]


def test_slots_skip_the_weekend_gap(config):
    slots = next_open_slots(set(), 3, config.schedule, now=at(2026, 8, 13, 9))

    # Thu 13, Fri 14, then Mon 17 - Saturday and Sunday are skipped.
    assert [s.date() for s in slots] == [date(2026, 8, 13), date(2026, 8, 14), date(2026, 8, 17)]


def test_today_is_skipped_once_10am_has_passed(config):
    slots = next_open_slots(set(), 1, config.schedule, now=at(2026, 8, 12, 14))

    assert slots[0].date() == date(2026, 8, 13)


def test_min_lead_time_pushes_past_an_imminent_slot(config):
    # 9:50am with a 20-minute minimum lead: today's 10:00 slot is too close.
    slots = next_open_slots(set(), 1, config.schedule, now=at(2026, 8, 12, 9, 50))

    assert slots[0].date() == date(2026, 8, 13)


def test_zero_count_returns_nothing(config):
    assert next_open_slots(set(), 0, config.schedule, now=at(2026, 8, 12)) == []


def test_fully_booked_calendar_raises(config):
    taken = {date(2026, 8, 12) + __import__("datetime").timedelta(days=i) for i in range(400)}
    with pytest.raises(SchedulerError):
        next_open_slots(taken, 1, config.schedule, now=at(2026, 8, 12, 9))


def test_parse_timestamp_handles_iso_and_epoch_millis():
    assert parse_timestamp("2026-08-12T15:00:00Z").hour == 15
    assert parse_timestamp("2026-08-12T15:00:00.000Z").hour == 15
    assert parse_timestamp(1786000000000).year == 2026
    assert parse_timestamp("") is None
    assert parse_timestamp("not-a-date") is None


def test_taken_days_converts_utc_to_local_posting_date(config):
    # 15:00 UTC on Aug 12 is 10:00 CDT the same day.
    posts = [{"publishedAt": "2026-08-12T15:00:00.000Z"}, {"publishedAt": "2026-08-13T15:00:00.000Z"}]

    assert taken_days_from_posts(posts, config.schedule) == {date(2026, 8, 12), date(2026, 8, 13)}


def test_taken_days_ignores_posts_with_no_timestamp(config):
    assert taken_days_from_posts([{"title": "draft with no date"}], config.schedule) == set()
