""""i feel like he's not learning and is going dumb everyday" - lessons kept
and read before asking, and a scorecard instead of a feeling."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from wilbyte import lessons, rytevoice, scorecard

NOW = datetime(2026, 11, 13, 22, 0, tzinfo=timezone.utc)
DAY = (NOW - timedelta(days=1)).isoformat()


def _records():
    return {
        # Real: flagged, then the hub was fixed.
        "1": {"client": "Gabriel Espada", "flag_texts": {"a": {"text": "The hub has 30 ordered, paid for 58", "at": DAY}},
              "open_problems": []},
        # False alarm: Rook said it's right as it is.
        "2": {"client": "Christopher Gallion", "flag_texts": {"b": {"text": "Not on the hub", "at": DAY}},
              "open_problems": ["Not on the hub"], "ok_problems": ["Not on the hub"],
              "ok_by": "Rook – “AGED order, no hub row needed”"},
        # Still open.
        "3": {"client": "Derek Broschinsky", "flag_texts": {"c": {"text": "Lead type: hub has Plus, paid Standard",
                                                                  "at": DAY}},
              "open_problems": ["Lead type: hub has Plus, paid Standard"]},
        # Old - outside the week.
        "4": {"client": "Old", "flag_texts": {"d": {"text": "Not on the hub", "at": (NOW - timedelta(days=20)).isoformat()}},
              "open_problems": []},
    }


def test_the_scorecard_counts_real_false_and_open(tmp_path):
    lessons.missed("Bryce Evans - paid, nobody set it up", path=tmp_path / "l.json")
    got = scorecard.score(_records(), now=NOW, missed=lessons.misses(tmp_path / "l.json"))
    assert (got["flags"], got["real"], got["false alarm"], got["open"], got["misses"]) == (3, 1, 1, 1, 1)
    assert got["right"] == 50
    text = scorecard.said(got)
    assert "Right 50% of the time" in text and "not set up 1" in text and "Christopher Gallion" in text
    assert "1 real catches, 1 false alarms (50% right), 1 missed" in scorecard.one_line(got)


def test_flags_explained_away_become_lessons_read_before_asking(tmp_path):
    path = tmp_path / "l.json"
    assert lessons.teach("Aged orders never have a hub row", by="Franklin", path=path) == 1
    got = lessons.for_question(_records(), ["Not on the hub"], path=path)
    assert got[0] == "Franklin: Aged orders never have a hub row"
    assert any("Christopher Gallion" in one and "AGED order" in one for one in got)
    # A lesson about another kind of problem isn't read into this one.
    assert not any("Derek" in one for one in got)
    assert lessons.forget(1, path=path) == "Aged orders never have a hub row"
    assert lessons.forget(1, path=path) == ""


def test_a_lesson_that_explains_it_means_not_asking(monkeypatch):
    monkeypatch.setattr(rytevoice, "_ask_claude", lambda *a: {"skip": True, "lesson": "aged orders have no hub row",
                                                              "message": ""})
    assert rytevoice.compose_or_skip(None, {"lessons_from_the_team": ["Franklin: aged has no hub row"]}) == (
        "", "aged orders have no hub row")
    # No lessons given: never skipped on a whim - the plain question.
    monkeypatch.setattr(rytevoice, "compose", lambda config, said: "Row 1 is wrong.")
    assert rytevoice.compose_or_skip(None, {}) == ("Row 1 is wrong.", "")


def test_the_commands():
    from wilbyte.bot.mentions import parse

    assert (parse("<@1> lesson: aged orders never have a hub row").action, parse(
        "<@1> lesson: aged orders never have a hub row").brief) == ("lessonadd", "aged orders never have a hub row")
    assert parse("<@1> you missed Bryce Evans - paid, nobody set it up").action == "lessonmiss"
    assert parse("<@1> forget lesson 2").brief == "2"
    assert parse("<@1> lessons").action == "lessonlist"
    assert parse("<@1> scorecard").action == "scorecard"
    assert parse("<@1> how are you doing").action == "scorecard"
    assert parse("<@1> did we miss anything").action == "missedsetup"
