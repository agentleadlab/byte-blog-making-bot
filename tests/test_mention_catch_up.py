"""Mentions typed while RYTE was restarting or offline are answered when he's back.

Discord does not send a bot what it missed. RYTE restarts onto every update,
several times a day, and a mention typed in those seconds was never answered.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from wilbyte import mentionseen
from wilbyte.bot import client

NOW = datetime(2026, 9, 30, 20, 45, tzinfo=timezone.utc)
RYTE = NS(id=999, bot=True)
FRANKLIN = NS(id=1, bot=False, roles=[])
ALEX = NS(id=2, bot=False, roles=[])


def _said(message_id, minutes_ago, text="<@999> trello tags", *, author=FRANKLIN,
          reply_to=None):
    channel = NS(id=50, name="trello-manager")
    return NS(
        id=message_id, content=text, author=author, channel=channel, guild=None,
        created_at=NOW - timedelta(minutes=minutes_ago),
        mention_everyone=False,
        mentions=[RYTE] if "<@999>" in text else [],
        reference=NS(message_id=reply_to) if reply_to else None,
    )


class Channel:
    def __init__(self, messages):
        self.id = 50
        self.messages = messages
        self.asked = []

    async def history(self, **kw):
        self.asked.append(kw)
        for one in self.messages:
            yield one


class Bot:
    def __init__(self):
        self.user = RYTE
        self.gone_since = None
        self.alive_task = None
        self.config = NS(
            secrets=NS(discord_watch_channel_ids=(), discord_channel_ids=(),
                       discord_role_ids=(), ringcentral_channel_id="",
                       ringcentral_shared_channel_id="", discord_sop_channel_ids=()),
            discord=NS(approval_timeout_seconds=1),
        )

    def is_closed(self):
        return True


def _missed(messages, *, since=NOW - timedelta(hours=1), until=NOW):
    found = asyncio.run(client.missed_mentions(
        Bot(), since=since, until=until, channels=[Channel(messages)],
    ))
    return [one.id for one in found]


def test_a_mention_nobody_answered_is_found():
    assert _missed([_said(10, 5), _said(11, 4, "good morning")]) == [10]


def test_one_already_answered_is_not():
    mentionseen.add(10)
    assert _missed([_said(10, 5), _said(12, 3)]) == [12]


def test_one_ryte_replied_to_is_not():
    """Answered before the answering was written down."""
    reply = _said(11, 4, "Reading the board —", author=RYTE, reply_to=10)
    assert _missed([_said(10, 5), reply]) == []


def test_one_typed_again_later_is_answered_once():
    """They got tired of waiting and typed it again - the second one counts."""
    assert _missed([_said(10, 5), _said(11, 1)]) == [11]
    mentionseen.add(11)
    assert _missed([_said(10, 5), _said(11, 1)]) == []


def test_somebody_else_asking_the_same_is_still_answered():
    assert _missed([_said(10, 5), _said(11, 1, author=ALEX)]) == [10, 11]


def test_one_typed_after_ryte_was_back_is_left_to_the_ordinary_way():
    later = _said(11, 1, "<@999> cost")
    assert _missed([_said(10, 5), later], until=NOW - timedelta(minutes=2)) == [10]


def test_typed_again_once_ryte_was_back_is_not_answered_twice():
    """Retyped after the restart, and answered the ordinary way already."""
    assert _missed([_said(10, 5), _said(11, 1)], until=NOW - timedelta(minutes=2)) == []


def test_a_bot_mentioning_ryte_is_not_somebody_asking():
    assert _missed([_said(10, 5, author=NS(id=3, bot=True))]) == []


@pytest.fixture
def answering(monkeypatch):
    done, asked, replies = [], [], []

    async def answer(bot, message):
        done.append(message.id)

    async def ask(bot, message):
        asked.append(message.id)

    async def reply(self, text, **kw):
        replies.append(text)

    monkeypatch.setattr(client, "answer_mention", answer)
    monkeypatch.setattr(client, "_ask_about_missed", ask)
    monkeypatch.setattr(client, "listening_loop", lambda bot: asyncio.sleep(0))
    return done, asked, replies


def _catch_up(monkeypatch, messages, replies):
    channel = Channel(messages)
    for one in messages:
        one.reply = lambda text, _r=replies, **kw: _record(_r, text)

    monkeypatch.setattr(client, "_where_mentions_are_answered", lambda bot, since: [channel])

    async def run():
        await client.mention_catch_up(Bot(), now=NOW)
        await asyncio.sleep(0)

    asyncio.run(run())
    return channel


async def _record(replies, text):
    replies.append(text)


def test_just_missed_is_simply_done(monkeypatch, answering):
    """Typed seconds before an update, and still waiting for the answer."""
    done, asked, replies = answering
    mentionseen.listening(NOW - timedelta(minutes=1))
    _catch_up(monkeypatch, [_said(10, 1)], replies)

    assert done == [10] and asked == []
    assert "restarting" in replies[0]
    assert "10" in mentionseen.answered()


def test_one_from_a_while_ago_is_asked_about_first(monkeypatch, answering):
    """An hour on it may have been done by hand, or not be wanted any more."""
    done, asked, _replies = answering
    mentionseen.listening(NOW - timedelta(hours=2))
    _catch_up(monkeypatch, [_said(10, 90)], [])

    assert done == [] and asked == [10]


def test_the_first_start_ever_reads_nothing_back(monkeypatch, answering):
    """Nothing to go back to - a day of the team's messages would answer things
    long since dealt with."""
    done, asked, _replies = answering
    channel = _catch_up(monkeypatch, [_said(10, 1)], [])

    assert done == asked == [] and channel.asked == []


def test_it_reads_back_from_just_before_it_stopped_listening(monkeypatch, answering):
    mentionseen.listening(NOW - timedelta(minutes=30))
    channel = _catch_up(monkeypatch, [], [])

    assert channel.asked[0]["after"] == NOW - timedelta(minutes=32)


def test_and_never_further_than_half_a_day(monkeypatch, answering):
    """Yesterday's command is not something to start doing today."""
    mentionseen.listening(NOW - timedelta(days=3))
    channel = _catch_up(monkeypatch, [], [])

    assert channel.asked[0]["after"] == NOW - timedelta(hours=client.MENTION_CATCH_UP_HOURS)


def test_a_dropped_connection_reads_back_from_when_it_dropped(monkeypatch, answering):
    mentionseen.listening(NOW - timedelta(minutes=1))
    channel = Channel([])
    monkeypatch.setattr(client, "_where_mentions_are_answered", lambda bot, since: [channel])
    bot = Bot()
    bot.gone_since = NOW - timedelta(minutes=20)
    asyncio.run(client.mention_catch_up(bot, now=NOW))

    assert channel.asked[0]["after"] == NOW - timedelta(minutes=22)
    assert bot.gone_since is None


def test_a_channel_ryte_only_watches_is_never_read_back(monkeypatch):
    """The announcements feed has clients in it. RYTE says nothing there."""
    bot = Bot()
    bot.config.secrets.discord_watch_channel_ids = ("50",)
    assert client._never_speaks_in(bot, NS(id=50, name="announcements", guild=None)) is True
    bot.config.secrets.discord_watch_channel_ids = ()
    bot.config.secrets.discord_channel_ids = ("60",)
    assert client._never_speaks_in(bot, NS(id=50, name="random", guild=None)) is True
    assert client._never_speaks_in(bot, NS(id=60, name="trello", guild=None)) is False


def test_answering_a_mention_writes_it_down(monkeypatch):
    async def handled(bot, message):
        pass

    monkeypatch.setattr(client, "handle_mention", handled)
    asyncio.run(client.answer_mention(Bot(), _said(77, 0)))

    assert "77" in mentionseen.answered()


# ------------------------------------- "no need for ryte to show updates here unless he was tag"


def _updating(monkeypatch, tagged_ago):
    import asyncio
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from wilbyte.bot import client

    said = []

    class Place:
        async def send(self, text=None, **kw):
            # Said as an answer, so it stays where they tagged RYTE.
            assert client.mirror._SPOKEN_TO.get() is True
            said.append(text)

    monkeypatch.setattr(client, "_LAST_TAGGED", [])
    if tagged_ago is not None:
        client._LAST_TAGGED.append((datetime.now(timezone.utc) - timedelta(minutes=tagged_ago), Place()))

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(client.asyncio, "sleep", no_wait)
    monkeypatch.setattr(client.version, "update_waiting", lambda: "abc123 Something new")
    announced = []
    monkeypatch.setattr(client, "_announce_channel", lambda bot: announced.append(1) or Place())

    def leave(code):
        raise SystemExit(code)

    monkeypatch.setattr(client.os, "_exit", leave)

    async def close():
        return None

    bot = SimpleNamespace(is_closed=lambda: False, run_lock=asyncio.Lock(), close=close)
    try:
        asyncio.run(client.updater_loop(bot))
    except SystemExit:
        pass
    return said, announced


def test_an_automatic_update_is_said_nowhere(monkeypatch):
    """"why is it sending the updates here again" - a blog run tagged in
    #blogs-copywriter got "Updating myself" two minutes later."""
    for ago in (None, 2, 30):
        said, announced = _updating(monkeypatch, tagged_ago=ago)
        assert said == [] and announced == []


def test_an_at_ryte_is_answered_as_spoken_to(monkeypatch):
    """So the answer goes where it was asked, Growth server or not."""
    import asyncio
    from types import SimpleNamespace

    from wilbyte.bot import client, mirror

    seen = []

    async def handle(bot, message):
        seen.append(mirror._SPOKEN_TO.get())

    monkeypatch.setattr(client, "handle_mention", handle)
    monkeypatch.setattr(client.mentionseen, "add", lambda said: None)
    asyncio.run(client.answer_mention(None, SimpleNamespace(id=1, channel=None)))
    assert seen == [True] and mirror._SPOKEN_TO.get() is False


def test_updates_are_looked_for_every_couple_of_minutes():
    """"can the update be less than 15 minutes?\""""
    from wilbyte.bot import client

    assert client.UPDATE_CHECK_SECONDS <= 120


@pytest.mark.parametrize("said", ["<@1> update", "<@1> update now", "<@1> restart", "<@1> Update!"])
def test_at_ryte_update_is_a_command(said):
    from wilbyte.bot import mentions

    assert mentions.parse(said).action == "update"


def test_update_in_a_sentence_is_not_the_command():
    from wilbyte.bot import mentions

    assert mentions.parse("<@1> update the card for Jay Rodriguez").action != "update"


def _asking_to_update(monkeypatch, *, waiting, locked=False):
    import asyncio
    from types import SimpleNamespace

    from wilbyte.bot import client

    said, closed = [], []
    monkeypatch.setattr(client.version, "update_waiting", lambda: waiting)
    monkeypatch.setattr(client.version, "code_version", lambda: "abc1234 Oct 08 15:00")

    def leave(code):
        raise SystemExit(code)

    monkeypatch.setattr(client.os, "_exit", leave)

    async def close():
        closed.append(1)

    lock = asyncio.Lock()

    async def go():
        if locked:
            await lock.acquire()

        class Responder:
            async def send(self, text=None, **kw):
                said.append(text)

        await client._update_now(SimpleNamespace(run_lock=lock, close=close), Responder())

    try:
        asyncio.run(go())
    except SystemExit as exc:
        said.append(f"exit {exc.code}")
    return said, closed


def test_update_now_restarts_onto_a_new_version(monkeypatch):
    said, closed = _asking_to_update(monkeypatch, waiting="def5678 Tracker fix")
    assert said[0].startswith("🔄 Updating myself") and closed == [1]
    assert said[-1] == "exit 42"


def test_update_now_with_nothing_new_says_so(monkeypatch):
    said, closed = _asking_to_update(monkeypatch, waiting="")
    assert "Already on the latest" in said[0] and closed == []


def test_update_now_never_restarts_through_an_open_run(monkeypatch):
    said, closed = _asking_to_update(monkeypatch, waiting="def5678 Tracker fix", locked=True)
    assert "a run is open" in said[0] and closed == []


def test_hub_alerts_go_to_the_hub_channel_when_there_is_one():
    from types import SimpleNamespace

    from wilbyte.bot import client

    hub_channel = SimpleNamespace(name="📮｜hub-agent-fulfillment")
    bot = SimpleNamespace(guilds=[SimpleNamespace(text_channels=[SimpleNamespace(name="announcements"), hub_channel])])
    assert client._hub_responder(bot).channel is hub_channel
    assert client._hub_responder(SimpleNamespace(guilds=[SimpleNamespace(text_channels=[])])) is None


@pytest.mark.parametrize("name", ["📮｜hub-agent-fulfillment", "📮┃𝗁𝗎𝖻-𝖺𝗀𝖾𝗇𝗍-𝖿𝗎𝗅𝖿𝗂𝗅𝗅𝗆𝖾𝗇𝗍", "hub_agent_fulfilment"])
def test_the_hub_channel_is_found_however_its_name_is_written(name, monkeypatch):
    """"why is it updating here, iwant it here" - the alert went to
    #trello-manager when the channel's name wasn't plain letters."""
    from types import SimpleNamespace

    from wilbyte.bot import client

    monkeypatch.delenv("DISCORD_HUB_CHANNEL_ID", raising=False)
    hub_channel = SimpleNamespace(name=name)
    bot = SimpleNamespace(guilds=[SimpleNamespace(text_channels=[SimpleNamespace(name="trello-manager"), hub_channel])])
    assert client._hub_responder(bot).channel is hub_channel
