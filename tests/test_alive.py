"""The down alarm - "right now, if Ryte goes down, nobody finds out"."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from wilbyte import alive

NOW = datetime(2026, 10, 5, 21, 45, tzinfo=timezone.utc)


def test_a_restart_isnt_being_down_but_a_sleeping_mac_is():
    assert alive.was_down(NOW, NOW - timedelta(minutes=6)) is None
    assert alive.was_down(NOW, None) is None
    assert alive.was_down(NOW, NOW - timedelta(hours=2, minutes=35)) == timedelta(hours=2, minutes=35)


def test_how_long_and_what_it_says():
    assert alive.how_long(timedelta(hours=2, minutes=35)) == "2 h 35 min"
    assert alive.how_long(timedelta(days=1, hours=3)) == "1 day 3 h"
    said = alive.back_up(NOW - timedelta(hours=2, minutes=35), NOW, ZoneInfo("America/New_York"))
    assert said.startswith("⚠ **I was down** from 3:10 PM to 5:45 PM (2 h 35 min)")


def test_the_beat_is_kept_between_starts():
    alive.beat(NOW)
    assert alive.last_seen() == NOW


def test_the_check_in_is_a_plain_https_visit_and_never_raises(monkeypatch):
    visited = []

    class Answer:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def visit(request, timeout=0):
        visited.append((request.full_url, request.get_method(), request.data))
        return Answer()

    monkeypatch.setattr(alive.urllib.request, "urlopen", visit)
    assert alive.check_in("https://hc-ping.com/abc") is True
    assert visited == [("https://hc-ping.com/abc", "GET", None)], "nothing but the visit"
    assert alive.check_in("http://insecure.example") is False and len(visited) == 1

    def broken(request, timeout=0):
        raise OSError("no wifi")

    monkeypatch.setattr(alive.urllib.request, "urlopen", broken)
    assert alive.check_in("https://hc-ping.com/abc") is False


def _started(monkeypatch, last):
    import asyncio

    from wilbyte.bot import client

    if last is not None:
        alive.beat(last)
    said, checked = [], []

    class Place:
        async def send(self, text=None, **kw):
            said.append(text)

    monkeypatch.setattr(client, "_announce_channel", lambda bot: Place())
    monkeypatch.setattr(alive, "check_in", lambda url, **kw: checked.append(url) or True)

    async def stop(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(client.asyncio, "sleep", stop)
    bot = SimpleNamespace(is_closed=lambda: False, config=SimpleNamespace(
        secrets=SimpleNamespace(healthcheck_url="https://hc-ping.com/abc", discord_notify_user_id="42"),
        schedule=SimpleNamespace(timezone="America/New_York")))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(client.alive_loop(bot))
    return said, checked


def test_back_after_a_long_gap_it_tags_franklin_and_checks_in(monkeypatch):
    said, checked = _started(monkeypatch, datetime.now(timezone.utc) - timedelta(hours=3))
    assert said[0].startswith("<@42>\n⚠ **I was down**")
    assert checked == ["https://hc-ping.com/abc"]
    assert datetime.now(timezone.utc) - alive.last_seen() < timedelta(minutes=1)


def test_after_a_quick_restart_or_a_first_start_it_says_nothing(monkeypatch):
    for last in (datetime.now(timezone.utc) - timedelta(minutes=3), None):
        said, checked = _started(monkeypatch, last)
        assert said == [] and checked == ["https://hc-ping.com/abc"]
