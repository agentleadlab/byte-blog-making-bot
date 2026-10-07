""""this schedule is Philippine Time, i want ryte to add these to Tre's calendar"."""

from datetime import date, time
from types import SimpleNamespace

import pytest

from wilbyte import itinerary

CEBU = [
    [], ["", "Cebu 13-Days Itinerary (Complete)"], ["", "Travel Dates: Oct 9 - Oct 21,2026"],
    ["", "Date", "Time", "Category", "Activity", "Location / Notes", "Links"],
    ["", "Day 1 (Thursday) - PH Team Meetup/ Arrivals"],
    ["", "October 8", "1:30 AM - 2:00 AM", "Travel / Transport", "Frank, Nicole & Marc's arrival", "MCIA - Terminal 1"],
    ["", "", "10:00 AM - 3:00PM", "Sleeping Time"],
    ["", "", "4:00:00 PM - 6:00 PM", "Food & Drink", "Early Dinner"],
    ["", "", "9:00PM +", "Activity / Tour", "Chill at the Cafe"],
    ["", "Day 2 (Friday) - PH Team Meetup/ Arrivals"],
    ["", "Oct 9", "3:30 PM - 4:00 PM", "Travel / Transport", "Travel"],
    ["", "", "", "Activity / Tour", "SM Seaside", "Fun Park or Archery", "FUNPARK"],
    ["", "", "23:30", "Travel / Transport", "Tre & Maya's arrival", "MCIA - Terminal 2"],
]


def _read():
    return itinerary.read(CEBU, today=date(2026, 10, 7))


def test_each_row_lands_on_its_day_and_time():
    got = [(one.day, one.start, one.title) for one in _read().events]
    assert got == [
        (date(2026, 10, 8), time(1, 30), "Frank, Nicole & Marc's arrival"),
        (date(2026, 10, 8), time(16, 0), "Early Dinner"),
        (date(2026, 10, 8), time(21, 0), "Chill at the Cafe"),
        (date(2026, 10, 9), time(15, 30), "Travel"),
        (date(2026, 10, 9), time(23, 30), "Tre & Maya's arrival"),
    ]


def test_sleeping_time_and_a_row_with_no_time_are_left_out_and_said():
    assert [(row, what, why) for row, what, why in _read().skipped] == [
        (7, "Sleeping Time", "Sleeping Time"), (12, "SM Seaside", "no time")]


@pytest.mark.parametrize("said, start, end", [
    ("1:30 AM - 2:00 AM", time(1, 30), time(2, 0)),
    ("10:00 AM - 3:00PM", time(10, 0), time(15, 0)),
    ("4:00:00 PM - 6:00 PM", time(16, 0), time(18, 0)),
    ("6:00PM - 9:00PM", time(18, 0), time(21, 0)),
    ("6:00 - 9:00 PM", time(18, 0), time(21, 0)),
    ("9:00PM +", time(21, 0), None),
    ("23:30", time(23, 30), None),
    ("6:00 PM onwards", time(18, 0), None),
    ("7:30 PM onward", time(19, 30), None),
    ("8 PM until late", time(20, 0), None),
    ("12:00 AM - 1:00 AM", time(0, 0), time(1, 0)),
])
def test_times_written_every_way(said, start, end):
    assert itinerary.times(said) == (start, end)


def test_open_ended_is_an_hour_and_past_midnight_runs_into_the_next_day():
    events = {one.title: one for one in _read().events}
    assert events["Chill at the Cafe"].ends().hour == 22
    late = itinerary.Event(day=date(2026, 10, 9), start=time(22, 0), end=time(1, 0), title="x")
    assert late.ends().day == 10


def test_the_file_is_in_manila_time_and_imports_the_same_twice():
    text = itinerary.ics(_read().events, name="Agent Lead Lab goes to Cebu")
    assert "DTSTART;TZID=Asia/Manila:20261008T013000" in text
    assert "TZOFFSETTO:+0800" in text
    assert "SUMMARY:Frank\\, Nicole & Marc's arrival" in text
    assert "LOCATION:MCIA - Terminal 1" in text
    uids = [line for line in text.split("\r\n") if line.startswith("UID:")]
    again = [line for line in itinerary.ics(_read().events, name="Agent Lead Lab goes to Cebu").split("\r\n")
             if line.startswith("UID:")]
    assert uids == again and len(set(uids)) == len(uids)


def test_the_command_posts_the_file_with_the_eastern_time_said(monkeypatch):
    import asyncio

    from wilbyte.bot import client, jobs, mentions

    assert mentions.parse("<@1> calendar https://docs.google.com/spreadsheets/d/1abcDEFghijklmnopqrstuv/edit").action \
        == "calendar"
    found = _read()
    monkeypatch.setattr(jobs, "itinerary_calendar", lambda config, link: (
        itinerary.ics(found.events, name="Cebu"), "Agent Lead Lab goes to Cebu", found, []))
    sent = []

    class Heard:
        async def send(self, text=None, **kw):
            sent.append((text, kw.get("file")))

    asyncio.run(client._itinerary_calendar(
        Heard(), SimpleNamespace(), "calendar https://docs.google.com/spreadsheets/d/1abcDEFghijklmnopqrstuv/edit"))
    text, file = sent[0]
    assert "5 event(s), Oct 8 to Oct 9" in text
    assert "Oct 8 1:30 AM in Manila, is Oct 7 1:30 PM Eastern" in text
    assert "row 12 “SM Seaside” (no time)" in text
    assert file.filename == "Agent-Lead-Lab-goes-to-Cebu.ics"


def test_a_link_with_no_tab_finds_the_itinerary_tab(monkeypatch):
    """The link had no #gid, so the first tab - "Airbnbs" - was read, and
    "No rows with a date and a time to put in a calendar"."""
    from wilbyte import gsheets
    from wilbyte.bot import jobs

    class Sheets:
        def __init__(self, creds):
            self.read = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def title(self, sheet_id):
            return "Agent Lead Lab goes to Cebu"

        def tabs(self, sheet_id):
            return [{"title": "Airbnbs"}, {"title": "Expenses & Activities to Book"}, {"title": "Cebu Itinerary"}]

        def rows(self, sheet_id, span):
            return CEBU if span.startswith("'Cebu Itinerary'") else [["Name", "Price"], ["Casa", "100"]]

    monkeypatch.setattr(gsheets, "SheetsClient", Sheets)
    monkeypatch.setattr(gsheets, "credentials", lambda secrets: None)
    text, name, found, problems = jobs.itinerary_calendar(
        SimpleNamespace(secrets=None),
        "https://docs.google.com/spreadsheets/d/1j2h8vq1waZXem1U67LroRC13AaTw-sqhg003078OQVI/edit?usp=sharing",
        today=date(2026, 10, 7))

    assert problems == [] and len(found.events) == 5
    assert name == "Agent Lead Lab goes to Cebu · Cebu Itinerary"



# ------------------------------------------- what the 93-event Cebu file got wrong


def _day(*rows, first="Oct 16"):
    head = [["", "Date", "Time", "Category", "Activity", "Location / Notes", "Links"]]
    body = [["", first if n == 0 else "", when, "Activity / Tour", what] for n, (when, what) in enumerate(rows)]
    return itinerary.read(head + body, today=date(2026, 10, 7))


def test_a_time_with_no_am_or_pm_after_dinner_is_the_evening():
    """"Free Time" 8:30-9:30 sat at 8:30 in the morning after a 7 PM dinner."""
    found = _day(("7:00 PM - 8:30 PM", "Dinner"), ("8:30 - 9:30", "Free Time"))
    free = found.events[1]
    assert (free.start, free.end) == (time(20, 30), time(21, 30))


def test_a_morning_time_without_am_or_pm_stays_morning():
    found = _day(("7:00 AM - 8:00 AM", "Breakfast"), ("8:30 - 9:30", "Walk"))
    assert found.events[1].start == time(8, 30)


def test_after_midnight_belongs_to_the_next_day():
    """"Back to Airbnb" at 12 AM after a 10 PM night club landed at the start
    of the same day."""
    found = _day(("10:00 PM - 11:00 PM", "Night Club"), ("12:00 AM - 12:30 AM", "Back to Airbnb"),
                 first="Oct 18")
    back = found.events[1]
    assert back.day == date(2026, 10, 19) and back.start == time(0, 0)


def test_the_next_days_rows_are_unaffected():
    head = [["", "Date", "Time", "Category", "Activity"]]
    rows = head + [["", "Oct 18", "10:00 PM - 11:00 PM", "x", "Night Club"],
                   ["", "Oct 19", "11:00 AM - 12:00 PM", "x", "Prep for Check Out"]]
    found = itinerary.read(rows, today=date(2026, 10, 7))
    assert found.events[1].day == date(2026, 10, 19) and found.events[1].start == time(11, 0)


def test_an_event_too_long_to_be_right_is_put_in_and_listed():
    """"Tumalog Falls" 10:30 PM - 11:00 AM, "Going to Balamban" 12:00 AM - 2:00 PM."""
    found = _day(("10:00 AM - 10:30 AM", "Going to Tumalog Falls"), ("10:30 PM - 11:00 AM", "Tumalog Falls"))
    assert [one.title for one in found.to_check()] == ["Tumalog Falls"]
    assert len(found.events) == 2
