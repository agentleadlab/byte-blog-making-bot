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
