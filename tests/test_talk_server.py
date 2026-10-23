"""Ryte The Goat: RYTE answers there without being tagged. Only there."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from wilbyte.bot import client

RYTE = SimpleNamespace(id=99)
GOAT = SimpleNamespace(id=1, name="Ryte The Goat")
GROWTH = SimpleNamespace(id=2, name="Agent Lead Lab Growth Team")
CONFIG = SimpleNamespace(secrets=SimpleNamespace(discord_talk_guild_id=""))


def said(text, *, guild=GOAT, bot=False, everyone=False, roles=(), reply_to=None, files=()):
    ref = SimpleNamespace(resolved=SimpleNamespace(author=SimpleNamespace(id=reply_to))) if reply_to else None
    return SimpleNamespace(content=text, guild=guild, author=SimpleNamespace(id=5, bot=bot),
                           mention_everyone=everyone, role_mentions=list(roles), reference=ref,
                           attachments=list(files))


@pytest.mark.parametrize("message", [
    said("checklist"), said("how many leads did Gavin get?"), said("<@99> update"),
    said("thanks!", reply_to=99), said("", files=["shot.png"]),
])
def test_answered_in_ryte_the_goat(message):
    assert client.talks_to_ryte(message, CONFIG, RYTE)


@pytest.mark.parametrize("message", [
    said("checklist", guild=GROWTH), said("checklist", guild=None), said("checklist", bot=True),
    said("<@5> can you check this"), said("heads up", everyone=True), said("team", roles=["r"]),
    said("lol same", reply_to=7), said("   "),
])
def test_left_alone(message):
    assert not client.talks_to_ryte(message, CONFIG, RYTE)


def test_found_by_id_when_set():
    config = SimpleNamespace(secrets=SimpleNamespace(discord_talk_guild_id="3"))
    renamed = SimpleNamespace(id=3, name="Franklin's HQ")
    assert client.talks_to_ryte(said("checklist", guild=renamed), config, RYTE)
    assert not client.talks_to_ryte(said("checklist"), config, RYTE)


def test_a_refusal_untagged_says_nothing(monkeypatch):
    import asyncio

    replies = []
    monkeypatch.setattr(client, "is_watched", lambda message, config: False)
    monkeypatch.setattr(client, "is_allowed", lambda **kw: (False, "You need the Lab role."))
    message = said("checklist")
    message.channel = SimpleNamespace(id=10, name="trello-manager")

    async def reply(text, **kw):
        replies.append(text)

    message.reply = reply
    bot = SimpleNamespace(config=SimpleNamespace(secrets=SimpleNamespace(discord_channel_ids=())))
    asyncio.run(client.handle_mention(bot, message, untagged=True))
    assert replies == []
    asyncio.run(client.handle_mention(bot, message))
    assert replies == ["You need the Lab role."]


def test_the_server_renamed_the_goat_and_the_best():
    """Ryte went quiet untagged once the server was renamed "The Goat & The
    Best" - and Gabi's channels in it are Gabi's."""
    merged = SimpleNamespace(id=4, name="The Goat & The Best")
    ryte_side = said("ryte", guild=merged)
    ryte_side.channel = SimpleNamespace(id=10, category=SimpleNamespace(name="Ryte The Goat"))
    assert client.talks_to_ryte(ryte_side, CONFIG, RYTE)
    gabi_side = said("test", guild=merged)
    gabi_side.channel = SimpleNamespace(id=11, category=SimpleNamespace(name="Gabi The Best"))
    assert not client.talks_to_ryte(gabi_side, CONFIG, RYTE)
    # A thread under one of Gabi's channels too.
    thread = said("test", guild=merged)
    thread.channel = SimpleNamespace(id=12, category=None,
                                     parent=SimpleNamespace(category=SimpleNamespace(name="Gabi The Best")))
    assert not client.talks_to_ryte(thread, CONFIG, RYTE)


def test_a_tag_in_growth_is_answered_unless_its_on_a_payment(monkeypatch):
    """"respond if tagged here remember?" - "@Ryte are u here" in Growth's
    #trello-manager got silence. A bare tag on a payment still points Ryte at
    the order and says nothing."""
    import asyncio

    answered, noted = [], []
    monkeypatch.setattr(client, "is_direct_mention", lambda message, user: True)
    monkeypatch.setattr(client, "_in_setup_guild", lambda bot, guild: True)
    monkeypatch.setattr(client, "_order_note_posted", lambda bot, message: noted.append(message))

    async def answer(bot, message, **kw):
        answered.append(message)

    monkeypatch.setattr(client, "answer_mention", answer)
    bot = SimpleNamespace(user=SimpleNamespace(id=99))

    def tagged(text, channel):
        return SimpleNamespace(content=text, author=SimpleNamespace(id=5, bot=False), guild=GROWTH, channel=channel)

    here = tagged("<@99> are u here", SimpleNamespace(id=1, name="trello-manager", parent=None))
    asyncio.run(client.WilByteBot.on_message(bot, here))
    assert answered == [here]
    thread = SimpleNamespace(id=2, name="Jackson Ruehl · $152.14", parent=SimpleNamespace(name="💸 | new-payment"))
    on_order = tagged("<@99>", thread)
    asyncio.run(client.WilByteBot.on_message(bot, on_order))
    assert answered == [here] and noted == [on_order]


def test_rytes_own_role_tagged_is_ryte_tagged():
    """"@Ryte trello daycheck" with the @Ryte off the list being Ryte's role -
    no answer. Somebody else's role still isn't Ryte."""
    own = SimpleNamespace(id=777, tags=SimpleNamespace(bot_id=99))
    other = SimpleNamespace(id=888, tags=None)
    message = said("<@&777> trello daycheck", roles=[own])
    message.mentions = []
    assert client.is_direct_mention(message, RYTE)
    assert client.talks_to_ryte(message, CONFIG, RYTE)
    team = said("<@&888> heads up", roles=[other])
    team.mentions = []
    assert not client.is_direct_mention(team, RYTE)
    assert not client.talks_to_ryte(team, CONFIG, RYTE)
