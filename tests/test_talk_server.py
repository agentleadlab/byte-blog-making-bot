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
