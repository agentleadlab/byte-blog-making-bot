"""Who rang the bell, and what they said, remembered across restarts."""

from __future__ import annotations

from wilbyte import bell


def _kept(data, **kw):
    bell.keep(data, **{
        "author_id": 7, "who": "Artur | NOVA |", "where": "ring-da-bell", **kw,
    })


def test_what_somebody_said_comes_back_by_their_id():
    data = bell.load(_missing())
    _kept(data, message_id=11, when="May 08", text="$1548 ethos aged 6/7",
          at="2026-05-08T14:09:00")

    assert [one["text"] for one in bell.theirs(data, 7)] == ["$1548 ethos aged 6/7"]
    assert bell.theirs(data, 99) == []


def test_it_is_kept_in_the_order_it_was_said():
    """The first deep read walks backwards and every catch-up walks forwards,
    so appending alone would leave one person's list in two directions."""
    data = bell.load(_missing())
    _kept(data, message_id=13, when="May 25", text="third", at="2026-05-25T18:46:00")
    _kept(data, message_id=11, when="May 08", text="first", at="2026-05-08T14:09:00")
    _kept(data, message_id=12, when="May 16", text="second", at="2026-05-16T17:51:00")

    assert [one["text"] for one in bell.theirs(data, 7)] == [
        "first", "second", "third",
    ]


def test_the_same_message_twice_is_kept_once():
    data = bell.load(_missing())
    for _ in range(3):
        _kept(data, message_id=11, when="May 08", text="$1548", at="2026-05-08T14:09")

    assert len(bell.theirs(data, 7)) == 1


def test_only_the_last_few_of_anybodys():
    """A record of who sold what, not a copy of the channel."""
    data = bell.load(_missing())
    for mark in range(60):
        _kept(data, message_id=mark, when="May", text=f"sale {mark:02d}",
              at=f"2026-05-01T00:{mark:02d}:00")

    kept = bell.theirs(data, 7)
    assert len(kept) == bell.KEEP_EACH
    assert kept[-1]["text"] == "sale 59", "it kept the oldest and dropped the newest"


def test_how_far_a_channel_has_been_read_is_remembered():
    data = bell.load(_missing())

    assert bell.since(data, "1") == ""

    bell.read_to(data, "1", "12345")

    assert bell.since(data, "1") == "12345"
    assert bell.since(data, "2") == ""


def test_nothing_is_remembered_about_a_channel_never_read():
    assert bell.read_to(bell.load(_missing()), "1", "") is None


def test_somebody_cleared_out_is_forgotten():
    data = bell.load(_missing())
    _kept(data, message_id=11, when="May", text="x", at="2026-05-01T00:00")
    bell.forget(data, 7)

    assert bell.theirs(data, 7) == []


def test_it_survives_a_restart(tmp_path):
    where = tmp_path / "bell.json"
    data = bell.load(where)
    _kept(data, message_id=11, when="May 08", text="$1548 ethos aged 6/7",
          at="2026-05-08T14:09:00")
    bell.read_to(data, "1", "11")
    bell.save(data, where)

    again = bell.load(where)

    assert [one["text"] for one in bell.theirs(again, 7)] == ["$1548 ethos aged 6/7"]
    assert bell.since(again, "1") == "11"


def test_a_half_written_file_is_the_same_as_no_file(tmp_path):
    """Nothing here is the only copy of anything - the channel is still there
    to read again."""
    where = tmp_path / "bell.json"
    where.write_text('{"said": {"7": [', encoding="utf-8")

    assert bell.load(where) == {"channels": {}, "said": {}}


def _missing():
    from pathlib import Path

    return Path("/nowhere/at/all/bell.json")
