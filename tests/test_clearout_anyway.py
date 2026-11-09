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


# "I checked the channels one by one see if there are quiet channels ryte
# missed ... there are alot of them" - a pasted list, misspellings and all.
from wilbyte import clearout, quietauto  # noqa: E402

LIST = """<@1> clearout
Jaontay Foster
Tre Howe
Leonrdo Lopez
Thoms Barksi
Ceth Stocchero
Sebastian guerrero"""


def _channels(*names):
    return [clearout.Channel(channel_id=str(at), name=name, category="") for at, name in enumerate(names, start=1)]


def test_a_pasted_list_is_names_one_a_line():
    assert clearout.named_list(LIST)[:3] == ["Jaontay Foster", "Tre Howe", "Leonrdo Lopez"]
    assert clearout.named_list("<@1> clear these out:\nTre Howe\nAlma Garcia") == ["Tre Howe", "Alma Garcia"]
    assert clearout.named_list("<@1> clearout Tre Howe\nAlma Garcia") == ["Tre Howe", "Alma Garcia"]
    assert clearout.named_list("<@1> clearout Madison Mcgill") == []
    assert client.clearout_names(LIST)


def test_each_name_finds_its_channel_and_a_misspelling_is_marked():
    channels = _channels("jaontay-foster-iul", "tre-howe-fex", "leonardo-lopez-mtg", "thomas-barski-vet",
                         "seth-stocchero-iul", "sebastian-guerrero-mtg", "sebastian-guerrero-iul")
    found, missing, several = client._resolve_named(clearout.named_list(LIST), channels)
    got = {name: (one.name, guessed) for name, one, guessed in found}
    assert got["Tre Howe"] == ("tre-howe-fex", False)
    assert got["Leonrdo Lopez"] == ("leonardo-lopez-mtg", True)
    assert got["Thoms Barksi"] == ("thomas-barski-vet", True)
    assert got["Ceth Stocchero"] == ("seth-stocchero-iul", True)
    # Two channels of the same person is a question, not a pick.
    assert [name for name, _hits in several] == ["Sebastian guerrero"]
    assert missing == []


def test_two_people_as_near_is_no_guess():
    assert clearout.closest_channels("Jon Smit", _channels("jon-smith-iul", "jon-smitt-vet")) == []


def test_why_a_channel_was_held_is_kept(tmp_path):
    from datetime import datetime, timezone

    path = tmp_path / "quiet-auto.json"
    quietauto.left_for_a_person(["11", "12"], datetime.now(timezone.utc), path,
                                why={"11": "no sheet link found"})
    assert quietauto.why_left(path) == {"11": "no sheet link found"}
    assert quietauto.leave_alone(datetime.now(timezone.utc), quietauto.load(path)) == {"11", "12"}


def test_quiet_with_everything_held_says_which_and_why(monkeypatch):
    sent = []

    class Responder:
        requester_id = 1

        async def send(self, text="", **_kw):
            sent.append(text)

    class View:
        def __init__(self, **_kw):
            self.confirmed = False

        async def wait(self):
            return None

    monkeypatch.setattr(client.views, "ConfirmView", View)
    monkeypatch.setattr(quietauto, "why_left", lambda: {"5": "their other channel #x is still in use"})
    held = [clearout.Channel(channel_id="5", name="tre-howe-fex", category="", readable=True)]
    config = SimpleNamespace(discord=SimpleNamespace(approval_timeout_seconds=1))
    asyncio.run(client._the_ones_left(None, Responder(), config, held, datetime.now(timezone.utc)))
    assert "<#5>" in sent[-1] and "still in use" in sent[-1] and "1 quiet channel" in sent[-1]


def test_ryte_s_messages_stay_short_and_on_the_one_point():
    # "broo" - a 900-character follow-up about an earlier order Rook had
    # already said was separate, the card's wording and 98 of 100 aged.
    from wilbyte import rytevoice

    assert rytevoice.MOST_CHARS <= 500
    assert "under 60 words" in rytevoice._WHO
    assert "separate or earlier order" in rytevoice._WHO
    assert "aged pulls are checked separately" in rytevoice._WHO
    assert "No deadlines or threats" in rytevoice._WHO
