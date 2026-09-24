"""How each tagged comment was read, so it is read the same way every time."""

from __future__ import annotations

import threading
from types import SimpleNamespace as NS

from wilbyte import tagreads
from wilbyte.bot import jobs

TONY = """Tony and Overnight Orders

Faith tony order top priority CC: Therese and Nicole

Everyone else who paid last night is second

@faithhannahcalla @kharylmayecanizares @thereseguba"""

PEOPLE = {"faithhannahcalla": object(), "kharylmayecanizares": object()}


def _note(comment_id="c1", text=TONY):
    return NS(comment_id=comment_id, text=text)


class Reader:
    """Stands in for Claude. Every call reads a little differently, which is
    the whole problem."""

    def __init__(self):
        self.calls = 0
        self.asked = []

    def __call__(self, config, notes, people):
        self.calls += 1
        self.asked.append([one.comment_id for one in notes])
        said = [
            "Tony order top priority, others who paid last night second",
            "Everyone else who paid last night is second",
        ][(self.calls - 1) % 2]
        return {
            one.comment_id: [{"person": "kharylmayecanizares", "summary": said,
                              "kind": "general"}]
            for one in notes
        }


def _reading(monkeypatch):
    reader = Reader()
    monkeypatch.setattr(jobs, "_read_tags_fresh", reader)
    return reader


def test_the_same_comment_reads_the_same_the_second_time(monkeypatch):
    """Arnold's "Tony and Overnight Orders" came back two different ways
    seconds apart, and the second took the wrong half."""
    reader = _reading(monkeypatch)

    first = jobs._ask_about_tags(None, [_note()], PEOPLE)
    second = jobs._ask_about_tags(None, [_note()], PEOPLE)

    assert first == second
    assert first["c1"][0]["summary"].startswith("Tony order top priority")
    assert reader.calls == 1, "read twice"


def test_only_the_new_comments_are_sent_to_be_read(monkeypatch):
    reader = _reading(monkeypatch)

    jobs._ask_about_tags(None, [_note("c1")], PEOPLE)
    jobs._ask_about_tags(None, [_note("c1"), _note("c2", "Frank book the call")], PEOPLE)

    assert reader.asked == [["c1"], ["c2"]]


def test_an_edited_comment_is_read_again(monkeypatch):
    """An edited comment is a different comment."""
    reader = _reading(monkeypatch)

    jobs._ask_about_tags(None, [_note(text=TONY)], PEOPLE)
    jobs._ask_about_tags(None, [_note(text=TONY + "\nand Jenn too")], PEOPLE)

    assert reader.calls == 2


def test_a_new_checklist_keeper_means_reading_it_again(monkeypatch):
    """Who a job belongs to is decided against who keeps a checklist, and a
    comment read before Therese had one is worth reading again once she has."""
    reader = _reading(monkeypatch)

    jobs._ask_about_tags(None, [_note()], PEOPLE)
    jobs._ask_about_tags(None, [_note()], {**PEOPLE, "thereseguba": object()})

    assert reader.calls == 2


def test_a_comment_with_no_job_in_it_is_not_read_every_time(monkeypatch):
    """"Read and found to hold nothing" is an answer worth keeping too."""
    calls = []

    def nothing(config, notes, people):
        calls.append(1)
        return {}

    monkeypatch.setattr(jobs, "_read_tags_fresh", nothing)

    assert jobs._ask_about_tags(None, [_note()], PEOPLE) == {}
    assert jobs._ask_about_tags(None, [_note()], PEOPLE) == {}
    assert len(calls) == 1


def test_a_reading_that_failed_is_not_remembered(monkeypatch):
    """Otherwise one bad night at Anthropic would be remembered as the
    comment holding no job at all."""
    calls = []

    def breaks(config, notes, people):
        calls.append(1)
        raise RuntimeError("overloaded")

    monkeypatch.setattr(jobs, "_read_tags_fresh", breaks)
    for _ in range(2):
        try:
            jobs._ask_about_tags(None, [_note()], PEOPLE)
        except RuntimeError:
            pass

    assert len(calls) == 2, "remembered a failure as an answer"


def test_two_looks_at_once_make_one_reading(monkeypatch):
    """The watcher and a typed "@RYTE tags" both run in threads, and both
    reading the same comment at once is how it came back two ways."""
    reader = Reader()
    started = threading.Event()

    def slow(config, notes, people):
        started.set()
        threading.Event().wait(0.1)
        return reader(config, notes, people)

    monkeypatch.setattr(jobs, "_read_tags_fresh", slow)
    got = []
    looks = [
        threading.Thread(
            target=lambda: got.append(jobs._ask_about_tags(None, [_note()], PEOPLE))
        )
        for _ in range(2)
    ]
    for one in looks:
        one.start()
    for one in looks:
        one.join()

    assert reader.calls == 1
    assert got[0] == got[1]


def test_it_survives_a_restart(tmp_path):
    where = tmp_path / "reads.json"
    tagreads.save({"c1:ab": [{"summary": "x"}]}, where)

    assert tagreads.load(where) == {"c1:ab": [{"summary": "x"}]}


def test_a_half_written_file_is_the_same_as_no_file(tmp_path):
    where = tmp_path / "reads.json"
    where.write_text("{not json", encoding="utf-8")

    assert tagreads.load(where) == {}


def test_only_the_newest_are_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(tagreads, "KEEP", 3)
    where = tmp_path / "reads.json"
    tagreads.save({f"c{i}": [] for i in range(5)}, where)

    assert list(tagreads.load(where)) == ["c2", "c3", "c4"]
