"""Videos announced while RYTE was offline are written up when it's back.

"i have no wifi right now ... dont want him missing posting or all of that
once wifi comes back in". An announcement is a message, and Discord does not
send a bot the messages it missed.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from wilbyte import state, waiting, watchedseen
from wilbyte.bot import client


def _video(message_id, video_id, *, text="New video!"):
    return NS(id=message_id, content=f"{text} https://www.youtube.com/watch?v={video_id}", embeds=[],
              channel=NS(id=1))


class Channel:
    def __init__(self, messages):
        self.messages = messages
        self.asked = []

    async def history(self, **kw):
        self.asked.append(kw)
        for one in self.messages:
            yield one


class Bot:
    def __init__(self, channel):
        self.config = NS(secrets=NS(discord_watch_channel_ids=("1",)))
        self.channel = channel
        self.run_lock = asyncio.Lock()
        self.ticks = 0

    def get_channel(self, wanted):
        return self.channel if wanted == 1 else None

    def is_closed(self):
        self.ticks += 1
        return self.ticks > 1


@pytest.fixture(autouse=True)
def _nowhere_real(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "DEFAULT_LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(waiting, "WAITING_PATH", tmp_path / "waiting.json")


def _missed(channel):
    return [one.id for one in asyncio.run(client.missed_announcements(Bot(channel)))]


def test_a_video_announced_while_away_is_found():
    channel = Channel([_video(10, "AAAAAAAAAAA"), NS(id=11, content="good morning", embeds=[])])
    assert _missed(channel) == [10]


def test_what_was_already_done_or_is_waiting_is_left_alone():
    ledger = state.Ledger.load()
    from wilbyte.state import LedgerEntry

    ledger.entries["DONEDONEDON"] = LedgerEntry(video_id="DONEDONEDON", title="t", url_slug="s",
                                                scheduled_at=None, ghl_post_id=None, processed_at="")
    ledger.freed["DELETEDDELE"] = "2026-09-29"
    ledger.save()
    queue = waiting.Queue.load()
    queue.add("https://www.youtube.com/watch?v=CAPTIONSCAP", title="t", channel_id=1)
    watchedseen.add(13)

    channel = Channel([
        _video(10, "DONEDONEDON"), _video(11, "DELETEDDELE"), _video(12, "CAPTIONSCAP"),
        _video(13, "SEENSEENSEE"), _video(14, "MISSEDMISSE"),
    ])
    assert _missed(channel) == [14]


def test_the_first_time_it_reads_back_a_day_and_then_three():
    channel = Channel([])
    _missed(channel)
    first = channel.asked[-1]["after"]
    assert timedelta(hours=23) < datetime.now(timezone.utc) - first < timedelta(hours=25)

    watchedseen.add(1)
    _missed(channel)
    assert datetime.now(timezone.utc) - channel.asked[-1]["after"] > timedelta(days=2, hours=23)
    assert channel.asked[-1]["oldest_first"] is True


def test_a_video_taken_in_hand_is_never_taken_again(monkeypatch):
    from wilbyte.bot import jobs

    ran = []

    async def running(bot, responder, links, *a, **kw):
        ran.append(links)

    monkeypatch.setattr(jobs, "waiting_on_captions", lambda link: ("transcript", ""))
    monkeypatch.setattr(client, "_execute_run", running)
    monkeypatch.setattr(client, "_post_channel", lambda bot: NS(id=5, send=None))

    class Said:
        def __init__(self, *a, **k):
            self.channel_id = 5

        async def send(self, *a, **k):
            pass

    monkeypatch.setattr(client, "ChannelResponder", Said)
    bot = Bot(Channel([]))
    asyncio.run(client.handle_watched(bot, _video(20, "NEWNEWNEWNE")))

    assert ran and "20" in watchedseen.load()
    assert _missed(Channel([_video(20, "NEWNEWNEWNE")])) == []


def test_mid_run_it_is_left_for_the_catch_up(monkeypatch):
    said = []

    class Said:
        def __init__(self, *a, **k):
            self.channel_id = 5

        async def send(self, text, **k):
            said.append(text)

    monkeypatch.setattr(client, "ChannelResponder", Said)
    monkeypatch.setattr(client, "_post_channel", lambda bot: NS(id=5))
    bot = Bot(Channel([]))

    async def busy():
        async with bot.run_lock:
            await client.handle_watched(bot, _video(30, "BUSYBUSYBUS"))

    asyncio.run(busy())
    assert said == ["A new video just landed but I'm mid-run — I'll write it up as soon as this batch is done."]
    assert "30" not in watchedseen.load()
    assert _missed(Channel([_video(30, "BUSYBUSYBUS")])) == [30]


def test_the_catch_up_writes_up_what_was_missed(monkeypatch):
    handled = []

    async def missed(bot):
        return [_video(40, "MISSEDMISSE")]

    async def handling(bot, message):
        handled.append(message.id)

    monkeypatch.setattr(client, "missed_announcements", missed)
    monkeypatch.setattr(client, "handle_watched", handling)
    monkeypatch.setattr(client, "WATCH_CATCH_UP_SECONDS", 0)

    asyncio.run(client.watch_catch_up_loop(Bot(Channel([]))))
    assert handled == [40]


def test_a_failed_catch_up_does_not_stop_it(monkeypatch):
    async def broken(bot):
        raise RuntimeError("no wifi")

    monkeypatch.setattr(client, "missed_announcements", broken)
    monkeypatch.setattr(client, "WATCH_CATCH_UP_SECONDS", 0)
    asyncio.run(client.watch_catch_up_loop(Bot(Channel([]))))
