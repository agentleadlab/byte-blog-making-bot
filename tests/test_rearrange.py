"""Re-laying a calendar that is already booked.

Opening the weekend up does nothing on its own: everything is still sitting on
the weekdays it was given, and the new Saturday and Sunday go by empty. These
cover the pairing rule - keep the running order, take the earliest slot - and
the reading of "weekends on" as a change rather than a question.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from wilbyte import prefs, rearrange
from wilbyte.bot import mentions

EASTERN = ZoneInfo("America/New_York")


def at(day: int, *, month: int = 8, year: int = 2026, hour: int = 10):
    return datetime(year, month, day, hour, tzinfo=EASTERN)


def post(video_id: str, title: str, when):
    return (video_id, title, when)


# ------------------------------------------------- pairing posts with slots


def test_the_running_order_survives_the_move():
    """Nobody asked for a different order - they asked for it to happen sooner."""
    posts = [post("a", "First", at(24)), post("b", "Second", at(25))]
    slots = [at(22), at(23)]

    moves = rearrange.pair(posts, slots)

    assert [move.title for move in moves] == ["First", "Second"]
    assert [move.now for move in moves] == [at(22), at(23)]


def test_a_post_already_on_the_right_day_is_not_a_move():
    posts = [post("a", "First", at(22)), post("b", "Second", at(25))]

    moves = rearrange.pair(posts, [at(22), at(23)])

    assert moves[0].moved is False
    assert moves[1].moved is True


def test_a_post_with_no_date_is_always_a_move():
    moves = rearrange.pair([post("a", "First", None)], [at(22)])

    assert moves[0].moved is True


def test_a_queue_longer_than_the_calendar_drops_the_overflow():
    """Inventing a date for the extra post would hide a real problem."""
    posts = [post("a", "First", at(24)), post("b", "Second", at(25))]

    assert len(rearrange.pair(posts, [at(22)])) == 1


def test_the_days_they_hold_now_are_reported_so_they_can_be_freed():
    """Otherwise every post blocks its own move: Monday is taken, by the post
    that wants to move off Monday."""
    posts = [post("a", "First", at(24)), post("b", "Second", at(25))]

    assert rearrange.held_days(posts, EASTERN) == {date(2026, 8, 24), date(2026, 8, 25)}


def test_a_post_with_no_date_holds_no_day():
    assert rearrange.held_days([post("a", "First", None)], EASTERN) == set()


# ---------------------------------------------------------- what it will say


def test_nothing_to_do_says_so_plainly():
    moves = rearrange.pair([post("a", "First", at(22))], [at(22)])

    assert "Nothing to move" in rearrange.summarise(moves)


def test_the_summary_names_both_days():
    moves = rearrange.pair([post("a", "The Churn Post", at(24))], [at(22)])

    said = rearrange.summarise(moves)

    assert "The Churn Post" in said
    assert "Mon Aug 24" in said and "Sat Aug 22" in said


def test_posts_that_are_not_moving_are_counted_not_listed():
    posts = [post("a", "Staying", at(22)), post("b", "Moving", at(25))]

    said = rearrange.summarise(rearrange.pair(posts, [at(22), at(23)]))

    assert "Staying" not in said
    assert "1 already on the right day" in said


# -------------------------------------------------- weekends, on and off


@pytest.mark.parametrize(
    "said", ["weekends on", "weekends yes", "weekend on", "turn on weekends",
             "include weekends", "everyday"],
)
def test_turning_the_weekend_on(said):
    assert mentions.weekend_switch(said) is True


@pytest.mark.parametrize(
    "said", ["weekends off", "weekends no", "turn off weekends", "exclude weekends"],
)
def test_turning_the_weekend_off(said):
    assert mentions.weekend_switch(said) is False


@pytest.mark.parametrize("said", ["weekends", "do we post on weekends?", "weekends ?"])
def test_a_question_about_weekends_changes_nothing(said):
    """"do we post on weekends?" carries an "on" that means nothing. Answering
    it by silently rewriting the schedule is the worst kind of helpful."""
    assert mentions.weekend_switch(said) is None


def test_weekends_is_read_as_a_command():
    assert mentions.parse("weekends on").action == "weekends"


def test_a_brief_about_the_weekend_still_writes_copy():
    """"an email about our weekend sale" must not change the calendar."""
    assert mentions.parse("email about our weekend sale").action == "write"


def test_rearrange_is_read_as_a_command():
    for said in ("rearrange", "reschedule", "reshuffle the calendar"):
        assert mentions.parse(said).action == "rearrange", said


# ------------------------------------------------------- publishing early


def test_publish_only_counts_as_a_command_when_it_opens_the_message():
    assert mentions.parse("publish monday").action == "publish"
    assert mentions.parse("publish monday").brief.strip() == "monday"


def test_a_brief_that_mentions_publishing_is_not_a_command():
    """A blog post is the thing this bot makes - "write a post about how to
    publish faster" must not push Monday's article live."""
    request = mentions.parse("write an email about how to publish faster")

    assert request.action == "write"


def test_publish_with_a_link_still_writes_the_post():
    """"publish this <youtube link>" is a request to make one, not to send an
    existing one out early."""
    request = mentions.parse("publish this https://www.youtube.com/watch?v=abc12345678")

    assert request.action == "run"


# ------------------------------------------------- the preference itself


def test_weekends_on_makes_every_day_a_posting_day(tmp_path, monkeypatch):
    from wilbyte.config import load_config
    from wilbyte.scheduler import is_posting_day

    store = tmp_path / "prefs.json"
    config = load_config()
    saturday = date(2026, 8, 22)

    assert is_posting_day(saturday, prefs.apply(config, store).schedule) is False

    prefs.set_weekends(True, store)
    assert is_posting_day(saturday, prefs.apply(config, store).schedule) is True

    prefs.set_weekends(False, store)
    assert is_posting_day(saturday, prefs.apply(config, store).schedule) is False


def test_the_earliest_day_and_the_weekend_are_kept_apart(tmp_path):
    """Setting one must not silently drop the other."""
    from wilbyte.config import load_config

    store = tmp_path / "prefs.json"
    prefs.set_weekends(True, store)
    prefs.set_earliest_day(date(2026, 9, 1), store)

    schedule = prefs.apply(load_config(), store).schedule

    assert schedule.weekdays_only is False
    assert schedule.earliest_day == "2026-09-01"


def test_saying_nothing_leaves_the_config_alone(tmp_path):
    from wilbyte.config import load_config

    config = load_config()

    assert prefs.apply(config, tmp_path / "none.json") is config


def test_the_weekend_slots_are_actually_used(tmp_path):
    """The point of the whole thing: with weekends on, a Friday post is
    followed by Saturday rather than by Monday."""
    from wilbyte.config import load_config
    from wilbyte.scheduler import next_open_slots

    store = tmp_path / "prefs.json"
    prefs.set_weekends(True, store)
    schedule = prefs.apply(load_config(), store).schedule
    friday = datetime(2026, 8, 21, 8, tzinfo=EASTERN)

    slots = next_open_slots(set(), 3, schedule, now=friday)

    assert [slot.date() for slot in slots] == [
        date(2026, 8, 21), date(2026, 8, 22), date(2026, 8, 23),
    ]


def test_with_weekends_off_the_weekend_is_still_skipped(tmp_path):
    from wilbyte.config import load_config
    from wilbyte.scheduler import next_open_slots

    schedule = prefs.apply(load_config(), tmp_path / "none.json").schedule
    friday = datetime(2026, 8, 21, 8, tzinfo=EASTERN)

    slots = next_open_slots(set(), 2, schedule, now=friday)

    assert [slot.date() for slot in slots] == [date(2026, 8, 21), date(2026, 8, 24)]


def test_describe_says_which_days(tmp_path):
    from wilbyte.config import load_config

    store = tmp_path / "prefs.json"
    config = load_config()

    assert "Weekdays only" in prefs.describe_days(config, store)

    prefs.set_weekends(True, store)
    assert "Every day" in prefs.describe_days(config, store)


# ------------------------------------------------ the whole point, end to end


def test_opening_the_weekend_pulls_a_weekday_queue_forward(tmp_path):
    """Saturday, with Monday/Tuesday/Wednesday booked: the three posts come
    forward onto Saturday, Sunday and Monday."""
    from wilbyte.config import load_config
    from wilbyte.scheduler import next_open_slots

    store = tmp_path / "prefs.json"
    prefs.set_weekends(True, store)
    schedule = prefs.apply(load_config(), store).schedule

    posts = [
        post("a", "First", at(24)),
        post("b", "Second", at(25)),
        post("c", "Third", at(26)),
    ]
    saturday = datetime(2026, 8, 22, 8, tzinfo=EASTERN)
    booked = {date(2026, 8, 24), date(2026, 8, 25), date(2026, 8, 26)}
    booked -= rearrange.held_days(posts, EASTERN)

    moves = rearrange.pair(posts, next_open_slots(booked, 3, schedule, now=saturday))

    assert [move.now.date() for move in moves] == [
        date(2026, 8, 22), date(2026, 8, 23), date(2026, 8, 24),
    ]
    assert [move.title for move in moves] == ["First", "Second", "Third"]


def test_a_slot_too_close_to_now_is_not_offered(tmp_path):
    """Saturday 10am has already gone by 2pm. The post goes to Sunday, and
    getting it out today is what `publish` is for."""
    from wilbyte.config import load_config
    from wilbyte.scheduler import next_open_slots

    store = tmp_path / "prefs.json"
    prefs.set_weekends(True, store)
    schedule = prefs.apply(load_config(), store).schedule
    saturday_afternoon = datetime(2026, 8, 22, 14, tzinfo=EASTERN)

    slots = next_open_slots(set(), 1, schedule, now=saturday_afternoon)

    assert slots[0].date() == date(2026, 8, 23)


# ---------------------------------------------- giving up the 10am rule for today

# Ten in the morning has almost always gone by the time somebody decides they
# want something out today, so the day was being skipped and the answer to "can
# we get this out this afternoon" was "no, Monday".


def test_today_is_offered_at_the_soonest_time_gh_l_will_take():
    from wilbyte.config import load_config
    from wilbyte.scheduler import next_open_slots

    schedule = load_config().schedule
    monday_afternoon = datetime(2026, 8, 24, 14, 30, tzinfo=EASTERN)

    (slot,) = next_open_slots(set(), 1, schedule, now=monday_afternoon, include_today=True)

    assert slot.date() == date(2026, 8, 24)
    assert slot == monday_afternoon + timedelta(minutes=schedule.min_lead_minutes)


def test_without_asking_the_afternoon_still_waits_for_tomorrow():
    """The 10:00 rule is the rule. This is an exception somebody has to ask for."""
    from wilbyte.config import load_config
    from wilbyte.scheduler import next_open_slots

    monday_afternoon = datetime(2026, 8, 24, 14, 30, tzinfo=EASTERN)

    (slot,) = next_open_slots(set(), 1, load_config().schedule, now=monday_afternoon)

    assert slot == datetime(2026, 8, 25, 10, tzinfo=EASTERN)


def test_only_today_gives_up_the_rule():
    """Every day after it is back to 10:00 - this is a one-off, not a new time."""
    from wilbyte.config import load_config
    from wilbyte.scheduler import next_open_slots

    monday_afternoon = datetime(2026, 8, 24, 14, 30, tzinfo=EASTERN)

    slots = next_open_slots(
        set(), 3, load_config().schedule, now=monday_afternoon, include_today=True
    )

    assert slots[0].hour == 14
    assert [(s.date(), s.hour) for s in slots[1:]] == [
        (date(2026, 8, 25), 10), (date(2026, 8, 26), 10),
    ]


def test_a_morning_request_still_gets_ten_oclock():
    """Asked at 8am, today's slot has not gone anywhere - use it as it is."""
    from wilbyte.config import load_config
    from wilbyte.scheduler import next_open_slots

    monday_morning = datetime(2026, 8, 24, 8, tzinfo=EASTERN)

    (slot,) = next_open_slots(
        set(), 1, load_config().schedule, now=monday_morning, include_today=True
    )

    assert slot == datetime(2026, 8, 24, 10, tzinfo=EASTERN)


def test_late_at_night_the_lead_time_would_land_tomorrow_so_today_is_dropped():
    """23:55 plus the lead is tomorrow, and tomorrow has its own slot coming.
    Booking both would put two posts on one day."""
    from wilbyte.config import load_config
    from wilbyte.scheduler import next_open_slots

    late = datetime(2026, 8, 24, 23, 55, tzinfo=EASTERN)

    slots = next_open_slots(set(), 2, load_config().schedule, now=late, include_today=True)

    assert [slot.date() for slot in slots] == [date(2026, 8, 25), date(2026, 8, 26)]


def test_a_taken_today_is_still_taken():
    """Asking for today doesn't mean posting twice in one day."""
    from wilbyte.config import load_config
    from wilbyte.scheduler import next_open_slots

    monday_afternoon = datetime(2026, 8, 24, 14, 30, tzinfo=EASTERN)

    (slot,) = next_open_slots(
        {date(2026, 8, 24)}, 1, load_config().schedule,
        now=monday_afternoon, include_today=True,
    )

    assert slot.date() == date(2026, 8, 25)


def test_a_weekend_today_needs_the_weekend_on_as_well(tmp_path):
    """Today being allowed is not the same as Saturday being a posting day."""
    from wilbyte.config import load_config
    from wilbyte.scheduler import next_open_slots

    saturday = datetime(2026, 8, 22, 14, tzinfo=EASTERN)
    weekdays_only = prefs.apply(load_config(), tmp_path / "none.json").schedule

    (slot,) = next_open_slots(set(), 1, weekdays_only, now=saturday, include_today=True)
    assert slot.date() == date(2026, 8, 24), "Saturday is not a posting day yet"

    store = tmp_path / "prefs.json"
    prefs.set_weekends(True, store)
    both = prefs.apply(load_config(), store).schedule

    (slot,) = next_open_slots(set(), 1, both, now=saturday, include_today=True)
    assert slot == saturday + timedelta(minutes=both.min_lead_minutes)


@pytest.mark.parametrize(
    "said",
    [
        "https://youtu.be/abc12345678 today",
        "today https://youtu.be/abc12345678",
        "https://youtu.be/abc12345678 now",
        "post this asap https://youtu.be/abc12345678",
    ],
)
def test_asking_for_today_is_read_off_the_message(said):
    assert mentions.parse(said).today is True


def test_not_asking_for_today_leaves_the_rule_alone():
    assert mentions.parse("https://youtu.be/abc12345678").today is False


def test_a_word_inside_a_link_is_not_somebody_asking_for_today():
    """A video called "Start Now" is not a request to publish this afternoon."""
    assert mentions.parse("https://www.youtube.com/watch?v=nowornever1").today is False


def test_rearrange_can_be_asked_to_use_today():
    assert mentions.parse("rearrange today").today is True
    assert mentions.parse("rearrange").today is False


# --------------------------------------- saying why, once, when it all fails

# GHL refused a queue of fifteen and sent back the same 400 fifteen times. The
# message that reached Discord was the reason cut off at 120 characters,
# repeated - too long to read and missing the only part that mattered.


def test_one_failure_is_reported_as_it_is():
    said = rearrange.explain_failures(["A Post — HTTP 400: something specific"])

    assert "something specific" in said


def test_the_same_reason_is_given_once_not_once_per_post():
    problems = [f"Post {n} — HTTP 400: blogId is required" for n in range(15)]

    said = rearrange.explain_failures(problems)

    assert said.count("blogId is required") == 1
    assert "All 15 were refused for the same reason" in said


def test_the_posts_it_happened_to_are_still_named():
    problems = [f"Post {n} — HTTP 400: same" for n in range(15)]

    said = rearrange.explain_failures(problems)

    assert "Post 0, Post 1, Post 2" in said
    assert "12 more" in said


def test_different_reasons_are_all_listed():
    problems = ["A — one thing", "B — another thing"]

    said = rearrange.explain_failures(problems)

    assert "one thing" in said and "another thing" in said


def test_nothing_wrong_says_nothing():
    assert rearrange.explain_failures([]) == ""
