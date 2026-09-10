"""The notebook: what RYTE picked up while working, kept so it can be said."""

from __future__ import annotations

from datetime import date

import pytest

from wilbyte import noticed


@pytest.fixture
def book(tmp_path):
    return tmp_path / "noticed.json"


def test_one_sighting_is_not_worth_saying(book):
    """Once is a typo. It goes in the notebook and stays there."""
    noticed.note("unplaced", "PHX STNDRD", on=date(2026, 9, 1), path=book)

    found = noticed.notes(path=book)

    assert [one.subject for one in found] == ["PHX STNDRD"]
    assert noticed.worth_saying(found) == []


def test_twice_on_different_days_is(book):
    """Twice is somebody's habit, and a habit is what a suggestion changes."""
    noticed.note("unplaced", "PHX STNDRD", on=date(2026, 9, 1), path=book)
    noticed.note("unplaced", "PHX STNDRD", on=date(2026, 9, 2), path=book)

    (one,) = noticed.worth_saying(noticed.notes(path=book))

    assert one.times == 2
    assert (one.first, one.last) == ("2026-09-01", "2026-09-02")


def test_the_same_day_twice_is_one_day(book):
    """The tag watcher looks every minute and sees the same thing each time.
    That is one fact about Tuesday, not four hundred."""
    for _ in range(400):
        noticed.note("no_checklist", "@tretarpley", on=date(2026, 9, 9), path=book)

    (one,) = noticed.notes(path=book)

    assert one.times == 1


def test_the_most_seen_comes_first(book):
    for day in range(1, 6):
        noticed.note("unplaced", "PHX STNDRD", on=date(2026, 9, day), path=book)
    for day in range(1, 3):
        noticed.note("unplaced", "ASCEND", on=date(2026, 9, day), path=book)

    assert [one.subject for one in noticed.notes(path=book)] == ["PHX STNDRD", "ASCEND"]


def test_a_detail_is_kept_and_updated(book):
    noticed.note("unplaced", "2.0", detail="seen on “PHNX 2.0”", on=date(2026, 9, 1), path=book)

    (one,) = noticed.notes(path=book)

    assert one.detail == "seen on “PHNX 2.0”"


def test_a_kind_he_does_not_have_is_ignored(book):
    """Rather than filling the notebook with entries nothing can say anything
    about."""
    noticed.note("whatever", "something", on=date(2026, 9, 1), path=book)

    assert noticed.notes(path=book) == []


def test_an_empty_subject_is_not_a_sighting(book):
    noticed.note("unplaced", "   ", on=date(2026, 9, 1), path=book)

    assert noticed.notes(path=book) == []


# ------------------------------------------------------------------- hushing


def test_a_hushed_thing_stops_being_raised(book):
    noticed.note("unplaced", "OPUS", on=date(2026, 9, 1), path=book)
    noticed.note("unplaced", "OPUS", on=date(2026, 9, 2), path=book)

    assert noticed.hush("unplaced", "OPUS", path=book) is True
    assert noticed.notes(path=book) == []


def test_hushing_by_subject_covers_every_kind_it_was_seen_under(book):
    noticed.note("unplaced", "OTP FEX", on=date(2026, 9, 1), path=book)
    noticed.note("ambiguous", "OTP FEX", on=date(2026, 9, 1), path=book)

    done = noticed.hush_subject("otp fex", path=book)

    assert len(done) == 2
    assert noticed.notes(path=book) == []


def test_hushing_something_he_never_saw_says_so(book):
    assert noticed.hush("unplaced", "NOTHING", path=book) is False
    assert noticed.hush_subject("nothing", path=book) == []


def test_a_hushed_thing_is_still_counted_underneath(book):
    """So `noticed all` can show it, and so a count is not lost by hushing."""
    noticed.note("unplaced", "OPUS", on=date(2026, 9, 1), path=book)
    noticed.hush("unplaced", "OPUS", path=book)
    noticed.note("unplaced", "OPUS", on=date(2026, 9, 2), path=book)

    assert noticed.load(book)["unplaced::OPUS"]["times"] == 2


def test_clearing_one_restarts_its_count(book):
    """It was dealt with. If it comes back, it comes back from zero."""
    noticed.note("carried", "trucker website", on=date(2026, 9, 1), path=book)

    assert noticed.clear("carried", "trucker website", path=book) is True
    assert noticed.notes(path=book) == []


# ---------------------------------------------------------------- forgetting


def test_a_month_of_silence_is_forgotten(book):
    """A word somebody fixed in August should not still be suggested in
    October."""
    noticed.note("unplaced", "OLD WORD", on=date(2026, 7, 1), path=book)
    noticed.note("unplaced", "NEW WORD", on=date(2026, 9, 10), path=book)

    assert [one.subject for one in noticed.notes(path=book)] == ["NEW WORD"]


def test_a_hush_dies_with_the_thing_it_hushed(book):
    noticed.note("unplaced", "OLD WORD", on=date(2026, 7, 1), path=book)
    noticed.hush("unplaced", "OLD WORD", path=book)
    noticed.note("unplaced", "NEW WORD", on=date(2026, 9, 10), path=book)

    assert "unplaced::OLD WORD" not in noticed.load(book).get(noticed.HUSHED, [])


# ------------------------------------------------------------------ reading


def test_a_corrupt_notebook_is_not_an_outage(book):
    """Noticing is a side effect of the work. It must never stop it."""
    book.write_text("{not json", encoding="utf-8")

    assert noticed.load(book) == {}
    assert noticed.notes(path=book) == []


def test_the_line_says_what_it_was_and_how_often():
    one = noticed.Seen(
        kind="unplaced", subject="PHX STNDRD", times=4,
        first="2026-09-01", last="2026-09-08",
    )

    said = noticed.describe(one)

    assert "PHX STNDRD" in said
    assert "4×" in said
    assert "couldn't place" in said


def test_the_prompt_carries_the_counts_and_asks_only_for_suggestions():
    found = [noticed.Seen("unplaced", "PHX STNDRD", 4, "2026-09-01", "2026-09-08")]

    asked = noticed.prompt(found)

    assert "PHX STNDRD" in asked and "4 times" in asked
    assert "Suggest only" in asked


def test_nothing_yet_explains_itself_rather_than_being_blank():
    assert "twice" in noticed.nothing_yet()
