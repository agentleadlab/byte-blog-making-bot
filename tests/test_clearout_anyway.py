"""'@Ryte clear her out still, she disputed' - her is the one named before,
and a dispute goes ahead though the channel isn't a month quiet."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from wilbyte.bot import client
from wilbyte.bot.mentions import parse as parse_mention


def test_her_out_still_she_disputed_is_her_and_go_ahead():
    who, anyway, why = client.clearout_asked("her out still, she disputed")
    assert who == "her" and anyway and why == "a dispute"
    assert client._CLEAROUT_PRONOUN.match(who)


def test_a_plain_name_is_left_alone():
    assert client.clearout_asked("Madison Mcgill") == ("Madison Mcgill", False, "")
    assert client.clearout_asked("<#123456>") == ("<#123456>", False, "")


def test_a_name_with_a_dispute_keeps_the_name():
    who, anyway, why = client.clearout_asked("Madison Mcgill anyway, she charged back")
    assert (who, anyway, why) == ("Madison Mcgill", True, "a dispute")
    who, anyway, why = client.clearout_asked("Madison Mcgill still")
    assert (who, anyway, why) == ("Madison Mcgill", True, "you said to go ahead")


def test_the_mention_reads_as_a_clearout():
    request = parse_mention("<@1> clear her out still, she disputed")
    assert request.action == "clearout"


def test_her_is_the_last_clearout_asked_in_the_channel():
    said = [
        SimpleNamespace(author=SimpleNamespace(bot=True), content="No channel looks like her"),
        SimpleNamespace(author=SimpleNamespace(bot=False), content="clearout Madison Mcgill"),
    ]

    class Channel:
        id = 77

        def history(self, limit, before):
            async def gen():
                for one in said:
                    yield one
            return gen()

    message = SimpleNamespace(channel=Channel(), reference=None)
    assert asyncio.run(client._clearout_earlier(message)) == "Madison Mcgill"


def test_a_dispute_lets_a_channel_under_a_month_go(monkeypatch):
    now = datetime.now(timezone.utc)
    messages = [SimpleNamespace(at=now - timedelta(days=18))]
    monkeypatch.setattr(client.jobs, "row_check", lambda *a: (True, "row", False))
    monkeypatch.setattr(client.jobs, "sheet_check", lambda *a: (True, "sheet"))
    held = asyncio.run(client._check_my_work(None, None, messages, []))
    assert not held[0][0]
    going = asyncio.run(client._check_my_work(None, None, messages, [], anyway="a dispute"))
    assert going[0][0] and "going ahead: a dispute" in going[0][1] and "18 day" in going[0][1]
