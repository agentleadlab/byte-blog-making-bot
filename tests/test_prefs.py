"""Moving the calendar's starting point from Discord.

Kept out of `config/wilbyte.toml` deliberately: that file is tracked, and
editing it on the Mac stops the auto-update fast-forwarding, which strands RYTE
on old code. That failure has already cost a day.
"""

from datetime import date

import pytest

from wilbyte import prefs

TODAY = date(2026, 8, 18)  # a Tuesday


# --------------------------------------------------------------- reading a day


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2026-08-18", date(2026, 8, 18)),
        ("Aug 18", date(2026, 8, 18)),
        ("aug 18", date(2026, 8, 18)),
        ("August 18", date(2026, 8, 18)),
        ("18 Aug", date(2026, 8, 18)),
        ("8/18", date(2026, 8, 18)),
        ("Sept 3", date(2026, 9, 3)),
        ("today", TODAY),
        ("tomorrow", date(2026, 8, 19)),
    ],
)
def test_days_are_read_the_way_people_type_them(text, expected):
    assert prefs.parse_day(text, today=TODAY) == expected


def test_a_weekday_name_means_the_next_one():
    assert prefs.parse_day("friday", today=TODAY) == date(2026, 8, 21)


def test_todays_weekday_name_means_next_week_not_today():
    """Saying "Tuesday" on a Tuesday means the one coming, not the one running."""
    assert prefs.parse_day("tuesday", today=TODAY) == date(2026, 8, 25)


def test_a_month_already_past_this_year_rolls_forward():
    assert prefs.parse_day("Feb 3", today=date(2026, 12, 1)) == date(2027, 2, 3)


def test_a_month_just_gone_stays_in_this_year():
    """"Aug 18" typed on Aug 20 is a backdate, not eleven months out."""
    assert prefs.parse_day("Aug 18", today=date(2026, 8, 20)) == date(2026, 8, 18)


@pytest.mark.parametrize("text", ["", "   ", "sometime", "the 45th"])
def test_junk_is_refused_with_an_example(text):
    with pytest.raises(prefs.PrefsError, match="Aug 18|Give me a day"):
        prefs.parse_day(text, today=TODAY)


# --------------------------------------------------------------- the floor


def test_setting_a_day_overrides_the_config(tmp_path, config):
    path = tmp_path / "prefs.json"
    prefs.set_earliest_day(date(2026, 8, 18), path)

    assert prefs.apply(config, path).schedule.floor == date(2026, 8, 18)


def test_no_preference_leaves_the_config_alone(tmp_path, config):
    assert prefs.apply(config, tmp_path / "prefs.json") is config


def test_clearing_restores_the_config(tmp_path, config):
    path = tmp_path / "prefs.json"
    prefs.set_earliest_day(date(2026, 8, 18), path)
    prefs.clear_earliest_day(path)

    assert prefs.apply(config, path).schedule.floor == config.schedule.floor


def test_a_corrupt_prefs_file_is_ignored_rather_than_fatal(tmp_path, config):
    """A broken settings file must not stop RYTE from posting."""
    path = tmp_path / "prefs.json"
    path.write_text("{not json")

    assert prefs.apply(config, path) is config


def test_the_floor_pushes_slots_forward(tmp_path, config):
    from wilbyte.scheduler import next_open_slots

    path = tmp_path / "prefs.json"
    prefs.set_earliest_day(date(2099, 9, 7), path)  # a Monday

    slots = next_open_slots(set(), 2, prefs.apply(config, path).schedule)

    assert [s.date() for s in slots] == [date(2099, 9, 7), date(2099, 9, 8)]
