"""Ryte The Goat: every channel RYTE talks in, copied into its twin."""

from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace as NS

import discord
import pytest

from wilbyte.bot import mirror


class Place:
    """A channel, as far as sending goes."""

    def __init__(self, cid, *, fails=False):
        self.id, self.fails = cid, fails

    async def _get_channel(self):
        return self


@pytest.fixture
def sent(monkeypatch):
    """What reached Discord, in order: (channel id, content, kwargs)."""
    got, edits, made = [], [], [100]

    async def send(where, content=None, **kw):
        if getattr(where, "fails", False):
            raise RuntimeError("Missing Access")
        for key in ("file", "files"):
            if kw.get(key) is not None:
                files = kw[key] if key == "files" else [kw[key]]
                kw[key] = [one.fp.read() for one in files]
        got.append((where.id, content, kw))
        made[0] += 1
        return NS(id=made[0], channel=where)

    async def edit(message, **kw):
        edits.append((message.id, kw))
        return message

    places = {1: Place(1), 2: Place(2), 4: Place(4), 7: Place(7), 9: Place(9, fails=True)}
    monkeypatch.setattr(mirror, "_SEND", send)
    monkeypatch.setattr(mirror, "_EDIT", edit)
    monkeypatch.setattr(mirror, "_CLIENT", NS(get_channel=places.get))
    monkeypatch.setattr(mirror, "PAIRS", {1: 2, 3: 4, 8: 9})
    monkeypatch.setattr(mirror, "TAGS_IN_COPIES_ONLY", True)
    monkeypatch.setattr(mirror, "_COPIES", mirror.OrderedDict())
    return NS(got=got, edits=edits)


def test_pairs_are_read_however_they_are_written():
    assert mirror.parse_pairs("111:222, 333>444 ; #555:#666") == {111: 222, 333: 444, 555: 666}
    assert mirror.parse_pairs("junk, 1:, :2, a:b") == {}


def test_a_channel_is_never_copied_into_itself_or_into_another_original():
    assert mirror.parse_pairs("1:1") == {}
    # 2 is an original too: copying 1 into it would copy a copy
    assert mirror.parse_pairs("1:2, 2:3") == {2: 3}


def test_a_message_goes_to_its_twin_and_only_the_twin_tags(sent):
    """"Ryte The Goat only" - the original still reads @Luna and tells nobody."""
    card = discord.Embed(title="Shelby")

    got = asyncio.run(mirror._send(Place(1), "<@42> new text", embed=card,
                                   reference=NS(), mention_author=False))

    (mine, theirs) = sent.got
    assert mine[0] == 1 and mine[1] == "<@42> new text"
    assert mine[2]["allowed_mentions"].users is False
    assert theirs[0] == 2 and theirs[1] == "<@42> new text" and theirs[2]["embed"] is card
    assert "allowed_mentions" not in theirs[2], "the copy should be the one that pings"
    assert "reference" not in theirs[2], "a reply can't point at another channel's message"
    assert got.id == 101 and mirror.copy_of_message(101).id == 102


def test_without_the_switch_both_tag_as_they_always_did(sent, monkeypatch):
    monkeypatch.setattr(mirror, "TAGS_IN_COPIES_ONLY", False)

    asyncio.run(mirror._send(Place(1), "<@42> hi"))

    assert "allowed_mentions" not in sent.got[0][2]


def test_a_channel_with_no_twin_is_left_alone(sent):
    asyncio.run(mirror._send(Place(7), "hi", reference=NS()))

    assert [(where, said) for where, said, _ in sent.got] == [(7, "hi")]
    assert "reference" in sent.got[0][2] and "allowed_mentions" not in sent.got[0][2]


def test_files_arrive_in_both(sent):
    asyncio.run(mirror._send(
        Place(1), "📘", file=discord.File(io.BytesIO(b"## playbook"), filename="p.md"),
    ))

    assert [kw["file"] for _, _, kw in sent.got] == [[b"## playbook"], [b"## playbook"]]


def test_the_copy_failing_never_costs_the_original(sent):
    got = asyncio.run(mirror._send(Place(8), "<@42> hi"))

    assert got.id == 101 and [where for where, _, _ in sent.got] == [8]


def test_an_edit_is_made_to_the_copy_too(sent):
    original = asyncio.run(mirror._send(Place(1), "Working on it…"))
    original.channel = Place(1)

    asyncio.run(mirror._edit(original, content="✅ Posted", attachments=[]))

    assert sent.edits[0][0] == 101 and sent.edits[0][1]["allowed_mentions"].users is False
    assert sent.edits[1] == (102, {"content": "✅ Posted"})


class Buttons:
    def __init__(self):
        self.children = [NS(disabled=False), NS(disabled=False)]
        self.done = asyncio.Event()

    def is_finished(self):
        return self.done.is_set()

    async def wait(self):
        await self.done.wait()


def test_buttons_are_in_both_and_both_grey_out_once_answered(sent):
    """A press in either answers it; the one not pressed must not sit there
    looking live."""
    async def go():
        view = Buttons()
        await mirror._send(Place(1), "Delete #x?", view=view)
        view.done.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return view

    view = asyncio.run(go())

    assert [kw.get("view") for _, _, kw in sent.got] == [view, view]
    assert [mark for mark, _ in sent.edits] == [101, 102]
    assert all(item.disabled for item in view.children)


def _message(cid):
    return NS(channel=NS(id=cid))


def test_what_works_in_a_channel_works_in_its_twin(sent):
    from wilbyte.bot import client

    config = NS(secrets=NS(discord_dispute_channel_id="3", discord_payment_channel_id="1",
                           discord_sop_channel_ids=["3"]))

    assert client.is_dispute(_message(4), config) and client.is_dispute(_message(3), config)
    assert not client.is_dispute(_message(2), config)
    assert client.is_payment(_message(2), config)
    assert client.is_sop_channel(_message(4), config)


def test_ryte_answers_in_every_twin(sent):
    from wilbyte.bot import client

    config = NS(secrets=NS(discord_channel_ids=["1"], discord_sop_channel_ids=[],
                           ringcentral_channel_id="", ringcentral_shared_channel_id="",
                           discord_role_ids=[]))

    assert client.is_allowed(channel_id=2, user=NS(), config=config)[0]
    assert not client.is_allowed(channel_id=5, user=NS(), config=config)[0]


def test_a_reply_to_the_twin_of_a_ringcentral_card_teaches_the_same(sent, monkeypatch):
    from wilbyte import ringtexts
    from wilbyte.bot import client, jobs

    monkeypatch.setattr(client, "_RING_POSTS", set())
    ringtexts.save({"pending": [{"id": "5"}]})
    original = asyncio.run(mirror._send(Place(1), "<@42>", embed=discord.Embed(title="x")))

    asyncio.run(client._remember_post(original, "5"))

    assert client._RING_POSTS == {101, 102}
    assert jobs.ring_told("102", "we call these") == "ping"


# ------------------------------------------ RYTE's own news to #announcements


def test_ryte_announcing_himself_goes_to_announcements_not_the_channels_twin(sent, monkeypatch):
    """"instead of this going to blog copywriter channel, it will go to
    announcement - original server is untouched"."""
    from wilbyte.bot import client

    monkeypatch.setattr(mirror, "ANNOUNCE_INTO", 4)
    blogs = client.Announcing(Place(1))

    asyncio.run(_say(blogs))

    assert [where for where, _, _ in sent.got] == [1, 4], "went to blogs-copy, not announcements"
    assert blogs.id == 1, "the channel should still read as #blogs to everything else"


async def _say(channel):
    # the channel's own send, as RYTE calls it, through the hook
    import wilbyte.bot.mirror as m

    class Hooked:
        def __init__(self, place):
            self.place, self.id = place, place.id

        async def send(self, content=None, **kw):
            return await m._send(self.place, content, **kw)

    channel._channel = Hooked(channel._channel)
    await channel.send("🔄 Updating myself — back in a moment.")


def test_everything_else_in_that_channel_still_goes_to_its_twin(sent, monkeypatch):
    monkeypatch.setattr(mirror, "ANNOUNCE_INTO", 4)

    asyncio.run(mirror._send(Place(1), "📝 blog card"))

    assert [where for where, _, _ in sent.got] == [1, 2]


def test_announcing_with_no_announcements_channel_set_is_as_before(sent, monkeypatch):
    monkeypatch.setattr(mirror, "ANNOUNCE_INTO", None)

    with mirror.copying_into(mirror.ANNOUNCE_INTO):
        asyncio.run(mirror._send(Place(1), "🔄 Updating myself"))

    assert [where for where, _, _ in sent.got] == [1, 2]


def test_announcing_into_the_channel_itself_is_said_once(sent):
    with mirror.copying_into(1):
        asyncio.run(mirror._send(Place(1), "🔄 Updating myself"))

    assert [where for where, _, _ in sent.got] == [1]


def test_a_channel_with_no_twin_keeps_its_tags_when_its_news_is_copied(sent):
    """Only a paired channel goes quiet. One whose announcements alone are
    copied still tags as it always did."""
    with mirror.copying_into(4):
        asyncio.run(mirror._send(Place(7), "<@42> heads up"))

    assert [where for where, _, _ in sent.got] == [7, 4]
    assert "allowed_mentions" not in sent.got[0][2]
